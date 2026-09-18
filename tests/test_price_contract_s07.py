"""S07 价格口径与执行缺陷修复（DF-S02-001/002/003 + Codex N3）。

对应蓝图 §5 P0-06 / 阶段施工提示词 S07：

```text
Feature Series != Tradable Execution Series
执行必须 raw；报告必须同时写 feature_price_mode 与 execution_price_mode
```

并落实 Codex 复审要求：
- DF-S02-001：删除估算涨跌停注入（缺列 → fail-closed，而不是按 ±10% 猜）；
- DF-S02-002：NaN/Inf 涨跌停价视为缺失（此前 NaN 比较恒 False → 涨停门 fail-open）；
- DF-S02-003：执行侧不再默认 0 滑点；
- N3：``EntrySimulation.slippage`` 是价格增量而非成交价本身。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from stock_analyzer.backtest.holding_curve import analyze_symbol_holding
from stock_analyzer.backtest.matcher import EntrySimulation, ExecutionMatcher
from stock_analyzer.backtest.price_contract import (
    EXECUTION_PRICE_MODE_RAW,
    FEATURE_PRICE_MODE_QFQ,
    resolve_price_contract,
)
from stock_analyzer.config import BacktestMatcherConfig, LimitRuleConfig, load_config
from stock_analyzer.data.limit_rule import build_price_limits

_ROOT = Path(__file__).resolve().parents[1]
_SIGNAL = datetime(2026, 9, 17, 15, 30)


@pytest.fixture(scope="module")
def production_config():
    return load_config(_ROOT / "config" / "default.yaml")


@pytest.fixture()
def matcher() -> ExecutionMatcher:
    return ExecutionMatcher(BacktestMatcherConfig(), limit_rule=LimitRuleConfig())


# ---------------------------------------------------------------------------
# DF-S02-002：NaN 涨跌停价一律 fail-closed（不得静默放行）
# ---------------------------------------------------------------------------


def test_nan_up_limit_is_treated_as_missing() -> None:
    limits = build_price_limits(
        bar={
            "up_limit": float("nan"),
            "down_limit": float("nan"),
            "close": 11.0,
            "pre_close": 10.0,
        },
        config=LimitRuleConfig(),
    )
    # NaN 不是有效涨跌停价：应退回 fallback 解析（10.0 × 1.1 = 11.0）
    assert limits.source == "fallback"
    assert limits.up_limit == pytest.approx(11.0)


def test_nan_limits_without_price_basis_fail_closed() -> None:
    """既无有效涨跌停价、又无 pre_close/pct_change → 不能猜，必须判不了。"""
    limits = build_price_limits(
        bar={"up_limit": float("nan"), "down_limit": float("nan"), "close": 10.0},
        config=LimitRuleConfig(),
    )
    assert limits.up_limit is None
    assert limits.down_limit is None


def test_nan_up_limit_does_not_let_one_price_limit_up_be_bought(
    matcher: ExecutionMatcher,
) -> None:
    """DF-S02-002 的实测复现：NaN 涨停价曾让一字涨停被买入。"""
    bar = {
        "open": 11.0,
        "high": 11.0,
        "low": 11.0,
        "close": 11.0,
        "pre_close": 10.0,
        "up_limit": float("nan"),
        "down_limit": float("nan"),
        "suspended": False,
    }
    result = matcher.simulate_entry(
        signal_date=_SIGNAL, future_bars=[(datetime(2026, 9, 18), bar)]
    )
    assert result.executed is False
    assert result.no_fill_reason == "limit_up_open"


# ---------------------------------------------------------------------------
# DF-S02-001：不再注入估算涨跌停（缺列 → fail-closed）
# ---------------------------------------------------------------------------


def _frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    dates = pd.bdate_range(start="2026-01-05", periods=len(rows))
    return pd.DataFrame(rows, index=dates)


def test_missing_limit_columns_fail_closed_instead_of_guessing(
    matcher: ExecutionMatcher,
) -> None:
    """旧行为：缺列时注入 close*1.1/0.9（掩盖真实板块涨跌幅）。新行为：判不了就不成交。"""
    frame = _frame(
        [
            {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "suspended": False},
            {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "suspended": False},
        ]
    )
    result = analyze_symbol_holding(
        symbol="TEST", bars=frame, entry_date=date(2026, 1, 5), matcher=matcher, horizon_days=1
    )
    assert result.status == "no_fill"
    assert result.no_fill_reason == "no_valid_price_data"


def test_one_price_limit_up_now_detected_in_holding_curve(matcher: ExecutionMatcher) -> None:
    """旧行为的实测反例：一字涨停曾因注入 close*1.1 被判成可成交。"""
    frame = _frame(
        [
            {
                "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
                "pre_close": 10.0, "up_limit": 11.0, "down_limit": 9.0, "suspended": False,
            },
            {
                "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0,
                "pre_close": 10.0, "up_limit": 11.0, "down_limit": 9.0, "suspended": False,
            },
        ]
    )
    result = analyze_symbol_holding(
        symbol="TEST", bars=frame, entry_date=date(2026, 1, 5), matcher=matcher, horizon_days=1
    )
    assert result.status == "no_fill"
    assert result.no_fill_reason == "limit_up_open"


def test_real_limit_columns_still_allow_fill(matcher: ExecutionMatcher) -> None:
    """反向保护：给了真实涨跌停价时必须正常成交（修复不能把正常路径也堵死）。"""
    frame = _frame(
        [
            {
                "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
                "pre_close": 10.0, "up_limit": 11.0, "down_limit": 9.0, "suspended": False,
            },
            {
                "open": 10.2, "high": 10.5, "low": 10.1, "close": 10.4,
                "pre_close": 10.0, "up_limit": 11.0, "down_limit": 9.0, "suspended": False,
            },
            {
                "open": 10.4, "high": 10.6, "low": 10.3, "close": 10.5,
                "pre_close": 10.4, "up_limit": 11.44, "down_limit": 9.36, "suspended": False,
            },
        ]
    )
    result = analyze_symbol_holding(
        symbol="TEST", bars=frame, entry_date=date(2026, 1, 5), matcher=matcher, horizon_days=1
    )
    assert result.status == "ok"
    assert result.entry_price_raw == pytest.approx(10.2)


# ---------------------------------------------------------------------------
# 价格口径契约
# ---------------------------------------------------------------------------


def test_price_contract_reports_both_modes(production_config) -> None:
    contract = resolve_price_contract(production_config)
    payload = contract.to_payload()
    assert payload["feature_price_mode"] in {"qfq", "raw"}
    assert payload["execution_price_mode"] in {"qfq", "raw"}
    assert payload["policy"] == "feature_may_be_adjusted_execution_must_be_raw"
    assert isinstance(payload["execution_uncertain"], bool)


def test_price_contract_flags_qfq_execution_as_uncertain(production_config) -> None:
    """执行口径不是 raw → 标 execution_uncertain（复权价不得当成交价）。"""
    patched = production_config.model_copy(
        update={
            "evolution": production_config.evolution.model_copy(
                update={
                    "execution_spec": production_config.evolution.execution_spec.model_copy(
                        update={"price_series_mode": "qfq"}
                    )
                }
            )
        }
    )
    contract = resolve_price_contract(patched)
    assert contract.execution_price_mode == "qfq"
    assert contract.execution_uncertain is True
    assert "raw" in contract.execution_uncertain_reason


def test_price_contract_raw_execution_is_certain(production_config) -> None:
    patched = production_config.model_copy(
        update={
            "evolution": production_config.evolution.model_copy(
                update={
                    "execution_spec": production_config.evolution.execution_spec.model_copy(
                        update={"price_series_mode": "raw"}
                    )
                }
            )
        }
    )
    contract = resolve_price_contract(patched)
    assert contract.execution_price_mode == EXECUTION_PRICE_MODE_RAW
    assert contract.execution_uncertain is False
    # 特征仍可以是 qfq（两者允许不同——这正是本阶段要分离的东西）
    assert contract.feature_price_mode == FEATURE_PRICE_MODE_QFQ
    assert contract.feature_equals_execution is False


# ---------------------------------------------------------------------------
# DF-S02-003：执行滑点不再为 0
# ---------------------------------------------------------------------------


def test_matcher_exposes_static_slippage(matcher: ExecutionMatcher) -> None:
    config = BacktestMatcherConfig()
    assert matcher.static_slippage_ratio("trend") == pytest.approx(
        config.slippage_by_strategy["trend"]
    )
    assert matcher.static_slippage_ratio("unknown-strategy") == pytest.approx(0.0)


def test_entry_slippage_is_price_delta_not_price() -> None:
    """Codex N3：slippage 字段是价格增量，net_entry_price 才是成交价。"""
    result = EntrySimulation(
        executed=True,
        signal_date=_SIGNAL,
        entry_price_raw=10.0,
        reference_open_raw=10.0,
        slippage=0.015,
        cost=5.0,
        net_entry_price=10.015,
    )
    assert result.slippage == pytest.approx(result.net_entry_price - result.entry_price_raw)
    doc = EntrySimulation.__doc__ or ""
    assert "价格增量" in doc


def test_asof_backtest_caveats_carry_price_contract_and_slippage(tmp_path: Path) -> None:
    """服务层必须把价格口径与执行滑点落进 caveats（不再默认 0 滑点）。"""
    import tempfile

    from stock_analyzer.runtime.services.asof_backtest_service import AsofBacktestService

    class _StubService:
        def __init__(self, config):
            self._config = config
            self._model_registry = None

        def watchlist_symbols(self):
            return ["600000"]

        def training_bootstrap_status(self):
            return {}

    config = load_config(_ROOT / "config" / "default.yaml")
    config.asof_backtest.output_dir = str(Path(tempfile.mkdtemp()) / "asof")
    service = AsofBacktestService(_StubService(config))
    result = service.run(
        symbols=["600000"],
        start_date=date(2026, 7, 31),
        end_date=date(2026, 7, 31),
        top_n=1,
        horizon_days=5,
    )
    caveats = result["caveats"]
    assert "price_contract" in caveats
    assert caveats["execution_slippage_ratio"] > 0
    assert caveats["execution_contract"]["entry_price_basis"] == "raw_open"


def test_holding_curve_applies_slippage_to_entry_price(matcher: ExecutionMatcher) -> None:
    frame = _frame(
        [
            {
                "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
                "pre_close": 10.0, "up_limit": 11.0, "down_limit": 9.0, "suspended": False,
            },
            {
                "open": 10.0, "high": 10.5, "low": 9.9, "close": 10.2,
                "pre_close": 10.0, "up_limit": 11.0, "down_limit": 9.0, "suspended": False,
            },
            {
                "open": 10.2, "high": 10.4, "low": 10.1, "close": 10.3,
                "pre_close": 10.2, "up_limit": 11.22, "down_limit": 9.18, "suspended": False,
            },
        ]
    )
    no_slip = analyze_symbol_holding(
        symbol="TEST", bars=frame, entry_date=date(2026, 1, 5), matcher=matcher,
        horizon_days=1, slippage_ratio=0.0,
    )
    with_slip = analyze_symbol_holding(
        symbol="TEST", bars=frame, entry_date=date(2026, 1, 5), matcher=matcher,
        horizon_days=1, slippage_ratio=0.002,
    )
    assert no_slip.status == "ok" and with_slip.status == "ok"
    assert with_slip.entry_price > no_slip.entry_price
    assert with_slip.entry_slippage > 0
    assert no_slip.entry_price_raw == with_slip.entry_price_raw  # raw 开盘价不变
