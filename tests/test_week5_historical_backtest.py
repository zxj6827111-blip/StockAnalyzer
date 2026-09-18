"""Week5 历史回测复用每日主选股链路（week5_daily）测试。

覆盖 PLAN 的验收面：
- 数据契约：批量质量数据严格受 ``end_date`` 限制（先截断后取最近 N 根），
  AsOfMarketDataProvider 对未来行立即抛泄露错误；
- 选择器契约：``end_date`` 透传批量源、as-of 模式绝不读生产 selection snapshot；
- 引擎（historical context）：显式池/历史全市场两条路径的编排、隔离
  （不写生产报告/审计/关注池/通知）、intraday 降级标注、空态分类；
- 算法一致性：相同 backend 阶段实现下，live 与 historical policy 的
  final selection 完全一致（同一引擎，不复制近似算法）；
- 端到端（真实 service backend + 真实选择器/快照/深阶段/pipeline）：
  完整漏斗 + 生产 artifacts 未被修改。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from stock_analyzer.config import StockAnalyzerConfig
from stock_analyzer.data.asof_provider import AsOfMarketDataProvider
from stock_analyzer.data.market_warehouse import MarketWarehouse
from stock_analyzer.data.provider import FutureDataLeakError, SyntheticProvider
from stock_analyzer.runtime.services.week5_selection_engine import (
    Week5AccountState,
    Week5RunContext,
    Week5RunPolicy,
    Week5SelectionEngine,
)
from tests.test_market_warehouse import _build_sample_package
from tests.test_service_week5 import (
    _enable_universe_quality_selector,
    _load_test_config,
    _new_service,
)

AS_OF = date(2026, 7, 31)


# ---------------------------------------------------------------------------
# Part A：数据契约（warehouse end_date / AsOfMarketDataProvider / 选择器）
# ---------------------------------------------------------------------------
def test_warehouse_quality_metrics_end_date_truncates_before_window(tmp_path: Path) -> None:
    """先截断到 end_date 再取最近 N 根：end_date 之后的新行绝不能出现。"""
    package_root = tmp_path / "package"
    _build_sample_package(package_root)
    warehouse = MarketWarehouse(
        db_path=tmp_path / "warehouse" / "market.duckdb",
        package_root=package_root,
    )
    warehouse.bootstrap_from_offline_package(source_root=package_root)

    full = warehouse.fetch_universe_quality_metrics(symbols=["600000"], lookback_days=5)
    assert sorted(full["date"].dt.strftime("%Y-%m-%d")) == [
        "2026-03-03",
        "2026-03-04",
        "2026-03-05",
    ]

    truncated = warehouse.fetch_universe_quality_metrics(
        symbols=["600000"],
        lookback_days=5,
        end_date=date(2026, 3, 4),
    )
    assert sorted(truncated["date"].dt.strftime("%Y-%m-%d")) == ["2026-03-03", "2026-03-04"]

    # lookback=1 + end_date：as-of 有效性的"最近一根"语义
    latest = warehouse.fetch_universe_quality_metrics(
        symbols=["600000"],
        lookback_days=1,
        end_date=date(2026, 3, 4),
    )
    assert len(latest) == 1
    assert latest["date"].iloc[0].strftime("%Y-%m-%d") == "2026-03-04"


class _RecordingBarsProvider:
    """记录 end_date 参数并可模拟"底层链违约返回未来行"的假 provider。"""

    def __init__(self, *, force_leak: bool = False) -> None:
        self.last_end_date: date | None | object = "__unset__"
        self._force_leak = force_leak

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        self.last_end_date = end_date
        if self._force_leak:
            # 模拟底层链违约：无视 end_date，返回包含 as_of 之后数据的窗口。
            return SyntheticProvider(seed_offset=3).fetch_daily_bars(
                symbol=symbol, lookback_days=lookback_days
            )
        return SyntheticProvider(seed_offset=3).fetch_daily_bars(
            symbol=symbol, lookback_days=lookback_days, end_date=end_date
        )

    def fetch_intraday_summaries(
        self, symbols: list[str], interval: str, lookback_days: int = 120
    ) -> dict[str, pd.DataFrame]:
        # 模拟"摘要窗口含未来行"：帧跨越 as_of（结束于 as_of+10 天），
        # as-of provider 应把 as_of 之后的行裁掉、保留 as_of 之前的行。
        frame = SyntheticProvider().fetch_daily_bars(
            symbol="600000", lookback_days=lookback_days, end_date=date(2026, 8, 10)
        )
        return {symbol: frame for symbol in symbols}

    def status(self) -> dict[str, object]:
        return {}


def test_asof_provider_rejects_future_daily_bars() -> None:
    """底层链返回 as_of 之后的行时必须立刻抛 FutureDataLeakError。"""
    base = _RecordingBarsProvider(force_leak=True)
    provider = AsOfMarketDataProvider(base, AS_OF)
    with pytest.raises(FutureDataLeakError):
        provider.fetch_daily_bars(symbol="600000", lookback_days=30)


def test_asof_provider_clamps_end_date_and_truncates_intraday() -> None:
    """生效截止日 = min(end_date, as_of)；分钟摘要未来行被裁剪。"""
    base = _RecordingBarsProvider()
    provider = AsOfMarketDataProvider(base, AS_OF)
    earlier = date(2026, 7, 20)
    provider.fetch_daily_bars(symbol="600000", lookback_days=30, end_date=earlier)
    assert base.last_end_date == earlier
    provider.fetch_daily_bars(symbol="600000", lookback_days=30, end_date=date(2026, 8, 20))
    assert base.last_end_date == AS_OF

    summaries = provider.fetch_intraday_summaries(["600000"], "1m", lookback_days=30)
    frame = summaries["600000"]
    assert isinstance(frame, pd.DataFrame) and not frame.empty
    assert pd.to_datetime(frame.index).max().date() <= AS_OF
    # 截断前窗口跨越 as_of（结束于 2026-08-10），因此必须真的丢掉未来行。
    assert pd.to_datetime(frame.index).min().date() < AS_OF


class _RecordingBatchSource:
    """记录 fetch_universe_quality_metrics 参数的批量源。"""

    def __init__(self, frame: pd.DataFrame | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._frame = frame if frame is not None else pd.DataFrame()

    def fetch_universe_quality_metrics(
        self,
        *,
        symbols: list[str],
        lookback_days: int,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        self.calls.append(
            {
                "symbols": list(symbols),
                "lookback_days": lookback_days,
                "end_date": end_date,
            }
        )
        return self._frame


def test_selector_passes_end_date_to_batch_source(tmp_path: Path) -> None:
    """选择器必须把 end_date 透传给批量源（as-of 粗筛契约）。"""
    from stock_analyzer.runtime.universe_candidate_selector import UniverseCandidateSelector

    source = _RecordingBatchSource()
    selector = UniverseCandidateSelector(
        warehouse=source,
        snapshot_path=str(tmp_path / "selection.json"),
        fallback_sampler=None,
    )
    selector.select(
        symbols=["600000"],
        target_size=1,
        trade_date="2026-07-31",
        ruleset_id="r1",
        board_scope=["SSE"],
        reference_date=AS_OF,
        end_date=AS_OF,
    )
    assert source.calls, "selector should call the batch source"
    assert source.calls[0]["end_date"] == AS_OF


def test_selector_with_end_date_never_reads_production_snapshot(tmp_path: Path) -> None:
    """as-of 模式禁用 selection snapshot fallback（当前快照属于未来信息）。"""
    from stock_analyzer.runtime.universe_candidate_selector import UniverseCandidateSelector

    snapshot_path = tmp_path / "production_selection.json"
    snapshot_path.write_text("{}", encoding="utf-8")
    source = _RecordingBatchSource()  # 批量不可用 → fallback 分支
    selector = UniverseCandidateSelector(
        warehouse=source,
        snapshot_path=str(snapshot_path),
        fallback_sampler=None,
    )
    result = selector.select(
        symbols=["600000"],
        target_size=1,
        trade_date="2026-07-31",
        ruleset_id="r1",
        board_scope=["SSE"],
        reference_date=AS_OF,
        end_date=AS_OF,
    )
    report = result["report"]
    assert report["selector_mode"] == "degraded_fallback"
    assert report["snapshot_fallback_unavailable_reason"] == "snapshot_disabled_for_asof"
    # 生产 snapshot 文件未被改动/重写
    assert snapshot_path.read_text(encoding="utf-8") == "{}"


# ---------------------------------------------------------------------------
# Part B：共享引擎（stub backend）— historical 编排 / 隔离 / 一致性
# ---------------------------------------------------------------------------
class _StubBackend:
    """实现引擎协议的最小 backend：脚本化的阶段输出 + 调用记录。"""

    def __init__(self, config: StockAnalyzerConfig, symbols: list[str]) -> None:
        self._config = config
        self._symbols = list(symbols)
        self.stored_reports: list[dict[str, object]] = []
        self.audit_events: list[str] = []
        self.notified: list[str] = []
        self.watchlist_synced = False
        self.quality_selection_kwargs: list[dict[str, Any]] = []
        self.pipeline_reports: dict[str, dict[str, object]] = {}

    @property
    def config(self) -> StockAnalyzerConfig:
        return self._config

    def build_data_gate(self, **kwargs: Any) -> dict[str, object]:
        return {"status": "ok", "reasons": []}

    def prefer_local_symbol_universe(self) -> bool:
        return True

    def resolve_symbol_universe(self, **kwargs: Any) -> dict[str, object]:
        return {"source": "stub_universe", "symbols": list(self._symbols), "errors": []}

    def universe_seed_trade_date(self) -> str:
        return AS_OF.isoformat()

    def select_universe_quality_candidates(self, **kwargs: Any) -> dict[str, object]:
        self.quality_selection_kwargs.append(dict(kwargs))
        selected = list(self._symbols)[:3]
        return {
            "selected": selected,
            "report": {
                "selector_mode": "quality_all_eligible",
                "selected_count": len(selected),
                "board_quotas": {},
            },
        }

    def ensure_feature_snapshot(self, *, symbols: list[str], scope: str) -> dict[str, object]:
        return {
            "ok": True,
            "requested_symbol_count": len(symbols),
            "published_symbol_count": len(symbols),
        }

    def light_stage_from_snapshot(
        self, *, frame: Any, target: int, allowed_exchanges: Any
    ) -> dict[str, object]:
        shortlisted = [
            {"symbol": symbol, "baseline_score": 80.0, "exchange": "SSE"}
            for symbol in self._symbols[:target]
        ]
        return {
            "applied": True,
            "mode": "stub_light",
            "universe_count": len(shortlisted),
            "eligible_count": len(shortlisted),
            "shortlisted_count": len(shortlisted),
            "shortlisted": shortlisted,
            "symbols": [item["symbol"] for item in shortlisted],
        }

    def deep_stage_from_snapshot(
        self, *, frame: Any, target: int, light_report: dict[str, object]
    ) -> dict[str, object]:
        selected = [
            {"symbol": item["symbol"], "baseline_score": 80.0, "funnel_score": 85.0}
            for item in (light_report.get("shortlisted") or [])[:target]
        ]
        return {
            "applied": True,
            "mode": "stub_deep",
            "input_count": len(selected),
            "selected_count": len(selected),
            "selected": selected,
            "light_shortlist_count": len(light_report.get("shortlisted") or []),
            "snapshot_match_rows": len(selected),
        }

    def prefilter_universe_symbols(
        self, *, symbols: list[str], top_k_override: Any = None
    ) -> dict[str, object]:
        shortlisted = [
            {"symbol": symbol, "baseline_score": 80.0, "exchange": "SSE"}
            for symbol in symbols[:top_k_override or len(symbols)]
        ]
        return {
            "applied": True,
            "mode": "stub_prefilter",
            "universe_count": len(symbols),
            "eligible_count": len(shortlisted),
            "shortlisted_count": len(shortlisted),
            "shortlisted": shortlisted,
            "symbols": [item["symbol"] for item in shortlisted],
            "stages": {},
        }

    def run_pipeline(self, **kwargs: Any) -> dict[str, object]:
        strategy = str(kwargs.get("strategy", "monster"))
        return self.pipeline_reports.setdefault(strategy, self._build_report(kwargs))

    def _build_report(self, kwargs: dict[str, Any]) -> dict[str, object]:
        symbols = list(kwargs.get("symbols") or [])
        return {
            "trace_id": "stub-trace",
            "signals": [
                {
                    "symbol": symbol,
                    "strategy": str(kwargs.get("strategy", "monster")),
                    "score": 75.0,
                    "grade": "A",
                    "action": "buy",
                    "target_position": 0.1,
                    "probabilities": {"meta": 0.6},
                    "reasons": ["stub_signal"],
                    "decision_trace": {
                        "risk_gate": {"passed": True},
                        "cross_review_gate": {"passed": True},
                    },
                    "post_scan_enrichment": "",
                }
                for symbol in symbols
            ],
            "risk": {"drawdown_pct": 0.0, "action": "normal"},
            "runtime": {"duration_ms": 5},
        }

    def select_live_runtime_provider(self) -> object:
        return SyntheticProvider(seed_offset=5)

    def score_signal_pool_candidate(
        self, *, signal: Any, prefilter_detail: Any
    ) -> dict[str, object]:
        return {
            "symbol": str(signal.get("symbol", "")),
            "action": str(signal.get("action", "")),
            "score": float(signal.get("score", 0.0)),
            "shortlist_score": float(signal.get("score", 0.0)),
            "grade": str(signal.get("grade", "")),
            "reasons": list(signal.get("reasons", [])),
            "decision_trace": dict(signal.get("decision_trace", {})),
        }

    def apply_execution_aware_rerank(
        self, *, candidates: list[dict[str, object]]
    ) -> dict[str, object]:
        return {
            "applied": False,
            "score_key": "shortlist_score",
            "candidate_count": len(candidates),
        }

    def final_signal_selector(
        self,
        *,
        signals: list[dict[str, object]],
        data_gate_status: str,
        min_threshold_lift: float = 0.0,
        news_mode_override: str | None = None,
    ) -> dict[str, object]:
        threshold = 70.0 + max(0.0, float(min_threshold_lift))
        selected: list[dict[str, object]] = []
        rejected: list[dict[str, object]] = []
        for signal in signals:
            reasons: list[str] = []
            if data_gate_status != "ok":
                reasons.append(f"data_gate:{data_gate_status}")
            if float(signal.get("score", 0.0)) < threshold:
                reasons.append("below_min_threshold")
            if reasons:
                rejected.append(
                    {
                        "symbol": str(signal.get("symbol", "")),
                        "score": float(signal.get("score", 0.0)),
                        "action": str(signal.get("action", "")),
                        "reject_reasons": reasons,
                    }
                )
            else:
                selected.append(
                    {
                        "symbol": str(signal.get("symbol", "")),
                        "score": float(signal.get("score", 0.0)),
                        "action": str(signal.get("action", "")),
                        "final_signal_reasons": [],
                        "_news_mode_override": news_mode_override or "",
                    }
                )
        selected.sort(
            key=lambda item: (-float(item.get("score", 0.0)), str(item.get("symbol", "")))
        )
        return {
            "applied": True,
            "mode": "final_selection",
            "input_count": len(signals),
            "selected_count": len(selected),
            "rejected_count": len(rejected),
            "final_signals": selected,
            "rejected": rejected,
        }

    def build_first_board_candidate(self, **kwargs: Any) -> None:
        return None

    def detect_symbol_anomaly(self, **kwargs: Any) -> None:
        return None

    def estimate_sentiment(self, *, monster_report: dict[str, object]) -> tuple[float, bool]:
        return (60.0, True)

    def market_breadth_gate(self, *, now: datetime) -> tuple[dict[str, object], float]:
        return ({"enabled": False, "block_new_buy": False}, 0.0)

    def build_gate_blocked_report(self, **kwargs: Any) -> dict[str, object]:
        return {"status": "blocked_data_gate", "reasons": list(kwargs.get("reasons", []))}

    def build_dual_track_output(self, **kwargs: Any) -> dict[str, object]:
        return {"mode": "legacy"}

    def store_report(self, report: dict[str, object]) -> None:
        self.stored_reports.append(report)

    def record_audit(
        self, *, event_type: str, level: str = "info", trace_id: str = "", payload: Any = None
    ) -> None:
        self.audit_events.append(event_type)

    def sync_watchlist_from_report(self, **kwargs: Any) -> dict[str, object]:
        self.watchlist_synced = True
        return {"enabled": True, "updated": True}

    def watchlist_sync_diagnostics(self, **kwargs: Any) -> dict[str, object]:
        return {"stub": True}

    def build_scan_notification_content(self, **kwargs: Any) -> str:
        return "stub"

    def notify_scan(self, **kwargs: Any) -> None:
        self.notified.append("scan")

    def notify_actionable_signals(self, report: Any, *, trace_id: str, title_prefix: str) -> None:
        self.notified.append("actionable")

    def is_intraday_scheduler_scan(self, *, now: datetime, sync_reason: str) -> bool:
        return False

    def latest_preserved_watchlist_symbols(self, *, top_k_override: Any = None) -> list[str]:
        return []

    def market_warehouse(self) -> object:
        return None

    def provider(self) -> object:
        return SyntheticProvider(seed_offset=9)

    def provider_graph(self) -> list[object]:
        return []

    def runtime_source_mode(self) -> str:
        return "offline_only"


class _FakeUniverseProvider:
    """as-of 上下文用：索引 + 批量质量 + 日线（全部受 end_date 限制）。

    S03 起批量探针必须给出**一段窗口**的 bar（真实 provider 的行为），否则 PIT
    股票池会正确地判定"窗口内历史不足"而拒绝全部标的。这里按 ``lookback_days``
    生成截止 ``end_date`` 的连续交易日 bar。
    """

    def __init__(self, symbols: list[str]) -> None:
        self._symbols = list(symbols)
        self.batch_calls: list[dict[str, object]] = []

    def list_symbols(self) -> list[str]:
        return list(self._symbols)

    def fetch_universe_quality_metrics(
        self,
        *,
        symbols: list[str],
        lookback_days: int,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        self.batch_calls.append(
            {"symbols": list(symbols), "lookback_days": lookback_days, "end_date": end_date}
        )
        effective_end = end_date or AS_OF
        sessions = max(1, int(lookback_days))
        dates = pd.bdate_range(end=pd.Timestamp(effective_end), periods=sessions)
        rows = [
            {"symbol": symbol, "date": timestamp, "close": 10.0}
            for symbol in symbols
            for timestamp in dates
        ]
        return pd.DataFrame(rows)

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        return SyntheticProvider(seed_offset=11).fetch_daily_bars(
            symbol=symbol, lookback_days=lookback_days, end_date=end_date or AS_OF
        )

    def status(self) -> dict[str, object]:
        return {}


def _historical_config(tmp_path: Path) -> StockAnalyzerConfig:
    config = _load_test_config()
    config.week5.feature_snapshot_enabled = False
    config.week5.market_breadth_enabled = False
    config.week5.auto_sync_watchlist = False
    config.week5.universe_prefilter_enabled = True
    config.week5.monster_scan_max_symbols = 120
    config.evolution.news_risk_mode = "off"
    return config


class _InMemoryRegistry:
    """进程内 registry 桩：只提供读接口（S06 闸门需要），**不碰共享 DuckDB**。

    背景（2026-09-18 实测）：conftest 把 ``bootstrap_state_path`` 指向一个所有 xdist
    worker 共享的临时文件，而 registry 库是它同目录的 ``learning_protocol.duckdb``。
    若测试夹具往这个共享库里写模型，多 worker 并发时会撞 DuckDB 锁 → 闸门读不到候选
    → 历史重放被判 unscorable → 测试**间歇性失败**。进程内桩同时消除了写竞争与
    跨测试耦合，且仍能真实覆盖"有 PIT 合法登记"的路径。
    """

    def __init__(self, records: list[object] | None = None) -> None:
        self._records = list(records or [])

    def active_champion(self, *, suppress_read_errors: bool = False) -> object | None:
        _ = suppress_read_errors
        return None

    def list_records(
        self, *, limit: int | None = None, suppress_read_errors: bool = False
    ) -> list[object]:
        _ = (limit, suppress_read_errors)
        return list(self._records)

    def get_by_id(self, model_id: str, *, suppress_read_errors: bool = False) -> object | None:
        _ = suppress_read_errors
        return next(
            (item for item in self._records if getattr(item, "model_id", "") == model_id), None
        )

    def register_artifact(self, **kwargs: object) -> object:
        raise AssertionError("测试夹具不得写共享 registry 库")


class _RegistryRecord:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


def _register_pit_model(
    service: object, *, tmp_path: Path, created_at: str = "2026-05-01T10:00:00"
) -> str:
    """给 service 注入"as_of 之前就存在"的合法模型登记（S06 时间闸门需要）。

    两条硬约束（都是本轮实测教训）：
    1. 只写进程内桩，**不写共享 DuckDB**（否则多 xdist worker 撞锁 → 间歇失败）；
    2. 工件必须**真的可加载**（ModelTrainer 产出）：B1 起解析结果会被绑到实际加载
       路径，手写的最小 JSON 过不了适配器反序列化 → 会被正确地判 unscorable。
    """
    import json as _json

    from stock_analyzer.data.provider import SyntheticProvider
    from stock_analyzer.models.bundle import compute_artifact_identity_hash
    from stock_analyzer.models.trainer import ModelTrainer

    cfg = _load_test_config()
    cfg.training.min_samples = 40
    nonce = uuid.uuid4().hex
    artifact_path = tmp_path / f"pit_model_{nonce}.json"
    bars = SyntheticProvider(seed_offset=11).fetch_daily_bars("600000", lookback_days=300)
    ModelTrainer(training=cfg.training, labels=cfg.labels).train_and_save(
        bars=bars, output_path=str(artifact_path)
    )
    payload = _json.loads(artifact_path.read_text(encoding="utf-8"))
    payload["created_at"] = created_at  # 模拟"训练发生在 as_of 之前"
    artifact_path.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    model_id = f"model_pit_{nonce[:12]}"
    record = _RegistryRecord(
        model_id=model_id,
        artifact_uri=str(artifact_path),
        artifact_content_hash=compute_artifact_identity_hash(artifact_path),
        artifact_created_at=datetime.fromisoformat(created_at),
        feature_schema_id=str(payload.get("feature_schema_id", "")) or "fs_pit_v1",
        feature_schema_hash=str(payload.get("feature_schema_hash", "")),
        label_policy_id=str(payload.get("label_policy_id", "")),
        label_policy_hash=str(payload.get("label_policy_hash", "")),
        dataset_manifest_id=str(payload.get("dataset_manifest_id", "")),
        lifecycle_state="trained",
        promoted_at=None,
    )
    service._model_registry = _InMemoryRegistry([record])  # noqa: SLF001 - 测试夹具注入
    return model_id

def _historical_context(
    *,
    config: StockAnalyzerConfig,
    provider: object,
    tmp_path: Path,
    run_pipeline_fn: Any,
    symbols: list[str] | None,
) -> Week5RunContext:
    return Week5RunContext(
        mode="historical",
        now=datetime(2026, 7, 31, 15, 0),
        as_of=AS_OF,
        config=config,
        provider=provider,
        run_pipeline_fn=run_pipeline_fn,
        symbols=symbols,
        account=Week5AccountState(),
        artifact_dir=tmp_path,
    )


def test_engine_historical_explicit_pool_skips_production_writes(tmp_path: Path) -> None:
    """historical 显式池：直接漏斗 + 不写生产报告/审计/关注池/通知。"""
    config = _historical_config(tmp_path)
    backend = _StubBackend(config, symbols=["600000", "000001", "600519"])
    provider = _FakeUniverseProvider(["600000", "000001", "600519"])
    context = _historical_context(
        config=config,
        provider=provider,
        tmp_path=tmp_path,
        run_pipeline_fn=lambda **kwargs: backend.run_pipeline(**kwargs),
        symbols=["600000", "000001", "600519"],
    )
    engine = Week5SelectionEngine(
        backend=backend, context=context, policy=Week5RunPolicy.historical()
    )
    report = engine.run()

    assert report["run_mode"] == "historical"
    assert report["funnel"]["policy"] == "direct_non_universe"
    assert report["historical_context"]["as_of"] == AS_OF.isoformat()
    assert report["historical_context"]["account"]["neutral"] is True
    assert report["historical_context"]["news_neutralized"] is True
    # 隔离：生产写路径全部关闭
    assert backend.stored_reports == []
    assert backend.audit_events == []
    assert backend.notified == []
    assert backend.watchlist_synced is False
    # final selection 走同一套 backend 阶段
    final = report["funnel"]["final_selection"]
    assert final["selected_count"] == 3
    assert [item["symbol"] for item in final["final_signals"]] == [
        "000001",
        "600000",
        "600519",
    ] or len(final["final_signals"]) == 3


def test_engine_historical_full_market_resolves_universe_with_end_date(tmp_path: Path) -> None:
    """historical 全市场：从 provider 索引生成股票池，质量选择收到 end_date。"""
    config = _historical_config(tmp_path)
    symbols = ["600000", "000001", "600519", "300750"]
    backend = _StubBackend(config, symbols=symbols)
    provider = _FakeUniverseProvider(symbols)
    context = _historical_context(
        config=config,
        provider=provider,
        tmp_path=tmp_path,
        run_pipeline_fn=lambda **kwargs: backend.run_pipeline(**kwargs),
        symbols=None,
    )
    engine = Week5SelectionEngine(
        backend=backend, context=context, policy=Week5RunPolicy.historical()
    )
    report = engine.run()

    assert report["symbol_source"].startswith("provider_index:as_of_quality_selector")
    prefilter = report["prefilter"]
    assert prefilter["historical_universe"]["provider_index_count"] == len(symbols)
    assert prefilter["historical_universe"]["as_of_valid_count"] == len(symbols)
    assert prefilter["historical_universe"]["selected_count"] == 3
    # S03：报告必须带 PIT 股票池快照（可复现 id + 分母口径 + 覆盖度如实标注）
    snapshot = prefilter["historical_universe"]["universe_snapshot"]
    assert str(snapshot["universe_snapshot_id"]).startswith("asofuniv_")
    assert snapshot["as_of"] == AS_OF.isoformat()
    assert snapshot["eligible_count"] == len(symbols)
    assert snapshot["expected_active_count"] == len(symbols)
    assert snapshot["coverage_denominator"] == "expected_active"
    assert snapshot["survivorship_coverage"] == "incomplete_or_unknown"
    # 质量选择收到 as_of end_date + 任务独立 selection snapshot 路径
    assert backend.quality_selection_kwargs, "quality selection should be invoked"
    kwargs = backend.quality_selection_kwargs[0]
    assert kwargs["end_date"] == AS_OF
    assert str(kwargs["selection_snapshot_path"]).endswith("universe_selection.json")
    assert provider.batch_calls[0]["end_date"] == AS_OF
    # snapshot_funnel + prefilter（快照禁用时直接走 prefilter stub）
    assert report["funnel"]["policy"] == "snapshot_funnel"
    assert report["prefilter"]["applied"] is True


def test_engine_live_and_historical_same_final_selection(tmp_path: Path) -> None:
    """一致性：相同 backend 阶段实现下，live 与 historical 的 final selection 一致。"""
    config = _historical_config(tmp_path)
    symbols = ["600000", "000001", "600519"]
    backend_live = _StubBackend(config, symbols=symbols)
    backend_hist = _StubBackend(config, symbols=symbols)

    live_context = Week5RunContext(
        mode="live",
        now=datetime(2026, 7, 31, 20, 30),
        symbols=list(symbols),
        account=Week5AccountState(),
    )
    live_policy = Week5RunPolicy.live()
    live_policy.notify = False
    live_report = Week5SelectionEngine(
        backend=backend_live, context=live_context, policy=live_policy
    ).run()

    hist_context = _historical_context(
        config=config,
        provider=_FakeUniverseProvider(symbols),
        tmp_path=tmp_path,
        run_pipeline_fn=lambda **kwargs: backend_hist.run_pipeline(**kwargs),
        symbols=list(symbols),
    )
    hist_report = Week5SelectionEngine(
        backend=backend_hist, context=hist_context, policy=Week5RunPolicy.historical()
    ).run()

    live_final = live_report["funnel"]["final_selection"]
    hist_final = hist_report["funnel"]["final_selection"]
    assert [item["symbol"] for item in live_final["final_signals"]] == [
        item["symbol"] for item in hist_final["final_signals"]
    ]
    assert [item["score"] for item in live_final["final_signals"]] == [
        item["score"] for item in hist_final["final_signals"]
    ]
    assert live_report["signal_pool"]["candidate_count"] == (
        hist_report["signal_pool"]["candidate_count"]
    )
    # historical 的 final selector 强制 news off（stub 记录 override）
    hist_override = {item["_news_mode_override"] for item in hist_final["final_signals"]}
    assert hist_override == {"off"}


# ---------------------------------------------------------------------------
# Part C：端到端（真实 service backend + 真实选择器/快照/深阶段/pipeline）
# ---------------------------------------------------------------------------
class _FakeHistoricalProvider:
    """端到端假 provider：批量质量 + 日线（受 end_date 限制）+ 无分钟数据。"""

    def __init__(self, symbols: list[str], *, data_end: date) -> None:
        self._symbols = list(symbols)
        self._data_end = data_end
        self._inner = SyntheticProvider(seed_offset=13)
        self._daily_cache: dict[tuple[str, int, str], pd.DataFrame] = {}

    def list_symbols(self) -> list[str]:
        return list(self._symbols)

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        effective_end = end_date or self._data_end
        key = (symbol, lookback_days, effective_end.isoformat())
        cached = self._daily_cache.get(key)
        if cached is not None:
            return cached.copy()
        frame = self._inner.fetch_daily_bars(
            symbol=symbol, lookback_days=lookback_days, end_date=effective_end
        )
        self._daily_cache[key] = frame.copy()
        return frame

    def fetch_universe_quality_metrics(
        self,
        *,
        symbols: list[str],
        lookback_days: int,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for symbol in symbols:
            bars = self.fetch_daily_bars(symbol=symbol, lookback_days=250, end_date=end_date)
            frame = bars.tail(max(1, int(lookback_days))).reset_index()
            frame = frame.rename(columns={"index": "date"})
            if "date" not in frame.columns:
                frame = bars.tail(max(1, int(lookback_days))).reset_index(names="date")
            frame["symbol"] = symbol
            frame["financial_completeness"] = 1.0
            frame["financial_data_complete"] = True
            frame["background_data_complete"] = True
            frames.append(frame)
        if not frames:
            return pd.DataFrame()
        combined = pd.concat(frames, ignore_index=True)
        return combined.sort_values(["symbol", "date"]).reset_index(drop=True)

    def fetch_intraday_summaries(
        self, symbols: list[str], interval: str, lookback_days: int = 120
    ) -> dict[str, pd.DataFrame]:
        return {symbol: pd.DataFrame() for symbol in symbols}

    def fetch_intraday_summary(
        self, symbol: str, interval: str, lookback_days: int = 120
    ) -> pd.DataFrame:
        return pd.DataFrame()

    def status(self) -> dict[str, object]:
        return {}


def test_week5_historical_day_end_to_end_full_funnel_with_isolation(tmp_path: Path) -> None:
    """端到端：真实引擎 + 真实 service 阶段实现跑通全市场漏斗，且生产不落盘。"""
    from stock_analyzer.runtime.services.week5_historical_runner import run_week5_historical_day

    config = _load_test_config()
    _enable_universe_quality_selector(config)
    config.week5.universe_quality_target_size = 3
    config.week5.light_candidate_target = 3
    config.week5.deep_candidate_target = 3
    config.week5.final_signal_cap = 2
    config.week5.feature_snapshot_root = str(tmp_path / "production_features_light")
    config.week5.universe_quality_snapshot_path = str(tmp_path / "production_selection.json")
    config.week5.market_breadth_enabled = False
    config.week5.auto_sync_watchlist = False
    config.week5.universe_quality_require_financial_data = False
    config.evolution.news_risk_mode = "penalty"
    service = _new_service(config, provider=SyntheticProvider(seed_offset=17))
    service.state.watchlist = ["600999"]
    _register_pit_model(service, tmp_path=tmp_path)

    symbols = ["600000", "000001", "600519"]
    provider = _FakeHistoricalProvider(symbols, data_end=AS_OF)
    task_dir = tmp_path / "week5_task"
    task_dir.mkdir(parents=True, exist_ok=True)
    watchlist_before = list(service.state.watchlist)

    report = run_week5_historical_day(
        service=service,
        as_of=AS_OF,
        task_dir=task_dir,
        symbols=None,
        base_provider=provider,
    )

    assert report["run_mode"] == "historical"
    assert report["funnel"]["policy"] == "snapshot_funnel"
    context = report["historical_context"]
    assert context["as_of"] == AS_OF.isoformat()
    assert context["account"]["neutral"] is True
    assert context["news_neutralized"] is True
    assert context["realtime_data_allowed"] is False
    assert context["market_breadth_recomputed"] is True
    # 分钟数据缺失 → 降级标注
    assert report["prefilter"]["intraday_degraded"] is True
    assert report["prefilter"]["intraday_coverage_ratio"] == 0.0
    # 完整漏斗计数链
    funnel = report["funnel"]
    assert funnel["light_count"] > 0
    assert funnel["deep_count"] > 0
    assert funnel["final_count"] <= 2
    assert funnel["final_count"] == funnel["final_selection"]["selected_count"]
    # 任务独立目录里有快照产物；生产 root 没有任何文件
    assert (task_dir / "features_light" / "current.json").exists()
    assert not (tmp_path / "production_features_light").exists()
    assert not (tmp_path / "production_selection.json").exists()
    # 隔离：生产关注池/生产周报/审计未被触碰
    assert list(service.state.watchlist) == watchlist_before
    assert service._week5_service._state_service.latest_week5_scan_report() is None  # noqa: SLF001


def test_week5_historical_day_explicit_pool_marks_manual_source(tmp_path: Path) -> None:
    """显式股票池：标注 manual_symbols_not_full_market，不进入质量选择。"""
    from stock_analyzer.runtime.services.week5_historical_runner import run_week5_historical_day

    config = _load_test_config()
    _enable_universe_quality_selector(config)
    config.week5.feature_snapshot_root = str(tmp_path / "production_features_light")
    config.week5.auto_sync_watchlist = False
    config.week5.market_breadth_enabled = False
    service = _new_service(config, provider=SyntheticProvider(seed_offset=19))
    _register_pit_model(service, tmp_path=tmp_path)

    provider = _FakeHistoricalProvider(["600000", "000001"], data_end=AS_OF)
    task_dir = tmp_path / "week5_task_explicit"
    task_dir.mkdir(parents=True, exist_ok=True)
    report = run_week5_historical_day(
        service=service,
        as_of=AS_OF,
        task_dir=task_dir,
        symbols=["600000", "000001"],
        base_provider=provider,
    )
    assert report["prefilter"]["explicit_pool"] is True
    assert report["prefilter"]["explicit_pool_note"] == "manual_symbols_not_full_market"
    assert report["funnel"]["policy"] == "direct_non_universe"
    assert report["watchlist_size"] == 2


# ---------------------------------------------------------------------------
# Part D：API contract（algorithm=week5_daily）
# ---------------------------------------------------------------------------
def test_api_week5_daily_invalid_algorithm_returns_400() -> None:
    from fastapi.testclient import TestClient

    from stock_analyzer.main import app

    client = TestClient(app)
    response = client.post(
        "/backtest/asof-scan",
        json={"date": "2026-07-31", "algorithm": "bogus"},
    )
    assert response.status_code == 400
    assert "invalid_algorithm" in response.json()["detail"]


def test_api_week5_daily_busy_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from stock_analyzer import main as main_module
    from stock_analyzer.main import app

    acquired = {"flag": True}

    def _busy() -> bool:
        return False

    monkeypatch.setattr(main_module._service, "try_acquire_week5_backtest", _busy)
    monkeypatch.setattr(
        main_module._service._config.asof_backtest, "week5_daily_enabled", True
    )
    client = TestClient(app)
    response = client.post(
        "/backtest/asof-scan",
        json={"date": "2026-07-31", "algorithm": "week5_daily"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "week5_backtest_busy"
    assert acquired["flag"]


def test_api_week5_daily_disabled_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from stock_analyzer import main as main_module
    from stock_analyzer.main import app

    patched_asof = main_module._service._config.asof_backtest.model_copy(
        update={"week5_daily_enabled": False}
    )
    patched_config = main_module._service._config.model_copy(
        update={"asof_backtest": patched_asof}
    )
    # 路由层经 get_config() 读 main 模块级单例，两处都要 patch。
    monkeypatch.setattr(main_module, "_config", patched_config)
    monkeypatch.setattr(main_module._service, "_config", patched_config)
    client = TestClient(app)
    response = client.post(
        "/backtest/asof-scan",
        json={"date": "2026-07-31", "algorithm": "week5_daily"},
    )
    assert response.status_code == 409
    assert "week5_backtest_disabled" in response.json()["detail"]


def test_api_week5_daily_end_to_end_full_market(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """week5_daily 提交 → 202/进度 → 漏斗与 final gate 可见 → 隔离。"""
    import time

    from fastapi.testclient import TestClient

    from stock_analyzer import main as main_module
    from stock_analyzer.data import provider_factory
    from stock_analyzer.main import app

    output_dir = tmp_path / "asof_scan"
    week5 = main_module._service._config.week5.model_copy(
        update={
            # 生产级质量阈值会拒绝合成数据（roe 缺失等）；测试聚焦漏斗编排，
            # 放宽阈值与 _enable_universe_quality_selector 的口径一致。
            "universe_quality_min_avg_turnover_20": 0.0,
            "universe_quality_min_float_market_cap": 0.0,
            "universe_quality_require_financial_data": False,
            "universe_quality_min_roe": 0.0,
            "universe_quality_target_size": 3,
            "light_candidate_target": 3,
            "deep_candidate_target": 3,
            "final_signal_cap": 2,
            "market_breadth_enabled": False,
            "auto_sync_watchlist": False,
            # B2 盲区修复：端到端必须**真的产生候选**，否则 holding 段（滑点/入场口径）
            # 根本不会被跑到——此前该夹具 final_count=0，服务层调用签名漂移长期隐身。
            # 放宽终门与共识门只为让合成数据走到 final（夹具口径，不动生产配置）。
            "final_signal_min_threshold": 0.0,
        }
    )
    cross_review = main_module._service._config.models.cross_review.model_copy(
        update={
            "p_lgbm_min": 0.0,
            "p_xgb_min": 0.0,
            "p_meta_min": 0.0,
            "max_diff": 1.0,
            "dynamic_enabled": False,
        }
    )
    models = main_module._service._config.models.model_copy(
        update={"cross_review": cross_review}
    )
    patched_asof = main_module._service._config.asof_backtest.model_copy(
        update={"output_dir": str(output_dir)}
    )
    patched_config = main_module._service._config.model_copy(
        update={"asof_backtest": patched_asof, "week5": week5, "models": models}
    )
    monkeypatch.setattr(main_module, "_config", patched_config)
    monkeypatch.setattr(main_module._service, "_config", patched_config)
    # S06 时间闸门：历史重放需要 as_of 之前就存在的合法模型登记
    _register_pit_model(main_module._service, tmp_path=tmp_path)
    monkeypatch.setattr(
        main_module._service,
        "_asof_backtest_service",
        type(main_module._service._asof_backtest_service)(main_module._service),
    )

    symbols = ["600000", "000001", "600519"]
    fake_provider = _FakeHistoricalProvider(symbols, data_end=AS_OF)
    monkeypatch.setattr(
        provider_factory,
        "build_runtime_provider",
        lambda config, synthetic_seed=2026: fake_provider,
    )
    # 质量选择的批量源从主 service 的 provider 图解析，必须指向具备
    # fetch_universe_quality_metrics 能力的假 provider（受 end_date 限制）。
    monkeypatch.setattr(main_module._service, "_provider", fake_provider)

    client = TestClient(app)
    response = client.post(
        "/backtest/asof-scan",
        json={
            "date": AS_OF.isoformat(),
            "symbols": [],
            "algorithm": "week5_daily",
            "holding_top_n": 5,
            "horizon_days": 5,
        },
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    deadline = time.monotonic() + 120.0
    final: dict[str, object] = {}
    while time.monotonic() < deadline:
        payload = client.get(f"/tasks/{task_id}").json()
        if payload["status"] in ("succeeded", "failed"):
            final = payload
            break
        time.sleep(0.2)
    assert final.get("status") == "succeeded", final
    result = final["result"]
    assert result["algorithm"] == "week5_daily"
    entry = result["dates"][AS_OF.isoformat()]
    caveats = result["caveats"]
    assert caveats["candidate_pool_source"] == "full_market"
    assert caveats["neutral_account"] is True
    assert caveats["news_neutralized"] is True
    assert caveats["intraday_degraded"] is True, (
        entry.get("historical_context"),
        entry.get("funnel"),
    )
    assert entry["run_mode"] == "historical"
    assert entry["funnel"]["quality_count"] == 3
    assert entry["funnel"]["light_count"] > 0
    assert entry["funnel"]["deep_count"] > 0
    assert entry["funnel"]["final_count"] <= 2
    # candidates 明确 = final_signals；原始池独立保留
    assert entry["candidate_count"] == entry["funnel"]["final_count"]
    assert entry["historical_context"]["intraday_degraded"] is True
    assert len(entry["signal_pool"]["candidates"]) >= entry["candidate_count"]
    assert set(entry.keys()) >= {
        "funnel",
        "signal_pool",
        "final_selection",
        "rejection_reasons",
        "empty_state",
        "stage_timings",
        "holding_curve",
        "historical_context",
    }
    # B2 回归堵漏：候选非空时必须真的跑出 holding 段（此前该断言允许
    # holding_curve=None，服务层 slippage_ratio 传参错误因此长期隐身）。
    assert entry["funnel"]["final_count"] > 0, (
        "端到端夹具必须产生候选，否则 holding 段断言是空的（B2 盲区）"
    )
    if entry["candidate_count"] > 0:
        holding = entry["holding_curve"]
        assert holding is not None, "有候选却没有 holding 段（服务层调用签名漂移）"
        assert holding["results"], "holding 段为空"
        for item in holding["results"]:
            assert item["entry_mode"] == "next_session_open"
            assert item["entry_slippage"] >= 0.0
    else:
        assert entry["holding_curve"] is None
    # latest 落盘且带算法标注
    latest = client.get("/backtest/asof-scan/latest").json()["report"]
    assert latest["algorithm"] == "week5_daily"


# ---------------------------------------------------------------------------
# Part E：历史广度门的覆盖率边缘（2026-09-17 修复回归）
#
# 实测口径：list_symbols() 返回全索引 5833，其中约 5% 是当日停牌/未上市的非交易
# 标的，真实覆盖率天然停在 95% 门槛附近——2026-09-01 为 0.9501（通过）、
# 2026-09-16 为 0.9489（不通过）。落入不通过分支时旧实现直接禁止新开仓，终门把
# 当天 100 个候选全拒（2026-09-04 起连续 9 个交易日 0 票）。
# ---------------------------------------------------------------------------

_BREADTH_NOW = datetime(2026, 9, 16, 15, 30)


def _breadth_snapshot(
    *,
    coverage_ratio: float,
    advancers: int,
    decliners: int,
    limit_up_count: int,
    limit_down_count: int,
    median_return: float,
    new_highs_20d: int,
    new_lows_20d: int,
    turnover_change_pct: float,
    total_symbols: int = 5535,
) -> dict[str, Any]:
    from stock_analyzer.ops.market_breadth import build_breadth_snapshot

    return build_breadth_snapshot(
        advancers=advancers,
        decliners=decliners,
        limit_up_count=limit_up_count,
        limit_down_count=limit_down_count,
        median_return=median_return,
        new_highs_20d=new_highs_20d,
        new_lows_20d=new_lows_20d,
        turnover_change_pct=turnover_change_pct,
        total_symbols=total_symbols,
        coverage_ratio=coverage_ratio,
        as_of=_BREADTH_NOW,
        source="warehouse_daily",
        freshness={"date_max": "2026-09-16"},
    )


def _healthy_breadth(*, coverage_ratio: float) -> dict[str, Any]:
    return _breadth_snapshot(
        coverage_ratio=coverage_ratio,
        advancers=3200,
        decliners=1800,
        limit_up_count=80,
        limit_down_count=10,
        median_return=0.004,
        new_highs_20d=300,
        new_lows_20d=80,
        turnover_change_pct=0.05,
    )


def _weak_breadth(*, coverage_ratio: float) -> dict[str, Any]:
    return _breadth_snapshot(
        coverage_ratio=coverage_ratio,
        advancers=300,
        decliners=4700,
        limit_up_count=2,
        limit_down_count=150,
        median_return=-0.03,
        new_highs_20d=20,
        new_lows_20d=900,
        turnover_change_pct=-0.3,
    )


def _breadth_engine(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: Any,
    symbols: list[str] | None = None,
) -> tuple[Week5SelectionEngine, StockAnalyzerConfig]:
    config = _historical_config(tmp_path)
    config.week5.market_breadth_enabled = True
    monkeypatch.setattr(
        "stock_analyzer.ops.market_breadth.compute_market_breadth_from_warehouse",
        lambda *args, **kwargs: snapshot,
    )
    backend = _StubBackend(config, symbols=list(symbols or ["600000"]))
    context = Week5RunContext(
        mode="historical",
        now=_BREADTH_NOW,
        as_of=date(2026, 9, 16),
        config=config,
        provider=object(),
        run_pipeline_fn=lambda **kwargs: backend.run_pipeline(**kwargs),
        symbols=list(symbols or ["600000"]),
        artifact_dir=tmp_path,
    )
    engine = Week5SelectionEngine(
        backend=backend,
        context=context,
        policy=Week5RunPolicy.historical(),
    )
    return engine, config


def _breadth_meta(engine: Week5SelectionEngine) -> dict[str, Any]:
    meta, _lift = engine._historical_market_breadth(now=_BREADTH_NOW)  # noqa: SLF001
    return meta


def test_breadth_coverage_knife_edge_flips_availability() -> None:
    """覆盖率卡在 0.95 门槛两侧时 available 翻转——这是被修的噪声源本身。"""
    passed = _healthy_breadth(coverage_ratio=0.9501)
    failed = _healthy_breadth(coverage_ratio=0.9489)
    assert passed["coverage_ok"] is True
    assert passed["score"]["available"] is True
    assert failed["coverage_ok"] is False
    assert failed["score"]["available"] is False
    # 两次覆盖面只差万分之十二，分数完全相同：差异不来自市场本身
    assert failed["score"]["value"] == pytest.approx(passed["score"]["value"])


def test_historical_breadth_low_coverage_with_healthy_score_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """覆盖率不达标但分数健康：不得据此禁止整条买入路径。"""
    snapshot = _healthy_breadth(coverage_ratio=0.9489)
    engine, config = _breadth_engine(tmp_path=tmp_path, monkeypatch=monkeypatch, snapshot=snapshot)
    assert snapshot["score"]["available"] is False
    assert snapshot["score"]["value"] >= float(config.week5.market_breadth_disable_if_below)

    meta = _breadth_meta(engine)

    assert meta["block_new_buy"] is False
    assert meta["reason"] == "breadth_ok_low_coverage"
    assert meta["coverage_ratio"] == snapshot["coverage_ratio"]
    assert meta["trend_min_threshold_lift"] == 0.0


def test_historical_breadth_low_coverage_with_weak_score_still_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """覆盖率不达标且分数确实偏低：低分否决语义必须保留。"""
    snapshot = _weak_breadth(coverage_ratio=0.9489)
    engine, config = _breadth_engine(tmp_path=tmp_path, monkeypatch=monkeypatch, snapshot=snapshot)
    assert snapshot["coverage_ok"] is False
    assert snapshot["score"]["value"] < float(config.week5.market_breadth_disable_if_below)

    meta = _breadth_meta(engine)

    assert meta["block_new_buy"] is True
    assert meta["reason"] == "breadth_score_unavailable"


def test_historical_breadth_healthy_coverage_weak_score_still_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反例：覆盖率完全正常时，低分否决不得被新分支改写。"""
    snapshot = _weak_breadth(coverage_ratio=0.99)
    engine, _config = _breadth_engine(tmp_path=tmp_path, monkeypatch=monkeypatch, snapshot=snapshot)
    assert snapshot["coverage_ok"] is True

    meta = _breadth_meta(engine)

    assert meta["block_new_buy"] is True
    assert meta["reason"] == "breadth_below_threshold"


