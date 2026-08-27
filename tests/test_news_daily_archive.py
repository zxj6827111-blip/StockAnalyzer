"""Task 1 回归测试：M7 实时新闻按日归档 + 幂等 + 滚动上限 + akshare 抓取失败降级。

覆盖点：
1. run_m7_live_news_sync 会在保留原滚动文件的同时，生成按日归档 JSONL，且每行可解析；
2. 同一天重复调用（mock 掉 _collect_live_m7_news_records），归档文件按 event_id 去重、幂等；
3. 原滚动文件仍受 m7_live_news_artifact_max_records 上限约束（此处压到很小便于验证）；
4. akshare stock_news_em 抛出（模拟 ArrowInvalid）时，_fetch_symbol_live_news 捕获异常、
   降级返回空并记录 live_news_fetch_failed 审计事件，整条链路不崩溃。
"""

from __future__ import annotations

import json
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import cast

from pytest import fixture

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.runtime import service as runtime_service_module
from stock_analyzer.runtime.service import StockAnalyzerService


def _load_test_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.notifications.primary = "console"
    config.notifications.backup = "console"
    config.notification_filter.enabled = False
    config.training.artifact_path = str(
        Path(tempfile.gettempdir())
        / f"stock_analyzer_news_daily_archive_{time.time_ns()}"
        / "nonexistent_test_model.json"
    )
    return config


def _new_service(config: StockAnalyzerConfig) -> StockAnalyzerService:
    provider = SyntheticProvider(seed_offset=4102)
    original_build_runtime_provider = runtime_service_module.build_runtime_provider
    original_build_realtime_runtime_provider = (
        runtime_service_module.build_realtime_runtime_provider
    )
    original_build_market_depth_provider = runtime_service_module.build_market_depth_provider
    try:
        runtime_service_module.build_runtime_provider = (
            lambda config, synthetic_seed=2026: provider
        )
        runtime_service_module.build_realtime_runtime_provider = (
            lambda config, synthetic_seed=2026, timezone="Asia/Shanghai": provider
        )
        runtime_service_module.build_market_depth_provider = lambda config: None
        service = StockAnalyzerService(config=config)
    finally:
        runtime_service_module.build_runtime_provider = original_build_runtime_provider
        runtime_service_module.build_realtime_runtime_provider = (
            original_build_realtime_runtime_provider
        )
        runtime_service_module.build_market_depth_provider = original_build_market_depth_provider
    service._provider = provider  # noqa: SLF001
    service._pipeline._provider = provider  # noqa: SLF001
    service._realtime_provider = provider  # noqa: SLF001
    if service._realtime_pipeline is not None:
        service._realtime_pipeline._provider = provider  # noqa: SLF001
    service._refresh_runtime_state_from_disk_if_changed = lambda: None  # noqa: SLF001
    return service


@fixture()
def service(tmp_path: Path) -> StockAnalyzerService:
    config = _load_test_config()
    svc = _new_service(config)
    # 把 evolution 相关路径都指向临时目录，避免污染仓库 artifacts/。
    svc._evolution_project_root = tmp_path  # noqa: SLF001
    svc._config.evolution.m7_news_records_path = "m7_news_latest.jsonl"
    svc._config.evolution.m7_news_daily_archive_dir = "news_daily"
    # 固定关注池，避免依赖真实 watchlist 解析。
    svc._config.evolution.m7_live_news_max_symbols = 8
    return svc


def _fake_record(event_id: str, symbol: str, published_at: str) -> dict[str, object]:
    return {
        "event_id": event_id,
        "symbol": symbol,
        "headline": f"headline-{event_id}",
        "content": f"content-{event_id}",
        "published_at": published_at,
        "source": "unit-test",
        "url": "",
        "sentiment": 0.5,
        "llm_sentiment": 0.5,
        "cost": 0.2,
    }


