"""主题价格确认（命门）：商品/期货近 1~3 日真实涨跌幅超阈值才激活主题。

防"听消息追高"：主题事件必须伴随对应商品价格异动才允许进入 boost/注入。
阈值冻结于 shadow 数据收集前（参照 Phase 1.5 多重比较教训），不可因
"信号太少"调低。

实现策略：一期用 akshare ``futures_zh_daily_sina``（新浪期货日线，主力连
续合约代码）按商品注册表拉日线，计算近 1 日 / 3 日涨跌幅。注册表把知识
库里的商品中文名映射到合约代码；未注册的商品记 unresolved（不激活、不报
错）。接口失败记 unconfirmed（当日该主题无法确认——fail-closed）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

import pandas as pd

from stock_analyzer.theme.taxonomy import ThemeConfirmation

_FUTURES_DAILY_FUNC = "futures_zh_daily_sina"

# 商品中文名 → 新浪主力连续合约代码（Phase 1 spike 清单，扩展只改这里）
# 来源：akshare futures_zh_daily_sina(symbol=...) 支持的连续合约代码。
COMMODITY_CONTRACT_REGISTRY: dict[str, str] = {
    "SC原油": "SC0",
    "BRENT": "B0",
    "白糖": "SR0",
    "棕榈油": "P0",
    "橡胶": "RU0",
    "豆粕": "M0",
    "动力煤": "ZC0",
    "螺纹钢": "RB0",
    "沪铜": "CU0",
    "黄金": "AU0",
    "原油": "SC0",
}


@dataclass(slots=True)
class CommodityConfirmation:
    """单个商品的价格确认结果。"""

    commodity: str
    contract: str = ""
    last_close: float | None = None
    move_1d: float | None = None
    move_3d: float | None = None
    confirmed: bool = False
    status: str = "unresolved"  # unresolved | ok | unconfirmed | resolved_empty
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "commodity": self.commodity,
            "contract": self.contract,
            "last_close": self.last_close,
            "move_1d": self.move_1d,
            "move_3d": self.move_3d,
            "confirmed": self.confirmed,
            "status": self.status,
            "error": self.error,
        }


@dataclass(slots=True)
class ThemeConfirmationResult:
    """单个主题族的价格确认汇总：任一商品确认即主题确认。"""

    theme_id: str
    direction: int = 1
    confirmed: bool = False
    confirmed_by: list[str] = field(default_factory=list)
    commodities: list[CommodityConfirmation] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "theme_id": self.theme_id,
            "direction": self.direction,
            "confirmed": self.confirmed,
            "confirmed_by": list(self.confirmed_by),
            "commodities": [item.to_dict() for item in self.commodities],
        }


class PriceConfirmationAdapter:
    """拉取商品期货日线并做阈值确认（注入 ak_module 便于测试）。"""

    def __init__(
        self,
        confirmation: ThemeConfirmation | None = None,
        registry: Mapping[str, str] | None = None,
        ak_module: object | None = None,
    ) -> None:
        self._confirmation = confirmation or ThemeConfirmation()
        self._registry = dict(registry or COMMODITY_CONTRACT_REGISTRY)
        self._ak_module = ak_module
        self._frame_cache: dict[str, pd.DataFrame | None] = {}

    @property
    def registry(self) -> dict[str, str]:
        return dict(self._registry)

    def confirm_commodity(self, commodity: str) -> CommodityConfirmation:
        """对单个商品做 1d/3d 阈值确认（未注册 → unresolved 不激活）。"""
        normalized = str(commodity).strip()
        contract = self._registry.get(normalized, "")
        if not contract:
            return CommodityConfirmation(
                commodity=normalized,
                status="unresolved",
                error="commodity not in registry",
            )
        frame = self._fetch_daily(contract=contract)
        if frame is None:
            return CommodityConfirmation(
                commodity=normalized,
                contract=contract,
                status="unconfirmed",
                error="fetch_failed",
            )
        move_1d, move_3d, last_close = _compute_moves(
            frame,
            lookback_days=self._confirmation.lookback_days,
        )
        if move_1d is None and move_3d is None:
            return CommodityConfirmation(
                commodity=normalized,
                contract=contract,
                status="resolved_empty",
                error="insufficient_bars",
            )
        confirmed = _meets_threshold(
            move_1d=move_1d,
            move_3d=move_3d,
            thresholds=self._confirmation,
        )
        return CommodityConfirmation(
            commodity=normalized,
            contract=contract,
            last_close=last_close,
            move_1d=move_1d,
            move_3d=move_3d,
            confirmed=confirmed,
            status="ok",
        )

    def confirm_theme(
        self,
        *,
        theme_id: str,
        commodities: list[str],
        direction: int = 1,
    ) -> ThemeConfirmationResult:
        """主题级确认：任一价格确认商品达到阈值（方向一致）即主题激活。

        direction=+1 的主题要求商品涨幅达到阈值（正向异动）；direction=-1
        要求跌幅达到绝对值阈值。混排商品按各自方向确认。
        """
        results: list[CommodityConfirmation] = []
        confirmed_by: list[str] = []
        for commodity in commodities:
            item = self.confirm_commodity(commodity)
            results.append(item)
            if item.confirmed and _direction_match(item=item, direction=direction):
                confirmed_by.append(item.commodity)
        return ThemeConfirmationResult(
            theme_id=theme_id,
            direction=direction,
            confirmed=bool(confirmed_by),
            confirmed_by=confirmed_by,
            commodities=results,
        )

    def _fetch_daily(self, *, contract: str) -> pd.DataFrame | None:
        if contract in self._frame_cache:
            cached = self._frame_cache[contract]
            return cached
        ak = self._resolve_ak_module()
        frame: pd.DataFrame | None = None
        if ak is not None:
            func = getattr(ak, _FUTURES_DAILY_FUNC, None)
            if callable(func):
                try:
                    payload = func(symbol=contract)
                    if isinstance(payload, pd.DataFrame):
                        frame = payload
                except Exception:
                    frame = None
        self._frame_cache[contract] = frame
        return frame

    def _resolve_ak_module(self) -> object | None:
        if self._ak_module is not None:
            return self._ak_module
        try:
            import akshare as ak  # type: ignore[import-untyped]
        except Exception:
            return None
        return cast(object, ak)


def _compute_moves(
    frame: pd.DataFrame,
    *,
    lookback_days: int,
) -> tuple[float | None, float | None, float | None]:
    """从日线算近 1 日 / 3 日涨跌幅与最新收盘价。

    返回 (move_1d, move_3d, last_close)；数据不足（<2 行或无有效收盘价）
    时对应值为 None。涨跌幅 = 期末/期初 - 1。
    """
    if frame is None or frame.empty:
        return None, None, None
    close_col = _pick_close_column(frame.columns)
    if close_col is None:
        return None, None, None
    closes = pd.to_numeric(frame[close_col], errors="coerce").dropna()
    if len(closes) < 2:
        return None, None, None
    last = float(closes.iloc[-1])
    base_1d = float(closes.iloc[-2])
    window = min(len(closes), max(2, int(lookback_days) + 1))
    base_3d = float(closes.iloc[-window])
    move_1d = last / base_1d - 1.0 if base_1d > 0 else None
    move_3d = last / base_3d - 1.0 if base_3d > 0 else None
    return move_1d, move_3d, last


def _meets_threshold(
    *,
    move_1d: float | None,
    move_3d: float | None,
    thresholds: ThemeConfirmation,
) -> bool:
    """任一窗口达到阈值即确认（1d 或 3d，取先到者）。"""
    if move_1d is not None and abs(move_1d) >= thresholds.price_move_1d_min:
        return True
    if move_3d is not None and abs(move_3d) >= thresholds.price_move_3d_min:
        return True
    return False


def _direction_match(*, item: CommodityConfirmation, direction: int) -> bool:
    """方向一致性：+1 主题要求正向异动，-1 要求负向异动。"""
    if direction < 0:
        return _negative_move(item)
    return _positive_move(item)


def _positive_move(item: CommodityConfirmation) -> bool:
    thresholds_pass_1d = item.move_1d is not None and item.move_1d > 0
    thresholds_pass_3d = item.move_3d is not None and item.move_3d > 0
    return thresholds_pass_1d or thresholds_pass_3d


def _negative_move(item: CommodityConfirmation) -> bool:
    thresholds_pass_1d = item.move_1d is not None and item.move_1d < 0
    thresholds_pass_3d = item.move_3d is not None and item.move_3d < 0
    return thresholds_pass_1d or thresholds_pass_3d


def _pick_close_column(columns: pd.Index) -> str | None:
    for col in columns:
        text = str(col).strip().lower()
        if "close" in text or "收盘" in text:
            return str(col)
    return None
