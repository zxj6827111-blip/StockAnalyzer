"""A3：v3 标签契约的参数持久化与可重放（learning chain remediation v2）。

被测契约：

- ``LabelPolicyRecord`` 持久化 top/bottom quantile、drop_middle、min_cross_section
  （四个参数此前只进 hash，不进记录与表）；
- 派生标签只按 manifest 绑定的契约取参，**不读当前 config**；
- 旧 v3 记录（缺参数）仅在当前 config 能重算出同一 hash 时受控采用，否则显式拒绝；
- v1/v2（soup）契约不含这些参数，不受影响。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stock_analyzer.config import LabelsConfig, StockAnalyzerConfig, load_config
from stock_analyzer.learning.label_policy_registry import (
    LabelPolicyRegistry,
    ReturnRankParams,
    build_label_policy_record,
    build_return_rank_policy_record,
    resolve_return_rank_params,
)
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.models.trainer import ModelTrainer, _return_rank_labels_from_outcomes

_ROOT = Path(__file__).resolve().parents[1]


def _config() -> StockAnalyzerConfig:
    config = load_config(_ROOT / "config" / "default.yaml")
    config.training.min_samples = 20
    config.training.validation_ratio = 0.2
    config.training.calibration_ratio = 0.1
    config.training.test_ratio = 0.1
    config.training.min_test_split_window_days = 1
    config.training.min_test_split_unique_symbol_dates = 1
    return config


def _return_rank_labels(basis: LabelsConfig, **overrides: object) -> LabelsConfig:
    values: dict[str, object] = {
        "basis": "return_rank",
        "horizon_days": 2,
        "return_rank_min_cross_section": 5,
    }
    values.update(overrides)
    return basis.model_copy(update=values)


def _write_cross_sections(
    store: SampleStore,
    *,
    label_policy_id: str,
    label_policy_hash: str,
    day_count: int,
    symbols_per_day: int,
    returns: list[float] | None = None,
) -> int:
    """写入 day_count 个决策日 × symbols_per_day 行的截面，返回行数。"""

    base_time = datetime(2026, 1, 1, 14, 30, tzinfo=UTC)
    default_returns = [0.1 - 0.02 * index for index in range(symbols_per_day)]
    rows = 0
    for day in range(day_count):
        decision_time = base_time + timedelta(days=day)
        for index in range(symbols_per_day):
            symbol = f"600{index:03d}.SH"
            snapshot_id = f"snap-{day:03d}-{symbol}"
            store.write_snapshot(
                SignalSnapshot(
                    snapshot_id=snapshot_id,
                    code_version="git:test",
                    symbol=symbol,
                    strategy="trend",
                    decision_time=decision_time,
                    feature_vector={"feature_a": float(index) / 10.0, "feature_b": 0.5},
                    feature_schema_id="fs-test",
                    feature_schema_hash="fsh-test",
                    runtime_config_hash="runtime_hash_test",
                    label_policy_id=label_policy_id,
                    label_policy_hash=label_policy_hash,
                )
            )
            values = returns if returns is not None else default_returns
            store.upsert_outcome(
                OutcomeRecord(
                    snapshot_id=snapshot_id,
                    maturity_status=MaturityStatus.RECONCILED,
                    label_anchor_time=decision_time,
                    label_mature_time=decision_time + timedelta(days=2),
                    realized_return=values[index],
                    backfill_fidelity_tier=BackfillFidelityTier.GOLD,
                    backfill_source="runtime_observed",
                )
            )
            rows += 1
    return rows


def test_v3_registration_persists_quantile_contract(tmp_path: Path) -> None:
    """v3 契约的四个分位参数必须持久化并可逐字读回。"""

    registry = LabelPolicyRegistry(db_path=tmp_path / "label_policy.duckdb")
    record = build_return_rank_policy_record(
        horizon_days=10,
        top_quantile=0.25,
        bottom_quantile=0.35,
        drop_middle=False,
        min_cross_section=42,
    )
    registry.register(record)

    reloaded = registry.get_by_id(record.label_policy_id)
    assert reloaded is not None
    assert reloaded.top_quantile == pytest.approx(0.25)
    assert reloaded.bottom_quantile == pytest.approx(0.35)
    assert reloaded.drop_middle is False
    assert reloaded.min_cross_section == 42
    assert reloaded.return_rank_params() == ReturnRankParams(
        top_quantile=0.25,
        bottom_quantile=0.35,
        drop_middle=False,
        min_cross_section=42,
    )
    # 参数进 hash：改参数必得新契约 id（原有不变量不能因持久化而失效）。
    other = build_return_rank_policy_record(
        horizon_days=10,
        top_quantile=0.25,
        bottom_quantile=0.35,
        drop_middle=False,
        min_cross_section=43,
    )
    assert other.label_policy_hash != record.label_policy_hash


def test_v1_v2_records_keep_quantile_contract_unset(tmp_path: Path) -> None:
    """soup 契约天然没有分位参数：保持 None，不参与 v3 校验。"""

    registry = LabelPolicyRegistry(db_path=tmp_path / "label_policy.duckdb")
    record = build_label_policy_record(
        label_name="label_future_return",
        take_profit_pct=0.06,
        stop_loss_pct=0.04,
        horizon_days=10,
        price_basis="next_tradable_open",
        exclude_untradable=True,
        conflict_policy="soft_label",
        conflict_soft_label_value=0.5,
    )
    registry.register(record)

    reloaded = registry.get_by_id(record.label_policy_id)
    assert reloaded is not None
    assert reloaded.return_rank_params() is None
    with pytest.raises(ValueError, match="missing the v3 quantile contract"):
        resolve_return_rank_params(reloaded)


def test_legacy_v3_record_migrates_only_when_hash_matches(tmp_path: Path) -> None:
    """旧 v3 记录缺参数：仅当配置能重算出同一 hash 时受控采用，否则拒绝。"""

    registry = LabelPolicyRegistry(db_path=tmp_path / "label_policy.duckdb")
    full = build_return_rank_policy_record(horizon_days=10, top_quantile=0.3, min_cross_section=30)
    # 模拟迁移前的旧记录：参数列为 NULL，hash 仍是绑定后的值。
    legacy = full.model_copy(
        update={
            "top_quantile": None,
            "bottom_quantile": None,
            "drop_middle": None,
            "min_cross_section": None,
        }
    )
    registry.register(legacy)
    stored = registry.get_by_id(full.label_policy_id)
    assert stored is not None
    assert stored.return_rank_params() is None

    matching = _config().labels.model_copy(
        update={"horizon_days": 10, "return_rank_min_cross_section": 30}
    )
    recovered = resolve_return_rank_params(stored, config_labels=matching)
    assert recovered == ReturnRankParams(
        top_quantile=0.3,
        bottom_quantile=0.3,
        drop_middle=True,
        min_cross_section=30,
    )

    drifted = matching.model_copy(update={"return_rank_min_cross_section": 20})
    with pytest.raises(ValueError, match="re-register the contract"):
        resolve_return_rank_params(stored, config_labels=drifted)


def test_derived_labels_follow_contract_not_current_config(tmp_path: Path) -> None:
    """复现审核对照实验：同 100 条收益，契约参数不随配置漂移。

    契约登记 0.3/0.3 → 60 条可训练标签；把当前配置改成 0.2/0.2 后，按契约派生
    仍是 60 条（旧实现读 config，会变成 40 条）。
    """

    contract_config = _return_rank_labels(_config().labels, return_rank_min_cross_section=10)
    registry = LabelPolicyRegistry(db_path=tmp_path / "label_policy.duckdb")
    record = registry.register_from_config(contract_config)

    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    returns = [0.2 - 0.004 * index for index in range(100)]
    _write_cross_sections(
        store,
        label_policy_id=record.label_policy_id,
        label_policy_hash=record.label_policy_hash,
        day_count=1,
        symbols_per_day=100,
        returns=returns,
    )
    snapshots = {snapshot.snapshot_id: snapshot for snapshot in store.list_snapshots()}
    outcomes = {outcome.snapshot_id: outcome for outcome in store.list_outcomes()}

    # 配置漂移到 0.2/0.2，但派生按契约（0.3/0.3）取参。
    drifted_config = contract_config.model_copy(
        update={
            "return_rank_top_quantile": 0.2,
            "return_rank_bottom_quantile": 0.2,
        }
    )
    contract_params = resolve_return_rank_params(record, config_labels=drifted_config)
    assert contract_params.top_quantile == pytest.approx(0.3)

    from_contract = _return_rank_labels_from_outcomes(
        outcomes=outcomes,
        snapshots=snapshots,
        params=contract_params,
    )
    from_drifted_config = _return_rank_labels_from_outcomes(
        outcomes=outcomes,
        snapshots=snapshots,
        params=ReturnRankParams(
            top_quantile=drifted_config.return_rank_top_quantile,
            bottom_quantile=drifted_config.return_rank_bottom_quantile,
            drop_middle=drifted_config.return_rank_drop_middle,
            min_cross_section=drifted_config.return_rank_min_cross_section,
        ),
    )

    assert len(from_contract) == 60
    assert len(from_drifted_config) == 40
    # 受契约约束：漂移配置不再影响派生结果。
    assert len(from_contract) == 60


def test_trainer_derives_v3_labels_from_manifest_contract(tmp_path: Path) -> None:
    """端到端：trainer 用 manifest 契约参数，而不是 trainer 自己的 labels 配置。"""

    config = _config()
    contract_labels = _return_rank_labels(
        config.labels,
        return_rank_min_cross_section=5,
        return_rank_top_quantile=0.3,
        return_rank_bottom_quantile=0.3,
    )
    registry = LabelPolicyRegistry(db_path=tmp_path / "label_policy.duckdb")
    record = registry.register_from_config(contract_labels)

    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    rows = _write_cross_sections(
        store,
        label_policy_id=record.label_policy_id,
        label_policy_hash=record.label_policy_hash,
        day_count=24,
        symbols_per_day=10,
    )
    assert rows == 240

    # trainer 自身配置漂移到 0.2/0.2：派生仍须走 manifest 契约（0.3/0.3）。
    drifted_labels = contract_labels.model_copy(
        update={
            "return_rank_top_quantile": 0.2,
            "return_rank_bottom_quantile": 0.2,
        }
    )
    trainer = ModelTrainer(
        training=config.training,
        labels=drifted_labels,
        models=config.models,
    )
    result = trainer.train_on_sample_store(
        store=store,
        feature_schema_id="fs-test",
        feature_schema_hash="fsh-test",
        label_policy_id=record.label_policy_id,
        label_policy_hash=record.label_policy_hash,
        label_policy_registry=registry,
    )

    # 独立复算：按训练产物反查 manifest 成员，分别用契约参数与漂移配置参数重算
    # 应得标签数。断言实际样本数等于**契约**口径、且两者可区分；不写死行数，
    # 避免与切分/purge 规模耦合。
    manifest = store.get_manifest(str(result.artifact.dataset_manifest_id))
    assert manifest is not None
    item_ids = [
        item.snapshot_id for item in store.list_manifest_items(manifest.dataset_manifest_id)
    ]
    manifest_outcomes = {
        outcome.snapshot_id: outcome for outcome in store.list_outcomes(snapshot_ids=item_ids)
    }
    manifest_snapshots = {
        snapshot.snapshot_id: snapshot for snapshot in store.list_snapshots(snapshot_ids=item_ids)
    }
    contract_expected = len(
        _return_rank_labels_from_outcomes(
            outcomes=manifest_outcomes,
            snapshots=manifest_snapshots,
            params=ReturnRankParams(
                top_quantile=0.3,
                bottom_quantile=0.3,
                drop_middle=True,
                min_cross_section=5,
            ),
        )
    )
    drifted_expected = len(
        _return_rank_labels_from_outcomes(
            outcomes=manifest_outcomes,
            snapshots=manifest_snapshots,
            params=ReturnRankParams(
                top_quantile=0.2,
                bottom_quantile=0.2,
                drop_middle=True,
                min_cross_section=5,
            ),
        )
    )
    assert contract_expected != drifted_expected
    assert result.samples_total == contract_expected
