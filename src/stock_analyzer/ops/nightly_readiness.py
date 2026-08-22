"""Nightly readiness gate (PLAN Section 4).

The NAS ``stock_updater.sh`` must write one atomic JSON file
``artifacts/runtime/nightly_data_ready.json`` after the daily K + index +
delta steps all succeed.  The evolution scheduler then treats missing or
date-mismatched readiness as a hard scheduler failure
(``_scheduler_ran=true, _scheduler_success=false,
_scheduler_detail=nightly_data_not_ready``).

The file is consumed exactly once by a successful evolution/week5/final-
selector/watchlist-sync chain; on success it is atomically renamed to
``nightly_data_ready.consumed.json``.  On failure it is kept so the
scheduler backs off and retries.

The implementation deliberately avoids importing service internals so the
updater, the scheduler and tests can all call the same helpers.

Schema
------
``nightly_data_ready.json`` example ::

    {
        "schema_version": 2,
        "target_trade_date": "2026-08-19",
        "daily":   {"ok": true, "latest_trade_date": "2026-08-19"},
        "index":   {"ok": true, "symbols_on_target_date": 5541},
        "delta":   {"ok": true, "symbols_on_target_date": 5541},
        "created_at": "2026-08-19T19:48:12+08:00",
        "updater_commit": "abc1234",
        "source": "stock_updater.sh"
    }

Only the fields inspected by the gate are ``schema_version``,
``target_trade_date``, ``daily``/``index``/``delta`` and
``created_at``.  ``target_trade_date`` is the latest daily index date,
not the shell calendar date.

Consumption
-----------
``consume_nightly_readiness`` is called by the scheduler after a
successful full scan.  It renames ``nightly_data_ready.json`` to
``nightly_data_ready.consumed.json`` atomically (``os.replace``) and
returns the payload it consumed.  A missing file before consumption is
not an error; the caller decides the fate of the schedule.

Location
--------
The host actual location is the named volume source of
``/app/artifacts`` from ``docker-compose.runtime.yml``.  At runtime the
container sees it as ``/app/artifacts/runtime/nightly_data_ready.json``.
The helper ``nightly_readiness_paths`` resolves the candidate locations
so local tests using ``artifacts/runtime`` keep working.

Single authoritative path
-------------------------
``authoritative_readiness_path()`` is the single write target.  Legacy
mirrors under ``src/artifacts/runtime`` are never written; they are only
read as fallback for backwards-compat when the authoritative file is
absent, and ``consume`` drains all mirrors.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

READINESS_FILENAME = "nightly_data_ready.json"
CONSUMED_FILENAME = "nightly_data_ready.consumed.json"
READINESS_SCHEMA_VERSION = 2

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ReadinessGate:
    """Result of :func:`check_nightly_readiness`."""

    ready: bool
    reason: str
    payload: dict[str, Any]
    expected_trade_date: str

    def scheduler_triple(self) -> tuple[bool, bool, str]:
        """Return (ran, success, detail) for the scheduler contract."""
        if self.ready:
            return True, True, "ok"
        return True, False, self.reason  # ran=true, success=false


def _coerce_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def authoritative_readiness_path() -> Path:
    """Return the single authoritative path for the readiness file.

    Priority:
    1. ``SA__NIGHTLY_READINESS_PATH`` env var (explicit override, e.g. tests).
    2. ``/app/artifacts/runtime/nightly_data_ready.json`` when ``/app/artifacts``
       exists (container with named volume).
    3. ``<cwd>/artifacts/runtime/nightly_data_ready.json`` otherwise.
    """
    env_path = str(os.environ.get("SA__NIGHTLY_READINESS_PATH", "") or "").strip()
    if env_path:
        return Path(env_path)
    if Path("/app/artifacts").exists():
        return Path("/app/artifacts/runtime") / READINESS_FILENAME
    return Path.cwd() / "artifacts" / "runtime" / READINESS_FILENAME


def _candidate_readiness_paths() -> list[Path]:
    """All locations that may hold the readiness file, newest first.

    Includes legacy ``src/artifacts/runtime`` mirror for backwards-compat
    reads only — writes never target it.
    """
    candidates: list[Path] = []
    # Authoritative first.
    auth = authoritative_readiness_path()
    candidates.append(auth)
    # Legacy: repo-root relative and src/artifacts (read fallback only).
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "artifacts" / "runtime" / READINESS_FILENAME
        if candidate not in candidates:
            candidates.append(candidate)
        # Explicit src/artifacts mirror (baked image artifact).
        src_candidate = parent / "src" / "artifacts" / "runtime" / READINESS_FILENAME
        if src_candidate not in candidates:
            candidates.append(src_candidate)
    # CWD relative fallback.
    cwd_candidate = Path.cwd() / "artifacts" / "runtime" / READINESS_FILENAME
    if cwd_candidate not in candidates:
        candidates.append(cwd_candidate)
    # De-duplicate while preserving order (resolved path when file exists).
    seen: set[str] = set()
    unique: list[Path] = []
    for item in candidates:
        key = str(item.resolve()) if item.exists() else str(item)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def nightly_readiness_paths() -> list[Path]:
    return list(_candidate_readiness_paths())


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _expected_trade_date_from_payload(
    payload: dict[str, Any] | None,
    fallback: str = "",
) -> date | None:
    if payload is None:
        return _coerce_date(fallback)
    # Readiness itself may carry expected date
    expected = _coerce_date(payload.get("target_trade_date"))
    if expected is not None:
        return expected
    return _coerce_date(fallback)


def _required_artifact_path(value: str | Path | None, *, label: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required for nightly readiness")
    path = Path(text).expanduser()
    if not path.is_file():
        raise ValueError(f"{label} does not exist: {path}")
    return path


def _validate_daily_index(
    *,
    index_path: str | Path | None,
    target_trade_date: date,
) -> dict[str, Any]:
    path = _required_artifact_path(index_path, label="index_path")
    payload = _read_json(path)
    if payload is None:
        raise ValueError(f"index_path is not valid JSON: {path}")
    symbols = payload.get("symbols")
    if not isinstance(symbols, dict) or not symbols:
        raise ValueError(f"index_path has no symbols: {path}")

    latest_dates: list[date] = []
    for item in symbols.values():
        if not isinstance(item, dict):
            continue
        parsed = _coerce_date(item.get("latest_date"))
        if parsed is not None:
            latest_dates.append(parsed)
    if not latest_dates:
        raise ValueError(f"index_path has no latest_date values: {path}")

    index_latest = max(latest_dates)
    symbols_on_target = sum(1 for item in latest_dates if item == target_trade_date)
    if index_latest != target_trade_date:
        raise ValueError(
            "daily index latest date mismatch: "
            f"expected {target_trade_date.isoformat()}, got {index_latest.isoformat()}"
        )
    if symbols_on_target <= 0:
        raise ValueError(f"daily index has no symbols on {target_trade_date.isoformat()}")
    return {
        "ok": True,
        "path": str(path),
        "latest_trade_date": index_latest.isoformat(),
        "symbols_total": len(symbols),
        "symbols_on_target_date": symbols_on_target,
    }


def _validate_delta_db(
    *,
    db_path: str | Path | None,
    target_trade_date: date,
    expected_symbols_on_target: int,
) -> dict[str, Any]:
    path = _required_artifact_path(db_path, label="delta_db_path")
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - production dependency
        raise RuntimeError("duckdb is required to validate nightly readiness") from exc

    try:
        with duckdb.connect(str(path), read_only=True) as connection:
            table_exists = connection.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_name = 'daily_bars'
                """
            ).fetchone()
            if not table_exists or int(table_exists[0] or 0) <= 0:
                raise ValueError(f"delta DB has no daily_bars table: {path}")
            row = connection.execute(
                """
                SELECT
                    MAX(date),
                    COUNT(DISTINCT symbol),
                    COUNT(DISTINCT CASE WHEN date = ? THEN symbol END)
                FROM daily_bars
                """,
                [target_trade_date.isoformat()],
            ).fetchone()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"cannot validate delta DB {path}: {type(exc).__name__}:{exc}") from exc

    delta_latest = _coerce_date(row[0] if row else None)
    symbols_total = int(row[1] or 0) if row else 0
    symbols_on_target = int(row[2] or 0) if row else 0
    if delta_latest != target_trade_date:
        actual = delta_latest.isoformat() if delta_latest is not None else ""
        raise ValueError(
            "delta DB latest date mismatch: "
            f"expected {target_trade_date.isoformat()}, got {actual or 'missing'}"
        )
    if symbols_on_target < expected_symbols_on_target:
        raise ValueError(
            "delta DB target-date coverage is incomplete: "
            f"{symbols_on_target}<{expected_symbols_on_target}"
        )
    coverage_ratio = (
        round(symbols_on_target / expected_symbols_on_target, 6)
        if expected_symbols_on_target > 0
        else 0.0
    )
    return {
        "ok": True,
        "path": str(path),
        "latest_trade_date": delta_latest.isoformat(),
        "symbols_total": symbols_total,
        "symbols_on_target_date": symbols_on_target,
        "expected_symbols_on_target_date": expected_symbols_on_target,
        "coverage_ratio": coverage_ratio,
    }


