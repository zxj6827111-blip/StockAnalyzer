"""Alpha V2 统一数据契约（蓝图 §4 / §15）。

当前落地 ``SelectionContract``（S04 / 原 P0-07）：把"生产夜扫与历史 night-equivalent
必须同口径"这件事从**散落读取配置**变成**一个显式契约对象**。

蓝图 §4.2 的问题描述：同一套漏斗此前分散读取

```text
universe_quality_target_size / night_quality_target
light_candidate_target / night_light_candidate_target
deep_candidate_target / night_deep_candidate_target
```

由各路径自行解释，于是生产夜扫是 300/100/50，而历史回测走的是 100/100/20——
两者不可直接比较。S04 引入 ``contract_id`` 并把三个目标 + final_cap + allow_zero
一次性绑定，报告里必须原样写出，使"同口径"可被审计而不是靠默契。

其他 profile（offhours / intraday / monster）**不强行统一**：它们各自返回
``legacy_profile`` 契约并如实标注 ``unified=False``——蓝图明确允许独立 contract。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stock_analyzer.config import StockAnalyzerConfig

# 契约 id（稳定标识，报告与测试引用）
NIGHT_ALPHA_V2_CONTRACT_ID = "night_alpha_v2_v1"
LEGACY_PROFILE_CONTRACT_ID = "legacy_profile_v1"

# 映射到夜扫契约的 profile 名：
# - "night_scan"：生产夜间扫描（week5_automation_service）
# - "historical_night_equivalent"：历史回测的 night-equivalent 重放（week5_historical_runner）
NIGHT_CONTRACT_PROFILES = frozenset({"night_scan", "historical_night_equivalent"})


@dataclass(frozen=True, slots=True)
class SelectionContract:
    """一次选股运行的漏斗契约（目标数固定绑定，报告必须原样写出）。"""

    contract_id: str
    profile: str
    quality_target: int
    light_target: int
    deep_target: int
    final_cap: int
    allow_zero_signal: bool
    unified: bool
    source: dict[str, str] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "selection_contract_id": self.contract_id,
            "profile": self.profile,
            "quality_target": self.quality_target,
            "light_target": self.light_target,
            "deep_target": self.deep_target,
            "final_cap": self.final_cap,
            "allow_zero_signal": self.allow_zero_signal,
            "unified_with_night_scan": self.unified,
            "source": dict(self.source),
        }

    def funnel_targets(self) -> dict[str, int]:
        """引擎消费的三个目标（quality / light / deep）。"""
        return {
            "quality_target": self.quality_target,
            "light_target": self.light_target,
            "deep_target": self.deep_target,
        }


def resolve_selection_contract(
    config: StockAnalyzerConfig, *, profile: str
) -> SelectionContract:
    """按 profile 解析当前生效的漏斗契约。

    夜扫及其 night-equivalent 重放（生产/历史必须同口径）走
    :data:`NIGHT_ALPHA_V2_CONTRACT_ID`，读取 ``week5.night_*`` 三个目标；
    其余 profile 保持各自既有目标（``unified=False``，M1 不强行统一）。
    """
    normalized = str(profile or "").strip()
    week5 = config.week5
    if normalized in NIGHT_CONTRACT_PROFILES:
        return SelectionContract(
            contract_id=NIGHT_ALPHA_V2_CONTRACT_ID,
            profile=normalized,
            quality_target=max(1, int(week5.night_quality_target)),
            light_target=max(1, int(week5.night_light_candidate_target)),
            deep_target=max(1, int(week5.night_deep_candidate_target)),
            final_cap=max(0, int(week5.final_signal_cap)),
            allow_zero_signal=bool(week5.allow_zero_signal),
            unified=True,
            source={
                "quality_target": "week5.night_quality_target",
                "light_target": "week5.night_light_candidate_target",
                "deep_target": "week5.night_deep_candidate_target",
                "final_cap": "week5.final_signal_cap",
                "allow_zero_signal": "week5.allow_zero_signal",
            },
        )
    return SelectionContract(
        contract_id=LEGACY_PROFILE_CONTRACT_ID,
        profile=normalized or "default",
        quality_target=max(1, int(week5.universe_quality_target_size)),
        light_target=max(1, int(week5.light_candidate_target)),
        deep_target=max(1, int(week5.deep_candidate_target)),
        final_cap=max(0, int(week5.final_signal_cap)),
        allow_zero_signal=bool(week5.allow_zero_signal),
        unified=False,
        source={
            "quality_target": "week5.universe_quality_target_size",
            "light_target": "week5.light_candidate_target",
            "deep_target": "week5.deep_candidate_target",
            "final_cap": "week5.final_signal_cap",
            "allow_zero_signal": "week5.allow_zero_signal",
        },
    )


__all__ = [
    "LEGACY_PROFILE_CONTRACT_ID",
    "NIGHT_ALPHA_V2_CONTRACT_ID",
    "NIGHT_CONTRACT_PROFILES",
    "SelectionContract",
    "resolve_selection_contract",
]
