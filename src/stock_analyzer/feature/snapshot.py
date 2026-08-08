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
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
) -> dict[str, object]:
    """Build (or incrementally refresh) the full-market feature snapshot.

    Incremental semantics: when a structurally-compatible snapshot exists
    (same schema hash, factor archives, provider and data-root layout), only
    dirty symbols -- those missing from the manifest or with a newer provider
    trade date -- are re-fetched and re-engineered, then merged into the old
    parquet (replacing the touched rows).  A full rebuild happens on first
    build, on schema/factor/signature drift, or with ``force``.

    Skips when a current snapshot already exists and ``force`` is false.
    Returns a report dict; ``ok`` indicates the snapshot is ready for reads.
    """
    root = resolve_snapshot_root(config)
    manifest, frame = load_feature_snapshot(config)
    dirty = (
        _compute_dirty_symbols(
            provider=provider,
            symbols=_normalize_symbols(symbols),
            manifest=manifest,
        )
        if manifest is not None and frame is not None
        else []
    )
    if (
        manifest is not None
        and snapshot_is_current(manifest, config)
        and not dirty
        and not force
    ):
        return {
            "ok": True,
            "skipped": True,
            "data_snapshot_id": manifest.data_snapshot_id,
            "trade_date": manifest.trade_date,
            "symbol_count": manifest.symbol_count,
            "root": str(root),
        }

    lookback = max(60, int(lookback_days or config.week5.feature_snapshot_lookback_days))
    normalized_symbols = _normalize_symbols(symbols)
    engineer = feature_engineer or FeatureEngineer()
    fetched_status = getattr(provider, "status", None)
    provider_status = fetched_status() if callable(fetched_status) else {}

    schema_hash = _feature_schema_hash(engineer)
    signature = compute_source_signature(config, provider_status)
    factor_hash = _factor_archive_hash(config)

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
            normalized_symbols=normalized_symbols,
            latest_date=manifest.trade_date,
            schema_hash=schema_hash,
            signature=signature,
            factor_hash=factor_hash,
            lookback=lookback,
            root=root,
            on_progress=on_progress,
        )

    latest_date = _resolve_latest_trade_date(
        provider=provider,
        normalized_symbols=normalized_symbols,
        provider_status=provider_status,
    )

    rows: list[pd.DataFrame] = []
    failed: list[str] = []
    for index in range(0, len(normalized_symbols), 500):
        batch = normalized_symbols[index : index + 500]
        batch_rows: list[pd.DataFrame] = []
        for symbol in batch:
            try:
                bars = provider.fetch_daily_bars(symbol=symbol, lookback_days=lookback)
            except Exception:
                failed.append(symbol)
                continue
            if bars is None or not isinstance(bars, pd.DataFrame) or bars.empty:
                failed.append(symbol)
                continue
            row = _snapshot_row_for_symbol(
                bars=bars,
                symbol=symbol,
                engineer=engineer,
            )
            if row is not None:
                batch_rows.append(row)
        if batch_rows:
            rows.append(pd.concat(batch_rows, ignore_index=True))
        if callable(on_progress):
            on_progress(min(index + 500, len(normalized_symbols)), len(normalized_symbols))

    if not rows:
        return {"ok": False, "skipped": False, "errors": ["no_rows"], "root": str(root)}

    frame = pd.concat(rows, ignore_index=True)
    snapshot_id = _new_snapshot_id(latest_date)
    target_dir = root / snapshot_id
    target_dir.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(target_dir / SNAPSHOT_FILENAME, index=False)

    manifest = FeatureSnapshotManifest(
        data_snapshot_id=snapshot_id,
        trade_date=latest_date,
        built_at=datetime.now(UTC).isoformat(timespec="seconds"),
        feature_schema_hash=schema_hash,
        symbol_count=int(len(frame)),
        columns=[str(column) for column in frame.columns],
        source_signature=signature,
        source_provider=str(provider_status.get("provider_mode", "")),
        per_symbol=_per_symbol_entries(frame),
        factor_archive_hash=factor_hash,
    )
    _atomic_write_json(root / "current.json", manifest.to_payload())

    # Prune old snapshot directories (keep the previous one for rollback).
    _prune_old_snapshots(root, keep=2)

    return {
        "ok": True,
        "skipped": False,
        "data_snapshot_id": snapshot_id,
        "trade_date": latest_date,
        "symbol_count": int(len(frame)),
        "failed_symbols": len(failed),
        "root": str(root),
    }


