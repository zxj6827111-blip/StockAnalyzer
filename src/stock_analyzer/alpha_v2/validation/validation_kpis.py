"""Alpha V2 M3 验证 KPI 汇总（M3 §9–§14）。

输入 = 某 epoch 下的 shadow 快照（T 日冻结预测）+ outcome（真实成熟结果），
按冻结口径产出一份"今天为止"的自证报告。报告**只给状态不给结论**：

- ``alpha_verified`` 恒为 ``False``，``production_promotion`` 恒为 ``LOCKED``
  ——这两个词在 120D/250D 样本门之前被写成"真"的任何入口属于验收 FAIL；
- 样本门只看**成熟决策日数**（20/60/120/250），与样本只数无关
  （"300 只股票"不是 300 个独立样本，蓝图 §7.3）；
- 命中率绝不单独出现：Hit Rate 必须与平均/中位收益、超额、Rank IC、
  单调性、尾部风险**同屏**（M3 §11 的结构性执行）。

统计叮嘱（蓝图 §7.3）：IC 的显著性以逐日序列为单位（date 是独立统计单位）；
本模块的 95% CI 用 date-block bootstrap（块长 = 主 horizon 交易日），实现复用
``learning.scoring_eval`` 的移动块口径，不重新发明。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from stock_analyzer.alpha_v2.research.metrics import (
    DEFAULT_HORIZONS,
    NOT_AVAILABLE,
    daily_rank_ic,
    ic_summary,
    metric_column,
    quantile_monotonicity,
    quantile_returns,
)
from stock_analyzer.alpha_v2.research.winner_recall import (
    DEFAULT_WINNER_QUANTILE,
    RecallSpec,
    compute_winner_recall,
)
from stock_analyzer.alpha_v2.validation.data_health_capture import (
    REASON_NOT_AVAILABLE as REASON_DATA_HEALTH_NOT_AVAILABLE,
)
from stock_analyzer.alpha_v2.validation.data_health_capture import (
    data_health_gate_ok,
)
from stock_analyzer.alpha_v2.validation.epoch import (
    ROW_IDENTITY_KEYS,
    EpochRecord,
    epoch_identity_matches,
    epoch_subdirs,
)
from stock_analyzer.alpha_v2.validation.freeze import (
    FROZEN_BENCHMARK_LAYERS,
    SAMPLE_GATES,
    load_validation_freeze,
)
from stock_analyzer.alpha_v2.validation.outcome_maturation import outcome_path
from stock_analyzer.alpha_v2.validation.production_funnel import (
    AUTHORITATIVE_SELECTOR_MODES,
    FUNNEL_SCHEMA,
    FUNNEL_SOURCE,
    funnel_snapshot_hash,
)
from stock_analyzer.alpha_v2.validation.shadow_capture import (
    list_missing_days,
    list_shadow_dates,
    read_shadow_rows,
)

KPI_SCHEMA = "alpha_v2_validation_kpi.v1"
KPI_REPORT_PREFIX = "validation_kpi"

# 身份/元数据列：送入特征无关统计时被排除（快照行的业务列全集由采集契约定义）。
_IDENTITY_COLUMNS = {
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
    "recorded_at",
}

_TOP_KS: tuple[int, ...] = (1, 3, 5)
_TOPK_FIELDS: dict[int, str] = {1: "v2_top1", 3: "v2_top3", 5: "v2_top5"}

# M3 §10.5：超额必须与冻结的四层基准比。style/simple 的列名见下方函数。
_EXCESS_COLUMN_BY_LAYER: dict[str, str] = {
    "quality_pool_ew": "excess_return_{h}d",
    "eligible_ew": "excess_return_{h}d__eligible_ew",
    "style_matched": "residual_excess_return_{h}d",
}


class KpiReportError(RuntimeError):
    """KPI 汇总结构性失败（epoch 不存在、冻结清单缺失）。"""


def load_epoch_frames(
    root: str | Path, epoch_id: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """汇总某 epoch 全部 shadow/outcome 行（join 键 = signal_date + symbol）。"""
    shadow_rows: list[dict[str, object]] = []
    for day in list_shadow_dates(root, epoch_id):
        shadow_rows.extend(read_shadow_rows(root, epoch_id, day))
    outcome_rows: list[dict[str, object]] = []
    for day in list_shadow_dates(root, epoch_id):
        path = outcome_path(root, epoch_id, day)
        if not path.exists():
            continue
        for row in _read_jsonl(path):
            row = dict(row)
            row.setdefault("signal_date", day.isoformat())
            outcome_rows.append(row)
    shadow = pd.DataFrame(shadow_rows)
    outcomes = pd.DataFrame(outcome_rows)
    return shadow, outcomes


# 报告允许的"层级标签"
M3_CLEAN_OOS_VALIDATION_MODE = "production"
# clean 口径只认生产语义；``test`` 仅服务 M3 测试套件——freeze CLI 只会写
# production / rehearsal，所以"用 test 模式把某天算成 clean"在生产上不可达，
# 它存在的唯一理由是让 clean 流水线能在确定性时钟下被测试。
M3_CLEAN_OOS_VALIDATION_MODES: tuple[str, ...] = ("production", "test")


# ``_ANY_DAY`` 只是给"不关心日期"的调用点用的占位；治理层一定会传真实 signal_date。
_ANY_DAY = date(1970, 1, 1)


def _parse_instant_date(value: object) -> date | None:
    """把 ``recorded_at`` 这类 ISO 时间戳解析成日期（失败 = None）。"""
    text = str(value or "").strip()
    if not text or text == NOT_AVAILABLE:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _day_recorded_at_ok(
    group: pd.DataFrame, *, signal_date: date
) -> tuple[bool, str]:
    """R3 第二道闸：行 ``recorded_at`` 的日期必须等于 signal_date。

    捕获层（第一闸）已经要求"写入日 == 墙钟当日"，这里再按**落盘字段**复算一次：
    即便未来捕获层出现 bug、或有人直接改文件把某天补成"看起来正常"的行，
    只要 recorded_at / actual_capture_date 与 signal_date 不同日，这一天就不进 clean。
    """
    for _index, row in group.iterrows():
        recorded = _parse_instant_date(row.get("recorded_at"))
        if recorded is None:
            return False, "recorded_at_missing"
        if recorded != signal_date:
            return False, "late_recorded_at"
        actual = row.get("actual_capture_date")
        if actual is not None and str(actual).strip() not in {"", NOT_AVAILABLE}:
            actual_date = _parse_instant_date(actual)
            if actual_date is None or actual_date != signal_date:
                return False, "late_recorded_at"
    return True, ""


def _day_data_health_ok(
    group: pd.DataFrame, *, signal_date: date
) -> tuple[bool, str]:
    """逐行过 data_health 门（缺失/陈旧/降级/broken 都不算 clean）。"""
    if "data_health" not in group.columns:
        return False, REASON_DATA_HEALTH_NOT_AVAILABLE
    for value in group["data_health"]:
        ok, reason = data_health_gate_ok(value, signal_date)
        if not ok:
            return False, reason or REASON_DATA_HEALTH_NOT_AVAILABLE
    return True, ""


def _load_day_manifest(root: str | Path, epoch_id: str, signal_date: date) -> dict[str, object]:
    """读当日 shadow day manifest（capture 原子写；不存在返回空 dict）。"""
    path = (
        epoch_subdirs(root, epoch_id)["manifests"]
        / f"shadow_day_{signal_date.strftime('%Y%m%d')}.json"
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _day_funnel_ok(
    group: pd.DataFrame,
    *,
    root: str | Path,
    epoch_id: str,
    signal_date: date,
    freeze: Mapping[str, object],
) -> tuple[bool, str]:
    """M4-L 第二道闸：clean 日的 cohort 必须能被"当日内嵌生产漏斗证据"证明。

    只在冻结清单显式 ``require_production_funnel=true`` 时生效（生产 freeze CLI
    恒写 true；旧清单缺键即保持 M3 语义——向后兼容，不改历史口径）。

    逐日复核（全部对账 epoch 内的不可变 manifest，不依赖 runtime 目录）：

    - manifest 存在且带 funnel 块，``funnel_snapshot_hash`` 复算一致（写时伪造
      在写后同样会被抓到）；
    - schema / source / signal_date / selector_mode 全部是生产权威值；
    - 生产模式下 funnel 必须已链接正式晚报（``night_scan_report_id`` 非空）；
    - 当日 shadow 行集合 == funnel.deep_members 集合，逐行 ``deep_rank`` /
      ``quality_rank`` / ``light_rank`` 与漏斗名次一致，行来源标记为生产。
    """
    if not bool(freeze.get("require_production_funnel", False)):
        return True, ""
    manifest = _load_day_manifest(root, epoch_id, signal_date)
    funnel = manifest.get("funnel")
    if not isinstance(funnel, Mapping):
        return False, "funnel_evidence_missing"
    recorded = str(funnel.get("funnel_snapshot_hash", "") or "")
    if not recorded or funnel_snapshot_hash(funnel) != recorded:
        return False, "funnel_hash_mismatch"
    if str(funnel.get("schema", "")) != FUNNEL_SCHEMA:
        return False, "funnel_schema_mismatch"
    if str(funnel.get("signal_date", "")) != signal_date.isoformat():
        return False, "funnel_date_mismatch"
    if str(funnel.get("source", "")) != FUNNEL_SOURCE:
        return False, "funnel_source_not_production"
    selector_mode = str(funnel.get("selector_mode", "") or "").strip()
    if selector_mode not in AUTHORITATIVE_SELECTOR_MODES:
        return False, "funnel_selector_mode_not_authoritative"
    if (
        str(freeze.get("validation_mode", "production")).strip().lower() == "production"
        and not str(funnel.get("night_scan_report_id", "") or "").strip()
    ):
        return False, "funnel_report_not_linked"
    deep_members = funnel.get("deep_members")
    if not isinstance(deep_members, list) or not deep_members:
        return False, "funnel_cohort_empty"
    deep_rank_by_symbol: dict[str, int] = {}
    quality_rank_by_symbol: dict[str, object] = {}
    light_rank_by_symbol: dict[str, object] = {}
    for item in deep_members:
        if not isinstance(item, Mapping):
            return False, "funnel_member_malformed"
        symbol = str(item.get("symbol", "") or "").strip()
        rank = item.get("rank")
        if not symbol or not isinstance(rank, int):
            return False, "funnel_member_malformed"
        deep_rank_by_symbol[symbol] = rank
    for key, target in (
        ("quality_members", quality_rank_by_symbol),
        ("light_members", light_rank_by_symbol),
    ):
        rows = funnel.get(key)
        if not isinstance(rows, list):
            return False, "funnel_member_malformed"
        for item in rows:
            if isinstance(item, Mapping):
                target[str(item.get("symbol", "") or "").strip()] = item.get("rank")
    row_symbols = {str(value) for value in group.get("symbol", pd.Series(dtype=object))}
    if row_symbols != set(deep_rank_by_symbol):
        return False, "funnel_cohort_mismatch"
    for _index, row in group.iterrows():
        symbol = str(row.get("symbol", "") or "")
        if str(row.get("quality_pool_source", "") or "") != FUNNEL_SOURCE:
            return False, "funnel_source_not_production"
        try:
            if int(row.get("deep_rank")) != deep_rank_by_symbol.get(symbol):
                return False, "funnel_rank_mismatch"
        except (TypeError, ValueError):
            return False, "funnel_rank_mismatch"
        for field, table in (
            ("quality_rank", quality_rank_by_symbol),
            ("light_rank", light_rank_by_symbol),
        ):
            prior = row.get(field)
            expected = table.get(symbol)
            if expected is None:
                continue
            try:
                if int(prior) != int(expected):  # type: ignore[arg-type]
                    return False, "funnel_rank_mismatch"
            except (TypeError, ValueError):
                return False, "funnel_rank_mismatch"
    return True, ""


def _day_governance(
    joined: pd.DataFrame,
    *,
    missing_days: Sequence[Mapping[str, object]],
    epoch: EpochRecord,
    freeze: Mapping[str, object],
    root: str | Path,
) -> pd.DataFrame:
    """逐日的 clean OOS 资格表（B6/F6 的实现核心；R3 追加时间兜底）。

    每天都有明确的一句话判决（``eligible`` + ``reasons``）；样本门、
    KPI 主指标块只过 ``eligible==True`` 的日期。

    判据（全部硬规则，无自由裁量）：

    - ``backfilled=false``（补写日一律 not clean；KPI 不认行里写的
      clean_oos_eligible，只认原语字段重算）;
    - 不在 missing 台账（先记 missing 又想补写的不会绕过）；
    - ``data_health`` 同日且 status=ok（缺失/陈旧/降级 = 不参与 clean OOS，
      判定实现复用 :func:`data_health_capture.data_health_gate_ok`）；
    - **``recorded_at``（及 ``actual_capture_date``）与 signal_date 同日**
      ——R3 的 late-write 兜底闸；
    - 快照行身份与 epoch 冻结身份逐项一致（缺失 = 不匹配）；
    - 清单 execution_price_mode == raw 且 validation_mode ∈ {production, test}。
    """
    missing_set = {str(item.get("signal_date", "")) for item in missing_days}
    execution_ok = (
        str(freeze.get("execution_price_mode", "")).strip().lower() == "raw"
        and str(freeze.get("validation_mode", "production")).strip().lower()
        in M3_CLEAN_OOS_VALIDATION_MODES
    )
    columns = [
        "signal_date",
        "captured",
        "missing",
        "backfilled",
        "data_health_ok",
        "recorded_at_ok",
        "identity_ok",
        "execution_ok",
        "eligible",
        "reasons",
    ]
    rows_out: list[dict[str, object]] = []
    if joined.empty:
        return pd.DataFrame(columns=columns)
    for day, group in joined.groupby(joined["signal_date"].astype(str), sort=True):
        day_date = _parse_instant_date(day) or _ANY_DAY
        backfilled = bool(_bool_series(group, "backfilled").any())
        health_ok, health_reason = _day_data_health_ok(group, signal_date=day_date)
        recorded_ok, recorded_reason = _day_recorded_at_ok(group, signal_date=day_date)
        funnel_ok, funnel_reason = _day_funnel_ok(
            group,
            root=root,
            epoch_id=epoch.epoch_id,
            signal_date=day_date,
            freeze=freeze,
        )
        identity_ok = True
        identity_reasons: list[str] = []
        for _, row in group.iterrows():
            violations = epoch_identity_matches(epoch, row.to_dict(), keys=ROW_IDENTITY_KEYS)
            if violations:
                identity_ok = False
                identity_reasons = violations
                break
        is_missing = str(day) in missing_set
        reasons: list[str] = []
        if is_missing:
            reasons.append("missing_prediction_day")
        if backfilled:
            reasons.append("backfilled")
        if not health_ok:
            reasons.append(health_reason or REASON_DATA_HEALTH_NOT_AVAILABLE)
        if not recorded_ok:
            reasons.append(recorded_reason or "late_recorded_at")
        if not identity_ok:
            reasons.append("identity_mismatch")
        if not funnel_ok:
            reasons.append(funnel_reason or "funnel_evidence_invalid")
        if not execution_ok:
            reasons.append("execution_or_mode_not_production_raw")
        eligible = not reasons
        if not identity_ok and identity_reasons:
            reasons.extend(identity_reasons)
        rows_out.append(
            {
                "signal_date": str(day),
                "captured": True,
                "missing": is_missing,
                "backfilled": backfilled,
                "data_health_ok": health_ok,
                "recorded_at_ok": recorded_ok,
                "identity_ok": identity_ok,
                "execution_ok": bool(execution_ok),
                "eligible": bool(eligible),
                "reasons": reasons,
            }
        )
    return pd.DataFrame(rows_out)


def build_validation_kpi(
    *,
    root: str | Path,
    epoch: EpochRecord,
    report_date: date | None = None,
) -> dict[str, object]:
    """M3 验证 KPI 主入口（读盘 → 汇总 → 报告 payload）。"""
    freeze = load_validation_freeze(root)
    if freeze is None:
        raise KpiReportError(f"validation freeze manifest 缺失: {root}（M3 §3）")

    shadow, outcomes = load_epoch_frames(root, epoch.epoch_id)
    joined = _join_shadow_outcomes(shadow, outcomes)
    missing_days = list_missing_days(root, epoch.epoch_id)
    governance = _day_governance(
        joined, missing_days=missing_days, epoch=epoch, freeze=freeze, root=root
    )
    eligible_dates = (
        set(governance.loc[governance["eligible"], "signal_date"].astype(str))
        if not governance.empty
        else set()
    )
    clean_joined = (
        joined[joined["signal_date"].astype(str).isin(eligible_dates)]
        if eligible_dates
        else joined.iloc[0:0]
    )

    payload: dict[str, object] = {
        "schema": KPI_SCHEMA,
        "report_date": (
            str(report_date) if report_date is not None else datetime.now().date().isoformat()
        ),
        "validation_epoch_id": epoch.epoch_id,
        "epoch_status": epoch.status,
        "epoch_opened_on": epoch.opened_on_date,
        "freeze_manifest_hash": freeze.get("freeze_manifest_hash"),
        "model": _model_identity_block(freeze),
        "selection_contract_id": freeze.get("selection_contract_id"),
        "benchmarks_frozen": freeze.get("benchmarks"),
        "sample_gates": dict(SAMPLE_GATES),
        "missing_prediction_days": [
            {
                "signal_date": item.get("signal_date"),
                "reason": item.get("reason"),
            }
            for item in missing_days
        ],
        # M3 §14/§21：这两个词在达到样本门 + 复核前恒为 False/LOCKED。
        "alpha_verified": False,
        "production_promotion": "LOCKED",
    }

    if joined.empty:
        payload["status"] = "no_shadow_data"
        payload["note"] = "该 epoch 还没有任何 shadow 快照；所有 KPI 如实留空"
        payload["maturity"] = _maturity_block(joined)
        payload["clean_maturity"] = _maturity_block(clean_joined)
        payload["governance"] = _governance_block(governance, missing_days)
        payload["sample_gate_status"] = _sample_gate_block(payload["clean_maturity"])
        return payload

    # 证据块（hit_rate / returns / excess / IC / 单调性 / recall / 下行 / 基线配对）
    # 一律只在 clean 样本上——recorded≠clean 的日不出现在这里。
    payload["maturity"] = _maturity_block(joined)
    payload["clean_maturity"] = _maturity_block(clean_joined)
    payload["governance"] = _governance_block(governance, missing_days)
    payload["hit_rate"] = _hit_rate_block(clean_joined)
    payload["returns"] = _returns_block(clean_joined)
    payload["excess_vs_benchmarks"] = _excess_block(clean_joined)
    payload["rank_ic"] = _rank_ic_block(clean_joined)
    payload["quantile_monotonicity"] = _quantile_block(clean_joined)
    payload["winner_recall"] = _winner_recall_block(clean_joined)
    payload["downside"] = _downside_block(clean_joined)
    # 执行统计是链路健康（T+1 成交率/no-fill），按全快照陈述——但来源可能与
    # clean 样本不重合；报告同时给出 clean 子集口径便于交叉复核。
    payload["execution"] = {
        "all_captured": _execution_block(joined),
        "clean_only": _execution_block(clean_joined),
    }
    payload["baseline_pairing"] = _baseline_pairing(clean_joined)
    payload["cohort_counts"] = _cohort_counts(joined)
    payload["sample_gate_status"] = _sample_gate_block(payload["clean_maturity"])
    payload["statement_boundary"] = (
        "本报告为 shadow 期过程指标：alpha_verified=False 与 production_promotion=LOCKED "
        "在达到冻结样本门并独立复核前不得改写；任何'优秀命中率'必须与收益/超额/尾部同屏解读。"
    )
    payload["status"] = "ok"
    return payload


# ---------------------------------------------------------------------------
# 帧准备
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    import json

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


def _join_shadow_outcomes(shadow: pd.DataFrame, outcomes: pd.DataFrame) -> pd.DataFrame:
    if shadow.empty:
        return pd.DataFrame()
    left = shadow.copy()
    if outcomes.empty:
        return left
    right = outcomes.copy()
    drop_cols = [c for c in ("validation_epoch_id",) if c in right.columns and c in left.columns]
    right = right.drop(columns=drop_cols)
    join_cols = ["signal_date", "symbol"]
    for col in join_cols:
        left[col] = left[col].astype(str)
        right[col] = right[col].astype(str)
    joined = left.merge(right, on=join_cols, how="left", suffixes=("", "__dup"))
    # 复用 M2 指标函数的口径约定：decision_date 是统计单位（= signal_date）
    joined["decision_date"] = joined["signal_date"]
    return joined


def _bool_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(False, index=frame.index)
    series = frame[column]
    if series.dtype == bool:
        return series.fillna(False)
    return series.map(
        lambda value: value is True
        or (isinstance(value, str) and value.strip().lower() in {"true", "1"})
    )


def _num(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    values = frame[column].replace(NOT_AVAILABLE, np.nan)
    return pd.to_numeric(values, errors="coerce")


def _matured_mask(frame: pd.DataFrame, horizon: int) -> pd.Series:
    column = f"matured_{int(horizon)}d"
    if column not in frame.columns:
        return pd.Series(False, index=frame.index)
    return _bool_series(frame, column)


def _main_sample(frame: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """主口径样本：可成交 + 已成熟（M3 §8 复用 M1/S11 不成交不计收益）。"""
    executable = _bool_series(frame, "executable")
    matured = _matured_mask(frame, horizon)
    column = f"net_return_{int(horizon)}d"
    has_value = _num(frame, column).notna()
    return frame[executable & matured & has_value]


# ---------------------------------------------------------------------------
# 各 KPI 块
# ---------------------------------------------------------------------------


def _maturity_block(frame: pd.DataFrame) -> dict[str, object]:
    block: dict[str, object] = {"signal_dates_total": 0}
    if frame.empty:
        for horizon in DEFAULT_HORIZONS:
            block[f"mature_dates_{int(horizon)}d"] = 0
        return block
    dates = frame["signal_date"].astype(str).nunique()
    block["signal_dates_total"] = int(dates)
    block["rows_total"] = int(len(frame))
    for horizon in DEFAULT_HORIZONS:
        key = int(horizon)
        mask = _matured_mask(frame, key) & _bool_series(frame, "executable")
        usable = frame[mask]
        days = usable["signal_date"].astype(str).nunique() if not usable.empty else 0
        block[f"mature_dates_{key}d"] = int(days)
        block[f"mature_rows_{key}d"] = int(len(usable))
    return block


def _governance_block(
    governance: pd.DataFrame,
    missing_days: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Clean OOS 资格治理层（B6/F6 的落点）：

    - 样本门与主证据块只统计 ``eligible=True`` 的日期；
    - missing / backfilled / data_health / identity / execution 各自单列计数；
    - ``coverage_rate`` = clean_oos_days / captured_days（行有快照的天数）。
    """
    captured = int(len(governance)) if not governance.empty else 0
    clean = int(governance["eligible"].sum()) if not governance.empty else 0
    not_ok = ~governance["data_health_ok"] & ~governance["missing"] & ~governance["backfilled"]
    excluded_health = int(not_ok.sum()) if not governance.empty else 0
    backfilled = int(governance["backfilled"].sum()) if not governance.empty else 0
    late_recorded = (
        int((~governance["recorded_at_ok"]).sum()) if not governance.empty else 0
    )
    identity_invalid = (
        int((~governance["identity_ok"]).sum()) if not governance.empty else 0
    )
    execution_invalid = (
        int((~governance["execution_ok"]).sum()) if not governance.empty else 0
    )
    missing_captured = (
        int(governance.loc[governance["missing"], "signal_date"].nunique())
        if not governance.empty and "signal_date" in governance.columns
        else 0
    )
    return {
        "captured_days": captured,
        "clean_oos_days": clean,
        "missing_prediction_days": len(missing_days),
        "missing_days_with_snapshot_conflict": missing_captured,
        "backfilled_days": backfilled,
        "excluded_data_health_days": excluded_health,
        "identity_invalid_days": identity_invalid,
        "execution_or_mode_invalid_days": execution_invalid,
        "late_recorded_days": late_recorded,
        "coverage_rate": (clean / captured) if captured else 0.0,
        "by_date": (
            governance.to_dict(orient="records") if not governance.empty else []
        ),
        "definitions": (
            "eligible=该日的行全量通过 backfilled=false 且不在 missing 台账、"
            "data_health 同日且 ok（缺失/陈旧/降级一律不算）、"
            "recorded_at（与 actual_capture_date）与 signal_date 同日、"
            "行身份与冻结身份一致、文件侧 execution=raw 且 validation_mode ∈ {production, test}。"
            "不通过的日子如实列出原因、绝不混入证据。"
        ),
    }


