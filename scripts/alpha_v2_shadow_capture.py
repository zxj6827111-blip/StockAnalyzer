"""Alpha V2 M3：T 日 Shadow 预测快照采集（研究链路离线版）。

生产形态（部署授权后）：在 nightly 扫描完成后接入同一函数。当前离线版：

```bash
python scripts/alpha_v2_shadow_capture.py \
    --epoch-id alpha_v2_epoch_001 \
    --signal-date 2026-03-31 \
    --market-db artifacts/warehouse/market.duckdb
```

纪律（代码内执行而非口头）：

- 每一天的预测只能写一次；重跑同内容幂等，改内容直接失败（ShadowTamperError）；
- 没有合法校准的方向分写 ``not_available``（不制造伪概率）；
- 候选缺 quality/light/deep 标记时成员字段仍是 not_available——不猜；
- T 日快照只允许 T 日写：``--allow-backfill`` 显式打开后，行落
  ``backfilled=true`` + ``clean_oos_eligible=false``。
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
from stock_analyzer.alpha_v2.validation.frozen_model import (  # noqa: E402
    FrozenModelError,
    load_frozen_model,
    predict_frozen_model_matrix,
)
from stock_analyzer.alpha_v2.validation.runtime_identity import (  # noqa: E402
    config_hash_of,
    git_head,
    price_contract_block,
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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alpha V2 M3：每日 Shadow 快照采集")
    parser.add_argument("--epoch-id", default="")
    parser.add_argument("--signal-date", required=True)
    parser.add_argument("--market-db", default="artifacts/warehouse/market.duckdb")
    parser.add_argument("--warmup-days", type=int, default=260)
    parser.add_argument("--model-dir", default="")
    parser.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "alpha_v2"))
    parser.add_argument("--config", default=str(REPO_ROOT / "config" / "default.yaml"))
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument(
        "--capture-date",
        default="",
        help=(
            "确定性写入日（**仅 rehearsal/CI**；生产模式一律拒绝——生产写入日 = 系统真实日期）"
        ),
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
        "--quality-pool-source",
        default="research_proxy:alpha_v2_quality_v1",
        help="质量池口径标记（生产接生产漏斗时用 production_selection_engine）",
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
        )
    except FrozenModelError as exc:
        print(f"[shadow] 冻结模型校验失败: {exc}", file=sys.stderr)
        return 5

    # 第二道闸：运行身份全键核验（8 键严格缺失判违例）——model 已载入
    contract = resolve_selection_contract(config, profile="night_scan")
    price = price_contract_block(config)
    runtime_identity = {
        "code_commit": git_head(REPO_ROOT),
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

    snapshot = panel.pit_universe(as_of=signal_date)
    eligible = list(snapshot.eligible_symbols)
    decisions = [DecisionPoint(symbol, signal_date) for symbol in eligible]

    raw = daily_feature_frame(panel, decisions)
    if raw.empty:
        print("[shadow] 特征帧为空", file=sys.stderr)
        return 7
    safe = list(
        safe_feature_columns(
            [c for c in raw.columns if c not in {"decision_date", "symbol"}]
        )
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

    # Deep50 = alpha_rank 最高的 50 只可成交候选（Shadow 无阈值、允许不足 50）。
    work = work[work["decision_date"].astype(str) == signal_date.isoformat()]
    deep50_records = deep50_position_records(work)  # 库内统一口径（含 1..N 名次）
    top5_symbols = {str(r.get("symbol")) for r in deep50_records[:5]}
    top3_symbols = {str(r.get("symbol")) for r in deep50_records[:3]}
    top1_symbols = {str(r.get("symbol")) for r in deep50_records[:1]}

    # 质量池代理：本机没有生产链路成员 => PIT 合格即研究代理（台账如实标记）。
    identity = {
        "code_commit": git_head(REPO_ROOT),
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

    rows = []
    for record in deep50_records:
        # deep_rank = Deep50 名词（1..N），deep_rank_pct 保留 percentile——两类语义
        # 分开存（之前误写成 int(percentile)，恒为 0/1；本轮修复）
        clean_flag = clean_oos_row_eligible(
            backfilled=is_backfill,
            data_health=data_health_block,  # R3：结构化同日 data_health（缺失 = not_available）
            execution_price_mode=str(price["execution_price_mode"]),
            validation_mode=validation_mode,
        )
        rows.append(
            {
                "symbol": str(record.get("symbol")),
                "in_quality_pool": True,
                "in_light_pool": True,
                "in_deep_pool": True,
                "quality_pool_source": str(args.quality_pool_source),
                "quality_rank": None,
                "light_rank": None,
                "deep_rank": record["deep_rank"],
                "deep_rank_pct": record["deep_rank_pct"],
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
                "fillable": True,
                "fillability_note": "preview_only_true_assumed_when_missing",
                "baseline_score": record.get(BASELINE_SCORE_COLUMN),
                "v2_top1": str(record.get("symbol")) in top1_symbols,
                "v2_top3": str(record.get("symbol")) in top3_symbols,
                "v2_top5": str(record.get("symbol")) in top5_symbols,
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
            "counts": {
                "eligible": len(eligible),
                "deep50": int(len(deep50_records)),
                "top1": int(len(top1_symbols)),
                "top3": int(len(top3_symbols)),
                "top5": int(len(top5_symbols)),
                "written_rows": len(rows_written),
            },
            "model": {
                "model_id": model.model_id,
                "artifact_hash": model.manifest.get("artifact_hash", ""),
            },
            "price_contract": price_contract_block(config),
            "notes": (
                "研究链路离线版：quality/light/deep 的成员标记按流动性代理生成，"
                "接入生产后以真实 funnel 为准（quality_pool_source=production_selection_engine）"
            ),
        },
    )
    print(f"[shadow] 已写: {path}（rows={len(rows_written)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
