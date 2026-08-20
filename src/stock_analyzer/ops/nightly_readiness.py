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
        "schema_version": 1,
        "target_trade_date": "2026-08-19",
        "daily":   {"ok": true},
        "index":   {"ok": true},
        "delta":   {"ok": true},
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
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

READINESS_FILENAME = "nightly_data_ready.json"
CONSUMED_FILENAME = "nightly_data_ready.consumed.json"
READINESS_SCHEMA_VERSION = 1


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


def _candidate_readiness_paths() -> list[Path]:
    """All locations that may hold the readiness file, newest first."""
    candidates: list[Path] = []
    # Container path (authoritative at runtime).
    candidates.append(Path("/app/artifacts/runtime") / READINESS_FILENAME)
    # Repo-root relative (tests / local dev).
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "artifacts" / "runtime" / READINESS_FILENAME
        if candidate not in candidates:
            candidates.append(candidate)
    # CWD relative fallback.
    cwd_candidate = Path.cwd() / "artifacts" / "runtime" / READINESS_FILENAME
    if cwd_candidate not in candidates:
        candidates.append(cwd_candidate)
    return candidates


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


def read_nightly_readiness(path: str | Path | None = None) -> dict[str, Any] | None:
    """Return the readiness JSON payload, or ``None`` when absent / unreadable."""
    if path is not None:
        return _read_json(Path(path))
    for candidate in _candidate_readiness_paths():
        payload = _read_json(candidate)
        if payload is not None:
            return payload
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
    """Atomically write ``nightly_data_ready.json``.

    Args:
        target_trade_date: latest daily index date (not shell calendar date).
        db_path / index_path: informational only, recorded in payload.
        updater_commit: the updater git commit (from ``.build_commit``).
        extra: additional keys merged into the payload.
        path: override output path; when omitted the primary candidate is used.

    Returns:
        The path that was written.
    """
    coerced = _coerce_date(target_trade_date)
    if coerced is None:
        raise ValueError(f"invalid target_trade_date: {target_trade_date!r}")
    # Default write target must be the shared artifacts mount, not the repo's
    # src/stock_analyzer/ops/artifacts/... (old bug: _candidate_readiness_paths()[1]
    # resolved to src/stock_analyzer/ops/artifacts/... when here=.../ops).
    if path is not None:
        target = Path(path)
    else:
        # Prefer the CWD-anchored repo artifacts (e.g. /app/artifacts/runtime or
        # /repo/artifacts/runtime), falling back to /app/... when CWD is not
        # the repo root.
        repo_artifacts = (Path.cwd() / "artifacts" / "runtime" / READINESS_FILENAME).resolve()
        if repo_artifacts.parent.exists() or Path("/app/artifacts").exists():
            candidates = _candidate_readiness_paths()
            # Choose the first candidate whose parent exists or is /app/...
            target = next(
                (c for c in candidates if c.parent.exists() or str(c).startswith("/app/")),
                candidates[1] if len(candidates) > 1 else candidates[0],
            )
        else:
            target = _candidate_readiness_paths()[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema_version": READINESS_SCHEMA_VERSION,
        "target_trade_date": coerced.isoformat(),
        "daily": {"ok": True},
        "index": {"ok": True},
        "delta": {"ok": True},
        "created_at": datetime.now(UTC).isoformat(),
        "updater_commit": str(updater_commit or "").strip(),
        "source": "stock_updater.sh",
    }
    if db_path is not None:
        payload["delta_db_path"] = str(db_path)
    if index_path is not None:
        payload["index_path"] = str(index_path)
    if extra:
        payload.update(extra)
    tmp = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2, sort_keys=True)
        fp.write("\n")
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, target)
    # Mirror into sibling candidates only when they are discoverable/exist.
    # The /app/artifacts path is authoritive only inside the container; on
    # local dev or CI it may be absent or read-only — avoid spurious mkdir
    # there (old code unconditionally created parent dirs).
    for mirror in _candidate_readiness_paths():
        if mirror == target or mirror.exists():
            continue
        # Only mirror when the candidate's ancestor exists (i.e. the
        # artifacts mount/volume is actually present).  This avoids
        # mkdir(/app) on systems where that path is not expected.
        if not mirror.parent.parent.exists():
            continue
        try:
            mirror.parent.mkdir(parents=True, exist_ok=True)
            mirror.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
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
                expected_trade_date=str(expected_trade_date or payload.get("target_trade_date", "")),
            )
    readiness_date = _coerce_date(payload.get("target_trade_date"))
    if readiness_date is None:
        return ReadinessGate(
            ready=False,
            reason="nightly_data_not_ready",
            payload=payload,
            expected_trade_date=str(expected_trade_date or ""),
        )
    expected_date = _coerce_date(expected_trade_date) if expected_trade_date not in (None, "") else readiness_date
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


def consume_nightly_readiness(
    *,
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Atomically rename the readiness file to the consumed name.

    Returns the consumed payload, or ``None`` when no readiness file exists.
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
    # Walk candidates, consuming the first that exists.
    for candidate in _candidate_readiness_paths():
        payload = _read_json(candidate)
        if payload is None:
            continue
        target = candidate.with_name(CONSUMED_FILENAME)
        try:
            os.replace(candidate, target)
        except OSError:
            continue
        return payload
    return None