def _sample_gate_block(maturity: Mapping[str, object]) -> dict[str, object]:
    primary = int(maturity.get("mature_dates_5d", 0) or 0)  # type: ignore[arg-type]
    confirmation = int(maturity.get("mature_dates_3d", 0) or 0)  # type: ignore[arg-type]
    statuses: dict[str, object] = {}
    for name, threshold in SAMPLE_GATES.items():
        statuses[name] = {
            "threshold": int(threshold),
            "primary_5d_reached": primary >= threshold,
            "confirmation_3d_reached": confirmation >= threshold,
            # 业务判定以主 horizon 为准；确认 horizon 仅供并排。
            "reached": primary >= threshold,
        }
    return statuses


def _hit_rate_block(frame: pd.DataFrame) -> dict[str, object]:
    """Top1/3/5 的 3D/5D 命中率（M3 §10.1/§10.2；绝不单独当成功标准）。"""
    result: dict[str, object] = {}
    for k, field in _TOPK_FIELDS.items():
        selected = frame[_bool_series(frame, field)]
        per_k: dict[str, object] = {"days": 0, "rows": 0}
        if selected.empty:
            for horizon in (3, 5):
                per_k[f"hit_rate_{horizon}d"] = NOT_AVAILABLE
            result[f"top{int(k)}"] = per_k
            continue
        per_k["days"] = int(selected["signal_date"].astype(str).nunique())
        per_k["rows"] = int(len(selected))
        for horizon in (3, 5):
            sample = _main_sample(selected, horizon)
            column = f"net_return_{int(horizon)}d"
            values = _num(sample, column).dropna()
            if values.empty:
                per_k[f"hit_rate_{horizon}d"] = NOT_AVAILABLE
                per_k[f"sample_rows_{horizon}d"] = 0
                continue
            per_k[f"hit_rate_{horizon}d"] = float((values > 0).mean())
            per_k[f"sample_rows_{horizon}d"] = int(len(values))
        result[f"top{int(k)}"] = per_k
    return result


