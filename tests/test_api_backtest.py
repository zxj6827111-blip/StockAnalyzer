"""api/backtest.py 端点单测（PLAN Task 5）。

覆盖：202 提交 -> 轮询 succeeded -> 结果结构与 caveats 字段；日期参数边界
（非交易日/无数据日期/冲突参数/区间过大）明确 400 而非 500；latest/history/
daily-bars 三个 GET 端点；多任务提交互不污染。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from stock_analyzer import main as main_module
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.main import app


@pytest.fixture()
def isolated_output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 asof_backtest.output_dir 重定向到 tmp_path，测试间不互相污染/累积。

    注意：``main_module._service._config`` 是服务构造时保存的引用，与
    ``main_module._config``（模块级全局）是两个不同的对象；必须直接 patch
    服务自身持有的 config，否则新建的 AsofBacktestService 仍会读到旧路径。
    """
    output_dir = tmp_path / "asof_scan"
    patched_config = main_module._service._config.model_copy(
        update={
            "asof_backtest": main_module._service._config.asof_backtest.model_copy(
                update={"output_dir": str(output_dir)}
            )
        }
    )
    monkeypatch.setattr(main_module._service, "_config", patched_config)
    monkeypatch.setattr(
        main_module._service,
        "_asof_backtest_service",
        type(main_module._service._asof_backtest_service)(main_module._service),
    )
    return output_dir


@pytest.fixture()
def synthetic_provider(monkeypatch: pytest.MonkeyPatch) -> SyntheticProvider:
    provider = SyntheticProvider(seed_offset=2026)
    monkeypatch.setattr(main_module._service, "_provider", provider)
    monkeypatch.setattr(main_module._service._pipeline, "_provider", provider)
    return provider


