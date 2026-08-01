"""Recoverable, rate-limited, idempotent full-market PIT financial backfill.

Writes ONLY the ``financial_snapshots`` table of the delta DuckDB through
``MarketWarehouse.upsert_financial_snapshots`` (PIT ann_date semantics from
Tushare fina_indicator via ``stock_analyzer.data.financial_pit``). It does
NOT touch ZIP archives, daily bars, index files or named volumes.

Provider wiring
---------------
- The full-market universe is taken from the runtime provider graph (the
  ``list_symbols`` of ``VendorZipOverlayProvider`` in a NAS overlay deploy).
- Financial data is fetched by a DEDICATED provider built from
  ``market_warehouse.online_daily_primary`` (e.g. ``tushare``) exactly like
  the market sync service does (token, request interval, price mode). The
  overlay runtime graph never exposes ``fetch_fina_indicator``, so relying
  on it would exit 2 on NAS.

Guarantees
----------
- Resumable: a JSON checkpoint records each symbol whose fetch AND upsert
  succeeded; ``--resume`` skips those, so an interrupted run continues where
  it stopped. Checkpoint keys include the schema version, end/start date and
  the financial provider name, so expanding the history range, changing the
  provider or a schema bump invalidates stale entries instead of silently
  skipping symbols. Empty responses are NOT recorded, so a later wider
  history re-fetches them.
- Rate-limited: sleeps ``market_warehouse.request_interval_sec`` (fallback
  ``data_source.request_interval_sec``) between API calls, INCLUDING after
  failures, so API throttling cannot turn into a fast failure cascade.
- Idempotent: ``upsert_financial_snapshots`` merges by
  (symbol, end_date, ann_date, financial_source), so re-runs never duplicate
  and API failures never wipe prior trusted snapshots.

Usage:
    python scripts/backfill_financial_snapshots.py --config config/default.yaml
        [--universe-file symbols.txt] [--limit 3000]
        [--end-date 2026-08-01] [--start-date 2021-01-01]
        [--checkpoint artifacts/financial_backfill_checkpoint.json]
        [--resume] [--dry-run]

Exit code: 0 only when every symbol succeeded (or dry-run). 1 when any
symbol failed or returned empty data (``--allow-empty`` opts out of the
empty-data failure). 2 when no financial provider or universe could be
resolved.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_CHECKPOINT_SCHEMA_VERSION = 1

_WRAPPER_ATTRS = (
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


def _iter_provider_graph(*roots: object) -> list[object]:
    pending = [root for root in roots if root is not None]
    seen: set[int] = set()
    providers: list[object] = []
    while pending:
        provider = pending.pop(0)
        provider_id = id(provider)
        if provider_id in seen:
            continue
        seen.add(provider_id)
        providers.append(provider)
        for attr in _WRAPPER_ATTRS:
            nested = getattr(provider, attr, None)
            if nested is not None:
                pending.append(nested)
    return providers


def _resolve_callable(*roots: object, method_name: str) -> object | None:
    for provider in _iter_provider_graph(*roots):
        candidate = getattr(provider, method_name, None)
        if callable(candidate):
            return candidate
    return None


def _as_date(value: object, *, default: date) -> date:
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default


def _normalize_a_share_symbol(value: object) -> str:
    from stock_analyzer.data.tushare_provider import _normalize_symbol

    return _normalize_symbol(str(value or ""))


def _load_checkpoint(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_checkpoint(path: Path, checkpoint: dict[str, str], marker: str) -> None:
    checkpoint[marker] = datetime.now().isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _marker_key(*, end_date: date, start_date: date, provider: str, symbol: str) -> str:
    provider_key = str(provider or "unknown").strip().lower().replace("|", "_")
    return (
        f"v{_CHECKPOINT_SCHEMA_VERSION}|{end_date.isoformat()}|"
        f"{start_date.isoformat()}|{provider_key}|{symbol}"
    )


def _build_financial_provider(config: object, service: object) -> object:
    """Build the DEDICATED financial (fina_indicator) provider.

    Mirrors ``MarketSyncService._build_market_warehouse_online_provider`` so
    the token, request interval, price mode and backup chain are exactly the
    production ones, regardless of the runtime overlay primary.
    """
    from stock_analyzer.runtime.services.market_sync_service import RuntimeMarketSyncService

    sync = RuntimeMarketSyncService(service)
    return sync._build_market_warehouse_online_provider()


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(ROOT / "config" / "default.yaml"),
        help="Path to the YAML config (default: config/default.yaml)",
    )
    parser.add_argument(
        "--universe-file",
        default="",
        help="Text file with one symbol per line; defaults to the provider universe",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap the number of symbols processed this run (0 = unlimited)",
    )
    parser.add_argument(
        "--end-date",
        default="",
        help="Latest report period end date (YYYY-MM-DD); defaults to today",
    )
    parser.add_argument(
        "--start-date",
        default="",
        help="Earliest report period start date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--checkpoint",
        default=str(ROOT / "artifacts" / "financial_backfill_checkpoint.json"),
        help="Checkpoint file path for resume",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip symbols already recorded in the checkpoint",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report what would be processed; never fetch or write",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 when any symbol returned empty data (default; kept for "
        "compatibility with NAS commands)",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Do NOT exit 1 when symbols return empty data (empty responses "
        "are still never checkpointed)",
    )
    parser.add_argument(
        "--request-interval-sec",
        type=float,
        default=0.0,
        help="Override rate-limit sleep between API calls (0 = use config)",
    )
    args = parser.parse_args(argv)

    from stock_analyzer.config import load_config
    from stock_analyzer.data.market_warehouse import MarketWarehouse
    from stock_analyzer.data.provider_factory import build_runtime_provider
    from stock_analyzer.runtime.service import StockAnalyzerService

    config = load_config(args.config)
    service = object.__new__(StockAnalyzerService)
    object.__setattr__(service, "_config", config)
    runtime_data_source = service._resolve_runtime_data_source_config(config)
    provider = build_runtime_provider(runtime_data_source, synthetic_seed=2026)
    if bool(config.cache.enabled):
        from stock_analyzer.data.cached_provider import CachedProvider
        from stock_analyzer.infra.cache import InMemoryCache

        provider = CachedProvider(
            inner=provider,
            cache=InMemoryCache(),
            ttl_sec=max(1, int(config.cache.ttl_sec)),
            key_prefix="runtime_offline",
        )
    warehouse = MarketWarehouse(
        db_path=runtime_data_source.warehouse_db_path,
        package_root=runtime_data_source.local_data_root,
    )

    financial_provider = _build_financial_provider(config, service)
    fetch_fn = _resolve_callable(financial_provider, method_name="fetch_fina_indicator")
    if not callable(fetch_fn):
        print(
            "no financial provider exposes fetch_fina_indicator; check "
            "market_warehouse.online_daily_primary (e.g. 'tushare') in the "
            "config",
            file=sys.stderr,
        )
        return 2

    if args.universe_file.strip():
        raw_path = Path(args.universe_file.strip()).expanduser()
        symbols = [
            line.strip()
            for line in raw_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        symbols = []
        for node in _iter_provider_graph(provider):
            list_symbols = getattr(node, "list_symbols", None)
            if not callable(list_symbols):
                continue
            try:
                raw_symbols = list_symbols()
            except Exception:
                continue
            if isinstance(raw_symbols, (str, bytes)):
                continue
            try:
                symbols.extend(str(item) for item in raw_symbols)
            except TypeError:
                continue
    symbols = sorted(
        {code for item in symbols if str(item) and (code := _normalize_a_share_symbol(item))}
    )
    if not symbols:
        print("empty universe: nothing to backfill", file=sys.stderr)
        return 2

    end_date = _as_date(args.end_date, default=date.today())
    start_date = _as_date(args.start_date, default=date(2019, 1, 1))
    financial_provider_name = (
        str(getattr(config.market_warehouse, "online_daily_primary", "unknown")).strip().lower()
    )
    checkpoint = _load_checkpoint(Path(args.checkpoint).expanduser())
    marker_prefix = (
        f"v{_CHECKPOINT_SCHEMA_VERSION}|{end_date.isoformat()}|"
        f"{start_date.isoformat()}|{financial_provider_name}|"
    )
    already_done = {key.rsplit("|", 1)[-1] for key in checkpoint if key.startswith(marker_prefix)}
    if args.resume:
        symbols = [symbol for symbol in symbols if symbol not in already_done]

    limit = max(0, int(args.limit))
    if limit:
        symbols = symbols[:limit]
    interval_sec = (
        args.request_interval_sec
        if args.request_interval_sec > 0.0
        else max(
            0.0,
            float(
                getattr(
                    config.market_warehouse,
                    "request_interval_sec",
                    config.data_source.request_interval_sec,
                )
            ),
        )
    )

    counts = {"ok": 0, "empty": 0, "failed": 0, "skipped": len(already_done) if args.resume else 0}
    failures: list[str] = []
    processed = 0
    for symbol in symbols:
        processed += 1
        marker = _marker_key(
            end_date=end_date,
            start_date=start_date,
            provider=financial_provider_name,
            symbol=symbol,
        )
        if args.dry_run:
            print(f"dry-run: would backfill {symbol} ({processed}/{len(symbols)})")
            counts["ok"] += 1
        else:
            try:
                incoming = fetch_fn(symbol=symbol, end_date=end_date, start_date=start_date)
            except Exception as exc:
                counts["failed"] += 1
                failures.append(f"{symbol}:{type(exc).__name__}:{exc}")
            else:
                if incoming is None or incoming.empty:
                    counts["empty"] += 1
                else:
                    try:
                        warehouse.upsert_financial_snapshots(symbol=symbol, frame=incoming)
                    except Exception as exc:
                        counts["failed"] += 1
                        failures.append(f"{symbol}:{type(exc).__name__}:{exc}")
                    else:
                        counts["ok"] += 1
                        _save_checkpoint(Path(args.checkpoint).expanduser(), checkpoint, marker)
        if interval_sec > 0.0 and processed < len(symbols):
            time.sleep(interval_sec)

    summary = {
        "tool": "backfill_financial_snapshots",
        "checkpoint_schema_version": _CHECKPOINT_SCHEMA_VERSION,
        "financial_provider": financial_provider_name,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "symbols_total": len(symbols) + (counts["skipped"] if args.resume else 0),
        "symbols_processed": len(symbols),
        "symbols_skipped_resume": counts["skipped"],
        "ok": counts["ok"],
        "empty": counts["empty"],
        "failed": counts["failed"],
        "failures": failures[:100],
        "end_date": end_date.isoformat(),
        "start_date": start_date.isoformat(),
        "dry_run": bool(args.dry_run),
        "checkpoint": str(Path(args.checkpoint).expanduser()),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if counts["failed"]:
        return 1
    if counts["empty"] and (args.strict or not args.allow_empty):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