def _returns_block(frame: pd.DataFrame) -> dict[str, object]:
    """TopK 平均 + 中位净收益（3D/5D；M3 §10.3/§10.4：平均必须配中位数）。"""
    result: dict[str, object] = {}
    for k, field in _TOPK_FIELDS.items():
        selected = frame[_bool_series(frame, field)]
        per_k: dict[str, object] = {}
        for horizon in (3, 5):
            sample = _main_sample(selected, horizon)
            values = _num(sample, f"net_return_{int(horizon)}d").dropna()
            if values.empty:
                per_k[f"net_{horizon}d"] = {
                    "mean": NOT_AVAILABLE,
                    "median": NOT_AVAILABLE,
                    "rows": 0,
                }
                continue
            per_k[f"net_{horizon}d"] = {
                "mean": float(values.mean()),
                "median": float(values.median()),
                "rows": int(len(values)),
            }
        result[f"top{int(k)}"] = per_k
    return result


def _excess_block(frame: pd.DataFrame) -> dict[str, object]:
    """TopK 相对各冻结基准的超额（主口径 5D）。"""
    result: dict[str, object] = {}
    layers_present: list[str] = []
    for k, field in _TOPK_FIELDS.items():
        selected = frame[_bool_series(frame, field)]
        per_k: dict[str, object] = {}
        for layer, template in _EXCESS_COLUMN_BY_LAYER.items():
            column = template.format(h=5)
            sample = _main_sample(selected, 5)
            values = _num(sample, column).dropna()
            if values.empty:
                per_k[layer] = NOT_AVAILABLE
                continue
            per_k[layer] = {
                "mean": float(values.mean()),
                "median": float(values.median()),
                "rows": int(len(values)),
            }
            if layer not in layers_present:
                layers_present.append(layer)
        result[f"top{int(k)}"] = per_k
    result["frozen_layers"] = list(FROZEN_BENCHMARK_LAYERS)
    result["layers_with_data"] = layers_present
    result["simple_baseline_note"] = (
        "simple_baseline 见 baseline_pairing 块：TopK 模型组 vs Simple Baseline TopK 同日配对"
    )
    return result


