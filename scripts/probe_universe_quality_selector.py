"""Read-only NAS probe for the Week5 universe quality selector batch source.

Runs the exact production wiring (runtime provider graph -> batch source ->
UniverseCandidateSelector) against the local vendor ZIP overlay + delta
DuckDB, then prints a compact JSON audit payload for NAS acceptance.

The payload "ok" field and the exit code reflect the acceptance gate:
vendor-overlay batch-source identity, quality selector mode, no fallback,
batch coverage >= min coverage, input universe >= min input count, selector
elapsed time within the SLA, selected count >= target size and no NaN/Inf
scores. Any failed check is reported in "acceptance_failures" and the process
exits non-zero, so a NO-GO is never mistaken for PASS.

Read-only guarantees:
- does NOT start the scheduler;
- does NOT modify the DuckDB, ZIP archives, index or named volumes;
- the delta DuckDB is opened in ``read_only`` mode by default, so no database
  file is created and no table is created or written even on a cold cache;
  ``--allow-cache-write`` is an explicit opt-in that reverts to the normal
  read-write delta cache;
- does NOT run training or auto-promotion;
- does NOT modify runtime state;
- does NOT call any write API or persist the selection snapshot.

Usage:
    python scripts/probe_universe_quality_selector.py [--config config/default.yaml]
        [--target-size 300] [--min-coverage 0.90] [--min-input-count 5000]
        [--max-elapsed-ms 30000] [--allow-cache-write]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat()


def _enforce_probe_read_only(provider: object) -> None:
    """Recursively flip vendor ZIP overlay delta access to ``read_only``.

    Probes must never create or mutate the delta DuckDB. ``read_only`` mode
    opens the database read-only (a missing database file is left untouched
    and simply yields no delta rows), matching the probe's documented
    read-only guarantees.
    """
    pending = [provider]
    seen: set[int] = set()
    wrapper_attrs = (
        "primary",
        "backup",
        "inner",
        "base_provider",
        "provider",
        "_primary",
        "_backup",
        "_inner",
        "_base_provider",
        "_provider",
    )
    while pending:
        current = pending.pop(0)
        current_id = id(current)
        if current_id in seen:
            continue
        seen.add(current_id)
        if (
            type(current).__module__.endswith("vendor_zip_overlay")
            and type(current).__name__ == "VendorZipOverlayProvider"
        ):
            cast(Any, current).enforce_read_only_delta()
        for attr in wrapper_attrs:
            nested = getattr(current, attr, None)
            if nested is not None:
                pending.append(nested)


def _resolve_universe_symbols(service: object) -> list[str]:
    """Collect the provider-graph universe (vendor ZIP index for overlay mode)."""
    candidates: list[str] = []
    provider_graph = service._iter_market_data_provider_graph()
    for provider in provider_graph:
        list_symbols = getattr(provider, "list_symbols", None)
        if not callable(list_symbols):
            continue
        try:
            raw_symbols = list_symbols()
        except Exception:
            continue
        if isinstance(raw_symbols, (str, bytes)):
            continue
        try:
            candidates.extend(str(item) for item in raw_symbols)
        except TypeError:
            continue
    from stock_analyzer.runtime.service import _filter_supported_universe_symbols

    return _filter_supported_universe_symbols(candidates)


_EXPECTED_BATCH_SOURCE_MODULE = "stock_analyzer.data.vendor_zip_overlay"
_EXPECTED_PROVIDER_CLASS = f"{_EXPECTED_BATCH_SOURCE_MODULE}.VendorZipOverlayProvider"
_EXPECTED_PRIMARY = "vendor_zip_overlay"
_VENDOR_OVERLAY_ALIASES = {"vendor_zip_overlay", "vendor_overlay", "local_vendor_zip"}


def _normalize_primary(primary: str) -> str:
    """Normalize the configured data source primary into the canonical key."""
    normalized = str(primary or "").strip().lower()
    if normalized in _VENDOR_OVERLAY_ALIASES:
        return "vendor_zip_overlay"
    return normalized


def _score_has_nan_inf(report: dict[str, object]) -> bool:
    selected = report.get("selected", [])
    if not isinstance(selected, list):
        return False
    for item in selected:
        if not isinstance(item, dict):
            continue
        for key in ("score",):
            value = item.get(key)
            if isinstance(value, (int, float)) and (
                math.isnan(float(value)) or math.isinf(float(value))
            ):
                return True
        components = item.get("components")
        if isinstance(components, dict):
            for value in components.values():
                if isinstance(value, (int, float)) and (
                    math.isnan(float(value)) or math.isinf(float(value))
                ):
                    return True
    return False


def _acceptance_failures(
    payload: dict[str, object],
    *,
    min_coverage: float,
    target_size: int,
    min_input_count: int = 5000,
    max_elapsed_ms: int = 30_000,
    expected_primary: str = _EXPECTED_PRIMARY,
) -> list[str]:
    """Return the acceptance checks that failed, so NO-GO never reports ok.

    Includes the data-source identity gate: the probe must prove the selector
    used the VendorZipOverlayProvider batch source, not the legacy warehouse.
    ``min_input_count`` guards that the input universe is genuinely
    full-market (0 disables the explicit check, keeping the legacy empty-input
    check). ``max_elapsed_ms`` guards the selector SLA; it defaults to a
    non-zero SLA so the gate is never fail-open, and 0 explicitly disables it.
    """
    failures: list[str] = []
    configured_primary = str(payload.get("configured_primary", ""))
    if configured_primary != expected_primary:
        failures.append(
            f"configured_primary={configured_primary or 'none'} (expected {expected_primary})"
        )
    provider_class = str(payload.get("provider_class", ""))
    if provider_class != _EXPECTED_PROVIDER_CLASS:
        failures.append(
            f"provider_class={provider_class or 'none'} (expected {_EXPECTED_PROVIDER_CLASS})"
        )
    batch_source_module = str(payload.get("batch_source_module", ""))
    if batch_source_module != _EXPECTED_BATCH_SOURCE_MODULE:
        failures.append(
            f"batch_source_module={batch_source_module or 'none'} "
            f"(expected {_EXPECTED_BATCH_SOURCE_MODULE})"
        )
    selector_mode = str(payload.get("selector_mode", ""))
    if selector_mode not in {"quality", "quality_all_eligible"}:
        failures.append(
            f"selector_mode={selector_mode or 'none'} (expected quality/quality_all_eligible)"
        )
    if str(payload.get("fallback_reason", "")):
        failures.append(f"fallback_reason={payload.get('fallback_reason')}")
    input_count = _as_int(payload.get("input_count"), default=0)
    if min_input_count > 0:
        if input_count < min_input_count:
            failures.append(f"input_count={input_count} < min_input_count={min_input_count}")
    elif input_count <= 0:
        failures.append("input_count<=0")
    coverage = _as_float(payload.get("batch_coverage_ratio"), default=0.0)
    if coverage < min_coverage:
        failures.append(f"batch_coverage_ratio={coverage:.4f} < min {min_coverage:.4f}")
    if _as_int(payload.get("batch_calls"), default=0) < 1:
        failures.append("batch_calls<1")
    selected_count = _as_int(payload.get("selected_count"), default=0)
    if selected_count < target_size:
        failures.append(f"selected_count={selected_count} < target_size={target_size}")
    if bool(payload.get("score_has_nan_inf")):
        failures.append("score_has_nan_inf=true")
    if max_elapsed_ms > 0:
        elapsed_ms = _as_int(payload.get("elapsed_ms"), default=0)
        if elapsed_ms > max_elapsed_ms:
            failures.append(f"elapsed_ms={elapsed_ms} > max_elapsed_ms={max_elapsed_ms}")
    return failures


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(ROOT / "config" / "default.yaml"),
        help="Path to the YAML config (default: config/default.yaml)",
    )
    parser.add_argument(
        "--target-size",
        type=int,
        default=0,
        help="Override universe_quality_target_size (0 = use config default)",
    )
    parser.add_argument(
        "--min-coverage",
        type=float,
        default=0.0,
        help="Minimum acceptable batch_coverage_ratio (0 = use config default)",
    )
    parser.add_argument(
        "--min-input-count",
        type=int,
        default=0,
        help="Minimum accepted input universe size (0 = default 5000)",
    )
    parser.add_argument(
        "--max-elapsed-ms",
        type=int,
        default=30_000,
        help="Selector SLA in ms (default 30000); 0 explicitly disables the check",
    )
    parser.add_argument(
        "--allow-cache-write",
        action="store_true",
        help="Opt-in: open the delta DuckDB read-write and allow cache writes "
        "(default is read_only so the probe never creates or mutates the database)",
    )
    args = parser.parse_args(argv)

    from stock_analyzer.config import load_config
    from stock_analyzer.runtime.service import StockAnalyzerService

    config = load_config(args.config)
    service = object.__new__(StockAnalyzerService)
    object.__setattr__(service, "_config", config)

    provider = None
    try:
        from stock_analyzer.data.provider_factory import build_runtime_provider

        runtime_data_source = service._resolve_runtime_data_source_config(config)
        provider = build_runtime_provider(
            runtime_data_source,
            synthetic_seed=2026,
        )
        if not args.allow_cache_write:
            _enforce_probe_read_only(provider)
        if bool(config.cache.enabled):
            from stock_analyzer.data.cached_provider import CachedProvider
            from stock_analyzer.infra.cache import InMemoryCache

            provider = CachedProvider(
                inner=provider,
                cache=InMemoryCache(),
                ttl_sec=max(1, int(config.cache.ttl_sec)),
                key_prefix="runtime_offline",
            )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "probe": "universe_quality_selector",
                    "ok": False,
                    "generated_at": _now_iso(),
                    "provider_class": "",
                    "error": f"provider_build_error:{type(exc).__name__}:{exc}",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    object.__setattr__(service, "_provider", provider)
    object.__setattr__(service, "_realtime_provider", None)

    try:
        from stock_analyzer.data.market_warehouse import MarketWarehouse

        fallback_warehouse = MarketWarehouse(
            db_path=runtime_data_source.warehouse_db_path,
            package_root=runtime_data_source.local_data_root,
            read_only=not args.allow_cache_write,
        )

        class _WarehouseHost:
            def _market_warehouse(self) -> MarketWarehouse:
                return fallback_warehouse

        object.__setattr__(service, "_market_sync_service", _WarehouseHost())
    except Exception as exc:
        print(
            json.dumps(
                {
                    "probe": "universe_quality_selector",
                    "ok": False,
                    "generated_at": _now_iso(),
                    "provider_class": type(provider).__name__,
                    "error": f"warehouse_host_error:{type(exc).__name__}:{exc}",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    try:
        symbols = _resolve_universe_symbols(service)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "probe": "universe_quality_selector",
                    "ok": False,
                    "generated_at": _now_iso(),
                    "provider_class": type(provider).__name__,
                    "error": f"universe_resolve_error:{type(exc).__name__}:{exc}",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    try:
        batch_source = service._resolve_universe_quality_batch_source()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "probe": "universe_quality_selector",
                    "ok": False,
                    "generated_at": _now_iso(),
                    "provider_class": type(provider).__name__,
                    "input_count": len(symbols),
                    "error": f"batch_source_resolve_error:{type(exc).__name__}:{exc}",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    from stock_analyzer.runtime import service as service_module
    from stock_analyzer.runtime.universe_candidate_selector import UniverseCandidateSelector

    cfg = config.week5
    target_size = (
        args.target_size
        if args.target_size > 0
        else _as_int(cfg.universe_quality_target_size, default=300)
    )
    selector = UniverseCandidateSelector(
        warehouse=batch_source,
        weights=cfg.universe_quality_weights,
        min_history_days=_as_int(cfg.universe_quality_min_history_days, default=60),
        min_avg_turnover_20=_as_float(cfg.universe_quality_min_avg_turnover_20, default=0.0),
        min_float_market_cap=_as_float(cfg.universe_quality_min_float_market_cap, default=0.0),
        min_batch_coverage_ratio=_as_float(
            cfg.universe_quality_min_batch_coverage_ratio, default=0.90
        ),
        max_staleness_days=_as_int(cfg.universe_quality_max_staleness_days, default=10),
        require_financial_data=bool(cfg.universe_quality_require_financial_data),
        min_roe=_as_float(cfg.universe_quality_min_roe, default=0.0),
        max_debt_ratio=_as_float(cfg.universe_quality_max_debt_ratio, default=0.80),
        exploration_ratio=_as_float(cfg.universe_quality_exploration_ratio, default=0.05),
        lookback_days=max(60, _as_int(cfg.universe_prefilter_lookback_days, default=240)),
        snapshot_path=None,
        snapshot_max_age_days=_as_int(cfg.universe_quality_snapshot_max_age_days, default=7),
        fallback_sampler=service_module._quota_sample_universe,
    )

    try:
        trade_date = service._resolve_universe_seed_trade_date()
    except Exception as exc:
        trade_date = ""
        print(f"warning: seed trade date unavailable: {exc}", file=sys.stderr)

    probe_started_at = time.perf_counter()
    try:
        result = selector.select(
            symbols=symbols,
            target_size=target_size,
            trade_date=trade_date or date.today().isoformat(),
            reference_date=date.today().isoformat(),
            ruleset_id=str(config.evolution.universe_spec.universe_ruleset_id),
            board_scope=list(config.evolution.universe_spec.board_scope),
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "probe": "universe_quality_selector",
                    "ok": False,
                    "generated_at": _now_iso(),
                    "provider_class": type(batch_source).__name__,
                    "input_count": len(symbols),
                    "error": f"selector_error:{type(exc).__name__}:{exc}",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    elapsed_ms = int((time.perf_counter() - probe_started_at) * 1000)
    report = result.get("report", {})
    if not isinstance(report, dict):
        report = {}

    payload = {
        "probe": "universe_quality_selector",
        "ok": True,
        "generated_at": _now_iso(),
        "configured_primary": _normalize_primary(config.data_source.primary),
        "provider_class": f"{type(batch_source).__module__}.{type(batch_source).__name__}",
        "input_count": _as_int(report.get("input_count"), default=len(symbols)),
        "batch_calls": _as_int(report.get("batch_calls"), default=0),
        "batch_symbol_count": _as_int(report.get("batch_symbol_count"), default=0),
        "missing_batch_symbol_count": _as_int(report.get("missing_batch_symbol_count"), default=0),
        "batch_coverage_ratio": _as_float(report.get("batch_coverage_ratio"), default=0.0),
        "selector_mode": str(report.get("selector_mode", "")),
        "fallback_reason": str(report.get("fallback_reason", "")),
        "selected_count": _as_int(report.get("selected_count"), default=0),
        "hard_eligible_count": _as_int(report.get("hard_eligible_count"), default=0),
        "rejected_count_by_reason": (
            report.get("rejected_count_by_reason")
            if isinstance(report.get("rejected_count_by_reason"), dict)
            else {}
        ),
        "score_has_nan_inf": _score_has_nan_inf(report),
        "selected_symbols_hash": str(report.get("output_symbol_hash", "")),
        "elapsed_ms": elapsed_ms,
        "target_size": target_size,
        "trade_date": str(report.get("trade_date", trade_date)),
        "ruleset_id": str(report.get("ruleset_id", "")),
        "batch_source_module": type(batch_source).__module__,
        "delta_access_mode": ("read_write" if args.allow_cache_write else "read_only"),
    }
    min_coverage = (
        args.min_coverage
        if args.min_coverage > 0.0
        else _as_float(cfg.universe_quality_min_batch_coverage_ratio, default=0.90)
    )
    min_input_count = args.min_input_count if args.min_input_count > 0 else 5000
    acceptance_failures = _acceptance_failures(
        payload,
        min_coverage=min_coverage,
        target_size=target_size,
        min_input_count=min_input_count,
        max_elapsed_ms=max(0, int(args.max_elapsed_ms)),
    )
    payload["ok"] = not acceptance_failures
    if acceptance_failures:
        payload["acceptance_failures"] = acceptance_failures
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


if __name__ == "__main__":
    raise SystemExit(_main())
