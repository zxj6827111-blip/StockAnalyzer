"""As-of 截止日市场数据提供者（Week5 历史回测数据契约）。

包装一条离线 provider 链，对**所有**取数入口强制执行同一个 as_of 语义：

1. ``fetch_daily_bars``：生效截止日取 ``min(end_date, as_of)``，取回后立即
   断言 ``bars.index.max() <= as_of``——底层链若违背契约（例如包装层吞掉了
   ``end_date`` 参数），这里抛 ``FutureDataLeakError`` 而不是静默产出乐观结果。
2. ``fetch_universe_quality_metrics``（批量质量数据）：同样先截断后断言，
   作为选择器批量粗筛的 as-of 批量源（``UniverseQualityBatchSource`` 兼容）。
3. ``fetch_intraday_summary/summaries``（分钟摘要）：as-of 语义下逐日索引
   截断到 ``<= as_of``；分钟摘要表本身是按日聚合的历史表，多出的未来行属于
   泄露，直接裁剪并断言。
4. 其余能力（``list_symbols``/``status``/``latest_daily_dates`` 等）原样
   透传：``list_symbols`` 是"完整 provider 索引"，历史股票池的生成入口；
   ``latest_daily_dates`` 透传给 feature snapshot 增量判定（脏判定基于
   per-symbol 最新日期，仍由调用方以 as_of 上下文解释）。

本类不持有任何可变状态、不做缓存、绝不回退到实时 overlay——它只是把
"截止日"从调用约定变成类型保障。
"""

from __future__ import annotations

from datetime import date
from typing import Any, cast

import pandas as pd

from stock_analyzer.data.provider import FutureDataLeakError


