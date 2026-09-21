"""R4.1 定向测试：冻结模型训练身份绑定（model training commit == runtime commit）。

被钉住的契约（R4.1）：

```text
runtime code_commit == frozen model training code_commit
```

- 训练身份的唯一权威字段 = 冻结模型工件 manifest 的 ``code_commit``（由
  ``alpha_v2_shadow_model_freeze.py`` 在训练时从统一 Runtime Identity Resolver 取得）；
- validation freeze 把它传播成 ``model.model_training_code_commit``，受
  ``freeze_manifest_hash`` 覆盖；
- 生产模式下 missing / unknown / 非法 / 与运行身份不一致 → 在**开 epoch 之前** exit 5；
- 工件哈希覆盖该字段 → 事后改写训练身份会破坏工件完整性（不再 rc=0）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from _alpha_v2_m3_fixtures import capture_at, shadow_row_identity

from stock_analyzer.alpha_v2.research.multi_head import HeadFitSpec
from stock_analyzer.alpha_v2.validation.epoch import epoch_identity_matches, open_epoch
from stock_analyzer.alpha_v2.validation.freeze import (
    build_validation_freeze,
    freeze_manifest_hash,
    verify_freeze_against_runtime,
    verify_freeze_integrity,
    write_validation_freeze,
)
from stock_analyzer.alpha_v2.validation.freeze_precheck import (
    FreezeGateError,
    assert_model_training_commit,
)
from stock_analyzer.alpha_v2.validation.frozen_model import (
    FrozenModelError,
    fit_frozen_model,
    frozen_model_identity_payload,
    load_frozen_model,
    persist_frozen_model,
)
from stock_analyzer.alpha_v2.validation.shadow_capture import (
    build_shadow_rows,
    read_shadow_rows,
    write_shadow_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

A = "a1a2a3a4" * 5
B = "b1b2b3b4" * 5
EPOCH_ID = "alpha_v2_epoch_901"
SAFE_FEATURES = ["ret_1d", "ret_5d", "ma5", "ma20", "volume_ratio_5", "turnover_zscore20"]


def _matrix(rows_train: int = 160, rows_cal: int = 80) -> pd.DataFrame:
    """合成训练矩阵（配方与 M3 冻结模型测试同源：可训练、确定、不依赖外部数据）。"""
    rng = np.random.default_rng(7)
    total = rows_train + rows_cal
    days = np.repeat(np.arange(total // 20), 20)[:total]
    days = np.asarray([f"2026-07-{int(d) + 1:02d}" for d in days])
    frame = pd.DataFrame(
        {
            "decision_date": days,
            "symbol": [f"6000{index % 20:02d}" for index in range(total)],
            **{name: rng.normal(0.0, 1.0, total) for name in SAFE_FEATURES},
        }
    )
    base = 0.3 * frame["ret_1d"] + 0.2 * frame["ma5"] - 0.1 * frame["volume_ratio_5"]
    for horizon in (3, 5, 10, 15):
        frame[f"net_return_{horizon}d"] = base * (horizon / 5.0) + rng.normal(0.0, 0.01, total)
        frame[f"excess_return_{horizon}d"] = frame[f"net_return_{horizon}d"] - 0.001
        frame[f"mae_{horizon}d"] = -np.abs(frame[f"net_return_{horizon}d"]) * 0.6
        frame[f"up_net_{horizon}d"] = (frame[f"net_return_{horizon}d"] > 0).astype(float)
        frame[f"up_excess_{horizon}d"] = (frame[f"excess_return_{horizon}d"] > 0).astype(float)
        frame[f"mae_le_5pct_{horizon}d"] = (frame[f"mae_{horizon}d"] <= -0.05).astype(float)
    frame["alpha_target_5d"] = frame.groupby(frame["decision_date"])["excess_return_5d"].rank(
        pct=True
    )
    frame["is_train"] = False
    frame["is_calibration"] = False
    frame.loc[: rows_train - 1, "is_train"] = True
    frame.loc[rows_train : rows_train + rows_cal - 1, "is_calibration"] = True
    return frame


def _artifact(tmp_path: Path, *, commit: str = A, model_id: str = "epoch_test") -> Path:
    """落一份**真**冻结模型工件（真 artifact_hash），训练身份 = ``commit``。"""
    model = fit_frozen_model(
        frame=_matrix(),
        model_id=model_id,
        spec=HeadFitSpec(min_train_rows=20, min_class_balance=0.05),
        # M4-L / R1：provenance 必须带训练窗与训练数据指纹——production freeze 的
        # preflight 硬门会与报告逐项对账（gate 做一致性比对；重算链在 preflight 测试）。
        provenance={
            "source": "r41_test",
            "window": ["2026-05-01", "2026-06-30"],
            "training_data_fingerprint": "r41-fixture-fingerprint",
        },
        extra_identity={
            "code_commit": commit,
            "identity_source": "container_build_identity",
            "code_commit_source": "container_build_identity",
        },
    )
    return persist_frozen_model(model, tmp_path / "artifacts")


def _manifest_for(artifact: Path, *, commit: str = A, mode: str = "test") -> dict[str, object]:
    manifest = build_validation_freeze(
        validation_epoch_id=EPOCH_ID,
        code_commit=commit,
        git_branch="test",
        config_hash="cfg",
        config_hash_scope="test",
        model=frozen_model_identity_payload(artifact),
        feature_columns=SAFE_FEATURES,
        feature_group_ids=["price_volume_technical"],
        selection_contract={"selection_contract_id": "night_alpha_v2_v1"},
        execution_price_mode="raw",
        feature_price_mode="qfq",
        validation_start_date="2026-09-01",
        created_at="2026-09-20T00:00:00+08:00",
        validation_mode=mode,
        deterministic_clock=True,
        deterministic_clock_source="r41_test",
    )
    manifest["freeze_manifest_hash"] = freeze_manifest_hash(manifest)
    return manifest


def _patch_artifact_commit(artifact: Path, value: object) -> None:
    path = artifact / "model_manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if value is None:
        payload.pop("code_commit", None)
    else:
        payload["code_commit"] = value
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Case 1：train=A / runtime=A → PASS
# ---------------------------------------------------------------------------


def test_case1_same_training_and_runtime_commit_passes(tmp_path: Path):
    artifact = _artifact(tmp_path, commit=A)
    payload = frozen_model_identity_payload(artifact)
    assert payload["model_training_code_commit"] == A
    assert_model_training_commit(model_training_code_commit=A, runtime_code_commit=A)
    manifest = _manifest_for(artifact, commit=A)
    assert manifest["model"]["model_training_code_commit"] == A
    assert verify_freeze_integrity(manifest) is True
    assert verify_freeze_against_runtime(
        manifest, code_commit=A, model_training_code_commit=A
    ) == []


# ---------------------------------------------------------------------------
# Case 2：train=A / runtime=B → 开 epoch 之前 FAIL，epoch 不得 open
# ---------------------------------------------------------------------------


def test_case2_training_a_runtime_b_rejected_by_gate():
    with pytest.raises(FreezeGateError) as excinfo:
        assert_model_training_commit(model_training_code_commit=A, runtime_code_commit=B)
    assert excinfo.value.exit_code == 5
    assert "模型训练身份" in str(excinfo.value)


def _write_preflight_report(
    tmp_path: Path, *, artifact: Path, commit: str = A
) -> Path:
    """写一份与工件身份/训练窗/指纹绑定的 PASS preflight（M4-L R1 硬门输入）。"""
    from stock_analyzer.alpha_v2.validation.frozen_model import (
        frozen_model_identity_payload,
    )
    from stock_analyzer.alpha_v2.validation.preflight import (
        PREFLIGHT_SCHEMA,
        VERDICT_PASS,
        preflight_hash_of,
    )

    model_identity = dict(frozen_model_identity_payload(artifact))
    payload: dict[str, object] = {
        "schema": PREFLIGHT_SCHEMA,
        "generated_at": __import__("datetime").datetime.now().astimezone().isoformat(),
        "verdict": VERDICT_PASS,
        "blocking_findings": [],
        "warnings": [],
        "facts": {},
        "runtime_identity": {"code_commit": commit},
        # R1：gate 会逐项比对 model_identity（id/artifact hash/schema/training commit/
        # 指纹）与"即将冻结的模型块"——本夹具只证明**一致性比对**本身有效；
        # "重算指纹并比对数据"由 preflight 自身测试与 R1 端到端测试覆盖。
        "model_identity": {
            "model_id": str(model_identity.get("model_id", "")),
            "model_artifact_hash": str(model_identity.get("artifact_hash", "")),
            "feature_schema_hash": str(model_identity.get("feature_schema_hash", "")),
            "model_training_code_commit": str(
                model_identity.get("model_training_code_commit", "")
            ),
            "training_data_fingerprint": str(
                dict(model_identity.get("provenance", {}) or {}).get(
                    "training_data_fingerprint", ""
                )
            ),
        },
        "data_identity": {"market_db": "synthetic"},
        "training_window": {"start": "2026-05-01", "end": "2026-06-30"},
        "checks": [],
    }
    payload["preflight_hash"] = preflight_hash_of(payload)
    path = tmp_path / "preflight.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run_freeze_cli(
    sandbox: Path,
    *,
    model_dir: Path,
    out: Path,
    start_date: str,
    preflight_report: Path | None = None,
) -> object:
    keep = [
        part
        for part in os.environ.get("PATH", "").split(os.pathsep)
        if part and not ((Path(part) / "git.exe").exists() or (Path(part) / "git").exists())
    ]
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(keep)
    env["SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE"] = "raw"
    return subprocess.run(
        [
            sys.executable,
            str(sandbox / "scripts" / "alpha_v2_validation_freeze.py"),
            "--epoch-id",
            EPOCH_ID,
            "--out",
            str(out),
            "--model-dir",
            str(model_dir),
            "--start-date",
            start_date,
            "--open-epoch",
            *(
                ["--preflight-report", str(preflight_report)]
                if preflight_report is not None
                else []
            ),
        ],
        cwd=str(sandbox),
        capture_output=True,
        text=True,
        env=env,
        timeout=1200,
    )


def _sandbox(tmp_path: Path, commit: str) -> tuple[Path, object]:
    """无 git 容器形态沙箱：代码树副本 + 构建身份两文件（CLI 的 REPO_ROOT 即沙箱根）。

    返回 ``(sandbox, set_identity)``——``set_identity`` 用来在同一沙箱上切换运行身份
    （避免为第二个用例再复制一遍代码树）。
    """
    sandbox = tmp_path / "sandbox"
    for name in ("src", "scripts", "config"):
        shutil.copytree(
            REPO_ROOT / name,
            sandbox / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
        )

    def _set_identity(value: str) -> None:
        (sandbox / ".build_commit").write_text(f"{value}\n", encoding="utf-8")
        (sandbox / "build_manifest.json").write_text(
            json.dumps(
                {
                    "commit": value,
                    "short_commit": value[:12],
                    "dirty": False,
                    "built_at_utc": "2026-09-20T00:00:00Z",
                    "config_schema": "stock-analyzer-config.v1",
                    "runtime_state_schema": 9,
                }
            ),
            encoding="utf-8",
        )

    _set_identity(commit)
    return sandbox, _set_identity


def test_case2_freeze_cli_rejects_and_does_not_open_epoch(tmp_path: Path):
    """端到端：同一份工件，运行身份 A → 通过；运行身份 B → exit 5 且不落盘、不 open。"""
    artifact = _artifact(tmp_path, commit=A)
    sandbox, set_identity = _sandbox(tmp_path, A)
    today = date.today().isoformat()

    preflight = _write_preflight_report(tmp_path, artifact=artifact, commit=A)
    ok_out = tmp_path / "out_ok"
    ok_out.mkdir()
    good = _run_freeze_cli(
        sandbox, model_dir=artifact, out=ok_out, start_date=today, preflight_report=preflight
    )
    assert good.returncode == 0, good.stderr[-800:]
    assert (ok_out / "validation" / "validation_freeze_manifest.json").exists()
    epochs_ok = json.loads((ok_out / "validation" / "epochs.json").read_text(encoding="utf-8"))
    assert [e["epoch_id"] for e in epochs_ok["epochs"]] == [EPOCH_ID]

    set_identity(B)
    bad_out = tmp_path / "out_bad"
    bad_out.mkdir()
    bad = _run_freeze_cli(
        sandbox, model_dir=artifact, out=bad_out, start_date=today, preflight_report=preflight
    )
    assert bad.returncode == 5, (bad.returncode, bad.stdout[-500:], bad.stderr[-800:])
    assert "模型训练身份" in bad.stderr
    assert not (bad_out / "validation" / "validation_freeze_manifest.json").exists()
    assert not (bad_out / "validation" / "epochs.json").exists()


# ---------------------------------------------------------------------------
# P2（独立审稿 → 本批修复）：生产 freeze 必须同时校验工件**内容完整性**，
# 不能只读身份字段——身份可证但内容被改写的工件应当在冻结阶段就被拒。
# ---------------------------------------------------------------------------


def test_freeze_rejects_artifact_with_rewritten_identity_stale_hash(tmp_path: Path):
    """工件 manifest 的身份被改写成与运行一致、但哈希留旧不自洽 → 生产 freeze FAIL。

    复刻审稿 Attack Y：B 训的工件把 ``code_commit`` 改成 A 以冒充"同代代码"——
    R4.1 训练身份门读的是身份字段（会通过），R4.1.1 内容校验必须拦下它，
    且不落盘、不开 epoch。
    """
    artifact = _artifact(tmp_path, commit=B)
    _patch_artifact_commit(artifact, A)  # 只改身份，不重算哈希 → 内容不再自洽
    sandbox, _ = _sandbox(tmp_path, A)
    out = tmp_path / "out_stale"
    out.mkdir()
    today = date.today().isoformat()
    result = _run_freeze_cli(sandbox, model_dir=artifact, out=out, start_date=today)
    assert result.returncode == 5, (result.returncode, result.stdout[-500:], result.stderr[-800:])
    assert "完整性" in result.stderr
    assert not (out / "validation" / "validation_freeze_manifest.json").exists()
    assert not (out / "validation" / "epochs.json").exists()


def test_freeze_rejects_artifact_with_corrupted_booster(tmp_path: Path):
    """工件 booster 文件被改动（身份字段原样）→ 生产 freeze FAIL（逐文件 sha256）。

    身份完全合法的工件，内容被剪断一个字节就能被发现——证明完整性校验看的是
    **文件内容**，不是 manifest 自述。
    """
    artifact = _artifact(tmp_path, commit=A)
    booster_files = sorted(artifact.glob("booster__*.txt"))
    assert booster_files, f"工件里应至少有一个 booster 文件: {artifact}"
    target = booster_files[0]
    target.write_bytes(b"corrupted-prefix\n" + target.read_bytes())
    sandbox, _ = _sandbox(tmp_path, A)
    out = tmp_path / "out_corrupt"
    out.mkdir()
    today = date.today().isoformat()
    result = _run_freeze_cli(sandbox, model_dir=artifact, out=out, start_date=today)
    assert result.returncode == 5, (result.returncode, result.stdout[-500:], result.stderr[-800:])
    assert "完整性" in result.stderr
    assert not (out / "validation" / "validation_freeze_manifest.json").exists()
    assert not (out / "validation" / "epochs.json").exists()


# ---------------------------------------------------------------------------
# Case 3/4/5：训练身份 missing / unknown / malformed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "label"),
    [
        (None, "missing"),
        ("unknown", "unknown"),
        ("", "empty"),
        ("zzzz" + "z" * 36, "malformed"),
        ("abc123", "too-short"),
    ],
)
def test_case345_training_commit_not_provable_is_rejected(tmp_path: Path, value, label):
    artifact = _artifact(tmp_path, commit=A)
    _patch_artifact_commit(artifact, value)
    payload = frozen_model_identity_payload(artifact)
    raw = str(payload["model_training_code_commit"])
    assert raw != A, label
    with pytest.raises(FreezeGateError) as excinfo:
        assert_model_training_commit(model_training_code_commit=raw, runtime_code_commit=A)
    assert excinfo.value.exit_code == 5, label
    # 清单里如实落这个（不可证）值——绝不回退成运行 commit
    manifest = _manifest_for(artifact, commit=A)
    assert manifest["model"]["model_training_code_commit"] == raw
    assert manifest["model"]["model_training_code_commit"] != manifest["code_commit"]


def test_missing_training_commit_is_not_backfilled_from_runtime(tmp_path: Path):
    """缺失绝不允许被回退成 runtime commit（那正是漏洞的形态）。"""
    artifact = _artifact(tmp_path, commit=A)
    _patch_artifact_commit(artifact, None)
    payload = frozen_model_identity_payload(artifact)
    manifest = _manifest_for(artifact, commit=A)
    assert payload["model_training_code_commit"] == ""
    assert manifest["model"]["model_training_code_commit"] == ""
    assert manifest["model"]["model_training_code_commit"] != manifest["code_commit"]
    with pytest.raises(FreezeGateError):
        assert_model_training_commit(
            model_training_code_commit=manifest["model"]["model_training_code_commit"],
            runtime_code_commit=manifest["code_commit"],
        )


# ---------------------------------------------------------------------------
# Case 6a：冻结清单里的训练身份受 freeze_manifest_hash 覆盖
# ---------------------------------------------------------------------------


def test_case6a_freeze_manifest_training_identity_is_hash_covered(tmp_path: Path):
    artifact = _artifact(tmp_path, commit=A)
    manifest = _manifest_for(artifact, commit=A)
    assert verify_freeze_integrity(manifest) is True
    write_validation_freeze(manifest, root=tmp_path)
    stored = json.loads(
        (tmp_path / "validation" / "validation_freeze_manifest.json").read_text(encoding="utf-8")
    )
    assert stored["model"]["model_training_code_commit"] == A
    tampered = json.loads(json.dumps(stored))
    tampered["model"]["model_training_code_commit"] = B
    assert verify_freeze_integrity(tampered) is False
    assert any(
        "freeze_manifest_hash" in item
        for item in verify_freeze_against_runtime(tampered, code_commit=A)
    )


# ---------------------------------------------------------------------------
# Case 6b：工件里的训练身份被改写 → 工件完整性失败
# ---------------------------------------------------------------------------


def test_case6b_artifact_training_identity_tamper_breaks_integrity(tmp_path: Path):
    artifact = _artifact(tmp_path, commit=A)
    load_frozen_model(artifact)  # 未篡改时可加载
    _patch_artifact_commit(artifact, B)
    with pytest.raises(FrozenModelError) as excinfo:
        load_frozen_model(artifact)
    assert "artifact_hash" in str(excinfo.value)


def test_case6b_artifact_reload_after_retrain_is_consistent(tmp_path: Path):
    """反向对照：重新训练（同一身份）产出的工件必须可加载——哈希覆盖不会误伤正常产物。"""
    first = _artifact(tmp_path / "one", commit=A)
    second = _artifact(tmp_path / "two", commit=A)
    assert load_frozen_model(first).manifest["artifact_hash"] == load_frozen_model(
        second
    ).manifest["artifact_hash"]
    differing = _artifact(tmp_path / "three", commit=B)
    assert load_frozen_model(differing).manifest["artifact_hash"] != load_frozen_model(
        first
    ).manifest["artifact_hash"]


# ---------------------------------------------------------------------------
# 正例身份链
# ---------------------------------------------------------------------------


def test_positive_identity_chain_all_equal(tmp_path: Path):
    artifact = _artifact(tmp_path, commit=A)
    day = date(2026, 9, 1)
    manifest = _manifest_for(artifact, commit=A)
    write_validation_freeze(manifest, root=tmp_path)
    epoch = open_epoch(
        root=tmp_path,
        epoch_id=EPOCH_ID,
        freeze_manifest_hash=str(manifest["freeze_manifest_hash"]),
        identity={
            "code_commit": manifest["code_commit"],
            "config_hash": manifest["config_hash"],
            "model_id": manifest["model"]["model_id"],
            "model_artifact_hash": manifest["model"]["artifact_hash"],
            "model_training_code_commit": manifest["model"]["model_training_code_commit"],
            "feature_schema_hash": manifest["feature_schema_hash"],
            "label_policy_hash": manifest["label_policy_hash"],
            "selection_contract_id": manifest["selection_contract_id"],
            "execution_price_mode": manifest["execution_price_mode"],
        },
        opened_on_date=day.isoformat(),
    )
    with capture_at(day):
        rows = build_shadow_rows(
            signal_date=day,
            signal_time="15:35",
            epoch=epoch,
            candidates=[{"symbol": "600000", "alpha_rank": 0.9}],
            identity=shadow_row_identity(epoch),
        )
        write_shadow_snapshot(root=tmp_path, epoch=epoch, signal_date=day, rows=rows)
    stored = read_shadow_rows(tmp_path, epoch.epoch_id, day)[0]

    chain = {
        "model training": frozen_model_identity_payload(artifact)["model_training_code_commit"],
        "freeze runtime": manifest["code_commit"],
        "freeze model training": manifest["model"]["model_training_code_commit"],
        "epoch": epoch.identity["code_commit"],
        "epoch model training": epoch.identity["model_training_code_commit"],
        "capture": stored["code_commit"],
        # mature 形态：运行身份 A 通过绑定门 + epoch 身份门（成熟产物本身不存 code_commit）
        "mature runtime": A,
    }
    assert set(chain.values()) == {A}, chain
    assert epoch_identity_matches(epoch, {"code_commit": A}, keys=("code_commit",)) == []
    assert_model_training_commit(model_training_code_commit=A, runtime_code_commit=A)
    assert verify_freeze_against_runtime(
        manifest, code_commit=A, model_training_code_commit=A
    ) == []


def test_binding_gate_skips_non_production_modes(tmp_path: Path):
    """rehearsal / test 不做该门（排演工件不是生产身份），生产才 fail-closed。"""
    artifact = _artifact(tmp_path, commit=A)
    _patch_artifact_commit(artifact, None)
    payload = frozen_model_identity_payload(artifact)
    for mode in ("rehearsal", "test"):
        assert_model_training_commit(
            model_training_code_commit=str(payload["model_training_code_commit"]),
            runtime_code_commit=A,
            validation_mode=mode,
        )
    with pytest.raises(FreezeGateError):
        assert_model_training_commit(
            model_training_code_commit=str(payload["model_training_code_commit"]),
            runtime_code_commit=A,
            validation_mode="production",
        )
