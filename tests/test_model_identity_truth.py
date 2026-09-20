"""S01 真实模型身份链：报告的实际工件必须等于真实加载的工件。

对应蓝图 §5 P0-01 / 阶段施工提示词 S01 的 Done When：

- 历史/研究输出的 actual artifact hash 与真实文件 ``sha256sum`` 一致；
- 不再用 bootstrap 时间冒充真实加载模型身份；
- registry 只做补充：``registry hash != actual hash`` → ``mismatch`` 且研究侧
  fail-closed；
- 覆盖：有 champion / 无 champion / bootstrap 比工件新 / registry hash 不符 /
  artifact 缺失 / serving alias 被覆盖。

测试用最小合法工件 JSON（``ModelArtifact.load`` 需要的键齐备），避免为了"能加载"
而跑一次真实训练；仅在验证 pipeline 真实加载路径时使用 SyntheticProvider + trainer。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stock_analyzer.config import load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.models.bundle import compute_artifact_identity_hash
from stock_analyzer.models.identity import (
    IDENTITY_MATCH,
    IDENTITY_MATCH_REGISTERED,
    IDENTITY_MISMATCH,
    IDENTITY_NO_CHAMPION,
    IDENTITY_REGISTRY_UNAVAILABLE,
    build_model_identity_report,
    load_artifact_facts,
    registry_identity,
)
from stock_analyzer.models.registry import ModelRegistry
from stock_analyzer.models.trainer import ModelTrainer
from stock_analyzer.pipeline import AnalyzerPipeline

_ROOT = Path(__file__).resolve().parents[1]

ARTIFACT_CREATED_AT = "2026-08-16T10:00:00"
BOOTSTRAP_NEWER = "2026-09-15T21:00:00"


def _write_minimal_artifact(
    path: Path,
    *,
    created_at: str = ARTIFACT_CREATED_AT,
    feature_schema_id: str = "fs_v1",
    label_policy_id: str = "label_policy_v1_e2afc1135a3f",
) -> Path:
    """写一份 ``ModelArtifact.load`` 能读的最小工件（内容用于实算哈希）。"""
    payload = {
        "version": "v2",
        "created_at": created_at,
        "feature_schema_id": feature_schema_id,
        "feature_schema_hash": "schema-hash-1",
        "label_policy_id": label_policy_id,
        "label_policy_hash": "label-hash-1",
        "dataset_manifest_id": "dataset_manifest_1",
        "feature_columns": ["close", "ret_5d"],
        "lgbm_model": {},
        "xgb_model": {},
        "lgbm_calibrator": {},
        "xgb_calibrator": {},
        "training_metrics": {"auc": 0.5},
        "metadata": {},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class _RecordStub:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


class _RegistryStub:
    def __init__(
        self,
        record: object | None,
        *,
        registered: list[object] | None = None,
        error: Exception | None = None,
        list_error: Exception | None = None,
    ) -> None:
        self._record = record
        self._registered = list(registered or [])
        self._error = error
        self._list_error = list_error

    def active_champion(self, *, suppress_read_errors: bool = False) -> object | None:
        _ = suppress_read_errors
        if self._error is not None:
            raise self._error
        return self._record

    def list_records(
        self, *, limit: int | None = None, suppress_read_errors: bool = False
    ) -> list[object]:
        _ = (limit, suppress_read_errors)
        if self._list_error is not None:
            raise self._list_error
        return list(self._registered)


class _StubService:
    """只实现身份解析真正用到的两个接口。"""

    def __init__(
        self, *, registry: object | None = None, last_bootstrap_at: str = BOOTSTRAP_NEWER
    ) -> None:
        self._model_registry = registry
        self._last_bootstrap_at = last_bootstrap_at

    def training_bootstrap_status(self) -> dict[str, object]:
        return {"last_bootstrap_at": self._last_bootstrap_at}


# ---------------------------------------------------------------------------
# 事实来源：磁盘工件
# ---------------------------------------------------------------------------


def test_load_artifact_facts_reads_real_file_facts(tmp_path: Path) -> None:
    artifact = _write_minimal_artifact(tmp_path / "model_v1.json")
    facts = load_artifact_facts(artifact)
    assert facts["artifact_exists"] is True
    assert facts["artifact_content_hash"] == compute_artifact_identity_hash(artifact)
    assert facts["artifact_created_at"] == ARTIFACT_CREATED_AT
    assert facts["feature_schema_id"] == "fs_v1"
    assert facts["label_policy_id"] == "label_policy_v1_e2afc1135a3f"
    assert facts["dataset_manifest_id"] == "dataset_manifest_1"
    assert facts["load_error"] == ""


def test_load_artifact_facts_missing_file_is_reported_not_raised(tmp_path: Path) -> None:
    facts = load_artifact_facts(tmp_path / "nope.json")
    assert facts["artifact_exists"] is False
    assert facts["artifact_content_hash"] == ""
    assert facts["load_error"] == "artifact_missing"


def test_load_artifact_facts_corrupted_file_is_reported_not_raised(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not-json", encoding="utf-8")
    facts = load_artifact_facts(broken)
    assert facts["artifact_exists"] is True
    assert str(facts["load_error"]).startswith("artifact_load_failed:")
    # 内容哈希仍要尽力算出来：文件坏了但"这份文件是什么"依然可审计。
    assert facts["artifact_content_hash"] == compute_artifact_identity_hash(broken)


# ---------------------------------------------------------------------------
# 判定：事实 + registry 补充
# ---------------------------------------------------------------------------


def test_report_matches_champion(tmp_path: Path) -> None:
    artifact = _write_minimal_artifact(tmp_path / "model_v1.json")
    facts = load_artifact_facts(artifact)
    report = build_model_identity_report(
        facts,
        registry=_RegistryStub(
            _RecordStub(
                model_id="model_v3_abc",
                artifact_content_hash=facts["artifact_content_hash"],
                lifecycle_state="champion",
            )
        ),
    )
    assert report["status"] == IDENTITY_MATCH
    assert report["identity_verified"] is True
    assert report["research_fail_closed"] is False
    assert report["registry_model_id"] == "model_v3_abc"
    # 事实字段不得被 registry 覆盖
    assert report["artifact_created_at"] == ARTIFACT_CREATED_AT
    assert report["artifact_content_hash"] == facts["artifact_content_hash"]


def test_report_no_champion_does_not_fake_a_match(tmp_path: Path) -> None:
    artifact = _write_minimal_artifact(tmp_path / "model_v1.json")
    report = build_model_identity_report(
        load_artifact_facts(artifact), registry=_RegistryStub(None)
    )
    assert report["status"] == IDENTITY_NO_CHAMPION
    assert report["identity_verified"] is False
    # 无 champion 属治理缺口，不是"身份不符"：不得因此锁死整条研究链。
    assert report["research_fail_closed"] is False


def test_report_registered_match_without_champion(tmp_path: Path) -> None:
    artifact = _write_minimal_artifact(tmp_path / "model_v1.json")
    facts = load_artifact_facts(artifact)
    report = build_model_identity_report(
        facts,
        registry=_RegistryStub(
            None,
            registered=[
                _RecordStub(
                    model_id="model_v3_6d7486bc1af6",
                    artifact_content_hash=facts["artifact_content_hash"],
                    lifecycle_state="trained",
                )
            ],
        ),
    )
    assert report["status"] == IDENTITY_MATCH_REGISTERED
    assert report["identity_verified"] is True
    assert report["registry_model_id"] == "model_v3_6d7486bc1af6"


def test_report_registry_hash_mismatch_fails_closed_for_research(tmp_path: Path) -> None:
    artifact = _write_minimal_artifact(tmp_path / "model_v1.json")
    report = build_model_identity_report(
        load_artifact_facts(artifact),
        registry=_RegistryStub(
            _RecordStub(
                model_id="model_other",
                artifact_content_hash="b" * 64,
                lifecycle_state="champion",
            )
        ),
    )
    assert report["status"] == IDENTITY_MISMATCH
    assert report["research_fail_closed"] is True
    assert report["identity_verified"] is False


def test_report_registry_unavailable_is_not_mismatch(tmp_path: Path) -> None:
    artifact = _write_minimal_artifact(tmp_path / "model_v1.json")
    report = build_model_identity_report(
        load_artifact_facts(artifact),
        registry=_RegistryStub(None, error=RuntimeError("registry db locked")),
    )
    assert report["status"] == IDENTITY_REGISTRY_UNAVAILABLE
    assert report["research_fail_closed"] is False
    assert "locked" in str(report["registry_error"])


def test_report_without_registry_attached_marks_scope(tmp_path: Path) -> None:
    artifact = _write_minimal_artifact(tmp_path / "model_v1.json")
    report = build_model_identity_report(load_artifact_facts(artifact))
    # 没有登记信息时状态是 no_champion，而不是 registry_unavailable：
    # 后者会掩盖真正的工件问题（例如下面的 loaded_hash_missing）。
    assert report["status"] == IDENTITY_NO_CHAMPION
    assert report["registry_attached"] is False
    assert report["registry_error"] == ""


def test_report_without_registry_still_reports_missing_artifact(tmp_path: Path) -> None:
    report = build_model_identity_report(load_artifact_facts(tmp_path / "missing.json"))
    assert report["status"] == "loaded_hash_missing"
    assert report["research_fail_closed"] is True


def test_registry_identity_reports_error_without_inventing_busy() -> None:
    """读失败一律按"读不到"上报；不靠异常文本猜"写锁占用"（避免假分类）。"""
    snapshot = registry_identity(_RegistryStub(None, error=RuntimeError("db locked")))
    assert snapshot["registry_busy"] is False
    assert "locked" in str(snapshot["registry_error"])


# ---------------------------------------------------------------------------
# negative：serving alias 被覆盖（工件换了但登记没换）
# ---------------------------------------------------------------------------


def test_serving_alias_overwritten_is_detected_as_mismatch(tmp_path: Path) -> None:
    artifact = _write_minimal_artifact(tmp_path / "model_v1.json")
    registered_hash = compute_artifact_identity_hash(artifact)
    registry = _RegistryStub(
        _RecordStub(
            model_id="model_v3_registered",
            artifact_content_hash=registered_hash,
            lifecycle_state="champion",
        )
    )
    assert (
        build_model_identity_report(load_artifact_facts(artifact), registry=registry)["status"]
        == IDENTITY_MATCH
    )

    # 用另一份内容覆盖同一路径（alias 被换件），登记哈希不变
    _write_minimal_artifact(tmp_path / "model_v1.json", created_at="2026-09-15T10:00:00")
    overwritten = load_artifact_facts(artifact)
    report = build_model_identity_report(
        overwritten, registry=registry, claimed_content_hash=registered_hash
    )
    assert overwritten["artifact_content_hash"] != registered_hash
    assert report["status"] == IDENTITY_MISMATCH
    assert report["research_fail_closed"] is True
    assert report["content_hash_verified"] is False


# ---------------------------------------------------------------------------
# pipeline：报告必须等于实际加载的工件
# ---------------------------------------------------------------------------


def _trained_pipeline(tmp_path: Path) -> tuple[AnalyzerPipeline, Path, str]:
    config = load_config(_ROOT / "config" / "default.yaml")
    config.training.min_samples = 40
    artifact_path = tmp_path / "artifact.json"
    config.training.artifact_path = str(artifact_path)
    provider = SyntheticProvider(seed_offset=4321)
    bars = provider.fetch_daily_bars("600000", lookback_days=300)
    trainer = ModelTrainer(training=config.training, labels=config.labels)
    trainer.train_and_save(bars=bars, output_path=config.training.artifact_path)
    created_at = json.loads(artifact_path.read_text(encoding="utf-8"))["created_at"]
    return AnalyzerPipeline(config=config, provider=provider), artifact_path, created_at


def test_pipeline_model_identity_facts_equal_real_file_hash(tmp_path: Path) -> None:
    pipeline, artifact_path, created_at = _trained_pipeline(tmp_path)
    facts = pipeline.model_identity_facts()
    assert facts["predictor_loaded"] is True
    assert facts["artifact_uri"] == str(artifact_path)
    # Done When 的核心断言：报告里的哈希 == 真实文件哈希
    assert facts["artifact_content_hash"] == compute_artifact_identity_hash(artifact_path)
    assert facts["artifact_created_at"] == created_at
    assert facts["score_source"] in {"raw", "calibrated"}
    assert isinstance(facts["output_semantics"], str)
    assert facts["load_error"] == ""


def test_pipeline_model_identity_is_read_only_and_registry_free(tmp_path: Path) -> None:
    """没有附加 registry 时也必须能给出事实（身份读取不得依赖 registry 可用）。"""
    pipeline, artifact_path, _ = _trained_pipeline(tmp_path)
    report = pipeline.model_identity()
    assert report["artifact_content_hash"] == compute_artifact_identity_hash(artifact_path)
    assert report["registry_attached"] is False


def test_pipeline_model_identity_facts_artifact_missing(tmp_path: Path) -> None:
    config = load_config(_ROOT / "config" / "default.yaml")
    config.training.artifact_path = str(tmp_path / "missing.json")
    pipeline = AnalyzerPipeline(config=config, provider=SyntheticProvider(seed_offset=1))
    facts = pipeline.model_identity_facts()
    assert facts["predictor_loaded"] is False
    assert facts["artifact_exists"] is False
    assert facts["load_error"] == "artifact_missing"
    report = pipeline.model_identity()
    assert report["status"] == "loaded_hash_missing"
    assert report["research_fail_closed"] is True


def test_pipeline_model_identity_with_champion_registry(tmp_path: Path) -> None:
    pipeline, artifact_path, _ = _trained_pipeline(tmp_path)
    registry = ModelRegistry.__new__(ModelRegistry)  # 只读桩：不建库、不写盘
    stub = _RegistryStub(
        _RecordStub(
            model_id="model_champion_1",
            artifact_content_hash=compute_artifact_identity_hash(artifact_path),
            lifecycle_state="champion",
        )
    )
    report = pipeline.model_identity(registry=stub)
    assert registry is not None  # 明确"仅用桩对象"，不触碰真实注册表
    assert report["status"] == IDENTITY_MATCH
    assert report["registry_model_id"] == "model_champion_1"


# ---------------------------------------------------------------------------
# 历史回测 / asof 回测：不得再用 bootstrap 时间冒充模型身份
# ---------------------------------------------------------------------------


def test_historical_model_info_uses_artifact_created_at_not_bootstrap(tmp_path: Path) -> None:
    from stock_analyzer.runtime.services.week5_historical_runner import _resolve_model_info

    pipeline, artifact_path, created_at = _trained_pipeline(tmp_path)
    config = load_config(_ROOT / "config" / "default.yaml")
    info = _resolve_model_info(
        service=_StubService(registry=None, last_bootstrap_at=BOOTSTRAP_NEWER),
        config=config,
        pipeline=pipeline,
    )
    assert info.trained_at == created_at
    assert info.trained_at_source == "artifact_created_at"
    assert info.artifact_content_hash == compute_artifact_identity_hash(artifact_path)
    # bootstrap 时间只作独立标注，绝不冒充训练时间
    assert info.bootstrap_last_bootstrap_at == BOOTSTRAP_NEWER
    assert BOOTSTRAP_NEWER not in info.trained_at


def test_historical_model_info_mismatch_flags_research_fail_closed(tmp_path: Path) -> None:
    from stock_analyzer.runtime.services.week5_historical_runner import _resolve_model_info

    pipeline, _, _ = _trained_pipeline(tmp_path)
    config = load_config(_ROOT / "config" / "default.yaml")
    info = _resolve_model_info(
        service=_StubService(
            registry=_RegistryStub(
                _RecordStub(
                    model_id="model_other",
                    artifact_content_hash="c" * 64,
                    lifecycle_state="champion",
                )
            )
        ),
        config=config,
        pipeline=pipeline,
    )
    assert info.identity_status == IDENTITY_MISMATCH
    assert info.research_fail_closed is True
    # 名字与内容对不上时不得报 registry 的 model_id（那正是"报告一个、加载另一个"）
    assert info.model_id == ""


def test_historical_model_info_without_artifact_marks_unavailable(tmp_path: Path) -> None:
    from stock_analyzer.runtime.services.week5_historical_runner import _resolve_model_info

    config = load_config(_ROOT / "config" / "default.yaml")
    config.training.artifact_path = str(tmp_path / "missing.json")
    pipeline = AnalyzerPipeline(config=config, provider=SyntheticProvider(seed_offset=2))
    info = _resolve_model_info(
        service=_StubService(registry=None), config=config, pipeline=pipeline
    )
    assert info.trained_at == ""
    assert info.trained_at_source == "unavailable"
    assert info.bootstrap_last_bootstrap_at == BOOTSTRAP_NEWER
    assert info.research_fail_closed is True


def test_week5_model_info_payload_carries_identity_fields() -> None:
    from stock_analyzer.runtime.services.week5_selection_engine import Week5ModelInfo

    payload = Week5ModelInfo(
        model_id="m1",
        trained_at=ARTIFACT_CREATED_AT,
        trained_at_source="artifact_created_at",
        artifact_content_hash="h",
        identity_status=IDENTITY_MATCH,
        identity_verified=True,
        bootstrap_last_bootstrap_at=BOOTSTRAP_NEWER,
    ).to_payload()
    assert payload["trained_at_source"] == "artifact_created_at"
    assert payload["artifact_content_hash"] == "h"
    assert payload["identity_status"] == IDENTITY_MATCH
    assert payload["identity_verified"] is True
    assert payload["bootstrap_last_bootstrap_at"] == BOOTSTRAP_NEWER


def test_asof_backtest_identity_helper_ignores_bootstrap_time(tmp_path: Path) -> None:
    from stock_analyzer.runtime.services.asof_backtest_service import (
        _resolve_backtest_model_identity,
    )

    artifact = _write_minimal_artifact(tmp_path / "model_v1.json")
    config = load_config(_ROOT / "config" / "default.yaml")
    config.training.artifact_path = str(artifact)
    identity = _resolve_backtest_model_identity(
        service=_StubService(registry=None, last_bootstrap_at=BOOTSTRAP_NEWER), config=config
    )
    assert identity["artifact_created_at"] == ARTIFACT_CREATED_AT
    assert identity["artifact_content_hash"] == compute_artifact_identity_hash(artifact)
    assert identity["label_policy_id"] == "label_policy_v1_e2afc1135a3f"
    assert identity["bootstrap_last_bootstrap_at"] == BOOTSTRAP_NEWER
    assert identity["identity_status"] in {IDENTITY_NO_CHAMPION, IDENTITY_REGISTRY_UNAVAILABLE}


@pytest.mark.parametrize("status", ["mismatch", "loaded_hash_missing", "champion_hash_missing"])
def test_research_fail_closed_statuses(status: str) -> None:
    from stock_analyzer.models.identity import research_fail_closed

    assert research_fail_closed(status) is True


@pytest.mark.parametrize(
    "status", ["match", "match_registered", "no_champion", "registry_unavailable", "registry_busy"]
)
def test_research_open_statuses(status: str) -> None:
    from stock_analyzer.models.identity import research_fail_closed

    assert research_fail_closed(status) is False
