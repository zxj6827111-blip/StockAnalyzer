"""Alpha V2 特征健康诊断（DF-M2-003 / M3 §16）。

背景：M2 的 S14 审计发现 101/208 个特征列在本窗口近乎常数或近全空，
与 Phase 2 归因的「98 特征全 NaN」同源。本模块把这些列逐列拆开诊断，
回答"它为什么几乎没变"：

- 统计层：coverage / missing_ratio / unique_count / std / zero_ratio /
  mode_ratio / mode_value；
- 来源层：登记的 feature group（source / missing_policy / asof_safe），
  以及对上游表（如 ``market_duckdb.daily_bars``）的实际列探测；
- 分类层（每列恰好一类，判定顺序自上而下）：

```text
UPSTREAM_NOT_POPULATED  上游来源从未填充（源列在仓库中不存在/整空）
FILL_ZERO_ARTIFACT      众数是 0 且占比极高，且组内 missing_policy = fill_zero_after_shift
DATA_MISSINGNESS        近全空，但组口径是 keep_nan（数据真的缺席，不是被填 0）
REAL_CONSTANT           有数据但统计上接近常数（低方差/高众数占比，非零填充）
UNKNOWN                 都不符合——需要人工看
```

**纪律**：本模块只做诊断，不删除/不整改任何特征。把某列从 Base V2 移除
属于「改冻结特征集」，必须关闭当前 epoch 后开新 epoch（M3 §16）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from stock_analyzer.alpha_v2.research.feature_audit import (
    CONSTANT_RATIO_THRESHOLD,
    FEATURE_GROUPS,
    NULL_RATIO_THRESHOLD,
    FeatureGroupSpec,
    classify_feature_columns,
)
from stock_analyzer.alpha_v2.research.metrics import NOT_AVAILABLE

DIAGNOSIS_SCHEMA = "alpha_v2_feature_diagnosis.v1"

CLASS_REAL_CONSTANT = "REAL_CONSTANT"
CLASS_DATA_MISSINGNESS = "DATA_MISSINGNESS"
CLASS_UPSTREAM_NOT_POPULATED = "UPSTREAM_NOT_POPULATED"
CLASS_FILL_ZERO_ARTIFACT = "FILL_ZERO_ARTIFACT"
CLASS_UNKNOWN = "UNKNOWN"

SOURCE_MARKET_DAILY_BARS = "market_duckdb.daily_bars"


@dataclass(frozen=True, slots=True)
class FeatureDiagnosis:
    column: str
    group_id: str
    classification: str
    coverage: float
    missing_ratio: float
    unique_count: int
    std: float | None
    zero_ratio: float
    mode_value: object
    mode_ratio: float
    asof_safe: str
    missing_policy: str
    upstream_source: str
    upstream_evidence: str

    def to_payload(self) -> dict[str, object]:
        return {
            "column": self.column,
            "group_id": self.group_id,
            "classification": self.classification,
            "coverage": self.coverage,
            "missing_ratio": self.missing_ratio,
            "unique_count": self.unique_count,
            "std": self.std,
            "zero_ratio": self.zero_ratio,
            "mode_value": self.mode_value,
            "mode_ratio": self.mode_ratio,
            "asof_safe": self.asof_safe,
            "missing_policy": self.missing_policy,
            "upstream_source": self.upstream_source,
            "upstream_evidence": self.upstream_evidence,
        }


@dataclass
class FeatureDiagnosisReport:
    rows: list[FeatureDiagnosis] = field(default_factory=list)

    def classification_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.rows:
            counts[row.classification] = counts.get(row.classification, 0) + 1
        return dict(sorted(counts.items()))

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": DIAGNOSIS_SCHEMA,
            "columns_diagnosed": len(self.rows),
            "classification_counts": self.classification_counts(),
            "rows": [row.to_payload() for row in self.rows],
        }


def diagnose_features(
    frame: pd.DataFrame,
    *,
    columns: Sequence[str] | None = None,
    groups: Sequence[FeatureGroupSpec] = FEATURE_GROUPS,
    upstream_probe: Mapping[str, str] | None = None,
    min_rows: int = 10,
) -> FeatureDiagnosisReport:
    """对一帧特征做逐列诊断。

    ``upstream_probe``：``{group_id: "ok" | "source_columns_absent: col1,col2" ...}``
    ——由调用方（CLI）先探测上游来源再传入；模块本身不连库（保持单测可控）。
    """
    assignment, _unregistered = classify_feature_columns(
        [str(column) for column in frame.columns], groups=list(groups)
    )
    probe = dict(upstream_probe or {})
    group_by_id = {spec.group_id: spec for spec in groups}

    targets = list(columns) if columns is not None else list(frame.columns)
    rows: list[FeatureDiagnosis] = []
    total = len(frame)
    for column in targets:
        if column not in frame.columns:
            continue
        # 身份/索引列不是特征（与 S14 IDENTITY_COLUMNS 同一约定）
        if column in {"decision_date", "symbol"} or str(column).startswith("__"):
            continue
        series = frame[column]
        group_id = assignment.get(column, "unregistered")
        spec = group_by_id.get(group_id)
        rows.append(_diagnose_column(column, series, total, spec, probe.get(group_id, "")))
    return FeatureDiagnosisReport(rows=rows)


def _diagnose_column(
    column: str,
    series: pd.Series,
    total_rows: int,
    spec: FeatureGroupSpec | None,
    upstream_evidence: str,
) -> FeatureDiagnosis:
    total = max(1, int(total_rows))
    missing_ratio = float(series.isna().mean()) if total else 1.0
    non_null = series.dropna()
    coverage = 1.0 - missing_ratio
    unique_count = int(non_null.nunique()) if not non_null.empty else 0
    numeric = pd.to_numeric(non_null, errors="coerce")
    numeric = numeric[np.isfinite(numeric)]
    std = float(numeric.std(ddof=1)) if len(numeric) > 1 else None
    if std is not None and (math.isnan(std) or not math.isfinite(std)):
        std = None
    zero_ratio = (
        float((numeric == 0).mean()) if not numeric.empty else 0.0
    )
    mode_value: object = NOT_AVAILABLE
    mode_ratio = 0.0
    if not non_null.empty:
        counts = non_null.value_counts()
        mode_value = counts.index[0]
        if isinstance(mode_value, (np.integer,)):
            mode_value = int(mode_value)
        elif isinstance(mode_value, (np.floating,)):
            mode_value = float(mode_value)
        mode_ratio = float(counts.iloc[0] / total)

    classification, resolved_evidence = _classify(
        spec=spec,
        missing_ratio=missing_ratio,
        unique_count=unique_count,
        std=std,
        zero_ratio=zero_ratio,
        mode_value=mode_value,
        mode_ratio=mode_ratio,
        upstream_evidence=upstream_evidence,
        column=column,
    )
    return FeatureDiagnosis(
        column=column,
        group_id=str(spec.group_id) if spec else "unregistered",
        classification=classification,
        coverage=round(coverage, 6),
        missing_ratio=round(missing_ratio, 6),
        unique_count=unique_count,
        std=std,
        zero_ratio=round(zero_ratio, 6),
        mode_value=mode_value,
        mode_ratio=round(mode_ratio, 6),
        asof_safe=str(spec.asof_safe) if spec else NOT_AVAILABLE,
        missing_policy=str(spec.missing_policy) if spec else NOT_AVAILABLE,
        upstream_source=str(spec.source) if spec else NOT_AVAILABLE,
        upstream_evidence=resolved_evidence,
    )


def _classify(
    *,
    spec: FeatureGroupSpec | None,
    missing_ratio: float,
    unique_count: int,
    std: float | None,
    zero_ratio: float,
    mode_value: object,
    mode_ratio: float,
    upstream_evidence: str,
    column: str,
) -> tuple[str, str]:
    # 1) 上游来源根本没数据（源列缺失或全空）——根因在上游，不在特征
    if upstream_evidence.startswith(("source_columns_absent", "source_columns_all_null")):
        return CLASS_UPSTREAM_NOT_POPULATED, upstream_evidence
    # 2) 众数为 0 且占比极高：要么组登记了 fill_zero，要么工程师末端 fillna(0)
    #    （engineer.py 对全部特征最后统一 fillna(0.0)，常数 0 可能不是真实值）
    if mode_ratio >= CONSTANT_RATIO_THRESHOLD and _is_zeroish(mode_value) and zero_ratio >= 0.9:
        policy = spec.missing_policy if spec else "unknown"
        return CLASS_FILL_ZERO_ARTIFACT, (
            f"mode=0 占比 {mode_ratio:.3f}（zero_ratio={zero_ratio:.3f}），组策略={policy}"
        )
    # 3) 数据真的缺席（近全空且非 0 填充形态）
    if missing_ratio >= NULL_RATIO_THRESHOLD:
        return CLASS_DATA_MISSINGNESS, upstream_evidence or f"missing_ratio={missing_ratio:.3f}"
    # 4) 真是常数（有数据但低方差：唯一值极少或众数占比近 1）
    if unique_count <= 1 or (mode_ratio >= CONSTANT_RATIO_THRESHOLD):
        return CLASS_REAL_CONSTANT, (
            upstream_evidence
            or f"unique_count={unique_count}, mode_ratio={mode_ratio:.3f}, std={std}"
        )
    # 5) 低方差但不构成常数 / 混合情形
    if std is not None and std == 0:
        return CLASS_REAL_CONSTANT, upstream_evidence or "std == 0"
    return CLASS_UNKNOWN, upstream_evidence or "no_rule_matched"


def _is_zeroish(value: object) -> bool:
    if isinstance(value, (int, float)):
        return float(value) == 0.0
    return str(value) in {"0", "0.0"}


# 特征组 → FeatureEngineer 直接读取的上游源列（engineer.py 的真实接线，不是猜测）。
# 这是 "UPSTREAM_NOT_POPULATED" 判定的证据来源。
_GROUP_SOURCE_COLUMNS: dict[str, list[str]] = {
    "price_volume_technical": ["open", "high", "low", "close", "volume", "turnover"],
    "financial_pit": ["roe", "debt_ratio"],
    "shareholder_count": ["holder_count"],
    "block_trade": [
        "block_trade_net",
        "block_trade_amount",
        "block_trade_volume",
        "block_trade_premium_discount",
    ],
    "margin_financing": ["margin_financing_balance"],
    "northbound": ["northbound_net"],
    "dragon_tiger_inst": ["dragon_tiger_flag", "inst_net_amount"],
    "moneyflow": ["moneyflow_net_amount"],
    "hk_hold": ["hk_hold_ratio", "hk_hold_change"],
}


def probe_market_duckdb_sources(
    market_db: str | Path, *, groups: Sequence[FeatureGroupSpec] = FEATURE_GROUPS
) -> dict[str, str]:
    """探测各组上游源列在 ``daily_bars`` 中"存在 + 有非空值"两条证据。

    返回 ``{group_id: str}``；取值：

    - ``ok`` —— 源列存在且有非空值；
    - ``source_columns_absent: a,b`` —— 源列在表中都不存在；
    - ``source_columns_all_null: a,b`` —— 列存在但全空（上游有字段没填充）;
    - ``partial: present=[...] absent=[...]`` —— 部分源列缺失；
    - ``skipped:<source>`` —— 组的登记来源不是 daily_bars（不猜）。
    """
    import duckdb

    path = str(market_db)
    result: dict[str, str] = {}
    try:
        con = duckdb.connect(path, read_only=True)
    except Exception as exc:  # noqa: BLE001 - 只读探测失败如实标注
        return {spec.group_id: f"probe_failed:{type(exc).__name__}:{exc}" for spec in groups}
    try:
        rows = con.execute("DESCRIBE daily_bars").fetchall()
        available = {str(row[0]) for row in rows}
        for spec in groups:
            if not str(spec.source).startswith(SOURCE_MARKET_DAILY_BARS):
                result[spec.group_id] = f"skipped:{spec.source}"
                continue
            candidates = _GROUP_SOURCE_COLUMNS.get(spec.group_id, [])
            if not candidates:
                result[spec.group_id] = "no_named_source_columns"
                continue
            present = [column for column in candidates if column in available]
            absent = [column for column in candidates if column not in available]
            if not present:
                result[spec.group_id] = f"source_columns_absent:{','.join(candidates)}"
                continue
            # 进一步查"列在不在且有没有真值"（列存在但全空也算上游未填充）
            null_share: dict[str, float] = {}
            for column in present:
                row = con.execute(
                    f"SELECT COUNT(*) - COUNT({column}) AS nulls FROM daily_bars"  # noqa: S608
                ).fetchone()
                total = con.execute("SELECT COUNT(*) FROM daily_bars").fetchone()
                if row and total and int(total[0]) > 0:
                    null_share[column] = float(int(row[0]) / int(total[0]))
            all_null = [column for column, share in null_share.items() if share >= 0.9999]
            if all_null and len(all_null) == len(present):
                result[spec.group_id] = f"source_columns_all_null:{','.join(all_null)}"
            elif absent:
                result[spec.group_id] = (
                    f"partial:present={'|'.join(present)} absent={'|'.join(absent)}"
                )
            else:
                result[spec.group_id] = "ok"
    finally:
        con.close()
    return result


def build_diagnosis_markdown(report: FeatureDiagnosisReport) -> str:
    lines: list[str] = ["# DF-M2-003 特征诊断", ""]
    counts = report.classification_counts()
    lines.append(f"- 列数: {len(report.rows)}")
    for name, count in counts.items():
        lines.append(f"- {name}: {count}")
    lines.append("")
    lines.append("| column | group | class | cov | miss | uniq | std | zero% | mode | mode% |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for row in report.rows:
        lines.append(
            f"| {row.column} | {row.group_id} | {row.classification} | {row.coverage:.3f} | "
            f"{row.missing_ratio:.3f} | {row.unique_count} | "
            f"{row.std if row.std is not None else 'nan'} | {row.zero_ratio:.3f} | "
            f"{row.mode_value} | {row.mode_ratio:.3f} |"
        )
    return "\n".join(lines)


__all__ = [
    "CLASS_DATA_MISSINGNESS",
    "CLASS_FILL_ZERO_ARTIFACT",
    "CLASS_REAL_CONSTANT",
    "CLASS_UNKNOWN",
    "CLASS_UPSTREAM_NOT_POPULATED",
    "DIAGNOSIS_SCHEMA",
    "FeatureDiagnosis",
    "FeatureDiagnosisReport",
    "build_diagnosis_markdown",
    "diagnose_features",
    "probe_market_duckdb_sources",
]
