"""在服工件的身份对账契约（models/identity.py + 加载期哈希接线）。

背景（2026-09-16）：registry 有 ``artifact_content_hash`` 列、``bundle.py`` 能算哈希，
但**加载路径从不参与**，于是"生产在跑哪个工件"只能靠文件 mtime 旁证。本文件钉住：

1. 六种身份状态的判定顺序——尤其「无法判定」不得被读成 match；
2. 盖章哈希（发布时写入）vs 实算哈希（加载时算出）的一致性三态；
3. ``SignalPredictor.load`` 真的把实算哈希记进 mode_details。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stock_analyzer.models.bundle import compute_artifact_identity_hash
from stock_analyzer.models.identity import (
    IDENTITY_CHAMPION_HASH_MISSING,
    IDENTITY_LOADED_HASH_MISSING,
    IDENTITY_MATCH,
    IDENTITY_MATCH_REGISTERED,
    IDENTITY_MISMATCH,
    IDENTITY_NO_CHAMPION,
    IDENTITY_REGISTRY_BUSY,
    IDENTITY_REGISTRY_UNAVAILABLE,
    IDENTITY_STATUSES,
    content_hash_matches_stamp,
    describe_artifact_identity,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


# --- 判定顺序 ----------------------------------------------------------------


def test_registry_error_wins_over_everything() -> None:
    """读不到注册表 ≠ 身份不一致——绝不能报 mismatch。"""
    result = describe_artifact_identity(
        loaded_uri="/app/artifacts/model_v1.json",
        loaded_hash=HASH_A,
        champion={"model_id": "m1", "artifact_content_hash": HASH_B},
        registry_error="RuntimeError: db locked",
    )
    assert result["status"] == IDENTITY_REGISTRY_UNAVAILABLE
    assert "db locked" in str(result["detail"])


def test_loaded_hash_missing_is_not_a_match() -> None:
    """在服工件没算出哈希时，即便 champion 有哈希也不得判 match。"""
    result = describe_artifact_identity(
        loaded_uri="/app/artifacts/model_v1.json",
        loaded_hash="",
        champion={"model_id": "m1", "artifact_content_hash": HASH_A},
    )
    assert result["status"] == IDENTITY_LOADED_HASH_MISSING


def test_no_champion_is_distinct_from_mismatch() -> None:
    result = describe_artifact_identity(loaded_uri="x", loaded_hash=HASH_A, champion=None)
    assert result["status"] == IDENTITY_NO_CHAMPION


def test_champion_without_hash_is_not_a_match() -> None:
    """这正是 2026-09-05 quarantine:empty_content_hash 拦下那条 champion 的形态。"""
    result = describe_artifact_identity(
        loaded_uri="x", loaded_hash=HASH_A, champion={"model_id": "m1", "artifact_content_hash": ""}
    )
    assert result["status"] == IDENTITY_CHAMPION_HASH_MISSING
    assert result["champion_model_id"] == "m1"


def test_match_and_mismatch() -> None:
    same = describe_artifact_identity(
        loaded_uri="x",
        loaded_hash=HASH_A,
        champion={"model_id": "m1", "artifact_content_hash": HASH_A},
    )
    assert same["status"] == IDENTITY_MATCH
    other = describe_artifact_identity(
        loaded_uri="x",
        loaded_hash=HASH_A,
        champion={"model_id": "m1", "artifact_content_hash": HASH_B},
    )
    assert other["status"] == IDENTITY_MISMATCH
    assert "m1" in str(other["detail"])


def test_hash_comparison_ignores_case_and_whitespace() -> None:
    """格式差异不该被读成身份不符（那会制造假警报）。"""
    result = describe_artifact_identity(
        loaded_uri="x",
        loaded_hash=f"  {HASH_A.upper()}  ",
        champion={"model_id": "m1", "artifact_content_hash": HASH_A},
    )
    assert result["status"] == IDENTITY_MATCH


def test_statuses_are_the_declared_set() -> None:
    assert set(IDENTITY_STATUSES) == {
        IDENTITY_MATCH,
        IDENTITY_MATCH_REGISTERED,
        IDENTITY_MISMATCH,
        IDENTITY_NO_CHAMPION,
        IDENTITY_CHAMPION_HASH_MISSING,
        IDENTITY_LOADED_HASH_MISSING,
        IDENTITY_REGISTRY_UNAVAILABLE,
        IDENTITY_REGISTRY_BUSY,
    }


# --- 盖章 vs 实算 ------------------------------------------------------------


def test_content_hash_matches_stamp_three_states() -> None:
    assert content_hash_matches_stamp(claimed="", actual=HASH_A) is None
    assert content_hash_matches_stamp(claimed=HASH_A, actual="") is None
    assert content_hash_matches_stamp(claimed="", actual="") is None
    assert content_hash_matches_stamp(claimed=HASH_A.upper(), actual=HASH_A) is True
    assert content_hash_matches_stamp(claimed=HASH_B, actual=HASH_A) is False


# --- 加载期接线 --------------------------------------------------------------


def test_predictor_load_records_content_hash_and_uri(tmp_path: Path) -> None:
    """load 必须记下**实际文件**的哈希与来源路径，且不依赖发布时盖章。"""
    from test_model_inference_safety import _artifact_from_payload, _fit_scaled_model

    from stock_analyzer.models.predictor import SignalPredictor

    artifact_path = tmp_path / "model_v1.json"
    _artifact_from_payload(_fit_scaled_model(seed=31).to_dict(), path=artifact_path)

    predictor = SignalPredictor.load(artifact_path)
    details = predictor.mode_details()
    assert details["artifact_uri"] == str(artifact_path)
    assert details["artifact_content_hash"] == compute_artifact_identity_hash(artifact_path)
    # 该工件没有发布盖章 → 一致性无法判定，必须是 None 而不是 True
    assert details["content_hash_verified"] is None

    # 盖章与实算一致 → True；不一致（换件/篡改）→ False
    predictor.artifact_metadata["bundle_content_hash"] = details["artifact_content_hash"]
    assert predictor.mode_details()["content_hash_verified"] is True
    predictor.artifact_metadata["bundle_content_hash"] = HASH_B
    assert predictor.mode_details()["content_hash_verified"] is False


def test_predictor_load_hash_changes_when_file_changes(tmp_path: Path) -> None:
    """改一个字节，加载期哈希必须变——这是"文件被换过"的可检测性。"""
    from test_model_inference_safety import _artifact_from_payload, _fit_scaled_model

    from stock_analyzer.models.predictor import SignalPredictor

    artifact_path = tmp_path / "model_v1.json"
    _artifact_from_payload(_fit_scaled_model(seed=37).to_dict(), path=artifact_path)
    before = SignalPredictor.load(artifact_path).mode_details()["artifact_content_hash"]
    artifact_path.write_bytes(artifact_path.read_bytes() + b"\n")
    after = SignalPredictor.load(artifact_path).mode_details()["artifact_content_hash"]
    assert before != after


def test_identity_hash_on_missing_file_does_not_raise(tmp_path: Path) -> None:
    """哈希失败不得让加载路径抛异常（identity 缺失要可观测，但不该拦服务启动）。"""
    with pytest.raises(FileNotFoundError):
        compute_artifact_identity_hash(tmp_path / "nope.json")


# --- 服务层组合 --------------------------------------------------------------


class _PredictorStub:
    def __init__(self, details: dict[str, object]) -> None:
        self._details = details

    def mode_details(self) -> dict[str, object]:
        return dict(self._details)


class _RegistryStub:
    def __init__(
        self,
        record: object | None,
        *,
        error: Exception | None = None,
        registered: list[object] | None = None,
        list_error: Exception | None = None,
    ) -> None:
        self._record = record
        self._error = error
        self._registered = list(registered or [])
        self._list_error = list_error

    def active_champion(self, suppress_read_errors: bool = False) -> object:
        _ = suppress_read_errors
        if self._error is not None:
            raise self._error
        return self._record

    def list_records(
        self, *, limit: int | None = None, suppress_read_errors: bool = False
    ) -> list[object]:
        # 桩必须跟上真实 registry 的接口：2026-09-16 的缺口正是"服务只问 champion、
        # 从不问登记清单"，桩缺 list_records 就会把这个缺口掩盖成"已覆盖"。
        _ = (limit, suppress_read_errors)
        if self._list_error is not None:
            raise self._list_error
        return list(self._registered)


class _RecordStub:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


def _service(predictor: object, registry: object) -> object:
    from stock_analyzer.runtime.service import StockAnalyzerService

    service = StockAnalyzerService.__new__(StockAnalyzerService)
    service._pipeline = type("_Pipeline", (), {"_predictor": predictor})()  # noqa: SLF001
    service._model_registry = registry  # noqa: SLF001
    return service


def test_service_reports_no_champion_without_faking_a_match() -> None:
    service = _service(
        _PredictorStub(
            {"artifact_uri": "/app/artifacts/model_v1.json", "artifact_content_hash": HASH_A}
        ),
        _RegistryStub(None),
    )
    report = service.artifact_identity_report()  # type: ignore[attr-defined]
    assert report["status"] == IDENTITY_NO_CHAMPION
    assert report["loaded_content_hash"] == HASH_A


def test_service_reports_match_and_mismatch() -> None:
    predictor = _PredictorStub(
        {"artifact_uri": "/app/artifacts/model_v1.json", "artifact_content_hash": HASH_A}
    )
    match = _service(
        predictor, _RegistryStub(_RecordStub(model_id="m1", artifact_content_hash=HASH_A))
    )
    assert match.artifact_identity_report()["status"] == IDENTITY_MATCH  # type: ignore[attr-defined]
    mismatch = _service(
        predictor, _RegistryStub(_RecordStub(model_id="m1", artifact_content_hash=HASH_B))
    )
    assert mismatch.artifact_identity_report()["status"] == IDENTITY_MISMATCH  # type: ignore[attr-defined]


def test_service_reports_registry_error_as_unavailable() -> None:
    """注册表读失败必须是 unavailable，不能报成 mismatch（会造成假警报）。"""
    service = _service(
        _PredictorStub({"artifact_uri": "x", "artifact_content_hash": HASH_A}),
        _RegistryStub(None, error=RuntimeError("db locked")),
    )
    report = service.artifact_identity_report()  # type: ignore[attr-defined]
    assert report["status"] == IDENTITY_REGISTRY_UNAVAILABLE
    assert "db locked" in str(report["detail"])


def test_service_sees_registered_copy_when_no_champion() -> None:
    """无 champion 但在服内容 == 某条登记记录：必须报 match_registered。

    2026-09-16 实测缺口：PR #74 只把这条判定加进了 identity.py 与巡检器，服务报告
    没传 ``registered=``，于是 ``/health/deep`` 恒答 no_champion —— 而巡检器的直连
    路径又被学习库写锁挡死（实测 ``Conflicting lock is held``）。两条活路径一起瞎，
    "身份可验证"这半在生产上根本看不见，登记了 challenger 也白登记。
    """
    service = _service(
        _PredictorStub(
            {"artifact_uri": "/app/artifacts/model_v1.json", "artifact_content_hash": HASH_A}
        ),
        _RegistryStub(
            None,
            registered=[
                _RecordStub(
                    model_id="model_v3_deadbeef",
                    artifact_content_hash=HASH_A,
                    lifecycle_state="trained",
                )
            ],
        ),
    )
    report = service.artifact_identity_report()  # type: ignore[attr-defined]
    assert report["status"] == IDENTITY_MATCH_REGISTERED
    assert report["champion_model_id"] == "model_v3_deadbeef"


def test_service_registered_read_failure_does_not_fake_a_verdict() -> None:
    """登记清单读失败只降级那半判定，不得覆盖既有的 champion 结论（也不得报错）。"""
    service = _service(
        _PredictorStub({"artifact_uri": "x", "artifact_content_hash": HASH_A}),
        _RegistryStub(
            _RecordStub(model_id="m1", artifact_content_hash=HASH_A),
            list_error=RuntimeError("registry busy"),
        ),
    )
    report = service.artifact_identity_report()  # type: ignore[attr-defined]
    assert report["status"] == IDENTITY_MATCH
    assert report["champion_model_id"] == "m1"


def test_service_tolerates_predictor_without_mode_details() -> None:
    service = _service(object(), _RegistryStub(None))
    report = service.artifact_identity_report()  # type: ignore[attr-defined]
    assert report["status"] == IDENTITY_LOADED_HASH_MISSING


def test_registry_busy_is_distinct_from_unavailable() -> None:
    """写锁占用（registry_busy）与真读不到（registry_unavailable）必须分开。"""
    busy = describe_artifact_identity(
        loaded_uri="x",
        loaded_hash=HASH_A,
        registry_error="IO Error: Could not set lock",
        registry_busy=True,
    )
    assert busy["status"] == IDENTITY_REGISTRY_BUSY
    assert "锁" in str(busy["detail"])
    unavailable = describe_artifact_identity(
        loaded_uri="x", loaded_hash=HASH_A, registry_error="CatalogException: no such table"
    )
    assert unavailable["status"] == IDENTITY_REGISTRY_UNAVAILABLE


def test_registered_match_without_champion_is_its_own_state() -> None:
    """身份"可验证"与"已批准"必须分开。

    在服工件与某条 **challenger/trained** 记录同哈希、但没有 champion 时，答案是
    "这就是登记过的那份内容、只是没人批准它"，而不是 "无登记身份"——后者会让
    "我到底在跑什么"这个问题继续无解（2026-09-16 定案）。
    """
    result = describe_artifact_identity(
        loaded_uri="/app/artifacts/model_v1.json",
        loaded_hash=HASH_A,
        champion=None,
        registered=[
            {"model_id": "m_other", "artifact_content_hash": HASH_B, "lifecycle_state": "revoked"},
            {"model_id": "m_live", "artifact_content_hash": HASH_A, "lifecycle_state": "trained"},
        ],
    )
    assert result["status"] == IDENTITY_MATCH_REGISTERED
    assert result["champion_model_id"] == "m_live"
    assert "trained" in str(result["detail"])


def test_registered_empty_hash_never_matches() -> None:
    """空哈希不得参与匹配——否则"没有任何哈希的记录"会被当成身份一致。"""
    result = describe_artifact_identity(
        loaded_uri="x",
        loaded_hash=HASH_A,
        champion=None,
        registered=[{"model_id": "m", "artifact_content_hash": "", "lifecycle_state": "trained"}],
    )
    assert result["status"] == IDENTITY_NO_CHAMPION