class AsOfMarketDataProvider:
    """对底层 provider 链执行统一 as-of 截止裁剪与未来数据断言。"""

    def __init__(self, base: Any, as_of: date) -> None:
        self._base = base
        self._as_of = as_of

    @property
    def as_of(self) -> date:
        return self._as_of

    # ------------------------------------------------------------------
    # 核心取数入口（截断 + 断言）
    # ------------------------------------------------------------------
    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        effective_end = self._effective_end(end_date)
        bars = cast(
            pd.DataFrame,
            self._base.fetch_daily_bars(
                symbol=symbol,
                lookback_days=max(1, int(lookback_days)),
                end_date=effective_end,
            ),
        )
        if isinstance(bars, pd.DataFrame) and not bars.empty:
            self._assert_no_future_rows(bars.index, symbol=symbol)
        return bars

    def fetch_universe_quality_metrics(
        self,
        *,
        symbols: list[str],
        lookback_days: int,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        effective_end = self._effective_end(end_date)
        frame = cast(
            pd.DataFrame,
            self._base.fetch_universe_quality_metrics(
                symbols=list(symbols or []),
                lookback_days=max(1, int(lookback_days)),
                end_date=effective_end,
            ),
        )
        if isinstance(frame, pd.DataFrame) and not frame.empty and "date" in frame.columns:
            dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
            if not dates.empty and dates.max().date() > self._as_of:
                raise FutureDataLeakError(
                    "as-of leak detected in universe quality metrics: "
                    f"as_of={self._as_of.isoformat()} but max date={dates.max().date().isoformat()}"
                )
        return frame

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        frame = self._base.fetch_intraday_summary(
            symbol=symbol,
            interval=interval,
            lookback_days=max(1, int(lookback_days)),
        )
        return self._truncate_intraday_frame(frame, symbol=symbol)

    def fetch_intraday_summaries(
        self,
        symbols: list[str],
        interval: str,
        lookback_days: int = 120,
    ) -> dict[str, pd.DataFrame]:
        payload = self._base.fetch_intraday_summaries(
            symbols=list(symbols or []),
            interval=interval,
            lookback_days=max(1, int(lookback_days)),
        )
        if not isinstance(payload, dict):
            return cast(dict[str, pd.DataFrame], payload)
        return {
            str(key): self._truncate_intraday_frame(frame, symbol=str(key))
            for key, frame in payload.items()
        }

    # ------------------------------------------------------------------
    # 透传能力（不改变语义）
    # ------------------------------------------------------------------
    def list_symbols(self) -> list[str]:
        """完整 provider 索引（历史股票池的生成入口，不做退市名单过滤）。"""
        return cast(list[str], self._base.list_symbols())

    def latest_daily_dates(self, *, symbols: list[str] | None = None) -> dict[str, date]:
        latest_fn = getattr(self._base, "latest_daily_dates", None)
        if not callable(latest_fn):
            raise AttributeError("base provider has no latest_daily_dates capability")
        payload = latest_fn(symbols=symbols)
        if not isinstance(payload, dict):
            return cast(dict[str, date], payload)
        # as-of 视角：晚于 as_of 的日期对历史不可见（未来上市/未来同步的行）。
        return {
            str(key): value
            for key, value in payload.items()
            if not isinstance(value, date) or value <= self._as_of
        }

    def status(self) -> dict[str, Any]:
        status_fn = getattr(self._base, "status", None)
        payload: dict[str, Any] = dict(status_fn()) if callable(status_fn) else {}
        # 只追加 as_of 标注，绝不新增 provider_key/provider_mode 之类的键：
        # feature snapshot 的 source_signature 在构建时取 provider status、
        # 校验时（snapshot_is_current）不带 status，二者必须逐字段一致，
        # 这里多注入任何键都会让快照永远无法通过 current 校验。
        payload.setdefault("as_of", self._as_of.isoformat())
        return payload

    def clear_cache(self) -> None:
        clear_fn = getattr(self._base, "clear_cache", None)
        if callable(clear_fn):
            clear_fn()

    def __getattr__(self, name: str) -> Any:
        # 其余能力原样透传（注意 __getattr__ 只在常规属性查找失败时触发，
        # 不会遮蔽本类已定义的方法）。避免初始化阶段 self._base 缺失时的
        # 无限递归。
        if name.startswith("_") or name in {"_base", "_as_of"}:
            raise AttributeError(name)
        return getattr(self._base, name)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _effective_end(self, end_date: date | None) -> date:
        if end_date is not None and end_date < self._as_of:
            return end_date
        return self._as_of

    def _truncate_intraday_frame(self, frame: object, *, symbol: str) -> pd.DataFrame:
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return cast(pd.DataFrame, frame)
        index = frame.index
        if isinstance(index, pd.DatetimeIndex):
            if index.max().date() > self._as_of:
                frame = frame[index <= pd.Timestamp(self._as_of)]
            return frame
        if "date" in frame.columns:
            dates = pd.to_datetime(frame["date"], errors="coerce")
            if dates.notna().any() and dates.max().date() > self._as_of:
                frame = frame[dates <= pd.Timestamp(self._as_of)]
            return frame
        # 索引与列都不含日期：无法判定是否泄露，fail-closed 直接拒绝。
        raise FutureDataLeakError(
            f"as-of leak check failed for intraday frame of {symbol}: "
            "frame has neither DatetimeIndex nor date column"
        )

    def _assert_no_future_rows(self, index: pd.Index, *, symbol: str) -> None:
        if isinstance(index, pd.DatetimeIndex):
            max_date = index.max().date()
        else:
            try:
                parsed = pd.to_datetime(pd.Index(index), errors="coerce")
            except Exception as exc:  # noqa: BLE001 - 防御性：异常索引按泄露处理
                raise FutureDataLeakError(
                    f"as-of leak check failed for {symbol}: unreadable bar index"
                ) from exc
            if parsed.isna().all():
                raise FutureDataLeakError(
                    f"as-of leak check failed for {symbol}: unreadable bar index"
                )
            max_date = parsed.max().date()
        if max_date > self._as_of:
            raise FutureDataLeakError(
                f"future data leak detected for {symbol}: "
                f"as_of={self._as_of.isoformat()} but bars max date={max_date.isoformat()}"
            )