def read_nightly_readiness(path: str | Path | None = None) -> dict[str, Any] | None:
    """Return the readiness JSON payload, or ``None`` when absent / unreadable."""
    if path is not None:
        return _read_json(Path(path))
    auth = authoritative_readiness_path()
    payload = _read_json(auth)
    if payload is not None:
        return payload
    # Fallback: legacy mirrors (warn so they get cleaned up).
    for candidate in _candidate_readiness_paths():
        if candidate == auth:
            continue
        candidate_payload = _read_json(candidate)
        if candidate_payload is not None:
            logger.warning(
                "readiness read from legacy mirror %s (authoritative %s missing)",
                candidate,
                auth,
            )
            return candidate_payload
    return None


def write_nightly_readiness(
    *,
    target_trade_date: date | str,
    db_path: str | Path | None = None,
    index_path: str | Path | None = None,
    updater_commit: str = "",
    extra: dict[str, Any] | None = None,
    path: str | Path | None = None,
) -> Path:
    """Atomically write ``nightly_data_ready.json`` to the authoritative path.

    Args:
        target_trade_date: latest daily index date (not shell calendar date).
        db_path / index_path: required artifacts. Both are opened and checked
            against `target_trade_date` before readiness is published.
        updater_commit: the updater git commit (from ``.build_commit``).
        extra: additional keys merged into the payload.
        path: override output path; when omitted the authoritative path is used.

    Returns:
        The path that was written.
    """
    coerced = _coerce_date(target_trade_date)
    if coerced is None:
        raise ValueError(f"invalid target_trade_date: {target_trade_date!r}")
    index_validation = _validate_daily_index(
        index_path=index_path,
        target_trade_date=coerced,
    )
    delta_validation = _validate_delta_db(
        db_path=db_path,
        target_trade_date=coerced,
        expected_symbols_on_target=int(index_validation["symbols_on_target_date"]),
    )
    target = Path(path) if path is not None else authoritative_readiness_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema_version": READINESS_SCHEMA_VERSION,
        "target_trade_date": coerced.isoformat(),
        "daily": {
            "ok": True,
            "latest_trade_date": coerced.isoformat(),
            "symbols_on_target_date": index_validation["symbols_on_target_date"],
        },
        "index": index_validation,
        "delta": delta_validation,
        "created_at": datetime.now(UTC).isoformat(),
        "updater_commit": str(updater_commit or "").strip(),
        "source": "stock_updater.sh",
        "delta_db_path": str(db_path),
        "index_path": str(index_path),
    }
    if extra:
        reserved = {
            "schema_version",
            "target_trade_date",
            "daily",
            "index",
            "delta",
            "created_at",
            "delta_db_path",
            "index_path",
        }
        payload.update({key: value for key, value in extra.items() if key not in reserved})
    tmp = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2, sort_keys=True)
        fp.write("\n")
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, target)
    return target


