"""Alpha V2 P4-B1 历史扫描效果成绩单（Historical Scan Performance Scorecard）。

回答一个问题：**系统过去扫描出来的股票，实际表现如何？**

本模块是纯读取的研究报告层，只消费 M4-H Historical Locked OOS 的既有工件，
不重新生成任何数据：

- ``predictions/fold_[0-9][0-9][0-9].json``
  phase A 逐票预测（``rank_score`` + 5d 收益）。``*_b.json`` 是 phase B 概率层，
  按仓库既有约定必须排除——参见 ``tests/test_alpha_v2_m4h_phase_isolation.py``
  （把 ``_b`` 读进来会让样本翻倍、报告不可复现）。
- ``audit/run_manifest.json``
  定位 dataset cache，从而唯一定位 outcomes pickle（一个 root 里可能残留多个
  历史实验的 cache，必须按 run_manifest 解析，不能 glob）。
- ``cache/outcomes_<key>.pkl``
  唯一带分 horizon ``net/excess/MAE/MFE/matured`` 的 outcome 来源；预测 JSON
  本身只携带 5d（schema 如此，不是缺陷）。
- ``metrics/metrics_summary.json``、``audit/leakage_audit.json``
  provenance 转录（缺失只降级为 warning，不阻断）。

纪律（与任务书 P4-B1 及 ``alpha_v2/research/__init__.py`` 一致）：

1. **只读**：绝不写任何 M4-H 工件；输出只落在调用方指定的 out 目录，且 out
   不得位于 m4h root 内部（防止覆盖证据目录）。
2. 不改模型 / 特征 / label / ``HORIZONS`` / M4-H 协议 / M4-L runtime / 生产路径
   ——本模块不 import 训练与验证链路的任何写路径。
3. 非训练 horizon（如 T+20/T+60）只能作为**评价指标**从 outcome 数据读取；
   outcome 列不存在时如实标注 unavailable，不推算、不编造。
4. **fail closed**：predictions 缺失、schema 不符、协议混杂、预测行对不上
   outcome 帧、预测 5d 与 outcome 帧 5d 数值矛盾、跨 fold 决策日重叠
   → 抛 :class:`ScorecardError`（带真实退出码），不产出半份报告。

收益口径（镜像锁定协议自身的评估口径，见 ``scripts/alpha_v2_m4h_run.py``
"执行契约：只有 executable 行允许进入可成交收益口径"）：

- 统计总体 = ``executable == True`` 且该 horizon ``matured == True`` 且收益为数值；
- ``not_available`` 哨兵（= 未成熟 / 不可成交）一律剔除并单独计数；
- excess 收益的基准按工件内的 ``benchmark_name`` 如实转录（实测为
  ``eligible_ew``），本层不选择、不重算基准。

退出码（本 CLI 自己的约定；仓库无中心注册表，按脚本各自成惯例，见
NOTE-001 §4）：

===  =============================================================
0     成功
2     参数 / 输入路径错误（含 out 目录位于 m4h root 内）
3     所需 outcome 数据缺失或不可用（显式 ``allow_missing_outcomes`` 降级除外）
4     工件 schema / 一致性违例（协议混杂、join 缺行、5d 数值矛盾、重复键）
===  =============================================================
"""

from __future__ import annotations

import json
import os
import pickle
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

__all__: list[str] = [
    "NOT_AVAILABLE",
    "PHASE_A_PREDICTION_GLOB",
    "OutcomeDataUnavailable",
    "ScorecardContractError",
    "ScorecardError",
    "ScorecardInputError",
    "build_scorecard",
    "discover_horizons",
    "load_outcome_frame",
    "load_predictions",
    "load_run_manifest",
    "render_markdown",
    "resolve_outcome_cache_path",
    "run_scorecard",
    "write_outputs",
]

# ---------------------------------------------------------------------------
# 常量（全部对照 artifacts/alpha_v2/m4h 实测 schema，不假设字段）
# ---------------------------------------------------------------------------

SCORECARD_SCHEMA = "alpha_v2_p4_scorecard.v1"
PREDICTION_SCHEMA = "alpha_v2_m4h_fold.v1"
NOT_AVAILABLE = "not_available"

#: phase A 预测文件名模式；``_b`` 后缀是 phase B 概率层，必须排除（仓库既有约定）。
PHASE_A_PREDICTION_GLOB = "fold_[0-9][0-9][0-9].json"

JOIN_KEYS = ["decision_date", "symbol"]

REQUIRED_PREDICTION_COLUMNS = (
    "protocol_id",
    "fold_id",
    "decision_date",
    "symbol",
    "rank_score",
    "net_return_5d",
    "excess_return_5d",
    "mae_5d",
    "mfe_5d",
    "entry_date",
    "entry_price_raw",
    "no_fill_reason",
    "executable",
)

#: run_manifest.dataset.dataset_cache 形如 "saved:dataset_<key>.pkl"。
_DATASET_CACHE_RE = re.compile(r"^saved:dataset_(?P<key>[0-9A-Za-z_]{4,64})\.pkl$")
#: outcome 帧中的 horizon 列族：net_return_{h}d / matured_{h}d 必须成对出现。
_HORIZON_COLUMN_RE = re.compile(r"^net_return_(\d+)d$")

DEFAULT_PRIMARY_HORIZON = 5
#: 任务书点名的最小 horizon 集（缺失只告警，不编造）。
TASK_MINIMUM_HORIZONS = (3, 5, 10, 15)
#: 任务书点名的"只作评价指标"horizon（绝不能进训练 label）。
EVALUATION_ONLY_HORIZONS = (20, 60)

_TOOL_NAME = "alpha_v2_p4_scorecard"
#: 预测 JSON 可能有舍入；5d 收益与 outcome 帧差值超过该容差视为两份数据不一致。
_FLOAT_TOLERANCE = 1e-6