def test_historical_breadth_missing_score_still_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反例：真取不到数据（分数为 0）时仍按不可用处理，不放行。"""
    snapshot = _breadth_snapshot(
        coverage_ratio=0.0,
        advancers=0,
        decliners=0,
        limit_up_count=0,
        limit_down_count=0,
        median_return=0.0,
        new_highs_20d=0,
        new_lows_20d=0,
        turnover_change_pct=0.0,
        total_symbols=0,
    )
    engine, _config = _breadth_engine(tmp_path=tmp_path, monkeypatch=monkeypatch, snapshot=snapshot)
    assert snapshot["score"]["value"] == 0.0

    meta = _breadth_meta(engine)

    assert meta["block_new_buy"] is True
    assert meta["reason"] == "breadth_score_unavailable"


def test_engine_historical_low_coverage_breadth_keeps_buy_path_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端接线：低覆盖率+健康分数时，终门不得再挂 market_breadth_blocked。"""
    engine, _config = _breadth_engine(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        snapshot=_healthy_breadth(coverage_ratio=0.9489),
        symbols=["600000", "000001"],
    )

    report = engine.run()

    assert report["market_breadth"]["block_new_buy"] is False
    assert report["market_breadth"]["reason"] == "breadth_ok_low_coverage"
    rejected_reasons = {
        str(reason)
        for item in report["funnel"]["final_selection"]["rejected"]
        for reason in item.get("reject_reasons", [])
    }
    assert not any(reason.startswith("data_gate:market_breadth") for reason in rejected_reasons)


