"""Data Health 与 Market Breadth 分层门（S08 / 原 P0-08）。

**问题**（蓝图 §2.14）：历史覆盖不足时曾用 ``breadth_score_unavailable`` fail-closed，
而生产在 ``market_breadth.json`` 缺失时 fail-open——同一个系统对"数据是否可信"给出
两个相反方向。根因是把**数据健康**（能不能信这批数据）和**市场状态**（今天市场适合
不适合开仓）混成了一道门。

目标结构（阶段施工提示词 S08）：

```text
Data Health
  broken   -> 禁止新买（V2 语义：数据不可信时不允许开仓）
  degraded -> 只记录 / shadow（不阻断，但必须可见）
  healthy  -> 才进入 Market Breadth
Market Breadth
  weak             -> 风险门（禁止新买）
  normal / strong  -> 正常
```

三条硬约束：

1. **缺失 ≠ 健康**：任何一项数据缺失/无法验证都不得记 ``ok``（Codex 复审要求）；
2. **coverage 坏但 breadth score 高不得放行**：数据健康先判，坏就是坏，
   高分不能"覆盖"它（蓝图 §P0-08 严禁项）；
3. **灰度**：默认只产报告不影响决策（``enforce=False``）；转换为 fail-closed
   需要"连续 5 个生产日观察 + 历史回放 ≥60 日 + board coverage 检查"之后人工开启。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime

CHECK_OK = "ok"
CHECK_DEGRADED = "degraded"
CHECK_BROKEN = "broken"

HEALTH_HEALTHY = "healthy"
HEALTH_DEGRADED = "degraded"
HEALTH_BROKEN = "broken"

# 检查项名称（稳定契约）
CHECK_TRADE_DATE_FRESHNESS = "trade_date_freshness"
CHECK_EXPECTED_ACTIVE_COVERAGE = "expected_active_coverage"
CHECK_BOARD_COVERAGE = "board_coverage"
CHECK_FEATURE_SNAPSHOT_COVERAGE = "feature_snapshot_coverage"
CHECK_MODEL_IDENTITY_HEALTH = "model_identity_health"
CHECK_PRICE_SERIES_AVAILABILITY = "price_series_availability"
CHECK_BREADTH_ARTIFACT = "breadth_artifact"

CHECK_NAMES = (
    CHECK_TRADE_DATE_FRESHNESS,
    CHECK_EXPECTED_ACTIVE_COVERAGE,
    CHECK_BOARD_COVERAGE,
    CHECK_FEATURE_SNAPSHOT_COVERAGE,
    CHECK_MODEL_IDENTITY_HEALTH,
    CHECK_PRICE_SERIES_AVAILABILITY,
    CHECK_BREADTH_ARTIFACT,
)

DEFAULT_MIN_EXPECTED_ACTIVE_COVERAGE = 0.95
DEFAULT_MAX_FRESHNESS_DAYS = 3


@dataclass(frozen=True, slots=True)
class DataHealthCheck:
    name: str
    status: str
    detail: str
    facts: dict[str, object] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "facts": dict(self.facts),
        }


@dataclass(slots=True)
class DataHealthReport:
    as_of: date
    status: str
    checks: list[DataHealthCheck] = field(default_factory=list)
    coverage_ratio: float = 0.0
    min_expected_active_coverage: float = DEFAULT_MIN_EXPECTED_ACTIVE_COVERAGE
    missing_artifacts: list[str] = field(default_factory=list)

    def check(self, name: str) -> DataHealthCheck | None:
        for item in self.checks:
            if item.name == name:
                return item
        return None

    @property
    def broken_checks(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.checks if item.status == CHECK_BROKEN)

    @property
    def degraded_checks(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.checks if item.status == CHECK_DEGRADED)

    def to_payload(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "status": self.status,
            "coverage_ratio": self.coverage_ratio,
            "coverage_denominator": "expected_active",
            "min_expected_active_coverage": self.min_expected_active_coverage,
            "broken_checks": list(self.broken_checks),
            "degraded_checks": list(self.degraded_checks),
            "missing_artifacts": list(self.missing_artifacts),
            "checks": [item.to_payload() for item in self.checks],
            "policy": "missing_or_unverifiable_is_never_healthy",
        }


def evaluate_data_health(
    *,
    as_of: date,
    latest_trade_date: str | date | None = None,
    universe_snapshot: Mapping[str, object] | None = None,
    valid_symbol_count: int | None = None,
    board_coverage: Mapping[str, object] | None = None,
    feature_snapshot: Mapping[str, object] | None = None,
    model_identity: Mapping[str, object] | None = None,
    price_contract: Mapping[str, object] | None = None,
    breadth_artifact_present: bool = False,
    min_expected_active_coverage: float = DEFAULT_MIN_EXPECTED_ACTIVE_COVERAGE,
    max_freshness_days: int = DEFAULT_MAX_FRESHNESS_DAYS,
) -> DataHealthReport:
    """逐项评估数据健康；**任何缺失/无法验证都不得记 ok**。"""
    checks: list[DataHealthCheck] = []
    missing: list[str] = []

    # 1) 交易日新鲜度
    trade_date = _to_date(latest_trade_date)
    if trade_date is None:
        missing.append("latest_trade_date")
        checks.append(
            DataHealthCheck(
                CHECK_TRADE_DATE_FRESHNESS,
                CHECK_DEGRADED,
                "拿不到最新交易日（无法证明数据新鲜）",
            )
        )
    else:
        age_days = (as_of - trade_date).days
        status = CHECK_OK if age_days <= max_freshness_days else CHECK_BROKEN
        if status == CHECK_BROKEN:
            checks.append(
                DataHealthCheck(
                    CHECK_TRADE_DATE_FRESHNESS,
                    CHECK_BROKEN,
                    f"最新交易日 {trade_date.isoformat()} 距 as_of {age_days} 天，"
                    f"超过 {max_freshness_days} 天",
                    {"latest_trade_date": trade_date.isoformat(), "age_days": age_days},
                )
            )
        else:
            checks.append(
                DataHealthCheck(
                    CHECK_TRADE_DATE_FRESHNESS,
                    CHECK_OK,
                    f"最新交易日 {trade_date.isoformat()}（{age_days} 天前）",
                    {"latest_trade_date": trade_date.isoformat(), "age_days": age_days},
                )
            )

    # 2) expected-active 覆盖率（S03 的 PIT 分母）
    coverage_ratio = 0.0
    expected_active = 0
    if universe_snapshot is None:
        missing.append("universe_snapshot")
        checks.append(
            DataHealthCheck(
                CHECK_EXPECTED_ACTIVE_COVERAGE,
                CHECK_DEGRADED,
                "缺少 PIT 股票池快照，覆盖率无法计算（不得当健康）",
            )
        )
    else:
        expected_active = _as_int(universe_snapshot.get("expected_active_count"), default=0)
        if expected_active <= 0:
            checks.append(
                DataHealthCheck(
                    CHECK_EXPECTED_ACTIVE_COVERAGE,
                    CHECK_BROKEN,
                    "expected_active=0：分母不可信，覆盖率无法证明",
                    {"expected_active_count": 0},
                )
            )
        elif valid_symbol_count is None:
            # N2（半批审）：分母有了但**分子拿不到**时不得当成 1.0 判 ok——
            # 模块自立的"缺失/无法验证不得记 ok"不能被默认值绕过。
            missing.append("valid_symbol_count")
            checks.append(
                DataHealthCheck(
                    CHECK_EXPECTED_ACTIVE_COVERAGE,
                    CHECK_DEGRADED,
                    (
                        f"expected_active={expected_active} 已知，但 valid_symbol_count 缺失，"
                        "覆盖率无法验证（不得当健康）"
                    ),
                    {"expected_active_count": expected_active, "coverage_ratio": None},
                )
            )
        else:
            valid = int(valid_symbol_count)
            coverage_ratio = max(0.0, min(1.0, valid / expected_active))
            status = (
                CHECK_OK
                if coverage_ratio >= float(min_expected_active_coverage)
                else CHECK_BROKEN
            )
            checks.append(
                DataHealthCheck(
                    CHECK_EXPECTED_ACTIVE_COVERAGE,
                    status,
                    (
                        f"valid_expected_active/expected_active = {valid}/{expected_active} "
                        f"= {coverage_ratio:.4f}（阈值 {min_expected_active_coverage}）"
                    ),
                    {
                        "valid_expected_active": valid,
                        "expected_active_count": expected_active,
                        "coverage_ratio": coverage_ratio,
                    },
                )
            )

    # 3) 板块级覆盖
    if not board_coverage:
        missing.append("board_coverage")
        checks.append(
            DataHealthCheck(
                CHECK_BOARD_COVERAGE,
                CHECK_DEGRADED,
                "缺少板块级覆盖信息（无法证明各板块数据完整）",
            )
        )
    else:
        worst = min(
            (float(str(value)) for value in board_coverage.values() if _is_number(value)),
            default=0.0,
        )
        status = CHECK_OK if worst >= float(min_expected_active_coverage) else CHECK_DEGRADED
        checks.append(
            DataHealthCheck(
                CHECK_BOARD_COVERAGE,
                status,
                f"板块最差覆盖率 {worst:.4f}",
                {"worst_board_coverage": worst},
            )
        )

    # 4) Feature snapshot 覆盖
    if not feature_snapshot:
        missing.append("feature_snapshot")
        checks.append(
            DataHealthCheck(
                CHECK_FEATURE_SNAPSHOT_COVERAGE,
                CHECK_DEGRADED,
                "缺少 Feature Snapshot 清单（无法证明特征覆盖）",
            )
        )
    else:
        current = bool(feature_snapshot.get("current", False))
        ratio = _as_float(feature_snapshot.get("coverage_ratio"), default=0.0)
        if not current:
            checks.append(
                DataHealthCheck(
                    CHECK_FEATURE_SNAPSHOT_COVERAGE,
                    CHECK_DEGRADED,
                    "Feature Snapshot 非当前（stale）",
                    {"coverage_ratio": ratio, "current": False},
                )
            )
        else:
            status = CHECK_OK if ratio >= float(min_expected_active_coverage) else CHECK_DEGRADED
            checks.append(
                DataHealthCheck(
                    CHECK_FEATURE_SNAPSHOT_COVERAGE,
                    status,
                    f"Feature Snapshot 覆盖率 {ratio:.4f}",
                    {"coverage_ratio": ratio, "current": True},
                )
            )

    # 5) 模型身份健康（S01）
    if not model_identity:
        missing.append("model_identity")
        checks.append(
            DataHealthCheck(
                CHECK_MODEL_IDENTITY_HEALTH,
                CHECK_DEGRADED,
                "缺少模型身份报告（无法证明在服身份）",
            )
        )
    else:
        identity_status = str(model_identity.get("status", "") or "")
        if bool(model_identity.get("research_fail_closed", False)) or identity_status in {
            "mismatch",
            "loaded_hash_missing",
        }:
            checks.append(
                DataHealthCheck(
                    CHECK_MODEL_IDENTITY_HEALTH,
                    CHECK_BROKEN,
                    f"模型身份不可信：status={identity_status}",
                    {"identity_status": identity_status},
                )
            )
        elif not bool(model_identity.get("identity_verified", False)):
            checks.append(
                DataHealthCheck(
                    CHECK_MODEL_IDENTITY_HEALTH,
                    CHECK_DEGRADED,
                    f"模型身份未验证：status={identity_status}（治理缺口，不阻断）",
                    {"identity_status": identity_status},
                )
            )
        else:
            checks.append(
                DataHealthCheck(
                    CHECK_MODEL_IDENTITY_HEALTH,
                    CHECK_OK,
                    f"模型身份已验证：status={identity_status}",
                    {"identity_status": identity_status},
                )
            )

    # 6) 价格序列可用性（S07）
    if not price_contract:
        missing.append("price_contract")
        checks.append(
            DataHealthCheck(
                CHECK_PRICE_SERIES_AVAILABILITY,
                CHECK_DEGRADED,
                "缺少价格口径契约（无法证明成交价是 raw）",
            )
        )
    elif bool(price_contract.get("execution_uncertain", False)):
        checks.append(
            DataHealthCheck(
                CHECK_PRICE_SERIES_AVAILABILITY,
                CHECK_DEGRADED,
                str(
                    price_contract.get("execution_uncertain_reason", "")
                    or "执行价格口径不确定"
                ),
                {"execution_price_mode": price_contract.get("execution_price_mode", "")},
            )
        )
    else:
        checks.append(
            DataHealthCheck(
                CHECK_PRICE_SERIES_AVAILABILITY,
                CHECK_OK,
                "成交价口径 = raw",
                {"execution_price_mode": price_contract.get("execution_price_mode", "")},
            )
        )

    # 7) Breadth artifact 是否存在（缺失 ≠ 健康）
    if breadth_artifact_present:
        checks.append(
            DataHealthCheck(CHECK_BREADTH_ARTIFACT, CHECK_OK, "market_breadth 产物存在")
        )
    else:
        missing.append("market_breadth_artifact")
        checks.append(
            DataHealthCheck(
                CHECK_BREADTH_ARTIFACT,
                CHECK_DEGRADED,
                "market_breadth 产物缺失（缺失不得当健康）",
            )
        )

    statuses = {item.status for item in checks}
    overall = (
        HEALTH_BROKEN
        if CHECK_BROKEN in statuses
        else (HEALTH_DEGRADED if CHECK_DEGRADED in statuses else HEALTH_HEALTHY)
    )
    return DataHealthReport(
        as_of=as_of,
        status=overall,
        checks=checks,
        coverage_ratio=coverage_ratio,
        min_expected_active_coverage=float(min_expected_active_coverage),
        missing_artifacts=sorted(set(missing)),
    )


def combined_gate_decision(
    *,
    report: DataHealthReport,
    breadth_policy: Mapping[str, object] | None,
    enforce: bool = False,
) -> dict[str, object]:
    """数据健康 → 市场广度的分层门（默认只观测，不改变决策）。

    ``enforce=False``（灰度默认）：算出"如果开启会怎样"，但 ``block_new_buy`` 恒为 False，
    并把建议写进 ``shadow_recommendation``——满足"先产报告、连续观察后再 fail-closed"。
    """
    breadth_block = bool((breadth_policy or {}).get("block_new_buy", False))
    breadth_reason = str((breadth_policy or {}).get("reason", "") or "")
    if report.status == HEALTH_BROKEN:
        recommendation = {
            "block_new_buy": True,
            "reason": "data_health_broken:"
            + (report.broken_checks[0] if report.broken_checks else ""),
        }
    elif report.status == HEALTH_DEGRADED:
        recommendation = {
            "block_new_buy": False,
            "reason": "data_health_degraded:observe_only",
        }
    else:
        recommendation = {
            "block_new_buy": breadth_block,
            "reason": breadth_reason or "breadth_ok",
        }
    decision: dict[str, object] = {
        "data_health_status": report.status,
        "enforced": bool(enforce),
        "breadth_block_new_buy": breadth_block,
        "breadth_reason": breadth_reason,
        "shadow_recommendation": dict(recommendation),
        "coverage_ratio": report.coverage_ratio,
    }
    if enforce:
        decision["block_new_buy"] = bool(recommendation["block_new_buy"])
        decision["reason"] = str(recommendation["reason"])
    else:
        # 灰度期：决策不受影响，但仍记录"数据健康坏时的建议"（供观察期评估）。
        decision["block_new_buy"] = False
        decision["reason"] = "grey_period_observation_only"
    return decision


def _to_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_int(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: object, *, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


__all__ = [
    "CHECK_BOARD_COVERAGE",
    "CHECK_BREADTH_ARTIFACT",
    "CHECK_DEGRADED",
    "CHECK_EXPECTED_ACTIVE_COVERAGE",
    "CHECK_FEATURE_SNAPSHOT_COVERAGE",
    "CHECK_MODEL_IDENTITY_HEALTH",
    "CHECK_NAMES",
    "CHECK_OK",
    "CHECK_PRICE_SERIES_AVAILABILITY",
    "CHECK_BROKEN",
    "CHECK_TRADE_DATE_FRESHNESS",
    "DEFAULT_MAX_FRESHNESS_DAYS",
    "DEFAULT_MIN_EXPECTED_ACTIVE_COVERAGE",
    "HEALTH_BROKEN",
    "HEALTH_DEGRADED",
    "HEALTH_HEALTHY",
    "DataHealthCheck",
    "DataHealthReport",
    "combined_gate_decision",
    "evaluate_data_health",
]