def _rank_ic_block(frame: pd.DataFrame) -> dict[str, object]:
    """逐日 Spearman(alpha_rank, future excess)；20D/60D 滚动与 CI 由 ic_summary 统一出。"""
    block: dict[str, object] = {}
    if "alpha_rank" not in frame.columns:
        return {"status": NOT_AVAILABLE, "reason": "alpha_rank 缺失"}
    for horizon in DEFAULT_HORIZONS:
        key = int(horizon)
        excess_column = metric_column("excess_return", key)
        if excess_column not in frame.columns:
            block[f"{key}d"] = {"status": NOT_AVAILABLE, "reason": f"missing {excess_column}"}
            continue
        daily = daily_rank_ic(
            frame, score_column="alpha_rank", metric_column_=excess_column, min_cross_section=10
        )
        if daily.empty:
            block[f"{key}d"] = {"status": NOT_AVAILABLE, "reason": "no usable daily IC"}
            continue
        entry = ic_summary(daily)
        entry["daily"] = [
            {"date": str(row.decision_date), "ic": float(row.ic)}
            for row in daily.itertuples(index=False)
        ]
        block[f"{key}d"] = entry
    block["statistical_unit"] = "decision_date"
    return block


def _quantile_block(frame: pd.DataFrame) -> dict[str, object]:
    """分位单调性（Deep50 上的 Q1..Q10，5D 超额；M3 §10.7）。"""
    column = metric_column("excess_return", 5)
    if "alpha_rank" not in frame.columns:
        return {"status": NOT_AVAILABLE, "reason": "alpha_rank 缺失"}
    deep = frame[_bool_series(frame, "in_deep_pool")]
    pool = deep if not deep.empty else frame
    sample = _main_sample(pool, 5)
    if sample.empty or sample["alpha_rank"].replace(NOT_AVAILABLE, np.nan).notna().sum() < 30:
        return {"status": NOT_AVAILABLE, "reason": "成熟样本不足以做十分位（<30 行）"}
    table = quantile_returns(
        sample,
        score_column="alpha_rank",
        metric_column_=column,
        quantiles=10,
    )
    mono = quantile_monotonicity(table)
    return {"status": "ok", "quantiles": table.to_dict(orient="records"), **mono}


