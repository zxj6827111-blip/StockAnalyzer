"""RAW execution delta 的生产接线（P1 §3–§5、§9–§21）。

这一组测试回答的是**事务性问题**，不是单元行为：

```text
两份 delta 是不是各自按显式口径推进？
任一份失败时 readiness 会不会仍然发布？   （答案必须是"不会"）
raw 基线不存在时，会不会被 --incremental 偷偷初始化？（答案必须是"不会"）
两份 delta 目标日成员数一样、成员不同，会不会被放行？（答案必须是"不会"）
```

实现上故意让 RAW-1/RAW-2、TX-4 用**真实**的 vendor ZIP / index / DuckDB 夹具，而
TX-2/TX-3 只把"角色失败"这一点注入——因为"失败时不放行"是编排层的不变量，用桩注入
才能精确地只让一半失败，而真实夹具保证其余每一步都没有被绕过。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import zipfile
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from stock_analyzer.ops.nightly_readiness import (
    check_nightly_readiness,
    read_nightly_readiness,
    write_nightly_readiness,
)
from stock_analyzer.ops.raw_delta_baseline import (
    build_bootstrap_marker,
    write_bootstrap_marker,
)

ROOT = Path(__file__).resolve().parents[1]
_UPDATER_PATH = ROOT / "scripts" / "update_vendor_daily_from_tushare.py"
_IMPORTER_PATH = ROOT / "scripts" / "import_vendor_zip_to_delta.py"
_COVERAGE_PATH = ROOT / "scripts" / "alpha_v2_raw_delta_coverage.py"
#: 夹具 ZIP 里的唯一交易日；source window 也用它（覆盖判据按实测事实而非整数）。
FIXTURE_DATE = "2025-07-17"
TARGET_DATE = "2025-07-20"


def _load_script(name: str, path: Path) -> object:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def updater() -> object:
    return _load_script("update_vendor_daily_from_tushare", _UPDATER_PATH)


@pytest.fixture(scope="module")
def coverage_module() -> object:
    return _load_script("alpha_v2_raw_delta_coverage", _COVERAGE_PATH)


# ---------------------------------------------------------------------------
# 夹具：真实 ZIP / index / DuckDB
# ---------------------------------------------------------------------------


def _write_zip(path: Path, entries: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content.encode("utf-8"))


def _daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["600000.SH", "600000.SH"],
            "trade_date": ["20250717", "20250720"],
            "open": [10.0, 9.5],
            "high": [11.0, 10.0],
            "low": [9.0, 9.0],
            "close": [10.5, 9.5],
            "pre_close": [10.2, 10.5],
            "change": [0.3, -1.0],
            "pct_chg": [2.9, -9.5],
            "vol": [100, 300],
            "amount": [123.4, 345.6],
        }
    )


def _basic_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["600000.SH", "600000.SH"],
            "trade_date": ["20250717", "20250720"],
            "turnover_rate": [0.5, 0.7],
            "turnover_rate_f": [0.4, 0.6],
            "volume_ratio": [1.1, 1.3],
            "pe": [10.1, 10.3],
            "pe_ttm": [9.1, 9.3],
            "pb": [1.1, 1.3],
            "ps": [2.1, 2.3],
            "ps_ttm": [2.0, 2.2],
            "dv_ratio": [3.1, 3.3],
            "dv_ttm": [3.0, 3.2],
            "total_share": [100.0, 100.0],
            "float_share": [80.0, 80.0],
            "free_share": [70.0, 70.0],
            "total_mv": [1000.0, 1000.0],
            "circ_mv": [800.0, 800.0],
        }
    )


def _adj_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["600000.SH"] * 3,
            "trade_date": ["20250701", "20250717", "20250720"],
            "adj_factor": [1.1, 1.1, 2.2],
        }
    )


def _fake_pro() -> object:
    class _FakePro:
        def trade_cal(self, **kwargs: object) -> pd.DataFrame:
            return pd.DataFrame({"cal_date": ["20250720"]})

        def daily(self, ts_code: str = "", **kwargs: object) -> pd.DataFrame:
            return _daily_frame()

        def daily_basic(self, ts_code: str = "", **kwargs: object) -> pd.DataFrame:
            return _basic_frame()

        def adj_factor(self, ts_code: str = "", **kwargs: object) -> pd.DataFrame:
            return _adj_frame()

    return _FakePro()


def _vendor_fixture(tmp_path: Path) -> Path:
    _write_zip(
        tmp_path / "全A日K" / "2025.zip",
        {
            "2025/600000.SH.csv": (
                "code,datetime,open,high,low,close,pre_close,change,pct_chg,volume,amount\n"
                "600000.SH,2025-07-17,10,11,9,10.5,10.5,0,0,100,123.4\n"
            )
        },
    )
    _write_zip(
        tmp_path / "复权因子" / "复权因子_前复权.zip",
        {
            "2025/600000.SH.csv": (
                "股票代码,交易日期,复权因子\n600000.SH,20250701,0.9\n600000.SH,20250717,1.0\n"
            )
        },
    )
    _write_zip(
        tmp_path / "复权因子" / "复权因子_后复权.zip",
        {
            "2025/600000.SH.csv": (
                "股票代码,交易日期,复权因子\n"
                "600000.SH,20250701,1.0\n"
                "600000.SH,20250717,1.1111111111111112\n"
            )
        },
    )
    return tmp_path


def _full_daily_index(root: Path) -> Path:
    from stock_analyzer.data.vendor_zip_overlay import write_vendor_zip_daily_index

    index_path = root / "index" / "daily_index.json"
    write_vendor_zip_daily_index(root=root, output_path=index_path)
    return index_path


def _run_importer(argv: list[str]) -> tuple[int, dict[str, object]]:
    module = _load_script("import_vendor_zip_to_delta", _IMPORTER_PATH)
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        exit_code = module._main(argv)
    text = captured.getvalue().strip()
    return exit_code, (json.loads(text) if text else {})


def _bootstrap_raw_baseline(
    *,
    vendor_root: Path,
    index_path: Path,
    raw_db: Path,
    feature_db: Path,
    coverage_module: object,
) -> dict[str, object]:
    """走**真实**路径建一份 raw 基线：全量导入 → 覆盖校验 → 写 marker。

    P1 的产物是"接线"，不是"基线"；所以测试里也不许手工糊一个 marker 出来——
    必须证明这条流程本身能把一份真库认证成基线。
    """
    feature_rc, _ = _run_importer(
        [
            "--data-root",
            str(vendor_root),
            "--index-path",
            str(index_path),
            "--delta-db-path",
            str(feature_db),
            "--price-series-mode",
            "qfq",
        ]
    )
    assert feature_rc == 0
    raw_rc, raw_report = _run_importer(
        [
            "--data-root",
            str(vendor_root),
            "--index-path",
            str(index_path),
            "--delta-db-path",
            str(raw_db),
            "--price-series-mode",
            "raw",
        ]
    )
    assert raw_rc == 0
    assert raw_report["price_series_mode"] == "raw"
    report = coverage_module.evaluate_coverage(
        raw_db=raw_db,
        feature_db=feature_db,
        index_path=index_path,
        source_window_start=FIXTURE_DATE,
        source_window_end=FIXTURE_DATE,
    )
    assert report["coverage_status"] == "PASS", report.get("blockers")
    payload = build_bootstrap_marker(
        db_path=raw_db,
        coverage_report=report,
        source_index_path=index_path,
        source_index_hash=str(report.get("source_index_hash", "")),
        source_index_latest_date=FIXTURE_DATE,
        build_commit="test",
    )
    write_bootstrap_marker(payload, raw_db_path=raw_db)
    return report


class _RecordingImporter:
    """记录 argv 之后**委派给真 importer**：既证明编排传了什么口径，又不伪造结果。

    只记录不执行会让 raw 库停在旧日期，readiness 就永远不会成立——那样这条测试就
    只能断言半个性质（参数对了但链路不通），失去了"两个角色都在真跑"的证据。
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._real: object | None = None

    def _main(self, argv: list[str]) -> int:
        self.calls.append(list(argv))
        if self._real is None:
            self._real = _load_script("import_vendor_zip_to_delta", _IMPORTER_PATH)
        return self._real._main(argv)  # type: ignore[attr-defined]