# ---------------------------------------------------------------------------
# 异常：exit_code 必须被 CLI 真实返回（ADR-001 §3.7 纪律）
# ---------------------------------------------------------------------------


class ScorecardError(RuntimeError):
    """成绩单失败基类。``exit_code`` 由 CLI 返回给进程。"""

    def __init__(self, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class ScorecardInputError(ScorecardError):
    """参数 / 输入路径错误。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=2)


class OutcomeDataUnavailable(ScorecardError):
    """所需 outcome 数据缺失或不可用（fail closed，除非显式降级）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=3)


class ScorecardContractError(ScorecardError):
    """工件 schema / 一致性违例。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=4)


# ---------------------------------------------------------------------------
# 加载层
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ScorecardInputError(f"无法读取 JSON 工件 {path}: {exc}") from exc


def load_predictions(m4h_root: Path) -> dict[str, Any]:
    """加载全部 phase A 预测文件并做结构性校验。

    返回 ``{"frame": DataFrame, "files": [...], "protocol_id": str}``。
    fail closed：无文件、schema 不符、行数不符、协议混杂、重复键、跨 fold 日期重叠。
    """
    predictions_dir = m4h_root / "predictions"
    if not predictions_dir.is_dir():
        raise ScorecardInputError(f"预测目录不存在: {predictions_dir}")
    files = sorted(predictions_dir.glob(PHASE_A_PREDICTION_GLOB))
    if not files:
        raise ScorecardInputError(
            f"{predictions_dir} 下没有 phase A 预测文件（模式 {PHASE_A_PREDICTION_GLOB}）；"
            "缺 fold 文件时不得生成成绩单（fail closed）"
        )

    frames: list[pd.DataFrame] = []
    protocol_ids: list[str] = []
    file_infos: list[dict[str, Any]] = []
    for path in files:
        payload = _read_json(path)
        if not isinstance(payload, dict) or payload.get("schema") != PREDICTION_SCHEMA:
            raise ScorecardContractError(
                f"{path.name} 的 schema 不是 {PREDICTION_SCHEMA}，拒绝对未知 schema 出报告"
            )
        records = payload.get("records")
        if not isinstance(records, list) or not records:
            raise ScorecardContractError(f"{path.name} 无 records，拒绝对空预测出报告")
        declared_rows = payload.get("rows")
        if not isinstance(declared_rows, int) or declared_rows != len(records):
            raise ScorecardContractError(
                f"{path.name} 声明 rows={declared_rows} 但实际 records={len(records)}"
            )
        missing_cols = [c for c in REQUIRED_PREDICTION_COLUMNS if c not in records[0]]
        if missing_cols:
            raise ScorecardContractError(
                f"{path.name} 缺少必需列 {missing_cols}（以实测 schema 为准，不猜测字段）"
            )
        frames.append(pd.DataFrame.from_records(records))
        protocol_ids.append(str(payload.get("protocol_id")))
        file_infos.append(
            {
                "file": f"predictions/{path.name}",
                "fold_id": payload.get("fold_id"),
                "rows": len(records),
            }
        )

    unique_protocols = sorted(set(protocol_ids))
    if len(unique_protocols) > 1:
        raise ScorecardContractError(
            f"预测文件 protocol_id 混杂 {unique_protocols}，疑似混入不同 run 的工件"
        )

    frame = pd.concat(frames, ignore_index=True)
    frame["decision_date"] = frame["decision_date"].astype(str)
    frame["symbol"] = frame["symbol"].astype(str)
    dup = int(frame.duplicated(subset=["fold_id", *JOIN_KEYS]).sum())
    if dup:
        raise ScorecardContractError(f"预测存在重复 (fold_id, decision_date, symbol) 键 {dup} 行")
    # 同一决策日出现在多个 fold 会让 pooled 统计双计数（锁定协议 fold 窗不重叠，
    # 这里显式钉住该前提，而不是默默假设）。
    overlap = frame.groupby("decision_date")["fold_id"].nunique()
    if int(overlap.max()) > 1:
        shared = sorted(overlap[overlap > 1].index)[:5]
        raise ScorecardContractError(
            f"决策日跨 fold 重叠（如 {shared}），pooled 统计会双计数，fail closed"
        )
    return {"frame": frame, "files": file_infos, "protocol_id": unique_protocols[0]}


def load_run_manifest(m4h_root: Path) -> dict[str, Any]:
    """加载 ``audit/run_manifest.json``（outcome 解析与 provenance 的依据）。"""
    path = m4h_root / "audit" / "run_manifest.json"
    if not path.is_file():
        raise OutcomeDataUnavailable(
            f"缺少 {path}：无法唯一定位 outcome cache"
            "（一个 root 可能残留多个实验 cache），fail closed"
        )
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise ScorecardContractError(f"{path} 不是 JSON 对象")
    return payload


def resolve_outcome_cache_path(m4h_root: Path, run_manifest: dict[str, Any]) -> Path:
    """从 run_manifest.dataset.dataset_cache 解析 outcomes pickle 路径。"""
    dataset = run_manifest.get("dataset")
    ref = dataset.get("dataset_cache") if isinstance(dataset, dict) else None
    match = _DATASET_CACHE_RE.match(str(ref)) if ref else None
    if not match:
        raise OutcomeDataUnavailable(
            f"run_manifest.dataset.dataset_cache={ref!r} 无法解析出 cache key，fail closed"
        )
    return m4h_root / "cache" / f"outcomes_{match.group('key')}.pkl"


def load_outcome_frame(cache_path: Path) -> dict[str, Any]:
    """加载 outcomes pickle（结构为 dict{"frame": DataFrame, "diagnostics": dict}）。"""
    if not cache_path.is_file():
        raise OutcomeDataUnavailable(f"outcome cache 缺失: {cache_path}（fail closed）")
    try:
        with open(cache_path, "rb") as handle:
            payload = pickle.load(handle)  # noqa: S301 - 本仓库自产的研究工件
    except Exception as exc:  # pickle 损坏 / 依赖版本不兼容都属于"不可用"
        raise OutcomeDataUnavailable(f"outcome cache 无法加载: {cache_path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("frame"), pd.DataFrame):
        raise OutcomeDataUnavailable(f"outcome cache 结构不符合预期: {cache_path}")
    frame = payload["frame"]
    for col in ("decision_date", "symbol", "executable"):
        if col not in frame.columns:
            raise OutcomeDataUnavailable(f"outcome 帧缺少必需列 {col!r}")
    dup = int(frame.duplicated(subset=JOIN_KEYS).sum())
    if dup:
        raise ScorecardContractError(f"outcome 帧存在重复 (decision_date, symbol) 键 {dup} 行")
    return payload


def discover_horizons(columns: list[str]) -> list[int]:
    """从 outcome 帧列名发现 horizon（要求 net_return_{h}d 与 matured_{h}d 成对）。"""
    horizons: set[int] = set()
    for col in columns:
        match = _HORIZON_COLUMN_RE.match(col)
        if match and f"matured_{match.group(1)}d" in columns:
            horizons.add(int(match.group(1)))
    return sorted(horizons)


# ---------------------------------------------------------------------------
# 统计层（纯函数，便于用手算值做正确性测试）
# ---------------------------------------------------------------------------


def _num(frame: pd.DataFrame, column: str) -> pd.Series | None:
    """object 列（混杂 "not_available" 哨兵与 float）安全转数值；列缺则 None。"""
    if column not in frame.columns:
        return None
    return pd.to_numeric(frame[column], errors="coerce")


def _mean_or_none(values: pd.Series) -> float | None:
    return round(float(values.mean()), 8) if len(values) else None


def _stats(values: pd.Series) -> dict[str, Any]:
    return {
        "samples": int(len(values)),
        "average_return": _mean_or_none(values),
        "median_return": round(float(values.median()), 8) if len(values) else None,
        "positive_rate": round(float((values > 0).mean()), 8) if len(values) else None,
        "best_return": round(float(values.max()), 8) if len(values) else None,
        "worst_return": round(float(values.min()), 8) if len(values) else None,
    }


def _horizon_section(
    merged: pd.DataFrame, horizon: int, *, executable: pd.Series, maturity_known: bool
) -> dict[str, Any]:
    """单 horizon 的收益统计（总体 = executable ∧ matured ∧ 数值收益）。"""
    net = _num(merged, f"net_return_{horizon}d")
    if net is None:
        raise OutcomeDataUnavailable(f"缺少 net_return_{horizon}d 列")
    matured_col = f"matured_{horizon}d"
    matured = merged[matured_col].eq(True) if matured_col in merged.columns else None

    base = executable & net.notna()
    if maturity_known:
        if matured is None:
            raise OutcomeDataUnavailable(f"缺少 {matured_col} 列（maturity_known 模式必须成对）")
        population = base & matured
        immature_excluded: int | None = int((executable & ~matured & net.isna()).sum())
    else:
        population = base
        immature_excluded = None

    section = _stats(net[population])
    excess = _num(merged, f"excess_return_{horizon}d")
    if excess is not None:
        section["average_excess_return"] = _mean_or_none(excess[population])
    mae = _num(merged, f"mae_{horizon}d")
    if mae is not None:
        section["MAE"] = _mean_or_none(mae[population])
    mfe = _num(merged, f"mfe_{horizon}d")
    if mfe is not None:
        section["MFE"] = _mean_or_none(mfe[population])
    section["immature_excluded"] = immature_excluded
    exit_col = f"exit_no_fill_{horizon}d"
    if exit_col in merged.columns:
        section["exit_no_fill_rows"] = int(merged[exit_col].eq(True).sum())
    return section


def _bucket_section(net: pd.Series, score: pd.Series) -> dict[str, Any]:
    """评分分层 + 单调性检验（如实报告，不人为调整）。"""
    buckets: list[dict[str, Any]] = []
    # 与 int(score*10) 截断语义一致的十分位索引（1.0 → 桶 9）。
    bucket_index = (score * 10).astype(int).clip(upper=9)
    for index in range(9, -1, -1):
        values = net[bucket_index == index]
        buckets.append(
            {
                "score_bucket": f"{index * 10}-{index * 10 + 10}",
                "samples": int(len(values)),
                "avg_return": _mean_or_none(values),
                "win_rate": (round(float((values > 0).mean()), 8) if len(values) else None),
            }
        )
    filled = [b for b in buckets if b["samples"] > 0]
    violations = [
        {
            "higher_bucket": filled[i]["score_bucket"],
            "lower_bucket": filled[i + 1]["score_bucket"],
            "higher_avg": filled[i]["avg_return"],
            "lower_avg": filled[i + 1]["avg_return"],
        }
        for i in range(len(filled) - 1)
        if (filled[i + 1]["avg_return"] or 0.0) > (filled[i]["avg_return"] or 0.0)
    ]
    spread = (
        round(float(filled[0]["avg_return"]) - float(filled[-1]["avg_return"]), 8)
        if len(filled) >= 2
        else None
    )
    return {
        "buckets": buckets,
        "monotonic_nonincreasing": not violations,
        "monotonicity_violations": violations,
        "top_bottom_spread": spread,
        "note": "avg return 沿高分桶到低分桶应单调不增；violations 为空即单调。未做任何人工调整。",
    }


def _yearly_section(primary_frame: pd.DataFrame, net: pd.Series) -> list[dict[str, Any]]:
    years = primary_frame["decision_date"].astype(str).str.slice(0, 4)
    rows: list[dict[str, Any]] = []
    for year in sorted(years.unique()):
        mask = years == year
        values = net[mask]
        rows.append(
            {
                "year": str(year),
                "decision_days": int(primary_frame.loc[mask, "decision_date"].nunique()),
                "samples": int(len(values)),
                "avg_return": _mean_or_none(values),
                "win_rate": (round(float((values > 0).mean()), 8) if len(values) else None),
            }
        )
    return rows


def _regime_section(regime_values: pd.Series | None, net: pd.Series) -> dict[str, Any]:
    """market_regime 仅当工件已有该字段时输出，且必须标注 ex-post。"""
    if regime_values is None:
        return {
            "available": False,
            "rows": [],
            "note": "工件不含 market_regime 字段。若未来加入，属 ex-post 分析字段，"
            "只能用于解释历史表现，不得进入预测输入。",
        }
    values = regime_values.astype(str)
    rows = []
    for regime in sorted(values.unique()):
        sub = net[values == regime]
        rows.append(
            {
                "regime": str(regime),
                "samples": int(len(sub)),
                "avg_return": _mean_or_none(sub),
                "win_rate": (round(float((sub > 0).mean()), 8) if len(sub) else None),
            }
        )
    return {
        "available": True,
        "rows": rows,
        "note": "market_regime 是 ex-post 分析字段：只用于解释历史表现，不得进入预测输入。",
    }


def _round_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(result):
        return None
    return round(result, 8)


def _worst_rows(
    primary_frame: pd.DataFrame, primary_net: pd.Series, top_n: int, horizon: int
) -> list[dict[str, Any]]:
    """失败分析：主 horizon 净收益最低的 Top N（含 MAE/MFE，供归因）。"""
    order = primary_net.sort_values(kind="stable").head(top_n).index
    rows: list[dict[str, Any]] = []
    for idx in order:
        row = primary_frame.loc[idx]
        rows.append(
            {
                "symbol": str(row["symbol"]),
                "decision_date": str(row["decision_date"]),
                "score": _round_or_none(row["rank_score"]),
                "return": round(float(primary_net.loc[idx]), 8),
                "MAE": _round_or_none(row.get(f"mae_{horizon}d")),
                "MFE": _round_or_none(row.get(f"mfe_{horizon}d")),
                "entry_date": str(row.get("entry_date", "")),
                "fold_id": int(row["fold_id"]) if "fold_id" in row.index else None,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# 组装层
# ---------------------------------------------------------------------------


def _merge_and_validate(pred_frame: pd.DataFrame, oc_frame: pd.DataFrame) -> dict[str, Any]:
    """把预测与 outcome 帧按 (decision_date, symbol) 合并并执行一致性硬门。

    返回 ``{"merged", "horizons", "regime_source", "join_report"}``。
    任何违例（join 缺行 / executable 不一致 / 5d 数值或哨兵矛盾 / 重复键）
    抛 :class:`ScorecardContractError`，供 fallback 逐候选验证复用。
    """
    horizons = discover_horizons(list(oc_frame.columns))
    # regime 字段（若工件已有）必须在裁剪列之前发现，否则进不了 merge。
    regime_source = next((c for c in oc_frame.columns if "regime" in str(c).lower()), None)
    wanted = set(JOIN_KEYS) | {"executable", "benchmark_name"}
    if regime_source is not None:
        wanted.add(regime_source)
    for horizon in horizons:
        wanted |= {
            f"net_return_{horizon}d",
            f"matured_{horizon}d",
            f"excess_return_{horizon}d",
            f"mae_{horizon}d",
            f"mfe_{horizon}d",
            f"exit_no_fill_{horizon}d",
        }
    # 预测侧与 outcome 侧存在同名统计列（net_return_5d 等）：
    # outcome 列带 oc_ 前缀进 merge，校验后再替换预测侧的同名列，
    # 避免出现重复列名。
    outcome_targets = wanted - set(JOIN_KEYS)
    available = {c for c in outcome_targets if c in oc_frame.columns}
    # 注意列顺序必须与赋名严格一致：JOIN_KEYS 本身就是 list，
    # 不能用 list(set(JOIN_KEYS))（哈希顺序不保证，会错位换名）。
    renamed = oc_frame[JOIN_KEYS + sorted(available)].copy()
    renamed.columns = JOIN_KEYS + [f"oc_{c}" for c in sorted(available)]
    merged = pred_frame.merge(renamed, on=JOIN_KEYS, how="left", validate="many_to_one")

    missing_rows = int(merged["oc_executable"].isna().sum())
    if missing_rows:
        raise ScorecardContractError(
            f"{missing_rows} 行预测在 outcome 帧中找不到 (decision_date, symbol)，"
            "工件不一致，fail closed"
        )
    exe_disagree = int(
        (
            pred_frame["executable"].eq(True).to_numpy()
            != merged["oc_executable"].eq(True).to_numpy()
        ).sum()
    )
    if exe_disagree:
        raise ScorecardContractError(
            f"预测与 outcome 帧的 executable 标志有 {exe_disagree} 行不一致"
        )
    p5 = pd.to_numeric(pred_frame["net_return_5d"], errors="coerce")
    o5 = pd.to_numeric(merged["oc_net_return_5d"], errors="coerce")
    both = p5.notna() & o5.notna()
    max_diff = float((p5[both] - o5[both]).abs().max()) if bool(both.any()) else 0.0
    sentinel_disagree = int((p5.isna() != o5.isna()).sum())
    if sentinel_disagree or max_diff > _FLOAT_TOLERANCE:
        raise ScorecardContractError(
            f"预测 5d 收益与 outcome 帧矛盾（哨兵不一致 {sentinel_disagree} 行，"
            f"max|Δ|={max_diff:.3e}），fail closed"
        )
    # 校验通过后改用 outcome 帧侧（全精度 + matured 标志）：
    # 删掉预测侧同名统计列，再把 oc_ 前缀列还原成裸名。
    collisions = [c for c in sorted(available) if c in pred_frame.columns]
    merged = merged.drop(columns=collisions)
    merged = merged.rename(columns={f"oc_{c}": c for c in sorted(available)})
    return {
        "merged": merged,
        "horizons": horizons,
        "regime_source": regime_source,
        "join_report": {
            "mode": "joined_on_decision_date_symbol",
            "joined_rows": int(len(merged)),
            "max_abs_diff_5d": round(max_diff, 12),
        },
    }


def build_scorecard(
    predictions: dict[str, Any],
    outcome: dict[str, Any] | None,
    *,
    run_manifest: dict[str, Any] | None,
    metrics_summary: dict[str, Any] | None,
    leakage_audit: dict[str, Any] | None,
    m4h_root: Path,
    top_n: int = 20,
    outcome_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把加载好的工件组装成 scorecard dict（统计层为纯函数，可独立测试）。"""
    warnings: list[str] = []
    pred_frame = predictions["frame"]
    if outcome_resolution and outcome_resolution.get("mode") == "verified_fallback":
        warnings.append(
            "manifest 指向的 outcome cache 已不存在（"
            f"{outcome_resolution.get('manifest_ref')}），"
            "改用通过逐行一致性验证（join 完整 + executable 一致 + 5d 数值一致）"
            f"的候选 cache {outcome_resolution.get('path')}"
        )

    metrics_summary = metrics_summary or {}
    if not metrics_summary:
        warnings.append("metrics/metrics_summary.json 缺失：provenance 转录不完整")
    leakage_audit = leakage_audit or {}
    if not leakage_audit:
        warnings.append("audit/leakage_audit.json 缺失：泄漏审计计数未转录")

    primary_horizon = metrics_summary.get("primary_horizon", DEFAULT_PRIMARY_HORIZON)
    if not isinstance(primary_horizon, int):
        primary_horizon = DEFAULT_PRIMARY_HORIZON

    if outcome is None:
        # 降级模式：只吃预测自带的 5d（无 matured 标志，成熟度未知）。
        merged = pred_frame.copy()
        horizons = [5] if "net_return_5d" in merged.columns else []
        maturity_known = False
        regime_source: str | None = None
        join_report: dict[str, Any] = {
            "mode": "predictions_only_degraded",
            "joined_rows": int(len(merged)),
        }
        warnings.append(
            "outcome 帧不可用（显式降级）：仅 T+5 可统计、成熟度未知，其余 horizon 一律 unavailable"
        )
    else:
        info = _merge_and_validate(pred_frame, outcome["frame"])
        merged = info["merged"]
        horizons = info["horizons"]
        regime_source = info["regime_source"]
        maturity_known = True
        join_report = info["join_report"]

    if not horizons:
        raise OutcomeDataUnavailable(
            "没有任何可统计的 horizon（预测缺 net_return_5d 且无 outcome 帧）"
        )
    if primary_horizon not in horizons:
        raise OutcomeDataUnavailable(f"主 horizon {primary_horizon} 在 outcome 数据中不存在")

    for horizon in TASK_MINIMUM_HORIZONS:
        if horizon not in horizons:
            warnings.append(
                f"任务书最小 horizon 集 (3,5,10,15) 缺 T+{horizon}：如实标注 unavailable"
            )
    capability: dict[str, Any] = {
        "outcome_horizons_present": horizons,
        "horizons_unavailable": {
            str(h): f"outcome 数据无 net_return_{h}d/matured_{h}d 列"
            for h in (*TASK_MINIMUM_HORIZONS, *EVALUATION_ONLY_HORIZONS)
            if h not in horizons
        },
        "t20_t60_usage": (
            "T+20/T+60 只能作为评价指标，绝不修改训练 label（HORIZONS 常量不受本层影响）"
        ),
    }

    executable = merged["executable"].eq(True)
    horizon_stats = {
        f"T+{h}": _horizon_section(merged, h, executable=executable, maturity_known=maturity_known)
        for h in horizons
    }

    net_primary = pd.to_numeric(merged[f"net_return_{primary_horizon}d"], errors="coerce")
    primary_mask = executable & net_primary.notna()
    if maturity_known:
        primary_mask = primary_mask & merged[f"matured_{primary_horizon}d"].eq(True)
    primary_frame = merged[primary_mask]
    primary_net = net_primary[primary_mask]

    score = pd.to_numeric(primary_frame["rank_score"], errors="coerce")
    scored = score.notna()
    bucket_section = _bucket_section(primary_net[scored], score[scored])
    yearly = _yearly_section(primary_frame, primary_net)

    regime = _regime_section(
        merged[regime_source]
        if regime_source is not None and regime_source in merged.columns
        else None,
        primary_net,
    )
    worst = _worst_rows(primary_frame, primary_net, top_n, primary_horizon)

    decision_days = int(pred_frame["decision_date"].nunique())
    metrics_days = metrics_summary.get("historical_locked_oos_mature_decision_dates")
    if metrics_days is not None and metrics_days != decision_days:
        warnings.append(
            f"决策日数 {decision_days} 与 metrics_summary."
            f"historical_locked_oos_mature_decision_dates={metrics_days} 不一致，请核对"
        )

    no_fill_by_reason = {
        str(k): int(v)
        for k, v in pred_frame.loc[~executable, "no_fill_reason"].astype(str).value_counts().items()
    }

    protocol_block = (run_manifest or {}).get("protocol", {})
    diagnostics = (outcome or {}).get("diagnostics", {})
    benchmark_names: dict[str, int] = {}
    if "benchmark_name" in merged.columns:
        benchmark_names = {
            str(k): int(v) for k, v in merged["benchmark_name"].value_counts(dropna=False).items()
        }

    return {
        "schema": SCORECARD_SCHEMA,
        "tool": _TOOL_NAME,
        "generated_at": datetime.now(UTC).isoformat(),
        "m4h_root": str(m4h_root),
        "read_only": True,
        "overall": {
            "evaluation_period": {
                "from": str(pred_frame["decision_date"].min()),
                "to": str(pred_frame["decision_date"].max()),
            },
            "decision_count": decision_days,
            "prediction_rows": int(len(pred_frame)),
            "symbols_count": int(pred_frame["symbol"].nunique()),
            "fold_count": (
                int(pred_frame["fold_id"].nunique()) if "fold_id" in pred_frame.columns else None
            ),
            "executable_rows": int(executable.sum()),
            "no_fill_rows": int((~executable).sum()),
            "no_fill_by_reason": no_fill_by_reason,
            "mature_outcome_count_by_horizon": {
                f"T+{h}": horizon_stats[f"T+{h}"]["samples"] for h in horizons
            },
            "primary_horizon": primary_horizon,
            "primary_mature_outcome_count": horizon_stats[f"T+{primary_horizon}"]["samples"],
        },
        "horizon_stats": horizon_stats,
        "score_buckets": bucket_section,
        "yearly": yearly,
        "market_regime": regime,
        "failure_analysis": {"worst_return_top": worst},
        "capability": capability,
        "provenance": {
            "protocol_id": predictions["protocol_id"],
            "prediction_files": predictions["files"],
            "join": join_report,
            "outcome_cache_resolution": outcome_resolution,
            "run_protocol": {
                "experiment_id": protocol_block.get("experiment_id"),
                "code_commit": protocol_block.get("code_commit"),
                "protocol_hash": protocol_block.get("protocol_hash")
                or (run_manifest or {}).get("protocol_hash"),
                "window": protocol_block.get("window"),
                "label_policy": protocol_block.get("label_policy"),
                "execution_contract": protocol_block.get("execution_contract"),
            },
            "outcome_diagnostics": {
                "price_mode": diagnostics.get("price_mode"),
                "price_mode_certified": diagnostics.get("price_mode_certified"),
                "entry_mode": diagnostics.get("entry_mode"),
                "cost": diagnostics.get("cost"),
                "slippage_ratio": diagnostics.get("slippage_ratio"),
                "no_fill_by_reason": diagnostics.get("no_fill_by_reason"),
                "execution_uncertain_share": (
                    round(float(outcome["frame"]["execution_uncertain"].eq(True).mean()), 8)
                    if outcome is not None and "execution_uncertain" in outcome["frame"].columns
                    else None
                ),
            },
            "benchmark_name_distribution": benchmark_names,
            "metrics_summary_echo": {
                k: metrics_summary[k]
                for k in (
                    "folds_planned",
                    "folds_usable",
                    "historical_locked_oos_mature_decision_dates",
                    "alpha_verified",
                    "production_promotion",
                    "fold_ic_consistency",
                )
                if k in metrics_summary
            },
            "leakage_audit_echo": {
                k: leakage_audit[k]
                for k in (
                    "lookahead_violations",
                    "pit_violations",
                    "execution_violations",
                    "calibration_violations",
                )
                if k in leakage_audit
            },
        },
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 输出层
# ---------------------------------------------------------------------------


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.2f}%"


