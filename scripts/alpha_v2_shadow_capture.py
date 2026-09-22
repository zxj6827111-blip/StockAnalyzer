"""Alpha V2 M3 + M4-L：T 日 Shadow 预测快照采集。

**M4-L 起 cohort 双口径（验证模式决定，不可混用）**：

- ``validation_mode=production``：cohort **只能**是当天真实生产漏斗
  （``production_funnel`` 按日工件，写方 = Week5 夜扫 + 晚报发布链接）。
  ``research_proxy`` 在生产模式被直接拒绝（exit 10）；funnel 缺失/日期不符/
  契约不符/hash 篡改/未链接正式报告同样 exit 10（fail-closed）。
- ``validation_mode=rehearsal``：默认 ``research_proxy``（Alpha 自建 Top50，
  行恒 clean_oos_eligible=false）；也可用 ``--cohort-source production_funnel``
  做端到端排演（证据字段全真，clean 资格仍被 rehearsal 模式一票否决）。

```bash
python scripts/alpha_v2_shadow_capture.py \
    --epoch-id alpha_v2_epoch_001 \
    --signal-date 2026-03-31 \
    --market-db artifacts/warehouse/market.duckdb
```

纪律（代码内执行而非口头）：

- 每一天的预测只能写一次；重跑同内容幂等，改内容直接失败（ShadowTamperError）；
- 没有合法校准的方向分写 ``not_available``（不制造伪概率）；
- 三个 rank 的权威来源不同：``quality_rank/light_rank/deep_rank`` = 生产漏斗名次，
  ``alpha_rank`` = Alpha 在真实 cohort 内的独立排序分——生产 rank 绝不由 Alpha
  推导，Alpha rank 也绝不冒充生产 rank；
- T 日快照只允许 T 日写：``--allow-backfill`` 显式打开后，行落
  ``backfilled=true`` + ``clean_oos_eligible=false``；
- 运行身份（BLK-D2 修复）：``code_commit`` 来自 ``resolve_runtime_code_identity``
  ——源码检出用 git HEAD，不可变容器用镜像构建身份（.build_commit ==
  build_manifest.commit）。**不再**直接取 ``git_head()``（容器里恒为 unknown）。
"""

from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from stock_analyzer.alpha_v2.dual_price_series import (  # noqa: E402
    FEATURE_PRICE_SERIES_EVIDENCE_SCHEMA,
    PriceSeriesContractError,
    certification_evidence_block,
    feature_mode_of_frozen_model,
    is_live_strict_mode,
    require_declared_feature_series,
)
from stock_analyzer.alpha_v2.research.feature_audit import safe_feature_columns  # noqa: E402
from stock_analyzer.alpha_v2.research.outcomes import DecisionPoint, OutcomeSpec  # noqa: E402
from stock_analyzer.alpha_v2.research.panel import (  # noqa: E402
    load_daily_panel,
    panel_fingerprint,
)
from stock_analyzer.alpha_v2.research.simple_baseline import (  # noqa: E402
    BASELINE_SCORE_COLUMN,
    compute_simple_baseline,
)
from stock_analyzer.alpha_v2.validation.data_health_capture import (  # noqa: E402
    capture_data_health_block,
    load_data_health_artifact,
)
from stock_analyzer.alpha_v2.validation.epoch import (  # noqa: E402
    EpochRegistryError,
    active_epoch,
    epoch_identity_matches,
    get_epoch,
    require_epoch_identity_match,
)
from stock_analyzer.alpha_v2.validation.feature_frame import daily_feature_frame  # noqa: E402
from stock_analyzer.alpha_v2.validation.freeze import (  # noqa: E402
    label_policy_payload,
    load_validation_freeze,
)
from stock_analyzer.alpha_v2.validation.freeze_precheck import (  # noqa: E402
    FreezeGateError,
    assert_model_training_commit,
    assert_runtime_identity,
)
from stock_analyzer.alpha_v2.validation.frozen_model import (  # noqa: E402
    FrozenModelError,
    load_frozen_model,
    predict_frozen_model_matrix,
)
from stock_analyzer.alpha_v2.validation.production_funnel import (  # noqa: E402
    FUNNEL_SOURCE,
    FunnelError,
    funnel_snapshot_path,
    load_funnel_snapshot,
    verify_funnel_for_capture,
)
from stock_analyzer.alpha_v2.validation.runtime_identity import (  # noqa: E402
    config_hash_of,
    price_contract_block,
    resolve_runtime_code_identity,
)
from stock_analyzer.alpha_v2.validation.shadow_capture import (  # noqa: E402
    ShadowCaptureError,
    build_shadow_rows,
    clean_oos_row_eligible,
    deep50_position_records,
    frozen_wall_clock,
    wall_clock_is_injected,
    wall_clock_now,
    write_shadow_day_manifest,
    write_shadow_snapshot,
)
from stock_analyzer.config import load_config  # noqa: E402
from stock_analyzer.config_identity import stable_payload_hash  # noqa: E402
from stock_analyzer.contracts.alpha_v2 import resolve_selection_contract  # noqa: E402


