"""尾盘模型工件的可加载性验收（改进计划 §3.3 工件 + §4"线上与历史一致"）。

关键不是"能不能存成 JSON"，而是**加载回来的那个东西打分是否与训练时同源**：
这里复用训练时那两个类本身（``LogisticProbModel`` / ``IsotonicCalibrator``），
所以工件侧不存在第二套坐标系；同时缺特征、改权重、换契约都必须硬失败。
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    NET_PROFIT_PROBABILITY_FIELD,
    TrendStrategyContract,
)
from stock_analyzer.labels.tail_net_profit import tail_label_policy_record
from stock_analyzer.models.calibration import IsotonicCalibrator
from stock_analyzer.models.fallback import LogisticProbModel
from stock_analyzer.models.tail_model_artifact import (
    TAIL_ARTIFACT_SCHEMA,
    TailArtifactError,
    _stable_digest,
    load_tail_model_predictor,
    serialize_tail_artifact,
    write_tail_artifact,
)
from stock_analyzer.models.tail_net_profit_trainer import KIND_LIGHTGBM, KIND_LOGISTIC

COMMIT = "cafe1234567890abcdef1234567890abcdef1234"
LABEL_POLICY_ID = tail_label_policy_record(DEFAULT_TREND_CONTRACT).label_policy_id
FEATURES = ("excess_ret_20", "atr14_pct")


def _resigned(payload: dict) -> dict:
    """改完内容再按同一套规则重签：用来单独测语义门，而不是被完整性门先拦住。"""
    body = {key: value for key, value in payload.items() if key != "artifact_digest"}
    payload["artifact_digest"] = _stable_digest(body)
    return payload


def _trained_payload() -> dict:
    matrix = np.asarray([[0.02, 0.03], [0.04, 0.02], [0.06, 0.01], [0.08, 0.05],
                         [0.01, 0.04], [0.05, 0.02], [0.09, 0.03], [0.03, 0.06]],
                        dtype=float)
    vector = np.asarray([0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0], dtype=float)
    model = LogisticProbModel(epochs=80)
    model.fit(matrix, vector)
    calibrator = IsotonicCalibrator()
    calibrator.fit(model.predict_proba(matrix), vector)
    return {
        "model_id": "trend-tail-lr-selfcheck",
        "kind": KIND_LOGISTIC,
        "feature_names": list(FEATURES),
        "training_commit": COMMIT,
        "feature_compute_version": 7,
        "label_policy_id": LABEL_POLICY_ID,
        "probability_field": NET_PROFIT_PROBABILITY_FIELD,
        "contract_digest": DEFAULT_TREND_CONTRACT.digest(),
        "split": {"train_dates": ["2026-09-01"], "test_dates": ["2026-10-01"]},
        "metrics": {"net_profit_rate": 0.62},
        "model": model,
        "calibrator": calibrator,
        "artifact_digest": "trainer-side-digest",
    }


def _artifact_dict(tmp_path: Path) -> dict:
    payload = serialize_tail_artifact(_trained_payload())
    return json.loads(write_tail_artifact(tmp_path / "tail_lr.json", payload).read_text(
        encoding="utf-8"))


def test_round_trip_scores_identically_to_the_trained_objects(tmp_path: Path) -> None:
    trained = _trained_payload()
    predictor = load_tail_model_predictor(
        write_tail_artifact(tmp_path / "tail_lr.json", serialize_tail_artifact(trained)))

    features = {"excess_ret_20": 0.05, "atr14_pct": 0.02}
    expected = float(trained["calibrator"].predict(
        trained["model"].predict_proba(np.asarray([[0.05, 0.02]])))[0])
    assert predictor.probability(features) == pytest.approx(expected)
    assert predictor.feature_names == FEATURES
    assert predictor.contract_digest == DEFAULT_TREND_CONTRACT.digest()
    # 批量入口就是下游 probabilities 参数的形状，键序不影响结果。
    assert predictor.probabilities([("600000.SH", features)]) == {
        "600000.SH": pytest.approx(expected)}


def test_serialize_refuses_unserializable_or_unfitted_inputs(tmp_path: Path) -> None:
    with pytest.raises(TailArtifactError, match="kind"):
        serialize_tail_artifact({**_trained_payload(), "kind": "xgboost"})
    no_scaler = _trained_payload()
    no_scaler["model"].scaler = None
    with pytest.raises(TailArtifactError, match="scaler"):
        serialize_tail_artifact(no_scaler)
    no_calib = _trained_payload()
    no_calib["calibrator"] = IsotonicCalibrator()
    with pytest.raises(TailArtifactError, match="校准器"):
        serialize_tail_artifact(no_calib)
    with pytest.raises(TailArtifactError, match="特征顺序"):
        serialize_tail_artifact({**_trained_payload(), "feature_names": []})
    with pytest.raises(TailArtifactError, match="booster"):
        serialize_tail_artifact({**_trained_payload(), "kind": KIND_LIGHTGBM,
                                 "model": object()})


def test_load_detects_tampering_and_foreign_meaning(tmp_path: Path) -> None:
    artifact = _artifact_dict(tmp_path)
    tampered = json.loads(json.dumps(artifact))
    tampered["model_params"]["weights"] = [9.9, 9.9]
    with pytest.raises(TailArtifactError, match="artifact_digest_mismatch"):
        load_tail_model_predictor(tampered)

    with pytest.raises(TailArtifactError, match="artifact_contract_mismatch"):
        load_tail_model_predictor(artifact,
                                  contract=TrendStrategyContract(take_profit_pct=0.10))
    for key, pattern in (
        (("identity", "label_policy_id"), "label_policy_not_v4"),
        (("identity", "probability_field"), "probability_field_mismatch"),
        (("identity", "training_code_commit"), "training_commit_missing"),
    ):
        broken = json.loads(json.dumps(artifact))
        broken[key[0]][key[1]] = (
            "" if key[1] != "label_policy_id" else "label_policy_v1_x")
        with pytest.raises(TailArtifactError, match=pattern):
            load_tail_model_predictor(_resigned(broken))

    bad_schema = json.loads(json.dumps(artifact))
    bad_schema["schema"] = "tail_model_artifact.v0"
    with pytest.raises(TailArtifactError, match="artifact_schema_mismatch"):
        load_tail_model_predictor(_resigned(bad_schema))
    assert TAIL_ARTIFACT_SCHEMA == "tail_model_artifact.v1"


def test_missing_or_dirty_feature_is_refused_not_zero_filled(tmp_path: Path) -> None:
    predictor = load_tail_model_predictor(
        write_tail_artifact(tmp_path / "tail_lr.json", serialize_tail_artifact(_trained_payload())))
    with pytest.raises(TailArtifactError, match="feature_missing:atr14_pct"):
        predictor.probability({"excess_ret_20": 0.05})
    with pytest.raises(TailArtifactError, match="feature_not_finite"):
        predictor.probability({"excess_ret_20": float("nan"), "atr14_pct": 0.02})
    with pytest.raises(TailArtifactError, match="feature_not_numeric"):
        predictor.probability({"excess_ret_20": "0.05", "atr14_pct": 0.02})


def _load_cli(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name}_cli", Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_freeze_cli():
    return _load_cli("freeze_tail_model_candidate")


def _samples(tmp_path: Path) -> str:
    path = tmp_path / "samples.jsonl"
    path.write_text("\n".join(json.dumps({
        "entry_date": f"2026-09-{day:02d}", "label": day % 2,
        "excess_ret_20": day / 100.0, "atr14_pct": 0.02,
    }) for day in range(1, 9)), encoding="utf-8")
    return str(path)


def test_cli_writes_loadable_artifact_and_manifest(tmp_path: Path, monkeypatch) -> None:
    cli = _load_cli("train_tail_net_profit_model")
    monkeypatch.setattr(cli, "_training_identity", lambda: (COMMIT, "git_checkout", []))
    monkeypatch.setattr(cli, "train_tail_net_profit_model", lambda **_: _trained_payload())
    out = tmp_path / "models" / "tail_lr.json"
    manifest = tmp_path / "tail_model_serving_manifest.json"
    rc = cli.main(["--samples", _samples(tmp_path), "--features", ",".join(FEATURES),
                   "--model-id", "trend-tail-lr-selfcheck", "--out", str(out),
                   "--manifest-out", str(manifest), "--quiet"])
    assert rc == 0

    predictor = load_tail_model_predictor(out)
    assert predictor.identity["training_code_commit"] == COMMIT
    # 清单里的内容哈希必须真是这个工件字节的哈希：两个出口指向同一份东西。
    from stock_analyzer.models.tail_serving_manifest import (
        read_tail_serving_manifest,
        tail_artifact_facts,
    )

    recorded = read_tail_serving_manifest(manifest)
    assert recorded["serving"]["artifact_content_hash"] == tail_artifact_facts(out)[
        "artifact_content_hash"]


def test_cli_exit_codes_are_the_real_ones(tmp_path: Path, monkeypatch) -> None:
    cli = _load_cli("train_tail_net_profit_model")
    monkeypatch.setattr(cli, "_training_identity", lambda: (COMMIT, "git_checkout", []))
    out = tmp_path / "tail_lr.json"

    def stop(**_):
        from stock_analyzer.models.tail_net_profit_trainer import TailTrainingError

        raise TailTrainingError("train split has 3 rows < required 400")

    monkeypatch.setattr(cli, "train_tail_net_profit_model", stop)
    assert cli.main(["--samples", _samples(tmp_path), "--features", ",".join(FEATURES),
                     "--model-id", "m", "--out", str(out), "--quiet"]) == 3
    assert not out.exists()

    monkeypatch.setattr(cli, "_training_identity",
                        lambda: ("unknown", "unknown", ["training_identity_unverified"]))
    assert cli.main(["--samples", _samples(tmp_path), "--features", ",".join(FEATURES),
                     "--model-id", "m", "--out", str(out), "--quiet"]) == 5

    monkeypatch.setattr(cli, "_training_identity", lambda: (COMMIT, "git_checkout", []))
    missing = str(tmp_path / "gone.jsonl")
    assert cli.main(["--samples", missing, "--features", ",".join(FEATURES),
                     "--model-id", "m", "--out", str(out), "--quiet"]) == 3


@pytest.mark.parametrize("script", ("train_tail_net_profit_model", "freeze_tail_model_candidate"))
def test_new_clis_never_resolve_git_head_by_themselves(script: str) -> None:
    """ADR-001 §7.2：新增入口也只能走共享解析器。"""
    source = (Path(__file__).resolve().parents[1] / "scripts" / f"{script}.py").read_text(
        encoding="utf-8")
    assert "rev-parse" not in source
    assert "resolve_runtime_code_identity" in source


def test_train_then_freeze_then_shadow_chain_agrees(tmp_path: Path, monkeypatch) -> None:
    """样本 → 工件 → challenger 清单 → 影子链路绑上同一个身份，中间不换口径。

    这条是 §4"相同输入产生一致判定"在身份层的版本：清单必须是读**分层工件**写出来的，
    而不是靠调用方再传一遍参数猜出来。
    """
    from stock_analyzer.labels.tail_net_profit import register_tail_label_policy
    from stock_analyzer.learning.label_policy_registry import LabelPolicyRegistry
    from stock_analyzer.runtime.services.trend_tail_shadow_service import (
        TrendTailShadowService,
    )
    from tests.test_trend_tail_shadow_runtime import FakeService, _bars, _pool

    train_cli = _load_cli("train_tail_net_profit_model")
    monkeypatch.setattr(train_cli, "_training_identity", lambda: (COMMIT, "git_checkout", []))
    monkeypatch.setattr(train_cli, "train_tail_net_profit_model", lambda **_: _trained_payload())
    artifact = tmp_path / "models" / "tail_lr.json"
    manifest = tmp_path / "tail_model_serving_manifest.json"
    assert train_cli.main(["--samples", _samples(tmp_path), "--features", ",".join(FEATURES),
                           "--model-id", "trend-tail-lr-selfcheck", "--out", str(artifact),
                           "--manifest-out", str(manifest), "--quiet"]) == 0

    # 冻结 CLI 只读工件，不许有人再把 label 口径重新说一遍。
    freeze_cli = _load_freeze_cli()
    monkeypatch.setattr(freeze_cli, "_resolve_training_commit",
                        lambda **_: (COMMIT, "git_checkout", []))
    registry_db = tmp_path / "learning_protocol.duckdb"
    register_tail_label_policy(LabelPolicyRegistry(registry_db))
    assert freeze_cli.main(["--artifact", str(artifact), "--registry-db", str(registry_db),
                            "--out", str(manifest), "--quiet"]) == 0

    service = FakeService(manifest=None, tmp_path=tmp_path, tail_manifest_path=str(manifest))
    # ADR-001：训练 commit 与运行 commit 是两个事实，但必须相等，否则身份不成立。
    service._runtime_code_commit = lambda: COMMIT
    report = TrendTailShadowService(service, report_dir=tmp_path / "shadow").run(
        timestamp=datetime(2026, 10, 9, 14, 45, 0), watch_pool=_pool(["600000.SH"]),
        minute_bars={"600000.SH": _bars()}, probabilities={"600000.SH": 0.9},
    )
    assert report["blocking_reason"] in (None, "")
    assert report["model_identity"]["training_manifest_id"] == ""
    assert report["final_symbols"] == ["600000.SH"]


def test_shadow_service_scores_the_pool_from_the_challenger_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    """没人生成 p_net_profit_5d_tail 时，由已核验的工件自己算 —— 缺特征的不补名额。

    这条是 §3.4"统一按新概率排序 + 数据不足就 0 只"的落地证据：分数必须与
    独立加载同一工件算出来的值逐位一致，而不是留档里另一个数。
    """
    from stock_analyzer.labels.tail_net_profit import register_tail_label_policy
    from stock_analyzer.learning.label_policy_registry import LabelPolicyRegistry
    from stock_analyzer.runtime.services.trend_tail_shadow_service import (
        TrendTailShadowService,
    )
    from tests.test_trend_tail_shadow_runtime import FakeService, _bars

    train_cli = _load_cli("train_tail_net_profit_model")
    monkeypatch.setattr(train_cli, "_training_identity", lambda: (COMMIT, "git_checkout", []))
    monkeypatch.setattr(train_cli, "train_tail_net_profit_model", lambda **_: _trained_payload())
    artifact = tmp_path / "models" / "tail_lr.json"
    manifest = tmp_path / "tail_model_serving_manifest.json"
    assert train_cli.main(["--samples", _samples(tmp_path), "--features", ",".join(FEATURES),
                           "--model-id", "trend-tail-lr-selfcheck", "--out", str(artifact),
                           "--manifest-out", str(manifest), "--quiet"]) == 0

    predictor = load_tail_model_predictor(artifact)
    strong = {"excess_ret_20": 0.09, "atr14_pct": 0.03}
    weak = {"excess_ret_20": 0.01, "atr14_pct": 0.06}
    assert predictor.probability(strong) > predictor.probability(weak)

    registry_db = tmp_path / "learning_protocol.duckdb"
    register_tail_label_policy(LabelPolicyRegistry(registry_db))
    service = FakeService(manifest=None, tmp_path=tmp_path, tail_manifest_path=str(manifest))
    service._runtime_code_commit = lambda: COMMIT
    pool = [
        {"symbol": "600111.SH", "risk_state": "", "features": strong},
        {"symbol": "600222.SH", "risk_state": "", "features": weak},
        # 缺 atr14_pct：不许当成 0 分参与排序，必须点名。
        {"symbol": "600333.SH", "risk_state": "", "features": {"excess_ret_20": 0.09}},
    ]
    report = TrendTailShadowService(service, report_dir=tmp_path / "shadow").run(
        timestamp=datetime(2026, 10, 9, 14, 45, 0), watch_pool=pool,
        minute_bars={row["symbol"]: _bars() for row in pool},
    )
    identity = report["model_identity"]
    assert identity["probability_source"] == "challenger_artifact"
    assert any(item.startswith("probability_scoring_failed:600333.SH:feature_missing")
               for item in identity["recording_failures"])
    # 只有过阈值（0.60）的才会被推荐；断言用的是同一把尺子，不是写死的期望。
    assert report["final_symbols"] == ([
        "600111.SH"] if predictor.probability(strong) >= DEFAULT_TREND_CONTRACT
        .min_net_profit_probability else [])
    assert "600333.SH" not in report["final_symbols"]


def test_shadow_service_without_challenger_artifact_does_not_invent_scores(
    tmp_path: Path,
) -> None:
    """没有清单就是不打分：0 只 + 点名，而不是拿旧综合分冒充净盈利概率。"""
    from stock_analyzer.runtime.services.trend_tail_shadow_service import (
        TrendTailShadowService,
    )
    from tests.test_trend_tail_shadow_runtime import FakeService, _bars

    service = FakeService(manifest=None, tmp_path=tmp_path)
    report = TrendTailShadowService(service, report_dir=tmp_path / "shadow").run(
        timestamp=datetime(2026, 10, 9, 14, 45, 0),
        watch_pool=[{"symbol": "600111.SH", "risk_state": "", "features": {}}],
        minute_bars={"600111.SH": _bars()},
    )
    assert report["final_symbols"] == []
    assert report["model_identity"]["probability_source"] == "none"
    assert "challenger_artifact_not_bound" in report["model_identity"]["recording_failures"]
