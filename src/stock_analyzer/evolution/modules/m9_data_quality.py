"""M9 data quality inspection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

_REQUIRED_FIELDS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True, slots=True)
class M9InspectionResult:
    """M9 inspection output."""

    frozen_symbols: tuple[str, ...]
    degraded: bool
    blackout_day: bool
    freeze_reasons: dict[str, str]


def inspect_data_quality(
    records: Sequence[Mapping[str, object]],
    required_fields: tuple[str, ...] = _REQUIRED_FIELDS,
) -> M9InspectionResult:
    """Inspect records and freeze symbols with invalid critical fields.

    Freeze rules:
    1. Any required field missing or None.
    2. ``volume <= 0``.

    blackout_day rules:
    1. Any record explicitly marks ``blackout_day=True``.
    2. No records available.
    3. All symbols are frozen.

    Args:
        records: Daily market records with at least ``symbol`` and OHLCV fields.
        required_fields: Required data fields for validity checks.

    Returns:
        Inspection result with freeze list and day-level flags.
    """
    if not records:
        return M9InspectionResult(
            frozen_symbols=(),
            degraded=True,
            blackout_day=True,
            freeze_reasons={"*": "empty_records"},
        )

    frozen: dict[str, str] = {}
    all_symbols: set[str] = set()
    explicit_blackout = False

    for record in records:
        symbol_raw = record.get("symbol", "UNKNOWN")
        symbol = str(symbol_raw)
        all_symbols.add(symbol)
        explicit_blackout = explicit_blackout or bool(record.get("blackout_day", False))

        missing = [field for field in required_fields if record.get(field) is None]
        if missing:
            frozen[symbol] = f"missing_fields:{','.join(sorted(missing))}"
            continue

        volume_value = record.get("volume")
        try:
            numeric_volume = float(volume_value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            frozen[symbol] = "invalid_volume"
            continue
        if numeric_volume <= 0.0:
            frozen[symbol] = "volume_non_positive"

    frozen_symbols = tuple(sorted(frozen))
    degraded = len(frozen_symbols) > 0
    blackout_day = explicit_blackout or (
        len(all_symbols) > 0 and len(frozen_symbols) == len(all_symbols)
    )

    return M9InspectionResult(
        frozen_symbols=frozen_symbols,
        degraded=degraded,
        blackout_day=blackout_day,
        freeze_reasons=frozen,
    )