def _winner_recall_block(frame: pd.DataFrame) -> dict[str, object]:
    """赢家召回：赢家 = 质量池内当日按 5D 真实可执行超额的前 10%（M3 §10.8）。

    四级成员取自快照（quality/light/deep/final=Top5），fail-closed 于"用预测
    分定义赢家"（compute_winner_recall 内部已拦）。
    """
    column = metric_column("excess_return", 5)
    sample = _main_sample(frame, 5)
    if sample.empty or "in_quality_pool" not in sample.columns:
        return {"status": NOT_AVAILABLE, "reason": "无质量池成员标记"}
    work = sample.copy()
    work["quality_pool"] = _bool_series(work, "in_quality_pool")
    work["light_pool"] = _bool_series(work, "in_light_pool")
    work["deep_pool"] = _bool_series(work, "in_deep_pool")
    work["final_pool"] = _bool_series(work, "v2_top5")
    report = compute_winner_recall(
        work,
        spec=RecallSpec(metric=column, winner_quantile=DEFAULT_WINNER_QUANTILE, min_pool_size=10),
    )
    out = dict(report.summary)
    out["winner_definition"] = (
        f"quality 池内按真实 {column} 前 {int(DEFAULT_WINNER_QUANTILE * 100)}%"
        "（self-proof 校验已在模块内）"
    )
    return out