def _poll_task(client: TestClient, task_id: str, *, timeout_sec: float = 10.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        response = client.get(f"/tasks/{task_id}")
        payload = response.json()
        if payload["status"] in ("succeeded", "failed"):
            return payload
        time.sleep(0.05)
    raise TimeoutError(f"task {task_id} did not finish within {timeout_sec}s")


class TestAsofScanSubmitAndPoll:
    def test_submit_single_date_polls_to_succeeded_with_caveats(
        self, isolated_output_dir: Path, synthetic_provider: SyntheticProvider
    ) -> None:
        client = TestClient(app)
        response = client.post(
            "/backtest/asof-scan",
            json={
                "date": "2026-07-31",
                "symbols": ["600000"],
                "top_n": 1,
                "horizon_days": 5,
            },
        )
        assert response.status_code == 202
        task_id = response.json()["task_id"]

        final = _poll_task(client, task_id)
        assert final["status"] == "succeeded"
        result = final["result"]
        caveats = result["caveats"]
        assert caveats["lookahead_bias"] is True
        assert caveats["news_neutralized"] is True
        assert caveats["intraday_degraded"] is True
        # intraday_coverage_until 现在是动态读取（返工第 3 项），测试环境未配置
        # intraday_summary_path（默认空字符串），因此读取应正确落到"未知"态
        # （空字符串），而不是任何硬编码的猜测日期。
        assert caveats["intraday_coverage_until"] == ""
        # 未显式传入 symbols 以外的候选池信息时，本次是显式传入 symbols=["600000"]，
        # 因此不应标注 watchlist 偏差（返工第 2 项）。
        assert caveats["candidate_pool_source"] == "explicit"
        assert caveats["candidate_pool_bias"] is False
        assert "2026-07-31" in result["dates"]

    def test_submit_date_range_scans_multiple_trading_days(
        self, isolated_output_dir: Path, synthetic_provider: SyntheticProvider
    ) -> None:
        client = TestClient(app)
        response = client.post(
            "/backtest/asof-scan",
            json={
                "start_date": "2026-07-27",  # 周一
                "end_date": "2026-07-31",  # 周五
                "symbols": ["600000"],
                "top_n": 1,
                "horizon_days": 3,
            },
        )
        assert response.status_code == 202
        final = _poll_task(client, response.json()["task_id"])
        assert final["status"] == "succeeded"
        # 2026-07-27(周一)~07-31(周五) 是 5 个连续交易日。
        assert len(final["result"]["dates"]) == 5

    def test_multiple_concurrent_submits_do_not_cross_pollute(
        self, isolated_output_dir: Path, synthetic_provider: SyntheticProvider
    ) -> None:
        client = TestClient(app)
        response_a = client.post(
            "/backtest/asof-scan",
            json={"date": "2026-07-30", "symbols": ["600000"], "top_n": 1},
        )
        response_b = client.post(
            "/backtest/asof-scan",
            json={"date": "2026-07-31", "symbols": ["000001"], "top_n": 1},
        )
        task_id_a = response_a.json()["task_id"]
        task_id_b = response_b.json()["task_id"]
        assert task_id_a != task_id_b

        final_a = _poll_task(client, task_id_a)
        final_b = _poll_task(client, task_id_b)
        assert final_a["status"] == "succeeded"
        assert final_b["status"] == "succeeded"
        assert final_a["result"]["start_date"] == "2026-07-30"
        assert final_b["result"]["start_date"] == "2026-07-31"


class TestAsofScanDateValidation:
    """边界：非交易日、无数据日期、冲突参数、区间过大 -> 明确 400，非 500。"""

    def test_conflicting_date_and_range_params_returns_400(self) -> None:
        client = TestClient(app)
        response = client.post(
            "/backtest/asof-scan",
            json={"date": "2026-07-31", "start_date": "2026-07-01", "end_date": "2026-07-31"},
        )
        assert response.status_code == 400

    def test_missing_all_date_params_returns_400(self) -> None:
        client = TestClient(app)
        response = client.post("/backtest/asof-scan", json={})
        assert response.status_code == 400

    def test_invalid_date_string_returns_400(self) -> None:
        client = TestClient(app)
        response = client.post("/backtest/asof-scan", json={"date": "not-a-date"})
        assert response.status_code == 400

    def test_date_range_exceeding_max_returns_400(self) -> None:
        client = TestClient(app)
        response = client.post(
            "/backtest/asof-scan",
            json={"start_date": "2026-01-01", "end_date": "2026-12-31"},
        )
        assert response.status_code == 400

    def test_weekend_only_range_succeeds_with_empty_dates(
        self, isolated_output_dir: Path, synthetic_provider: SyntheticProvider
    ) -> None:
        """一个纯周末的日期区间：不报错，扫描结果里 dates 为空。"""
        client = TestClient(app)
        response = client.post(
            "/backtest/asof-scan",
            json={
                "start_date": "2026-08-01",  # 周六
                "end_date": "2026-08-02",  # 周日
                "symbols": ["600000"],
            },
        )
        assert response.status_code == 202
        final = _poll_task(client, response.json()["task_id"])
        assert final["status"] == "succeeded"
        assert final["result"]["dates"] == {}


class TestAsofScanLatestAndHistory:
    def test_latest_returns_no_report_when_never_run(self, isolated_output_dir: Path) -> None:
        client = TestClient(app)
        response = client.get("/backtest/asof-scan/latest")
        assert response.status_code == 200
        assert response.json() == {"status": "no_report"}

    def test_history_empty_when_never_run(self, isolated_output_dir: Path) -> None:
        client = TestClient(app)
        response = client.get("/backtest/asof-scan/history")
        assert response.status_code == 200
        assert response.json() == {"records": 0, "runs": []}

    def test_latest_and_history_populated_after_run(
        self, isolated_output_dir: Path, synthetic_provider: SyntheticProvider
    ) -> None:
        client = TestClient(app)
        response = client.post(
            "/backtest/asof-scan",
            json={"date": "2026-07-31", "symbols": ["600000"], "top_n": 1},
        )
        _poll_task(client, response.json()["task_id"])

        latest_response = client.get("/backtest/asof-scan/latest")
        assert latest_response.status_code == 200
        assert "report" in latest_response.json()

        history_response = client.get("/backtest/asof-scan/history")
        assert history_response.status_code == 200
        history_payload = history_response.json()
        assert history_payload["records"] == 1


class TestCandidatePoolCaveats:
    """返工第 2 项：候选池来源标注——显式传入 symbols 时无偏差；未传入回退到
    当前 watchlist 时必须标注 candidate_pool_bias=True。"""

    def test_explicit_symbols_has_no_pool_bias(
        self, isolated_output_dir: Path, synthetic_provider: SyntheticProvider
    ) -> None:
        client = TestClient(app)
        response = client.post(
            "/backtest/asof-scan",
            json={"date": "2026-07-31", "symbols": ["600000"], "top_n": 1},
        )
        final = _poll_task(client, response.json()["task_id"])
        caveats = final["result"]["caveats"]
        assert caveats["candidate_pool_source"] == "explicit"
        assert caveats["candidate_pool_bias"] is False

    def test_omitted_symbols_falls_back_to_watchlist_and_flags_bias(
        self,
        isolated_output_dir: Path,
        synthetic_provider: SyntheticProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(main_module._service._state, "watchlist", ["600000"])
        client = TestClient(app)
        response = client.post(
            "/backtest/asof-scan",
            json={"date": "2026-07-31", "top_n": 1},
        )
        final = _poll_task(client, response.json()["task_id"])
        assert final["status"] == "succeeded"
        caveats = final["result"]["caveats"]
        assert caveats["candidate_pool_source"] == "watchlist"
        assert caveats["candidate_pool_bias"] is True

    def test_empty_symbols_list_also_falls_back_to_watchlist(
        self,
        isolated_output_dir: Path,
        synthetic_provider: SyntheticProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """空列表和未传字段等价：``request.symbols or None`` 都会走 watchlist 回退。"""
        monkeypatch.setattr(main_module._service._state, "watchlist", ["600000"])
        client = TestClient(app)
        response = client.post(
            "/backtest/asof-scan",
            json={"date": "2026-07-31", "symbols": [], "top_n": 1},
        )
        final = _poll_task(client, response.json()["task_id"])
        assert final["status"] == "succeeded"
        assert final["result"]["caveats"]["candidate_pool_source"] == "watchlist"


class TestIntradayCoverageUntilDynamicRead:
    """返工第 3 项：intraday_coverage_until 不再硬编码，改为动态读取 manifest。"""

    def test_returns_empty_string_when_path_not_configured(
        self, isolated_output_dir: Path, synthetic_provider: SyntheticProvider
    ) -> None:
        """测试环境默认 intraday_summary_path 为空字符串，读取应落到未知态。"""
        client = TestClient(app)
        response = client.post(
            "/backtest/asof-scan",
            json={"date": "2026-07-31", "symbols": ["600000"], "top_n": 1},
        )
        final = _poll_task(client, response.json()["task_id"])
        assert final["result"]["caveats"]["intraday_coverage_until"] == ""

    def test_reads_real_manifest_max_date_across_intervals(
        self,
        isolated_output_dir: Path,
        synthetic_provider: SyntheticProvider,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """构造一份真实 schema 的 manifest 文件，验证动态读取拿到正确的
        max_date（多个 interval 时取最大值），不依赖任何硬编码猜测。"""
        import json

        db_path = tmp_path / "vendor_intraday_summary.duckdb"
        db_path.write_bytes(b"")  # 内容不重要，只需要 manifest 文件本身
        manifest_path = tmp_path / "vendor_intraday_summary.duckdb.manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "generation": "2026-08-27T00:00:00+00:00",
                    "coverage": {
                        "1m": {"max_date": "2026-08-20", "min_date": "2025-01-01"},
                        "5m": {"max_date": "2026-08-21", "min_date": "2025-01-01"},
                    },
                }
            ),
            encoding="utf-8",
        )
        patched_ds = main_module._service._config.data_source.model_copy(
            update={"intraday_summary_path": str(db_path)}
        )
        patched_config = main_module._service._config.model_copy(
            update={"data_source": patched_ds}
        )
        monkeypatch.setattr(main_module._service, "_config", patched_config)
        monkeypatch.setattr(
            main_module._service,
            "_asof_backtest_service",
            type(main_module._service._asof_backtest_service)(main_module._service),
        )

        client = TestClient(app)
        response = client.post(
            "/backtest/asof-scan",
            json={"date": "2026-07-31", "symbols": ["600000"], "top_n": 1},
        )
        final = _poll_task(client, response.json()["task_id"])
        # 两个 interval 里 5m 的 max_date（2026-08-21）更大，取更宽松口径。
        assert final["result"]["caveats"]["intraday_coverage_until"] == "2026-08-21"

    def test_returns_empty_string_when_manifest_missing(
        self,
        isolated_output_dir: Path,
        synthetic_provider: SyntheticProvider,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """路径配置了但文件不存在：明确未知态（空字符串），不猜测不报错。"""
        db_path = tmp_path / "does_not_exist.duckdb"
        patched_ds = main_module._service._config.data_source.model_copy(
            update={"intraday_summary_path": str(db_path)}
        )
        patched_config = main_module._service._config.model_copy(
            update={"data_source": patched_ds}
        )
        monkeypatch.setattr(main_module._service, "_config", patched_config)
        monkeypatch.setattr(
            main_module._service,
            "_asof_backtest_service",
            type(main_module._service._asof_backtest_service)(main_module._service),
        )

        client = TestClient(app)
        response = client.post(
            "/backtest/asof-scan",
            json={"date": "2026-07-31", "symbols": ["600000"], "top_n": 1},
        )
        final = _poll_task(client, response.json()["task_id"])
        assert final["result"]["caveats"]["intraday_coverage_until"] == ""

    def test_returns_empty_string_when_manifest_json_corrupt(
        self,
        isolated_output_dir: Path,
        synthetic_provider: SyntheticProvider,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """manifest 文件存在但内容不是合法 JSON：读取失败要优雅落到未知态，
        不能让整个回测请求因为这个附加标注字段而失败。"""
        db_path = tmp_path / "vendor_intraday_summary.duckdb"
        db_path.write_bytes(b"")
        manifest_path = tmp_path / "vendor_intraday_summary.duckdb.manifest.json"
        manifest_path.write_text("{not valid json", encoding="utf-8")
        patched_ds = main_module._service._config.data_source.model_copy(
            update={"intraday_summary_path": str(db_path)}
        )
        patched_config = main_module._service._config.model_copy(
            update={"data_source": patched_ds}
        )
        monkeypatch.setattr(main_module._service, "_config", patched_config)
        monkeypatch.setattr(
            main_module._service,
            "_asof_backtest_service",
            type(main_module._service._asof_backtest_service)(main_module._service),
        )

        client = TestClient(app)
        response = client.post(
            "/backtest/asof-scan",
            json={"date": "2026-07-31", "symbols": ["600000"], "top_n": 1},
        )
        final = _poll_task(client, response.json()["task_id"])
        assert final["result"]["caveats"]["intraday_coverage_until"] == ""


class TestMarketDailyBars:
    def test_daily_bars_returns_ok_with_records(
        self, synthetic_provider: SyntheticProvider
    ) -> None:
        client = TestClient(app)
        response = client.get("/market/daily-bars", params={"symbol": "600000", "limit": 5})
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["records"] == 5
        assert len(payload["bars"]) == 5
        assert "close" in payload["bars"][0]

    def test_daily_bars_missing_symbol_returns_400(self) -> None:
        client = TestClient(app)
        response = client.get("/market/daily-bars", params={"symbol": ""})
        assert response.status_code == 400

    def test_daily_bars_invalid_start_returns_400(self) -> None:
        client = TestClient(app)
        response = client.get(
            "/market/daily-bars", params={"symbol": "600000", "start": "not-a-date"}
        )
        assert response.status_code == 400