@contextmanager
def _deterministic_clock(day: date | None):
    """rehearsal/CI 专用：把墙上时钟固定到 ``day`` 21:45（生产模式走不到这里）。"""
    if day is None:
        yield
        return
    zone = datetime.now().astimezone().tzinfo
    with frozen_wall_clock(datetime.combine(day, time(21, 45), tzinfo=zone)):
        yield


def _as_float_or_neg_inf(value: object) -> float:
    try:
        numeric = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("-inf")
    if numeric != numeric:  # NaN 视作最低分
        return float("-inf")
    return numeric


def _signal_close_lookup(panel: object, signal_date: date) -> dict[str, float]:
    """面板当日 close（raw 口径，特征面板同口径）→ {symbol: close}。

    缺失即空 dict（行上如实标 not_available，不伪造可成交）。
    """
    bars = getattr(panel, "bars", None)
    if bars is None or not hasattr(bars, "columns"):
        return {}
    try:
        day_rows = bars.loc[bars["trade_date"] == pd.Timestamp(signal_date)]
    except Exception:  # noqa: BLE001 - 预览字段绝不阻塞捕获主流程
        return {}
    if "close" not in day_rows.columns or "symbol" not in day_rows.columns:
        return {}
    result: dict[str, float] = {}
    for row in day_rows[["symbol", "close"]].to_dict("records"):
        try:
            close = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if close != close or close <= 0:
            continue
        result[str(row.get("symbol"))] = close
    return result