def _downside_block(frame: pd.DataFrame) -> dict[str, object]:
    """下行情形（M3 §10.9）：MAE / 5% 尾部 / 大亏损频率 / 止损命中。"""
    result: dict[str, object] = {}
    for k, field in _TOPK_FIELDS.items():
        selected = frame[_bool_series(frame, field)]
        sample = _main_sample(selected, 5)
        entry: dict[str, object] = {"rows": int(len(sample))}
        if sample.empty:
            result[f"top{int(k)}"] = entry
            continue
        mae = _num(sample, "mae_5d").dropna()
        net = _num(sample, "net_return_5d").dropna()
        if not mae.empty:
            entry["mean_mae_5d"] = float(mae.mean())
            entry["worst_mae_5d"] = float(mae.min())
        if not net.empty:
            entry["tail_return_5pct"] = float(net.quantile(0.05))
            entry["large_loss_frequency"] = float((net <= -0.05).mean())
            entry["worst_net_return_5d"] = float(net.min())
        # 止损命中 = 路径标签判"先碰 -5% 再碰 +8%"（S11 的 tp8_before_sl5_10d）
        if "tp8_before_sl5_10d" in sample.columns:
            labels = _num(sample, "tp8_before_sl5_10d").dropna()
            if not labels.empty:
                entry["stop_loss_hit_rate"] = float((labels < 0.5).mean())
        result[f"top{int(k)}"] = entry
    return result


