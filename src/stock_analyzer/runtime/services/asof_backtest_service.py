"""历史回溯选股 + 持有期走势 API 服务层（PLAN Task 5）。

自成一体地落盘持久化，独立于 ``runtime_state.json`` 那套承载全局单例状态的
归档链路（多进程合并语义复杂，不适合承载这类按需触发的分析任务）。落盘目录
``config.asof_backtest.output_dir``（默认 ``artifacts/backtest/asof_scan``），
容器重启不丢；``latest.json`` 保存最近一次结果指针，``history.jsonl`` 追加
每次运行的精简摘要（供 ``GET .../history`` 分页，不含逐日走势明细，避免文件
无限增长）。

进程内并发保护用普通 ``threading.Lock``——本服务只在单个 API 进程内被
FastAPI ``BackgroundTasks`` worker 线程调用（而非像 scheduler 那样跨进程/
跨容器竞争），不需要 ``ops/file_lock.py`` 的跨进程分布式锁。
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pandas as pd

from stock_analyzer.backtest.asof_scan import AsofScanReport, run_asof_scan
from stock_analyzer.backtest.holding_curve import HoldingCurveReport, analyze_holding_curve
from stock_analyzer.backtest.matcher import ExecutionMatcher
from stock_analyzer.config import StockAnalyzerConfig
from stock_analyzer.market_calendar import is_a_share_trading_day

_HISTORY_FILENAME = "history.jsonl"
_LATEST_FILENAME = "latest.json"


def _read_intraday_coverage_until(config: StockAnalyzerConfig) -> str:
    """动态读取 intraday 摘要 DuckDB 实际覆盖到的最新日期（返工第 3 项）。

    之前 caveats 里的 ``intraday_coverage_until`` 是硬编码的 "2026-07-17"——
    一旦 Task 2 排查修复后数据链路恢复，这个硬编码值会一直显示错误的覆盖范围，
    标注反而变成误导。这里改为每次运行时动态读取
    ``vendor_intraday_summary.duckdb.manifest.json``（与
    ``vendor_zip_overlay.py`` 的 ``_intraday_manifest_path``/
    ``_load_intraday_manifest`` 完全同一份文件、同一种 schema：
    ``manifest["coverage"][interval]["max_date"]``），取所有 interval 里的
    最大日期作为整体覆盖上限（哪个 interval 更新就用哪个，取更宽松口径）。

    读取失败（路径未配置、文件不存在、JSON 损坏、schema 不含 coverage）时
    返回空字符串——不返回任何猜测值或旧硬编码值，前端已有的"未知日期"兜底
    分支可以直接复用这个空字符串。不在这里 raise，因为 caveats 字段本身是
    「附加说明」，不应该因为读取失败就让整个回测请求失败。
    """
    raw_path = str(config.data_source.intraday_summary_path or "").strip()
    if not raw_path:
        return ""
    db_path = Path(raw_path).expanduser()
    manifest_path = Path(str(db_path) + ".manifest.json")
    if not manifest_path.exists():
        return ""
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    coverage = payload.get("coverage")
    if not isinstance(coverage, dict):
        return ""
    max_dates: list[str] = []
    for interval_coverage in coverage.values():
        if isinstance(interval_coverage, dict):
            candidate = str(interval_coverage.get("max_date", "") or "").strip()
            if candidate:
                max_dates.append(candidate)
    if not max_dates:
        return ""
    return max(max_dates)


def _to_jsonable(value: Any) -> Any:
    """把 dataclass/date/datetime 递归转换成可 json.dumps 的原生结构。

    backtest/asof_scan.py 与 backtest/holding_curve.py 的返回值全部是
    ``@dataclass(slots=True)``，pipeline.PipelineSignal 同理；这里统一做
    一次递归转换，而不是在每个 dataclass 上手写 to_dict。
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return value


