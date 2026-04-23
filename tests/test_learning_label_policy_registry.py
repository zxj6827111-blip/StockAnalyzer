from __future__ import annotations

from pathlib import Path

import pytest

from stock_analyzer.config import LabelsConfig
from stock_analyzer.learning.label_policy_registry import (
    LabelPolicyRegistry,
    build_label_policy_record,
)


def test_label_policy_registry_registers_from_config_and_round_trips(tmp_path: Path) -> None:
    registry = LabelPolicyRegistry(db_path=tmp_path / "label_policy.duckdb")
    labels = LabelsConfig(
        primary="soup_5d_tp5_before_sl5",
        take_profit_pct=0.05,
        stop_loss_pct=0.05,
        horizon_days=5,
        exclude_untradable=True,
        pnl_price_basis="next_tradable_vwap",
        conflict_policy="conservative_zero",
        conflict_soft_label_value=0.5,
    )

    record = registry.register_from_config(labels, maturity_rule="label_mature_time_v1")

    loaded = registry.get_by_id(record.label_policy_id)
    assert loaded is not None
    assert loaded.model_dump() == record.model_dump()
    assert loaded.price_basis == "next_tradable_vwap"
    assert loaded.conflict_policy == "conservative_zero"


def test_label_policy_registry_dedupes_identical_contract_by_hash(tmp_path: Path) -> None:
    registry = LabelPolicyRegistry(db_path=tmp_path / "label_policy.duckdb")

    first = registry.register(
        build_label_policy_record(
            label_name="soup_5d_tp5_before_sl5",
            take_profit_pct=0.05,
            stop_loss_pct=0.05,
            horizon_days=5,
            price_basis="next_tradable_vwap",
            exclude_untradable=True,
            conflict_policy="conservative_zero",
            conflict_soft_label_value=0.5,
        )
    )
    second = registry.register(
        build_label_policy_record(
            label_name="soup_5d_tp5_before_sl5",
            take_profit_pct=0.05,
            stop_loss_pct=0.05,
            horizon_days=5,
            price_basis="next_tradable_vwap",
            exclude_untradable=True,
            conflict_policy="conservative_zero",
            conflict_soft_label_value=0.5,
            label_policy_id="custom_but_same_payload",
        )
    )

    assert second.label_policy_id == first.label_policy_id
    assert len(registry.list_records()) == 1


def test_label_policy_registry_rejects_conflicting_reuse_of_policy_id(tmp_path: Path) -> None:
    registry = LabelPolicyRegistry(db_path=tmp_path / "label_policy.duckdb")
    registry.register(
        build_label_policy_record(
            label_name="soup_5d_tp5_before_sl5",
            take_profit_pct=0.05,
            stop_loss_pct=0.05,
            horizon_days=5,
            price_basis="next_tradable_vwap",
            exclude_untradable=True,
            conflict_policy="conservative_zero",
            conflict_soft_label_value=0.5,
            label_policy_id="label_policy_v1_fixed",
        )
    )

    with pytest.raises(ValueError, match="label_policy_id already registered"):
        registry.register(
            build_label_policy_record(
                label_name="soup_10d_tp8_before_sl5",
                take_profit_pct=0.08,
                stop_loss_pct=0.05,
                horizon_days=10,
                price_basis="next_tradable_vwap",
                exclude_untradable=True,
                conflict_policy="conservative_zero",
                conflict_soft_label_value=0.5,
                label_policy_id="label_policy_v1_fixed",
            )
        )


def test_label_policy_hash_changes_when_contract_changes() -> None:
    first = build_label_policy_record(
        label_name="soup_5d_tp5_before_sl5",
        take_profit_pct=0.05,
        stop_loss_pct=0.05,
        horizon_days=5,
        price_basis="next_tradable_vwap",
        exclude_untradable=True,
        conflict_policy="conservative_zero",
        conflict_soft_label_value=0.5,
    )
    second = build_label_policy_record(
        label_name="soup_5d_tp5_before_sl5",
        take_profit_pct=0.05,
        stop_loss_pct=0.05,
        horizon_days=7,
        price_basis="next_tradable_vwap",
        exclude_untradable=True,
        conflict_policy="conservative_zero",
        conflict_soft_label_value=0.5,
    )

    assert second.label_policy_hash != first.label_policy_hash
