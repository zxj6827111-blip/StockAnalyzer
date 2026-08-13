"""Incremental full-market feature snapshot layer.

Builds a per-symbol "latest feature row" table (208 engineered columns plus a
small set of raw snapshot columns used by the light-stage candidate scoring)
into a parquet file referenced by ``current.json``.  The scan path loads the
snapshot once instead of re-reading vendor archives and re-running feature
engineering per symbol, so deep work only happens for the funnel tail.

Layout (never inside the vendor directory):

    <root>/current.json
    <root>/<data_snapshot_id>/market_features.parquet

Invalidation: ``source_signature`` mixes the provider's latest trade date,
the feature-schema hash and a data-root fingerprint.  A mismatch between the
manifest signature and the current signature means the snapshot is stale and
must be rebuilt (the build skips straight to a rebuild when they differ).
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter, sleep
from typing import Any

import numpy as np
import pandas as pd

from stock_analyzer.config import StockAnalyzerConfig
from stock_analyzer.feature.engineer import FeatureEngineer

FORMAT_VERSION = 1
SNAPSHOT_FILENAME = "market_features.parquet"

# Raw columns stored alongside the engineered features so the light-stage
# scoring is byte-identical to the direct bars path.
RAW_SNAPSHOT_COLUMNS = (
    "latest_close",
    "ma20",
    "ma60",
    "ma120",
    "ma240",
    "ret20",
    "ret60",
    "ret120",
    "recent_high",
    "avg_turnover_20",
    "avg_turnover_60",
    "volume_5d",
    "volume_20d",
    "atr_20d",
    "atr_60d",
    "volatility_20d",
    "float_market_cap",
    "turnover_rate_20d",
    "holder_count_chg_60d",
    "northbound_net_20d",
    "dragon_tiger_freq_20d",
    "bg_holder_present",
    "bg_block_trade_present",
    "bg_financing_present",
    "bg_northbound_present",
    "bg_dragon_tiger_present",
    "bg_roe_present",
    "bg_debt_ratio_present",
    "suspended",
    "is_st",
    "is_delisting_risk",
    "financial_data_complete",
    "background_data_complete",
)


@dataclass(slots=True)
class FeatureSnapshotManifest:
    data_snapshot_id: str
    trade_date: str
    built_at: str
    feature_schema_hash: str
    symbol_count: int
    columns: list[str] = field(default_factory=list)
    source_signature: str = ""
    format_version: int = FORMAT_VERSION
    source_provider: str = ""
    # Per-symbol incremental bookkeeping: {symbol: {"latest_date", "fingerprint"}}.
    per_symbol: dict[str, dict[str, str]] = field(default_factory=dict)
    # Fingerprint of the vendor factor archives (qfq/hfq ZIPs); empty when no
    # factor source is configured (comparison is skipped in that case).
    factor_archive_hash: str = ""
    # Freshness/integrity bookkeeping of the last build/refresh.
    dirty_count: int = 0
    refreshed_count: int = 0
    failed_symbols: int = 0
    coverage_ratio: float = 1.0
    max_trade_date: str = ""
    # Symbols that failed the last refresh; they are retried on the next
    # incremental build until they succeed (or are dropped from the universe).
    failed_symbols_list: list[str] = field(default_factory=list)
    # Candidate-set scope bookkeeping: which caller-selected universe this
    # snapshot serves (e.g. "universe_quality"), a hash of the requested
    # symbol set, and the requested vs published symbol counts.  A scope or
    # universe-hash mismatch forces at least an incremental refresh so
    # symbols that left the candidate set are never served stale.
    scope: str = ""
    universe_hash: str = ""
    requested_symbol_count: int = 0
    published_symbol_count: int = 0

    @classmethod
    def from_payload(cls, payload: object) -> FeatureSnapshotManifest | None:
        if not isinstance(payload, dict):
            return None
        try:
            raw_per_symbol = payload.get("per_symbol") or {}
            per_symbol: dict[str, dict[str, str]] = {}
            if isinstance(raw_per_symbol, dict):
                for symbol, entry in raw_per_symbol.items():
                    if not isinstance(entry, dict):
                        continue
                    per_symbol[str(symbol)] = {
                        "latest_date": str(entry.get("latest_date", "")),
                        "fingerprint": str(entry.get("fingerprint", "")),
                    }
            return cls(
                data_snapshot_id=str(payload["data_snapshot_id"]),
                trade_date=str(payload["trade_date"]),
                built_at=str(payload["built_at"]),
                feature_schema_hash=str(payload.get("feature_schema_hash", "")),
                symbol_count=int(payload.get("symbol_count", 0)),
                columns=list(payload.get("columns", []) or []),
                source_signature=str(payload.get("source_signature", "")),
                format_version=int(payload.get("format_version", FORMAT_VERSION)),
                source_provider=str(payload.get("source_provider", "")),
                per_symbol=per_symbol,
                factor_archive_hash=str(payload.get("factor_archive_hash", "")),
                dirty_count=int(payload.get("dirty_count", 0)),
                refreshed_count=int(payload.get("refreshed_count", 0)),
                failed_symbols=int(payload.get("failed_symbols", 0)),
                coverage_ratio=float(payload.get("coverage_ratio", 1.0)),
                max_trade_date=str(payload.get("max_trade_date", "")),
                failed_symbols_list=[
                    str(item) for item in (payload.get("failed_symbols_list") or [])
                ],
                scope=str(payload.get("scope", "")),
                universe_hash=str(payload.get("universe_hash", "")),
                requested_symbol_count=int(payload.get("requested_symbol_count", 0)),
                published_symbol_count=int(payload.get("published_symbol_count", 0)),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def to_payload(self) -> dict[str, object]:
        return asdict(self)


def _data_root_layout_fingerprint(config: StockAnalyzerConfig) -> str:
    """Structural fingerprint of the data roots that feed the snapshot.

    Intentionally ignores file mtimes so routine nightly content updates do
    not invalidate the snapshot; only layout changes (missing directories,
    renamed/new year archives) count as a structural anomaly.
    """
    parts: list[str] = []
    data_root = str(config.data_source.local_data_root).strip()
    if data_root:
        root = Path(data_root)
        if root.exists():
            layout: list[str] = []
            for child in sorted(root.iterdir()):
                try:
                    stat = child.stat()
                except OSError:
                    continue
                suffix = "/" if child.is_dir() else ""
                layout.append(f"{child.name}{suffix}:{stat.st_size if not child.is_dir() else ''}")
            parts.append("|".join(layout[-256:]))
    index_path = str(config.data_source.vendor_zip_index_path).strip()
    if index_path:
        index = Path(index_path).expanduser()
        parts.append(f"index:{index.exists()}:{index.stat().st_size if index.exists() else 0}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def compute_source_signature(
    config: StockAnalyzerConfig,
    provider_status: dict[str, object] | None = None,
) -> str:
    """Structural signature of everything the snapshot depends on.

    Deliberately excludes the data's latest trade date: a routine trading-day
    update must NOT invalidate the snapshot (incremental rebuild handles it).
    """
    status = provider_status or {}
    parts = [
        str(status.get("provider_key", status.get("provider_mode", ""))),
        str(config.evolution.code_commit_id or ""),
        _data_root_layout_fingerprint(config),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def resolve_snapshot_root(config: StockAnalyzerConfig) -> Path:
    root = str(config.week5.feature_snapshot_root).strip()
    if not root:
        root = "artifacts/features_light"
    return Path(root).expanduser()


def load_feature_snapshot(
    config: StockAnalyzerConfig,
) -> tuple[FeatureSnapshotManifest | None, pd.DataFrame | None]:
    """Load the current snapshot; returns (None, None) when missing/corrupt."""
    root = resolve_snapshot_root(config)
    manifest_path = root / "current.json"
    if not manifest_path.exists():
        return None, None
    try:
        manifest = FeatureSnapshotManifest.from_payload(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError):
        return None, None
    if manifest is None:
        return None, None
    frame_path = root / manifest.data_snapshot_id / SNAPSHOT_FILENAME
    if not frame_path.exists():
        return None, None
    try:
        frame = pd.read_parquet(frame_path)
    except Exception:
        return None, None
    return manifest, frame


def snapshot_is_current(
    manifest: FeatureSnapshotManifest,
    config: StockAnalyzerConfig,
    now: datetime | None = None,
) -> bool:
    """Age/signature checks; the caller decides whether a stale snapshot blocks."""
    if manifest.format_version != FORMAT_VERSION:
        return False
    built_at = _parse_utc(manifest.built_at)
    if built_at is None:
        return False
    current = now or datetime.now(UTC)
    if not isinstance(current, datetime):
        return False
    if current.tzinfo is None:
        # Naive callers (e.g. tests passing a wall-clock timestamp) are
        # interpreted as UTC so the age comparison is always tz-aware.
        current = current.replace(tzinfo=UTC)
    max_age = max(0, int(config.week5.feature_snapshot_max_age_days))
    if current - built_at > timedelta(days=max_age):
        return False
    expected_signature = compute_source_signature(config)
    if manifest.source_signature and manifest.source_signature != expected_signature:
        return False
    # Schema drift: recompute the current feature-schema hash and compare.
    # A change in the engineered column set or feature logic must invalidate
    # the snapshot so stale features are never reused.
    try:
        current_schema_hash = _feature_schema_hash(FeatureEngineer())
    except Exception:
        return False
    if manifest.feature_schema_hash and manifest.feature_schema_hash != current_schema_hash:
        return False
    # Factor-version drift: vendor factor archives changed -> full rebuild.
    current_factor_hash = _factor_archive_hash(config)
    if (
        current_factor_hash
        and manifest.factor_archive_hash
        and current_factor_hash != manifest.factor_archive_hash
    ):
        return False
    # Completeness: any symbol that failed the last refresh must NOT be
    # published as fully current (stale rows would be served silently and the
    # failures would be hidden).  Strict fail-close: zero tolerance.
    if manifest.failed_symbols > 0:
        return False
    return True


def build_feature_snapshot(
    config: StockAnalyzerConfig,
    provider: object,
    *,
    symbols: list[str],
    lookback_days: int | None = None,
    feature_engineer: FeatureEngineer | None = None,
    max_workers: int = 4,
    force: bool = False,
    on_progress: object | None = None,
    scope: str = "",
    batch_size: int = 20,
    progress_path: str | None = None,
) -> dict[str, object]:
    """Build (or incrementally refresh) the full-market feature snapshot.

    Incremental semantics: when a structurally-compatible snapshot exists
    (same schema hash, factor archives, provider and data-root layout), only
    dirty symbols -- those missing from the manifest or with a newer provider
    trade date -- are re-fetched and re-engineered, then merged into the old
    parquet (replacing the touched rows).  Symbols that left the requested
    candidate set are dropped from the published frame.  A full rebuild
    happens on first build, on schema/factor/signature drift, or with
    ``force``.

    ``scope`` labels the caller-selected candidate set (e.g. "universe_quality")
    and is recorded in the manifest alongside the requested set hash; a scope
    or set mismatch prevents an unconditional skip.  Transform work is
    submitted to the process pool in chunks of ``batch_size`` so the IPC queue
    never holds every symbol's bars at once, and a ``progress_path`` (when
    given) receives atomic JSON progress marks per phase.

    Skips when a current snapshot already exists for the same candidate set
    and ``force`` is false.  Returns a report dict; ``ok`` indicates the
    snapshot is ready for reads.
    """
    root = resolve_snapshot_root(config)
    manifest, frame = load_feature_snapshot(config)
    normalized = _normalize_symbols(symbols)
    universe_hash = _universe_hash(normalized)
    # 日期能力同时决定增量路径里的同日指纹比较是否可参与：无能力时指纹
    # 基准跨窗口不可比，比较会把全部候选误判为 dirty（见 _detect_revisions）。
    fingerprint_compare = _supports_incremental_dates(provider)
    dirty = (
        _compute_dirty_symbols(
            provider=provider,
            symbols=normalized,
            manifest=manifest,
        )
        if manifest is not None and frame is not None
        else []
    )
    if not dirty and manifest is not None and frame is not None:
        # No date-driven dirtiness: still probe for same-day revisions, which
        # a pure date comparison would miss.
        dirty = _detect_revisions(
            provider=provider,
            symbols=normalized,
            manifest=manifest,
        )
    scope_match = bool(
        manifest is not None
        and manifest.scope == scope
        and manifest.universe_hash == universe_hash
    )
    if (
        manifest is not None
        and snapshot_is_current(manifest, config)
        and scope_match
        and not dirty
        and not force
    ):
        return {
            "ok": True,
            "skipped": True,
            "data_snapshot_id": manifest.data_snapshot_id,
            "trade_date": manifest.trade_date,
            "symbol_count": manifest.symbol_count,
            "scope": manifest.scope,
            "universe_hash": manifest.universe_hash,
            "root": str(root),
        }

    lookback = max(60, int(lookback_days or config.week5.feature_snapshot_lookback_days))
    engineer = feature_engineer or FeatureEngineer()
    fetched_status = getattr(provider, "status", None)
    provider_status = fetched_status() if callable(fetched_status) else {}

    schema_hash = _feature_schema_hash(engineer)
    signature = compute_source_signature(config, provider_status)
    factor_hash = _factor_archive_hash(config)

    workers = max(1, min(8, int(max_workers)))
    chunk_size = max(1, int(batch_size))
    _write_progress_mark(
        progress_path,
        phase="snapshot_fetch",
        completed=0,
        total=len(normalized),
        workers=workers,
        batch_size=chunk_size,
    )

    # Incremental path: structurally-compatible old snapshot present.
    if (
        not force
        and manifest is not None
        and frame is not None
        and manifest.feature_schema_hash == schema_hash
        and manifest.factor_archive_hash == factor_hash
        and manifest.source_signature == signature
    ):
        return _incremental_snapshot_build(
            config=config,
            provider=provider,
            engineer=engineer,
            old_manifest=manifest,
            old_frame=frame,
            dirty=dirty,
            normalized_symbols=normalized,
            latest_date=manifest.trade_date,
            schema_hash=schema_hash,
            signature=signature,
            factor_hash=factor_hash,
            lookback=lookback,
            root=root,
            on_progress=on_progress,
            max_workers=workers,
            scope=scope,
            universe_hash=universe_hash,
            batch_size=chunk_size,
            progress_path=progress_path,
            fingerprint_compare=fingerprint_compare,
        )

    latest_date = _resolve_latest_trade_date(
        provider=provider,
        normalized_symbols=normalized,
        provider_status=provider_status,
    )

    # Parallel full build: thread pool for the I/O-bound fetch, process pool
    # for the CPU-bound feature engineering (max_workers is real here too).
    fetch_started = perf_counter()

    def _fetch_full(symbol: str) -> tuple[str, pd.DataFrame | None]:
        try:
            bars = provider.fetch_daily_bars(symbol=symbol, lookback_days=lookback)
            if bars is not None and isinstance(bars, pd.DataFrame) and not bars.empty:
                return symbol, bars
        except Exception:
            pass
        return symbol, None

    bars_by_symbol: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for symbol, bars in executor.map(_fetch_full, normalized):
            if bars is not None:
                bars_by_symbol[symbol] = bars
    failed = [symbol for symbol in normalized if symbol not in bars_by_symbol]
    fetch_ms = int((perf_counter() - fetch_started) * 1000)
    _write_progress_mark(
        progress_path,
        phase="snapshot_fetch",
        completed=len(bars_by_symbol),
        total=len(normalized),
        workers=workers,
        batch_size=chunk_size,
        failed=len(failed),
    )

    payloads = [
        (symbol, bars_by_symbol[symbol].reset_index().to_dict("list"))
        for symbol in bars_by_symbol
    ]
    transform_started = perf_counter()
    rows, failed = _transform_payloads_chunked(
        payloads=payloads,
        engineer=engineer,
        failed=failed,
        workers=workers,
        chunk_size=chunk_size,
        on_progress=on_progress,
        progress_path=progress_path,
    )
    transform_ms = int((perf_counter() - transform_started) * 1000)

    if not rows:
        failed = _dedupe_preserve_order(failed)
        return {
            "ok": False,
            "skipped": False,
            "errors": ["no_rows"],
            "failed_symbols": len(failed),
            "failed_symbols_list": failed,
            "root": str(root),
            "stages": {
                "snapshot_fetch": {
                    "duration_ms": fetch_ms,
                    "completed": len(bars_by_symbol),
                    "total": len(normalized),
                    "failed": len(failed),
                },
                "snapshot_transform": {
                    "duration_ms": transform_ms,
                    "completed": 0,
                    "total": len(payloads),
                    "failed": len(failed),
                },
            },
            "workers": workers,
            "batch_size": chunk_size,
            "scope": scope,
            "universe_hash": universe_hash,
        }

    failed = _dedupe_preserve_order(failed)
    frame = pd.concat(rows, ignore_index=True)
    tails_by_symbol = {
        symbol: bars.tail(lookback) for symbol, bars in bars_by_symbol.items()
    }
    bar_fingerprints = {
        symbol: _bar_tail_fingerprint(bars) for symbol, bars in bars_by_symbol.items()
    }
    snapshot_id = _new_snapshot_id(latest_date)
    _publish_snapshot_dir(
        root=root,
        snapshot_id=snapshot_id,
        frame=frame,
        tails_by_symbol=tails_by_symbol,
    )

    manifest = FeatureSnapshotManifest(
        data_snapshot_id=snapshot_id,
        trade_date=latest_date,
        built_at=datetime.now(UTC).isoformat(timespec="seconds"),
        feature_schema_hash=schema_hash,
        symbol_count=int(len(frame)),
        columns=[str(column) for column in frame.columns],
        source_signature=signature,
        source_provider=str(provider_status.get("provider_mode", "")),
        per_symbol=_per_symbol_entries(frame, bar_fingerprints),
        factor_archive_hash=factor_hash,
        dirty_count=len(normalized),
        refreshed_count=int(len(frame)),
        failed_symbols=len(failed),
        coverage_ratio=round(int(len(frame)) / max(len(normalized), 1), 4),
        max_trade_date=latest_date,
        failed_symbols_list=failed,
        scope=scope,
        universe_hash=universe_hash,
        requested_symbol_count=len(normalized),
        published_symbol_count=int(len(frame)),
    )
    _atomic_write_json(root / "current.json", manifest.to_payload())

    # Prune old snapshot directories (keep the previous one for rollback).
    _prune_old_snapshots(root, keep=2)

    return {
        "ok": not failed,
        "skipped": False,
        "data_snapshot_id": snapshot_id,
        "trade_date": latest_date,
        "symbol_count": int(len(frame)),
        "failed_symbols": len(failed),
        "failed_symbols_list": failed,
        "scope": scope,
        "universe_hash": universe_hash,
        "requested_symbol_count": len(normalized),
        "published_symbol_count": int(len(frame)),
        "root": str(root),
        "stages": {
            "snapshot_fetch": {
                "duration_ms": fetch_ms,
                "completed": len(bars_by_symbol),
                "total": len(normalized),
                "failed": len(failed),
            },
            "snapshot_transform": {
                "duration_ms": transform_ms,
                "completed": int(len(frame)),
                "total": len(payloads),
                "failed": len(failed),
            },
        },
        "workers": workers,
        "batch_size": chunk_size,
    }


def _compute_dirty_symbols(
    *,
    provider: object,
    symbols: list[str],
    manifest: FeatureSnapshotManifest,
) -> list[str]:
    """Symbols whose provider trade date is newer than the snapshot's entry.

    Providers without a ``latest_daily_dates`` interface cannot drive the
    date comparison; an empty result then delegates the freshness decision
    to ``_detect_revisions`` (per-symbol bar fingerprint probes), which runs
    whenever the date-driven pass yields nothing.  Same-day revisions are
    detected there as well (the date may be identical while the content
    changed).
    """
    latest_dates_fn = getattr(provider, "latest_daily_dates", None)
    if not callable(latest_dates_fn):
        return []
    try:
        current_dates = latest_dates_fn(symbols=symbols)
    except Exception:
        return []
    # None 表示包装链（如 CachedProvider/ResilientProvider）没有日期能力：
    # 与"接口不存在"同等对待，由 _detect_revisions 的 probe 兜底。
    # 空 dict 只意味着"有接口但无记录"，绝不能在这里当 None 处理
    # （否则每个 symbol 都会被误判为 dirty 导致每次全量刷新）。
    if current_dates is None:
        return []
    if not isinstance(current_dates, dict):
        return []
    dirty: list[str] = []
    for symbol in symbols:
        entry = manifest.per_symbol.get(symbol)
        if entry is None:
            dirty.append(symbol)
            continue
        current_date = current_dates.get(symbol)
        if current_date is None:
            # Provider has no record -> be conservative and recompute.
            dirty.append(symbol)
            continue
        try:
            current_iso = current_date.isoformat()
        except AttributeError:
            current_iso = str(current_date)
        latest = str(entry.get("latest_date", "")).strip()
        if current_iso > latest:
            dirty.append(symbol)
    # Symbols that failed the previous refresh are retried until they
    # succeed; otherwise a partial-failure snapshot would never heal.
    failed_symbols = set(manifest.failed_symbols_list)
    if failed_symbols:
        for symbol in symbols:
            if symbol in failed_symbols and symbol not in dirty:
                dirty.append(symbol)
    return dirty


# Number of recent bars fetched per symbol for the incremental probe.  The
# probe doubles as the same-day revision detector: content changes are caught
# by comparing the latest bar's fingerprint even when the trade date matches.
PROBE_DAYS = 5
TAILS_FILENAME = "tails.parquet"


def _supports_incremental_dates(provider: object) -> bool:
    """True when the provider can serve trade dates for incremental checks.

    A callable ``latest_daily_dates`` that returns ``None`` means the
    interface exists but the chain has NO date capability (a
    CachedProvider/ResilientProvider wrapping a provider without the
    interface): callers must fall back to bar probing, and must NOT compare
    fingerprints from short probe windows -- a synthetic source generates
    different tails per lookback window, so a probe fingerprint is not
    comparable to the one stored at build time.
    """
    latest_dates_fn = getattr(provider, "latest_daily_dates", None)
    if not callable(latest_dates_fn):
        return False
    try:
        return latest_dates_fn(symbols=[]) is not None
    except Exception:
        return False


def _detect_revisions(
    *,
    provider: object,
    symbols: list[str],
    manifest: FeatureSnapshotManifest,
) -> list[str]:
    """Symbols whose latest bar changed while the trade date stayed identical.

    Probes each symbol with a few recent bars (cheap) and compares the probe's
    latest trade date against the manifest entry — this detects a new trading
    day for ANY provider and is the freshness fallback when
    ``latest_daily_dates`` is unavailable.  Same-day content revisions are
    detected via the latest bar's fingerprint, but only for providers that
    expose the incremental date interface (real archives, including the
    production CachedProvider/ResilientProvider wrappers after pass-through):
    synthetic providers generate different tails per lookback window and
    would misreport.  A symbol whose probe fails is treated as dirty
    (conservative: better to refresh than to serve a stale row silently).
    """
    if not manifest.per_symbol:
        return []
    # 指纹同日修订检测只在具备日期能力时启用（真实 archive 的 tail 跨窗口
    # 一致；包装链无能力时返回 None，此时退化为纯日期探测）。
    fingerprint_compare = _supports_incremental_dates(provider)

    def _probe(symbol: str) -> tuple[str, str, str]:
        try:
            bars = provider.fetch_daily_bars(symbol=symbol, lookback_days=PROBE_DAYS)
            if bars is None or not isinstance(bars, pd.DataFrame) or bars.empty:
                return symbol, "", ""
            ordered = bars if bars.index.is_monotonic_increasing else bars.sort_index()
            latest_date = str(ordered.index[-1].date())
            return symbol, latest_date, _bar_tail_fingerprint(bars)
        except Exception:
            return symbol, "", ""

    revised: list[str] = []
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(symbols)))) as executor:
        for symbol, probe_date, fingerprint in executor.map(_probe, symbols):
            entry = manifest.per_symbol.get(symbol)
            stored_date = str(entry.get("latest_date", "")) if entry else ""
            stored_fp = str(entry.get("fingerprint", "")) if entry else ""
            if not probe_date:
                # Probe failed or no fresh data -> conservative refresh.
                if symbol in manifest.per_symbol:
                    revised.append(symbol)
                continue
            if stored_date and probe_date > stored_date:
                # New trading day observed through the probe window.
                revised.append(symbol)
                continue
            if fingerprint_compare and stored_fp and fingerprint and fingerprint != stored_fp:
                # Same-day content revision.
                revised.append(symbol)
    return revised


def _write_snapshot_tails(
    target_dir: Path,
    tails_by_symbol: dict[str, pd.DataFrame],
) -> None:
    """Persist the per-symbol rolling bar windows used by later increments."""
    if not tails_by_symbol:
        return
    combined = _tails_long_table(tails_by_symbol)
    combined.to_parquet(target_dir / TAILS_FILENAME, index=False)


def _tails_long_table(tails_by_symbol: dict[str, pd.DataFrame]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for symbol, bars in tails_by_symbol.items():
        frame = bars.reset_index().copy()
        frame["symbol"] = symbol
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _write_snapshot_tails_inherited(
    *,
    target_dir: Path,
    old_tails: pd.DataFrame | None,
    refreshed_tails: dict[str, pd.DataFrame],
) -> None:
    """Write the next snapshot's tails as ``old tails + dirty overrides``.

    Clean symbols keep their inherited tail window; only symbols that were
    refreshed this run get their window replaced.
    """
    clean_rows = pd.DataFrame()
    if old_tails is not None and not old_tails.empty and refreshed_tails:
        clean_rows = old_tails[~old_tails["symbol"].isin(set(refreshed_tails))]
    pieces: list[pd.DataFrame] = []
    if not clean_rows.empty:
        pieces.append(clean_rows)
    refreshed_long = _tails_long_table(refreshed_tails)
    if not refreshed_long.empty:
        pieces.append(refreshed_long)
    if not pieces:
        _write_snapshot_tails(target_dir, refreshed_tails)
        return
    combined = pd.concat(pieces, ignore_index=True)
    combined.to_parquet(target_dir / TAILS_FILENAME, index=False)


def _combined_tails_by_symbol(
    *,
    old_tails: pd.DataFrame | None,
    refreshed_tails: dict[str, pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    """Merge inherited clean tails with refreshed windows into {symbol: frame}.

    Equivalent to ``_write_snapshot_tails_inherited`` but returns the
    per-symbol dict so the caller can publish it atomically together with the
    feature parquet.
    """
    result: dict[str, pd.DataFrame] = {}
    if old_tails is not None and not old_tails.empty and refreshed_tails:
        clean = old_tails[~old_tails["symbol"].isin(set(refreshed_tails))]
        if not clean.empty:
            result = _tail_by_symbol_index(clean)
    for symbol, frame in refreshed_tails.items():
        result[str(symbol)] = frame
    return result


def load_snapshot_tails(root: Path, snapshot_id: str) -> pd.DataFrame | None:
    """Load the per-symbol bar windows of one snapshot; None when absent."""
    path = Path(root) / snapshot_id / TAILS_FILENAME
    if not path.exists():
        return None
    try:
        return pd.read_parquet(path)
    except Exception:
        return None


def _tail_by_symbol_index(tails: pd.DataFrame | None) -> dict[str, pd.DataFrame]:
    """Split the tails long table into {symbol: DataFrame(indexed by date)}."""
    if tails is None or tails.empty:
        return {}
    result: dict[str, pd.DataFrame] = {}
    date_columns = [
        column
        for column in ("date", "trade_date", "index")
        if column in tails.columns
    ]
    date_column = date_columns[0] if date_columns else None
    for symbol, group in tails.groupby("symbol"):
        frame = group.drop(columns=["symbol"]).copy()
        if date_column is not None:
            frame = frame.set_index(date_column)
        result[str(symbol)] = frame
    return result


def _splice_window(
    tail: pd.DataFrame | None,
    probe: pd.DataFrame | None,
    lookback: int,
) -> pd.DataFrame:
    """Combine the stored tail window with the fresh probe bars.

    The probe (recent bars) overrides the tail's overlapping rows, so new
    trading days and same-day revisions both land in the final window without
    re-fetching the full history.  Falls back to the probe when no tail.
    """
    if tail is None or tail.empty:
        if probe is None or probe.empty:
            return pd.DataFrame()
        return probe.tail(lookback).copy()
    if probe is None or probe.empty:
        return tail.tail(lookback).copy()
    tail_sorted = tail if tail.index.is_monotonic_increasing else tail.sort_index()
    probe_sorted = probe if probe.index.is_monotonic_increasing else probe.sort_index()
    tail_before = tail_sorted.loc[tail_sorted.index < probe_sorted.index.min()]
    combined = pd.concat([tail_before, probe_sorted])
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined.sort_index().tail(lookback)


def _transform_row_worker(
    payload: tuple[str, dict[str, object], object],
) -> tuple[str, dict[str, object] | None]:
    """ProcessPool worker: engineer one symbol's window into a snapshot row."""
    symbol, bars_dict, engineer = payload
    try:
        bars = pd.DataFrame.from_dict(bars_dict)
        if bars.empty or "close" not in bars.columns:
            return symbol, None
        if "date" in bars.columns:
            bars["date"] = pd.to_datetime(bars["date"])
            bars = bars.set_index("date")
        bars.index.name = "date"
        row = _snapshot_row_for_symbol(bars=bars, symbol=symbol, engineer=engineer)
        if row is None or row.empty:
            return symbol, None
        return symbol, row.iloc[0].to_dict()
    except Exception:
        return symbol, None