class AsofBacktestService:
    """历史回溯选股 + 持有期走势的编排、落盘与查询。

    与其它 ``runtime/services/*.py`` 子服务保持同一构造约定：只接收父
    ``StockAnalyzerService`` 实例，通过 ``self._service`` 访问配置与既有
    能力（watchlist、训练状态），不重复定义参数传递层。
    """

    def __init__(self, service: Any, *, project_root: Path | None = None) -> None:
        self._service = service
        config = cast(StockAnalyzerConfig, service._config)
        self._config = config
        root = project_root or Path(__file__).resolve().parents[3]
        self._output_dir = (root / config.asof_backtest.output_dir).resolve()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 核心编排
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        symbols: list[str] | None,
        start_date: date,
        end_date: date,
        top_n: int | None = None,
        horizon_days: int | None = None,
    ) -> dict[str, object]:
        """跑一次 as-of 扫描 + 持有期走势分析，并落盘持久化。

        Args:
            symbols: 候选标的清单；None 时使用 watchlist_provider() 的默认值。
            start_date/end_date: 目标 as_of 日期区间（含端点，逐个交易日）。
                单日回测时 start_date == end_date。
            top_n: 候选标的裁剪上限；None 时用 config 默认值。
            horizon_days: 持有期交易日数；None 时用 config 默认值。
        """
        config = self._config
        resolved_top_n = top_n if top_n is not None else config.asof_backtest.default_top_n
        resolved_horizon = (
            horizon_days if horizon_days is not None else config.asof_backtest.default_horizon_days
        )
        resolved_symbols = list(symbols) if symbols else list(self._service.watchlist_symbols())
        # 候选池来源标注（返工第 2 项）：显式传入 symbols 时无 watchlist 偏差；
        # 未传入而回退到当前 watchlist 时，watchlist 的构成受回测日之后的信息
        # 影响（例如今天才加入关注池的票，用它去回测更早的历史日期，等价于
        # "用未来才知道的关注结果去筛选历史候选池"），这是比 intraday 降级更
        # 隐蔽的一种偏差来源，必须在 caveats 里显式标注而不是只在代码注释里
        # 说明。
        candidate_pool_source = "explicit" if symbols else "watchlist"
        candidate_pool_bias = candidate_pool_source == "watchlist"

        as_of_dates = _trading_days_in_range(start_date, end_date)
        bootstrap_status = self._service.training_bootstrap_status()
        model_trained_at = str(bootstrap_status.get("last_bootstrap_at", "") or "").strip()

        scan_report = run_asof_scan(
            config=config,
            symbols=resolved_symbols,
            as_of_dates=as_of_dates,
            top_n=resolved_top_n,
            model_trained_at=model_trained_at,
        )

        holding_reports: dict[str, HoldingCurveReport] = {}
        matcher = ExecutionMatcher(config.backtest_matcher, limit_rule=config.limit_rule)
        for as_of in as_of_dates:
            candidates = scan_report.candidates_for(as_of)
            if not candidates:
                continue
            bars_by_symbol = _fetch_extended_bars_for_holding_curve(
                config=config,
                symbols=[signal.symbol for signal in candidates],
                as_of=as_of,
                horizon_days=resolved_horizon,
            )
            holding_report = analyze_holding_curve(
                bars_by_symbol=bars_by_symbol,
                entry_date=as_of,
                matcher=matcher,
                horizon_days=resolved_horizon,
                take_profit_pct=config.asof_backtest.take_profit_pct,
                stop_loss_pct=config.asof_backtest.stop_loss_pct,
                symbols=[signal.symbol for signal in candidates],
            )
            holding_reports[as_of.isoformat()] = holding_report

        result = self._build_result_payload(
            scan_report=scan_report,
            holding_reports=holding_reports,
            start_date=start_date,
            end_date=end_date,
            horizon_days=resolved_horizon,
            candidate_pool_source=candidate_pool_source,
            candidate_pool_bias=candidate_pool_bias,
        )
        self._persist(result)
        return result

    def _build_result_payload(
        self,
        *,
        scan_report: AsofScanReport,
        holding_reports: dict[str, HoldingCurveReport],
        start_date: date,
        end_date: date,
        horizon_days: int,
        candidate_pool_source: str,
        candidate_pool_bias: bool,
    ) -> dict[str, object]:
        by_date: dict[str, object] = {}
        for as_of in scan_report.as_of_dates:
            date_key = as_of.isoformat()
            candidates = scan_report.candidates_for(as_of)
            errors = [
                _to_jsonable(item)
                for item in scan_report.results
                if item.as_of == as_of and item.status == "error"
            ]
            by_date[date_key] = {
                "as_of": date_key,
                "candidates": [_to_jsonable(signal) for signal in candidates],
                "candidate_count": len(candidates),
                "errors": errors,
                "holding_curve": (
                    _to_jsonable(holding_reports[date_key]) if date_key in holding_reports else None
                ),
            }
        return {
            "generated_at": datetime.now().isoformat(),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "horizon_days": horizon_days,
            "dates": by_date,
            "caveats": {
                **scan_report.caveats,
                "intraday_coverage_until": _read_intraday_coverage_until(self._config),
                "candidate_pool_source": candidate_pool_source,
                "candidate_pool_bias": candidate_pool_bias,
            },
        }

    # ------------------------------------------------------------------
    # 落盘持久化
    # ------------------------------------------------------------------

    def _persist(self, result: dict[str, object]) -> None:
        with self._lock:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            latest_path = self._output_dir / _LATEST_FILENAME
            latest_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            dates_payload = result.get("dates", {})
            dates_map: dict[str, object] = dates_payload if isinstance(dates_payload, dict) else {}
            total_candidates = 0
            for entry in dates_map.values():
                if isinstance(entry, dict):
                    total_candidates += int(cast(int, entry.get("candidate_count", 0)))
            history_entry = {
                "generated_at": result.get("generated_at"),
                "start_date": result.get("start_date"),
                "end_date": result.get("end_date"),
                "horizon_days": result.get("horizon_days"),
                "dates_scanned": list(dates_map.keys()),
                "total_candidates": total_candidates,
            }
            history_path = self._output_dir / _HISTORY_FILENAME
            with history_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(history_entry, ensure_ascii=False) + "\n")
            self._trim_history_locked()

    def _trim_history_locked(self) -> None:
        history_path = self._output_dir / _HISTORY_FILENAME
        if not history_path.exists():
            return
        limit = max(1, int(self._config.asof_backtest.history_limit))
        lines = history_path.read_text(encoding="utf-8").splitlines()
        if len(lines) <= limit:
            return
        trimmed = lines[-limit:]
        history_path.write_text("\n".join(trimmed) + "\n", encoding="utf-8")

    def latest(self) -> dict[str, object] | None:
        latest_path = self._output_dir / _LATEST_FILENAME
        if not latest_path.exists():
            return None
        try:
            payload = json.loads(latest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def history(self, limit: int = 20) -> dict[str, object]:
        history_path = self._output_dir / _HISTORY_FILENAME
        if not history_path.exists():
            return {"records": 0, "runs": []}
        lines = history_path.read_text(encoding="utf-8").splitlines()
        capped_limit = max(1, min(int(limit), len(lines) or 1))
        selected = lines[-capped_limit:]
        runs: list[dict[str, object]] = []
        for line in selected:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                runs.append(parsed)
        runs.reverse()  # newest first
        return {"records": len(runs), "runs": runs}


def _trading_days_in_range(start_date: date, end_date: date) -> list[date]:
    """按 A 股交易日历枚举 [start_date, end_date] 区间内的交易日（含端点）。

    非交易日（周末/法定节假日）直接跳过，而不是报错——调用方选中一个周末日期
    区间时，返回空列表比强行报错更符合"选区间"的直觉；上层 API 会在结果为空
    时给出明确提示而非 500。
    """
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    days: list[date] = []
    cursor = start_date
    while cursor <= end_date:
        if is_a_share_trading_day(cursor):
            days.append(cursor)
        cursor = cursor + timedelta(days=1)
    return days


def _fetch_extended_bars_for_holding_curve(
    *,
    config: StockAnalyzerConfig,
    symbols: list[str],
    as_of: date,
    horizon_days: int,
) -> dict[str, pd.DataFrame]:
    """为持有期走势分析取「entry_date 之后 horizon_days 根」的行情。

    与 as-of 扫描阶段的取数**故意不做 end_date 截断**——持有期走势分析的职责
    就是看 entry_date 之后的真实走势，这里取的是"未来"数据，属于分析本身的
    定义（不是防泄露断言要拦截的场景，防泄露断言只保护"选股决策"那一步）。
    直接复用离线 provider 链（build_runtime_provider），同样不触碰
    HybridRuntimeProvider。
    """
    from stock_analyzer.data.provider_factory import build_runtime_provider

    provider = build_runtime_provider(config.data_source, synthetic_seed=2026)
    lookback_days = max(250, horizon_days + 30)
    result: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        try:
            bars = provider.fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)
        except Exception:
            result[symbol] = pd.DataFrame()
            continue
        result[symbol] = bars
    return result