def render_markdown(scorecard: dict[str, Any]) -> str:
    """把 scorecard dict 渲染为中文 Markdown 成绩单。"""
    overall = scorecard["overall"]
    prov = scorecard["provenance"]
    run_protocol = prov["run_protocol"]
    diag = prov["outcome_diagnostics"]
    lines: list[str] = []
    add = lines.append

    add("# Alpha V2 历史扫描效果成绩单（P4-B1）")
    add("")
    add(f"- 数据源：`{scorecard['m4h_root']}`（protocol `{prov['protocol_id']}`）")
    add(
        f"- 生成时间：{scorecard['generated_at']}　工具：{scorecard['tool']}"
        "（纯读取，不修改任何输入工件）"
    )
    add(
        f"- 评估区间：{overall['evaluation_period']['from']} ~ {overall['evaluation_period']['to']}"
    )
    add("")

    add("## 一、总体表现")
    add("")
    add("| 指标 | 值 |")
    add("| --- | --- |")
    add(f"| 决策日数 | {overall['decision_count']} |")
    add(f"| 预测行数 | {overall['prediction_rows']} |")
    add(f"| 标的数 | {overall['symbols_count']} |")
    add(f"| fold 数 | {overall['fold_count']} |")
    add(f"| 可执行（executable）行 | {overall['executable_rows']} |")
    add(
        f"| no-fill 行 | {overall['no_fill_rows']}"
        f"（{json.dumps(overall['no_fill_by_reason'], ensure_ascii=False)}） |"
    )
    add(
        f"| 成熟 outcome 行（主 horizon T+{overall['primary_horizon']}）"
        f" | {overall['primary_mature_outcome_count']} |"
    )
    mature_counts = "、".join(
        f"{k}: {v}" for k, v in overall["mature_outcome_count_by_horizon"].items()
    )
    add(f"| 分 horizon 成熟 outcome 行 | {mature_counts} |")
    add("")

    add("## 二、分 horizon 收益统计")
    add("")
    add(
        "统计总体 = executable ∧ matured ∧ 数值收益（`not_available` 哨兵剔除并单独计数）；"
        "收益为净收益口径，超额为对工件内基准（见文末）的 excess。"
    )
    add("")
    add(
        "| horizon | samples | 平均收益 | 中位收益 | 胜率 | 最好 | 最差 "
        "| MAE | MFE | 平均超额 | 未成熟剔除 |"
    )
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for name, section in scorecard["horizon_stats"].items():
        immature = section["immature_excluded"]
        immature_txt = "—" if immature is None else str(immature)
        add(
            f"| {name} | {section['samples']} "
            f"| {_fmt_pct(section['average_return'])} "
            f"| {_fmt_pct(section['median_return'])} "
            f"| {_fmt_pct(section['positive_rate'])} "
            f"| {_fmt_pct(section['best_return'])} "
            f"| {_fmt_pct(section['worst_return'])} "
            f"| {_fmt_pct(section.get('MAE'))} | {_fmt_pct(section.get('MFE'))} "
            f"| {_fmt_pct(section.get('average_excess_return'))} "
            f"| {immature_txt} |"
        )
    unavailable = scorecard["capability"]["horizons_unavailable"]
    if unavailable:
        add("")
        add(f"不可用 horizon（如实标注，不推算）：{json.dumps(unavailable, ensure_ascii=False)}")
    add("")

    add("## 三、评分分层（rank_score 十分位）")
    add("")
    add("| score bucket | samples | 平均收益 | 胜率 |")
    add("| --- | --- | --- | --- |")
    for bucket in scorecard["score_buckets"]["buckets"]:
        add(
            f"| {bucket['score_bucket']} | {bucket['samples']} "
            f"| {_fmt_pct(bucket['avg_return'])} | {_fmt_pct(bucket['win_rate'])} |"
        )
    monotonic = scorecard["score_buckets"]["monotonic_nonincreasing"]
    spread = scorecard["score_buckets"]["top_bottom_spread"]
    verdict = (
        "通过（高分桶收益单调不低于低分桶）" if monotonic else "未通过（存在倒挂，见 violations）"
    )
    add("")
    add(f"**单调性**：{verdict}；最高/最低桶平均收益差 = {_fmt_pct(spread)}。未做任何人工调整。")
    add("")

    add("## 四、时间分层（年度）")
    add("")
    add("| 年度 | 决策日 | samples | 平均收益 | 胜率 |")
    add("| --- | --- | --- | --- | --- |")
    for row in scorecard["yearly"]:
        add(
            f"| {row['year']} | {row['decision_days']} | {row['samples']} "
            f"| {_fmt_pct(row['avg_return'])} | {_fmt_pct(row['win_rate'])} |"
        )
    add("")
    regime = scorecard["market_regime"]
    if regime.get("available"):
        add("market_regime 分层（**ex-post 分析字段，不得进入预测**）：")
        add("")
        add("| regime | samples | 平均收益 | 胜率 |")
        add("| --- | --- | --- | --- |")
        for row in regime["rows"]:
            add(
                f"| {row['regime']} | {row['samples']} "
                f"| {_fmt_pct(row['avg_return'])} | {_fmt_pct(row['win_rate'])} |"
            )
    else:
        add(f"market_regime：{regime['note']}")
    add("")

    add("## 五、失败分析（最低收益 Top N）")
    add("")
    add("| symbol | decision_date | score | return | MAE | MFE | entry_date | fold |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in scorecard["failure_analysis"]["worst_return_top"]:
        score_txt = "—" if row["score"] is None else f"{row['score']:.4f}"
        add(
            f"| {row['symbol']} | {row['decision_date']} | {score_txt} "
            f"| {_fmt_pct(row['return'])} | {_fmt_pct(row['MAE'])} | {_fmt_pct(row['MFE'])} "
            f"| {row['entry_date']} | {row['fold_id']} |"
        )
    add("")

    add("## 数据来源与口径")
    add("")
    pred_files = [f["file"] for f in prov["prediction_files"]]
    add(f"- 预测文件：{json.dumps(pred_files, ensure_ascii=False)}")
    add(f"- join 方式：`{prov['join']['mode']}`（键 decision_date+symbol）")
    if "max_abs_diff_5d" in prov["join"]:
        add(f"- 预测 5d 与 outcome 帧 5d 最大差值：{prov['join']['max_abs_diff_5d']}")
    resolution = prov.get("outcome_cache_resolution")
    if resolution:
        if resolution.get("mode") == "manifest":
            add(f"- outcome cache 解析：manifest 指针（`{resolution.get('path')}`）")
        else:
            add(
                f"- outcome cache 解析：**verified_fallback**——manifest 指针失效"
                f"（`{resolution.get('manifest_ref')}`），改用通过逐行一致性验证的"
                f" `{resolution.get('path')}`（非按文件名猜测）"
            )
    add(
        f"- 实验身份：experiment `{run_protocol.get('experiment_id')}` / "
        f"code_commit `{run_protocol.get('code_commit')}` / "
        f"protocol_hash `{str(run_protocol.get('protocol_hash'))[:12]}…`"
    )
    add(
        f"- 执行契约（协议声明）："
        f"`{json.dumps(run_protocol.get('execution_contract'), ensure_ascii=False)}`"
    )
    add(
        f"- outcome 帧实测价格模式：price_mode=`{diag.get('price_mode')}`、"
        f"certified=`{diag.get('price_mode_certified')}`、"
        f"execution_uncertain 占比={_fmt_pct(diag.get('execution_uncertain_share'))}"
        "（如实转录工件记录，本层不裁决）"
    )
    add(f"- 超额收益基准：{json.dumps(prov['benchmark_name_distribution'], ensure_ascii=False)}")
    add(f"- metrics 转录：{json.dumps(prov['metrics_summary_echo'], ensure_ascii=False)[:400]}")
    leak = prov["leakage_audit_echo"]
    add(
        f"- 泄漏审计转录：lookahead/pit/execution/calibration = "
        f"{leak.get('lookahead_violations')}/{leak.get('pit_violations')}/"
        f"{leak.get('execution_violations')}/{leak.get('calibration_violations')}"
    )
    add("")
    add("## 注意事项")
    add("")
    add(
        "1. 本成绩单是**纯读取**研究报告：不修改模型、特征、label、HORIZONS、"
        "M4-H 工件与任何生产路径。"
    )
    add("2. T+20/T+60 等非训练 horizon 只能作为评价指标，绝不进入训练 label。")
    add("3. market_regime（若存在）是 ex-post 分析字段，不得进入预测输入。")
    add("4. no-fill（涨停无法买入/停牌等）行已从收益统计剔除，计入总体表现一节的计数。")
    add("5. 分层单调性结论只做如实报告，不构成参数调整建议。")
    if scorecard["warnings"]:
        add("6. 警告：")
        for warning in scorecard["warnings"]:
            add(f"   - {warning}")
    add("")
    return "\n".join(lines)


def write_outputs(scorecard: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    """原子写出 scorecard.json / scorecard.md（只允许落在 out_dir）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(scorecard, ensure_ascii=False, indent=2, default=str)
    tmp = out_dir / "scorecard.json.tmp"
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, out_dir / "scorecard.json")
    (out_dir / "scorecard.md").write_text(render_markdown(scorecard), encoding="utf-8")
    return {"json": out_dir / "scorecard.json", "markdown": out_dir / "scorecard.md"}


# ---------------------------------------------------------------------------
# 编排层
# ---------------------------------------------------------------------------


def _assert_out_dir_safe(m4h_root: Path, out_dir: Path) -> None:
    root_resolved = m4h_root.resolve()
    out_resolved = out_dir.resolve()
    if out_resolved == root_resolved or root_resolved in out_resolved.parents:
        raise ScorecardInputError(
            f"输出目录 {out_dir} 位于 m4h root {m4h_root} 内部：禁止覆盖 M4-H 证据工件"
        )


def _resolve_outcome_by_consistency(
    m4h_root: Path, manifest_ref: Any, pred_frame: pd.DataFrame
) -> tuple[dict[str, Any], dict[str, Any]]:
    """manifest 指针失效时的降级解析：逐候选通过硬一致性门才算数。

    一个 root 可能残留多个实验的 cache——绝不能按文件名猜。这里对每个
    ``cache/outcomes_*.pkl`` 候选执行与主路径**完全相同**的一致性硬门
    （join 完整 + executable 一致 + 5d 数值/哨兵一致），第一个全过的才被
    接受；全部不过则维持 fail closed。这不是猜测，是可验证的等价性。
    """
    cache_dir = m4h_root / "cache"
    candidates = sorted(cache_dir.glob("outcomes_*.pkl")) if cache_dir.is_dir() else []
    if not candidates:
        raise OutcomeDataUnavailable(
            f"manifest 指向的 outcome cache 缺失（{manifest_ref}），且 {cache_dir} 无任何候选"
        )
    for path in candidates:
        try:
            payload = load_outcome_frame(path)
            _merge_and_validate(pred_frame, payload["frame"])
        except ScorecardError:
            continue
        return payload, {
            "mode": "verified_fallback",
            "path": str(path),
            "manifest_ref": str(manifest_ref),
            "note": "候选 cache 已通过与主路径相同的逐行一致性硬门，非按文件名猜测",
        }
    raise OutcomeDataUnavailable(
        f"manifest 指向的 outcome cache 缺失（{manifest_ref}），"
        f"且 {len(candidates)} 个候选 cache 均未通过一致性验证，fail closed"
    )


def run_scorecard(
    m4h_root: Path,
    out_dir: Path,
    *,
    top_n: int = 20,
    allow_missing_outcomes: bool = False,
) -> tuple[dict[str, Any], dict[str, Path]]:
    """编排：加载 → 校验 → 组装 → 写出。任何失败都在写文件之前抛出。"""
    m4h_root = Path(m4h_root)
    out_dir = Path(out_dir)
    if not m4h_root.is_dir():
        raise ScorecardInputError(f"--m4h-root 不存在: {m4h_root}")
    if top_n <= 0:
        raise ScorecardInputError("--top-n 必须为正整数")
    _assert_out_dir_safe(m4h_root, out_dir)

    predictions = load_predictions(m4h_root)

    run_manifest: dict[str, Any] | None = None
    outcome: dict[str, Any] | None = None
    outcome_resolution: dict[str, Any] | None = None
    try:
        run_manifest = load_run_manifest(m4h_root)
        manifest_protocol = (run_manifest.get("protocol") or {}).get("protocol_id")
        if manifest_protocol not in (None, predictions["protocol_id"]):
            raise ScorecardContractError(
                f"run_manifest.protocol_id={manifest_protocol} 与预测 "
                f"protocol_id={predictions['protocol_id']} 不一致，疑似混入不同 run 的工件"
            )
        cache_path = resolve_outcome_cache_path(m4h_root, run_manifest)
        if cache_path.is_file():
            outcome = load_outcome_frame(cache_path)
            outcome_resolution = {"mode": "manifest", "path": str(cache_path)}
        else:
            # manifest 指针失效（cache 已被清理）：按一致性验证解析，不按文件名猜。
            dataset = run_manifest.get("dataset")
            ref = dataset.get("dataset_cache") if isinstance(dataset, dict) else None
            outcome, outcome_resolution = _resolve_outcome_by_consistency(
                m4h_root, ref, predictions["frame"]
            )
    except OutcomeDataUnavailable:
        if not allow_missing_outcomes:
            raise
        outcome = None

    metrics_path = m4h_root / "metrics" / "metrics_summary.json"
    metrics_summary = _read_json(metrics_path) if metrics_path.is_file() else None
    leakage_path = m4h_root / "audit" / "leakage_audit.json"
    leakage_audit = _read_json(leakage_path) if leakage_path.is_file() else None

    scorecard = build_scorecard(
        predictions,
        outcome,
        run_manifest=run_manifest,
        metrics_summary=metrics_summary,
        leakage_audit=leakage_audit,
        m4h_root=m4h_root,
        top_n=top_n,
        outcome_resolution=outcome_resolution,
    )
    outputs = write_outputs(scorecard, out_dir)
    return scorecard, outputs