def _main_with_fake_api(
    updater: object,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> tuple[int, dict[str, object]]:
    class _FakeTushareProvider:
        def __init__(self, **kwargs: object) -> None:
            pass

        def _resolve_pro_api(self) -> object:
            return _fake_pro()

        def _call_with_retry(self, fn: object) -> object:
            return fn()

    monkeypatch.setattr(updater, "TushareProvider", _FakeTushareProvider)
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    output: list[str] = []

    class _Stdout:
        def write(self, text: str) -> int:
            output.append(text)
            return len(text)

        def flush(self) -> None:
            return None

    monkeypatch.setattr(updater.sys, "stdout", _Stdout())
    exit_code = updater._main(argv)
    text = "".join(output)
    # 参数校验类失败（退出 2）在任何 summary 之前就返回，此时 stdout 为空是正常的。
    return exit_code, (json.loads(text) if text.strip() else {})


def _dual_run_argv(
    *,
    vendor_root: Path,
    index_path: Path,
    feature_db: Path,
    raw_db: Path,
    end_date: str = TARGET_DATE,
) -> list[str]:
    return [
        "--vendor-root",
        str(vendor_root),
        "--end-date",
        end_date,
        "--interval-sec",
        "0",
        "--index-path",
        str(index_path),
        "--sync-vendor-delta",
        str(feature_db),
        "--sync-vendor-delta-raw",
        str(raw_db),
    ]


def _prepared_layout(tmp_path: Path, coverage_module: object) -> dict[str, Path]:
    """搭好一套"已经建过 raw 基线"的夜间链路现场。"""
    vendor_root = _vendor_fixture(tmp_path)
    index_path = _full_daily_index(vendor_root)
    raw_db = tmp_path / "vendor_delta_raw" / "market_delta_raw.duckdb"
    feature_db = tmp_path / "vendor_delta" / "market_delta.duckdb"
    _bootstrap_raw_baseline(
        vendor_root=vendor_root,
        index_path=index_path,
        raw_db=raw_db,
        feature_db=feature_db,
        coverage_module=coverage_module,
    )
    return {
        "vendor_root": vendor_root,
        "index_path": index_path,
        "raw_db": raw_db,
        "feature_db": feature_db,
        "readiness": tmp_path / "runtime" / "nightly_data_ready.json",
    }


# ---------------------------------------------------------------------------
# §3/§4 RAW-1 / RAW-2：两个角色各自按显式口径推进
# ---------------------------------------------------------------------------


def test_both_delta_roles_receive_explicit_price_series_mode(
    updater: object,
    coverage_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RAW-1 / RAW-2：qfq 与 raw 都必须由编排**显式**传给 importer。

    隐式依赖 ``config/default.yaml`` 的默认值意味着"某天改个配置，raw 目标就被喂成
    qfq"，而这件事不会有任何一步报错——所以这里要求参数在 argv 里出现。
    """
    layout = _prepared_layout(tmp_path, coverage_module)
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(layout["readiness"]))
    recorder = _RecordingImporter()
    monkeypatch.setattr(updater, "_load_delta_importer", lambda: recorder)

    exit_code, summary = _main_with_fake_api(
        updater,
        monkeypatch,
        _dual_run_argv(
            vendor_root=layout["vendor_root"],
            index_path=layout["index_path"],
            feature_db=layout["feature_db"],
            raw_db=layout["raw_db"],
        ),
    )

    assert exit_code == 0, summary
    modes = {
        call[call.index("--delta-db-path") + 1]: call[call.index("--price-series-mode") + 1]
        for call in recorder.calls
    }
    assert modes[str(layout["feature_db"])] == "qfq"
    assert modes[str(layout["raw_db"])] == "raw"
    assert summary["feature_delta_sync"]["price_series_mode"] == "qfq"
    assert summary["execution_delta_sync"]["price_series_mode"] == "raw"
    assert summary["dual_delta_enabled"] is True


def test_sync_vendor_delta_raw_requires_the_feature_target(
    updater: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """raw 角色是同一事务的第二半，不许它单独启动。"""
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(tmp_path / "r.json"))
    exit_code, _ = _main_with_fake_api(
        updater,
        monkeypatch,
        [
            "--vendor-root",
            str(tmp_path),
            "--end-date",
            TARGET_DATE,
            "--interval-sec",
            "0",
            "--index-path",
            str(tmp_path / "index.json"),
            "--sync-vendor-delta-raw",
            str(tmp_path / "raw.duckdb"),
        ],
    )
    assert exit_code == 2


# ---------------------------------------------------------------------------
# §21 RAW incremental：因子漂移重写对 non-qfq 必须关闭
# ---------------------------------------------------------------------------


def test_raw_incremental_never_triggers_factor_drift_rewrite(tmp_path: Path) -> None:
    """raw 序列没有因子可漂移；这条通道必须显式关闭并如实上报。"""
    vendor_root = _vendor_fixture(tmp_path)
    index_path = _full_daily_index(vendor_root)
    raw_db = tmp_path / "raw.duckdb"
    _run_importer(
        [
            "--data-root",
            str(vendor_root),
            "--index-path",
            str(index_path),
            "--delta-db-path",
            str(raw_db),
            "--price-series-mode",
            "raw",
        ]
    )
    exit_code, report = _run_importer(
        [
            "--data-root",
            str(vendor_root),
            "--index-path",
            str(index_path),
            "--delta-db-path",
            str(raw_db),
            "--price-series-mode",
            "raw",
            "--incremental",
        ]
    )

    assert exit_code == 0
    assert report["price_series_mode"] == "raw"
    assert report["factor_drift_detection"] == "disabled_non_qfq_mode"
    assert report["drift_refreshed_symbol_count"] == 0
    assert report["drift_refreshed_symbols"] == []


def test_qfq_incremental_keeps_factor_drift_detection_enabled(tmp_path: Path) -> None:
    """对照组：qfq 侧的漂移检测不能被这次改动顺手关掉。"""
    vendor_root = _vendor_fixture(tmp_path)
    index_path = _full_daily_index(vendor_root)
    feature_db = tmp_path / "feature.duckdb"
    _run_importer(
        [
            "--data-root",
            str(vendor_root),
            "--index-path",
            str(index_path),
            "--delta-db-path",
            str(feature_db),
            "--price-series-mode",
            "qfq",
        ]
    )
    exit_code, report = _run_importer(
        [
            "--data-root",
            str(vendor_root),
            "--index-path",
            str(index_path),
            "--delta-db-path",
            str(feature_db),
            "--price-series-mode",
            "qfq",
            "--incremental",
        ]
    )

    assert exit_code == 0
    assert report["price_series_mode"] == "qfq"
    assert report["factor_drift_detection"] == "enabled"


# ---------------------------------------------------------------------------
# §5/§17 TX-4：raw 基线缺失 → 在增量之前 fail closed
# ---------------------------------------------------------------------------


def test_missing_raw_baseline_fails_before_incremental_bootstrap(
    updater: object,
    coverage_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RAW 库不存在时：非零退出、无 readiness，且**从未**调用过 raw 角色的增量导入。

    最后一条是关键。若只断言"没 readiness"，一个"先偷偷全量补导、再因为别的原因为
    失败"的实现也能通过——而那种实现已经在磁盘上留下了一份浅深度假基线。
    """
    layout = _prepared_layout(tmp_path, coverage_module)
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(layout["readiness"]))
    layout["raw_db"].unlink()
    recorder = _RecordingImporter()
    monkeypatch.setattr(updater, "_load_delta_importer", lambda: recorder)

    exit_code, summary = _main_with_fake_api(
        updater,
        monkeypatch,
        _dual_run_argv(
            vendor_root=layout["vendor_root"],
            index_path=layout["index_path"],
            feature_db=layout["feature_db"],
            raw_db=layout["raw_db"],
        ),
    )

    assert exit_code == 1
    assert summary["execution_delta_sync"]["updated"] is False
    assert summary["execution_delta_sync"]["reason"] == "raw_delta_baseline_missing"
    assert summary["readiness"]["written"] is False
    assert not layout["readiness"].exists()
    raw_paths = [call[call.index("--delta-db-path") + 1] for call in recorder.calls]
    assert str(layout["raw_db"]) not in raw_paths
    assert not layout["raw_db"].exists()


def test_missing_raw_marker_fails_closed(
    updater: object,
    coverage_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """库在但 marker 不在：同样 fail closed（"库存在"不等于"基线成立"）。"""
    layout = _prepared_layout(tmp_path, coverage_module)
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(layout["readiness"]))
    marker = layout["raw_db"].with_name("raw_delta_bootstrap.json")
    assert marker.exists()
    marker.unlink()

    exit_code, summary = _main_with_fake_api(
        updater,
        monkeypatch,
        _dual_run_argv(
            vendor_root=layout["vendor_root"],
            index_path=layout["index_path"],
            feature_db=layout["feature_db"],
            raw_db=layout["raw_db"],
        ),
    )

    assert exit_code == 1
    assert summary["execution_delta_sync"]["reason"] == "raw_delta_baseline_marker_unreadable"
    assert not layout["readiness"].exists()


# ---------------------------------------------------------------------------
# §9/§19 TX-1 / TX-2 / TX-3：事务原子性
# ---------------------------------------------------------------------------


def test_tx1_both_roles_ok_writes_v3_readiness(
    updater: object,
    coverage_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TX-1：ZIP/index + 两份 delta 全通过 → readiness v3 落盘且可消费。"""
    layout = _prepared_layout(tmp_path, coverage_module)
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(layout["readiness"]))

    exit_code, summary = _main_with_fake_api(
        updater,
        monkeypatch,
        _dual_run_argv(
            vendor_root=layout["vendor_root"],
            index_path=layout["index_path"],
            feature_db=layout["feature_db"],
            raw_db=layout["raw_db"],
        ),
    )

    assert exit_code == 0, summary
    assert summary["readiness"]["written"] is True
    payload = read_nightly_readiness()
    assert payload is not None
    assert payload["schema_version"] == 3
    assert payload["delta"]["role"] == "feature"
    assert payload["delta"]["price_series_mode"] == "qfq"
    assert payload["execution_delta"]["role"] == "execution"
    assert payload["execution_delta"]["price_series_mode"] == "raw"
    assert payload["execution_delta"]["latest_trade_date"] == TARGET_DATE
    assert payload["symbol_membership"]["membership_locked"] is True
    assert payload["raw_delta_baseline"]["ok"] is True
    assert check_nightly_readiness(expected_trade_date=TARGET_DATE).ready is True


def test_tx2_execution_failure_blocks_readiness(
    updater: object,
    coverage_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TX-2：feature 成功 + execution 失败 → 不发布 readiness。"""
    layout = _prepared_layout(tmp_path, coverage_module)
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(layout["readiness"]))
    real_sync = updater._sync_vendor_delta_role

    def _failing_execution(**kwargs: object) -> dict[str, object]:
        if kwargs.get("role") == updater.DELTA_ROLE_EXECUTION:
            return {"updated": False, "exit_code": 1, "reason": "simulated_import_failure"}
        return real_sync(**kwargs)

    monkeypatch.setattr(updater, "_sync_vendor_delta_role", _failing_execution)

    exit_code, summary = _main_with_fake_api(
        updater,
        monkeypatch,
        _dual_run_argv(
            vendor_root=layout["vendor_root"],
            index_path=layout["index_path"],
            feature_db=layout["feature_db"],
            raw_db=layout["raw_db"],
        ),
    )

    assert exit_code == 1
    assert summary["feature_delta_sync"]["updated"] is True
    assert summary["execution_delta_sync"]["updated"] is False
    assert summary["readiness"]["written"] is False
    assert not layout["readiness"].exists()


def test_tx3_feature_failure_blocks_readiness(
    updater: object,
    coverage_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TX-3：feature 失败 + execution 成功 → 同样不发布 readiness。"""
    layout = _prepared_layout(tmp_path, coverage_module)
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(layout["readiness"]))
    real_sync = updater._sync_vendor_delta_role

    def _failing_feature(**kwargs: object) -> dict[str, object]:
        if kwargs.get("role") == updater.DELTA_ROLE_FEATURE:
            return {"updated": False, "exit_code": 1, "reason": "simulated_import_failure"}
        return real_sync(**kwargs)

    monkeypatch.setattr(updater, "_sync_vendor_delta_role", _failing_feature)

    exit_code, summary = _main_with_fake_api(
        updater,
        monkeypatch,
        _dual_run_argv(
            vendor_root=layout["vendor_root"],
            index_path=layout["index_path"],
            feature_db=layout["feature_db"],
            raw_db=layout["raw_db"],
        ),
    )

    assert exit_code == 1
    assert summary["feature_delta_sync"]["updated"] is False
    assert summary["execution_delta_sync"]["updated"] is True
    assert summary["readiness"]["written"] is False
    assert not layout["readiness"].exists()


def test_legacy_delta_sync_field_shape_is_preserved(
    updater: object,
    coverage_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``delta_sync`` 的既有消费者按原形状继续工作（新增信息只进新键）。"""
    layout = _prepared_layout(tmp_path, coverage_module)
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(layout["readiness"]))

    exit_code, summary = _main_with_fake_api(
        updater,
        monkeypatch,
        _dual_run_argv(
            vendor_root=layout["vendor_root"],
            index_path=layout["index_path"],
            feature_db=layout["feature_db"],
            raw_db=layout["raw_db"],
        ),
    )

    assert exit_code == 0
    assert set(summary["delta_sync"]) <= {
        "updated",
        "exit_code",
        "reason",
        "import_report",
        "import_output",
    }
    assert summary["delta_sync"]["updated"] is True


# ---------------------------------------------------------------------------
# §20 Retry / 幂等
# ---------------------------------------------------------------------------


def test_retry_after_execution_failure_converges_without_duplicates(
    updater: object,
    coverage_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第一次 feature 成功 / raw 失败；第二次两半都成功 → v3 PASS，且无重复行。"""
    layout = _prepared_layout(tmp_path, coverage_module)
    monkeypatch.setenv("SA__NIGHTLY_READINESS_PATH", str(layout["readiness"]))
    real_sync = updater._sync_vendor_delta_role
    fail_execution = {"value": True}

    def _flaky(**kwargs: object) -> dict[str, object]:
        if kwargs.get("role") == updater.DELTA_ROLE_EXECUTION and fail_execution["value"]:
            return {"updated": False, "exit_code": 1, "reason": "simulated_transient"}
        return real_sync(**kwargs)

    monkeypatch.setattr(updater, "_sync_vendor_delta_role", _flaky)
    argv = _dual_run_argv(
        vendor_root=layout["vendor_root"],
        index_path=layout["index_path"],
        feature_db=layout["feature_db"],
        raw_db=layout["raw_db"],
    )

    first_exit, first = _main_with_fake_api(updater, monkeypatch, argv)
    assert first_exit == 1
    assert first["readiness"]["written"] is False
    assert not layout["readiness"].exists()

    fail_execution["value"] = False
    second_exit, second = _main_with_fake_api(updater, monkeypatch, argv)

    assert second_exit == 0, second
    assert second["readiness"]["written"] is True
    payload = read_nightly_readiness()
    assert payload is not None and payload["schema_version"] == 3

    for db_path, expected_mode in ((layout["feature_db"], "qfq"), (layout["raw_db"], "raw")):
        with duckdb.connect(str(db_path), read_only=True) as connection:
            duplicates = connection.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT symbol, date FROM daily_bars GROUP BY symbol, date HAVING COUNT(*) > 1
                )
                """
            ).fetchone()
            modes = {
                str(item[0])
                for item in connection.execute(
                    "SELECT DISTINCT price_series_mode FROM daily_bars"
                ).fetchall()
            }
            latest = str(connection.execute("SELECT MAX(date) FROM daily_bars").fetchone()[0])
        assert int(duplicates[0]) == 0, db_path
        assert modes == {expected_mode}, db_path
        assert latest == TARGET_DATE, db_path


# ---------------------------------------------------------------------------
# §11–§14 readiness 侧：口径门与成员锁步（直接打在 writer 上）
# ---------------------------------------------------------------------------


def _db(path: Path, *, rows: dict[str, tuple[str, ...]], mode: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [(symbol, item, 10.0, mode) for symbol, dates in rows.items() for item in dates]
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            """
            CREATE TABLE daily_bars (
                symbol VARCHAR, date DATE, close DOUBLE, price_series_mode VARCHAR
            )
            """
        )
        connection.executemany("INSERT INTO daily_bars VALUES (?, ?, ?, ?)", payload)
    return path


def _index(path: Path, *, symbols: tuple[str, ...], latest_date: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "symbols_total": len(symbols),
                "symbols": {symbol: {"latest_date": latest_date} for symbol in symbols},
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_dual(
    *,
    index_path: Path,
    feature_db: Path,
    execution_db: Path,
    out: Path,
    target: str = "2026-08-19",
    verify_raw_baseline: bool = False,
) -> Path:
    return write_nightly_readiness(
        target_trade_date=target,
        index_path=index_path,
        db_path=feature_db,
        execution_db_path=execution_db,
        path=out,
        verify_raw_baseline=verify_raw_baseline,
    )


def test_raw3_execution_db_in_qfq_mode_is_blocked(tmp_path: Path) -> None:
    index_path = _index(
        tmp_path / "index.json", symbols=("000001", "600000"), latest_date="2026-08-19"
    )
    feature_db = _db(
        tmp_path / "feature.duckdb",
        rows={"000001": ("2026-08-19",), "600000": ("2026-08-19",)},
        mode="qfq",
    )
    execution_db = _db(
        tmp_path / "execution.duckdb",
        rows={"000001": ("2026-08-19",), "600000": ("2026-08-19",)},
        mode="qfq",
    )

    with pytest.raises(ValueError, match="execution delta DB price_series_mode mismatch"):
        _write_dual(
            index_path=index_path,
            feature_db=feature_db,
            execution_db=execution_db,
            out=tmp_path / "ready.json",
        )
    assert not (tmp_path / "ready.json").exists()


def test_raw4_execution_db_mixed_mode_is_blocked(tmp_path: Path) -> None:
    index_path = _index(
        tmp_path / "index.json", symbols=("000001", "600000"), latest_date="2026-08-19"
    )
    feature_db = _db(
        tmp_path / "feature.duckdb",
        rows={"000001": ("2026-08-19",), "600000": ("2026-08-19",)},
        mode="qfq",
    )
    execution_db = _db(tmp_path / "execution.duckdb", rows={"000001": ("2026-08-19",)}, mode="raw")
    with duckdb.connect(str(execution_db)) as connection:
        connection.execute("INSERT INTO daily_bars VALUES ('600000', '2026-08-19', 10.0, 'qfq')")

    with pytest.raises(ValueError, match="price_series_mode mismatch"):
        _write_dual(
            index_path=index_path,
            feature_db=feature_db,
            execution_db=execution_db,
            out=tmp_path / "ready.json",
        )
    assert not (tmp_path / "ready.json").exists()


def test_raw5_feature_db_in_raw_mode_is_blocked(tmp_path: Path) -> None:
    index_path = _index(
        tmp_path / "index.json", symbols=("000001", "600000"), latest_date="2026-08-19"
    )
    feature_db = _db(
        tmp_path / "feature.duckdb",
        rows={"000001": ("2026-08-19",), "600000": ("2026-08-19",)},
        mode="raw",
    )
    execution_db = _db(
        tmp_path / "execution.duckdb",
        rows={"000001": ("2026-08-19",), "600000": ("2026-08-19",)},
        mode="raw",
    )

    with pytest.raises(ValueError, match="feature delta DB price_series_mode mismatch"):
        _write_dual(
            index_path=index_path,
            feature_db=feature_db,
            execution_db=execution_db,
            out=tmp_path / "ready.json",
        )
    assert not (tmp_path / "ready.json").exists()


def test_tx5_latest_date_mismatch_blocks_readiness(tmp_path: Path) -> None:
    """TX-5：两份 delta 的最新交易日不一致 → 不发布。"""
    index_path = _index(
        tmp_path / "index.json", symbols=("000001", "600000"), latest_date="2026-08-19"
    )
    feature_db = _db(
        tmp_path / "feature.duckdb",
        rows={"000001": ("2026-08-19",), "600000": ("2026-08-19",)},
        mode="qfq",
    )
    execution_db = _db(
        tmp_path / "execution.duckdb",
        rows={"000001": ("2026-08-18",), "600000": ("2026-08-18",)},
        mode="raw",
    )

    with pytest.raises(ValueError, match="execution delta DB latest date mismatch"):
        _write_dual(
            index_path=index_path,
            feature_db=feature_db,
            execution_db=execution_db,
            out=tmp_path / "ready.json",
        )
    assert not (tmp_path / "ready.json").exists()


def test_tx6_same_count_different_membership_blocks_readiness(tmp_path: Path) -> None:
    """TX-6：两份 delta 目标日都"有 2 只票"，但成员不同 → 必须拦下。

    旧口径只比 ``symbols_on_target_date`` 的计数，这个夹具会**通过**旧门（2 == 2）。
    """
    index_path = _index(
        tmp_path / "index.json", symbols=("000001", "600000"), latest_date="2026-08-19"
    )
    feature_db = _db(
        tmp_path / "feature.duckdb",
        rows={"000001": ("2026-08-19",), "600000": ("2026-08-19",)},
        mode="qfq",
    )
    execution_db = _db(
        tmp_path / "execution.duckdb",
        rows={"000001": ("2026-08-19",), "600001": ("2026-08-19",)},
        mode="raw",
    )
    legacy_out = tmp_path / "legacy-ready.json"
    # 对照：单 delta 的 v2 路径在同样数据上是放行的——这正是 P1 要补的缺口。
    write_nightly_readiness(
        target_trade_date="2026-08-19",
        index_path=index_path,
        db_path=feature_db,
        path=legacy_out,
    )
    legacy = json.loads(legacy_out.read_text(encoding="utf-8"))
    assert legacy["schema_version"] == 2
    assert legacy["delta"]["symbols_on_target_date"] == 2

    with pytest.raises(ValueError) as excinfo:
        _write_dual(
            index_path=index_path,
            feature_db=feature_db,
            execution_db=execution_db,
            out=tmp_path / "ready.json",
        )
    # execution 的成员数是 2（与 feature 相同），缺的是**成员身份**：600000 被 600001 顶掉。
    assert "missing" in str(excinfo.value)
    assert "600000" in str(excinfo.value)
    assert not (tmp_path / "ready.json").exists()


def test_feature_extra_symbol_missing_from_execution_blocks_readiness(tmp_path: Path) -> None:
    """包含链的第二段：feature 有而 execution 没有的票 = label 算不出来，必须拦。"""
    index_path = _index(tmp_path / "index.json", symbols=("000001",), latest_date="2026-08-19")
    feature_db = _db(
        tmp_path / "feature.duckdb",
        rows={"000001": ("2026-08-19",), "600000": ("2026-08-19",)},
        mode="qfq",
    )
    execution_db = _db(tmp_path / "execution.duckdb", rows={"000001": ("2026-08-19",)}, mode="raw")

    with pytest.raises(ValueError, match="execution delta is missing") as excinfo:
        _write_dual(
            index_path=index_path,
            feature_db=feature_db,
            execution_db=execution_db,
            out=tmp_path / "ready.json",
        )
    assert "600000" in str(excinfo.value)
    assert not (tmp_path / "ready.json").exists()


def test_v3_payload_requires_execution_block_to_be_ok(tmp_path: Path) -> None:
    """v3 文件缺 execution 块 / 块不 ok → gate 一律不 ready（不许按 v2 降级放行）。"""
    ready_path = tmp_path / "nightly_data_ready.json"
    base = {
        "schema_version": 3,
        "target_trade_date": "2026-08-19",
        "daily": {"ok": True},
        "index": {"ok": True},
        "delta": {"ok": True},
        "symbol_membership": {"membership_locked": True},
        "raw_delta_baseline": {"ok": True},
    }
    ready_path.write_text(json.dumps(base), encoding="utf-8")
    assert check_nightly_readiness(expected_trade_date="2026-08-19", path=ready_path).ready is False

    ok_with_execution = {**base, "execution_delta": {"ok": False}}
    ready_path.write_text(json.dumps(ok_with_execution), encoding="utf-8")
    assert check_nightly_readiness(expected_trade_date="2026-08-19", path=ready_path).ready is False

    healthy = {**base, "execution_delta": {"ok": True}}
    ready_path.write_text(json.dumps(healthy), encoding="utf-8")
    assert check_nightly_readiness(expected_trade_date="2026-08-19", path=ready_path).ready is True


def test_v2_payload_without_execution_block_still_reads(tmp_path: Path) -> None:
    """§14 向后兼容：单 delta 的 v2 文件仍然可读、仍然 ready。"""
    ready_path = tmp_path / "nightly_data_ready.json"
    ready_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "target_trade_date": "2026-08-19",
                "daily": {"ok": True},
                "index": {"ok": True},
                "delta": {"ok": True},
            }
        ),
        encoding="utf-8",
    )
    assert check_nightly_readiness(expected_trade_date="2026-08-19", path=ready_path).ready is True


