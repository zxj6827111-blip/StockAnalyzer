"""尾盘 challenger 在服清单验收（改进计划 §3.3 身份失败即停 + §3.4 只生成 challenger）。

钉住的是"身份能不能被第三方复核"，不是"清单好不好看"：内容哈希必须来自**盘上文件**，
复核必须重新哈希一次；工件被换过、状态不是 challenger、口径不是 v4 净盈利标签、
契约摘要对不上 —— 任何一条都得变成可见的失败原因，而不是"清单里有 id 就算绑上了"。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    NET_PROFIT_PROBABILITY_FIELD,
    TrendStrategyContract,
)
from stock_analyzer.labels.tail_net_profit import tail_label_policy_record
from stock_analyzer.models.tail_serving_manifest import (
    TAIL_SERVING_MANIFEST_SCHEMA,
    TAIL_STATUS_CHALLENGER,
    TailManifestError,
    build_tail_serving_manifest,
    read_tail_serving_manifest,
    tail_artifact_facts,
    verify_tail_serving_manifest,
    write_tail_serving_manifest,
)

COMMIT = "cafe1234567890abcdef1234567890abcdef1234"
LABEL_POLICY_ID = tail_label_policy_record(DEFAULT_TREND_CONTRACT).label_policy_id


def _artifact(tmp_path: Path, **overrides) -> Path:
    payload = {
        "model_id": "trend-tail-lgbm-2026q4",
        "label_policy_id": LABEL_POLICY_ID,
        "contract_digest": DEFAULT_TREND_CONTRACT.digest(),
        "probability_field": NET_PROFIT_PROBABILITY_FIELD,
        "feature_compute_version": 7,
        "feature_names": ["excess_ret_20", "atr14_pct"],
        "artifact_digest": "digest-from-trainer",
        "split": {"train_days": 40, "test_days": 10},
        "metrics": {"net_profit_rate": 0.58},
    }
    payload.update(overrides)
    path = tmp_path / "tail_lr_v1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _manifest(tmp_path: Path, **overrides) -> dict:
    artifact_path = overrides.pop("artifact", None) or _artifact(tmp_path)
    kwargs = {
        "artifact_path": artifact_path,
        "model_id": "trend-tail-lgbm-2026q4",
        "training_code_commit": COMMIT,
        "training_manifest_id": "dataset_manifest_v9",
        "artifact": json.loads(Path(artifact_path).read_text(encoding="utf-8")),
    }
    kwargs.update(overrides)
    return build_tail_serving_manifest(**kwargs)


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "freeze_tail_model_candidate",
        Path(__file__).resolve().parents[1] / "scripts" / "freeze_tail_model_candidate.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 构造：身份不可证就拒写，不写"以后补"
# ---------------------------------------------------------------------------


def test_manifest_facts_come_from_the_file_not_from_the_caller(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    manifest = _manifest(tmp_path)
    facts = tail_artifact_facts(artifact)
    assert manifest["serving"]["artifact_content_hash"] == facts["artifact_content_hash"]
    assert manifest["serving"]["artifact_content_hash"].startswith("sha256:")
    # 训练器自己的摘要与文件内容哈希是两个独立事实，都留档。
    assert manifest["serving"]["artifact_internal_digest"] == "digest-from-trainer"
    assert manifest["status"] == TAIL_STATUS_CHALLENGER
    assert manifest["schema"] == TAIL_SERVING_MANIFEST_SCHEMA


def test_build_refuses_unverifiable_identity_inputs(tmp_path: Path) -> None:
    with pytest.raises(TailManifestError, match="training_code_commit"):
        _manifest(tmp_path, training_code_commit="")
    with pytest.raises(TailManifestError, match="工件不存在"):
        _manifest(tmp_path, artifact_path=tmp_path / "gone.json")
    with pytest.raises(TailManifestError, match="challenger"):
        _manifest(tmp_path, status="champion")
    with pytest.raises(TailManifestError, match="label_policy_v4"):
        _manifest(tmp_path, artifact=_artifact(tmp_path, label_policy_id="label_policy_v2_soup"))
    # 工件是按另一套 TP/SL 训练的，就不是这条链路的模型。
    other = TrendStrategyContract(take_profit_pct=0.10)
    with pytest.raises(TailManifestError, match="另一套契约"):
        _manifest(tmp_path, artifact=_artifact(tmp_path, contract_digest=other.digest()))


# ---------------------------------------------------------------------------
# 复核：每一种失败都要点名，且都不抛异常
# ---------------------------------------------------------------------------


def test_verify_rerehashes_the_artifact(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    manifest = _manifest(tmp_path, artifact_path=artifact)
    checked, failures = verify_tail_serving_manifest(manifest)
    assert checked is not None and failures == ()

    artifact.write_text('{"tampered": true}', encoding="utf-8")
    _, failures = verify_tail_serving_manifest(manifest)
    assert any(item.startswith("tail_serving_manifest_artifact_content_hash_mismatch")
               for item in failures)


def test_verify_names_every_kind_of_identity_break(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    cases = {
        "schema": ({"schema": "tail_model_serving_manifest.v0"}, "schema_mismatch"),
        "status": ({"status": "champion"}, "status_not_challenger"),
        "model_id": ({"identity": {"training_code_commit": COMMIT}}, "model_id_missing"),
        "commit": ({"identity": {"model_id": "m", "training_code_commit": "unknown"}},
                   "training_commit_unverified"),
        "label": ({"label": {"label_policy_id": "label_policy_v1_x"}}, "label_policy_not_v4"),
        "probability": ({"serving": {"probability_field": "p_meta"}},
                        "probability_field_mismatch"),
    }
    for label, (override, expected) in cases.items():
        payload = json.loads(json.dumps(manifest))
        if label in {"schema", "status"}:
            payload.update(override)
        else:
            section = next(iter(override))
            payload[section] = override[section]
        _, failures = verify_tail_serving_manifest(payload, recheck_artifact=False)
        assert any(expected in item for item in failures), (label, failures)

    other = TrendStrategyContract(take_profit_pct=0.10)
    _, failures = verify_tail_serving_manifest(manifest, contract=other, recheck_artifact=False)
    assert any("contract_digest_mismatch" in item for item in failures)
    assert verify_tail_serving_manifest({}, recheck_artifact=False)[1] == (
        "tail_serving_manifest_empty",)


def test_round_trip_read_and_write(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    path = write_tail_serving_manifest(tmp_path / "nested" / "m.json", manifest)
    assert read_tail_serving_manifest(path) == manifest
    assert read_tail_serving_manifest(tmp_path / "absent.json") == {}
    path.write_text("{not json", encoding="utf-8")
    assert read_tail_serving_manifest(path) == {}


# ---------------------------------------------------------------------------
# CLI：退出码是真实退出码，且不许自己解析 git
# ---------------------------------------------------------------------------


def test_cli_never_resolves_git_head_by_itself() -> None:
    """ADR-001 §7.2：任何 CLI 不得自行 ``git rev-parse``，训练身份走共享 resolver。"""
    source = (Path(__file__).resolve().parents[1] / "scripts"
              / "freeze_tail_model_candidate.py").read_text(encoding="utf-8")
    assert "rev-parse" not in source
    assert "resolve_runtime_code_identity" in source


def test_cli_stops_before_writing_when_facts_are_wrong(tmp_path: Path, monkeypatch) -> None:
    cli = _load_cli()
    monkeypatch.setattr(cli, "_resolve_training_commit",
                        lambda **_: (COMMIT, "git_checkout", []))
    artifact = _artifact(tmp_path)
    out = tmp_path / "manifest.json"

    # 没有 --registry-db 时只比字段；给了就必须查到这条口径。
    empty_registry = tmp_path / "learning_protocol.duckdb"
    assert cli.main(["--artifact", str(artifact), "--registry-db", str(empty_registry),
                     "--out", str(out), "--quiet"]) == 3
    assert not out.exists()

    registered = tmp_path / "registered.duckdb"
    from stock_analyzer.labels.tail_net_profit import register_tail_label_policy
    from stock_analyzer.learning.label_policy_registry import LabelPolicyRegistry

    register_tail_label_policy(LabelPolicyRegistry(registered))
    assert cli.main(["--artifact", str(artifact), "--registry-db", str(registered),
                     "--out", str(out), "--quiet"]) == 0
    assert read_tail_serving_manifest(out) != {}

    mismatched = _artifact(tmp_path, contract_digest="0" * 16)
    mismatched_path = tmp_path / "other.json"
    mismatched_path.write_bytes(mismatched.read_bytes())
    assert cli.main(["--artifact", str(mismatched_path), "--out", str(out), "--quiet"]) == 4

    assert cli.main(["--artifact", str(tmp_path / "gone.json"),
                     "--out", str(out), "--quiet"]) == 5


def test_cli_stops_when_training_identity_is_unverifiable(tmp_path: Path, monkeypatch) -> None:
    cli = _load_cli()
    monkeypatch.setattr(
        cli, "_resolve_training_commit",
        lambda **_: ("unknown", "unknown", ["training_identity_unverified:unknown"]),
    )
    out = tmp_path / "manifest.json"
    assert cli.main(["--artifact", str(_artifact(tmp_path)), "--out", str(out),
                     "--quiet"]) == 5
    assert not out.exists()