def _compute_dirty_symbols(
    *,
    provider: object,
    symbols: list[str],
    manifest: FeatureSnapshotManifest,
) -> list[str]:
    """Symbols whose provider trade date is newer than the snapshot's entry.

    Providers without a ``latest_daily_dates`` interface cannot drive the
    incremental check; an empty result then delegates the skip/rebuild
    decision to the structural signature checks.
    """
    latest_dates_fn = getattr(provider, "latest_daily_dates", None)
    if not callable(latest_dates_fn):
        return []
    try:
        current_dates = latest_dates_fn(symbols=symbols) or {}
    except Exception:
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
    return dirty


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
) -> dict[str, object]:
    """Recompute only dirty symbols and merge them into the old parquet."""
    if not dirty:
        # Nothing to recompute; refresh the freshness stamp so age-based
        # expiry (e.g. after a long holiday) does not force a wasted rebuild.
        refreshed = dict(old_manifest.to_payload())
        refreshed["built_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        _atomic_write_json(root / "current.json", refreshed)
        return {
            "ok": True,
            "skipped": False,
            "touched": True,
            "data_snapshot_id": old_manifest.data_snapshot_id,
            "trade_date": latest_date,
            "symbol_count": int(len(old_frame)),
            "dirty_symbols": 0,
            "failed_symbols": 0,
            "root": str(root),
        }

    rows: list[pd.DataFrame] = []
    failed: list[str] = []
    for symbol in dirty:
        try:
            bars = provider.fetch_daily_bars(symbol=symbol, lookback_days=lookback)
        except Exception:
            failed.append(symbol)
            continue
        if bars is None or not isinstance(bars, pd.DataFrame) or bars.empty:
            failed.append(symbol)
            continue
        row = _snapshot_row_for_symbol(bars=bars, symbol=symbol, engineer=engineer)
        if row is not None:
            rows.append(row)
        if callable(on_progress):
            on_progress(len(rows), len(dirty))

    if rows:
        new_rows = pd.concat(rows, ignore_index=True)
        keep = old_frame[~old_frame["symbol"].isin(set(new_rows["symbol"]))]
        merged = pd.concat([keep, new_rows], ignore_index=True).reset_index(drop=True)
    else:
        merged = old_frame.reset_index(drop=True)

    snapshot_id = _new_snapshot_id(latest_date)
    target_dir = root / snapshot_id
    target_dir.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(target_dir / SNAPSHOT_FILENAME, index=False)

    per_symbol = dict(old_manifest.per_symbol)
    per_symbol.update(_per_symbol_entries(merged))

    new_manifest = FeatureSnapshotManifest(
        data_snapshot_id=snapshot_id,
        trade_date=latest_date,
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
    )
    _atomic_write_json(root / "current.json", new_manifest.to_payload())
    _prune_old_snapshots(root, keep=2)

    return {
        "ok": True,
        "skipped": False,
        "data_snapshot_id": snapshot_id,
        "trade_date": latest_date,
        "symbol_count": int(len(merged)),
        "incremental": True,
        "dirty_symbols": len(dirty),
        "failed_symbols": len(failed),
        "root": str(root),
    }


def _per_symbol_entries(frame: pd.DataFrame) -> dict[str, dict[str, str]]:
    """Build the per-symbol incremental bookkeeping map from a snapshot frame."""
    entries: dict[str, dict[str, str]] = {}
    for _, row in frame.iterrows():
        symbol = str(row.get("symbol", "")).strip()
        if not symbol:
            continue
        entries[symbol] = {
            "latest_date": str(row.get("trade_date", "")),
            "fingerprint": _row_fingerprint(row),
        }
    return entries


def _row_fingerprint(row: pd.Series) -> str:
    """Content fingerprint of one snapshot row's latest bar (close/flow/volume)."""
    parts: list[str] = []
    for column in ("latest_close", "ret20", "volume_5d", "avg_turnover_20"):
        value = row.get(column)
        try:
            numeric = float(value)
            if pd.isna(numeric):
                numeric = 0.0
        except (TypeError, ValueError):
            numeric = 0.0
        parts.append(f"{numeric:.4f}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _factor_archive_hash(config: StockAnalyzerConfig) -> str:
    """Fingerprint of the vendor factor ZIPs (qfq/hfq), or "" when absent."""
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
                stat = archive.stat()
                key = f"{archive.name}:{stat.st_size}"
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            parts.append(f"{key}:{stat.st_mtime_ns}")
    if not parts:
        return ""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


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
        item for item in root.iterdir() if item.is_dir() and item.name.startswith("snap_")
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
