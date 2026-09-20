"""S04 SelectionContract：生产夜扫与历史 night-equivalent 必须同口径（300/100/50）。

对应蓝图 §5 P0-07 / 阶段施工提示词 S04 的 Done When：

```text
同一 contract 下：production night shadow 与 historical night-equivalent
都明确报告 300 -> 100 -> 50，且 contract ID 相同
```

同时钉住"不强行统一其他 profile"：offhours / default 等仍返回各自目标，
且明确标注 ``unified_with_night_scan=False``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stock_analyzer.config import load_config
from stock_analyzer.contracts.alpha_v2 import (
    LEGACY_PROFILE_CONTRACT_ID,
    NIGHT_ALPHA_V2_CONTRACT_ID,
    NIGHT_CONTRACT_PROFILES,
    resolve_selection_contract,
)

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def config():
    return load_config(_ROOT / "config" / "default.yaml")


# ---------------------------------------------------------------------------
# 契约解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", sorted(NIGHT_CONTRACT_PROFILES))
def test_night_profiles_share_one_contract(config, profile: str) -> None:
    contract = resolve_selection_contract(config, profile=profile)
    assert contract.contract_id == NIGHT_ALPHA_V2_CONTRACT_ID
    assert (contract.quality_target, contract.light_target, contract.deep_target) == (300, 100, 50)
    assert contract.final_cap == 5
    assert contract.allow_zero_signal is True
    assert contract.unified is True


def test_production_night_and_historical_report_same_contract_id(config) -> None:
    """Done When 的核心：两条路径拿到的 contract 必须逐字段相同。"""
    production = resolve_selection_contract(config, profile="night_scan")
    historical = resolve_selection_contract(config, profile="historical_night_equivalent")
    assert production.contract_id == historical.contract_id
    assert production.funnel_targets() == historical.funnel_targets()
    assert production.final_cap == historical.final_cap
    assert production.allow_zero_signal == historical.allow_zero_signal


def test_other_profiles_keep_their_own_targets_and_are_marked_not_unified(config) -> None:
    """蓝图 §4.2：不强行统一所有 profile（intraday/monster/offhours 可有独立契约）。"""
    for profile in ("default", "offhours_friday_full_deep", "intraday_scheduler"):
        contract = resolve_selection_contract(config, profile=profile)
        assert contract.contract_id == LEGACY_PROFILE_CONTRACT_ID
        assert contract.unified is False
        assert contract.deep_target == int(config.week5.deep_candidate_target)


def test_contract_payload_reports_everything_required_by_gate(config) -> None:
    payload = resolve_selection_contract(config, profile="night_scan").to_payload()
    for key in (
        "selection_contract_id",
        "quality_target",
        "light_target",
        "deep_target",
        "final_cap",
        "allow_zero_signal",
    ):
        assert key in payload
    assert payload["quality_target"] == 300
    assert payload["light_target"] == 100
    assert payload["deep_target"] == 50
    # 契约必须自述来源（哪个配置键），否则"为什么是 300"事后不可追
    assert payload["source"]["quality_target"] == "week5.night_quality_target"


def test_contract_targets_follow_config_values(config) -> None:
    """契约读取的是配置真值：改配置必须同步反映到契约（不得硬编码 300/100/50）。"""
    patched = config.model_copy(
        update={
            "week5": config.week5.model_copy(
                update={
                    "night_quality_target": 250,
                    "night_light_candidate_target": 80,
                    "night_deep_candidate_target": 40,
                }
            )
        }
    )
    contract = resolve_selection_contract(patched, profile="night_scan")
    assert (contract.quality_target, contract.light_target, contract.deep_target) == (250, 80, 40)


def test_contract_rejects_empty_profile_gracefully(config) -> None:
    contract = resolve_selection_contract(config, profile="")
    assert contract.profile == "default"
    assert contract.unified is False


# ---------------------------------------------------------------------------
# 集成：引擎报告里必须带契约块，且历史路径用夜扫口径
# ---------------------------------------------------------------------------


def test_engine_funnel_report_carries_selection_contract(tmp_path: Path) -> None:
    """引擎报告 funnel 段必须带 selection_contract（供生产/历史同口径比对）。"""
    import pandas as pd

    from stock_analyzer.runtime.services.week5_selection_engine import (
        Week5RunPolicy,
        Week5SelectionEngine,
    )
    from tests.test_week5_historical_backtest import (  # type: ignore[attr-defined]
        _FakeUniverseProvider,
        _StubBackend,
        _historical_config,
        _historical_context,
    )

    config = _historical_config(tmp_path)
    symbols = ["600000", "000001", "600519", "300750"]
    backend = _StubBackend(config, symbols=symbols)
    provider = _FakeUniverseProvider(symbols)
    context = _historical_context(
        config=config,
        provider=provider,
        tmp_path=tmp_path,
        run_pipeline_fn=lambda **kwargs: backend.run_pipeline(**kwargs),
        symbols=None,
    )
    engine = Week5SelectionEngine(
        backend=backend, context=context, policy=Week5RunPolicy.historical()
    )
    report = engine.run()
    contract_payload = report["funnel"]["selection_contract"]
    assert contract_payload["selection_contract_id"] == NIGHT_ALPHA_V2_CONTRACT_ID
    assert contract_payload["quality_target"] == 300
    assert contract_payload["light_target"] == 100
    assert contract_payload["deep_target"] == 50
    assert report["funnel"]["deep_candidate_target"] == 50
    assert report["prefilter"]["historical_universe"]["quality_target"] == 300
    assert isinstance(pd.DataFrame, type)


def test_historical_runner_default_profile_is_night_equivalent() -> None:
    """守卫：历史重放默认 profile 必须是 night-equivalent，否则又回到 100/100/20。"""
    import inspect

    from stock_analyzer.runtime.services import week5_historical_runner

    signature = inspect.signature(week5_historical_runner.run_week5_historical_day)
    assert signature.parameters["scan_profile"].default == "historical_night_equivalent"