def test_dual_write_requires_certified_raw_baseline(tmp_path: Path) -> None:
    """v3 写入默认要 marker：没有它的 raw 库不算基线（release 级 fail closed）。"""
    index_path = _index(tmp_path / "index.json", symbols=("000001",), latest_date="2026-08-19")
    feature_db = _db(tmp_path / "feature.duckdb", rows={"000001": ("2026-08-19",)}, mode="qfq")
    execution_db = _db(tmp_path / "execution.duckdb", rows={"000001": ("2026-08-19",)}, mode="raw")

    with pytest.raises(ValueError, match="not a certified RAW baseline"):
        _write_dual(
            index_path=index_path,
            feature_db=feature_db,
            execution_db=execution_db,
            out=tmp_path / "ready.json",
            verify_raw_baseline=True,
        )
    assert not (tmp_path / "ready.json").exists()


# ---------------------------------------------------------------------------
# §24 配置：生产绝对路径不进 Python 默认值，走既有环境变量约定
# ---------------------------------------------------------------------------


def test_dual_delta_paths_resolve_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NAS 上的三个环境变量必须能解析到两个角色各自的位置与口径。

    这条测试防的是"把生产绝对路径硬编码进 Python 默认值"——那样本机/CI 与 NAS 会共享
    同一个错误默认值，而错误只在生产上显现。
    """
    from stock_analyzer.alpha_v2.dual_price_series import resolve_market_dbs
    from stock_analyzer.config import load_config

    feature_db = "/app/artifacts/vendor_delta/market_delta.duckdb"
    execution_db = "/app/artifacts/vendor_delta_raw/market_delta_raw.duckdb"
    monkeypatch.setenv("SA__ALPHA_V2__FEATURE_MARKET_DB", feature_db)
    monkeypatch.setenv("SA__ALPHA_V2__EXECUTION_MARKET_DB", execution_db)
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")

    config = load_config()
    assert config.alpha_v2.feature_market_db == feature_db
    assert config.alpha_v2.execution_market_db == execution_db
    assert config.evolution.execution_spec.price_series_mode == "raw"

    resolution = resolve_market_dbs(config)
    assert resolution.feature_db == feature_db
    assert resolution.execution_db == execution_db
    assert resolution.dual_source is True
    assert resolution.same_db is False


def test_dual_delta_paths_have_no_hardcoded_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未配置时 execution 侧必须留空（fail closed），而不是某个看似可用的默认路径。

    给默认路径等于让生产悄悄读到一份没被任何人认证过的序列；留空则让"没有可证的 raw
    库"这件事在 preflight / readiness 上直接暴露。
    """
    from stock_analyzer.alpha_v2.dual_price_series import resolve_market_dbs
    from stock_analyzer.config import load_config

    monkeypatch.delenv("SA__ALPHA_V2__FEATURE_MARKET_DB", raising=False)
    monkeypatch.delenv("SA__ALPHA_V2__EXECUTION_MARKET_DB", raising=False)

    config = load_config()
    assert config.alpha_v2.feature_market_db == ""
    assert config.alpha_v2.execution_market_db == ""

    resolution = resolve_market_dbs(config)
    assert resolution.execution_db == ""
    assert resolution.execution_source == "unset"