def _transform_batch_worker(
    payload: tuple[object, list[tuple[str, Any]]],
) -> list[tuple[str, dict[str, object] | None]]:
    """ProcessPool worker: engineer a batch of symbol windows.

    The engineer object is pickled once per batch instead of once per symbol,
    and each row is delegated to ``_transform_row_worker`` so tests that
    monkeypatch the per-row worker keep working.
    """
    engineer, batch = payload
    return [
        _transform_row_worker((symbol, bars_dict, engineer))
        for symbol, bars_dict in batch
    ]


def _transform_payloads_chunked(
    *,
    payloads: list[tuple[str, Any]],
    engineer: FeatureEngineer,
    failed: list[str],
    workers: int,
    chunk_size: int,
    on_progress: object | None = None,
    progress_path: str | None = None,
) -> tuple[list[pd.DataFrame], list[str]]:
    """Run feature engineering over ``payloads`` in bounded batches.

    A sliding window keeps at most ``workers`` batches in flight at any time:
    the first ``workers`` chunks are submitted, and each completed batch
    immediately frees a slot for the next chunk.  The IPC queue therefore
    never holds more than ``workers * chunk_size`` bar frames, and every
    completed batch atomically refreshes the progress file.  With
    ``workers <= 1`` the work runs inline on the calling process.
    """
    rows: list[pd.DataFrame] = []
    chunks = [
        payloads[index : index + chunk_size]
        for index in range(0, len(payloads), chunk_size)
    ]

    def _record_progress() -> None:
        _write_progress_mark(
            progress_path,
            phase="snapshot_transform",
            completed=len(rows),
            total=len(payloads),
            workers=workers,
            batch_size=chunk_size,
            failed=len(failed),
        )

    if payloads and workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            pending: dict[Future[Any], list[tuple[str, Any]]] = {}
            iterator = iter(chunks)

            def _submit_next() -> None:
                try:
                    chunk = next(iterator)
                except StopIteration:
                    return
                pending[executor.submit(_transform_batch_worker, (engineer, chunk))] = chunk

            for _ in range(min(workers, len(chunks))):
                _submit_next()
            while pending:
                done, _pending_set = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    chunk = pending.pop(future)
                    for symbol, row_dict in future.result():
                        if row_dict is None:
                            failed.append(str(symbol))
                            continue
                        rows.append(pd.DataFrame([row_dict]))
                        if callable(on_progress):
                            on_progress(len(rows), len(payloads))
                    _submit_next()
                _record_progress()
    else:
        for chunk in chunks:
            for symbol, row_dict in _transform_batch_worker((engineer, chunk)):
                if row_dict is None:
                    failed.append(str(symbol))
                    continue
                rows.append(pd.DataFrame([row_dict]))
                if callable(on_progress):
                    on_progress(len(rows), len(payloads))
            _record_progress()
    return rows, failed


