"""In-memory portfolio book and trade audit log."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal, TypedDict

from stock_analyzer.types import PipelineSignal

TradeSide = Literal["buy", "sell"]


class ManualFillPayload(TypedDict):
    entry_price: float | None
    quantity: int | None
    fee: float
    account: str
    manual_trade_time: str
    note: str


class ManualCloseFillPayload(TypedDict):
    exit_price: float | None
    quantity: int | None
    fee: float
    account: str
    manual_trade_time: str
    note: str


@dataclass(slots=True)
class PositionRecord:
    symbol: str
    strategy: str
    target_position: float
    opened_at: datetime
    updated_at: datetime
    open_trace_id: str
    open_reason: str
    status: str = "open"
    entry_price: float | None = None
    quantity: int | None = None
    fee: float = 0.0
    account: str = ""
    manual_trade_time: str = ""
    note: str = ""
    sector_tag: str = ""
    take_profit_stage: int = 0
    peak_price: float | None = None
    peak_pnl_pct: float = 0.0

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["opened_at"] = self.opened_at.isoformat()
        payload["updated_at"] = self.updated_at.isoformat()
        return payload


@dataclass(slots=True)
class TradeRecord:
    trade_id: str
    side: TradeSide
    symbol: str
    strategy: str
    target_position: float
    timestamp: datetime
    trace_id: str
    reason: str
    entry_price: float | None = None
    quantity: int | None = None
    fee: float = 0.0
    account: str = ""
    manual_trade_time: str = ""
    note: str = ""
    exit_price: float | None = None
    exit_quantity: int | None = None
    exit_fee: float = 0.0
    exit_account: str = ""
    exit_trade_time: str = ""
    exit_note: str = ""

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["timestamp"] = self.timestamp.isoformat()
        return payload


class PortfolioBook:
    """Track open positions and append trade logs."""

    def __init__(self, max_holdings: int, max_hold_days: int, max_same_sector: int = 0) -> None:
        self._max_holdings = max(1, max_holdings)
        self._max_hold_days = max(1, max_hold_days)
        self._max_same_sector = max(0, max_same_sector)
        self._positions: dict[str, PositionRecord] = {}
        self._trades: list[TradeRecord] = []
        self._trade_seq = 0

    def apply_signals(
        self,
        trace_id: str,
        timestamp: datetime,
        signals: list[PipelineSignal],
    ) -> dict[str, int]:
        closed_expired = self._close_expired(timestamp)
        opened = 0
        adjusted = 0
        skipped_limit = 0
        skipped_same_sector = 0
        for signal in signals:
            if signal.action != "buy":
                continue
            existing = self._positions.get(signal.symbol)
            if existing is not None:
                existing.target_position = signal.target_position
                existing.updated_at = timestamp
                adjusted += 1
                continue

            if len(self._positions) >= self._max_holdings:
                skipped_limit += 1
                continue

            sector_tag = _infer_sector_tag(signal.symbol)
            if (
                self._max_same_sector > 0
                and self._count_open_positions_by_sector(sector_tag) >= self._max_same_sector
            ):
                skipped_same_sector += 1
                continue

            position = PositionRecord(
                symbol=signal.symbol,
                strategy=signal.strategy,
                target_position=signal.target_position,
                opened_at=timestamp,
                updated_at=timestamp,
                open_trace_id=trace_id,
                open_reason=";".join(signal.reasons),
                sector_tag=sector_tag,
            )
            self._positions[signal.symbol] = position
            self._append_trade(
                side="buy",
                symbol=signal.symbol,
                strategy=signal.strategy,
                target_position=signal.target_position,
                timestamp=timestamp,
                trace_id=trace_id,
                reason=signal.reasons[-1] if signal.reasons else "signal_buy",
            )
            opened += 1

        return {
            "opened": opened,
            "adjusted": adjusted,
            "closed_expired": closed_expired,
            "skipped_max_holdings": skipped_limit,
            "skipped_same_sector": skipped_same_sector,
            "open_positions": len(self._positions),
        }

    def positions(self) -> list[dict[str, object]]:
        ordered = sorted(self._positions.values(), key=lambda item: item.opened_at)
        return [item.to_dict() for item in ordered]

    def trades(self, limit: int = 100) -> list[dict[str, object]]:
        capped = max(1, limit)
        return [item.to_dict() for item in self._trades[-capped:]]

    def export_state(self) -> dict[str, object]:
        return {
            "trade_seq": self._trade_seq,
            "positions": self.positions(),
            "trades": [item.to_dict() for item in self._trades],
        }

    def restore_state(self, payload: Mapping[str, object] | None) -> None:
        self._positions = {}
        self._trades = []
        self._trade_seq = 0
        if payload is None:
            return

        raw_positions = payload.get("positions")
        if isinstance(raw_positions, list):
            for item in raw_positions:
                position_record = _parse_position_record(item)
                if position_record is None:
                    continue
                self._positions[position_record.symbol] = position_record

        raw_trades = payload.get("trades")
        if isinstance(raw_trades, list):
            parsed_trades: list[TradeRecord] = []
            for item in raw_trades:
                trade_record = _parse_trade_record(item)
                if trade_record is not None:
                    parsed_trades.append(trade_record)
            parsed_trades.sort(key=lambda item: (item.timestamp, item.trade_id))
            self._trades = parsed_trades

        trade_seq = _safe_int(payload.get("trade_seq"))
        latest_trade_seq = max(
            (_trade_seq_from_id(item.trade_id) for item in self._trades),
            default=0,
        )
        self._trade_seq = max(trade_seq or 0, latest_trade_seq)

    def position_map(self) -> dict[str, float]:
        return {symbol: item.target_position for symbol, item in self._positions.items()}

    def sector_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self._positions.values():
            sector_tag = record.sector_tag or _infer_sector_tag(record.symbol)
            counts[sector_tag] = counts.get(sector_tag, 0) + 1
        return counts

    def update_target_position(
        self,
        *,
        symbol: str,
        target_position: float,
        timestamp: datetime,
        reason: str = "signal_adjust_position",
    ) -> bool:
        if target_position <= 0:
            return False
        record = self._positions.get(symbol)
        if record is None:
            return False
        record.target_position = target_position
        record.updated_at = timestamp
        record.open_reason = reason
        return True

    def close_position(
        self,
        symbol: str,
        timestamp: datetime,
        trace_id: str,
        reason: str = "manual_close",
        manual_fill: dict[str, object] | None = None,
    ) -> bool:
        record = self._positions.pop(symbol, None)
        if record is None:
            return False
        self._append_trade(
            side="sell",
            symbol=record.symbol,
            strategy=record.strategy,
            target_position=0.0,
            timestamp=timestamp,
            trace_id=trace_id,
            reason=reason,
            close_fill=manual_fill,
        )
        return True

    def reduce_position(
        self,
        *,
        symbol: str,
        target_position: float,
        timestamp: datetime,
        trace_id: str,
        reason: str = "signal_reduce_position",
        manual_fill: dict[str, object] | None = None,
    ) -> str:
        if target_position < 0:
            raise ValueError("target_position must be >= 0")
        record = self._positions.get(symbol)
        if record is None:
            return "missing"

        current_target = max(0.0, float(record.target_position))
        if current_target <= 0:
            return "missing"
        if target_position >= current_target - 1e-9:
            return "unchanged"
        if target_position <= 1e-9:
            closed = self.close_position(
                symbol=symbol,
                timestamp=timestamp,
                trace_id=trace_id,
                reason=reason,
                manual_fill=manual_fill,
            )
            return "closed" if closed else "missing"

        normalized_fill = _normalize_manual_close_fill_payload(manual_fill)
        current_quantity = (
            record.quantity if isinstance(record.quantity, int) and record.quantity > 0 else None
        )
        if current_quantity is not None:
            exit_quantity = normalized_fill["quantity"]
            if exit_quantity is None or exit_quantity <= 0:
                remaining_ratio = max(0.0, min(1.0, target_position / current_target))
                remaining_quantity = int(round(current_quantity * remaining_ratio))
                remaining_quantity = min(current_quantity, max(0, remaining_quantity))
                if remaining_quantity >= current_quantity:
                    remaining_quantity = current_quantity - 1
                exit_quantity = current_quantity - remaining_quantity
            else:
                exit_quantity = min(current_quantity, exit_quantity)
                remaining_quantity = max(0, current_quantity - exit_quantity)
            normalized_fill["quantity"] = exit_quantity if exit_quantity > 0 else None
            record.quantity = remaining_quantity if remaining_quantity > 0 else None

        record.target_position = max(0.0, float(target_position))
        record.updated_at = timestamp
        self._append_trade(
            side="sell",
            symbol=record.symbol,
            strategy=record.strategy,
            target_position=record.target_position,
            timestamp=timestamp,
            trace_id=trace_id,
            reason=reason,
            close_fill=normalized_fill,
        )
        return "trimmed"

    def annotate_position_state(
        self,
        *,
        symbol: str,
        timestamp: datetime | None = None,
        sector_tag: str | None = None,
        take_profit_stage: int | None = None,
        peak_price: float | None = None,
        peak_pnl_pct: float | None = None,
    ) -> bool:
        record = self._positions.get(symbol)
        if record is None:
            return False

        changed = False
        if sector_tag is not None:
            normalized_sector = _infer_sector_tag(symbol, sector_tag)
            if normalized_sector != record.sector_tag:
                record.sector_tag = normalized_sector
                changed = True
        if take_profit_stage is not None:
            normalized_stage = max(0, int(take_profit_stage))
            if normalized_stage != record.take_profit_stage:
                record.take_profit_stage = normalized_stage
                changed = True
        if peak_price is not None and peak_price > 0:
            normalized_peak = float(peak_price)
            if record.peak_price is None or normalized_peak > record.peak_price + 1e-9:
                record.peak_price = normalized_peak
                changed = True
        if peak_pnl_pct is not None:
            normalized_peak_pnl = max(record.peak_pnl_pct, float(peak_pnl_pct))
            if abs(normalized_peak_pnl - record.peak_pnl_pct) > 1e-9:
                record.peak_pnl_pct = normalized_peak_pnl
                changed = True
        if changed and timestamp is not None:
            record.updated_at = timestamp
        return changed

    def set_manual_position(
        self,
        symbol: str,
        strategy: str,
        target_position: float,
        timestamp: datetime,
        trace_id: str,
        reason: str = "manual_set_position",
        manual_fill: dict[str, object] | None = None,
        sector_tag: str = "",
    ) -> str:
        if target_position <= 0:
            raise ValueError("target_position must be > 0")

        normalized_fill = _normalize_manual_fill_payload(manual_fill)
        normalized_sector = _infer_sector_tag(symbol, sector_tag)
        existing = self._positions.get(symbol)
        if existing is not None:
            existing.target_position = target_position
            existing.updated_at = timestamp
            existing.open_reason = reason
            existing.sector_tag = normalized_sector
            _apply_manual_fill_to_position(existing, normalized_fill)
            self._append_trade(
                side="buy",
                symbol=symbol,
                strategy=strategy,
                target_position=target_position,
                timestamp=timestamp,
                trace_id=trace_id,
                reason="manual_adjust_position",
                manual_fill=normalized_fill,
            )
            return "adjusted"

        if (
            self._max_same_sector > 0
            and self._count_open_positions_by_sector(normalized_sector) >= self._max_same_sector
        ):
            return "rejected_same_sector"
        if len(self._positions) >= self._max_holdings:
            return "rejected_max_holdings"

        entry_price = normalized_fill["entry_price"]
        self._positions[symbol] = PositionRecord(
            symbol=symbol,
            strategy=strategy,
            target_position=target_position,
            opened_at=timestamp,
            updated_at=timestamp,
            open_trace_id=trace_id,
            open_reason=reason,
            entry_price=normalized_fill["entry_price"],
            quantity=normalized_fill["quantity"],
            fee=normalized_fill["fee"],
            account=normalized_fill["account"],
            manual_trade_time=normalized_fill["manual_trade_time"],
            note=normalized_fill["note"],
            sector_tag=normalized_sector,
            peak_price=float(entry_price)
            if isinstance(entry_price, (int, float)) and entry_price > 0
            else None,
        )
        self._append_trade(
            side="buy",
            symbol=symbol,
            strategy=strategy,
            target_position=target_position,
            timestamp=timestamp,
            trace_id=trace_id,
            reason=reason,
            manual_fill=normalized_fill,
        )
        return "opened"

    def _count_open_positions_by_sector(self, sector_tag: str) -> int:
        normalized_sector = _infer_sector_tag("", sector_tag)
        return sum(
            1
            for record in self._positions.values()
            if (record.sector_tag or _infer_sector_tag(record.symbol)) == normalized_sector
        )

    def _close_expired(self, now: datetime) -> int:
        expired_symbols: list[str] = []
        for symbol, record in self._positions.items():
            holding_days = (now.date() - record.opened_at.date()).days
            if holding_days >= self._max_hold_days:
                expired_symbols.append(symbol)

        for symbol in expired_symbols:
            record = self._positions.pop(symbol)
            self._append_trade(
                side="sell",
                symbol=record.symbol,
                strategy=record.strategy,
                target_position=0.0,
                timestamp=now,
                trace_id=record.open_trace_id,
                reason="max_hold_days_exit",
            )
        return len(expired_symbols)

    def _append_trade(
        self,
        side: TradeSide,
        symbol: str,
        strategy: str,
        target_position: float,
        timestamp: datetime,
        trace_id: str,
        reason: str,
        manual_fill: ManualFillPayload | dict[str, object] | None = None,
        close_fill: ManualCloseFillPayload | dict[str, object] | None = None,
    ) -> None:
        normalized_fill = _normalize_manual_fill_payload(manual_fill)
        normalized_close_fill = _normalize_manual_close_fill_payload(close_fill)
        self._trade_seq += 1
        self._trades.append(
            TradeRecord(
                trade_id=f"TRD-{self._trade_seq:08d}",
                side=side,
                symbol=symbol,
                strategy=strategy,
                target_position=target_position,
                timestamp=timestamp,
                trace_id=trace_id,
                reason=reason,
                entry_price=normalized_fill["entry_price"],
                quantity=normalized_fill["quantity"],
                fee=normalized_fill["fee"],
                account=normalized_fill["account"],
                manual_trade_time=normalized_fill["manual_trade_time"],
                note=normalized_fill["note"],
                exit_price=normalized_close_fill["exit_price"],
                exit_quantity=normalized_close_fill["quantity"],
                exit_fee=normalized_close_fill["fee"],
                exit_account=normalized_close_fill["account"],
                exit_trade_time=normalized_close_fill["manual_trade_time"],
                exit_note=normalized_close_fill["note"],
            )
        )


def _normalize_manual_fill_payload(
    manual_fill: Mapping[str, object] | None,
) -> ManualFillPayload:
    if manual_fill is None:
        return {
            "entry_price": None,
            "quantity": None,
            "fee": 0.0,
            "account": "",
            "manual_trade_time": "",
            "note": "",
        }
    entry_price_raw = manual_fill.get("entry_price")
    quantity_raw = manual_fill.get("quantity")
    fee_raw = manual_fill.get("fee")
    account_raw = manual_fill.get("account")
    trade_time_raw = manual_fill.get("manual_trade_time")
    note_raw = manual_fill.get("note")

    entry_price = _safe_float(entry_price_raw)
    if entry_price is not None and entry_price <= 0:
        entry_price = None
    quantity = _safe_int(quantity_raw)
    if quantity is not None and quantity <= 0:
        quantity = None
    fee = _safe_float(fee_raw)
    fee_value = fee if fee is not None and fee >= 0 else 0.0
    account = str(account_raw).strip() if isinstance(account_raw, str) else ""
    manual_trade_time = str(trade_time_raw).strip() if isinstance(trade_time_raw, str) else ""
    note = str(note_raw).strip() if isinstance(note_raw, str) else ""
    return {
        "entry_price": entry_price,
        "quantity": quantity,
        "fee": fee_value,
        "account": account,
        "manual_trade_time": manual_trade_time,
        "note": note,
    }


def _normalize_manual_close_fill_payload(
    close_fill: Mapping[str, object] | None,
) -> ManualCloseFillPayload:
    if close_fill is None:
        return {
            "exit_price": None,
            "quantity": None,
            "fee": 0.0,
            "account": "",
            "manual_trade_time": "",
            "note": "",
        }
    exit_price_raw = close_fill.get("exit_price")
    quantity_raw = close_fill.get("quantity")
    fee_raw = close_fill.get("fee")
    account_raw = close_fill.get("account")
    trade_time_raw = close_fill.get("manual_trade_time")
    note_raw = close_fill.get("note")

    exit_price = _safe_float(exit_price_raw)
    if exit_price is not None and exit_price <= 0:
        exit_price = None
    quantity = _safe_int(quantity_raw)
    if quantity is not None and quantity <= 0:
        quantity = None
    fee = _safe_float(fee_raw)
    fee_value = fee if fee is not None and fee >= 0 else 0.0
    account = str(account_raw).strip() if isinstance(account_raw, str) else ""
    manual_trade_time = str(trade_time_raw).strip() if isinstance(trade_time_raw, str) else ""
    note = str(note_raw).strip() if isinstance(note_raw, str) else ""
    return {
        "exit_price": exit_price,
        "quantity": quantity,
        "fee": fee_value,
        "account": account,
        "manual_trade_time": manual_trade_time,
        "note": note,
    }


def _apply_manual_fill_to_position(record: PositionRecord, manual_fill: ManualFillPayload) -> None:
    entry_price = manual_fill.get("entry_price")
    if isinstance(entry_price, (int, float)) and entry_price > 0:
        record.entry_price = float(entry_price)
    quantity = manual_fill.get("quantity")
    if isinstance(quantity, int) and quantity > 0:
        record.quantity = quantity
    fee = manual_fill.get("fee")
    if isinstance(fee, (int, float)) and fee >= 0:
        record.fee = float(fee)
    account = manual_fill.get("account")
    if isinstance(account, str):
        normalized = account.strip()
        if normalized:
            record.account = normalized
    manual_trade_time = manual_fill.get("manual_trade_time")
    if isinstance(manual_trade_time, str):
        normalized_trade_time = manual_trade_time.strip()
        if normalized_trade_time:
            record.manual_trade_time = normalized_trade_time
    note = manual_fill.get("note")
    if isinstance(note, str):
        normalized_note = note.strip()
        if normalized_note:
            record.note = normalized_note


def _parse_position_record(raw: object) -> PositionRecord | None:
    if not isinstance(raw, Mapping):
        return None
    symbol = str(raw.get("symbol", "")).strip()
    strategy = str(raw.get("strategy", "")).strip()
    opened_at = _safe_datetime(raw.get("opened_at"))
    updated_at = _safe_datetime(raw.get("updated_at"))
    if not symbol or not strategy or opened_at is None or updated_at is None:
        return None
    return PositionRecord(
        symbol=symbol,
        strategy=strategy,
        target_position=_safe_float(raw.get("target_position")) or 0.0,
        opened_at=opened_at,
        updated_at=updated_at,
        open_trace_id=str(raw.get("open_trace_id", "")).strip(),
        open_reason=str(raw.get("open_reason", "")).strip(),
        status=str(raw.get("status", "open")).strip() or "open",
        entry_price=_safe_float(raw.get("entry_price")),
        quantity=_safe_int(raw.get("quantity")),
        fee=_safe_float(raw.get("fee")) or 0.0,
        account=str(raw.get("account", "")).strip(),
        manual_trade_time=str(raw.get("manual_trade_time", "")).strip(),
        note=str(raw.get("note", "")).strip(),
        sector_tag=_infer_sector_tag(symbol, raw.get("sector_tag")),
        take_profit_stage=max(0, _safe_int(raw.get("take_profit_stage")) or 0),
        peak_price=_safe_float(raw.get("peak_price")),
        peak_pnl_pct=_safe_float(raw.get("peak_pnl_pct")) or 0.0,
    )


def _parse_trade_record(raw: object) -> TradeRecord | None:
    if not isinstance(raw, Mapping):
        return None
    trade_id = str(raw.get("trade_id", "")).strip()
    side = str(raw.get("side", "")).strip().lower()
    symbol = str(raw.get("symbol", "")).strip()
    strategy = str(raw.get("strategy", "")).strip()
    timestamp = _safe_datetime(raw.get("timestamp"))
    if (
        not trade_id
        or side not in {"buy", "sell"}
        or not symbol
        or not strategy
        or timestamp is None
    ):
        return None
    trade_side: TradeSide = "buy" if side == "buy" else "sell"
    return TradeRecord(
        trade_id=trade_id,
        side=trade_side,
        symbol=symbol,
        strategy=strategy,
        target_position=_safe_float(raw.get("target_position")) or 0.0,
        timestamp=timestamp,
        trace_id=str(raw.get("trace_id", "")).strip(),
        reason=str(raw.get("reason", "")).strip(),
        entry_price=_safe_float(raw.get("entry_price")),
        quantity=_safe_int(raw.get("quantity")),
        fee=_safe_float(raw.get("fee")) or 0.0,
        account=str(raw.get("account", "")).strip(),
        manual_trade_time=str(raw.get("manual_trade_time", "")).strip(),
        note=str(raw.get("note", "")).strip(),
        exit_price=_safe_float(raw.get("exit_price")),
        exit_quantity=_safe_int(raw.get("exit_quantity")),
        exit_fee=_safe_float(raw.get("exit_fee")) or 0.0,
        exit_account=str(raw.get("exit_account", "")).strip(),
        exit_trade_time=str(raw.get("exit_trade_time", "")).strip(),
        exit_note=str(raw.get("exit_note", "")).strip(),
    )


def _safe_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None


def _trade_seq_from_id(trade_id: str) -> int:
    raw = trade_id.strip()
    if not raw.startswith("TRD-"):
        return 0
    return _safe_int(raw.replace("TRD-", "")) or 0


def _safe_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def _safe_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return int(float(raw))
        except ValueError:
            return None
    return None


def _infer_sector_tag(symbol: str, raw_sector: object | None = None) -> str:
    if isinstance(raw_sector, str):
        normalized_sector = raw_sector.strip().upper()
        if normalized_sector:
            return normalized_sector
    digits = "".join(ch for ch in symbol if ch.isdigit())
    if len(digits) >= 3:
        return f"SEC-{digits[:3]}"
    normalized_symbol = symbol.strip().upper()
    return normalized_symbol[:6] if normalized_symbol else "UNKNOWN"
