"""Alpha V2 每日 Shadow 预测快照（M3 §6/§7）。

T 日收盘后，把"当时可见的一切"一次性冻结成不可篡改的行：

- 身份：``validation_epoch_id`` / ``code_commit`` / ``config_hash`` / 模型身份 /
  ``universe_snapshot_id`` / ``data_snapshot_id`` / ``selection_contract_id``;
- 漏斗：Quality300 / Light100 / Deep50 成员与名次（缺 = ``not_available``）;
- V2 输出：``v2_top1/3/5``、``alpha_rank``、方向分与概率、预期净/超额收益、
  风险分、可成交性预览、``data_health`` / ``market_regime``;
- Legacy 对照：``legacy_score`` / ``legacy_final`` / ``legacy_reject_reasons``。

两条铁律是对验收门禁「S10：不得编造 / signal 当天不得写未来数据」的 M3 扩展：

1. **同键重写必须逐字段一致**（见 :data:`PREDICTION_CRITICAL_FIELDS`）——
   否则抛 :class:`ShadowTamperError`；
2. **T 日快照只允许在 T 日写入**：``signal_date != 真实墙上时钟当日`` 时默认
   抛 :class:`ShadowLateWriteError`；显式 ``allow_backfill`` 才能补写，
   且补写行落 ``backfilled=true / clean_oos_eligible=false``；
   ``signal_date`` 早于 ``validation_start_date`` 一律拒绝（没有例外）；
3. **快照缺失有专门台账**（``missing/missing_days.jsonl``）：同一信号日
   出现"台账记了 missing 却又补快照"属于口径冲突，补写只能以 backfill
   标记落账，KPI 会把该日从 clean OOS 剔除。

写入日取"真实墙钟"（R3 修复，封死 BLK-R2-1）
------------------------------------------------

修复前写入窗口比的是**调用方自称的写入日**（``capture_date`` 参数 / CLI
``--capture-date``），于是任何调用方都能把今天补写的行伪装成"T 日当时写入"，
行上还不留补写标记。现在：

- 写入日一律由 :func:`wall_clock_now` 决定（生产 = 系统时间）；
- 只有**冻结清单允许**的上下文才能固定时钟：``validation_mode != production``
  或清单显式 ``deterministic_clock=true``。生产 freeze CLI 恒写
  ``deterministic_clock=false``，所以**任何 CLI 产出的 epoch 都无法注入时钟**；
- 每行同时落 ``recorded_at``（首次冻结时刻）与 ``actual_capture_date``
  （真实写入日）两个字段，配合 KPI 层的 ``late_recorded_at`` 兜底形成第二道闸。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from stock_analyzer.alpha_v2.artifacts import write_json_atomic
from stock_analyzer.alpha_v2.validation.epoch import (
    ROW_IDENTITY_KEYS,
    EpochRecord,
    epoch_identity_matches,
    epoch_subdirs,
    require_epoch_identity_match,
)

SHADOW_ROW_SCHEMA = "alpha_v2_shadow_prediction.v1"
NOT_AVAILABLE = "not_available"

SHADOW_FILENAME_PREFIX = "shadow"
MISSING_LEDGER_FILENAME = "missing_days.jsonl"

# 行内允许的固定键（写入时未知键一律进 ``extra``，不失败但可审计）。
SHADOW_CORE_FIELDS: tuple[str, ...] = (
    "signal_date",
    "signal_time",
    "validation_epoch_id",
    "code_commit",
    "config_hash",
    "model_id",
    "model_artifact_hash",
    "model_created_at",
    "universe_snapshot_id",
    "data_snapshot_id",
    "selection_contract_id",
    "symbol",
    "in_quality_pool",
    "in_light_pool",
    "in_deep_pool",
    "quality_rank",
    "light_rank",
    "deep_rank",
    "v2_top1",
    "v2_top3",
    "v2_top5",
    "alpha_rank",
    "direction_score_3d",
    "direction_score_5d",
    "p_up_3d",
    "p_up_5d",
    "p_up_calibration",
    "expected_net_return_3d",
    "expected_net_return_5d",
    "expected_excess_return_3d",
    "expected_excess_return_5d",
    "risk_score",
    "expected_mae_5d",
    "fillable",
    "fillability_note",
    "data_health",
    "market_regime",
    "legacy_score",
    "legacy_final",
    "legacy_reject_reasons",
    "signal_close_raw",
    "baseline_score",
    # M3 修复轮新增：写入纪律与 clean OOS 资格要用到的行级字段（缺失即 not_available）。
    "backfilled",
    "backfill_reason",
    "clean_oos_eligible",
    "quality_pool_source",
    "deep_rank_pct",
    "recorded_at",
    # R3 修复轮新增：真实写入日 + 是否使用了确定性时钟接缝（审计可见）。
    "actual_capture_date",
    "deterministic_clock",
)

# 已存在的行被重写时，以下字段必须逐值一致；任何一个变化 = 事后改写预测。
PREDICTION_CRITICAL_FIELDS: tuple[str, ...] = (
    "model_artifact_hash",
    "code_commit",
    "config_hash",
    "selection_contract_id",
    "universe_snapshot_id",
    "data_snapshot_id",
    "in_quality_pool",
    "in_light_pool",
    "in_deep_pool",
    "quality_rank",
    "light_rank",
    "deep_rank",
    "v2_top1",
    "v2_top3",
    "v2_top5",
    "alpha_rank",
    "direction_score_3d",
    "direction_score_5d",
    "p_up_3d",
    "p_up_5d",
    "expected_net_return_3d",
    "expected_net_return_5d",
    "expected_excess_return_3d",
    "expected_excess_return_5d",
    "risk_score",
    "expected_mae_5d",
    "fillable",
    "legacy_score",
    "legacy_final",
    "legacy_reject_reasons",
    "signal_close_raw",
    "baseline_score",
    # M3 修复轮：资格/来源字段同样属于"身份一旦写下不得漂"的证据。
    "backfilled",
    "backfill_reason",
    "clean_oos_eligible",
    "quality_pool_source",
    "deep_rank_pct",
)


class ShadowTamperError(RuntimeError):
    """同一 (signal_date, symbol) 的预测内容被事后改写时抛出。"""


class ShadowCaptureError(RuntimeError):
    """捕获输入不合法（缺身份/日期不一致等）。"""


class ShadowLateWriteError(ShadowCaptureError):
    """写入窗口违例：signal_date 不是写入当天、且未显式允许补写。``signal_date``
    早于 epoch 的 ``validation_start_date`` 也同样走这条线——验证起点之前的
    任何"预测"都不可能是真 OOS。"""


class ShadowMissingDayConflictError(ShadowCaptureError):
    """同一信号日在 missing 台账与快照文件之间出现双态（先哪边都不合规）。


    只允许显式补写并落 backfilled 标记；默认一律拒绝。"""


class ShadowClockNotAuthorizedError(ShadowCaptureError):
    """当前 epoch 未授权固定墙钟（生产 freeze 恒 ``deterministic_clock=false``）。

    确定性时钟只属于 rehearsal / test：生产 epoch 想固定时钟 = 想伪造写入日。
    """


# ---------------------------------------------------------------------------
# 墙上时钟（R3：写入日不再由调用方"自称"）
# ---------------------------------------------------------------------------

def _system_now() -> datetime:
    """真实系统时间（带本地时区）。"""
    return datetime.now().astimezone()


_NOW_PROVIDER: Callable[[], datetime] = _system_now


def wall_clock_now() -> datetime:
    """当前墙上时钟（生产 = 系统时间；rehearsal/test 可经 :func:`frozen_wall_clock` 固定）。"""
    return _NOW_PROVIDER()


def wall_clock_is_injected() -> bool:
    """当前是否处于"被固定的时钟"上下文中。"""
    return _NOW_PROVIDER is not _system_now


@contextmanager
def frozen_wall_clock(instant: datetime):
    """**rehearsal / test 专用**：把墙上时钟固定到 ``instant``。

    生产不可达：只有冻结清单允许的 epoch 才接受被固定的时钟
    （``validation_mode != production`` 或 ``deterministic_clock=True``），
    而生产 freeze CLI 恒写 ``deterministic_clock=False`` —— 于是"CLI 产出的
    epoch 里固定时钟"这条路被 :class:`ShadowClockNotAuthorizedError` 关死。
    """
    global _NOW_PROVIDER
    previous = _NOW_PROVIDER

    def _fixed_now() -> datetime:
        return instant

    _NOW_PROVIDER = _fixed_now
    try:
        yield instant
    finally:
        _NOW_PROVIDER = previous


def capture_clock_policy(root: str | Path) -> dict[str, object]:
    """从磁盘冻结清单读"这个 epoch 允许怎样的写入时钟"。

    **production 模式永久不可注入**（没有任何开关能打开它）：确定性写入日只属于
    rehearsal / test。``deterministic_clock`` 是该 epoch 的声明式标记
    （生产恒 false；rehearsal/test 由产出方置 true），用来审计"这些行是不是
    来自确定性时钟"。
    """
    from stock_analyzer.alpha_v2.validation.freeze import (  # 惰性导入避免循环
        load_validation_freeze,
    )

    manifest = load_validation_freeze(root) or {}
    mode = str(manifest.get("validation_mode", "production") or "production").strip().lower()
    deterministic = bool(manifest.get("deterministic_clock", False))
    return {
        "validation_mode": mode or "production",
        "deterministic_clock": deterministic,
        # ← 唯一条件：不是 production。生产 epoch 想固定时钟 = 想伪造写入日。
        "clock_injection_allowed": bool(mode != "production"),
    }


def assert_clock_injection_allowed(root: str | Path) -> dict[str, object]:
    """被固定时钟时调用：不在允许的上下文里就直接拒绝。"""
    policy = capture_clock_policy(root)
    if wall_clock_is_injected() and not policy["clock_injection_allowed"]:
        raise ShadowClockNotAuthorizedError(
            f"production epoch 不接受固定时钟（validation_mode={policy['validation_mode']}）："
            "生产写入日必须等于系统真实日期，且没有开关可以在 production 下打开确定性时钟；"
            "确定性写入日只属于 rehearsal / test。"
        )
    return policy


def clean_oos_row_eligible(
    *,
    backfilled: object = False,
    data_health: object = NOT_AVAILABLE,
    execution_price_mode: str = "raw",
    validation_mode: str = "production",
) -> bool:
    """行级 clean-OOS 资格（写入时刻口径）。

    KPI 层会按日从原始字段**重新计算**同样的口径，不提供"写行为 True 就当数"
    的信任路径——这里的值只是捕获时的自述，方便事后审计分工。
    """
    if isinstance(backfilled, str):
        backfilled = backfilled.strip().lower() in {"true", "1"}
    if bool(backfilled):
        return False
    if isinstance(data_health, Mapping):
        status = str(data_health.get("status", "") or "").strip().lower()
    else:
        status = str(data_health or "").strip().lower()
        if status == "none":
            status = ""
    if status != "ok":
        return False
    if str(execution_price_mode).strip().lower() != "raw":
        return False
    # ``test`` 与 ``production`` 同语义（test 只服务测试套件；CLI 产不出该模式）。
    return str(validation_mode).strip().lower() in {"production", "test"}


def signal_date_of(value: object) -> date:
    """把 str/date/datetime 统一成 date（fail-fast）。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ShadowCaptureError(f"signal_date 无法解析: {value!r}") from exc


