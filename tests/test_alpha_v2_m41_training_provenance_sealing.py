"""M4-L R1.1 定向测试：训练 provenance 封存（fingerprint 覆盖真实训练输入 + 工件哈希封存）。

被钉住的契约（R1.1，外部复核"最终证据完整性"问题）：

```text
training_data_fingerprint  ← 覆盖 load_daily_panel 实际读取的 source_window
                             （含 warmup）与全部面板源列；
artifact_hash (v2)         ← 覆盖 provenance 的 window / warmup_days /
                             source_window / fingerprint(+version) / rows /
                             columns 与 config_hash；
production                 ← 只接受 v2 工件；preflight 按模型记录的同参数复算。
```

用例编号对应工作单 §10（FP-1..FP-10）：

- FP-1..FP-3：决策窗内改 close / float_market_cap / board·is_st → 指纹改变；
  其中 FP-2/FP-3 同时给出**反证**——用 R1 的 8 列清单调用同一函数时这些改动
  不会改变指纹（这正是 R1.1 修掉的漏检）。
- FP-4：改 warmup 段（``window_start`` 之前、source_window 之内）→ 指纹改变；
  同一条改动用 R1 参数（``warmup_days=0``）算则不变——warmup 曾整体在覆盖之外。
- FP-5：改 source_window 之前的数据 → 指纹不变（训练链根本没读那些行）。
- FP-6：只在 ``window_end`` 之后追加交易日 → 指纹不变。
- FP-7/FP-8/FP-9：改写 ``provenance.training_data_fingerprint`` / ``window`` /
  ``warmup_days`` 但不重算工件哈希 → ``load_frozen_model`` 必须拒绝；
  另加 config_hash 同类用例（"不要只保护 code_commit"）。
- FP-10：preflight 用当前 DB 复算与模型指纹不符 → BLOCKED；validation freeze
  硬门 exit 7。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from stock_analyzer.alpha_v2.research.multi_head import HeadFitSpec
from stock_analyzer.alpha_v2.validation import preflight as pf
from stock_analyzer.alpha_v2.validation.frozen_model import (
    ARTIFACT_HASH_VERSION_V1,
    ARTIFACT_HASH_VERSION_V2,
    SEALED_PROVENANCE_REQUIRED_KEYS,
    FrozenModelError,
    fit_frozen_model,
    load_frozen_model,
    missing_sealed_provenance_keys,
    persist_frozen_model,
)
from stock_analyzer.alpha_v2.validation.training_data_fingerprint import (
    MODEL_TRAINING_SOURCE_COLUMNS,
    compute_training_data_fingerprint,
    source_window_start,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# R1 的 8 列清单（对照用：证明"漏检"确实存在，而不是纸面论断）。
R1_FINGERPRINT_COLUMNS = ("symbol", "date", "open", "high", "low", "close", "volume", "turnover")

# 决策窗（含）与 warmup：source_window 起点 = 2026-03-02 - 20 自然日 = 2026-02-10。
WINDOW_START = date(2026, 3, 2)
WINDOW_END = date(2026, 3, 13)
WARMUP_DAYS = 20
SOURCE_START = date(2026, 2, 10)
# 数据范围刻意包住三段：source_window 之前 / warmup 段 / 决策窗。
DATA_START = date(2026, 1, 5)
DATA_END = date(2026, 3, 20)
COMMIT = "a" * 40
MODEL_ID = "r11_sealed_model"


def _fp_db(path: Path, *, symbols: tuple[str, ...] = ("600001", "600002")) -> Path:
    """合成 daily_bars：**全 17 个面板源列**（含 float_market_cap / board / is_st /
    pre_close / up_limit / down_limit / price_series_mode），覆盖 DATA_START..DATA_END。"""
    rows: list[tuple[object, ...]] = []
    day = DATA_START
    index = 0
    while day <= DATA_END:
        for position, symbol in enumerate(symbols):
            price = 10.0 + position + index * 0.01
            share = (index + position) % 3
            volume = 1_000_000.0 if share else 10_000.0
            rows.append(
                (
                    symbol,
                    day,
                    price,
                    price * 1.01,
                    price * 0.99,
                    price,
                    volume,
                    volume * price * (1.0 if share else 100.0),
                    5.0e9 + index,  # float_market_cap
                    "gem" if position else "main",  # board（源列用代码，面板里归一化）
                    bool(index % 7 == 0),  # is_st
                    False,  # is_delisting_risk
                    False,  # suspended
                    price * 0.995,  # pre_close
                    round(price * 1.1, 2),  # up_limit
                    round(price * 0.9, 2),  # down_limit
                    "raw",  # price_series_mode
                )
            )
        day = date.fromordinal(day.toordinal() + 1)
        index += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE daily_bars (symbol VARCHAR, date DATE, open DOUBLE, high DOUBLE, "
            "low DOUBLE, close DOUBLE, volume DOUBLE, turnover DOUBLE, "
            "float_market_cap DOUBLE, board VARCHAR, is_st BOOLEAN, "
            "is_delisting_risk BOOLEAN, suspended BOOLEAN, pre_close DOUBLE, "
            "up_limit DOUBLE, down_limit DOUBLE, price_series_mode VARCHAR)"
        )
        connection.executemany(
            "INSERT INTO daily_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    finally:
        connection.close()
    return path


def _fp(path: Path, *, warmup_days: int = WARMUP_DAYS, columns=None) -> dict[str, object]:
    return compute_training_data_fingerprint(
        path,
        training_start=WINDOW_START,
        training_end=WINDOW_END,
        warmup_days=warmup_days,
        columns=columns,
    )


def _update(db: Path, sql: str) -> None:
    connection = duckdb.connect(str(db))
    try:
        connection.execute(sql)
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# FP-0（附加）：source_window 必须等于 load_daily_panel 真正读取的行范围
# ---------------------------------------------------------------------------


def test_fp0_source_window_equals_panel_read_range(tmp_path):
    """指纹窗口 == 训练链实际读取范围（附件的锚点；不对齐则 FP-4/FP-5 都是空话）。"""
    from stock_analyzer.alpha_v2.research.panel import load_daily_panel

    db = _fp_db(tmp_path / "m.duckdb")
    panel = load_daily_panel(
        market_db=db,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        warmup_days=WARMUP_DAYS,
    )
    assert source_window_start(WINDOW_START, WARMUP_DAYS) == SOURCE_START
    earliest = panel.bars["trade_date"].min().date()
    assert earliest == SOURCE_START, (
        f"面板实际最早 bar={earliest}，指纹声明 source_window 起点={SOURCE_START}"
    )
    # 面板日历只覆盖决策窗（warmup 行不进日历，但仍参与特征构造）
    assert panel.calendar[0] == WINDOW_START
    assert panel.calendar[-1] == WINDOW_END


# ---------------------------------------------------------------------------
# FP-1 / FP-2 / FP-3：决策窗内改数据
# ---------------------------------------------------------------------------


def test_fp1_close_change_changes_fingerprint(tmp_path):
    db = _fp_db(tmp_path / "m.duckdb")
    before = _fp(db)
    _update(
        db,
        "UPDATE daily_bars SET close = close * 1.01 "
        "WHERE symbol = '600001' AND date = DATE '2026-03-05'",
    )
    after = _fp(db)
    assert before["fingerprint"] != after["fingerprint"]
    assert before["rows"] == after["rows"]
    assert before["decision_window"] == [WINDOW_START.isoformat(), WINDOW_END.isoformat()]
    assert before["source_window"] == [SOURCE_START.isoformat(), WINDOW_END.isoformat()]


def test_fp2_float_market_cap_change_changes_fingerprint(tmp_path):
    """FP-2：float_market_cap 进指纹（R1 的 8 列清单会漏掉它）。"""
    db = _fp_db(tmp_path / "m.duckdb")
    legacy_before = _fp(db, warmup_days=0, columns=R1_FINGERPRINT_COLUMNS)
    before = _fp(db)
    _update(
        db,
        "UPDATE daily_bars SET float_market_cap = float_market_cap * 2 "
        "WHERE symbol = '600002' AND date = DATE '2026-03-06'",
    )
    after = _fp(db)
    legacy_after = _fp(db, warmup_days=0, columns=R1_FINGERPRINT_COLUMNS)
    assert before["fingerprint"] != after["fingerprint"]
    # 反证：R1 的 8 列 + 决策窗口径对这种改动是盲的（这就是 R1.1 要修的漏检）
    assert legacy_before["fingerprint"] == legacy_after["fingerprint"]


@pytest.mark.parametrize(
    ("column", "sql_value"),
    [
        ("board", "'star'"),
        ("is_st", "TRUE"),
        ("is_delisting_risk", "TRUE"),
        ("suspended", "TRUE"),
        ("pre_close", "pre_close * 1.02"),
        ("price_series_mode", "'qfq'"),
    ],
)
def test_fp3_board_and_flags_change_fingerprint(tmp_path, column, sql_value):
    """FP-3：board / 状态位 / 前收 / 价格口径列全部进指纹。"""
    db = _fp_db(tmp_path / "m.duckdb")
    before = _fp(db)
    _update(
        db,
        f"UPDATE daily_bars SET {column} = {sql_value} "
        "WHERE symbol = '600001' AND date = DATE '2026-03-06'",
    )
    after = _fp(db)
    assert before["fingerprint"] != after["fingerprint"], column


def test_fp3_missing_optional_column_is_recorded_in_header(tmp_path):
    """列**存在但全空**与**列不存在**必须是两种身份（header 记录存在性）。"""
    db = _fp_db(tmp_path / "m.duckdb")
    before = _fp(db)
    assert before["missing_optional_source_columns"] == []
    assert "float_market_cap" in before["available_source_columns"]
    _update(db, "ALTER TABLE daily_bars DROP COLUMN float_market_cap")
    after = _fp(db)
    assert after["missing_optional_source_columns"] == ["float_market_cap"]
    assert "float_market_cap" not in after["available_source_columns"]
    assert before["fingerprint"] != after["fingerprint"]
    # 必需列缺失 → 直接报错（不产生"少列也算过"的指纹）
    _update(db, "ALTER TABLE daily_bars DROP COLUMN close")
    from stock_analyzer.alpha_v2.validation.training_data_fingerprint import (
        TrainingDataFingerprintError,
    )

    with pytest.raises(TrainingDataFingerprintError, match="必需源列"):
        _fp(db)


# ---------------------------------------------------------------------------
# FP-4 / FP-5 / FP-6：窗口边界
# ---------------------------------------------------------------------------


def test_fp4_warmup_row_change_changes_fingerprint(tmp_path):
    """FP-4：``window_start`` 之前、source_window 之内的行参与指纹（R1 漏掉的那段）。"""
    db = _fp_db(tmp_path / "m.duckdb")
    warmup_row = date(2026, 2, 15)
    assert SOURCE_START <= warmup_row < WINDOW_START
    legacy_before = _fp(db, warmup_days=0)
    before = _fp(db)
    _update(
        db,
        f"UPDATE daily_bars SET close = close * 1.07 WHERE date = DATE '{warmup_row}'",
    )
    after = _fp(db)
    legacy_after = _fp(db, warmup_days=0)
    assert before["fingerprint"] != after["fingerprint"]
    # 反证：R1 口径（warmup_days=0 ⇒ source_window == 决策窗）对 warmup 段是盲的
    assert legacy_before["fingerprint"] == legacy_after["fingerprint"]


def test_fp5_before_source_window_change_keeps_fingerprint(tmp_path):
    """FP-5：source_window 之前的数据不参与训练读取 → 指纹不变。"""
    db = _fp_db(tmp_path / "m.duckdb")
    before = _fp(db)
    earlier = date(2026, 1, 20)
    assert earlier < SOURCE_START
    _update(
        db,
        f"UPDATE daily_bars SET close = close * 1.5 WHERE date = DATE '{earlier}'",
    )
    _update(db, "DELETE FROM daily_bars WHERE date = DATE '2026-01-05'")
    after = _fp(db)
    assert before["fingerprint"] == after["fingerprint"]


def test_fp6_post_window_append_keeps_fingerprint(tmp_path):
    """FP-6：只在 ``window_end`` 之后追加新交易日 → 指纹不变。"""
    db = _fp_db(tmp_path / "m.duckdb")
    before = _fp(db)
    connection = duckdb.connect(str(db))
    try:
        connection.execute(
            "INSERT INTO daily_bars SELECT * REPLACE (DATE '2026-03-19' AS date) "
            "FROM daily_bars WHERE symbol = '600001' AND date = DATE '2026-03-18'"
        )
        connection.execute(
            "INSERT INTO daily_bars SELECT * REPLACE (DATE '2026-03-20' AS date) "
            "FROM daily_bars WHERE symbol = '600002' AND date = DATE '2026-03-18'"
        )
    finally:
        connection.close()
    after = _fp(db)
    assert before["fingerprint"] == after["fingerprint"]
    assert after["rows"] == before["rows"]


def test_fingerprint_contract_fields_are_versioned_and_self_describing(tmp_path):
    """§4 契约：payload 必须自述版本/窗口/列存在性/行数/内容哈希。"""
    db = _fp_db(tmp_path / "m.duckdb")
    payload = _fp(db)
    for key in (
        "fingerprint_version",
        "decision_window",
        "source_window",
        "warmup_days",
        "requested_source_columns",
        "available_source_columns",
        "missing_optional_source_columns",
        "row_count",
        "content_hash",
    ):
        assert key in payload, key
    assert payload["fingerprint_version"] == "v2"
    assert payload["content_hash"] == payload["fingerprint"]
    assert tuple(payload["requested_source_columns"]) == MODEL_TRAINING_SOURCE_COLUMNS
    assert payload["warmup_days"] == WARMUP_DAYS
    assert payload["source_window"][0] == SOURCE_START.isoformat()
    assert payload["row_count"] == payload["rows"]
    assert payload["schema"] == "alpha_v2_training_data_fingerprint.v2"


# ---------------------------------------------------------------------------
# FP-7 / FP-8 / FP-9：工件哈希封存 provenance
# ---------------------------------------------------------------------------


def _matrix(rows_train: int = 140, rows_cal: int = 60) -> pd.DataFrame:
    rng = np.random.default_rng(11)
    total = rows_train + rows_cal
    features = ["ret_1d", "ma5", "volume_ratio_5"]
    frame = pd.DataFrame(
        {
            "decision_date": [
                f"2026-0{1 + (index // 60)}-{1 + index % 28:02d}" for index in range(total)
            ],
            "symbol": [f"6000{index % 20:02d}" for index in range(total)],
            **{name: rng.normal(0.0, 1.0, total) for name in features},
        }
    )
    base = 0.3 * frame["ret_1d"] + 0.2 * frame["ma5"]
    for horizon in (3, 5, 10, 15):
        frame[f"net_return_{horizon}d"] = base * (horizon / 5.0) + rng.normal(0.0, 0.01, total)
        frame[f"excess_return_{horizon}d"] = frame[f"net_return_{horizon}d"] - 0.001
        frame[f"mae_{horizon}d"] = -np.abs(frame[f"net_return_{horizon}d"]) * 0.6
        frame[f"up_net_{horizon}d"] = (frame[f"net_return_{horizon}d"] > 0).astype(float)
        frame[f"up_excess_{horizon}d"] = (frame[f"excess_return_{horizon}d"] > 0).astype(float)
        frame[f"mae_le_5pct_{horizon}d"] = (frame[f"mae_{horizon}d"] <= -0.05).astype(float)
    frame["alpha_target_5d"] = frame.groupby("decision_date")["excess_return_5d"].rank(pct=True)
    frame["is_train"] = False
    frame["is_calibration"] = False
    frame.loc[: rows_train - 1, "is_train"] = True
    frame.loc[rows_train:, "is_calibration"] = True
    return frame


def _sealed_model(tmp_path: Path, *, db: Path, model_id: str = MODEL_ID) -> Path:
    """按真实指纹落一份**封存完整**的冻结模型工件（v2）。"""
    payload = _fp(db)
    model = fit_frozen_model(
        frame=_matrix(),
        model_id=model_id,
        spec=HeadFitSpec(min_train_rows=20, min_class_balance=0.05),
        provenance={
            "source": "r11_sealing_test",
            "window": [WINDOW_START.isoformat(), WINDOW_END.isoformat()],
            "warmup_days": int(payload["warmup_days"]),
            "source_window": list(payload["source_window"]),
            "training_data_fingerprint": str(payload["fingerprint"]),
            "training_data_fingerprint_version": str(payload["fingerprint_version"]),
            "training_data_rows": int(payload["rows"]),
            "training_data_columns": list(payload["columns"]),
        },
        extra_identity={"code_commit": COMMIT, "config_hash": "cfg-r11"},
    )
    return persist_frozen_model(model, tmp_path / "artifacts")


def _tamper_provenance(artifact: Path, key: str, value: object) -> None:
    """只改 manifest 的 provenance 字段，**不**重算 artifact_hash。"""
    path = artifact / "model_manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if value is None:
        payload.get("provenance", {}).pop(key, None)
    else:
        payload.setdefault("provenance", {})[key] = value
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _tamper_top_level(artifact: Path, key: str, value: object) -> None:
    path = artifact / "model_manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if value is None:
        payload.pop(key, None)
    else:
        payload[key] = value
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_sealed_artifact_loads_with_production_gate(tmp_path):
    """正例：v2 封存工件在生产加载门下通过（封存门不误伤正常产物）。"""
    db = _fp_db(tmp_path / "m.duckdb")
    artifact = _sealed_model(tmp_path, db=db)
    manifest = json.loads((artifact / "model_manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifact_hash_version"] == ARTIFACT_HASH_VERSION_V2
    assert missing_sealed_provenance_keys(manifest) == []
    model = load_frozen_model(artifact, require_sealed_provenance=True)
    assert model.manifest["artifact_hash"] == manifest["artifact_hash"]


@pytest.mark.parametrize(
    ("key", "value", "label"),
    [
        ("training_data_fingerprint", "0" * 64, "fingerprint"),
        ("window", ["2026-01-01", "2026-06-30"], "window"),
        ("warmup_days", 999, "warmup"),
        ("source_window", ["2026-01-01", "2026-03-13"], "source_window"),
        ("training_data_rows", 1, "rows"),
        ("training_data_columns", ["symbol"], "columns"),
        ("training_data_fingerprint_version", ARTIFACT_HASH_VERSION_V1, "version"),
    ],
)
def test_fp7_9_provenance_tamper_breaks_artifact_integrity(tmp_path, key, value, label):
    """FP-7/8/9：改 provenance 任意封存项而不重算哈希 → 加载必须拒绝。"""
    db = _fp_db(tmp_path / "m.duckdb")
    artifact = _sealed_model(tmp_path, db=db)
    load_frozen_model(artifact, require_sealed_provenance=True)  # 未篡改时可加载
    _tamper_provenance(artifact, key, value)
    with pytest.raises(FrozenModelError, match="artifact_hash"):
        load_frozen_model(artifact, require_sealed_provenance=True)
    with pytest.raises(FrozenModelError, match="artifact_hash"):
        load_frozen_model(artifact)  # 非生产加载同样拒绝（完整性是同一套算法）


def test_sealed_identity_covers_config_hash_not_only_code_commit(tmp_path):
    """封存身份不止 code_commit：改 config_hash 同样破坏完整性。"""
    db = _fp_db(tmp_path / "m.duckdb")
    artifact = _sealed_model(tmp_path, db=db)
    _tamper_top_level(artifact, "config_hash", "cfg-other")
    with pytest.raises(FrozenModelError, match="artifact_hash"):
        load_frozen_model(artifact)


def _legacy_v1_hash(manifest: dict) -> str:
    """独立复现 R4.1（v1）工件哈希：7 键正文 + canonical JSON sha256。

    刻意在测试里重写一遍公式（而不是调私有函数）：这样"v1 算法没有被新版本
    顺手改掉"才是被独立验证的事实——磁盘上仍有 v1 归档模型。
    """
    from stock_analyzer.config_identity import stable_payload_hash

    return stable_payload_hash(
        {
            "model_id": manifest.get("model_id", ""),
            "feature_columns": list(manifest.get("feature_columns", []) or []),
            "params": manifest.get("params", {}),
            "targets": manifest.get("targets", []),
            "calibration": manifest.get("calibration", {}),
            "files": dict(sorted(dict(manifest.get("files", {})).items())),
            "code_commit": str(manifest.get("code_commit", "") or ""),
        }
    )


def test_legacy_v1_artifact_still_loads_but_is_rejected_in_production(tmp_path):
    """§6 兼容：真正按 v1 公式生成的旧工件仍可加载；生产路径拒绝 unsealed。"""
    db = _fp_db(tmp_path / "m.duckdb")
    artifact = _sealed_model(tmp_path, db=db)
    manifest_path = artifact / "model_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    # 还原成"R1.1 之前冻结出来的工件"：没有版本字段，哈希按 v1 公式重算
    payload.pop("artifact_hash_version")
    payload["artifact_hash"] = _legacy_v1_hash(payload)
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    assert load_frozen_model(artifact).manifest["artifact_hash"] == payload["artifact_hash"]
    # production：拒绝（provenance 未封存）
    with pytest.raises(FrozenModelError, match="未封存训练 provenance"):
        load_frozen_model(artifact, require_sealed_provenance=True)
    check = pf.check_model_identity(artifact)
    assert check.verdict == pf.VERDICT_BLOCKED
    assert check.facts["artifact_hash_version"] == ARTIFACT_HASH_VERSION_V1


def test_unsealed_manifest_without_provenance_is_rejected_by_production_load(tmp_path):
    """v2 版本号但封存项缺失（手改 provenance）→ 生产加载拒绝。"""
    db = _fp_db(tmp_path / "m.duckdb")
    artifact = _sealed_model(tmp_path, db=db)
    for key in SEALED_PROVENANCE_REQUIRED_KEYS:
        _tamper_provenance(artifact, key, None)
    with pytest.raises(FrozenModelError, match="封存不完整"):
        load_frozen_model(artifact, require_sealed_provenance=True)


def test_fp6_legacy_v1_artifact_rejected_by_real_freeze_cli(tmp_path):
    """§6 端到端：自洽的 v1 工件在真实 freeze CLI 的生产分支被拒（exit 5）。

    与"哈希被改坏"不同——这里工件**完全自洽**（按 v1 公式重算哈希，能正常加载），
    只是训练 provenance 没被封存。生产必须拒绝它，而不是"能加载就算过"。
    """
    db = _fp_db(tmp_path / "m.duckdb")
    artifact = _sealed_model(tmp_path, db=db)
    manifest_path = artifact / "model_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload.pop("artifact_hash_version")
    payload["artifact_hash"] = _legacy_v1_hash(payload)
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    load_frozen_model(artifact)  # 自洽：非生产路径能加载
    sandbox = _sandbox(tmp_path, COMMIT)
    keep = [
        part
        for part in os.environ.get("PATH", "").split(os.pathsep)
        if part and not ((Path(part) / "git.exe").exists() or (Path(part) / "git").exists())
    ]
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(keep)
    env["SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE"] = "raw"
    out = tmp_path / "out_v1"
    out.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            sys.executable,
            str(sandbox / "scripts" / "alpha_v2_validation_freeze.py"),
            "--epoch-id",
            "alpha_v2_epoch_912",
            "--out",
            str(out),
            "--model-dir",
            str(artifact),
            "--start-date",
            date.today().isoformat(),
            "--open-epoch",
        ],
        cwd=str(sandbox),
        capture_output=True,
        text=True,
        env=env,
        timeout=1200,
    )
    assert result.returncode == 5, (result.returncode, result.stderr[-600:])
    assert "未封存训练 provenance" in result.stderr
    assert not (out / "validation" / "epochs.json").exists()


# ---------------------------------------------------------------------------
# FP-10：preflight 复算绑定 + validation freeze exit 7
# ---------------------------------------------------------------------------


def test_fp10_preflight_recompute_binding_blocks_on_data_change(tmp_path):
    """FP-10（上）：模型指纹 == 当前 DB 重算值 → PASS；改一行 → BLOCKED。"""
    db = _fp_db(tmp_path / "m.duckdb")
    artifact = _sealed_model(tmp_path, db=db)
    identity = pf.check_model_identity(artifact)
    assert identity.verdict == pf.VERDICT_PASS, identity.findings
    ok = pf.check_training_data_fingerprint(
        market_db=db,
        model_identity=dict(identity.facts),
        training_start=WINDOW_START,
        training_end=WINDOW_END,
    )
    assert ok.verdict == pf.VERDICT_PASS, ok.findings
    assert ok.facts["recomputed_fingerprint"] == identity.facts["training_data_fingerprint"]
    assert ok.facts["recomputed_source_window"] == identity.facts["provenance_source_window"]
    _update(
        db,
        "UPDATE daily_bars SET float_market_cap = float_market_cap * 3 "
        "WHERE symbol = '600001' AND date = DATE '2026-02-15'",
    )
    blocked = pf.check_training_data_fingerprint(
        market_db=db,
        model_identity=dict(identity.facts),
        training_start=WINDOW_START,
        training_end=WINDOW_END,
    )
    assert blocked.verdict == pf.VERDICT_BLOCKED
    assert any("mismatch" in item for item in blocked.findings), blocked.findings


def _preflight_report_with_mismatched_fingerprint(tmp_path: Path, *, artifact: Path) -> Path:
    """报告自称的模型指纹与**冻结模型块**不一致（FP-10 下：gate 必须 exit 7）。"""
    from stock_analyzer.alpha_v2.validation.frozen_model import (
        frozen_model_identity_payload,
    )

    identity = dict(frozen_model_identity_payload(artifact))
    payload: dict[str, object] = {
        "schema": pf.PREFLIGHT_SCHEMA,
        "generated_at": __import__("datetime").datetime.now().astimezone().isoformat(),
        "verdict": pf.VERDICT_PASS,
        "blocking_findings": [],
        "warnings": [],
        "facts": {},
        "runtime_identity": {"code_commit": COMMIT},
        "model_identity": {
            "model_id": str(identity.get("model_id", "")),
            "model_artifact_hash": str(identity.get("artifact_hash", "")),
            "artifact_hash_version": str(identity.get("artifact_hash_version", "")),
            "feature_schema_hash": str(identity.get("feature_schema_hash", "")),
            "model_training_code_commit": str(identity.get("model_training_code_commit", "")),
            "provenance_window": list(
                dict(identity.get("provenance", {}) or {}).get("window") or []
            ),
            "provenance_warmup_days": dict(identity.get("provenance", {}) or {}).get("warmup_days"),
            "provenance_source_window": list(
                dict(identity.get("provenance", {}) or {}).get("source_window") or []
            ),
            "training_data_fingerprint": "f" * 64,  # ← 与模型不一致
            "training_data_fingerprint_version": str(
                dict(identity.get("provenance", {}) or {}).get(
                    "training_data_fingerprint_version", ""
                )
            ),
        },
        "data_identity": {
            "market_db": "synthetic",
            "warmup_days": WARMUP_DAYS,
            "source_window": [SOURCE_START.isoformat(), WINDOW_END.isoformat()],
            "training_data_fingerprint_version": "v2",
        },
        "training_window": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        "checks": [],
    }
    payload["preflight_hash"] = pf.preflight_hash_of(payload)
    path = tmp_path / "preflight_mismatch.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_fp10_gate_rejects_mismatched_fingerprint(tmp_path):
    """gate 的**唯一**不一致项必须就是指纹：其余字段全部对齐（否则测不出这条门）。"""
    from stock_analyzer.alpha_v2.validation.frozen_model import (
        frozen_model_identity_payload,
    )

    db = _fp_db(tmp_path / "m.duckdb")
    artifact = _sealed_model(tmp_path, db=db)
    report = _preflight_report_with_mismatched_fingerprint(tmp_path, artifact=artifact)
    model_block = dict(frozen_model_identity_payload(artifact))
    with pytest.raises(pf.PreflightError) as excinfo:
        pf.assert_preflight_gate(
            report_path=report,
            runtime_code_commit=COMMIT,
            model_block=model_block,
            max_age_hours=48.0,
        )
    message = str(excinfo.value)
    assert "training_data_fingerprint:" in message
    # 只允许指纹这一项不一致（其余字段必须逐项相等，证明拒绝是针对指纹的）
    assert message.count(";") == 0, message


def _sandbox(tmp_path: Path, commit: str) -> Path:
    """无 git 容器形态沙箱（代码树副本 + 构建身份两文件）。

    与 R4.1 绑定测试同型的极小复刻：R1.1 的 exit 7 证据必须来自**真实 CLI**
    （而不是只测函数），所以需要一份 REPO_ROOT 指向沙箱的代码副本。
    """
    sandbox = tmp_path / "sandbox"
    for name in ("src", "scripts", "config"):
        shutil.copytree(
            REPO_ROOT / name,
            sandbox / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
        )
    (sandbox / ".build_commit").write_text(f"{commit}\n", encoding="utf-8")
    (sandbox / "build_manifest.json").write_text(
        json.dumps(
            {
                "commit": commit,
                "short_commit": commit[:12],
                "dirty": False,
                "built_at_utc": "2026-09-20T00:00:00Z",
                "config_schema": "stock-analyzer-config.v1",
                "runtime_state_schema": 9,
            }
        ),
        encoding="utf-8",
    )
    return sandbox


def test_fp10_validation_freeze_exit_7_on_fingerprint_mismatch(tmp_path):
    """FP-10（下）：端到端——真实 freeze CLI 在指纹不一致时 exit 7 且不落盘。

    对照组：同一份工件 + **一致**报告 → 走到开 epoch（rc=0），证明 exit 7 是
    指纹绑定门判出来的，不是环境问题（沙箱本身能通过）。
    """
    db = _fp_db(tmp_path / "m.duckdb")
    artifact = _sealed_model(tmp_path, db=db)
    sandbox = _sandbox(tmp_path, COMMIT)
    keep = [
        part
        for part in os.environ.get("PATH", "").split(os.pathsep)
        if part and not ((Path(part) / "git.exe").exists() or (Path(part) / "git").exists())
    ]
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(keep)
    env["SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE"] = "raw"
    today = date.today().isoformat()

    def _run(report: Path, out: Path) -> subprocess.CompletedProcess[str]:
        out.mkdir(parents=True, exist_ok=True)
        return subprocess.run(
            [
                sys.executable,
                str(sandbox / "scripts" / "alpha_v2_validation_freeze.py"),
                "--epoch-id",
                "alpha_v2_epoch_911",
                "--out",
                str(out),
                "--model-dir",
                str(artifact),
                "--start-date",
                today,
                "--open-epoch",
                "--preflight-report",
                str(report),
            ],
            cwd=str(sandbox),
            capture_output=True,
            text=True,
            env=env,
            timeout=1200,
        )

    mismatched = _run(
        _preflight_report_with_mismatched_fingerprint(tmp_path, artifact=artifact),
        tmp_path / "out_bad",
    )
    assert mismatched.returncode == 7, (mismatched.returncode, mismatched.stderr[-800:])
    assert "training_data_fingerprint" in mismatched.stderr
    assert not (tmp_path / "out_bad" / "validation" / "epochs.json").exists()

    # 对照：把报告里的指纹改成模型指纹 → 同一环境跑到开 epoch
    report_payload = json.loads(
        _preflight_report_with_mismatched_fingerprint(tmp_path, artifact=artifact).read_text(
            encoding="utf-8"
        )
    )
    provenance = str(pf.check_model_identity(artifact).facts["training_data_fingerprint"])
    report_payload["model_identity"]["training_data_fingerprint"] = provenance
    report_payload["preflight_hash"] = pf.preflight_hash_of(report_payload)
    consistent = tmp_path / "preflight_consistent.json"
    consistent.write_text(json.dumps(report_payload), encoding="utf-8")
    good = _run(consistent, tmp_path / "out_ok")
    assert good.returncode == 0, (good.returncode, good.stdout[-500:], good.stderr[-800:])
    assert (tmp_path / "out_ok" / "validation" / "epochs.json").exists()
    manifest = json.loads(
        (tmp_path / "out_ok" / "validation" / "validation_freeze_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    binding = manifest["production_preflight"]
    assert binding["training_data_fingerprint"] == provenance
    assert binding["training_data_fingerprint_version"] == "v2"
    assert binding["warmup_days"] == WARMUP_DAYS
    assert binding["source_window"] == [SOURCE_START.isoformat(), WINDOW_END.isoformat()]
    assert manifest["model"]["artifact_hash_version"] == ARTIFACT_HASH_VERSION_V2