def _patch_collect(
    service: StockAnalyzerService,
    records: list[dict[str, object]],
) -> None:
    """替换实际网络抓取，返回构造好的假记录。"""

    def _fake_collect(
        *,
        symbols: list[str],
        now: datetime,
        max_age_hours: float,
        per_symbol_limit: int,
        force_refresh: bool,
        enable_ai_review: bool,
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        summary = {
            "provider": "akshare_em",
            "symbol_count": len(symbols),
            "fetched_symbols": len(symbols),
            "raw_items": len(records),
            "records": len(records),
            "ai_review": {
                "enabled": enable_ai_review,
                "attempted": 0,
                "succeeded": 0,
                "failed": 0,
            },
            "errors": [],
        }
        return [dict(item) for item in records], summary

    service._news_service._collect_live_m7_news_records = _fake_collect  # noqa: SLF001
    # run_m7_live_news_sync 内部通过 service._collect_live_m7_news_records 调用（主服务副本），
    # 因此这里同时覆盖主服务上的实现，确保 mock 生效。
    service._collect_live_m7_news_records = _fake_collect  # noqa: SLF001


def _resolve_archive_path(service: StockAnalyzerService, day: str) -> Path:
    archive_dir = cast(
        Path,
        service._news_service._resolve_m7_news_daily_archive_dir(),  # noqa: SLF001
    )
    return archive_dir / f"{day}.jsonl"


def test_daily_archive_file_generated_and_valid_jsonl(service: StockAnalyzerService) -> None:
    now = datetime(2026, 8, 27, 16, 30, 0)
    records = [
        _fake_record("evt-a", "600000", "2026-08-27 09:30:00"),
        _fake_record("evt-b", "000001", "2026-08-27 10:00:00"),
    ]
    _patch_collect(service, records)

    report = service.run_m7_live_news_sync(symbols=["600000", "000001"], timestamp=now)

    assert report["status"] == "ok"
    archive_meta = report["daily_archive"]
    assert isinstance(archive_meta, dict)
    assert archive_meta["enabled"] is True
    assert archive_meta["date"] == "2026-08-27"
    assert archive_meta["written_records"] == 2

    archive_path = _resolve_archive_path(service, "2026-08-27")
    assert archive_path.exists(), "按日归档文件应生成在预期路径"

    lines = archive_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    parsed_ids = set()
    for line in lines:
        parsed = json.loads(line)  # 每行必须是合法 JSON
        parsed_ids.add(parsed["event_id"])
    assert parsed_ids == {"evt-a", "evt-b"}


def test_daily_archive_is_idempotent_on_same_day(service: StockAnalyzerService) -> None:
    now = datetime(2026, 8, 27, 16, 30, 0)
    records = [
        _fake_record("evt-a", "600000", "2026-08-27 09:30:00"),
        _fake_record("evt-b", "000001", "2026-08-27 10:00:00"),
    ]
    _patch_collect(service, records)

    service.run_m7_live_news_sync(symbols=["600000", "000001"], timestamp=now)
    # 同一天第二次运行（相同 event_id），归档不应重复写入。
    report2 = service.run_m7_live_news_sync(symbols=["600000", "000001"], timestamp=now)

    archive_path = _resolve_archive_path(service, "2026-08-27")
    lines = archive_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2, "同日重复运行相同 event_id 不应导致归档条数增长"
    ids = [json.loads(line)["event_id"] for line in lines]
    assert sorted(ids) == ["evt-a", "evt-b"]
    assert report2["daily_archive"]["written_records"] == 2

    # 第三次追加一条新记录，归档应合并去重后变为 3 条。
    records_with_new = records + [_fake_record("evt-c", "600519", "2026-08-27 11:00:00")]
    _patch_collect(service, records_with_new)
    service.run_m7_live_news_sync(symbols=["600000", "000001", "600519"], timestamp=now)
    lines_after = archive_path.read_text(encoding="utf-8").splitlines()
    ids_after = sorted(json.loads(line)["event_id"] for line in lines_after)
    assert ids_after == ["evt-a", "evt-b", "evt-c"]


def test_rolling_artifact_respects_max_records_cap(service: StockAnalyzerService) -> None:
    # 把滚动上限压到 3，验证原有滚动文件行为不受归档改造影响。
    service._config.evolution.m7_live_news_artifact_max_records = 3  # noqa: SLF001
    now = datetime(2026, 8, 27, 16, 30, 0)
    records = [
        _fake_record(f"evt-{idx}", "600000", f"2026-08-27 09:{idx:02d}:00")
        for idx in range(10)
    ]
    _patch_collect(service, records)

    report = service.run_m7_live_news_sync(symbols=["600000"], timestamp=now)

    assert report["persisted_records"] == 3, "滚动文件应受 max_records=3 上限约束"
    artifact_path = cast(
        Path,
        service._news_service._resolve_m7_news_artifact_path(),  # noqa: SLF001
    )
    lines = artifact_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3


def test_daily_archive_retention_cleans_expired_files(service: StockAnalyzerService) -> None:
    service._config.evolution.m7_news_daily_archive_retention_days = 7  # noqa: SLF001
    now = datetime(2026, 8, 27, 16, 30, 0)
    archive_dir = cast(
        Path,
        service._news_service._resolve_m7_news_daily_archive_dir(),  # noqa: SLF001
    )
    archive_dir.mkdir(parents=True, exist_ok=True)
    # 预置一个远早于保留期的过期归档文件。
    expired = archive_dir / "2026-08-01.jsonl"
    expired.write_text('{"event_id": "old"}\n', encoding="utf-8")
    # 预置一个非日期命名文件，不应被清理。
    keep_other = archive_dir / "README.jsonl"
    keep_other.write_text("{}\n", encoding="utf-8")

    records = [_fake_record("evt-a", "600000", "2026-08-27 09:30:00")]
    _patch_collect(service, records)
    service.run_m7_live_news_sync(symbols=["600000"], timestamp=now)

    assert not expired.exists(), "超过保留期的归档文件应被清理"
    assert keep_other.exists(), "非日期命名文件不应被清理"
    assert (archive_dir / "2026-08-27.jsonl").exists()


def test_fetch_failure_degrades_and_records_audit_event(service: StockAnalyzerService) -> None:
    class _FakeArrowInvalid(Exception):
        """模拟 pyarrow.lib.ArrowInvalid（pyarrow 未装时用通用异常替代）。"""

    class _FakeAkshare:
        def stock_news_em(self, symbol: str):  # noqa: ANN001, ARG002
            raise _FakeArrowInvalid(
                "Invalid regular expression: invalid escape sequence: \\u"
            )

    # mock akshare 导入，使 stock_news_em 抛出模拟 ArrowInvalid。
    service._import_akshare = lambda: _FakeAkshare()  # noqa: SLF001

    before = len(
        [e for e in service._audit_events if e.get("event_type") == "live_news_fetch_failed"]  # noqa: SLF001
    )

    rows = service._fetch_symbol_live_news(  # noqa: SLF001
        symbol="600000",
        now=datetime(2026, 8, 27, 16, 30, 0),
        max_age_hours=24.0,
        per_symbol_limit=5,
        force_refresh=True,
    )

    assert rows == [], "抓取失败应降级返回空列表，不崩溃"
    failed_events = [
        e for e in service._audit_events if e.get("event_type") == "live_news_fetch_failed"  # noqa: SLF001
    ]
    assert len(failed_events) == before + 1, "应记录一条 live_news_fetch_failed 审计事件"
    payload = failed_events[-1].get("payload", {})
    assert payload.get("symbol") == "600000"
    assert payload.get("error_type") == "_FakeArrowInvalid"
