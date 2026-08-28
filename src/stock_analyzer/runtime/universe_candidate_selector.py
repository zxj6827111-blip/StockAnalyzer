"""Universe candidate selector: batch quality filtering + scoring for the full market.

Replaces the previous ``_quota_sample_universe`` random board-quota sample as the
Week5 全市场入口. It takes the *complete* universe, batch-computes low-cost
indicators from the market warehouse in a single DuckDB pass, applies hard
eligibility filters, scores every eligible symbol with a configurable
``quality_score``, then selects ``target_size`` (default 300) symbols with
board-quotas applied *after* quality ranking plus a small deterministic
exploration slice.

The previous ``_quota_sample_universe`` is retained for degraded fallback and
for the exploration slice only; it is no longer the main 300-selector.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
from collections.abc import Callable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

import numpy as np
import pandas as pd


@runtime_checkable
class UniverseQualityBatchSource(Protocol):
    """Minimal batch quality-metrics contract for the selector.

    Anything with ``fetch_universe_quality_metrics`` may back the selector —
    a full ``MarketWarehouse``, a ``VendorZipOverlayProvider`` (ZIP history +
    delta overlay), or a wrapper around either. The selector never reaches
    into provider internals; it only calls this one batch method.

    ``end_date``（as-of 契约）：非 None 时批量源必须先截断到该日期再取最近
    N 根；历史回测的 as-of 粗筛依赖这个参数，避免混入未来行。
    """

    def fetch_universe_quality_metrics(
        self,
        *,
        symbols: list[str],
        lookback_days: int,
        end_date: date | None = None,
    ) -> pd.DataFrame: ...

# Five-board classification mirroring runtime.service._BOARD_ORDER / map.
# Re-implemented locally to avoid a circular import on service.
_BOARD_ORDER: tuple[str, ...] = (
    "SZ_MAIN",
    "SZ_GEM",
    "SH_MAIN",
    "SH_STAR",
    "BSE",
    "OTHER",
)
_BOARD_EXCHANGE_MAP: dict[str, str] = {
    "SZ_MAIN": "SZSE",
    "SZ_GEM": "SZSE",
    "SH_MAIN": "SSE",
    "SH_STAR": "SSE",
    "BSE": "BSE",
    "OTHER": "",
}

_DEFAULT_WEIGHTS: dict[str, float] = {
    "trend": 0.30,
    "capital_flow": 0.20,
    "price_volume": 0.15,
    "liquidity": 0.15,
    "fundamental": 0.20,
    "risk_penalty": 0.10,
}
_POSITIVE_WEIGHT_KEYS: tuple[str, ...] = (
    "trend",
    "capital_flow",
    "price_volume",
    "liquidity",
    "fundamental",
)
_SUCCESS_SELECTOR_MODES = {"quality", "quality_all_eligible"}

# Metric columns pulled from the warehouse batch frame.
_REQUIRED_COLUMNS = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "float_market_cap",
    "suspended",
    "is_st",
    "is_delisting_risk",
    "roe",
    "debt_ratio",
    "financial_data_complete",
    "financial_completeness",
    "background_data_complete",
    "holder_count",
    "northbound_net",
    "dragon_tiger_flag",
)


class UniverseCandidateSelector:
    """Select ``target_size`` quality candidates from the full market universe.

    The selector is intentionally a standalone module: it owns no service
    state, receives the warehouse + config-derived knobs, and returns a
    self-auditable report. Degraded fallback (when the batch capability is
    unavailable) is delegated to a caller-supplied ``fallback_sampler`` so this
    module never silently switches to per-symbol reads.
    """

    def __init__(
        self,
        *,
        warehouse: UniverseQualityBatchSource,
        weights: Mapping[str, float] | None = None,
        min_history_days: int = 60,
        min_avg_turnover_20: float = 0.0,
        min_float_market_cap: float = 0.0,
        min_batch_coverage_ratio: float = 0.90,
        max_staleness_days: int = 10,
        require_financial_data: bool = True,
        min_roe: float = 0.0,
        max_debt_ratio: float = 0.80,
        exploration_ratio: float = 0.05,
        min_quota_per_in_scope_board: int = 10,
        lookback_days: int = 240,
        snapshot_path: str | Path | None = None,
        snapshot_max_age_days: int = 7,
        fallback_sampler: Callable[..., tuple[list[str], dict[str, object]]] | None = None,
    ) -> None:
        self._warehouse = warehouse
        self._weights = _normalize_weights(weights)
        self._min_history_days = max(1, int(min_history_days))
        self._min_avg_turnover_20 = max(0.0, float(min_avg_turnover_20))
        self._min_float_market_cap = max(0.0, float(min_float_market_cap))
        self._min_batch_coverage_ratio = max(0.0, min(1.0, float(min_batch_coverage_ratio)))
        self._max_staleness_days = max(0, int(max_staleness_days))
        self._require_financial_data = bool(require_financial_data)
        self._min_roe = float(min_roe)
        self._max_debt_ratio = float(max_debt_ratio)
        self._exploration_ratio = max(0.0, min(1.0, float(exploration_ratio)))
        self._min_quota_per_in_scope_board = max(0, int(min_quota_per_in_scope_board))
        self._lookback_days = max(60, int(lookback_days))
        self._snapshot_path = Path(snapshot_path).expanduser() if snapshot_path else None
        self._snapshot_max_age_days = max(0, int(snapshot_max_age_days))
        self._fallback_sampler = fallback_sampler

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def select(
        self,
        *,
        symbols: list[str],
        target_size: int,
        trade_date: str,
        ruleset_id: str,
        board_scope: list[str],
        reference_date: date | datetime | str | None = None,
        end_date: date | None = None,
    ) -> dict[str, object]:
        """Select up to ``target_size`` symbols and return a full audit report.

        Returns ``{"selected": [...], "report": {...}}``. On batch failure the
        selector invokes ``fallback_sampler`` (when provided) and returns a
        ``degraded_fallback`` report with complete metadata.

        ``end_date``（as-of 契约）：非 None 时批量取数锚定该日期（先截断再取
        最近 N 根），且快照 fallback 一律禁用——生产 selection snapshot 写于
        当前时点，对历史日期属于未来信息，历史回测绝不读取。
        """
        target = max(1, int(target_size))
        normalized_input = _dedupe_preserve_order(
            [_normalize_a_share_symbol(s) for s in (symbols or []) if _normalize_a_share_symbol(s)]
        )
        input_hash = _symbol_hash(normalized_input)
        scope_set = {str(item).strip().upper() for item in board_scope if str(item).strip()}
        started_at = datetime.now()
        resolved_reference_date = _coerce_date(reference_date or trade_date)
        batch_stats: dict[str, object] = {
            "batch_symbol_count": 0,
            "missing_batch_symbol_count": len(normalized_input),
            "batch_coverage_ratio": 0.0,
            "missing_batch_symbols_sample": sorted(normalized_input)[:20],
        }

        batch_frame, fetch_error = self._safe_batch_fetch(normalized_input, end_date=end_date)
        if fetch_error or batch_frame.empty:
            return self._fallback_with_snapshot_or_quota(
                symbols=normalized_input,
                target_size=target,
                trade_date=trade_date,
                ruleset_id=ruleset_id,
                board_scope=board_scope,
                input_hash=input_hash,
                fallback_reason=fetch_error or "batch_metrics_unavailable",
                started_at=started_at,
                reference_date=resolved_reference_date,
                batch_stats=batch_stats,
                end_date=end_date,
            )

        missing_columns = sorted(set(_REQUIRED_COLUMNS) - set(batch_frame.columns))
        if missing_columns:
            return self._fallback_with_snapshot_or_quota(
                symbols=normalized_input,
                target_size=target,
                trade_date=trade_date,
                ruleset_id=ruleset_id,
                board_scope=board_scope,
                input_hash=input_hash,
                fallback_reason=f"schema_error:missing_required_columns:{','.join(missing_columns)}",
                started_at=started_at,
                reference_date=resolved_reference_date,
                batch_stats=batch_stats,
                end_date=end_date,
            )

        batch_frame = batch_frame.copy()
        batch_frame["symbol"] = batch_frame["symbol"].map(_normalize_a_share_symbol)
        requested_set = set(normalized_input)
        batch_frame = batch_frame[batch_frame["symbol"].isin(requested_set)]
        batch_symbols = set(batch_frame["symbol"].dropna().astype(str))
        missing_symbols = sorted(requested_set - batch_symbols)
        batch_symbol_count = len(batch_symbols)
        input_count = len(normalized_input)
        batch_coverage_ratio = batch_symbol_count / input_count if input_count else 1.0
        batch_stats = {
            "batch_symbol_count": batch_symbol_count,
            "missing_batch_symbol_count": len(missing_symbols),
            "batch_coverage_ratio": round(batch_coverage_ratio, 6),
            "missing_batch_symbols_sample": missing_symbols[:20],
        }
        if batch_coverage_ratio < self._min_batch_coverage_ratio:
            return self._fallback_with_snapshot_or_quota(
                symbols=normalized_input,
                target_size=target,
                trade_date=trade_date,
                ruleset_id=ruleset_id,
                board_scope=board_scope,
                input_hash=input_hash,
                fallback_reason=(
                    "batch_coverage_below_threshold:"
                    f"{batch_coverage_ratio:.6f}<{self._min_batch_coverage_ratio:.6f}"
                ),
                started_at=started_at,
                reference_date=resolved_reference_date,
                batch_stats=batch_stats,
                end_date=end_date,
            )

        try:
            metrics = _compute_quality_metrics(batch_frame, weights=self._weights)
        except Exception as exc:
            return self._fallback_with_snapshot_or_quota(
                symbols=normalized_input,
                target_size=target,
                trade_date=trade_date,
                ruleset_id=ruleset_id,
                board_scope=board_scope,
                input_hash=input_hash,
                fallback_reason=f"compute_error:{type(exc).__name__}",
                started_at=started_at,
                reference_date=resolved_reference_date,
                batch_stats=batch_stats,
                end_date=end_date,
            )
        try:
            eligible_rows, rejected_counts = self._hard_filter(
                metrics,
                scope_set=scope_set,
                reference_date=resolved_reference_date,
            )
        except Exception as exc:
            return self._fallback_with_snapshot_or_quota(
                symbols=normalized_input,
                target_size=target,
                trade_date=trade_date,
                ruleset_id=ruleset_id,
                board_scope=board_scope,
                input_hash=input_hash,
                fallback_reason=f"hard_filter_error:{type(exc).__name__}",
                started_at=started_at,
                reference_date=resolved_reference_date,
                batch_stats=batch_stats,
                end_date=end_date,
            )
        eligible_count = len(eligible_rows)

        if eligible_count == 0:
            return self._build_report(
                selected=[],
                target_size=target,
                trade_date=trade_date,
                ruleset_id=ruleset_id,
                board_scope=board_scope,
                input_symbols=normalized_input,
                input_hash=input_hash,
                eligible_rows=eligible_rows,
                rejected_counts=rejected_counts,
                core_selected=[],
                exploration_selected=[],
                selector_mode="quality_no_eligible",
                fallback_reason="",
                started_at=started_at,
                batch_calls=1,
                batch_stats=batch_stats,
                reference_date=resolved_reference_date,
            )

        # When the eligible pool is at or below target, return all of it.
        if eligible_count <= target:
            eligible_rows = eligible_rows.sort_values(
                ["quality_score", "symbol"], ascending=[False, True]
            ).reset_index(drop=True)
            selected_symbols = eligible_rows["symbol"].tolist()
            return self._build_report(
                selected=selected_symbols,
                target_size=target,
                trade_date=trade_date,
                ruleset_id=ruleset_id,
                board_scope=board_scope,
                input_symbols=normalized_input,
                input_hash=input_hash,
                eligible_rows=eligible_rows,
                rejected_counts=rejected_counts,
                core_selected=selected_symbols,
                exploration_selected=[],
                selector_mode="quality_all_eligible",
                fallback_reason="",
                started_at=started_at,
                batch_calls=1,
                batch_stats=batch_stats,
                reference_date=resolved_reference_date,
            )

        core_size, exploration_size = self._split_target(target)
        core_selected, board_quotas = self._select_core(
            eligible_rows=eligible_rows,
            core_size=core_size,
            scope_set=scope_set,
            trade_date=trade_date,
            ruleset_id=ruleset_id,
        )
        exploration_selected = self._select_exploration(
            eligible_rows=eligible_rows,
            core_selected=set(core_selected),
            exploration_size=exploration_size,
            scope_set=scope_set,
            trade_date=trade_date,
            ruleset_id=ruleset_id,
            target_size=target,
        )
        selected_symbols = _dedupe_preserve_order([*core_selected, *exploration_selected])
        return self._build_report(
            selected=selected_symbols,
            target_size=target,
            trade_date=trade_date,
            ruleset_id=ruleset_id,
            board_scope=board_scope,
            input_symbols=normalized_input,
            input_hash=input_hash,
            eligible_rows=eligible_rows,
            rejected_counts=rejected_counts,
            core_selected=core_selected,
            exploration_selected=exploration_selected,
            selector_mode="quality",
            fallback_reason="",
            started_at=started_at,
            batch_calls=1,
            board_quotas=board_quotas,
            batch_stats=batch_stats,
            reference_date=resolved_reference_date,
        )

    # ------------------------------------------------------------------
    # Batch fetch + hard filter
    # ------------------------------------------------------------------
    def _safe_batch_fetch(
        self, symbols: list[str], *, end_date: date | None = None
    ) -> tuple[pd.DataFrame, str]:
        try:
            frame = self._warehouse.fetch_universe_quality_metrics(
                symbols=symbols,
                lookback_days=self._lookback_days,
                end_date=end_date,
            )
            if not isinstance(frame, pd.DataFrame):
                return pd.DataFrame(), "batch_fetch_error:invalid_return_type"
            return frame, ""
        except Exception as exc:
            return pd.DataFrame(), f"batch_fetch_error:{type(exc).__name__}"

    def _hard_filter(
        self,
        metrics: pd.DataFrame,
        *,
        scope_set: set[str],
        reference_date: date | None,
    ) -> tuple[pd.DataFrame, dict[str, int]]:
        if metrics.empty:
            return metrics, {}
        rejected: dict[str, int] = {
            "invalid_code": 0,
            "out_of_board_scope": 0,
            "suspended": 0,
            "is_st": 0,
            "delisting_risk": 0,
            "invalid_history": 0,
            "insufficient_history": 0,
            "invalid_avg_turnover_20": 0,
            "low_avg_turnover_20": 0,
            "invalid_float_market_cap": 0,
            "low_float_market_cap": 0,
            "invalid_close": 0,
            "invalid_latest_data_date": 0,
            "stale_market_data": 0,
            "financial_data_incomplete": 0,
            "missing_roe": 0,
            "roe_below_min": 0,
            "missing_debt_ratio": 0,
            "debt_ratio_above_max": 0,
        }
        mask = pd.Series(True, index=metrics.index)

        # Vectorized A-share code validation: 6-digit string of digits.
        symbol_str = metrics["symbol"].astype(str)
        is_six_digits = symbol_str.str.len() == 6
        all_digits = symbol_str.str.isdigit()
        invalid_code = ~(is_six_digits & all_digits)
        rejected["invalid_code"] = int(invalid_code.sum())
        mask &= ~invalid_code

        # Board classification computed once, reused for eligible rows.
        board_series = metrics["symbol"].apply(_board_from_a_share_symbol)
        if scope_set:
            exchange_series = board_series.map(lambda b: _BOARD_EXCHANGE_MAP.get(b, ""))
            out_of_scope = ~exchange_series.isin(scope_set) | (exchange_series == "")
            rejected["out_of_board_scope"] = int((mask & out_of_scope).sum())
            mask &= ~out_of_scope

        suspended = _vectorized_coerce_bool(metrics["suspended"])
        rejected["suspended"] = int((mask & suspended).sum())
        mask &= ~suspended

        is_st = _vectorized_coerce_bool(metrics["is_st"])
        rejected["is_st"] = int((mask & is_st).sum())
        mask &= ~is_st

        delisting = _vectorized_coerce_bool(metrics["is_delisting_risk"])
        rejected["delisting_risk"] = int((mask & delisting).sum())
        mask &= ~delisting

        history_days = pd.to_numeric(metrics["history_days"], errors="coerce")
        invalid_history = ~pd.Series(
            np.isfinite(history_days.to_numpy(dtype=float)), index=metrics.index
        )
        rejected["invalid_history"] = int((mask & invalid_history).sum())
        mask &= ~invalid_history
        insufficient_history = history_days < self._min_history_days
        rejected["insufficient_history"] = int((mask & insufficient_history).sum())
        mask &= ~insufficient_history

        avg_turnover = pd.to_numeric(metrics["avg_turnover_20"], errors="coerce")
        invalid_turnover = ~pd.Series(
            np.isfinite(avg_turnover.to_numpy(dtype=float)), index=metrics.index
        )
        rejected["invalid_avg_turnover_20"] = int((mask & invalid_turnover).sum())
        mask &= ~invalid_turnover
        low_turnover = avg_turnover < self._min_avg_turnover_20
        rejected["low_avg_turnover_20"] = int((mask & low_turnover).sum())
        mask &= ~low_turnover

        float_market_cap = pd.to_numeric(metrics["float_market_cap"], errors="coerce")
        invalid_cap = ~pd.Series(
            np.isfinite(float_market_cap.to_numpy(dtype=float)), index=metrics.index
        )
        rejected["invalid_float_market_cap"] = int((mask & invalid_cap).sum())
        mask &= ~invalid_cap
        low_cap = float_market_cap < self._min_float_market_cap
        rejected["low_float_market_cap"] = int((mask & low_cap).sum())
        mask &= ~low_cap

        # Vectorized price validation: positive finite number.
        close_numeric = pd.to_numeric(metrics["latest_close"], errors="coerce")
        invalid_close = ~pd.Series(
            np.isfinite(close_numeric.to_numpy(dtype=float)), index=metrics.index
        ) | ~(close_numeric > 0.0)
        rejected["invalid_close"] = int((mask & invalid_close).sum())
        mask &= ~invalid_close

        latest_dates = pd.to_datetime(metrics["latest_data_date"], errors="coerce")
        invalid_latest_date = latest_dates.isna()
        rejected["invalid_latest_data_date"] = int((mask & invalid_latest_date).sum())
        mask &= ~invalid_latest_date
        if reference_date is not None:
            staleness_days = (pd.Timestamp(reference_date) - latest_dates).dt.days
            stale_data = staleness_days > self._max_staleness_days
            rejected["stale_market_data"] = int((mask & stale_data).sum())
            mask &= ~stale_data

        financial_complete = _vectorized_coerce_bool(metrics["financial_data_complete"])
        if self._require_financial_data:
            incomplete_financial = ~financial_complete
            rejected["financial_data_incomplete"] = int((mask & incomplete_financial).sum())
            mask &= ~incomplete_financial

        roe = pd.to_numeric(metrics["roe"], errors="coerce")
        finite_roe = pd.Series(np.isfinite(roe.to_numpy(dtype=float)), index=metrics.index)
        if self._require_financial_data:
            missing_roe = ~finite_roe
            rejected["missing_roe"] = int((mask & missing_roe).sum())
            mask &= ~missing_roe
        low_roe = finite_roe & (roe < self._min_roe)
        rejected["roe_below_min"] = int((mask & low_roe).sum())
        mask &= ~low_roe

        debt_ratio = pd.to_numeric(metrics["debt_ratio"], errors="coerce")
        finite_debt = pd.Series(np.isfinite(debt_ratio.to_numpy(dtype=float)), index=metrics.index)
        if self._require_financial_data:
            missing_debt = ~finite_debt
            rejected["missing_debt_ratio"] = int((mask & missing_debt).sum())
            mask &= ~missing_debt
        high_debt = finite_debt & (debt_ratio > self._max_debt_ratio)
        rejected["debt_ratio_above_max"] = int((mask & high_debt).sum())
        mask &= ~high_debt

        eligible = metrics.loc[mask].copy()
        # Reuse the already-computed board_series instead of re-applying.
        eligible["board"] = board_series.loc[mask].values
        return eligible, {k: v for k, v in rejected.items() if v > 0}

    # ------------------------------------------------------------------
    # Core + exploration selection
    # ------------------------------------------------------------------
    def _split_target(self, target_size: int) -> tuple[int, int]:
        if self._exploration_ratio <= 0.0:
            return target_size, 0
        exploration = int(round(target_size * self._exploration_ratio))
        exploration = max(0, min(exploration, target_size - 1))
        core = target_size - exploration
        return core, exploration

    def _select_core(
        self,
        *,
        eligible_rows: pd.DataFrame,
        core_size: int,
        scope_set: set[str],
        trade_date: str,
        ruleset_id: str,
    ) -> tuple[list[str], dict[str, dict[str, object]]]:
        """Guarantee a per-board floor, then fill globally by quality score."""
        eligible = eligible_rows.sort_values(
            ["quality_score", "symbol"], ascending=[False, True]
        ).reset_index(drop=True)

        groups: dict[str, list[str]] = {b: [] for b in _BOARD_ORDER}
        for symbol, board in zip(eligible["symbol"], eligible["board"], strict=True):
            groups.setdefault(board if board in _BOARD_ORDER else "OTHER", []).append(symbol)

        in_scope_boards = [
            b
            for b in _BOARD_ORDER
            if groups.get(b) and (not scope_set or _BOARD_EXCHANGE_MAP.get(b, "") in scope_set)
        ]
        pool_sizes = {b: len(groups[b]) for b in in_scope_boards}
        total_available = sum(pool_sizes.values())

        if total_available == 0:
            return [], {}

        effective_core = min(core_size, total_available)
        num_in_scope = len(in_scope_boards)
        min_quota = self._min_quota_per_in_scope_board
        if num_in_scope > 0:
            min_quota = min(min_quota, max(0, effective_core // num_in_scope))
        floor_quotas = {board: min(min_quota, pool_sizes[board]) for board in in_scope_boards}

        # First guarantee only the configured floor for each board. These are
        # not hard upper limits; all remaining slots compete globally.
        symbol_to_board = {symbol: board for board in in_scope_boards for symbol in groups[board]}
        selected: list[str] = []
        selected_set: set[str] = set()
        selected_counts: dict[str, int] = {b: 0 for b in in_scope_boards}
        for board in in_scope_boards:
            take = floor_quotas.get(board, 0)
            for symbol in groups[board]:
                if take <= 0:
                    break
                if symbol in selected_set:
                    continue
                selected.append(symbol)
                selected_set.add(symbol)
                take -= 1
                selected_counts[board] += 1

        # Every remaining slot is filled by the global quality ranking.
        deficit = effective_core - len(selected)
        if deficit > 0:
            for symbol in eligible["symbol"]:
                if deficit <= 0:
                    break
                if symbol in selected_set:
                    continue
                board = symbol_to_board.get(symbol)
                if board not in selected_counts:
                    continue
                selected.append(symbol)
                selected_set.add(symbol)
                selected_counts[board] += 1
                deficit -= 1

        boards_meta: dict[str, dict[str, object]] = {}
        for board in _BOARD_ORDER:
            in_scope = board in in_scope_boards
            boards_meta[board] = {
                "exchange": _BOARD_EXCHANGE_MAP.get(board, ""),
                "in_scope": in_scope,
                "input_count": len(groups.get(board, [])),
                "quota": floor_quotas.get(board, 0) if in_scope else 0,
                "selected_count": selected_counts.get(board, 0) if in_scope else 0,
            }
        quality_by_symbol = dict(zip(eligible["symbol"], eligible["quality_score"], strict=True))
        selected.sort(key=lambda symbol: (-_safe_float(quality_by_symbol.get(symbol)), symbol))
        return selected, boards_meta

    def _select_exploration(
        self,
        *,
        eligible_rows: pd.DataFrame,
        core_selected: set[str],
        exploration_size: int,
        scope_set: set[str],
        trade_date: str,
        ruleset_id: str,
        target_size: int,
    ) -> list[str]:
        if exploration_size <= 0 or self._exploration_ratio <= 0.0:
            return []
        eligible = eligible_rows.sort_values(
            ["quality_score", "symbol"], ascending=[False, True]
        ).reset_index(drop=True)
        pool = eligible[~eligible["symbol"].isin(core_selected)]
        if pool.empty:
            return []
        groups: dict[str, list[str]] = {b: [] for b in _BOARD_ORDER}
        for symbol, board in zip(pool["symbol"], pool["board"], strict=True):
            groups.setdefault(board if board in _BOARD_ORDER else "OTHER", []).append(symbol)
        in_scope_boards = [
            b
            for b in _BOARD_ORDER
            if groups.get(b) and (not scope_set or _BOARD_EXCHANGE_MAP.get(b, "") in scope_set)
        ]
        if not in_scope_boards:
            return []
        # Each board receives at most one guaranteed slot first; remaining
        # exploration capacity is then distributed by residual pool size.
        pool_sizes = {b: len(groups[b]) for b in in_scope_boards}
        quotas: dict[str, int] = {b: 0 for b in in_scope_boards}
        effective_size = min(exploration_size, sum(pool_sizes.values()))
        remaining = effective_size
        board_priority = sorted(in_scope_boards, key=lambda b: (-pool_sizes[b], b))
        for b in board_priority:
            if remaining <= 0:
                break
            quotas[b] = 1
            remaining -= 1

        while remaining > 0:
            room_by_board = {b: max(0, pool_sizes[b] - quotas[b]) for b in in_scope_boards}
            total_room = sum(room_by_board.values())
            if total_room <= 0:
                break
            raw_shares = {b: room_by_board[b] / total_room * remaining for b in in_scope_boards}
            floor_adds = {
                b: min(room_by_board[b], int(math.floor(raw_shares[b]))) for b in in_scope_boards
            }
            added = sum(floor_adds.values())
            for b, add in floor_adds.items():
                quotas[b] += add
            remaining -= added
            if remaining <= 0:
                break
            fractional_priority = sorted(
                in_scope_boards,
                key=lambda b: (
                    -(raw_shares[b] - math.floor(raw_shares[b])),
                    -room_by_board[b],
                    b,
                ),
            )
            assigned = False
            for b in fractional_priority:
                if remaining <= 0:
                    break
                room = pool_sizes[b] - quotas[b]
                if room > 0:
                    quotas[b] += 1
                    remaining -= 1
                    assigned = True
            if not assigned:
                break

        # Deterministic seeded sampling within each board (sorted pool for stability).
        seed_str = f"exploration|{trade_date}|{ruleset_id}|{target_size}|{exploration_size}"
        seed_int = int.from_bytes(hashlib.sha256(seed_str.encode("utf-8")).digest()[:8], "big")
        rng = random.Random(seed_int)
        sampled: list[str] = []
        for b in in_scope_boards:
            board_pool = sorted(groups[b])
            take = min(quotas[b], len(board_pool))
            if take <= 0:
                continue
            sampled.extend(rng.sample(board_pool, take))
        return sampled

    # ------------------------------------------------------------------
    # Report assembly + fallback
    # ------------------------------------------------------------------
    def _fallback_with_snapshot_or_quota(
        self,
        *,
        symbols: list[str],
        target_size: int,
        trade_date: str,
        ruleset_id: str,
        board_scope: list[str],
        input_hash: str,
        fallback_reason: str,
        started_at: datetime,
        reference_date: date | None,
        batch_stats: Mapping[str, object],
        end_date: date | None = None,
    ) -> dict[str, object]:
        snapshot_result, snapshot_unavailable_reason = (
            (None, "snapshot_disabled_for_asof")
            if end_date is not None
            else self._load_snapshot_fallback(
                symbols=symbols,
                target_size=target_size,
                ruleset_id=ruleset_id,
                board_scope=board_scope,
                reference_date=reference_date,
            )
        )
        if snapshot_result is not None:
            snapshot_selected, snapshot_payload, snapshot_meta = snapshot_result
            merged_selected = list(snapshot_selected)
            merged_payload = [dict(item) for item in snapshot_payload]
            selected_set = set(merged_selected)
            fallback_sampler_meta: dict[str, object] = {}
            fallback_sampler_error = ""
            missing_count = max(0, target_size - len(merged_selected))
            if missing_count > 0 and self._fallback_sampler is not None:
                remaining_symbols = [symbol for symbol in symbols if symbol not in selected_set]
                try:
                    sampled, sampler_meta = self._fallback_sampler(
                        remaining_symbols,
                        cap=missing_count,
                        board_scope=board_scope,
                        universe_ruleset_id=ruleset_id,
                        seed_trade_date=trade_date,
                    )
                    if isinstance(sampler_meta, dict):
                        fallback_sampler_meta = dict(sampler_meta)
                    scope_set = {
                        str(item).strip().upper() for item in board_scope if str(item).strip()
                    }
                    current_set = set(symbols)
                    for raw_symbol in sampled:
                        symbol = _normalize_a_share_symbol(raw_symbol)
                        if not symbol or symbol in selected_set or symbol not in current_set:
                            continue
                        board = _board_from_a_share_symbol(symbol)
                        exchange = _BOARD_EXCHANGE_MAP.get(board, "")
                        if scope_set and (not exchange or exchange not in scope_set):
                            continue
                        merged_selected.append(symbol)
                        selected_set.add(symbol)
                        merged_payload.append(
                            {
                                "symbol": symbol,
                                "score": 0.0,
                                "components": {},
                                "reason_codes": [
                                    "degraded_fallback",
                                    "snapshot_quota_topup",
                                ],
                            }
                        )
                        if len(merged_selected) >= target_size:
                            break
                except Exception as exc:
                    fallback_sampler_error = f"{type(exc).__name__}:{exc}"

            fallback_topup_count = len(merged_selected) - len(snapshot_selected)
            selected_by_board: dict[str, int] = {}
            for symbol in merged_selected:
                board = _board_from_a_share_symbol(symbol)
                selected_by_board[board] = selected_by_board.get(board, 0) + 1
            elapsed_ms = int((datetime.now() - started_at).total_seconds() * 1000)
            report: dict[str, object] = {
                "input_count": len(symbols),
                "target_size": target_size,
                "hard_eligible_count": 0,
                "rejected_count_by_reason": {},
                "selected_count": len(merged_selected),
                "core_selected_count": len(snapshot_selected),
                "exploration_selected_count": 0,
                "selected_by_board": selected_by_board,
                "score_distribution": _score_distribution(
                    [_safe_float(item.get("score")) for item in merged_payload]
                ),
                "selected": merged_payload,
                "trade_date": trade_date,
                "reference_date": reference_date.isoformat() if reference_date else "",
                "ruleset_id": ruleset_id,
                "selector_mode": "snapshot_fallback",
                "fallback_source": (
                    "quality_snapshot+quota_sampler"
                    if fallback_topup_count > 0
                    else "quality_snapshot"
                ),
                "fallback_reason": fallback_reason,
                "fallback_topup_count": fallback_topup_count,
                "fallback_sampler_error": fallback_sampler_error,
                "fallback_sampler_meta": fallback_sampler_meta,
                "snapshot_fallback_unavailable_reason": "",
                "input_symbol_hash": input_hash,
                "output_symbol_hash": _symbol_hash(merged_selected),
                "board_quotas": {},
                "applied_weights": dict(self._weights),
                "batch_calls": 1,
                "elapsed_ms": elapsed_ms,
                "generated_at": datetime.now().isoformat(),
                **dict(batch_stats),
                **snapshot_meta,
            }
            return {"selected": merged_selected, "report": report}

        fallback_selected: list[str] = []
        fallback_meta: dict[str, object] = {}
        fallback_sampler_error = ""
        if self._fallback_sampler is not None:
            try:
                fallback_selected, fallback_meta = self._fallback_sampler(
                    symbols,
                    cap=target_size,
                    board_scope=board_scope,
                    universe_ruleset_id=ruleset_id,
                    seed_trade_date=trade_date,
                )
            except Exception as exc:
                fallback_selected, fallback_meta = [], {}
                fallback_sampler_error = f"{type(exc).__name__}"
        raw_board_quotas = fallback_meta.get("boards", {})
        board_quotas = dict(raw_board_quotas) if isinstance(raw_board_quotas, dict) else {}
        output_hash = _symbol_hash(fallback_selected)
        elapsed_ms = int((datetime.now() - started_at).total_seconds() * 1000)
        report: dict[str, object] = {
            "input_count": len(symbols),
            "target_size": target_size,
            "hard_eligible_count": 0,
            "rejected_count_by_reason": {},
            "selected_count": len(fallback_selected),
            "core_selected_count": 0,
            "exploration_selected_count": 0,
            "selected_by_board": {},
            "score_distribution": {},
            "selected": [
                {
                    "symbol": s,
                    "score": 0.0,
                    "components": {},
                    "reason_codes": ["degraded_fallback"],
                }
                for s in fallback_selected
            ],
            "trade_date": trade_date,
            "reference_date": reference_date.isoformat() if reference_date else "",
            "ruleset_id": ruleset_id,
            "selector_mode": "degraded_fallback",
            "fallback_source": "quota_sampler" if self._fallback_sampler else "none",
            "fallback_reason": fallback_reason,
            "fallback_topup_count": 0,
            "snapshot_fallback_unavailable_reason": snapshot_unavailable_reason,
            "fallback_sampler_error": fallback_sampler_error,
            "fallback_sampler_meta": fallback_meta,
            "input_symbol_hash": input_hash,
            "output_symbol_hash": output_hash,
            "board_quotas": board_quotas,
            "applied_weights": dict(self._weights),
            "batch_calls": 1,
            "elapsed_ms": elapsed_ms,
            "generated_at": datetime.now().isoformat(),
            **dict(batch_stats),
        }
        return {"selected": fallback_selected, "report": report}

    def _load_snapshot_fallback(
        self,
        *,
        symbols: list[str],
        target_size: int,
        ruleset_id: str,
        board_scope: list[str],
        reference_date: date | None,
    ) -> tuple[tuple[list[str], list[dict[str, object]], dict[str, object]] | None, str]:
        if self._snapshot_path is None:
            return None, "snapshot_not_configured"
        if not self._snapshot_path.exists():
            return None, "snapshot_not_found"
        try:
            raw = json.loads(self._snapshot_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return None, f"snapshot_read_error:{type(exc).__name__}"
        if not isinstance(raw, dict):
            return None, "snapshot_invalid_payload"
        selector_mode = str(raw.get("selector_mode", "")).strip()
        if selector_mode not in _SUCCESS_SELECTOR_MODES:
            return None, f"snapshot_not_successful:{selector_mode or 'unknown'}"
        snapshot_ruleset = str(raw.get("ruleset_id", "")).strip()
        if snapshot_ruleset and snapshot_ruleset != ruleset_id:
            return None, "snapshot_ruleset_mismatch"
        generated_at = _coerce_datetime(raw.get("generated_at"))
        if generated_at is None:
            return None, "snapshot_missing_generated_at"
        effective_reference = reference_date or datetime.now().date()
        # A snapshot may be generated the day after its market as-of date
        # (for example, Friday data persisted during Saturday validation).
        # Compare against the wall clock for genuine future corruption, while
        # using the market reference date only for expiry age calculation.
        if generated_at > datetime.now():
            return None, "snapshot_from_future"
        snapshot_age_days = max(0, (effective_reference - generated_at.date()).days)
        if snapshot_age_days > self._snapshot_max_age_days:
            return None, (f"snapshot_expired:{snapshot_age_days}>{self._snapshot_max_age_days}")

        requested_set = set(symbols)
        scope_set = {str(item).strip().upper() for item in board_scope if str(item).strip()}
        raw_selected = raw.get("selected", [])
        if not isinstance(raw_selected, list):
            return None, "snapshot_selected_invalid"
        filtered_payload: list[dict[str, object]] = []
        seen: set[str] = set()
        for item in raw_selected:
            if not isinstance(item, dict):
                continue
            symbol = _normalize_a_share_symbol(item.get("symbol"))
            if not symbol or symbol in seen or symbol not in requested_set:
                continue
            board = _board_from_a_share_symbol(symbol)
            exchange = _BOARD_EXCHANGE_MAP.get(board, "")
            if scope_set and (not exchange or exchange not in scope_set):
                continue
            payload_item = dict(item)
            payload_item["symbol"] = symbol
            reasons = payload_item.get("reason_codes", [])
            reason_codes = [str(reason) for reason in reasons] if isinstance(reasons, list) else []
            if "snapshot_fallback" not in reason_codes:
                reason_codes.append("snapshot_fallback")
            payload_item["reason_codes"] = reason_codes
            payload_item["score"] = _safe_float(payload_item.get("score"))
            filtered_payload.append(payload_item)
            seen.add(symbol)
        filtered_payload.sort(
            key=lambda item: (-_safe_float(item.get("score")), str(item.get("symbol", "")))
        )
        filtered_payload = filtered_payload[:target_size]
        if not filtered_payload:
            return None, "snapshot_no_current_symbols"
        selected = [str(item["symbol"]) for item in filtered_payload]
        meta: dict[str, object] = {
            "snapshot_path": str(self._snapshot_path),
            "snapshot_age_days": snapshot_age_days,
            "snapshot_selector_mode": selector_mode,
            "snapshot_generated_at": generated_at.isoformat(),
            "snapshot_original_selected_count": _safe_int(raw.get("selected_count")),
            "snapshot_filtered_selected_count": len(selected),
        }
        return (selected, filtered_payload, meta), ""

    def _build_report(
        self,
        *,
        selected: list[str],
        target_size: int,
        trade_date: str,
        ruleset_id: str,
        board_scope: list[str],
        input_symbols: list[str],
        input_hash: str,
        eligible_rows: pd.DataFrame,
        rejected_counts: dict[str, int],
        core_selected: list[str],
        exploration_selected: list[str],
        selector_mode: str,
        fallback_reason: str,
        started_at: datetime,
        batch_calls: int = 1,
        board_quotas: dict[str, dict[str, object]] | None = None,
        batch_stats: Mapping[str, object] | None = None,
        reference_date: date | None = None,
    ) -> dict[str, object]:
        core_set = set(core_selected)
        exploration_set = set(exploration_selected)
        # Build full component/reason lookup for selected.
        selected_rows = eligible_rows[eligible_rows["symbol"].isin(set(selected))].copy()
        selected_rows = selected_rows.set_index("symbol")
        selected_payload: list[dict[str, object]] = []
        for symbol in selected:
            if symbol not in selected_rows.index:
                continue
            row = selected_rows.loc[symbol]
            components = {
                "trend": _safe_float(row.get("trend_component")),
                "capital_flow": _safe_float(row.get("capital_flow_component")),
                "price_volume": _safe_float(row.get("price_volume_component")),
                "liquidity": _safe_float(row.get("liquidity_component")),
                "fundamental": _safe_float(row.get("fundamental_component")),
                "risk_penalty": _safe_float(row.get("risk_penalty")),
            }
            reason_codes = _reason_codes_for_row(row)
            if symbol in core_set:
                reason_codes.append("core_quality_selected")
            if symbol in exploration_set:
                reason_codes.append("exploration_deterministic")
            selected_payload.append(
                {
                    "symbol": symbol,
                    "score": _safe_float(row.get("quality_score")),
                    "components": components,
                    "reason_codes": reason_codes,
                }
            )

        selected_by_board: dict[str, int] = {}
        for symbol in selected:
            board = _board_from_a_share_symbol(symbol)
            selected_by_board[board] = selected_by_board.get(board, 0) + 1

        scores = eligible_rows["quality_score"].tolist()
        score_distribution = _score_distribution(scores)
        output_hash = _symbol_hash(selected)
        elapsed_ms = int((datetime.now() - started_at).total_seconds() * 1000)
        report: dict[str, object] = {
            "input_count": len(input_symbols),
            "target_size": target_size,
            "hard_eligible_count": len(eligible_rows),
            "rejected_count_by_reason": rejected_counts,
            "selected_count": len(selected),
            "core_selected_count": len(core_selected),
            "exploration_selected_count": len(exploration_selected),
            "selected_by_board": selected_by_board,
            "score_distribution": score_distribution,
            "selected": selected_payload,
            "trade_date": trade_date,
            "reference_date": reference_date.isoformat() if reference_date else "",
            "ruleset_id": ruleset_id,
            "selector_mode": selector_mode,
            "fallback_source": "",
            "fallback_reason": fallback_reason,
            "input_symbol_hash": input_hash,
            "output_symbol_hash": output_hash,
            "board_quotas": board_quotas or {},
            "applied_weights": dict(self._weights),
            "batch_calls": batch_calls,
            "elapsed_ms": elapsed_ms,
            "generated_at": datetime.now().isoformat(),
            **dict(batch_stats or {}),
        }
        return {"selected": selected, "report": report}


# ----------------------------------------------------------------------
# Metric computation (vectorized over the batch frame)
# ----------------------------------------------------------------------
def _compute_quality_metrics(
    frame: pd.DataFrame,
    *,
    weights: Mapping[str, float],
) -> pd.DataFrame:
    """Compute per-symbol quality metrics from the batch warehouse frame.

    Input is the long frame returned by ``fetch_universe_quality_metrics``
    (one row per symbol/date, last ``lookback_days`` rows per symbol). Output
    is one row per symbol with all components needed for hard filtering and
    ``quality_score``. All operations are vectorized pandas groupby/rolling —
    no per-symbol Python loops over the universe.
    """
    if frame.empty:
        return frame

    work = frame.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    for col in (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover",
        "float_market_cap",
        "roe",
        "debt_ratio",
        "financial_completeness",
        "holder_count",
        "northbound_net",
        "dragon_tiger_flag",
    ):
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")
    for col in (
        "suspended",
        "is_st",
        "is_delisting_risk",
        "financial_data_complete",
        "background_data_complete",
    ):
        if col in work.columns:
            work[col] = _vectorized_coerce_bool(work[col])

    work = work.sort_values(["symbol", "date"], na_position="first")
    grouped = work.groupby("symbol", sort=False)

    # Per-symbol rolling metrics (computed on full group then we take the last row).
    work["_ma20"] = grouped["close"].transform(lambda s: s.rolling(20, min_periods=1).mean())
    work["_ma60"] = grouped["close"].transform(lambda s: s.rolling(60, min_periods=1).mean())
    work["_ma120"] = grouped["close"].transform(lambda s: s.rolling(120, min_periods=1).mean())
    work["_ma240"] = grouped["close"].transform(lambda s: s.rolling(240, min_periods=1).mean())

    close_shifted = grouped["close"].shift(20)
    work["_ret20"] = work["close"] / close_shifted - 1.0
    work["_ret60"] = work["close"] / grouped["close"].shift(60) - 1.0
    work["_ret120"] = work["close"] / grouped["close"].shift(120) - 1.0

    work["_turnover20"] = grouped["turnover"].transform(
        lambda s: s.rolling(20, min_periods=1).mean()
    )
    work["_turnover60"] = grouped["turnover"].transform(
        lambda s: s.rolling(60, min_periods=1).mean()
    )
    work["_volume5"] = grouped["volume"].transform(lambda s: s.rolling(5, min_periods=1).mean())
    work["_volume20"] = grouped["volume"].transform(lambda s: s.rolling(20, min_periods=1).mean())

    pct_change = grouped["close"].pct_change()
    work["_volatility20"] = pct_change.groupby(work["symbol"]).transform(
        lambda s: s.rolling(20, min_periods=2).std(ddof=0)
    )
    work["_volatility20"] = work["_volatility20"].fillna(0.0)

    high = work["high"].fillna(work["close"])
    low = work["low"].fillna(work["close"])
    atr_ratio = (
        ((high - low) / work["close"].replace(0.0, np.nan))
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0.0)
    )
    work["_atr_ratio"] = atr_ratio
    work["_atr20"] = work.groupby("symbol")["_atr_ratio"].transform(
        lambda s: s.rolling(20, min_periods=1).mean()
    )
    work["_atr60"] = work.groupby("symbol")["_atr_ratio"].transform(
        lambda s: s.rolling(60, min_periods=1).mean()
    )

    work["_northbound20"] = work.groupby("symbol")["northbound_net"].transform(
        lambda s: s.rolling(20, min_periods=1).sum()
    )
    work["_northbound60"] = work.groupby("symbol")["northbound_net"].transform(
        lambda s: s.rolling(60, min_periods=1).sum()
    )
    work["_dragon_tiger20"] = work.groupby("symbol")["dragon_tiger_flag"].transform(
        lambda s: s.rolling(20, min_periods=1).mean()
    )

    holder = work.groupby("symbol")["holder_count"]
    work["_holder_chg60"] = work["holder_count"] / holder.shift(60) - 1.0
    work["_holder_chg60"] = work["_holder_chg60"].replace([np.inf, -np.inf], np.nan).fillna(0.0)

    # cummax is O(n) per group vs rolling(len).max() which is O(n^2).
    work["_recent_high"] = grouped["close"].cummax()

    # Never cross-fill between symbols and never synthesize market cap from turnover.
    fmc = work.groupby("symbol")["float_market_cap"].transform(
        lambda series: series.ffill().bfill()
    )
    work["_float_market_cap"] = fmc

    # Take the latest row per symbol.
    latest = work.groupby("symbol", sort=False).tail(1).copy()
    latest = latest.set_index("symbol")
    valid_close = pd.to_numeric(work["close"], errors="coerce")
    valid_close_mask = pd.Series(
        np.isfinite(valid_close.to_numpy(dtype=float)), index=work.index
    ) & (valid_close > 0.0)
    history_days = valid_close_mask.groupby(work["symbol"]).sum()
    latest["history_days"] = history_days
    # Drop the raw float_market_cap so the forward-filled _float_market_cap (renamed
    # below) is the sole column — avoids duplicate-column ambiguity in _score_quality.
    if "float_market_cap" in latest.columns and "_float_market_cap" in latest.columns:
        latest = latest.drop(columns=["float_market_cap"])

    latest = latest.rename(
        columns={
            "close": "latest_close",
            "date": "latest_data_date",
            "suspended": "suspended",
            "is_st": "is_st",
            "is_delisting_risk": "is_delisting_risk",
            "_ma20": "ma20",
            "_ma60": "ma60",
            "_ma120": "ma120",
            "_ma240": "ma240",
            "_ret20": "ret20",
            "_ret60": "ret60",
            "_ret120": "ret120",
            "_turnover20": "avg_turnover_20",
            "_turnover60": "avg_turnover_60",
            "_volume5": "volume_5d",
            "_volume20": "volume_20d",
            "_volatility20": "volatility_20d",
            "_atr20": "atr_20d",
            "_atr60": "atr_60d",
            "_northbound20": "northbound_net_20d",
            "_northbound60": "northbound_net_60d",
            "_dragon_tiger20": "dragon_tiger_freq_20d",
            "_holder_chg60": "holder_count_chg_60d",
            "_recent_high": "recent_high",
            "_float_market_cap": "float_market_cap",
        }
    )

    latest = _score_quality(latest, weights=weights)
    return latest.reset_index()


def _score_quality(
    df: pd.DataFrame,
    *,
    weights: Mapping[str, float],
) -> pd.DataFrame:
    """Compute quality components and ``quality_score`` for each symbol row."""
    close = df["latest_close"]
    ma20 = df["ma20"]
    ma60 = df["ma60"]
    ma120 = df["ma120"]
    ma240 = df["ma240"]
    recent_high = df["recent_high"]

    ma_alignment = (
        0.30 * (close >= ma20).astype(float)
        + 0.30 * (close >= ma60).astype(float)
        + 0.25 * (close >= ma120.fillna(close)).astype(float)
        + 0.15 * (close >= ma240.fillna(close)).astype(float)
    )
    momentum = (
        0.40 * (df["ret20"].fillna(0.0) / 0.18).clip(lower=0.0, upper=1.0)
        + 0.35 * (df["ret60"].fillna(0.0) / 0.35).clip(lower=0.0, upper=1.0)
        + 0.25 * (df["ret120"].fillna(0.0) / 0.60).clip(lower=0.0, upper=1.0)
    )
    breakout = (
        ((close / recent_high.replace(0.0, np.nan) - 0.82) / 0.18)
        .clip(lower=0.0, upper=1.0)
        .fillna(0.0)
    )
    trend_component = (0.45 * ma_alignment + 0.35 * momentum + 0.20 * breakout).clip(
        lower=0.0, upper=1.0
    )

    holder_component = ((0.05 - df["holder_count_chg_60d"].fillna(0.0)) / 0.10).clip(
        lower=0.0, upper=1.0
    )
    avg_turnover_20 = df["avg_turnover_20"]
    northbound_flow_ratio20 = df["northbound_net_20d"].fillna(0.0) / (
        (avg_turnover_20 * 20).replace(0.0, np.nan)
    ).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    northbound_component = ((northbound_flow_ratio20 + 0.02) / 0.04).clip(lower=0.0, upper=1.0)
    dragon_tiger_component = 0.30 + 0.70 * (df["dragon_tiger_freq_20d"].fillna(0.0) / 0.08).clip(
        lower=0.0, upper=1.0
    )
    capital_flow_component = (
        0.45 * holder_component + 0.35 * northbound_component + 0.20 * dragon_tiger_component
    ).clip(lower=0.0, upper=1.0)

    atr60 = df["atr_60d"].replace(0.0, np.nan)
    atr_compression = ((1.10 - df["atr_20d"] / atr60) / 0.40).clip(lower=0.0, upper=1.0).fillna(0.0)
    volume_expansion = df["volume_5d"] / df["volume_20d"].replace(0.0, np.nan)
    volume_expansion = volume_expansion.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    volume_expansion_component = ((volume_expansion - 0.90) / 0.90).clip(lower=0.0, upper=1.0)
    heat_ratio = avg_turnover_20 / df["avg_turnover_60"].replace(0.0, np.nan)
    heat_ratio = heat_ratio.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    heat_component = ((heat_ratio - 0.85) / 0.65).clip(lower=0.0, upper=1.0)
    price_volume_component = (
        0.40 * volume_expansion_component + 0.30 * atr_compression + 0.30 * heat_component
    ).clip(lower=0.0, upper=1.0)

    turnover_component = (np.log10(avg_turnover_20.clip(lower=1.0)) / 9.0).clip(
        lower=0.0, upper=1.0
    )
    market_cap_component = (np.log10(df["float_market_cap"].clip(lower=1.0)) / 11.0).clip(
        lower=0.0, upper=1.0
    )
    turnover_rate_20d = avg_turnover_20 / df["float_market_cap"].replace(0.0, np.nan)
    turnover_rate_20d = turnover_rate_20d.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    turnover_rate_component = ((turnover_rate_20d - 0.001) / 0.02).clip(lower=0.0, upper=1.0)
    financial_complete = df["financial_data_complete"].astype(bool)
    background_complete = df["background_data_complete"].astype(bool)
    quality_component = 0.50 * np.where(financial_complete, 1.0, 0.35) + 0.50 * np.where(
        background_complete, 1.0, 0.35
    )
    liquidity_component = (
        0.45 * turnover_component
        + 0.25 * market_cap_component
        + 0.15 * turnover_rate_component
        + 0.15 * quality_component
    ).clip(lower=0.0, upper=1.0)

    # Fundamental: ROE, debt_ratio, credibility. Missing/untrusted -> 0 (no positive credit).
    roe = df.get("roe", pd.Series(dtype=float))
    if roe is not None:
        roe = pd.to_numeric(roe, errors="coerce")
    else:
        roe = pd.Series(np.nan, index=df.index)
    roe_score = (roe / 0.15).clip(lower=0.0, upper=1.0).fillna(0.0)
    debt_ratio = pd.to_numeric(df.get("debt_ratio", pd.Series(dtype=float)), errors="coerce")
    debt_score = ((0.6 - debt_ratio) / 0.6).clip(lower=0.0, upper=1.0).fillna(0.0)
    completeness = pd.to_numeric(
        df.get("financial_completeness", pd.Series(dtype=float)), errors="coerce"
    )
    credibility = np.where(financial_complete, completeness.fillna(0.0), 0.0)
    credibility = pd.Series(credibility, index=df.index).clip(lower=0.0, upper=1.0)
    fundamental_component = (0.40 * roe_score + 0.35 * debt_score + 0.25 * credibility).clip(
        lower=0.0, upper=1.0
    )

    drawdown_from_high = (
        (1.0 - close / recent_high.replace(0.0, np.nan)).clip(lower=0.0).fillna(0.0)
    )
    volatility_penalty = ((df["volatility_20d"] - 0.05).clip(lower=0.0) / 0.10).clip(
        lower=0.0, upper=1.0
    )
    drawdown_penalty = ((drawdown_from_high - 0.08).clip(lower=0.0) / 0.22).clip(
        lower=0.0, upper=1.0
    )
    risk_penalty = (0.65 * volatility_penalty + 0.35 * drawdown_penalty).clip(lower=0.0, upper=1.0)

    df = df.copy()
    df["trend_component"] = trend_component
    df["capital_flow_component"] = capital_flow_component
    df["price_volume_component"] = price_volume_component
    df["liquidity_component"] = liquidity_component
    df["fundamental_component"] = fundamental_component
    df["risk_penalty"] = risk_penalty
    df["turnover_rate_20d"] = turnover_rate_20d

    raw = (
        weights["trend"] * df["trend_component"]
        + weights["capital_flow"] * df["capital_flow_component"]
        + weights["price_volume"] * df["price_volume_component"]
        + weights["liquidity"] * df["liquidity_component"]
        + weights["fundamental"] * df["fundamental_component"]
        - weights["risk_penalty"] * df["risk_penalty"]
    )
    df["quality_score"] = (100.0 * raw.clip(lower=0.0, upper=1.0)).round(4)
    return df


def _reason_codes_for_row(row: pd.Series) -> list[str]:
    reasons: list[str] = []
    if _safe_float(row.get("latest_close")) >= _safe_float(row.get("ma60")):
        reasons.append("trend_above_ma60")
    if _safe_float(row.get("ret60")) > 0.08:
        reasons.append("ret60_positive")
    if _safe_float(row.get("capital_flow_component")) >= 0.55:
        reasons.append("capital_flow_support")
    if _safe_float(row.get("price_volume_component")) >= 0.55:
        reasons.append("price_volume_support")
    if _safe_float(row.get("liquidity_component")) >= 0.55:
        reasons.append("liquidity_ok")
    if _safe_float(row.get("fundamental_component")) >= 0.55:
        reasons.append("fundamental_strong")
    if _safe_float(row.get("risk_penalty")) >= 0.45:
        reasons.append("risk_penalty_high")
    if not bool(row.get("financial_data_complete", True)):
        reasons.append("financial_data_partial")
    if not bool(row.get("background_data_complete", True)):
        reasons.append("background_data_partial")
    return reasons


# ----------------------------------------------------------------------
# Small standalone helpers (no service import to avoid cycles)
# ----------------------------------------------------------------------
def _normalize_weights(weights: Mapping[str, float] | None) -> dict[str, float]:
    merged = dict(_DEFAULT_WEIGHTS)
    if weights is not None:
        unknown = sorted(set(weights) - set(_DEFAULT_WEIGHTS))
        if unknown:
            raise ValueError(f"unknown universe quality weights: {','.join(unknown)}")
        for key, value in weights.items():
            numeric = float(value)
            if not math.isfinite(numeric) or numeric < 0.0:
                raise ValueError(f"invalid universe quality weight: {key}")
            merged[key] = numeric
    positive_total = sum(merged[key] for key in _POSITIVE_WEIGHT_KEYS)
    if not math.isfinite(positive_total) or positive_total <= 0.0:
        raise ValueError("positive universe quality weights must sum above zero")
    normalized = {key: merged[key] / positive_total for key in _POSITIVE_WEIGHT_KEYS}
    risk_weight = merged["risk_penalty"]
    if not math.isfinite(risk_weight) or risk_weight < 0.0:
        raise ValueError("invalid universe quality risk_penalty weight")
    normalized["risk_penalty"] = risk_weight
    return normalized


def _coerce_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return pd.Timestamp(text).date()
    except (TypeError, ValueError):
        return None


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def _normalize_a_share_symbol(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) != 6:
        return ""
    if digits.startswith(("4", "8")) and not digits.startswith(("87", "88")):
        # BSE codes are 4xx/8xx; keep them. (43xxxx / 83xxxx / 87xxxx / 88xxxx)
        pass
    return digits


def _is_valid_a_share_symbol(value: object) -> bool:
    return bool(_normalize_a_share_symbol(value))


def _board_from_a_share_symbol(symbol: object) -> str:
    digits = _normalize_a_share_symbol(symbol)
    if not digits:
        return ""
    if digits.startswith("3"):
        return "SZ_GEM"
    if digits.startswith(("0", "1", "2")):
        return "SZ_MAIN"
    if digits.startswith(("688", "689")):
        return "SH_STAR"
    if digits.startswith(("6", "9")):
        return "SH_MAIN"
    if digits.startswith(("4", "8")):
        return "BSE"
    return "OTHER"


def _coerce_bool(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return False
        return bool(value)
    text = str(value).strip().lower()
    return text in {"true", "1", "yes", "t", "y"}


_TRUE_STRINGS = {"true", "1", "yes", "t", "y"}


def _vectorized_coerce_bool(series: pd.Series) -> pd.Series:
    """Vectorized boolean coercion for a full column — avoids per-row Python calls."""
    if series.dtype == bool:
        return series.fillna(False)
    numeric = pd.to_numeric(series, errors="coerce")
    # Numeric values: non-zero -> True, NaN -> False.
    numeric_result = numeric.notna() & (numeric != 0)
    # String values that aren't numeric: check against true-strings.
    text_result = series.astype(str).str.strip().str.lower().isin(_TRUE_STRINGS)
    # Use numeric result where available, else text result.
    return numeric_result.where(numeric.notna(), text_result)


def _is_valid_price(value: object) -> bool:
    if value is None:
        return False
    try:
        num = float(value)
    except (TypeError, ValueError):
        return False
    if math.isnan(num) or math.isinf(num):
        return False
    return num > 0.0


def _safe_float(value: object) -> float:
    if value is None:
        return 0.0
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(num) or math.isinf(num):
        return 0.0
    return num


def _safe_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _symbol_hash(symbols: list[str]) -> str:
    payload = "\n".join(sorted(symbols)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _score_distribution(scores: list[float]) -> dict[str, float]:
    if not scores:
        return {
            "min": 0.0,
            "p25": 0.0,
            "p50": 0.0,
            "p75": 0.0,
            "max": 0.0,
            "mean": 0.0,
            "count": 0,
        }
    arr = np.asarray(scores, dtype=float)
    return {
        "min": round(float(np.min(arr)), 4),
        "p25": round(float(np.percentile(arr, 25)), 4),
        "p50": round(float(np.percentile(arr, 50)), 4),
        "p75": round(float(np.percentile(arr, 75)), 4),
        "max": round(float(np.max(arr)), 4),
        "mean": round(float(np.mean(arr)), 4),
        "count": int(len(arr)),
    }


def persist_selection_snapshot(report: Mapping[str, object], snapshot_path: str | Path) -> Path:
    """Atomically persist only a successful quality-selection report."""

    path = Path(snapshot_path).expanduser()
    payload = {str(k): v for k, v in report.items()}
    selector_mode = str(payload.get("selector_mode", "")).strip()
    if selector_mode not in _SUCCESS_SELECTOR_MODES:
        raise ValueError(f"refusing to persist unsuccessful selector mode: {selector_mode}")
    if "target_size" in payload:
        target_size = _safe_int(payload.get("target_size"))
        selected_count = _safe_int(payload.get("selected_count"))
        if target_size > 0 and selected_count < target_size:
            raise ValueError(
                f"refusing to persist incomplete quality selection: {selected_count}<{target_size}"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload.setdefault("generated_at", datetime.now().isoformat())
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        temp_path.write_text(text, encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
    return path