def _publish_snapshot_dir(
    *,
    root: Path,
    snapshot_id: str,
    frame: pd.DataFrame,
    tails_by_symbol: dict[str, pd.DataFrame] | None = None,
) -> None:
    """Write snapshot artifacts under a staging dir, then atomically rename.

    current.json only ever points at a fully-written directory: the parquet
    and tails land in ``<root>/<snapshot_id>.staging`` first and the staging
    directory is renamed into place, so a crash mid-write can never leave a
    half-product behind.  The caller publishes current.json afterwards.
    """
    staging = root / f"{snapshot_id}.staging"
    if staging.exists():
        import shutil

        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(staging / SNAPSHOT_FILENAME, index=False)
    if tails_by_symbol:
        _write_snapshot_tails(staging, tails_by_symbol)
    final_dir = root / snapshot_id
    if final_dir.exists():
        import shutil

        shutil.rmtree(final_dir, ignore_errors=True)
    _replace_snapshot_dir_with_retry(staging=staging, final_dir=final_dir)


def _replace_snapshot_dir_with_retry(*, staging: Path, final_dir: Path) -> None:
    """Retry transient Windows directory locks without masking real failures."""
    attempts = 5
    for attempt in range(attempts):
        try:
            staging.replace(final_dir)
            return
        except PermissionError:
            if attempt >= attempts - 1:
                raise
            sleep(0.075 * (attempt + 1))