def shadow_path(root: str | Path, epoch_id: str, signal_date: date) -> Path:
    base = epoch_subdirs(root, epoch_id)["shadow"]
    return base / f"{signal_date.year:04d}" / f"{signal_date.month:02d}" / (
        f"{SHADOW_FILENAME_PREFIX}_{signal_date.strftime('%Y%m%d')}.jsonl"
    )


def build_shadow_rows(
    *,
    signal_date: date,
    signal_time: str,
    epoch: EpochRecord,
    candidates: Iterable[Mapping[str, object]],
    identity: Mapping[str, object],
    recorded_at: str | None = None,
) -> list[dict[str, object]]:
    """把决策时刻可见字段装配成快照行（缺值一律 ``not_available``，不编造）。

    ``identity`` 至少包含 ``code_commit`` / ``config_hash`` / ``model_id`` /
    ``model_artifact_hash`` / ``model_created_at`` / ``universe_snapshot_id`` /
    ``data_snapshot_id`` / ``selection_contract_id``。

    ``recorded_at`` 缺省取 :func:`wall_clock_now`（不是 ``datetime.now()`` 直调），
    这样 rehearsal/test 的确定性时钟能同时约束"首次冻结时刻"。
    """
    stamp = str(recorded_at or wall_clock_now().isoformat())
    required_identity = (
        "code_commit",
        "config_hash",
        "model_id",
        "model_artifact_hash",
        "model_created_at",
        "universe_snapshot_id",
        "data_snapshot_id",
        "selection_contract_id",
    )
    missing = [key for key in required_identity if key not in identity]
    if missing:
        raise ShadowCaptureError(
            f"identity 缺键: {missing}（身份块必须显式给全，缺写 not_available）"
        )

    rows: list[dict[str, object]] = []
    for candidate in candidates:
        symbol = str(candidate.get("symbol", "") or "").strip()
        if not symbol:
            continue
        row: dict[str, object] = {
            "signal_date": signal_date.isoformat(),
            "signal_time": str(signal_time),
            "validation_epoch_id": epoch.epoch_id,
            "code_commit": str(identity["code_commit"]),
            "config_hash": str(identity["config_hash"]),
            "model_id": str(identity["model_id"]),
            "model_artifact_hash": str(identity["model_artifact_hash"]),
            "model_created_at": str(identity["model_created_at"]),
            "universe_snapshot_id": str(identity["universe_snapshot_id"]),
            "data_snapshot_id": str(identity["data_snapshot_id"]),
            "selection_contract_id": str(identity["selection_contract_id"]),
            "symbol": symbol,
            "recorded_at": stamp,
        }
        for key in SHADOW_CORE_FIELDS:
            if key in row:
                continue
            row[key] = _candidate_value(candidate, key)
        extras = {
            str(key): value
            for key, value in candidate.items()
            if str(key) not in SHADOW_CORE_FIELDS and not str(key).startswith("_")
        }
        if extras:
            row["extra"] = extras
        rows.append(row)
    return rows


