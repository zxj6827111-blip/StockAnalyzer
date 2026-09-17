"""S06 HistoricalModelResolver：as_of 之前真实存在且可用的模型，否则 unscorable。

对应蓝图 §5 P0-03 / 阶段施工提示词 S06 的 Done When：

```text
任何 as_of 都不会加载 as_of 之后创建/激活的模型；找不到时明确 unscorable
```

三条绝对约束（本文件逐个钉住）：

1. ``no eligible PIT model -> fallback current serving model`` 是**禁止**的；
2. 工件缺失与 registry 状态无关，恒为硬拒绝（Codex N2）；
3. 时间语义不可验证时 fail-closed，而不是当它合法。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_analyzer.models.historical_resolver import (
    MODE_PIT_RESEARCH,
    MODE_STRICT_PRODUCTION_REPLAY,
    REASON_NO_ACTIVATION_EVIDENCE,
    REASON_NO_CANDIDATES,
    REASON_NO_ELIGIBLE_PIT_MODEL,
    STATUS_RESOLVED,
    STATUS_UNSCORABLE,
    load_registry_candidates,
    resolve_historical_model,
)

# 决策时刻：2026-09-30 15:30（Asia/Shanghai，aware）
DECISION = datetime(2026, 9, 30, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def _candidate(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "model_id": "model_v3_abc",
        "artifact_uri": "/app/artifacts/model_archive/model_v2_abc/model.json",
        "artifact_content_hash": "a" * 64,
        "artifact_created_at": "2026-08-16T10:00:00+08:00",
        "feature_schema_id": "fs_v1",
        "feature_schema_hash": "fs-hash",
        "label_policy_id": "label_policy_v1_e2afc1135a3f",
        "label_policy_hash": "label-hash",
        "dataset_manifest_id": "dataset_manifest_1",
        "lifecycle_state": "trained",
        "artifact_exists": True,
        "content_hash_verified": True,
        "dataset_manifest_exists": True,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 核心不变量：不得加载 as_of 之后创建的模型
# ---------------------------------------------------------------------------


def test_model_created_before_as_of_is_resolved() -> None:
    result = resolve_historical_model(
        as_of=DECISION, candidates=[_candidate()], mode=MODE_PIT_RESEARCH
    )
    assert result.status == STATUS_RESOLVED
    assert result.scorable is True
    assert result.model_id == "model_v3_abc"
    assert result.artifact_content_hash == "a" * 64
    assert result.fallback_used is False
    assert result.time_semantics == "aware"


def test_model_created_after_as_of_is_rejected_not_fallback() -> None:
    """未来模型必须被拒；且**不得**回退到其它模型（哪怕它可用）。"""
    result = resolve_historical_model(
        as_of=DECISION,
        candidates=[_candidate(artifact_created_at="2026-10-15T10:00:00+08:00")],
        mode=MODE_PIT_RESEARCH,
    )
    assert result.status == STATUS_UNSCORABLE
    assert result.reason == REASON_NO_ELIGIBLE_PIT_MODEL
    assert result.model_id == ""
    assert result.fallback_used is False
    assert result.rejected["model_v3_abc"] == "created_after_decision"


def test_picks_latest_eligible_model_before_as_of() -> None:
    result = resolve_historical_model(
        as_of=DECISION,
        candidates=[
            _candidate(model_id="old", artifact_created_at="2026-01-01T00:00:00+08:00"),
            _candidate(model_id="new", artifact_created_at="2026-09-01T00:00:00+08:00"),
            _candidate(model_id="future", artifact_created_at="2026-12-01T00:00:00+08:00"),
        ],
        mode=MODE_PIT_RESEARCH,
    )
    assert result.status == STATUS_RESOLVED
    assert result.model_id == "new"
    assert result.rejected["future"] == "created_after_decision"


def test_no_candidates_is_unscorable() -> None:
    result = resolve_historical_model(as_of=DECISION, candidates=[], mode=MODE_PIT_RESEARCH)
    assert result.status == STATUS_UNSCORABLE
    assert result.reason == REASON_NO_CANDIDATES
    assert result.fallback_used is False


# ---------------------------------------------------------------------------
# 硬拒绝（与 registry 状态无关）
# ---------------------------------------------------------------------------


def test_missing_artifact_is_hard_reject_even_if_registry_ok() -> None:
    """Codex N2：工件缺失恒为硬拒绝，不得被"registry 看起来正常"掩盖。"""
    result = resolve_historical_model(
        as_of=DECISION,
        candidates=[_candidate(artifact_exists=False, content_hash_verified=False)],
        mode=MODE_PIT_RESEARCH,
    )
    assert result.status == STATUS_UNSCORABLE
    assert result.rejected["model_v3_abc"] == "artifact_missing"


def test_hash_mismatch_rejected() -> None:
    result = resolve_historical_model(
        as_of=DECISION,
        candidates=[_candidate(content_hash_verified=False)],
        mode=MODE_PIT_RESEARCH,
    )
    assert result.status == STATUS_UNSCORABLE
    assert result.rejected["model_v3_abc"] == "hash_mismatch"


def test_revoked_lifecycle_rejected() -> None:
    result = resolve_historical_model(
        as_of=DECISION,
        candidates=[_candidate(lifecycle_state="revoked")],
        mode=MODE_PIT_RESEARCH,
    )
    assert result.status == STATUS_UNSCORABLE
    assert result.rejected["model_v3_abc"] == "lifecycle_not_eligible"


def test_unknown_schema_or_label_rejected_when_allowlists_given() -> None:
    result = resolve_historical_model(
        as_of=DECISION,
        candidates=[_candidate(feature_schema_id="fs_old")],
        mode=MODE_PIT_RESEARCH,
        allowed_feature_schema_ids=["fs_v1"],
    )
    assert result.status == STATUS_UNSCORABLE
    assert result.rejected["model_v3_abc"] == "feature_schema_unknown"


def test_manifest_blocking_outcomes_immature_and_future_features_rejected() -> None:
    for override, expected in (
        ({"dataset_manifest_exists": False}, "dataset_manifest_missing"),
        ({"dataset_manifest_blocking": True}, "dataset_manifest_blocking"),
        ({"training_outcomes_mature": False}, "training_outcomes_immature"),
        ({"future_feature_availability": True}, "future_feature_availability"),
    ):
        result = resolve_historical_model(
            as_of=DECISION, candidates=[_candidate(**override)], mode=MODE_PIT_RESEARCH
        )
        assert result.status == STATUS_UNSCORABLE
        assert result.rejected["model_v3_abc"] == expected


# ---------------------------------------------------------------------------
# 两种模式
# ---------------------------------------------------------------------------


def test_strict_production_replay_requires_activation_evidence() -> None:
    """严格重放：没有历史激活证据就 unscorable，禁止用 created_at 猜。"""
    no_evidence = resolve_historical_model(
        as_of=DECISION,
        candidates=[_candidate()],  # 没有 activated_at / promoted_at
        mode=MODE_STRICT_PRODUCTION_REPLAY,
    )
    assert no_evidence.status == STATUS_UNSCORABLE
    assert no_evidence.reason == REASON_NO_ACTIVATION_EVIDENCE
    assert no_evidence.rejected["model_v3_abc"] == "not_activated_yet"


def test_strict_production_replay_uses_activation_time() -> None:
    activated = resolve_historical_model(
        as_of=DECISION,
        candidates=[_candidate(promoted_at="2026-09-01T09:00:00+08:00")],
        mode=MODE_STRICT_PRODUCTION_REPLAY,
    )
    assert activated.status == STATUS_RESOLVED
    assert activated.activated_at == "2026-09-01T09:00:00+08:00"

    later = resolve_historical_model(
        as_of=DECISION,
        candidates=[_candidate(promoted_at="2026-10-01T09:00:00+08:00")],
        mode=MODE_STRICT_PRODUCTION_REPLAY,
    )
    assert later.status == STATUS_UNSCORABLE
    assert later.rejected["model_v3_abc"] == "not_activated_yet"


def test_unknown_mode_is_unscorable_not_defaulted() -> None:
    result = resolve_historical_model(
        as_of=DECISION, candidates=[_candidate()], mode="whatever"
    )
    assert result.status == STATUS_UNSCORABLE
    assert result.reason == "mode_unsupported"


# ---------------------------------------------------------------------------
# 时间语义
# ---------------------------------------------------------------------------


def test_naive_timestamp_assumed_local_timezone_is_labeled() -> None:
    result = resolve_historical_model(
        as_of=DECISION,
        candidates=[_candidate(artifact_created_at="2026-08-16T10:00:00")],
        mode=MODE_PIT_RESEARCH,
    )
    assert result.status == STATUS_RESOLVED
    assert result.time_semantics == "assumed_local_timezone"


def test_naive_decision_time_forbidden_when_assumption_disabled() -> None:
    """禁止假定时：naive 决策时刻 → 判不了 → unscorable（fail-closed）。"""
    result = resolve_historical_model(
        as_of=datetime(2026, 9, 30, 15, 30),
        candidates=[_candidate()],
        mode=MODE_PIT_RESEARCH,
        assume_local_timezone=False,
    )
    assert result.status == STATUS_UNSCORABLE
    assert result.reason == "time_semantics_unverified"


def test_naive_candidate_rejected_when_assumption_disabled() -> None:
    result = resolve_historical_model(
        as_of=DECISION,
        candidates=[_candidate(artifact_created_at="2026-08-16T10:00:00")],
        mode=MODE_PIT_RESEARCH,
        assume_local_timezone=False,
    )
    assert result.status == STATUS_UNSCORABLE
    assert result.rejected["model_v3_abc"] == "time_semantics_unverified"


def test_aware_utc_candidate_compares_across_timezones() -> None:
    """UTC 时间戳与上海决策时刻可比：2026-09-30T20:00Z 已晚于 15:30+08:00。"""
    result = resolve_historical_model(
        as_of=DECISION,
        candidates=[_candidate(artifact_created_at="2026-09-30T20:00:00+00:00")],
        mode=MODE_PIT_RESEARCH,
    )
    assert result.status == STATUS_UNSCORABLE
    assert result.rejected["model_v3_abc"] == "created_after_decision"


# ---------------------------------------------------------------------------
# registry 适配器
# ---------------------------------------------------------------------------


class _RecordStub:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


class _RegistryStub:
    def __init__(self, records: list[object]) -> None:
        self._records = records

    def list_records(
        self, *, limit: int | None = None, suppress_read_errors: bool = False
    ) -> list[object]:
        _ = (limit, suppress_read_errors)
        return list(self._records)


def test_load_registry_candidates_attaches_disk_facts(tmp_path: Path) -> None:
    from stock_analyzer.models.bundle import compute_artifact_identity_hash

    artifact = tmp_path / "model.json"
    artifact.write_text(
        '{"version":"v2","created_at":"2026-05-01T10:00:00","feature_columns":["close"],'
        '"lgbm_model":{},"xgb_model":{},"lgbm_calibrator":{},"xgb_calibrator":{},'
        '"training_metrics":{}}',
        encoding="utf-8",
    )
    digest = compute_artifact_identity_hash(artifact)
    registry = _RegistryStub(
        [
            _RecordStub(
                model_id="m_ok",
                artifact_uri=str(artifact),
                artifact_content_hash=digest,
                artifact_created_at=datetime(2026, 5, 1, tzinfo=UTC),
                feature_schema_id="fs",
                label_policy_id="lp",
                lifecycle_state="trained",
                promoted_at=None,
            ),
            _RecordStub(
                model_id="m_missing",
                artifact_uri=str(tmp_path / "gone.json"),
                artifact_content_hash="b" * 64,
                artifact_created_at=datetime(2026, 5, 1, tzinfo=UTC),
                feature_schema_id="fs",
                label_policy_id="lp",
                lifecycle_state="trained",
                promoted_at=None,
            ),
        ]
    )
    candidates = load_registry_candidates(registry)
    by_id = {str(row["model_id"]): row for row in candidates}
    assert by_id["m_ok"]["artifact_exists"] is True
    assert by_id["m_ok"]["content_hash_verified"] is True
    assert by_id["m_missing"]["artifact_exists"] is False
    assert by_id["m_missing"]["content_hash_verified"] is False


def test_resolution_from_registry_end_to_end(tmp_path: Path) -> None:
    """适配器 + 解析器端到端：磁盘存在且 PIT 合法 → resolved。"""
    from stock_analyzer.models.bundle import compute_artifact_identity_hash

    artifact = tmp_path / "model.json"
    artifact.write_text(
        '{"version":"v2","created_at":"2026-05-01T10:00:00","feature_columns":["close"],'
        '"lgbm_model":{},"xgb_model":{},"lgbm_calibrator":{},"xgb_calibrator":{},'
        '"training_metrics":{}}',
        encoding="utf-8",
    )
    registry = _RegistryStub(
        [
            _RecordStub(
                model_id="m_pit",
                artifact_uri=str(artifact),
                artifact_content_hash=compute_artifact_identity_hash(artifact),
                artifact_created_at=datetime(2026, 5, 1, tzinfo=UTC),
                feature_schema_id="fs",
                label_policy_id="lp",
                lifecycle_state="trained",
                promoted_at=None,
            )
        ]
    )
    result = resolve_historical_model(
        as_of=DECISION, candidates=load_registry_candidates(registry), mode=MODE_PIT_RESEARCH
    )
    assert result.status == STATUS_RESOLVED
    assert result.model_id == "m_pit"
    assert result.dataset_manifest_id == ""  # 记录里没有就是空，不编造


@pytest.mark.parametrize("mode", [MODE_PIT_RESEARCH, MODE_STRICT_PRODUCTION_REPLAY])
def test_payload_is_auditable_for_both_modes(mode: str) -> None:
    payload = resolve_historical_model(
        as_of=DECISION, candidates=[_candidate()], mode=mode
    ).to_payload()
    for key in ("status", "mode", "as_of", "decision_time", "time_semantics", "fallback_used"):
        assert key in payload
    assert payload["fallback_used"] is False