def _execution_block(frame: pd.DataFrame) -> dict[str, object]:
    """执行（M3 §10.10）：全快照的 T+1 成交率 / no-fill 原因 / 缺口 / 滑点。"""
    if frame.empty:
        return {"status": NOT_AVAILABLE}
    block: dict[str, object] = {"rows": int(len(frame))}
    executable = _bool_series(frame, "executable") if "executable" in frame.columns else None
    if executable is None or not executable.any():
        block["fill_rate"] = NOT_AVAILABLE
    else:
        block["fill_rate"] = float(executable.mean())
        block["no_fill_count"] = int((~executable).sum())
        reasons: dict[str, int] = {}
        if "no_fill_reason" in frame.columns:
            counts = frame.loc[~executable, "no_fill_reason"].astype(str).value_counts()
            reasons = {str(key): int(value) for key, value in counts.items() if key}
        block["no_fill_reasons"] = reasons
        # 细分：一字涨停类（含 limit）vs 停牌类（suspended）
        limit_bucket = sum(v for k2, v in reasons.items() if "limit" in k2)
        suspend_bucket = sum(v for k2, v in reasons.items() if "suspend" in k2 or "missing" in k2)
        block["limit_up_no_fill_ratio"] = float(limit_bucket / len(frame)) if len(frame) else 0.0
        block["suspension_no_fill_ratio"] = (
            float(suspend_bucket / len(frame)) if len(frame) else 0.0
        )
    # 缺口：净入场价 vs 原始价（滑点）、入场原始价 vs 信号日收盘（gap）
    if "entry_price_net" in frame.columns and "entry_price_raw" in frame.columns:
        raw = _num(frame, "entry_price_raw")
        net = _num(frame, "entry_price_net")
        valid = raw.notna() & net.notna() & (raw > 0)
        if int(valid.sum()):
            slip = (net[valid] / raw[valid]) - 1.0
            block["entry_slippage_mean"] = float(slip.mean())
    if "signal_close_raw" in frame.columns and "entry_price_raw" in frame.columns:
        base = _num(frame, "signal_close_raw")
        entry = _num(frame, "entry_price_raw")
        valid = base.notna() & entry.notna() & (base > 0)
        if int(valid.sum()):
            gap = (entry[valid] / base[valid]) - 1.0
            block["entry_gap_mean"] = float(gap.mean())
            block["entry_gap_median"] = float(gap.median())
    if "entry_delay_sessions" in frame.columns:
        delay = _num(frame, "entry_delay_sessions").dropna()
        if not delay.empty:
            block["entry_delay_mean"] = float(delay.mean())
            block["entry_delay_max"] = float(delay.max())
    return block


def _baseline_pairing(frame: pd.DataFrame) -> dict[str, object]:
    """Simple Baseline 对照（M3 §10.5）：同日 Top5 配对 —— baseline 取分最高 5 只。"""
    if "baseline_score" not in frame.columns:
        return {"status": NOT_AVAILABLE, "reason": "baseline_score 未入快照"}
    sample = _main_sample(frame, 5)
    values = _num(frame, "baseline_score")
    if sample.empty or values.notna().sum() == 0:
        return {"status": NOT_AVAILABLE, "reason": "无成熟样本或 baseline_score 全空"}
    pairs: list[dict[str, object]] = []
    for day, group in sample.groupby(sample["signal_date"].astype(str), sort=True):
        scores = _num(group, "baseline_score")
        has = scores.notna()
        if int(has.sum()) < 5:
            continue
        ranked = group.loc[has].copy()
        ranked["__score"] = scores[has]
        top = ranked.nlargest(5, "__score")
        legacy_v2 = group[_bool_series(group, "v2_top5")]
        v2_vals = _num(legacy_v2, "net_return_5d").dropna()
        base_vals = _num(top, "net_return_5d").dropna()
        if v2_vals.empty or base_vals.empty:
            continue
        pairs.append(
            {
                "date": str(day),
                "baseline_top5_mean": float(base_vals.mean()),
                "v2_top5_mean": float(v2_vals.mean()),
                "diff": float(v2_vals.mean() - base_vals.mean()),
            }
        )
    if not pairs:
        return {"status": "insufficient_paired_days"}
    diff = np.array([row["diff"] for row in pairs], dtype=float)
    return {
        "status": "ok",
        "paired_days": int(len(pairs)),
        "mean_diff_5d_net": float(diff.mean()),
        "positive_days_ratio": float((diff > 0).mean()),
        "pairs": pairs,
    }