def _candidate_value(candidate: Mapping[str, object], key: str) -> object:
    value = candidate.get(key, NOT_AVAILABLE)
    if value is None:
        return NOT_AVAILABLE
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return NOT_AVAILABLE
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, Mapping):
        # R3：data_health 这类"结构化标注"必须原样进 JSON（不能 str(dict) 毁掉契约）
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    return str(value)


def _json_safe_value(value: object) -> object:
    """递归 JSON 安全化（NaN/Inf → not_available；Mapping 保结构）。"""
    if value is None:
        return NOT_AVAILABLE
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return NOT_AVAILABLE
        return value
    if isinstance(value, (int, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    return str(value)


def write_shadow_snapshot(
    *,
    root: str | Path,
    epoch: EpochRecord,
    signal_date: date,
    rows: Sequence[Mapping[str, object]],
    allow_backfill: bool = False,
    backfill_reason: str = "",
) -> Path:
    """幂等写入当日快照；同键行内容变化 => :class:`ShadowTamperError`。

    幂等是为了让"同一天重跑 Shadow 管线"（失败重试 / 调度重入）安全，
    而不是给"事后重算预测"开门——后者会被关键字段比对拦住。

    写入窗口（F1 + R3 修复，fail-closed）：

    - **写入日 = 真实墙钟**（:func:`wall_clock_now`）：没有 ``capture_date`` 之类的
      "自称"参数；被固定的时钟只在 rehearsal/test 上下文里被接受
      （:func:`assert_clock_injection_allowed`，生产清单 ``deterministic_clock=false``）；
    - ``signal_date < epoch.opened_on_date``（= validation_start_date）一律拒绝；
    - ``signal_date != 墙钟当日`` 默认抛 :class:`ShadowLateWriteError`；只有显式
      ``allow_backfill=True`` 且给 ``backfill_reason`` 才允许落账，落账行
      ``backfilled=true`` 且 ``clean_oos_eligible`` 强制为 False；
    - 每行落 ``actual_capture_date``（本次真实写入日）与 ``deterministic_clock``
      （是否用了确定性时钟）——KPI 层的 ``late_recorded_at`` 兜底据此复算；
    - 该信号日已记入 missing 台账时，默认同样拒绝（避免"漏一天→补一天"的口径混淆）。
    """
    record = require_epoch_identity_match(
        root=root, epoch_id=epoch.epoch_id
    )  # 注册表为准，不信任内存对象
    policy = assert_clock_injection_allowed(root)

    opened_on = signal_date_of(record.opened_on_date or "1970-01-01")
    if signal_date < opened_on:
        raise ShadowLateWriteError(
            f"signal_date={signal_date} 早于 epoch 的 validation_start_date={opened_on}——"
            "验证起点之前的快照不构成 Clean OOS"
        )
    run_date = wall_clock_now().date()
    is_backfill = signal_date != run_date
    if is_backfill and not allow_backfill:
        raise ShadowLateWriteError(
            f"T 日快照只允许在 T 日写入：signal_date={signal_date} != 真实写入日 {run_date}。"
            "确需补历史，请显式 enable backfill（落 backfilled=true / clean_oos_eligible=false）"
        )
    backfilled_flag = bool(is_backfill and allow_backfill)
    if backfilled_flag and not str(backfill_reason).strip():
        raise ShadowCaptureError("backfill 必须给出原因（backfill_reason 非空）")

    missing_days = {
        str(item.get("signal_date", ""))
        for item in list_missing_days(root, epoch.epoch_id)
    }
    if signal_date.isoformat() in missing_days and not backfilled_flag:
        raise ShadowMissingDayConflictError(
            f"{signal_date} 已记入 missing 台账；同一天的正确动作是显式 backfill"
            "（保留 missing 记录并落 backfilled 标记），而不是静默改写为正常快照"
        )

    if any(str(row.get("validation_epoch_id")) != epoch.epoch_id for row in rows):
        raise ShadowTamperError("快照行的 validation_epoch_id 与注册表中的 epoch 不一致")

    # 行级身份（ROW_IDENTITY_KEYS）逐项严格对账：漂移/缺键都拒
    for row in rows:
        violations = epoch_identity_matches(record, row, keys=ROW_IDENTITY_KEYS)
        if violations:
            raise ShadowTamperError(
                f"快照行身份与 epoch 冻结身份不符（{row.get('symbol', '?')}）: "
                + "; ".join(violations)
            )
    path = shadow_path(root, epoch.epoch_id, signal_date)
    existing = _read_jsonl(path)
    existing_by_key = {str(item.get("symbol", "")): item for item in existing}
    merged: dict[str, dict[str, object]] = {}
    for item in existing:
        key = str(item.get("symbol", ""))
        if key:
            merged[key] = item
    for row in rows:
        key = str(row.get("symbol", ""))
        if not key:
            continue
        prior = existing_by_key.get(key)
        if prior is not None:
            _assert_row_compatible(prior, row, signal_date=signal_date, symbol=key)
            # 早先写入的 recorded_at 是"首次冻结时刻"，不得被覆盖。
            merged[key] = dict(prior)
            continue
        stamped = dict(row)
        # 真实写入日 / 时钟来源：审计字段，不由调用方声明。
        stamped["actual_capture_date"] = run_date.isoformat()
        stamped["deterministic_clock"] = bool(
            wall_clock_is_injected() and policy["deterministic_clock"]
        )
        if backfilled_flag:
            # 补写是"事后记录"的降口径证据：只能背 backfilled=true + clean_oos_eligible=false
            stamped["backfilled"] = True
            stamped["backfill_reason"] = str(backfill_reason).strip()
            stamped["clean_oos_eligible"] = False
        elif _truthy(stamped.get("backfilled")):
            # 反方向：正常写入日不得"自称补写"（那会让资格字段和窗口记錄不一致）
            raise ShadowCaptureError(
                "signal_date==当日 的正常写入不得携带 backfilled=true"
            )
        merged[key] = stamped
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_jsonl(path, [merged[key] for key in sorted(merged)])
    return path


def _assert_row_compatible(
    prior: Mapping[str, object], incoming: Mapping[str, object], *, signal_date: date, symbol: str
) -> None:
    diffs: list[str] = []
    for field in PREDICTION_CRITICAL_FIELDS:
        before = _json_scalar(prior.get(field))
        after = _json_scalar(incoming.get(field, NOT_AVAILABLE))
        if before != after:
            diffs.append(f"{field}:{before!r}=>{after!r}")
    if diffs:
        raise ShadowTamperError(
            f"拒绝改写 {signal_date} {symbol} 的已冻结预测（{len(diffs)} 个字段不同）: "
            + "; ".join(diffs[:10])
            + "。事后重算的正确做法是关闭当前 epoch 并开启新 epoch。"
        )


def _json_scalar(value: object) -> object:
    if value is None:
        return NOT_AVAILABLE
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return NOT_AVAILABLE
        return round(value, 12)
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return value


def record_missing_prediction_day(
    *,
    root: str | Path,
    epoch: EpochRecord,
    signal_date: date,
    reason: str,
    recorded_at: str | None = None,
) -> Path:
    """记录一个**没有合法 Shadow 预测**的交易日（幂等；reason 必填）。

    KPI/样本门会把它从 clean 口径剔除；"事后补一个漂亮结果"无法冒充。
    该日期若已有快照（不管是否 backfilled）则两个事实互相冲突——同样拒绝，
    需要先对冲突做人工裁定并关闭/新开 epoch。
    """
    require_epoch_identity_match(root=root, epoch_id=epoch.epoch_id)
    existing_rows = read_shadow_rows(root, epoch.epoch_id, signal_date)
    if existing_rows:
        raise ShadowMissingDayConflictError(
            f"{signal_date} 已存在 shadow 快照（{len(existing_rows)} 行）；"
            "同一天不能又算 missing。" "如属误记，请走 close-epoch→修正→新-epoch 流程"
        )
    reason_text = str(reason).strip()
    if not reason_text:
        raise ShadowCaptureError(
            "missing day 必须给出原因（如 upstream_data_missing / code_failure）"
        )
    ledger = epoch_subdirs(root, epoch.epoch_id)["missing"] / MISSING_LEDGER_FILENAME
    existing = _read_jsonl(ledger)
    token = signal_date.isoformat()
    if not any(str(item.get("signal_date")) == token for item in existing):
        existing.append(
            {
                "signal_date": token,
                "validation_epoch_id": epoch.epoch_id,
                "reason": reason_text,
                "recorded_at": str(recorded_at or wall_clock_now().isoformat()),
            }
        )
        ledger.parent.mkdir(parents=True, exist_ok=True)
        _write_jsonl(ledger, existing)
    return ledger


def list_missing_days(root: str | Path, epoch_id: str) -> list[dict[str, object]]:
    ledger = epoch_subdirs(root, epoch_id)["missing"] / MISSING_LEDGER_FILENAME
    return _read_jsonl(ledger)


def list_shadow_dates(root: str | Path, epoch_id: str) -> list[date]:
    base = epoch_subdirs(root, epoch_id)["shadow"]
    dates: list[date] = []
    if not base.exists():
        return dates
    for path in sorted(base.rglob(f"{SHADOW_FILENAME_PREFIX}_*.jsonl")):
        token = path.stem.removeprefix(f"{SHADOW_FILENAME_PREFIX}_")
        try:
            dates.append(date.fromisoformat(f"{token[0:4]}-{token[4:6]}-{token[6:8]}"))
        except ValueError:
            continue
    return sorted(set(dates))


def read_shadow_rows(root: str | Path, epoch_id: str, signal_date: date) -> list[dict[str, object]]:
    return _read_jsonl(shadow_path(root, epoch_id, signal_date))


def write_shadow_day_manifest(
    *,
    root: str | Path,
    epoch: EpochRecord,
    signal_date: date,
    payload: Mapping[str, object],
) -> Path:
    """当日运行清单：影子运行的身份与计数（``manifests/shadow_day_*.json``）。"""
    require_epoch_identity_match(root=root, epoch_id=epoch.epoch_id)
    body = dict(payload)
    body.setdefault("validation_epoch_id", epoch.epoch_id)
    body.setdefault("signal_date", signal_date.isoformat())
    target = (
        epoch_subdirs(root, epoch.epoch_id)["manifests"]
        / f"shadow_day_{signal_date.strftime('%Y%m%d')}.json"
    )
    return write_json_atomic(target, body)


def deep50_position_records(
    work: pd.DataFrame,
    *,
    score_column: str = "alpha_rank_score",
    limit: int = 50,
) -> list[dict[str, object]]:
    """Deep50 = 按分数降序取前 N 只；``deep_rank`` 是名次（1..N）、
    ``deep_rank_pct`` 保留原始分位数。两者语义分离（N2 修复）。

    之前用 ``int(percentile)`` 把分位数取整，恒为 0/1——这个字段从此
    就有了毫无意义而已可证伪的形态，修正是修正口径，不是改定义。
    """
    liquid = work.copy()
    liquid["__pct"] = pd.to_numeric(liquid[score_column], errors="coerce")
    liquid = liquid.sort_values("__pct", ascending=False, kind="mergesort").head(int(limit))
    records: list[dict[str, object]] = []
    for position, row in enumerate(liquid.to_dict(orient="records"), start=1):
        record = dict(row)
        pct = record.pop("__pct")
        record["deep_rank"] = int(position)
        record["deep_rank_pct"] = float(pct) if pd.notna(pct) else None
        records.append(record)
    return records


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    content = "\n".join(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) for row in rows)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(content + ("\n" if content else ""), encoding="utf-8")
    temp.replace(path)


__all__ = [
    "MISSING_LEDGER_FILENAME",
    "NOT_AVAILABLE",
    "PREDICTION_CRITICAL_FIELDS",
    "SHADOW_CORE_FIELDS",
    "SHADOW_FILENAME_PREFIX",
    "SHADOW_ROW_SCHEMA",
    "ShadowCaptureError",
    "ShadowClockNotAuthorizedError",
    "ShadowLateWriteError",
    "ShadowMissingDayConflictError",
    "ShadowTamperError",
    "assert_clock_injection_allowed",
    "build_shadow_rows",
    "capture_clock_policy",
    "clean_oos_row_eligible",
    "deep50_position_records",
    "frozen_wall_clock",
    "list_missing_days",
    "list_shadow_dates",
    "read_shadow_rows",
    "record_missing_prediction_day",
    "shadow_path",
    "signal_date_of",
    "wall_clock_is_injected",
    "wall_clock_now",
    "write_shadow_day_manifest",
    "write_shadow_snapshot",
]