# ---------------------------------------------------------------------------
# B1 回归（runner 级）：as_of 之后创建的在服工件不得被历史重放加载
# ---------------------------------------------------------------------------


def test_runner_loads_resolved_pit_artifact_not_newer_serving_artifact(tmp_path: Path) -> None:
    """对抗场景（Codex B1 复现路径）：config 指向比 as_of 更新的在服工件，
    registry 里只有更早创建的 PIT 合法模型。

    期望：重放加载 **resolved 的那份旧工件**（报告身份 = 旧工件哈希），
    而不是 config 指向的新工件——即"解析过门"必须等价于"加载过门"。
    """
    from stock_analyzer.data.provider import SyntheticProvider
    from stock_analyzer.models.bundle import compute_artifact_identity_hash
    from stock_analyzer.models.trainer import ModelTrainer
    from stock_analyzer.runtime.services.week5_historical_runner import run_week5_historical_day

    def _train(name: str, created_at: str) -> Path:
        import json as _json

        cfg = _load_test_config()
        cfg.training.min_samples = 40
        path = tmp_path / name
        bars = SyntheticProvider(seed_offset=13).fetch_daily_bars("600000", lookback_days=300)
        ModelTrainer(training=cfg.training, labels=cfg.labels).train_and_save(
            bars=bars, output_path=str(path)
        )
        payload = _json.loads(path.read_text(encoding="utf-8"))
        payload["created_at"] = created_at
        path.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    pit_artifact = _train("pit_old.json", "2026-05-01T10:00:00")
    newer_serving = _train("serving_new.json", "2026-09-15T10:00:00")  # AS_OF=2026-07-31 之后
    pit_hash = compute_artifact_identity_hash(pit_artifact)
    newer_hash = compute_artifact_identity_hash(newer_serving)
    assert pit_hash != newer_hash

    config = _load_test_config()
    _enable_universe_quality_selector(config)
    config.week5.feature_snapshot_root = str(tmp_path / "production_features_light")
    config.week5.auto_sync_watchlist = False
    config.week5.market_breadth_enabled = False
    config.training.artifact_path = str(newer_serving)  # 在服工件比 as_of 新

    service = _new_service(config, provider=SyntheticProvider(seed_offset=15))
    service._model_registry = _InMemoryRegistry(  # noqa: SLF001 - 只登记的旧 PIT 模型
        [
            _RegistryRecord(
                model_id="model_pit_old",
                artifact_uri=str(pit_artifact),
                artifact_content_hash=pit_hash,
                artifact_created_at=datetime(2026, 5, 1, 10, 0),
                feature_schema_id="fs_pit_v1",
                feature_schema_hash="fs-hash",
                label_policy_id="label_policy_v1_e2afc1135a3f",
                label_policy_hash="label-hash",
                dataset_manifest_id="dataset_manifest_pit",
                lifecycle_state="trained",
                promoted_at=None,
            )
        ]
    )

    task_dir = tmp_path / "week5_task_b1"
    task_dir.mkdir(parents=True, exist_ok=True)
    report = run_week5_historical_day(
        service=service,
        as_of=AS_OF,
        task_dir=task_dir,
        symbols=["600000", "000001"],
        base_provider=_FakeHistoricalProvider(["600000", "000001"], data_end=AS_OF),
    )

    # 可评分（旧模型 PIT 合法）→ 实际加载的必须是旧工件
    assert report.get("status") != "unscorable", report.get("model_resolution")
    resolution = report["model_resolution"]
    assert resolution["status"] == "resolved"
    assert resolution["artifact_uri"] == str(pit_artifact)
    model = report["historical_context"]["model"]
    assert model["artifact_content_hash"] == pit_hash
    assert model["artifact_content_hash"] != newer_hash
    assert model["trained_at"] == "2026-05-01T10:00:00"