def _cohort_counts(frame: pd.DataFrame) -> dict[str, object]:
    counts: dict[str, object] = {"rows": int(len(frame))}
    for column in (
        "in_quality_pool",
        "in_light_pool",
        "in_deep_pool",
        "v2_top1",
        "v2_top3",
        "v2_top5",
    ):
        counts[column] = int(_bool_series(frame, column).sum())
    if "fillable" in frame.columns:
        counts["fillable"] = int(_bool_series(frame, "fillable").sum())
    return counts


def _model_identity_block(freeze: Mapping[str, object]) -> dict[str, object]:
    model = freeze.get("model")
    if not isinstance(model, Mapping):
        return {"status": NOT_AVAILABLE}
    return {
        "model_id": model.get("model_id", NOT_AVAILABLE),
        "artifact_hash": model.get("artifact_hash", NOT_AVAILABLE),
        "artifact_created_at": model.get("artifact_created_at", NOT_AVAILABLE),
        "status": model.get("status", NOT_AVAILABLE),
    }


# ---------------------------------------------------------------------------
# 落盘与渲染
# ---------------------------------------------------------------------------


def _sanitize_for_strict_json(value: object) -> object:
    """把工作负载清成严格合法 JSON：NaN/±Inf 一律落到 ``not_available``。

    ci95 这类统计区间在样本不足时会产生 NaN；旧版直接写进文件，严格解析器
    （JSON.parse / jq / Go）会拒绝整份报告——证据文件本身不可用。
    """
    import math

    if isinstance(value, float) and not math.isfinite(value):
        return NOT_AVAILABLE
    if isinstance(value, Mapping):
        return {str(key): _sanitize_for_strict_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_for_strict_json(item) for item in value]
    return value


def write_kpi_report(
    *, root: str | Path, epoch: EpochRecord, payload: Mapping[str, object]
) -> tuple[Path, Path]:
    """JSON + Markdown 双落盘（``reports/validation_kpi_<epoch>_<date>``）。

    写前严格自检：``json.dumps(..., allow_nan=False)``——任何漏网 NaN/Inf 都
    在这里被打回，绝不产出非法 JSON 的证据文件。
    """
    import json

    from stock_analyzer.alpha_v2.artifacts import write_json_atomic

    day = str(payload.get("report_date", datetime.now().date().isoformat()))
    token = day.replace("-", "")
    reports_dir = epoch_subdirs(root, epoch.epoch_id)["reports"]
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / f"{KPI_REPORT_PREFIX}_{epoch.epoch_id}_{token}.json"
    md_path = reports_dir / f"{KPI_REPORT_PREFIX}_{epoch.epoch_id}_{token}.md"
    sanitized = _sanitize_for_strict_json(dict(payload))
    # 自检：assert strict JSON 合法（这是审计对象，必须能被严格解析器读开）
    json.dumps(sanitized, ensure_ascii=False, allow_nan=False)
    write_json_atomic(json_path, sanitized)
    md_path.write_text(render_kpi_markdown(sanitized), encoding="utf-8")
    return json_path, md_path


def render_kpi_markdown(payload: Mapping[str, object]) -> str:
    lines: list[str] = []
    lines.append(f"# Alpha V2 Validation KPI — {payload.get('validation_epoch_id')}")
    lines.append(f"- report_date: {payload.get('report_date')}")
    lines.append(
        f"- epoch_status: {payload.get('epoch_status')}  "
        f"opened_on: {payload.get('epoch_opened_on')}"
    )
    lines.append(f"- model: {_fmt(payload.get('model'))}")
    lines.append(
        f"- alpha_verified: {payload.get('alpha_verified')}  "
        f"production_promotion: {payload.get('production_promotion')}"
    )
    lines.append("")
    lines.append("## 样本门（成熟决策日，仅统计 clean_oos_eligible）")
    lines.append(f"- maturity: {_fmt(payload.get('maturity'))}")
    lines.append(f"- clean_maturity: {_fmt(payload.get('clean_maturity'))}")
    lines.append(f"- governance: {_fmt(payload.get('governance'))}")
    gates = payload.get("sample_gate_status")
    if isinstance(gates, Mapping):
        for name, gate in gates.items():
            lines.append(f"- {name}: {_fmt(gate)}")
    for name in (
        "hit_rate",
        "returns",
        "excess_vs_benchmarks",
        "rank_ic",
        "quantile_monotonicity",
        "winner_recall",
        "downside",
        "execution",
        "baseline_pairing",
        "cohort_counts",
    ):
        lines.append(f"## {name}")
        lines.append(_fmt(payload.get(name)))
        lines.append("")
    missing = payload.get("missing_prediction_days")
    if missing:
        lines.append("## missing_prediction_days（不计入 clean OOS）")
        for item in missing:  # type: ignore[union-attr]
            lines.append(f"- {item.get('signal_date')}: {item.get('reason')}")
    lines.append("")
    lines.append(f"> {payload.get('statement_boundary', '')}")
    return "\n".join(lines)


def _fmt(value: object) -> str:
    if isinstance(value, Mapping):
        items = "; ".join(f"{key}={_fmt(val)}" for key, val in value.items())
        return "{" + items + "}"
    if isinstance(value, list):
        if len(value) > 6:
            return f"[{len(value)} items]"
        return "[" + ", ".join(_fmt(item) for item in value) + "]"
    return str(value)


__all__ = [
    "FROZEN_BENCHMARK_LAYERS",
    "KPI_REPORT_PREFIX",
    "KPI_SCHEMA",
    "SAMPLE_GATES",
    "KpiReportError",
    "build_validation_kpi",
    "load_epoch_frames",
    "render_kpi_markdown",
    "write_kpi_report",
]