def test_readiness_rejects_same_db_for_both_roles(tmp_path: Path) -> None:
    """§2：两份 delta 必须物理独立。同一份文件承担两个角色时，成员锁步与口径门都会
    退化成"自己比自己"（恒真）——readiness 看起来通过，却什么都没证明。"""
    index_path = _index(tmp_path / "index.json", symbols=("000001",), latest_date="2026-08-19")
    # 一份同时"是 qfq 又是 raw"的库在生产里不存在；这里用 raw 库冒充两个角色，
    # 期望的是**在比较之前**就被路径同一性拦下，而不是靠口径门偶然拦下。
    shared_db = _db(tmp_path / "shared.duckdb", rows={"000001": ("2026-08-19",)}, mode="raw")

    with pytest.raises(ValueError, match="physically separate"):
        _write_dual(
            index_path=index_path,
            feature_db=shared_db,
            execution_db=shared_db,
            out=tmp_path / "ready.json",
        )
    assert not (tmp_path / "ready.json").exists()


def test_coverage_validator_rejects_same_db_for_both_roles(
    coverage_module: object, tmp_path: Path
) -> None:
    """覆盖校验器同样要拦：同一份库做 raw 与 feature 时，所有比较都是自反的。"""
    rows = {"600000": ("2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09", "2025-01-10")}
    shared_db = _db(tmp_path / "shared.duckdb", rows=rows, mode="raw")
    index_path = _index(tmp_path / "index.json", symbols=("600000",), latest_date="2025-01-10")

    report = coverage_module.evaluate_coverage(
        raw_db=shared_db,
        feature_db=shared_db,
        index_path=index_path,
        source_window_start="2025-01-06",
        source_window_end="2025-01-10",
        skip_price_mode_certification=True,
    )

    assert report["coverage_status"] == "BLOCKED"
    assert any(item.startswith("raw_and_feature_db_paths_identical") for item in report["blockers"])