def _production_cohort_records(
    work: pd.DataFrame, cohort_view: dict[str, object]
) -> list[dict[str, object]]:
    """把预测帧按**生产 Deep50 顺序**重排，并把生产三级 rank 钉到每行。

    Alpha 的预测只做"值"的来源；成员名单与名次一律以 funnel 工件为准——
    预测帧里多出的 symbol（理论不该有）被丢弃，缺的 symbol 直接缺行
    （特征链路问题如实暴露，不让影子行数凑数）。
    """
    by_symbol = {str(row.get("symbol")): row for row in work.to_dict(orient="records")}
    deep_members = list(cohort_view.get("deep_members", []))
    quality_ranks = {
        str(m.get("symbol")): m.get("rank") for m in cohort_view.get("quality_members", [])
    }
    light_ranks = {
        str(m.get("symbol")): m.get("rank") for m in cohort_view.get("light_members", [])
    }
    ordered: list[dict[str, object]] = []
    for member in deep_members:
        symbol = str(member.get("symbol", "")).strip()
        row = by_symbol.get(symbol)
        if not symbol or row is None:
            continue
        record = dict(row)
        record["quality_rank"] = quality_ranks.get(symbol)
        record["light_rank"] = light_ranks.get(symbol)
        record["deep_rank"] = int(member.get("rank"))
        ordered.append(record)
    return ordered


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alpha V2 M3：每日 Shadow 快照采集")
    parser.add_argument("--epoch-id", default="")
    parser.add_argument("--signal-date", required=True)
    parser.add_argument(
        "--market-db",
        default="artifacts/warehouse/market.duckdb",
        help=(
            "**feature 侧**行情库（capture 只用它算特征；生产走 "
            "alpha_v2.feature_market_db，为空时回退 market_warehouse.db_path）。"
            "当天会校验它的价格口径 == 冻结模型声明的 feature 口径，不一致即 exit 11"
        ),
    )
    parser.add_argument("--warmup-days", type=int, default=260)
    parser.add_argument("--model-dir", default="")
    parser.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "alpha_v2"))
    parser.add_argument("--config", default=str(REPO_ROOT / "config" / "default.yaml"))
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument(
        "--capture-date",
        default="",
        help=("确定性写入日（**仅 rehearsal/CI**；生产模式一律拒绝——生产写入日 = 系统真实日期）"),
    )
    parser.add_argument(
        "--data-health",
        default="",
        help=(
            "data_health 工件路径（S08 契约的 JSON）；缺省按候选路径自动查找，"
            "读不到就如实写 not_available（快照照写，但不进 clean OOS）"
        ),
    )
    parser.add_argument(
        "--allow-backfill",
        action="store_true",
        help="显式允许事后补写历史日期（行会落 backfilled=true / clean_oos_eligible=false）",
    )
    parser.add_argument(
        "--backfill-reason",
        default="",
        help="补写原因（missing day 情形必填；生产操作手册应注明记录位置）",
    )
    parser.add_argument(
        "--cohort-source",
        choices=("auto", "research_proxy", "production_funnel"),
        default="auto",
        help=(
            "影子 cohort 来源：production_funnel=真实生产漏斗快照（Quality300/Light100/Deep50"
            " 按日工件）；research_proxy=Alpha 自建 Top50（仅 rehearsal/test，永不算 clean OOS）。"
            "auto=production epoch 强制 production_funnel，其余模式回退 research_proxy"
        ),
    )
    parser.add_argument(
        "--funnel-root",
        default="",
        help="生产漏斗工件根目录（默认取 config 的 alpha_v2.production_funnel_root）",
    )
    parser.add_argument(
        "--report-root",
        default="",
        help="正式晚报根目录（复核 source_artifact sha256；默认 config nightly.reports_root）",
    )
    parser.add_argument(
        "--quality-pool-source",
        default="",
        help=(
            "质量池口径标记（留空=按 cohort-source 自动派生）。production epoch 下"
            "必须为空或 production_selection_engine——写别的字符串会被当作伪造来源拒绝"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    signal_date = date.fromisoformat(args.signal_date)
    config = load_config(Path(args.config))
    root = Path(args.out)

    epoch = get_epoch(root, args.epoch_id) if args.epoch_id else active_epoch(root)
    if epoch is None:
        print(
            f"[shadow] epoch 未找到或未开启: {args.epoch_id or '(未指定, active=None)'}",
            file=sys.stderr,
        )
        return 2

    # 第一道闸：磁盘冻结清单必须仍是 epoch 锚定的那份（在加载面板之前失败）
    try:
        require_epoch_identity_match(root=root, epoch_id=epoch.epoch_id)
    except EpochRegistryError as exc:
        print(f"[shadow] 冻结锚定失败: {exc}", file=sys.stderr)
        return 3
    freeze = load_validation_freeze(root)
    assert freeze is not None  # require_epoch_identity_match 已保证
    validation_mode = str(freeze.get("validation_mode", "production"))

    # ── 写入窗口（R3 / BLK-R2-1）：写入日 = 真实墙钟，生产不接受任何"自称"日期 ─────
    wall_date = wall_clock_now().date()
    requested_capture = date.fromisoformat(args.capture_date) if args.capture_date else None
    if requested_capture is not None and validation_mode == "production":
        print(
            f"[shadow] 拒绝：本 epoch 是 production（deterministic_clock="
            f"{freeze.get('deterministic_clock', False)}），不允许 --capture-date "
            f"（收到 {requested_capture}）。生产写入日恒等于系统真实日期 {wall_date}；"
            "需要补历史请用 --allow-backfill（会落 backfilled=true / clean_oos_eligible=false）",
            file=sys.stderr,
        )
        return 8
    if requested_capture is not None:
        print(
            f"[shadow] 注意：确定性写入日 {requested_capture}（validation_mode={validation_mode}）"
            "——该 epoch 永不进 clean OOS",
            file=sys.stderr,
        )
    run_date = requested_capture or wall_date
    is_backfill = signal_date != run_date
    if is_backfill:
        if not args.allow_backfill:
            print(
                f"[shadow] 拒绝：signal_date={signal_date} != 真实写入日 {run_date}。"
                "默认禁止事后补写；确需补写加 --allow-backfill 并给出 --backfill-reason",
                file=sys.stderr,
            )
            return 8
        if not str(args.backfill_reason or "").strip():
            print("[shadow] 拒绝：--allow-backfill 必须同时给 --backfill-reason", file=sys.stderr)
            return 8

    # ── M4-L  cohort 来源规则（纯参数判定；工件校验在身份门之后、面板之前）────
    # production epoch：cohort 只能来自当天真实生产漏斗（按日工件 + hash/link 校验）。
    # rehearsal/test：默认 research_proxy（Alpha 自建 Top50，永不 clean）；也可显式
    # 指定 production_funnel 做端到端排演（行落 production 证据，但 clean 资格仍被
    # validation_mode 一票否决——见 clean_oos_row_eligible）。
    requested_cohort = str(args.cohort_source).strip().lower()
    if validation_mode == "production":
        use_production_funnel = True
        if requested_cohort == "research_proxy":
            print(
                "[shadow] 拒绝：production epoch 禁止 research_proxy cohort"
                "（M4-L §8：生产模式 research_proxy fallback = FORBIDDEN）",
                file=sys.stderr,
            )
            return 10
    else:
        use_production_funnel = requested_cohort == "production_funnel"
    quality_pool_source_arg = str(args.quality_pool_source or "").strip()
    if use_production_funnel:
        if quality_pool_source_arg and quality_pool_source_arg != FUNNEL_SOURCE:
            print(
                f"[shadow] 拒绝：production funnel 模式下 --quality-pool-source 必须是"
                f" {FUNNEL_SOURCE}（收到 {quality_pool_source_arg!r}；写别的字符串"
                "就是 Attack A 的那类伪造）",
                file=sys.stderr,
            )
            return 10
        quality_pool_source = FUNNEL_SOURCE
    else:
        quality_pool_source = quality_pool_source_arg or "research_proxy:alpha_v2_quality_v1"

    # 第二道闸的第一步（BLK-D2）：**运行身份**——只依赖代码身份，不依赖模型/面板，
    # 所以放在最前面 fail-fast。code_commit 不能再取 git_head(REPO_ROOT)（容器里恒为
    # unknown）；走与 freeze 同一个 resolver：git_checkout 用 HEAD，容器用构建身份。
    try:
        runtime_identity_resolved = resolve_runtime_code_identity(
            REPO_ROOT, validation_mode=validation_mode
        )
        runtime_code_commit = assert_runtime_identity(
            runtime_identity_resolved, validation_mode=validation_mode
        )
    except FreezeGateError as exc:
        print(f"[shadow] 运行身份硬门未通过: {exc}", file=sys.stderr)
        return 3

    # ── R4.1：模型训练身份绑定（本 epoch 冻结值 == 当前运行身份）────────────────
    # 读的是**磁盘冻结清单**里由 freeze_manifest_hash 锚定的
    # model.model_training_code_commit（epoch 成立的前提条件已在 freeze 阶段强制过一次，
    # 这里是每日写入路径上的复查：epoch 被换/清单被改/跑的是另一份代码都拦下）。
    try:
        assert_model_training_commit(
            model_training_code_commit=str(
                (freeze.get("model") or {}).get("model_training_code_commit", "")
            ),
            runtime_code_commit=runtime_code_commit,
            validation_mode=validation_mode,
        )
    except FreezeGateError as exc:
        print(f"[shadow] 模型训练身份硬门未通过: {exc}", file=sys.stderr)
        return 3

    # ── 当天 data_health（N-R2-1）：读 S08 契约工件；读不到 = not_available（不阻塞写）──
    data_health_payload, data_health_source = load_data_health_artifact(
        path=(args.data_health or None), root=root
    )
    data_health_block = capture_data_health_block(
        signal_date=signal_date, payload=data_health_payload, source=data_health_source
    )
    print(
        f"[shadow] data_health: status={data_health_block['status']} "
        f"(source_status={data_health_block['source_status']}, "
        f"as_of={data_health_block['as_of']}, source={data_health_source})"
    )

    model_dir = (
        Path(args.model_dir)
        if args.model_dir
        else Path(args.out) / "model" / str((freeze or {}).get("model", {}).get("model_id") or "")
    )
    if not model_dir.exists():
        print(f"[shadow] 冻结模型工件不存在: {model_dir}（先跑 alpha_v2_shadow_model_freeze.py）")
        return 4
    try:
        model = load_frozen_model(
            model_dir,
            expected_artifact_hash=str(epoch.identity.get("model_artifact_hash", "")) or None,
            # R1.1：生产 epoch 的模型必须已封存训练 provenance（v2）。freeze 阶段
            # 已挡过一次，这里是每日写入路径上的复查——保证 epoch 存活期内这份模型
            # 不会被换成"训练输入身份可事后改写"的形态。
            require_sealed_provenance=(validation_mode == "production"),
        )
    except FrozenModelError as exc:
        print(f"[shadow] 冻结模型校验失败: {exc}", file=sys.stderr)
        return 5

    # ── 冻结 feature 价格口径（P0 Final R1 / BLOCKER 1）：权威来源 = 模型工件本身 ────
    # train feature=qfq 而当天 feature 库变成 raw/unknown 时，特征语义已换——但旧实现
    # 会照常预测并写快照。这里先取"训练时冻结的口径"，稍后在面板加载后逐日复核。
    live_strict = is_live_strict_mode(validation_mode)
    expected_feature_mode = feature_mode_of_frozen_model(model.manifest)
    if live_strict and not expected_feature_mode:
        print(
            "[shadow] 拒绝：冻结模型未声明 feature 价格口径"
            "（provenance.feature_data_identity.price_series_mode 缺失）——"
            "无法证明当天特征与训练同口径；生产/测试模式一律 fail closed",
            file=sys.stderr,
        )
        return 11

    # 第二道闸的第二步：运行身份全键核验（8 键严格缺失判违例）——需要 model 已载入
    contract = resolve_selection_contract(config, profile="night_scan")
    price = price_contract_block(config)
    runtime_identity = {
        "code_commit": runtime_code_commit,
        "config_hash": config_hash_of(config),
        "model_id": str(model.manifest.get("model_id", "")),
        "model_artifact_hash": str(model.manifest.get("artifact_hash", "")),
        "feature_schema_hash": str(model.manifest.get("feature_schema_hash", "")),
        "label_policy_hash": stable_payload_hash(label_policy_payload(OutcomeSpec())),
        "selection_contract_id": str(
            contract.to_payload().get("selection_contract_id", "night_alpha_v2_v1")
        ),
        "execution_price_mode": str(price["execution_price_mode"]),
    }
    violations = epoch_identity_matches(epoch, runtime_identity)
    if violations:
        print(f"[shadow] 运行身份与 epoch 冻结身份不符: {violations}", file=sys.stderr)
        return 3

    funnel_payload: dict[str, object] | None = None
    cohort_view: dict[str, object] | None = None
    funnel_file: Path | None = None

    if use_production_funnel:
        funnel_root = str(args.funnel_root or config.alpha_v2.production_funnel_root)
        report_root = str(args.report_root or config.nightly.reports_root)
        funnel_file = funnel_snapshot_path(funnel_root, signal_date)
        try:
            funnel_payload = load_funnel_snapshot(funnel_file)
            cohort_view = verify_funnel_for_capture(
                funnel_payload,
                signal_date=signal_date,
                selection_contract_id=str(freeze.get("selection_contract_id", "")),
                # 生产 epoch 要求 funnel 已链接正式晚报（不可变证据链末梢）；
                # rehearsal/test 允许未链接工件（排演环境没有真实发布流程）。
                require_linked_report=(validation_mode == "production"),
                report_root=report_root,
                funnel_root=funnel_root,
            )
        except FunnelError as exc:
            print(f"[shadow] 生产漏斗硬门未通过: {exc}", file=sys.stderr)
            return 10
        print(
            f"[shadow] 生产漏斗已验证: {funnel_file} "
            f"(quality={funnel_payload['quality_count']} "
            f"light={funnel_payload['light_count']} "
            f"deep={funnel_payload['deep_count']} "
            f"report={funnel_payload.get('night_scan_report_id') or 'unlinked'})"
        )

    # window 就是 signal_date 这一天；warmup 提供滚动特征的可见历史
    panel = load_daily_panel(
        market_db=REPO_ROOT / args.market_db,
        window_start=signal_date,
        window_end=signal_date,
        warmup_days=int(args.warmup_days),
        max_symbols=int(args.max_symbols),
    )

    if signal_date not in panel.calendar:
        print(f"[shadow] {signal_date} 不是面板交易日（或行情未到位）", file=sys.stderr)
        return 6

    # ── 当天 feature 面板口径复核（必须在 pit_universe / 特征 / 预测 / 写盘之前）────
    # 判据复用唯一实现 require_declared_feature_series：口径必须**可证**且等于冻结值。
    # 注意 feature 侧不要求 certified（qfq 的 certified 本来就是 False）——那是
    # execution 侧的标准，混用会把正确的 qfq 误判成失败。
    feature_certification = panel.certify_price_mode(min_sample=1000)
    feature_contract_ok = False
    feature_contract_reason = ""
    try:
        require_declared_feature_series(
            feature_certification,
            context=f"capture(signal_date={signal_date.isoformat()})",
            expected_mode=expected_feature_mode,
            db=str(args.market_db),
        )
        feature_contract_ok = bool(expected_feature_mode)
        if not expected_feature_mode:
            feature_contract_reason = (
                "expected_feature_mode_missing:冻结模型未声明 feature 口径（rehearsal 未强制）"
            )
    except PriceSeriesContractError as exc:
        feature_contract_reason = str(exc)
        if live_strict:
            print(f"[shadow] 拒绝：feature 价格口径契约未通过: {exc}", file=sys.stderr)
            return 11
        print(
            f"[shadow] 警告：{exc}（validation_mode={validation_mode}，"
            "本 epoch 永不进 clean OOS；证据已落当日清单）",
            file=sys.stderr,
        )
    feature_price_series_evidence = {
        "schema": FEATURE_PRICE_SERIES_EVIDENCE_SCHEMA,
        "expected_mode": expected_feature_mode,
        "expected_mode_declared": bool(expected_feature_mode),
        "observed_mode": str(feature_certification.mode),
        "mode_match": bool(expected_feature_mode)
        and str(feature_certification.mode) == expected_feature_mode,
        "certification_source": str(feature_certification.source),
        "certification_evidence": certification_evidence_block(feature_certification),
        "source_db": str(args.market_db),
        "contract_ok": bool(feature_contract_ok),
        "enforced": bool(live_strict),
        "reason": feature_contract_reason,
        "validation_mode": validation_mode,
        "checked_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
    }
    print(
        f"[shadow] feature 价格口径: expected={expected_feature_mode or '(未声明)'} "
        f"observed={feature_certification.mode} contract_ok={feature_contract_ok} "
        f"(enforced={live_strict})"
    )

    snapshot = panel.pit_universe(as_of=signal_date)
    eligible = list(snapshot.eligible_symbols)
    if use_production_funnel and cohort_view is not None:
        # M4-L：Alpha 只给"生产系统当天真实选出的 Deep50"打分——cohort 不再由
        # Alpha 自己从全市场挑。deep 成员顺序即生产 deep_rank（1..N）。
        deep_members = list(cohort_view["deep_members"])  # type: ignore[index]
        cohort_symbols = [str(m["symbol"]) for m in deep_members]
        decisions = [DecisionPoint(symbol, signal_date) for symbol in cohort_symbols]
    else:
        decisions = [DecisionPoint(symbol, signal_date) for symbol in eligible]

    raw = daily_feature_frame(panel, decisions)
    if raw.empty:
        print("[shadow] 特征帧为空", file=sys.stderr)
        return 7
    safe = list(
        safe_feature_columns([c for c in raw.columns if c not in {"decision_date", "symbol"}])
    )
    matrix_frame = raw[["decision_date", "symbol", *safe]].copy()
    if matrix_frame.empty:
        print("[shadow] 特征矩阵为空（确认行情/特征链路上游）", file=sys.stderr)
        return 7
    predictions = predict_frozen_model_matrix(model, matrix_frame)
    work = matrix_frame.merge(predictions, on=["decision_date", "symbol"], how="left")

    # 简单基线分数（S15 冻结因子组，可解释对照）
    baseline = compute_simple_baseline(panel=panel, decisions=decisions)
    baseline_map = (
        baseline.frame[["decision_date", "symbol", BASELINE_SCORE_COLUMN]].copy()
        if not baseline.frame.empty
        else pd.DataFrame(columns=["decision_date", "symbol", BASELINE_SCORE_COLUMN])
    )
    baseline_map["decision_date"] = baseline_map["decision_date"].astype(str)
    work = work.copy()
    work["decision_date"] = work["decision_date"].astype(str)
    work = work.merge(baseline_map, on=["decision_date", "symbol"], how="left")

    # 候选集构建按 cohort 来源分岔：
    # - production_funnel：cohort = 真实生产 Deep50（生产顺序），Alpha 只做内部排序；
    # - research_proxy（仅 rehearsal/test）：Alpha 自建 Top50（M3 研究口径原样保留）。
    work = work[work["decision_date"].astype(str) == signal_date.isoformat()]
    if use_production_funnel and cohort_view is not None:
        prod_records = _production_cohort_records(work, cohort_view)
        if not prod_records:
            print(
                "[shadow] 生产 Deep50 全部无法产出特征/预测行（特征链路问题），写 0 行 = 伪造；"
                "当日应记 missing day 而非快照",
                file=sys.stderr,
            )
            return 7
        deep50_records = prod_records
    else:
        # Deep50 = alpha_rank 最高的 50 只可成交候选（Shadow 无阈值、允许不足 50）。
        deep50_records = deep50_position_records(work)  # 库内统一口径（含 1..N 名次）
    # v2_top1/3/5：Alpha 分数在 cohort 内的独立排序产物（与生产 deep_rank 完全分离）。
    alpha_ordered = sorted(
        deep50_records,
        key=lambda r: (
            -_as_float_or_neg_inf(r.get("alpha_rank_score")),
            str(r.get("symbol", "")),
        ),
    )
    top5_symbols = {str(r.get("symbol")) for r in alpha_ordered[:5]}
    top3_symbols = {str(r.get("symbol")) for r in alpha_ordered[:3]}
    top1_symbols = {str(r.get("symbol")) for r in alpha_ordered[:1]}

    # 质量池来源：真实生产漏斗（M4-L 起生产唯一合法来源）或研究代理（如实标记）。
    identity = {
        "code_commit": runtime_code_commit,
        "config_hash": config_hash_of(config),
        "model_id": model.model_id,
        "model_artifact_hash": str(model.manifest.get("artifact_hash", "")),
        "model_created_at": str(model.manifest.get("created_at", "")),
        "universe_snapshot_id": (
            f"pit:{signal_date.isoformat()}:{len(eligible)}:{panel_fingerprint(panel)}"
        ),
        "data_snapshot_id": f"{panel.source}@{panel_fingerprint(panel)}",
        "selection_contract_id": runtime_identity["selection_contract_id"],
    }

    close_by_symbol = _signal_close_lookup(panel, signal_date)
    rows = []
    for record in deep50_records:
        symbol = str(record.get("symbol"))
        if use_production_funnel:
            # 生产漏斗语义：成员资格与各级 rank 一律来自当天真实生产结果；
            # Alpha 只贡献 alpha_rank（分数）与 v2_top1/3/5（cohort 内排序）。
            deep_rank = int(record["deep_rank"])
            quality_rank = record.get("quality_rank")
            light_rank = record.get("light_rank")
            deep_rank_pct = (
                round(deep_rank / max(1, int(funnel_payload["deep_count"])), 6)
                if funnel_payload
                else None
            )
        else:
            # 研究代理：ranks 由 Alpha 自建 Top50 名次给出（M1-M3 研究口径保持不变）
            deep_rank = int(record["deep_rank"])
            quality_rank = None
            light_rank = None
            deep_rank_pct = record["deep_rank_pct"]
        # signal_close_raw / fillable：有当日日线 = 名义可成交预览（与原 M3 占位口径
        # 的区别在于现在用真实行情判定；缺失即 not_available，不硬塞 True）。
        close = close_by_symbol.get(symbol)
        fillable = close is not None
        clean_flag = clean_oos_row_eligible(
            backfilled=is_backfill,
            data_health=data_health_block,  # R3：结构化同日 data_health（缺失 = not_available）
            execution_price_mode=str(price["execution_price_mode"]),
            validation_mode=validation_mode,
        )
        rows.append(
            {
                "symbol": symbol,
                # 生产漏斗模式：verify 已保证 Deep ⊆ Light ⊆ Quality，三个 True
                # 是嵌套证明的结论而非默认填充；research 代理保持 M3 旧口径。
                "in_quality_pool": True,
                "in_light_pool": True,
                "in_deep_pool": True,
                "quality_pool_source": quality_pool_source,
                "quality_rank": quality_rank,
                "light_rank": light_rank,
                "deep_rank": deep_rank,
                "deep_rank_pct": deep_rank_pct,
                "alpha_rank": record.get("alpha_rank_score"),
                "direction_score_3d": record.get("p_up_net_3d"),
                "direction_score_5d": record.get("p_up_net_5d"),
                "p_up_3d": record.get("p_up_net_3d_calibrated"),
                "p_up_5d": record.get("p_up_net_5d_calibrated"),
                "p_up_calibration": str(record.get("direction_calibration", "none")),
                "expected_net_return_3d": record.get("expected_net_return_3d"),
                "expected_net_return_5d": record.get("expected_net_return_5d"),
                "expected_excess_return_3d": record.get("expected_excess_return_3d"),
                "expected_excess_return_5d": record.get("expected_excess_return_5d"),
                "risk_score": record.get("p_mae_le_5pct_5d"),
                "expected_mae_5d": record.get("expected_mae_5d"),
                "fillable": fillable,
                "fillability_note": (
                    "preview_from_daily_bar_presence"
                    if close is not None
                    else "no_daily_bar_on_signal_date"
                ),
                "signal_close_raw": close,
                "baseline_score": record.get(BASELINE_SCORE_COLUMN),
                "v2_top1": symbol in top1_symbols,
                "v2_top3": symbol in top3_symbols,
                "v2_top5": symbol in top5_symbols,
                "data_health": data_health_block,
                "backfilled": bool(is_backfill),
                "backfill_reason": (str(args.backfill_reason).strip() if is_backfill else ""),
                "clean_oos_eligible": bool(clean_flag),
            }
        )

    # 行与写入必须共享同一个时钟：rehearsal/CI 的确定性写入日在这里生效，
    # 生产模式（requested_capture is None）时钟就是系统真实时间。
    with _deterministic_clock(requested_capture):
        rows_written = build_shadow_rows(
            signal_date=signal_date,
            signal_time="15:35",
            epoch=epoch,
            candidates=rows,
            identity=identity,
        )
        try:
            path = write_shadow_snapshot(
                root=root,
                epoch=epoch,
                signal_date=signal_date,
                rows=rows_written,
                allow_backfill=bool(args.allow_backfill),
                backfill_reason=str(args.backfill_reason),
            )
        except ShadowCaptureError as exc:
            print(f"[shadow] 写入被拒绝: {exc}", file=sys.stderr)
            return 9
    write_shadow_day_manifest(
        root=root,
        epoch=epoch,
        signal_date=signal_date,
        payload={
            "schema": "alpha_v2_shadow_day_manifest.v1",
            "validation_epoch_id": epoch.epoch_id,
            "signal_date": signal_date.isoformat(),
            "captured_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
            "panel_fingerprint": panel_fingerprint(panel),
            "backfilled": bool(is_backfill),
            "backfill_reason": (str(args.backfill_reason).strip() if is_backfill else ""),
            "validation_mode": validation_mode,
            # R3：真实写入日 / 时钟来源 / 当天 data_health（全部进审计清单）
            "capture": {
                "actual_capture_date": run_date.isoformat(),
                "wall_clock_used": run_date.isoformat(),
                "deterministic_clock": bool(
                    wall_clock_is_injected() and requested_capture is not None
                ),
                "clock_source": (
                    "arg_capture_date" if requested_capture is not None else "system_clock"
                ),
                "signal_date": signal_date.isoformat(),
                "backfilled": bool(is_backfill),
            },
            "data_health": dict(data_health_block),
            # P0 Final R1：当天 feature 价格口径证据——以后任何一个 L20/L60/L120/L250
            # 日都能直接回答"当天模型看到的 feature price mode 是什么、是否等于冻结值"。
            "feature_price_series": dict(feature_price_series_evidence),
            # M4-L §11：当日清单必须一眼回答"这一天凭什么被算成（或不算成）Clean OOS"
            "selection_contract_id": identity["selection_contract_id"],
            "code_commit": identity["code_commit"],
            "execution_price_mode": str(price["execution_price_mode"]),
            "production_funnel_source": (
                quality_pool_source if use_production_funnel else "research_proxy"
            ),
            # 运行身份来源（BLK-D2）：容器形态与源码检出形态在此可区分
            "runtime_identity": runtime_identity_resolved.to_payload(),
            "counts": {
                "eligible": len(eligible),
                "deep50": int(len(deep50_records)),
                "top1": int(len(top1_symbols)),
                "top3": int(len(top3_symbols)),
                "top5": int(len(top5_symbols)),
                "written_rows": len(rows_written),
                # M4-L：完整生产漏斗规模（与 cohort 行数并列，一眼可辨"截断/同集"）
                **(
                    {
                        "production_quality": int(funnel_payload["quality_count"]),
                        "production_light": int(funnel_payload["light_count"]),
                        "production_deep": int(funnel_payload["deep_count"]),
                    }
                    if funnel_payload is not None
                    else {}
                ),
            },
            # M4-L：funnel 工件**原文整体**嵌入（含 hash 字段本身）——KPI 治理层
            # 据此复算 funnel_snapshot_hash 并逐字段复核 cohort，不依赖 runtime
            # 目录里可能被后续流程覆盖的文件；cohort 语义与工件哈希绑死。
            **(
                {
                    "funnel": dict(funnel_payload),
                    "cohort_source": "production_funnel",
                }
                if funnel_payload is not None
                else {"cohort_source": "research_proxy"}
            ),
            "model": {
                "model_id": model.model_id,
                "artifact_hash": model.manifest.get("artifact_hash", ""),
            },
            "price_contract": price_contract_block(config),
            "notes": (
                "M4-L production epoch：cohort=真实生产 Deep50（funnel 工件），"
                "三级 rank 全部来自生产；Alpha 仅贡献 alpha_rank 分数与 v2_top1/3/5。"
                if use_production_funnel
                else "研究链路离线版（research_proxy）：成员标记为研究代理，"
                "该来源永恒 clean_oos_eligible=false，不得进入生产"
            ),
        },
    )
    print(f"[shadow] 已写: {path}（rows={len(rows_written)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