def _write_progress_mark(
    progress_path: str | None,
    *,
    phase: str,
    completed: int,
    total: int,
    workers: int,
    batch_size: int,
    failed: int = 0,
) -> None:
    """Atomically record a phase-level progress mark for external monitoring."""
    if not progress_path:
        return
    path = Path(str(progress_path)).expanduser()
    try:
        _atomic_write_json(
            path,
            {
                "phase": phase,
                "completed": int(completed),
                "total": int(total),
                "failed": int(failed),
                "workers": int(workers),
                "batch_size": int(batch_size),
                "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            },
        )
    except OSError:
        pass


def _incremental_snapshot_build(
    *,
    config: StockAnalyzerConfig,
    provider: object,
    engineer: FeatureEngineer,
    old_manifest: FeatureSnapshotManifest,
    old_frame: pd.DataFrame,
    dirty: list[str],
    normalized_symbols: list[str],
    latest_date: str,
    schema_hash: str,
    signature: str,
    factor_hash: str,
    lookback: int,
    root: Path,
    on_progress: object | None = None,
    max_workers: int = 4,
    scope: str = "",
    universe_hash: str = "",
    batch_size: int = 20,
    progress_path: str | None = None,
    fingerprint_compare: bool = False,
) -> dict[str, object]:
    """Refresh only changed symbols and merge them into the old parquet.

    Each symbol is *probed* with a small number of recent bars (PROBE_DAYS)
    instead of re-reading the full window.  The probe drives both the
    same-day-revision detection (fingerprint comparison, only when
    ``fingerprint_compare`` -- the provider exposes the incremental date
    interface) and the refresh itself (tail + probe splicing).  Fetching runs
    on a thread pool; the feature engineering runs on a process pool so
    ``max_workers`` is real.

    The published frame is enforced to exactly the requested candidate set:
    symbols that left the set (present in the old frame but missing from
    ``normalized_symbols``) are dropped from the frame, the per-symbol
    bookkeeping and the inherited tail windows.
    """
    workers = max(1, min(8, int(max_workers)))
    chunk_size = max(1, int(batch_size))
    requested_set = set(normalized_symbols)
    old_symbols = set(
        str(item).strip()
        for item in old_frame["symbol"].tolist()
        if str(item).strip()
    )
    removed_symbols = sorted(old_symbols - requested_set)
    # Candidate-set membership is enforced independently of the provider's
    # incremental interface: symbols requested but absent from the old frame
    # or bookkeeping are ALWAYS refreshed (a provider without
    # ``latest_daily_dates`` cannot drive date-based dirtiness by itself).
    missing_from_old = [
        symbol
        for symbol in normalized_symbols
        if symbol not in old_symbols or symbol not in old_manifest.per_symbol
    ]
    if missing_from_old:
        dirty = _dedupe_preserve_order(dirty + missing_from_old)

    if not dirty and not removed_symbols:
        # Nothing to recompute; refresh the freshness stamp so age-based
        # expiry (e.g. after a long holiday) does not force a wasted rebuild.
        # A changed scope/universe label is carried over so the manifest stays
        # honest about which candidate set it serves.  Failed symbols that
        # left the candidate set are dropped from the failure bookkeeping:
        # they were retried while requested (dirty computation re-adds them),
        # so at this point any remaining failed entry is out of scope and must
        # not keep the snapshot permanently non-current.
        refreshed = dict(old_manifest.to_payload())
        refreshed["built_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        refreshed["scope"] = scope
        refreshed["universe_hash"] = universe_hash
        refreshed["requested_symbol_count"] = len(normalized_symbols)
        refreshed["published_symbol_count"] = int(len(old_frame))
        failed_symbols_list = [
            symbol
            for symbol in old_manifest.failed_symbols_list
            if symbol in requested_set
        ]
        refreshed["failed_symbols_list"] = failed_symbols_list
        refreshed["failed_symbols"] = len(failed_symbols_list)
        _atomic_write_json(root / "current.json", refreshed)
        return {
            "ok": not failed_symbols_list,
            "skipped": False,
            "touched": True,
            "data_snapshot_id": old_manifest.data_snapshot_id,
            "trade_date": latest_date,
            "symbol_count": int(len(old_frame)),
            "dirty_symbols": 0,
            "failed_symbols": len(failed_symbols_list),
            "removed_symbols": [],
            "scope": scope,
            "universe_hash": universe_hash,
            "root": str(root),
        }

    if not dirty:
        # Candidate set shrank but nothing is date-dirty: publish the subset
        # without touching the untouched rows (keeps their tails/fingerprints).
        merged = old_frame[old_frame["symbol"].isin(requested_set)].reset_index(drop=True)
        per_symbol = {
            symbol: entry
            for symbol, entry in old_manifest.per_symbol.items()
            if symbol in requested_set
        }
        # Failure bookkeeping is restricted to the surviving candidate set for
        # the same reason as above (out-of-scope failures must not block).
        failed_symbols_list = [
            symbol
            for symbol in old_manifest.failed_symbols_list
            if symbol in requested_set
        ]
        old_tails = load_snapshot_tails(root, old_manifest.data_snapshot_id)
        inherited_tails = (
            old_tails[old_tails["symbol"].isin(requested_set)]
            if old_tails is not None and not old_tails.empty
            else None
        )
        snapshot_id = _new_snapshot_id(latest_date)
        _publish_snapshot_dir(
            root=root,
            snapshot_id=snapshot_id,
            frame=merged,
            tails_by_symbol=(
                _tail_by_symbol_index(inherited_tails)
                if inherited_tails is not None
                else {}
            ),
        )
        new_manifest = FeatureSnapshotManifest(
            data_snapshot_id=snapshot_id,
            trade_date=latest_date,
            built_at=datetime.now(UTC).isoformat(timespec="seconds"),
            feature_schema_hash=schema_hash,
            symbol_count=int(len(merged)),
            columns=[str(column) for column in merged.columns],
            source_signature=signature,
            source_provider=old_manifest.source_provider,
            per_symbol=per_symbol,
            factor_archive_hash=factor_hash,
            dirty_count=0,
            refreshed_count=0,
            failed_symbols=len(failed_symbols_list),
            coverage_ratio=old_manifest.coverage_ratio,
            max_trade_date=latest_date,
            failed_symbols_list=failed_symbols_list,
            scope=scope,
            universe_hash=universe_hash,
            requested_symbol_count=len(normalized_symbols),
            published_symbol_count=int(len(merged)),
        )
        _atomic_write_json(root / "current.json", new_manifest.to_payload())
        _prune_old_snapshots(root, keep=2)
        return {
            "ok": not failed_symbols_list,
            "skipped": False,
            "data_snapshot_id": snapshot_id,
            "trade_date": latest_date,
            "symbol_count": int(len(merged)),
            "incremental": True,
            "dirty_symbols": 0,
            "refreshed_count": 0,
            "failed_symbols": len(failed_symbols_list),
            "removed_symbols": removed_symbols,
            "coverage_ratio": old_manifest.coverage_ratio,
            "max_trade_date": latest_date,
            "root": str(root),
        }

    # Probe every symbol with a few recent bars (I/O-bound -> thread pool).
    probes: dict[str, pd.DataFrame] = {}
    fetch_started = perf_counter()

    def _probe_symbol(symbol: str) -> tuple[str, pd.DataFrame | None]:
        try:
            bars = provider.fetch_daily_bars(symbol=symbol, lookback_days=PROBE_DAYS)
            if bars is not None and isinstance(bars, pd.DataFrame) and not bars.empty:
                return symbol, bars
        except Exception:
            pass
        return symbol, None

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for symbol, bars in executor.map(_probe_symbol, normalized_symbols):
            probes[symbol] = bars
    probe_ms = int((perf_counter() - fetch_started) * 1000)
    _write_progress_mark(
        progress_path,
        phase="snapshot_fetch",
        completed=len(probes),
        total=len(normalized_symbols),
        workers=workers,
        batch_size=chunk_size,
    )

    # Same-day revision detection: fingerprint of the latest probed bar vs the
    # manifest entry.  Content may change while the trade date stays identical.
    # Only providers with the incremental date interface participate: for the
    # others the probe fingerprint (short window) is not comparable to the one
    # stored at build time (full window), so a window-dependent source would
    # falsely mark every candidate dirty (see ``_supports_incremental_dates``).
    if fingerprint_compare:
        for symbol in normalized_symbols:
            if symbol in dirty:
                continue
            entry = old_manifest.per_symbol.get(symbol)
            if entry is None:
                dirty.append(symbol)
                continue
            bars = probes.get(symbol)
            if bars is None or bars.empty:
                continue
            current_fp = _bar_tail_fingerprint(bars)
            stored_fp = str(entry.get("fingerprint", ""))
            if current_fp and stored_fp and current_fp != stored_fp:
                dirty.append(symbol)
        dirty = _dedupe_preserve_order(dirty)

    # Refresh: build each dirty symbol's window from tail + probe, then run
    # feature engineering on the process pool.
    old_tails = load_snapshot_tails(root, old_manifest.data_snapshot_id)
    tails = _tail_by_symbol_index(old_tails)

    def _window_for_symbol(symbol: str) -> tuple[str, pd.DataFrame]:
        probe = probes.get(symbol)
        tail = tails.get(symbol)
        if probe is None or probe.empty:
            # No fresh data source for this symbol -> treat as failed so the
            # refresh is not published as complete and the symbol is retried.
            return symbol, pd.DataFrame()
        if tail is not None and not tail.empty:
            return symbol, _splice_window(tail, probe, lookback)
        try:
            bars = provider.fetch_daily_bars(symbol=symbol, lookback_days=lookback)
            return symbol, bars
        except Exception:
            return symbol, pd.DataFrame()

    windows: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for symbol, window in executor.map(_window_for_symbol, dirty):
            windows[symbol] = window

    payloads = [
        (symbol, windows[symbol].reset_index().to_dict("list"))
        for symbol in dirty
        if windows.get(symbol) is not None and not windows[symbol].empty
    ]
    transform_started = perf_counter()
    rows, failed = _transform_payloads_chunked(
        payloads=payloads,
        engineer=engineer,
        failed=[],
        workers=workers,
        chunk_size=chunk_size,
        on_progress=on_progress,
        progress_path=progress_path,
    )
    transform_ms = int((perf_counter() - transform_started) * 1000)
    # Symbols whose window could not be built at all count as failed too.
    refreshed_symbols = {
        str(frame.iloc[0]["symbol"]) for frame in rows if not frame.empty
    }
    failed = _dedupe_preserve_order(
        failed
        + [
            symbol
            for symbol in dirty
            if symbol not in refreshed_symbols and symbol not in failed
        ]
    )

    if rows:
        new_rows = pd.concat(rows, ignore_index=True)
        refreshed_set = set(new_rows["symbol"])
        keep = old_frame[
            old_frame["symbol"].isin(requested_set)
            & ~old_frame["symbol"].isin(refreshed_set)
        ]
        merged = pd.concat([keep, new_rows], ignore_index=True).reset_index(drop=True)
    else:
        merged = old_frame[old_frame["symbol"].isin(requested_set)].reset_index(drop=True)

    snapshot_id = _new_snapshot_id(latest_date)

    # Persist tail windows: inherit ALL old tails (limited to the requested
    # candidate set) and override with the dirty symbols' refreshed windows.
    # Without inheritance, clean symbols lose their tail and the next run
    # falls back to full-window fetches for them, degrading incremental
    # performance over consecutive days.
    refreshed_tails: dict[str, pd.DataFrame] = {}
    for symbol in dirty:
        window = windows.get(symbol)
        if window is not None and not window.empty:
            refreshed_tails[symbol] = window.tail(lookback)
    inherited_tails = old_tails
    if inherited_tails is not None and not inherited_tails.empty:
        inherited_tails = inherited_tails[inherited_tails["symbol"].isin(requested_set)]
    combined_tails = _combined_tails_by_symbol(
        old_tails=inherited_tails,
        refreshed_tails=refreshed_tails,
    )
    _publish_snapshot_dir(
        root=root,
        snapshot_id=snapshot_id,
        frame=merged,
        tails_by_symbol=combined_tails,
    )

    bar_fingerprints = {
        symbol: _bar_tail_fingerprint(windows[symbol])
        for symbol in dirty
        if windows.get(symbol) is not None and not windows[symbol].empty
    }
    per_symbol = {
        symbol: entry
        for symbol, entry in old_manifest.per_symbol.items()
        if symbol in requested_set
    }
    # Only the dirty symbols' entries are rebuilt; clean symbols keep their
    # old latest_date AND fingerprint so same-day revision detection keeps
    # working for them on future runs.
    refreshed_rows = new_rows if rows else pd.DataFrame()
    per_symbol.update(_per_symbol_entries(refreshed_rows, bar_fingerprints))

    # Advance the market trade date to the freshest actually-refreshed row;
    # do not reuse the old manifest date when dirty symbols progressed.
    refreshed_count = len(rows)
    max_trade_date = latest_date
    if rows and "trade_date" in new_rows.columns:
        try:
            max_trade_date = str(
                pd.to_datetime(new_rows["trade_date"]).max().date().isoformat()
            )
        except Exception:
            max_trade_date = latest_date
    if max_trade_date and old_manifest.trade_date and max_trade_date < old_manifest.trade_date:
        max_trade_date = old_manifest.trade_date
    coverage_ratio = round(
        refreshed_count / max(len(dirty), 1), 4
    )

    new_manifest = FeatureSnapshotManifest(
        data_snapshot_id=snapshot_id,
        trade_date=max_trade_date,
        built_at=datetime.now(UTC).isoformat(timespec="seconds"),
        feature_schema_hash=schema_hash,
        symbol_count=int(len(merged)),
        columns=[str(column) for column in merged.columns],
        source_signature=signature,
        source_provider=str(
            getattr(provider, "status", lambda: {})().get("provider_mode", "")
            if callable(getattr(provider, "status", None))
            else ""
        ),
        per_symbol=per_symbol,
        factor_archive_hash=factor_hash,
        dirty_count=len(dirty),
        refreshed_count=refreshed_count,
        failed_symbols=len(failed),
        coverage_ratio=coverage_ratio,
        max_trade_date=max_trade_date,
        failed_symbols_list=failed,
        scope=scope,
        universe_hash=universe_hash,
        requested_symbol_count=len(normalized_symbols),
        published_symbol_count=int(len(merged)),
    )
    _atomic_write_json(root / "current.json", new_manifest.to_payload())
    _prune_old_snapshots(root, keep=2)

    return {
        "ok": True,
        "skipped": False,
        "data_snapshot_id": snapshot_id,
        "trade_date": max_trade_date,
        "symbol_count": int(len(merged)),
        "incremental": True,
        "dirty_symbols": len(dirty),
        "refreshed_count": refreshed_count,
        "failed_symbols": len(failed),
        "removed_symbols": removed_symbols,
        "coverage_ratio": coverage_ratio,
        "max_trade_date": max_trade_date,
        "scope": scope,
        "universe_hash": universe_hash,
        "requested_symbol_count": len(normalized_symbols),
        "published_symbol_count": int(len(merged)),
        "root": str(root),
        "stages": {
            "snapshot_fetch": {
                "duration_ms": probe_ms,
                "completed": len(probes),
                "total": len(normalized_symbols),
                "failed": len(failed),
            },
            "snapshot_transform": {
                "duration_ms": transform_ms,
                "completed": refreshed_count,
                "total": len(payloads),
                "failed": len(failed),
            },
        },
        "workers": workers,
        "batch_size": chunk_size,
    }


def _per_symbol_entries(
    frame: pd.DataFrame,
    bar_fingerprints: dict[str, str] | None = None,
) -> dict[str, dict[str, str]]:
    """Build the per-symbol incremental bookkeeping map from a snapshot frame."""
    fingerprints = bar_fingerprints or {}
    entries: dict[str, dict[str, str]] = {}
    for _, row in frame.iterrows():
        symbol = str(row.get("symbol", "")).strip()
        if not symbol:
            continue
        entries[symbol] = {
            "latest_date": str(row.get("trade_date", "")),
            "fingerprint": fingerprints.get(symbol, ""),
        }
    return entries


def _bar_tail_fingerprint(bars: pd.DataFrame) -> str:
    """Content fingerprint of a bar frame's latest bar (close/volume/flow).

    Used to detect same-day revisions: if the latest bar's values change
    while the trade date stays the same, the fingerprint changes.
    """
    ordered = bars if bars.index.is_monotonic_increasing else bars.sort_index()
    if ordered.empty:
        return ""
    latest = ordered.iloc[-1]
    parts: list[str] = []
    for column in ("close", "high", "low", "volume", "turnover"):
        try:
            numeric = float(latest.get(column, 0.0))
            if pd.isna(numeric):
                numeric = 0.0
        except (TypeError, ValueError):
            numeric = 0.0
        parts.append(f"{numeric:.6f}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _factor_archive_hash(config: StockAnalyzerConfig) -> str:
    """Content fingerprint of the vendor factor ZIPs (qfq/hfq), or "" if absent.

    Uses the entry-level (name, size, CRC32) metadata from the ZIP central
    directory — deliberately NOT the archive mtime: the factor ZIPs are
    rebuilt every night and an unchanged-content rebuild must not invalidate
    the snapshot (which would force a full rebuild every day).
    """
    candidates: list[Path] = []
    index_path = str(config.data_source.vendor_zip_index_path).strip()
    if index_path:
        candidates.append(Path(index_path).expanduser().parent)
    data_root = str(config.data_source.local_data_root).strip()
    if data_root:
        candidates.append(Path(data_root).expanduser())
    seen: set[str] = set()
    parts: list[str] = []
    for candidate in candidates:
        factors_dir = candidate / "复权因子"
        if not factors_dir.is_dir():
            continue
        for archive in sorted(factors_dir.glob("*.zip")):
            try:
                with zipfile.ZipFile(archive) as zf:
                    entries = sorted(
                        (info.filename, info.file_size, info.CRC)
                        for info in zf.infolist()
                        if not info.is_dir()
                    )
            except Exception:
                continue
            digest = hashlib.sha256(repr(entries).encode("utf-8")).hexdigest()[:16]
            key = f"{archive.name}:{digest}"
            if key in seen:
                continue
            seen.add(key)
            parts.append(key)
    if not parts:
        return ""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    """Atomic JSON write via a sibling temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _snapshot_row_for_symbol(
    *,
    bars: pd.DataFrame,
    symbol: str,
    engineer: FeatureEngineer,
) -> pd.DataFrame | None:
    """One row: symbol + trade_date + engineered features + raw columns."""
    try:
        features = engineer.transform(bars)
    except Exception:
        return None
    if features is None or features.empty:
        return None
    latest_features = features.iloc[-1]
    raw = _raw_snapshot_values(bars=bars, features=latest_features)
    if raw is None:
        return None
    payload: dict[str, object] = {
        "symbol": symbol,
        "trade_date": str(bars.index[-1].date()),
    }
    payload.update(raw)
    for key, value in latest_features.to_dict().items():
        payload[str(key)] = float(value)
    return pd.DataFrame([payload])


def _raw_snapshot_values(
    *,
    bars: pd.DataFrame,
    features: pd.Series,
) -> dict[str, object] | None:
    """Latest-window raw metrics equivalent to the direct bars path."""
    ordered = bars if bars.index.is_monotonic_increasing else bars.sort_index()
    close = pd.to_numeric(ordered["close"], errors="coerce").dropna()
    if close.empty:
        return None
    latest_close = float(close.iloc[-1])
    close_count = len(close)
    ma20 = float(close.tail(min(20, close_count)).mean())
    ma60 = float(close.tail(min(60, close_count)).mean())
    ma120 = float(close.tail(min(120, close_count)).mean())
    ma240 = float(close.tail(min(240, close_count)).mean())
    ret20 = _ret_at(close, 20)
    ret60 = _ret_at(close, 60)
    ret120 = _ret_at(close, 120)
    recent_high = float(close.max())
    volume = pd.to_numeric(ordered["volume"], errors="coerce").fillna(0.0)
    turnover = pd.to_numeric(
        ordered.get("turnover", pd.Series(np.nan, index=ordered.index)),
        errors="coerce",
    )
    if turnover.isna().all() and "amount" in ordered.columns:
        turnover = pd.to_numeric(ordered["amount"], errors="coerce")
    turnover = turnover.fillna(0.0)
    avg_turnover_20 = float(turnover.tail(min(20, len(turnover))).mean())
    avg_turnover_60 = float(turnover.tail(min(60, len(turnover))).mean())
    volume_5d = float(volume.tail(5).mean())
    volume_20d = float(volume.tail(20).mean())
    high = pd.to_numeric(ordered["high"], errors="coerce")
    low = pd.to_numeric(ordered["low"], errors="coerce")
    # Ratio-style ATR (same semantics as the light-stage scorer).
    atr_ratio = (
        (high - low) / close.replace(0.0, np.nan)
    ).replace([np.inf, -np.inf], np.nan)
    atr_ratio = atr_ratio.fillna(0.0)
    atr_20d = (
        float(atr_ratio.tail(min(20, len(atr_ratio))).mean())
        if not atr_ratio.empty
        else 0.0
    )
    atr_60d = (
        float(atr_ratio.tail(min(60, len(atr_ratio))).mean())
        if not atr_ratio.empty
        else atr_20d
    )
    returns20 = close.pct_change().dropna().tail(20)
    volatility_20d = float(returns20.std(ddof=0)) if not returns20.empty else 0.0
    float_market_cap = float(
        pd.to_numeric(ordered["float_market_cap"], errors="coerce").iloc[-1]
    ) if "float_market_cap" in ordered.columns else 0.0
    turnover_rate_20d = avg_turnover_20 / float_market_cap if float_market_cap > 0 else 0.0
    holder_chg = float(features.get("holder_count_chg_20", 0.0))
    northbound_20 = float(features.get("northbound_net_20", 0.0))
    dragon_freq = float(features.get("bg_dragon_tiger_freq20", 0.0))
    latest_bar = ordered.iloc[-1] if not ordered.empty else None
    return {
        "latest_close": latest_close,
        "ma20": ma20,
        "ma60": ma60,
        "ma120": ma120,
        "ma240": ma240,
        "ret20": ret20,
        "ret60": ret60,
        "ret120": ret120,
        "recent_high": recent_high,
        "avg_turnover_20": avg_turnover_20,
        "avg_turnover_60": avg_turnover_60,
        "volume_5d": volume_5d,
        "volume_20d": volume_20d,
        "atr_20d": atr_20d,
        "atr_60d": atr_60d,
        "volatility_20d": volatility_20d,
        "float_market_cap": float_market_cap,
        "turnover_rate_20d": turnover_rate_20d,
        "holder_count_chg_60d": holder_chg,
        "northbound_net_20d": northbound_20,
        "dragon_tiger_freq_20d": dragon_freq,
        "bg_holder_present": _latest_present(latest_bar, "holder_count"),
        "bg_block_trade_present": _latest_present(latest_bar, "block_trade_net"),
        "bg_financing_present": _latest_present(
            latest_bar,
            "margin_financing_balance",
            "financing_balance",
        ),
        "bg_northbound_present": _latest_present(latest_bar, "northbound_net"),
        "bg_dragon_tiger_present": _latest_present(latest_bar, "dragon_tiger_flag"),
        "bg_roe_present": _latest_present(latest_bar, "roe"),
        "bg_debt_ratio_present": _latest_present(latest_bar, "debt_ratio"),
        "suspended": (
            bool(ordered["suspended"].iloc[-1])
            if "suspended" in ordered.columns
            else False
        ),
        "is_st": bool(ordered["is_st"].iloc[-1]) if "is_st" in ordered.columns else False,
        "is_delisting_risk": bool(ordered["is_delisting_risk"].iloc[-1])
        if "is_delisting_risk" in ordered.columns
        else False,
        "financial_data_complete": bool(
            ordered["financial_data_complete"].iloc[-1]
        ) if "financial_data_complete" in ordered.columns else False,
        "background_data_complete": bool(
            ordered["background_data_complete"].iloc[-1]
        ) if "background_data_complete" in ordered.columns else False,
    }


def _latest_present(latest_bar: pd.Series | None, *columns: str) -> bool:
    if latest_bar is None:
        return False
    for column in columns:
        if column in latest_bar.index:
            value = latest_bar[column]
            try:
                if value is not None and not pd.isna(value):
                    return True
            except (TypeError, ValueError):
                if value is not None:
                    return True
    return False


def _ret_at(close: pd.Series, window: int) -> float:
    if len(close) <= window:
        return 0.0
    start = float(close.iloc[-window - 1])
    if start <= 0:
        return 0.0
    return float(close.iloc[-1]) / start - 1.0


def _normalize_symbols(symbols: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in symbols or []:
        code = str(item).strip()
        if not code:
            continue
        digits = "".join(ch for ch in code if ch.isdigit())
        key = digits if len(digits) == 6 else code
        if key and key not in seen:
            seen.add(key)
            normalized.append(key)
    return normalized


def _universe_hash(symbols: list[str]) -> str:
    """Stable hash of the requested candidate set (order-insensitive)."""
    ordered = sorted({str(item).strip() for item in symbols if str(item).strip()})
    return hashlib.sha256("|".join(ordered).encode("utf-8")).hexdigest()[:16]


def _resolve_latest_trade_date(
    *,
    provider: object,
    normalized_symbols: list[str],
    provider_status: dict[str, object],
) -> str:
    raw = str(provider_status.get("data_latest_trade_date", "")).strip()
    if raw:
        return raw
    if not normalized_symbols:
        return datetime.now(UTC).date().isoformat()
    try:
        bars = provider.fetch_daily_bars(symbol=normalized_symbols[0], lookback_days=10)
        if isinstance(bars, pd.DataFrame) and not bars.empty:
            return str(bars.index[-1].date())
    except Exception:
        pass
    return datetime.now(UTC).date().isoformat()


def _feature_schema_hash(engineer: FeatureEngineer) -> str:
    """Stable hash over the engineered output schema (column set).

    Probes the real feature pipeline with a synthetic bar frame so any change
    to the feature column definitions or compute logic changes the hash and
    invalidates existing snapshots.
    """
    try:
        probe = _dummy_bars()
        features = engineer.transform(probe)
        columns: list[str] = []
        if features is not None and not features.empty:
            columns = [str(column) for column in features.columns]
    except Exception:
        columns = []
    payload = f"{type(engineer).__name__}:{columns}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _dummy_bars() -> pd.DataFrame:
    """Minimal deterministic bar frame accepted by FeatureEngineer.transform."""
    rng = np.random.default_rng(7)
    dates = pd.bdate_range(end="2026-01-30", periods=60)
    close = np.cumprod(1 + rng.normal(0.0005, 0.01, size=len(dates))) * 10
    open_price = close * (1 + rng.normal(0, 0.002, size=len(dates)))
    high = np.maximum(open_price, close) * 1.01
    low = np.minimum(open_price, close) * 0.99
    volume = rng.integers(1_000_000, 8_000_000, size=len(dates)).astype(float)
    turnover = volume * close
    float_market_cap = np.full(len(dates), 10_000_000_000.0)
    frame = pd.DataFrame(
        {
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "turnover": turnover,
            "float_market_cap": float_market_cap,
            "suspended": False,
            "is_st": False,
            "is_delisting_risk": False,
            "roe": np.nan,
            "debt_ratio": np.nan,
            "holder_count": np.full(len(dates), 50_000.0),
            "block_trade_net": np.zeros(len(dates)),
            "margin_financing_balance": np.full(len(dates), 2_000_000_000.0),
            "northbound_net": np.zeros(len(dates)),
            "dragon_tiger_flag": np.zeros(len(dates)),
        },
        index=dates,
    )
    frame.index.name = "date"
    return frame


def _new_snapshot_id(trade_date: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"snap_{trade_date}_{stamp}"


def _prune_old_snapshots(root: Path, keep: int = 2) -> None:
    candidates = [
        item
        for item in root.iterdir()
        if item.is_dir()
        and item.name.startswith("snap_")
        and not item.name.endswith(".staging")
    ]
    candidates.sort(key=lambda item: item.name, reverse=True)
    for old in candidates[keep:]:
        try:
            import shutil

            shutil.rmtree(old)
        except OSError:
            pass


def _parse_utc(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