def check_nightly_readiness(
    *,
    expected_trade_date: str | date | datetime | None = None,
    path: str | Path | None = None,
) -> ReadinessGate:
    """Evaluate the nightly readiness gate.

    When ``expected_trade_date`` is omitted it is derived from the readiness
    payload's ``target_trade_date`` (i.e. the gate checks internal
    consistency only).  Callers that know the true expected date (from the
    daily index's latest date) should pass it.

    Returns a :class:`ReadinessGate` whose ``scheduler_triple`` satisfies the
    scheduler contract: missing or date-mismatched readiness yields
    ``(ran=true, success=false, detail=nightly_data_not_ready)``.
    """
    payload = read_nightly_readiness(path=path)
    if payload is None:
        expected_s = str(expected_trade_date or "").strip()
        return ReadinessGate(
            ready=False,
            reason="nightly_data_not_ready",
            payload={},
            expected_trade_date=expected_s,
        )
    schema_version = payload.get("schema_version")
    try:
        version = int(schema_version)  # type: ignore[arg-type]
    except Exception:
        version = -1
    if version != READINESS_SCHEMA_VERSION:
        return ReadinessGate(
            ready=False,
            reason="nightly_data_not_ready",
            payload=payload,
            expected_trade_date=str(expected_trade_date or payload.get("target_trade_date", "")),
        )
    # Require daily/index/delta success.
    for key in ("daily", "index", "delta"):
        slot = payload.get(key)
        if not isinstance(slot, dict) or not bool(slot.get("ok", False)):
            return ReadinessGate(
                ready=False,
                reason="nightly_data_not_ready",
                payload=payload,
                expected_trade_date=str(
                    expected_trade_date or payload.get("target_trade_date", "")
                ),
            )
    readiness_date = _coerce_date(payload.get("target_trade_date"))
    if readiness_date is None:
        return ReadinessGate(
            ready=False,
            reason="nightly_data_not_ready",
            payload=payload,
            expected_trade_date=str(expected_trade_date or ""),
        )
    expected_date = (
        _coerce_date(expected_trade_date)
        if expected_trade_date not in (None, "")
        else readiness_date
    )
    if expected_date is None:
        expected_date = readiness_date
    if readiness_date != expected_date:
        return ReadinessGate(
            ready=False,
            reason="nightly_data_not_ready",
            payload=payload,
            expected_trade_date=expected_date.isoformat(),
        )
    return ReadinessGate(
        ready=True,
        reason="ok",
        payload=payload,
        expected_trade_date=readiness_date.isoformat(),
    )


