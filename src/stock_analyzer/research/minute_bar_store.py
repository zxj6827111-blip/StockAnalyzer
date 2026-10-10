"""带**时刻**的分钟行情研究库（改进计划 §3.1 数据补齐 + §5 继续采集所需数据）。

为什么需要它：尾盘契约要求"14:30-14:50 每 5 分钟确认一次、只读当时已完成的
分钟 bar、成交用确认之后的下一根"。生产侧的
``intraday_summary_1m/5m`` 是**日级聚合**（``summarize_minute_bars`` 把
``normalize_vendor_minute_frame`` 已经拿到的 ``datetime`` 索引折成了一行一天），
所以时刻信息在源 ZIP 里存在，只是在落库时被丢掉了。本模块只做一件事：把同一批
源数据按分钟原样落到**独立研究库**，不碰生产仓库、不碰 runtime provider。

三条写进代码的约束：

1. ``price_basis`` 没有默认值，必须由调用方显式声明，且每行都带 ``source``。
   事后要能回答"这批分钟价到底是未复权还是前复权"。
2. 成交模拟只接受 ``price_basis='raw'``（``bars_for(require_raw=True)``）。
   用 QFQ 价格模拟成交是 ADR-002 明令禁止的，这里直接拒绝而不是告警。
3. bar 时刻语义必须声明（``bar_end`` / ``bar_start``）。源数据是本地 naive 时间，
   契约要的是**完成时刻**；``bar_start`` 会在读取时统一 +1 个间隔，
   而不是让调用方各自猜。
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    TrendStrategyContract,
)
from stock_analyzer.data.intraday_summary_builder import entry_symbol
from stock_analyzer.data.vendor_zip_overlay import normalize_vendor_minute_frame

MINUTE_TABLES: dict[str, str] = {"1m": "minute_bars_1min", "5m": "minute_bars_5min"}

PRICE_BASIS_RAW = "raw"
PRICE_BASIS_QFQ = "qfq"
PRICE_BASES = (PRICE_BASIS_RAW, PRICE_BASIS_QFQ)

SOURCE_VENDOR_ZIP = "vendor_zip"
SOURCE_TDX_VIPDOC = "tdx_vipdoc"
SOURCES = (SOURCE_VENDOR_ZIP, SOURCE_TDX_VIPDOC)

BAR_TIME_BAR_END = "bar_end"
BAR_TIME_BAR_START = "bar_start"
BAR_TIME_SEMANTICS = (BAR_TIME_BAR_END, BAR_TIME_BAR_START)

#: 研究库的默认落点。刻意不叫 market*.duckdb，避免被生产 provider 误读。
RESEARCH_DB_DEFAULT = "artifacts/research/tail_minute_bars.duckdb"

_INTERVAL_MINUTES = {"1m": 1, "5m": 5}


class MinuteStoreError(RuntimeError):
    """研究库自身的契约不成立（口径未声明、表缺失、时刻语义未知）。"""


def interval_minutes(interval: str) -> int:
    try:
        return _INTERVAL_MINUTES[str(interval)]
    except KeyError as exc:
        raise MinuteStoreError(
            f"unsupported interval {interval!r}; supported: {sorted(_INTERVAL_MINUTES)}"
        ) from exc


def _ddl(table: str) -> str:
    return f"""
        CREATE TABLE IF NOT EXISTS {table} (
            symbol VARCHAR NOT NULL,
            trade_date DATE NOT NULL,
            bar_time TIMESTAMP NOT NULL,
            open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
            volume DOUBLE, amount DOUBLE,
            up_limit DOUBLE, down_limit DOUBLE, trade_status VARCHAR,
            price_basis VARCHAR NOT NULL,
            bar_time_semantics VARCHAR NOT NULL,
            source VARCHAR NOT NULL,
            ingested_at TIMESTAMP NOT NULL,
            PRIMARY KEY (symbol, bar_time)
        )
    """


@dataclass(frozen=True)
class TailWindowCoverage:
    """尾盘窗口可重建性的量化结论。"""

    interval: str
    symbol_days: int
    complete_symbol_days: int
    days: int
    slots_per_day: int
    status: str
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "interval": self.interval,
            "symbol_days": self.symbol_days,
            "complete_symbol_days": self.complete_symbol_days,
            "days": self.days,
            "slots_per_day": self.slots_per_day,
            "status": self.status,
            **self.detail,
        }


class MinuteBarStore:
    """独立研究库：按分钟存 bar，读出来的形状直接就是契约要吃的形状。"""

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        import duckdb

        self.path = Path(str(path)).expanduser()
        if not read_only and self.path.parent != Path(""):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if read_only and not self.path.exists():
            raise MinuteStoreError(f"minute store does not exist: {self.path}")
        self._conn = duckdb.connect(database=str(self.path), read_only=read_only)
        self._read_only = read_only
        if not read_only:
            for table in MINUTE_TABLES.values():
                self._conn.execute(_ddl(table))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> MinuteBarStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- 写入 -------------------------------------------------------------

    def upsert_frame(
        self,
        frame: pd.DataFrame,
        *,
        interval: str,
        price_basis: str,
        bar_time_semantics: str,
        source: str,
    ) -> int:
        """写入 ``symbol / bar_time / open..amount (+可选 up_limit/...)`` 的帧。"""
        table = MINUTE_TABLES.get(str(interval))
        if table is None:
            raise MinuteStoreError(f"unsupported interval {interval!r}")
        if str(price_basis) not in PRICE_BASES:
            raise MinuteStoreError(
                f"price_basis must be declared as one of {PRICE_BASES}, got {price_basis!r}"
            )
        if str(bar_time_semantics) not in BAR_TIME_SEMANTICS:
            raise MinuteStoreError(
                f"bar_time_semantics must be one of {BAR_TIME_SEMANTICS}, "
                f"got {bar_time_semantics!r}"
            )
        if str(source) not in SOURCES:
            raise MinuteStoreError(f"source must be one of {SOURCES}, got {source!r}")
        if frame is None or frame.empty:
            return 0

        step = interval_minutes(interval)
        rows: list[tuple[Any, ...]] = []
        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        records = frame.reset_index() if isinstance(
            frame.index, pd.DatetimeIndex) else frame
        for record in records.to_dict("records"):
            symbol = str(record.get("symbol") or "").strip()
            bar_time = _as_datetime(record.get("bar_time") or record.get("datetime"))
            if not symbol or bar_time is None:
                continue
            if str(bar_time_semantics) == BAR_TIME_BAR_START:
                bar_time = bar_time + timedelta(minutes=step)
            close = _num(record.get("close"))
            if close is None or close <= 0:
                continue
            rows.append((
                symbol, bar_time.date(), bar_time,
                _num(record.get("open")), _num(record.get("high")),
                _num(record.get("low")), close,
                _num(record.get("volume"), 0.0), _num(record.get("amount"), 0.0),
                _num(record.get("up_limit")), _num(record.get("down_limit")),
                (str(record.get("trade_status")).strip()
                 if record.get("trade_status") is not None else None),
                str(price_basis), str(bar_time_semantics), str(source), now,
            ))
        if not rows:
            return 0
        columns = (
            "symbol", "trade_date", "bar_time", "open", "high", "low", "close", "volume",
            "amount", "up_limit", "down_limit", "trade_status", "price_basis",
            "bar_time_semantics", "source", "ingested_at",
        )
        # DuckDB 的 executemany 是 Python 层逐行绑定参数：实测 9.6 万行分钟 bar 要 ~100 秒，
        # 而同一批数据读源只用 1.4 秒 —— 瓶颈整个在写入侧。改成注册临时帧再批量 INSERT，
        # 列名显式列出（不依赖表定义里的列顺序），主键去重语义仍是 INSERT OR REPLACE。
        staging_view = "minute_bar_staging"
        staged = pd.DataFrame(rows, columns=list(columns))
        self._conn.register(staging_view, staged)
        try:
            self._conn.execute(
                f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) "
                f"SELECT {', '.join(columns)} FROM {staging_view}"
            )
        finally:
            self._conn.unregister(staging_view)
        return len(rows)

    # --- 读取 -------------------------------------------------------------

    def bars_for(
        self,
        symbol: str,
        trading_day: date | datetime,
        *,
        interval: str = "1m",
        require_raw: bool = True,
        day_limits: Mapping[str, Any] | None = None,
    ) -> list[tuple[datetime, dict[str, Any]]]:
        """返回契约要求的 ``[(完成时刻, bar 字典), ...]``。

        ``require_raw=True`` 时，只要这一天里出现过非 raw 口径就整体拒绝 ——
        混着复权价算成交，等于给同一笔收益换了一把尺子。

        ``day_limits`` 用来补精确涨跌停与交易状态：分钟源 CSV 只有
        ``datetime/open/high/low/close/volume/amount``，**没有** ``up_limit``，
        而契约的硬门要精确价（tushare ``stk_limit`` / ``suspend_d``，日级）。
        不传就按分钟源原样返回，契约自己会判 ``no_valid_price_data``。
        逐根 bar 已有的字段优先于传进来的日级值（分钟级证据比日级更细）。
        """
        table = MINUTE_TABLES.get(str(interval))
        if table is None:
            raise MinuteStoreError(f"unsupported interval {interval!r}")
        day = trading_day.date() if isinstance(trading_day, datetime) else trading_day
        rows = self._conn.execute(
            f"SELECT bar_time, open, high, low, close, volume, amount, up_limit,"
            f" down_limit, trade_status, price_basis FROM {table} "
            f"WHERE symbol = ? AND trade_date = ? ORDER BY bar_time",
            [str(symbol), day],
        ).fetchall()
        if not rows:
            return []
        bases = {str(row[10]) for row in rows}
        if require_raw and bases != {PRICE_BASIS_RAW}:
            raise MinuteStoreError(
                f"{symbol} {day} 分钟价口径是 {sorted(bases)}，不是纯 raw；"
                "成交模拟拒绝使用复权价（ADR-002）"
            )
        out: list[tuple[datetime, dict[str, Any]]] = []
        for row in rows:
            bar: dict[str, Any] = {
                "open": row[1], "high": row[2], "low": row[3], "close": row[4],
                "volume": row[5], "amount": row[6],
            }
            # 状态列没有就**留空**：填 "normal" 等于把"不知道"说成"可交易"，
            # 而且 setdefault 会让 day_limits 里来自 suspend_d 的权威状态永远进不来。
            if row[9] is not None:
                bar["trade_status"] = row[9]
            if row[7] is not None:
                bar["up_limit"] = row[7]
            if row[8] is not None:
                bar["down_limit"] = row[8]
            for key, value in (day_limits or {}).items():
                if value is not None:
                    bar.setdefault(str(key), value)
            out.append((_as_datetime(row[0]) or datetime.min, bar))
        return out

    def days_with_bars(self, *, interval: str = "1m") -> list[date]:
        table = MINUTE_TABLES[str(interval)]
        rows = self._conn.execute(
            f"SELECT DISTINCT trade_date FROM {table} ORDER BY trade_date"
        ).fetchall()
        return [row[0] if isinstance(row[0], date) else _as_date(row[0]) for row in rows]

    def tail_window_coverage(
        self,
        *,
        contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
        interval: str = "1m",
        start: date | None = None,
        end: date | None = None,
        min_complete_symbol_days: int = 1,
    ) -> TailWindowCoverage:
        """逐 (symbol, day) 检查：确认点有没有已完成 bar、其后有没有可成交 bar。"""
        table = MINUTE_TABLES.get(str(interval))
        if table is None:
            raise MinuteStoreError(f"unsupported interval {interval!r}")
        step = interval_minutes(interval)
        slot_minutes = [_minute_of(hhmm) for hhmm in contract.confirmation_slots]
        window_start = _minute_of(contract.entry_window_start) - step
        window_end = _minute_of(contract.entry_window_end) + 2 * step
        where = ["date_part('hour', bar_time) * 60 + date_part('minute', bar_time)"
                 " BETWEEN ? AND ?"]
        params: list[Any] = [window_start, window_end]
        if start is not None:
            where.append("trade_date >= ?")
            params.append(start)
        if end is not None:
            where.append("trade_date <= ?")
            params.append(end)
        rows = self._conn.execute(
            f"SELECT symbol, trade_date, "
            f"array_agg(DISTINCT date_part('hour', bar_time) * 60 "
            f"+ date_part('minute', bar_time)) "
            f"FROM {table} WHERE {' AND '.join(where)} GROUP BY symbol, trade_date",
            params,
        ).fetchall()

        complete = 0
        missing_next_bar = 0
        no_slot_bar = 0
        days: set[date] = set()
        for _symbol, trade_date, minutes in rows:
            day = trade_date if isinstance(trade_date, date) else _as_date(trade_date)
            days.add(day)
            present = {int(value) for value in (minutes or [])}
            usable_slots = [slot for slot in slot_minutes if slot in present]
            if not usable_slots:
                no_slot_bar += 1
                continue
            last_slot = max(usable_slots)
            if any(value > last_slot for value in present):
                complete += 1
            else:
                missing_next_bar += 1

        symbol_days = len(rows)
        status = (
            "ok" if complete >= max(1, int(min_complete_symbol_days))
            else ("insufficient" if symbol_days else "blocked")
        )
        return TailWindowCoverage(
            interval=str(interval),
            symbol_days=symbol_days,
            complete_symbol_days=complete,
            days=len(days),
            slots_per_day=len(slot_minutes),
            status=status,
            detail={
                "no_slot_bar_symbol_days": no_slot_bar,
                "missing_next_bar_symbol_days": missing_next_bar,
                "confirmation_slot_minutes": slot_minutes,
                "step_minutes": step,
            },
        )


# --- 源数据适配 -----------------------------------------------------------


def read_vendor_zip_minutes(
    archive_path: str | Path,
    *,
    symbols: Iterable[str] | None = None,
    volume_multiplier: float = 100.0,
    amount_multiplier: float = 1.0,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """从 vendor 分钟 ZIP 里读**带时刻**的 bar，复用现有归一化函数。

    源 CSV 的 ``datetime`` 列是本地 naive 时间；本函数不改语义、只搬运，
    语义由调用方在 ``upsert_frame(bar_time_semantics=...)`` 里声明。
    """
    wanted = {str(symbol) for symbol in symbols} if symbols else None
    path = Path(str(archive_path))
    if not path.exists():
        raise MinuteStoreError(f"vendor minute archive does not exist: {path}")
    frames: list[pd.DataFrame] = []
    with zipfile.ZipFile(path) as archive:
        grouped: dict[str, list[str]] = {}
        for info in archive.infolist():
            if info.is_dir() or info.filename.startswith("__MACOSX/"):
                continue
            symbol = entry_symbol(info.filename)
            if not symbol or (wanted is not None and symbol not in wanted):
                continue
            grouped.setdefault(symbol, []).append(info.filename)
        for symbol, entry_names in sorted(grouped.items()):
            pieces: list[pd.DataFrame] = []
            for entry_name in entry_names:
                try:
                    with archive.open(entry_name) as stream:
                        raw = pd.read_csv(stream)
                except (KeyError, OSError, ValueError, pd.errors.ParserError):
                    continue
                normalized = normalize_vendor_minute_frame(
                    raw,
                    volume_multiplier=volume_multiplier,
                    amount_multiplier=amount_multiplier,
                )
                if not normalized.empty:
                    pieces.append(normalized)
            if not pieces:
                continue
            frame = pd.concat(pieces, axis=0, sort=False)
            frame = frame[~frame.index.duplicated(keep="last")].sort_index()
            out = frame.reset_index().rename(columns={frame.index.name or "index": "bar_time"})
            out.insert(0, "symbol", symbol)
            out["bar_time"] = pd.to_datetime(out["bar_time"], errors="coerce")
            out = out.dropna(subset=["bar_time"])
            if start is not None:
                out = out.loc[out["bar_time"].dt.date >= start]
            if end is not None:
                out = out.loc[out["bar_time"].dt.date <= end]
            if not out.empty:
                frames.append(out)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=0, ignore_index=True)


def read_tdx_vipdoc_minutes(
    *,
    vipdoc_root: str | Path,
    symbol: str,
    interval: str = "1m",
) -> pd.DataFrame:
    """复用 ``read_tdx_minute_bars`` 把本地 vipdoc 分钟文件转成同一形状。"""
    from stock_analyzer.data.intraday_summary import read_tdx_minute_bars

    frame = read_tdx_minute_bars(
        vipdoc_root=vipdoc_root, symbol=symbol, interval=str(interval)
    )
    if frame is None or frame.empty:
        return pd.DataFrame()
    out = frame.reset_index()
    out.columns = ["bar_time" if col in ("datetime", "index") else col
                   for col in out.columns]
    if "bar_time" not in out.columns:
        return pd.DataFrame()
    out.insert(0, "symbol", str(symbol))
    out["bar_time"] = pd.to_datetime(out["bar_time"], errors="coerce")
    return out.dropna(subset=["bar_time"])


def coverage_report(
    store: MinuteBarStore,
    *,
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
    interval: str = "1m",
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    days = store.days_with_bars(interval=interval)
    return {
        "interval": interval,
        "table": MINUTE_TABLES[interval],
        "db_path": str(store.path),
        "distinct_days": len(days),
        "first_day": days[0].isoformat() if days else None,
        "last_day": days[-1].isoformat() if days else None,
        "tail_window": store.tail_window_coverage(
            contract=contract, interval=interval, start=start, end=end
        ).as_dict(),
    }


def _minute_of(hhmm: str) -> int:
    parts = str(hhmm).strip().split(":")
    if len(parts) < 2:
        raise MinuteStoreError(f"invalid clock literal {hhmm!r}, expected HH:MM[:SS]")
    return int(parts[0]) * 60 + int(parts[1])


def _as_datetime(value: Any) -> datetime | None:
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, time):
        return datetime.combine(date.today(), value)
    try:
        parsed = pd.to_datetime(value)
    except (ValueError, TypeError):
        return None
    return None if pd.isna(parsed) else parsed.to_pydatetime()


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = pd.to_datetime(value)
    moment = parsed.to_pydatetime()
    return date(moment.year, moment.month, moment.day)


def _num(value: Any, default: float | None = None) -> float | None:
    if value is None or value is pd.NA:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if pd.isna(out) else out


__all__ = [
    "BAR_TIME_BAR_END",
    "BAR_TIME_BAR_START",
    "BAR_TIME_SEMANTICS",
    "MINUTE_TABLES",
    "PRICE_BASES",
    "PRICE_BASIS_QFQ",
    "PRICE_BASIS_RAW",
    "RESEARCH_DB_DEFAULT",
    "SOURCES",
    "SOURCE_TDX_VIPDOC",
    "SOURCE_VENDOR_ZIP",
    "MinuteBarStore",
    "MinuteStoreError",
    "TailWindowCoverage",
    "coverage_report",
    "interval_minutes",
    "read_tdx_vipdoc_minutes",
    "read_vendor_zip_minutes",
]
