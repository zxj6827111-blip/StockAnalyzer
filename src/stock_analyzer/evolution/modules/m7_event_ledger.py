"""DuckDB-backed M7 event ledger with dedup, TTL, archive, and effectiveness stats."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast


@dataclass(frozen=True, slots=True)
class M7EventLedgerIngestSummary:
    """Write-path summary for one ledger update."""

    inserted: int
    deduplicated: int
    archived: int
    refreshed_effective_events: int

    def to_dict(self) -> dict[str, object]:
        return {
            "inserted": self.inserted,
            "deduplicated": self.deduplicated,
            "archived": self.archived,
            "refreshed_effective_events": self.refreshed_effective_events,
        }


@dataclass(frozen=True, slots=True)
class M7EventLedgerEffectivenessSummary:
    """Historical effectiveness summary exposed to the orchestrator."""

    active_events: int
    archived_events: int
    duplicate_hits: int
    dedup_ratio: float
    matured_1d: int
    hit_rate_1d: float | None
    matured_3d: int
    hit_rate_3d: float | None
    matured_5d: int
    hit_rate_5d: float | None
    average_effectiveness: float | None
    source_reliability: list[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return {
            "active_events": self.active_events,
            "archived_events": self.archived_events,
            "duplicate_hits": self.duplicate_hits,
            "dedup_ratio": self.dedup_ratio,
            "matured_1d": self.matured_1d,
            "hit_rate_1d": self.hit_rate_1d,
            "matured_3d": self.matured_3d,
            "hit_rate_3d": self.hit_rate_3d,
            "matured_5d": self.matured_5d,
            "hit_rate_5d": self.hit_rate_5d,
            "average_effectiveness": self.average_effectiveness,
            "source_reliability": [dict(item) for item in self.source_reliability],
        }


@dataclass(frozen=True, slots=True)
class M7EventLedgerRunReport:
    """Complete ledger report for one orchestrator run."""

    ingest: M7EventLedgerIngestSummary
    effectiveness: M7EventLedgerEffectivenessSummary
    db_path: str
    archive_dir: str
    archive_paths: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "ingest": self.ingest.to_dict(),
            "effectiveness": self.effectiveness.to_dict(),
            "paths": {
                "db_path": self.db_path,
                "archive_dir": self.archive_dir,
                "archive_paths": list(self.archive_paths),
            },
        }


class _DuckCursor(Protocol):
    def fetchone(self) -> Sequence[object] | None: ...

    def fetchall(self) -> list[Sequence[object]]: ...


class _DuckConnection(Protocol):
    def execute(
        self,
        query: str,
        parameters: Sequence[object] | None = None,
    ) -> _DuckCursor: ...

    def close(self) -> None: ...


_SELECT_COLUMNS = [
    "ledger_id",
    "event_id",
    "dedup_key",
    "symbol",
    "headline",
    "source",
    "provider",
    "url",
    "published_at",
    "first_seen_at",
    "last_seen_at",
    "expires_at",
    "status",
    "archived_at",
    "source_trace_id",
    "regime_state",
    "sentiment",
    "llm_verdict",
    "llm_confidence",
    "reference_price",
    "latest_price",
    "occurrence_count",
    "duplicate_hits",
    "return_1d",
    "return_3d",
    "return_5d",
    "effective_1d",
    "effective_3d",
    "effective_5d",
    "effectiveness_score",
    "last_effectiveness_update_at",
]

_HORIZON_COLUMNS = (
    (24.0, "return_1d", "effective_1d"),
    (72.0, "return_3d", "effective_3d"),
    (120.0, "return_5d", "effective_5d"),
)


class M7EventLedger:
    """Persist M7 news events with dedup, TTL archiving, and lagged effectiveness."""

    def __init__(
        self,
        *,
        db_path: str | Path,
        archive_dir: str | Path,
        ttl_days: int = 14,
        connection_factory: Callable[[str], _DuckConnection] | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._archive_dir = Path(archive_dir)
        self._ttl_days = max(1, int(ttl_days))
        self._connection_factory = connection_factory or _default_connection_factory
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._archive_dir.mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def archive_dir(self) -> Path:
        return self._archive_dir

    def record_run(
        self,
        *,
        records: Sequence[Mapping[str, object]],
        now: datetime,
        price_by_symbol: Mapping[str, float],
        source_trace_id: str = "",
        regime_state: str = "",
    ) -> M7EventLedgerRunReport:
        """Update the ledger for one orchestrator run and return a summary report."""

        observed_at = _normalize_datetime(now)
        normalized_prices = {
            str(symbol).strip().upper(): float(price)
            for symbol, price in price_by_symbol.items()
            if str(symbol).strip() and float(price) > 0.0
        }
        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            refreshed = self._refresh_effectiveness(
                conn=conn,
                now=observed_at,
                price_by_symbol=normalized_prices,
            )
            archived, archive_paths = self._archive_expired(conn=conn, now=observed_at)
            inserted = 0
            deduplicated = 0
            for record in records:
                candidate = _normalize_candidate(
                    record=record,
                    observed_at=observed_at,
                    ttl_days=self._ttl_days,
                    price_by_symbol=normalized_prices,
                    source_trace_id=source_trace_id,
                    regime_state=regime_state,
                )
                if candidate is None:
                    continue
                existing = self._select_active_by_dedup_key(
                    conn=conn,
                    dedup_key=str(candidate["dedup_key"]),
                )
                if existing is None:
                    conn.execute(
                        (
                            "INSERT INTO m7_event_ledger ("
                            f"{', '.join(_SELECT_COLUMNS)}"
                            ") VALUES ("
                            + ", ".join("?" for _ in _SELECT_COLUMNS)
                            + ")"
                        ),
                        [candidate[column] for column in _SELECT_COLUMNS],
                    )
                    inserted += 1
                    continue
                updated = _merge_duplicate_record(existing=existing, candidate=candidate)
                conn.execute(
                    (
                        "UPDATE m7_event_ledger SET "
                        "last_seen_at = ?, "
                        "source = ?, "
                        "provider = ?, "
                        "url = ?, "
                        "published_at = ?, "
                        "source_trace_id = ?, "
                        "regime_state = ?, "
                        "sentiment = ?, "
                        "llm_verdict = ?, "
                        "llm_confidence = ?, "
                        "reference_price = ?, "
                        "latest_price = ?, "
                        "occurrence_count = ?, "
                        "duplicate_hits = ? "
                        "WHERE ledger_id = ?"
                    ),
                    [
                        updated["last_seen_at"],
                        updated["source"],
                        updated["provider"],
                        updated["url"],
                        updated["published_at"],
                        updated["source_trace_id"],
                        updated["regime_state"],
                        updated["sentiment"],
                        updated["llm_verdict"],
                        updated["llm_confidence"],
                        updated["reference_price"],
                        updated["latest_price"],
                        updated["occurrence_count"],
                        updated["duplicate_hits"],
                        updated["ledger_id"],
                    ],
                )
                deduplicated += 1
            refreshed += self._refresh_effectiveness(
                conn=conn,
                now=observed_at,
                price_by_symbol=normalized_prices,
            )
            summary = self._build_effectiveness_summary(conn=conn)
        finally:
            conn.close()
        return M7EventLedgerRunReport(
            ingest=M7EventLedgerIngestSummary(
                inserted=inserted,
                deduplicated=deduplicated,
                archived=archived,
                refreshed_effective_events=refreshed,
            ),
            effectiveness=summary,
            db_path=str(self._db_path),
            archive_dir=str(self._archive_dir),
            archive_paths=[str(path) for path in archive_paths],
        )

    def list_records(self, *, status: str | None = None) -> list[dict[str, object]]:
        """Return ledger rows for tests and inspection."""

        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            if status is None:
                rows = conn.execute(
                    f"SELECT {', '.join(_SELECT_COLUMNS)} "
                    "FROM m7_event_ledger ORDER BY first_seen_at, ledger_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {', '.join(_SELECT_COLUMNS)} "
                    "FROM m7_event_ledger WHERE status = ? "
                    "ORDER BY first_seen_at, ledger_id",
                    [status],
                ).fetchall()
            return [_row_to_record(row) for row in rows]
        finally:
            conn.close()

    def _ensure_table(self, *, conn: _DuckConnection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS m7_event_ledger ("
            "ledger_id VARCHAR PRIMARY KEY, "
            "event_id VARCHAR NOT NULL, "
            "dedup_key VARCHAR NOT NULL, "
            "symbol VARCHAR NOT NULL, "
            "headline VARCHAR NOT NULL, "
            "source VARCHAR NOT NULL, "
            "provider VARCHAR NOT NULL, "
            "url VARCHAR NOT NULL, "
            "published_at VARCHAR, "
            "first_seen_at VARCHAR NOT NULL, "
            "last_seen_at VARCHAR NOT NULL, "
            "expires_at VARCHAR NOT NULL, "
            "status VARCHAR NOT NULL, "
            "archived_at VARCHAR, "
            "source_trace_id VARCHAR NOT NULL, "
            "regime_state VARCHAR NOT NULL, "
            "sentiment DOUBLE NOT NULL, "
            "llm_verdict VARCHAR NOT NULL, "
            "llm_confidence DOUBLE NOT NULL, "
            "reference_price DOUBLE, "
            "latest_price DOUBLE, "
            "occurrence_count INTEGER NOT NULL, "
            "duplicate_hits INTEGER NOT NULL, "
            "return_1d DOUBLE, "
            "return_3d DOUBLE, "
            "return_5d DOUBLE, "
            "effective_1d BOOLEAN, "
            "effective_3d BOOLEAN, "
            "effective_5d BOOLEAN, "
            "effectiveness_score DOUBLE, "
            "last_effectiveness_update_at VARCHAR"
            ")"
        )

    def _select_active_by_dedup_key(
        self,
        *,
        conn: _DuckConnection,
        dedup_key: str,
    ) -> dict[str, object] | None:
        row = conn.execute(
            f"SELECT {', '.join(_SELECT_COLUMNS)} "
            "FROM m7_event_ledger WHERE status = 'active' AND dedup_key = ? LIMIT 1",
            [dedup_key],
        ).fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    def _archive_expired(
        self,
        *,
        conn: _DuckConnection,
        now: datetime,
    ) -> tuple[int, list[Path]]:
        now_iso = now.isoformat()
        rows = conn.execute(
            f"SELECT {', '.join(_SELECT_COLUMNS)} "
            "FROM m7_event_ledger WHERE status = 'active' AND expires_at <= ? "
            "ORDER BY expires_at, ledger_id",
            [now_iso],
        ).fetchall()
        if not rows:
            return 0, []
        archive_path = self._archive_dir / f"m7_event_ledger_{now.strftime('%Y%m%d')}.jsonl"
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with archive_path.open("a", encoding="utf-8") as fp:
            for row in rows:
                record = _row_to_record(row)
                record["status"] = "archived"
                record["archived_at"] = now_iso
                fp.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        conn.execute(
            "UPDATE m7_event_ledger SET status = 'archived', archived_at = ? "
            "WHERE status = 'active' AND expires_at <= ?",
            [now_iso, now_iso],
        )
        return len(rows), [archive_path]

    def _refresh_effectiveness(
        self,
        *,
        conn: _DuckConnection,
        now: datetime,
        price_by_symbol: Mapping[str, float],
    ) -> int:
        if not price_by_symbol:
            return 0
        rows = conn.execute(
            f"SELECT {', '.join(_SELECT_COLUMNS)} "
            "FROM m7_event_ledger WHERE status = 'active' ORDER BY first_seen_at, ledger_id"
        ).fetchall()
        updated_rows = 0
        for row in rows:
            record = _row_to_record(row)
            symbol = str(record["symbol"]).strip().upper()
            current_price = price_by_symbol.get(symbol)
            reference_price = _as_positive_float(record.get("reference_price"))
            if current_price is None or current_price <= 0.0 or reference_price is None:
                continue
            first_seen_at = _parse_datetime(record.get("first_seen_at"))
            if first_seen_at is None:
                continue
            age_hours = max(0.0, (now - first_seen_at).total_seconds() / 3600.0)
            expected_direction = _expected_direction(
                sentiment=_as_float(record.get("sentiment"), default=0.0),
                llm_verdict=str(record.get("llm_verdict", "")).strip(),
            )
            if expected_direction == 0:
                continue
            updates: dict[str, object] = {"latest_price": float(current_price)}
            changed = False
            for horizon_hours, return_key, effective_key in _HORIZON_COLUMNS:
                if age_hours + 1e-9 < horizon_hours:
                    continue
                if record.get(return_key) is not None or record.get(effective_key) is not None:
                    continue
                realized_return = float(current_price / reference_price - 1.0)
                effective = realized_return * float(expected_direction) >= 0.0
                updates[return_key] = realized_return
                updates[effective_key] = effective
                changed = True
            if not changed:
                continue
            effectiveness_values = [
                _bool_to_float(updates.get("effective_1d", record.get("effective_1d"))),
                _bool_to_float(updates.get("effective_3d", record.get("effective_3d"))),
                _bool_to_float(updates.get("effective_5d", record.get("effective_5d"))),
            ]
            available_scores = [value for value in effectiveness_values if value is not None]
            updates["effectiveness_score"] = (
                float(sum(available_scores) / len(available_scores)) if available_scores else None
            )
            updates["last_effectiveness_update_at"] = now.isoformat()
            conn.execute(
                (
                    "UPDATE m7_event_ledger SET "
                    "latest_price = ?, "
                    "return_1d = ?, "
                    "return_3d = ?, "
                    "return_5d = ?, "
                    "effective_1d = ?, "
                    "effective_3d = ?, "
                    "effective_5d = ?, "
                    "effectiveness_score = ?, "
                    "last_effectiveness_update_at = ? "
                    "WHERE ledger_id = ?"
                ),
                [
                    updates.get("latest_price"),
                    updates.get("return_1d", record.get("return_1d")),
                    updates.get("return_3d", record.get("return_3d")),
                    updates.get("return_5d", record.get("return_5d")),
                    updates.get("effective_1d", record.get("effective_1d")),
                    updates.get("effective_3d", record.get("effective_3d")),
                    updates.get("effective_5d", record.get("effective_5d")),
                    updates.get("effectiveness_score"),
                    updates.get("last_effectiveness_update_at"),
                    record["ledger_id"],
                ],
            )
            updated_rows += 1
        return updated_rows

    def _build_effectiveness_summary(
        self,
        *,
        conn: _DuckConnection,
    ) -> M7EventLedgerEffectivenessSummary:
        rows = conn.execute(
            f"SELECT {', '.join(_SELECT_COLUMNS)} "
            "FROM m7_event_ledger ORDER BY first_seen_at, ledger_id"
        ).fetchall()
        records = [_row_to_record(row) for row in rows]
        active_events = sum(1 for item in records if item.get("status") == "active")
        archived_events = sum(1 for item in records if item.get("status") == "archived")
        duplicate_hits = sum(_as_int(item.get("duplicate_hits"), default=0) for item in records)
        total_occurrences = sum(_as_int(item.get("occurrence_count"), default=0) for item in records)
        dedup_ratio = float(duplicate_hits / max(total_occurrences, 1))
        matured_1d, hit_rate_1d = _hit_rate(records=records, key="effective_1d")
        matured_3d, hit_rate_3d = _hit_rate(records=records, key="effective_3d")
        matured_5d, hit_rate_5d = _hit_rate(records=records, key="effective_5d")
        effectiveness_values = [
            float(item["effectiveness_score"])
            for item in records
            if isinstance(item.get("effectiveness_score"), (int, float))
        ]
        average_effectiveness = (
            float(sum(effectiveness_values) / len(effectiveness_values))
            if effectiveness_values
            else None
        )
        source_reliability = _source_reliability(records=records)
        return M7EventLedgerEffectivenessSummary(
            active_events=active_events,
            archived_events=archived_events,
            duplicate_hits=duplicate_hits,
            dedup_ratio=dedup_ratio,
            matured_1d=matured_1d,
            hit_rate_1d=hit_rate_1d,
            matured_3d=matured_3d,
            hit_rate_3d=hit_rate_3d,
            matured_5d=matured_5d,
            hit_rate_5d=hit_rate_5d,
            average_effectiveness=average_effectiveness,
            source_reliability=source_reliability,
        )


def _normalize_candidate(
    *,
    record: Mapping[str, object],
    observed_at: datetime,
    ttl_days: int,
    price_by_symbol: Mapping[str, float],
    source_trace_id: str,
    regime_state: str,
) -> dict[str, object] | None:
    headline = _first_non_empty_str(
        record,
        keys=("headline", "title", "news_headline", "news_title", "news", "text"),
    )
    symbol = _first_non_empty_str(record, keys=("symbol", "code", "ticker"))
    if headline is None or symbol is None:
        return None
    normalized_symbol = symbol.strip().upper()
    published_at = _parse_datetime(
        _first_non_empty_str(
            record,
            keys=("published_at", "published_time", "timestamp", "time", "date", "发布时间"),
        )
    )
    event_id = _first_non_empty_str(record, keys=("event_id", "news_id", "id"))
    source = _first_non_empty_str(record, keys=("source", "文章来源", "media", "channel")) or ""
    provider = _first_non_empty_str(record, keys=("provider", "source_file")) or ""
    url = _first_non_empty_str(record, keys=("url", "news_url", "link", "新闻链接")) or ""
    sentiment = _clamp(_as_float(record.get("sentiment"), default=0.0), -1.0, 1.0)
    llm_verdict = (
        _first_non_empty_str(record, keys=("llm_verdict", "llm_news_verdict", "verdict"))
        or _default_verdict(sentiment)
    )
    llm_confidence = _clamp(
        _first_float(
            record,
            keys=("llm_confidence", "confidence", "probability", "weight"),
            default=max(0.1, abs(sentiment)),
        ),
        0.0,
        1.0,
    )
    dedup_key = _stable_hash(
        {
            "symbol": normalized_symbol,
            "headline": _canonical_headline(headline),
            "published_bucket": (
                published_at.strftime("%Y-%m-%d")
                if published_at is not None
                else observed_at.strftime("%Y-%m-%d")
            ),
        }
    )[:24]
    reference_price = price_by_symbol.get(normalized_symbol)
    if reference_price is None:
        reference_price = _first_float(record, keys=("close", "price", "last", "open"), default=0.0)
    reference_price = reference_price if reference_price > 0.0 else None
    ledger_id = _stable_hash(
        {"dedup_key": dedup_key, "first_seen_at": observed_at.isoformat()}
    )[:24]
    return {
        "ledger_id": ledger_id,
        "event_id": event_id or _stable_hash(
            {
                "symbol": normalized_symbol,
                "headline": headline,
                "published_at": published_at.isoformat() if published_at is not None else "",
            }
        )[:24],
        "dedup_key": dedup_key,
        "symbol": normalized_symbol,
        "headline": headline.strip(),
        "source": source,
        "provider": provider,
        "url": url,
        "published_at": published_at.isoformat() if published_at is not None else None,
        "first_seen_at": observed_at.isoformat(),
        "last_seen_at": observed_at.isoformat(),
        "expires_at": (observed_at + timedelta(days=ttl_days)).isoformat(),
        "status": "active",
        "archived_at": None,
        "source_trace_id": source_trace_id,
        "regime_state": str(regime_state).strip().lower(),
        "sentiment": float(sentiment),
        "llm_verdict": llm_verdict,
        "llm_confidence": float(llm_confidence),
        "reference_price": reference_price,
        "latest_price": reference_price,
        "occurrence_count": 1,
        "duplicate_hits": 0,
        "return_1d": None,
        "return_3d": None,
        "return_5d": None,
        "effective_1d": None,
        "effective_3d": None,
        "effective_5d": None,
        "effectiveness_score": None,
        "last_effectiveness_update_at": None,
    }


def _merge_duplicate_record(
    *,
    existing: Mapping[str, object],
    candidate: Mapping[str, object],
) -> dict[str, object]:
    existing_occurrences = _as_int(existing.get("occurrence_count"), default=1)
    candidate_sentiment = _as_float(candidate.get("sentiment"), default=0.0)
    existing_sentiment = _as_float(existing.get("sentiment"), default=0.0)
    averaged_sentiment = (
        existing_sentiment * existing_occurrences + candidate_sentiment
    ) / max(existing_occurrences + 1, 1)
    return {
        "ledger_id": existing["ledger_id"],
        "last_seen_at": candidate["last_seen_at"],
        "source": str(existing.get("source", "")).strip() or str(candidate.get("source", "")).strip(),
        "provider": str(existing.get("provider", "")).strip()
        or str(candidate.get("provider", "")).strip(),
        "url": str(existing.get("url", "")).strip() or str(candidate.get("url", "")).strip(),
        "published_at": existing.get("published_at") or candidate.get("published_at"),
        "source_trace_id": str(candidate.get("source_trace_id", "")).strip()
        or str(existing.get("source_trace_id", "")).strip(),
        "regime_state": str(candidate.get("regime_state", "")).strip()
        or str(existing.get("regime_state", "")).strip(),
        "sentiment": averaged_sentiment,
        "llm_verdict": str(existing.get("llm_verdict", "")).strip()
        or str(candidate.get("llm_verdict", "")).strip(),
        "llm_confidence": max(
            _as_float(existing.get("llm_confidence"), default=0.0),
            _as_float(candidate.get("llm_confidence"), default=0.0),
        ),
        "reference_price": existing.get("reference_price") or candidate.get("reference_price"),
        "latest_price": candidate.get("latest_price") or existing.get("latest_price"),
        "occurrence_count": existing_occurrences + 1,
        "duplicate_hits": _as_int(existing.get("duplicate_hits"), default=0) + 1,
    }


def _row_to_record(row: Sequence[object]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for index, column in enumerate(_SELECT_COLUMNS):
        payload[column] = row[index]
    return payload


def _source_reliability(records: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, list[float]] = {}
    for item in records:
        raw_effectiveness = item.get("effectiveness_score")
        if not isinstance(raw_effectiveness, (int, float)):
            continue
        source = str(item.get("source", "")).strip() or str(item.get("provider", "")).strip()
        label = source or "unknown"
        grouped.setdefault(label, []).append(float(raw_effectiveness))
    ranked: list[dict[str, object]] = []
    for source, values in grouped.items():
        ranked.append(
            {
                "source": source,
                "samples": len(values),
                "mean_effectiveness": float(sum(values) / len(values)),
            }
        )
    ranked.sort(
        key=lambda item: (
            -_as_int(item.get("samples"), default=0),
            -_as_float(item.get("mean_effectiveness"), default=0.0),
            str(item.get("source", "")),
        )
    )
    return ranked[:5]


def _hit_rate(
    *,
    records: Sequence[Mapping[str, object]],
    key: str,
) -> tuple[int, float | None]:
    values: list[float] = []
    for item in records:
        parsed = _bool_to_float(item.get(key))
        if parsed is not None:
            values.append(parsed)
    if not values:
        return 0, None
    return len(values), float(sum(values) / len(values))


def _expected_direction(*, sentiment: float, llm_verdict: str) -> int:
    normalized_verdict = llm_verdict.strip().lower()
    if normalized_verdict in {"reject", "negative", "bearish", "down"}:
        return -1
    if normalized_verdict in {"approve", "positive", "bullish", "up"}:
        return 1
    if sentiment > 0.0:
        return 1
    if sentiment < 0.0:
        return -1
    return 0


def _default_verdict(sentiment: float) -> str:
    if sentiment > 0.05:
        return "approve"
    if sentiment < -0.05:
        return "reject"
    return "neutral"


def _canonical_headline(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", " ", value.lower()).strip()
    return " ".join(token for token in normalized.split() if token)


def _first_non_empty_str(record: Mapping[str, object], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
    return None


def _first_float(record: Mapping[str, object], keys: Sequence[str], default: float) -> float:
    for key in keys:
        value = _as_float_or_none(record.get(key))
        if value is not None:
            return value
    return default


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _normalize_datetime(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        return _normalize_datetime(datetime.fromisoformat(normalized))
    except ValueError:
        return None


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _stable_hash(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _bool_to_float(value: object) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    return None


def _as_float(value: object, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_positive_float(value: object) -> float | None:
    parsed = _as_float_or_none(value)
    if parsed is None or parsed <= 0.0:
        return None
    return parsed


def _as_float_or_none(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return default
    return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _default_connection_factory(database: str) -> _DuckConnection:
    duckdb_module = importlib.import_module("duckdb")
    connection = cast(_DuckConnection, duckdb_module.connect(database=database))
    return connection