def invalidate_nightly_readiness(
    *,
    stamp: str | None = None,
) -> list[Path]:
    """Atomically retire every readable readiness file as ``*.stale-*``.

    A vendor update run must invalidate the previous night's readiness
    BEFORE touching any data: if the update then fails, no stale readiness
    may survive for the off-hours selector to consume (fail-closed).
    ``read_nightly_readiness`` falls back to legacy mirrors, so ALL
    candidate locations are drained here, not just the authoritative one.

    Consumed files are not touched; stale files keep their payload and
    mtime for post-mortem auditing and are never restored automatically.

    Returns the source paths that were invalidated (they no longer exist).
    """
    marker = stamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    invalidated: list[Path] = []
    failures: list[str] = []
    for candidate in _candidate_readiness_paths():
        if _read_json(candidate) is None:
            continue
        # 中缀命名与 consumed 文件（nightly_data_ready.consumed.json）一致：
        # 前缀固定，按文件名排序即按失效时间排序。
        target = candidate.with_name(
            f"nightly_data_ready.stale-{marker}.json"
        )
        suffix = 1
        while target.exists():
            target = candidate.with_name(
                f"nightly_data_ready.stale-{marker}.{suffix}.json"
            )
            suffix += 1
        try:
            os.replace(candidate, target)
        except FileNotFoundError:
            continue
        except OSError as exc:
            failures.append(f"{candidate}:{type(exc).__name__}:{exc}")
            continue
        invalidated.append(candidate)
    if failures:
        raise OSError(
            "failed to invalidate one or more nightly readiness files: "
            + " | ".join(failures)
        )
    return invalidated


def consume_nightly_readiness(
    *,
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Atomically rename the readiness file(s) to the consumed name.

    When ``path`` is given, only that file is consumed.  Otherwise all
    candidate locations are drained so a stale mirror cannot be re-read
    after the authoritative file is consumed.
    """
    if path is not None:
        source = Path(path)
        payload = _read_json(source)
        if payload is None:
            return None
        target = source.with_name(CONSUMED_FILENAME)
        try:
            os.replace(source, target)
        except OSError:
            return None
        return payload
    # Drain all candidates; return the first payload found.
    first_payload: dict[str, Any] | None = None
    for candidate in _candidate_readiness_paths():
        payload = _read_json(candidate)
        if payload is None:
            continue
        if first_payload is None:
            first_payload = payload
        target = candidate.with_name(CONSUMED_FILENAME)
        # Avoid overwriting an existing consumed file from another candidate
        # with different content — keep the first consumed payload's file.
        if target.exists():
            try:
                candidate.unlink()
            except OSError:
                pass
            continue
        try:
            os.replace(candidate, target)
        except OSError:
            continue
    return first_payload
