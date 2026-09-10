"""M12 主题维度事件账本（改造自 ``m7_event_ledger``，theme 维度去 symbol 强绑）。

m7 ledger 的 schema 强绑 ``symbol NOT NULL``（个股新闻线）；主题线以
theme 为主维度，代理价格用**板块指数收盘均价 / 确认商品价格**衡量有效性：
- ``reference_price``：事件首次入账时的主题代理价格（确认商品的最新收盘价，
  多商品取均值；缺失时为 NULL，有效性无法计算）；
- ``hit_rate_1d/3d/5d``：T+1/3/5 代理价格按主题方向变动的比例（方向一致
  = effective）。

保留 m7 的 dedup（主题+标题+日期桶稳定哈希）/ TTL 归档 / 重复计数 /
滞后有效性统计框架。DuckDB 连接工厂可注入便于测试（无真实 duckdb 时用
内存/临时库）。
"""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast


@dataclass(frozen=True, slots=True)
class ThemeLedgerIngestSummary:
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
class ThemeLedgerEffectivenessSummary:
    """主题线有效性统计（Phase 2 升级门槛的数据源）。

    ``hit_rate_*`` 只统计价格确认激活的事件（``price_confirmed=True``）——
    门槛语义是"确认激活的主题事件有效性"，未确认事件（未过价格命门）不应
    稀释分母。``matured_*``/``confirmed_matured_*`` 分别为全体与确认事件数。
    """

    active_events: int
    archived_events: int
    duplicate_hits: int
    matured_1d: int
    hit_rate_1d: float | None
    matured_3d: int
    hit_rate_3d: float | None
    matured_5d: int
    hit_rate_5d: float | None
    confirmed_matured_3d: int
    confirmed_hit_rate_3d: float | None
    by_theme: list[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return {
            "active_events": self.active_events,
            "archived_events": self.archived_events,
            "duplicate_hits": self.duplicate_hits,
            "matured_1d": self.matured_1d,
            "hit_rate_1d": self.hit_rate_1d,
            "matured_3d": self.matured_3d,
            "hit_rate_3d": self.hit_rate_3d,
            "matured_5d": self.matured_5d,
            "hit_rate_5d": self.hit_rate_5d,
            "confirmed_matured_3d": self.confirmed_matured_3d,
            "confirmed_hit_rate_3d": self.confirmed_hit_rate_3d,
            "by_theme": [dict(item) for item in self.by_theme],
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
    "theme_id",
    "event_type",
    "direction",
    "headline",
    "matched_keywords",
    "intensity",
    "confidence",
    "price_confirmed",
    "confirmed_by",
    "reference_price",
    "latest_price",
    "published_at",
    "first_seen_at",
    "last_seen_at",
    "expires_at",
    "status",
    "archived_at",
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

# 有效性 horizon（小时, return 列, effective 列）——与 m7 相同的 1/3/5 交易日节奏
_HORIZON_COLUMNS = (
    (24.0, "return_1d", "effective_1d"),
    (72.0, "return_3d", "effective_3d"),
    (120.0, "return_5d", "effective_5d"),
)


class ThemeEventLedger:
    """主题维度事件账本：dedup / TTL 归档 / 滞后有效性统计。"""

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
        proxy_price_by_theme: Mapping[str, float],
    ) -> ThemeLedgerIngestSummary:
        """一次同步的账本更新：刷新有效性 → 归档过期 → dedup 插入。"""
        observed_at = _normalize_datetime(now)
        normalized_prices = {
            str(theme).strip(): float(price)
            for theme, price in proxy_price_by_theme.items()
            if str(theme).strip() and float(price) > 0.0
        }
        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            refreshed = self._refresh_effectiveness(
                conn=conn,
                now=observed_at,
                price_by_theme=normalized_prices,
            )
            archived = self._archive_expired(conn=conn, now=observed_at)
            inserted = 0
            deduplicated = 0
            for record in records:
                candidate = _normalize_candidate(
                    record=record,
                    observed_at=observed_at,
                    ttl_days=self._ttl_days,
                    price_by_theme=normalized_prices,
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
                            "INSERT INTO m12_theme_ledger ("
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
                        "UPDATE m12_theme_ledger SET "
                        "last_seen_at = ?, "
                        "headline = ?, "
                        "matched_keywords = ?, "
                        "intensity = ?, "
                        "confidence = ?, "
                        "price_confirmed = ?, "
                        "confirmed_by = ?, "
                        "latest_price = ?, "
                        "occurrence_count = ?, "
                        "duplicate_hits = ? "
                        "WHERE ledger_id = ?"
                    ),
                    [
                        updated["last_seen_at"],
                        updated["headline"],
                        updated["matched_keywords"],
                        updated["intensity"],
                        updated["confidence"],
                        updated["price_confirmed"],
                        updated["confirmed_by"],
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
                price_by_theme=normalized_prices,
            )
        finally:
            conn.close()
        return ThemeLedgerIngestSummary(
            inserted=inserted,
            deduplicated=deduplicated,
            archived=archived,
            refreshed_effective_events=refreshed,
        )

    def effectiveness_summary(self) -> ThemeLedgerEffectivenessSummary:
        """汇总全表 + 分主题的有效性统计（shadow 周报数据源）。"""
        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            records = self._select_all(conn=conn)
        finally:
            conn.close()
        active_events = sum(1 for item in records if item.get("status") == "active")
        archived_events = sum(1 for item in records if item.get("status") == "archived")
        duplicate_hits = sum(_as_int(item.get("duplicate_hits"), default=0) for item in records)
        matured_1d, hit_rate_1d = _hit_rate(records=records, key="effective_1d")
        matured_3d, hit_rate_3d = _hit_rate(records=records, key="effective_3d")
        matured_5d, hit_rate_5d = _hit_rate(records=records, key="effective_5d")
        # 升级门槛专用：只统计价格确认激活的事件（未确认事件不稀释分母）。
        confirmed_records = [item for item in records if bool(item.get("price_confirmed", False))]
        confirmed_matured_3d, confirmed_hit_rate_3d = _hit_rate(
            records=confirmed_records,
            key="effective_3d",
        )
        by_theme = _theme_reliability(records)
        return ThemeLedgerEffectivenessSummary(
            active_events=active_events,
            archived_events=archived_events,
            duplicate_hits=duplicate_hits,
            matured_1d=matured_1d,
            hit_rate_1d=hit_rate_1d,
            matured_3d=matured_3d,
            hit_rate_3d=hit_rate_3d,
            matured_5d=matured_5d,
            hit_rate_5d=hit_rate_5d,
            confirmed_matured_3d=confirmed_matured_3d,
            confirmed_hit_rate_3d=confirmed_hit_rate_3d,
            by_theme=by_theme,
        )

    def list_records(
        self,
        *,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        """列出账本行（API /theme/events 预览数据源）。"""
        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            if status is None:
                rows = conn.execute(
                    f"SELECT {', '.join(_SELECT_COLUMNS)} "
                    "FROM m12_theme_ledger ORDER BY first_seen_at DESC, ledger_id LIMIT ?",
                    [max(1, int(limit))],
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {', '.join(_SELECT_COLUMNS)} "
                    "FROM m12_theme_ledger WHERE status = ? "
                    "ORDER BY first_seen_at DESC, ledger_id LIMIT ?",
                    [status, max(1, int(limit))],
                ).fetchall()
            return [_row_to_record(row) for row in rows]
        finally:
            conn.close()

    def _ensure_table(self, *, conn: _DuckConnection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS m12_theme_ledger ("
            "ledger_id VARCHAR PRIMARY KEY, "
            "event_id VARCHAR NOT NULL, "
            "dedup_key VARCHAR NOT NULL, "
            "theme_id VARCHAR NOT NULL, "
            "event_type VARCHAR NOT NULL, "
            "direction INTEGER NOT NULL, "
            "headline VARCHAR NOT NULL, "
            "matched_keywords VARCHAR NOT NULL, "
            "intensity DOUBLE NOT NULL, "
            "confidence DOUBLE NOT NULL, "
            "price_confirmed BOOLEAN NOT NULL, "
            "confirmed_by VARCHAR NOT NULL, "
            "reference_price DOUBLE, "
            "latest_price DOUBLE, "
            "published_at VARCHAR, "
            "first_seen_at VARCHAR NOT NULL, "
            "last_seen_at VARCHAR NOT NULL, "
            "expires_at VARCHAR NOT NULL, "
            "status VARCHAR NOT NULL, "
            "archived_at VARCHAR, "
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

    def _select_all(self, *, conn: _DuckConnection) -> list[dict[str, object]]:
        rows = conn.execute(
            f"SELECT {', '.join(_SELECT_COLUMNS)} "
            "FROM m12_theme_ledger ORDER BY first_seen_at, ledger_id"
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def _select_active_by_dedup_key(
        self,
        *,
        conn: _DuckConnection,
        dedup_key: str,
    ) -> dict[str, object] | None:
        row = conn.execute(
            f"SELECT {', '.join(_SELECT_COLUMNS)} "
            "FROM m12_theme_ledger WHERE status = 'active' AND dedup_key = ? LIMIT 1",
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
    ) -> int:
        now_iso = now.isoformat()
        rows = conn.execute(
            f"SELECT {', '.join(_SELECT_COLUMNS)} "
            "FROM m12_theme_ledger WHERE status = 'active' AND expires_at <= ? "
            "ORDER BY expires_at, ledger_id",
            [now_iso],
        ).fetchall()
        if not rows:
            return 0
        archive_path = self._archive_dir / f"m12_theme_ledger_{now.strftime('%Y%m%d')}.jsonl"
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with archive_path.open("a", encoding="utf-8") as fp:
            for row in rows:
                record = _row_to_record(row)
                record["status"] = "archived"
                record["archived_at"] = now_iso
                fp.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        conn.execute(
            "UPDATE m12_theme_ledger SET status = 'archived', archived_at = ? "
            "WHERE status = 'active' AND expires_at <= ?",
            [now_iso, now_iso],
        )
        return len(rows)

    def _refresh_effectiveness(
        self,
        *,
        conn: _DuckConnection,
        now: datetime,
        price_by_theme: Mapping[str, float],
    ) -> int:
        """滞后有效性：T+1/3/5 的代理价格按主题方向变动 → effective。"""
        if not price_by_theme:
            return 0
        rows = conn.execute(
            f"SELECT {', '.join(_SELECT_COLUMNS)} "
            "FROM m12_theme_ledger WHERE status = 'active' ORDER BY first_seen_at, ledger_id"
        ).fetchall()
        updated_rows = 0
        for row in rows:
            record = _row_to_record(row)
            theme_id = str(record["theme_id"]).strip()
            current_price = price_by_theme.get(theme_id)
            reference_price = _as_positive_float(record.get("reference_price"))
            if current_price is None or current_price <= 0.0 or reference_price is None:
                continue
            first_seen_at = _parse_datetime(record.get("first_seen_at"))
            if first_seen_at is None:
                continue
            age_hours = max(0.0, (now - first_seen_at).total_seconds() / 3600.0)
            direction = int(_as_int(record.get("direction"), default=1))
            if direction == 0:
                continue
            updates: dict[str, object] = {"latest_price": float(current_price)}
            changed = False
            for horizon_hours, return_key, effective_key in _HORIZON_COLUMNS:
                if age_hours + 1e-9 < horizon_hours:
                    continue
                if record.get(return_key) is not None or record.get(effective_key) is not None:
                    continue
                realized_return = float(current_price / reference_price - 1.0)
                effective = realized_return * float(direction) >= 0.0
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
                    "UPDATE m12_theme_ledger SET "
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


def _normalize_candidate(
    *,
    record: Mapping[str, object],
    observed_at: datetime,
    ttl_days: int,
    price_by_theme: Mapping[str, float],
) -> dict[str, object] | None:
    theme_id = _first_non_empty_str(record, keys=("theme_id", "theme"))
    headline = _first_non_empty_str(record, keys=("headline", "title", "新闻标题"))
    if theme_id is None or headline is None:
        return None
    published_at = _parse_datetime(
        _first_non_empty_str(record, keys=("published_at", "published_time", "timestamp", "time"))
    )
    event_id = _first_non_empty_str(record, keys=("event_id", "id", "news_id"))
    keywords = record.get("matched_keywords")
    keyword_text = (
        ",".join(str(item) for item in keywords if str(item).strip())
        if isinstance(keywords, (list, tuple))
        else ""
    )
    intensity = _clamp(_as_float(record.get("intensity"), default=0.0), 0.0, 1.0)
    confidence = _clamp(_as_float(record.get("confidence"), default=0.0), 0.0, 1.0)
    direction = int(_as_int(record.get("direction"), default=1)) or 1
    price_confirmed = bool(record.get("price_confirmed", False))
    confirmed_by = record.get("confirmed_by")
    confirmed_by_text = (
        ",".join(str(item) for item in confirmed_by if str(item).strip())
        if isinstance(confirmed_by, (list, tuple))
        else ""
    )
    dedup_key = _stable_hash(
        {
            "theme_id": theme_id,
            "headline": headline.strip(),
            "published_bucket": (
                published_at.strftime("%Y-%m-%d")
                if published_at is not None
                else observed_at.strftime("%Y-%m-%d")
            ),
        }
    )[:24]
    reference_price = _as_positive_float(price_by_theme.get(theme_id))
    ledger_id = _stable_hash(
        {"dedup_key": dedup_key, "first_seen_at": observed_at.isoformat()}
    )[:24]
    return {
        "ledger_id": ledger_id,
        "event_id": event_id
        or _stable_hash(
            {
                "theme_id": theme_id,
                "headline": headline,
                "published_at": published_at.isoformat() if published_at is not None else "",
            }
        )[:24],
        "dedup_key": dedup_key,
        "theme_id": theme_id,
        "event_type": _first_non_empty_str(record, keys=("event_type", "category")) or "",
        "direction": direction,
        "headline": headline.strip(),
        "matched_keywords": keyword_text,
        "intensity": float(intensity),
        "confidence": float(confidence),
        "price_confirmed": price_confirmed,
        "confirmed_by": confirmed_by_text,
        "reference_price": reference_price,
        "latest_price": reference_price,
        "published_at": published_at.isoformat() if published_at is not None else None,
        "first_seen_at": observed_at.isoformat(),
        "last_seen_at": observed_at.isoformat(),
        "expires_at": (observed_at + timedelta(days=ttl_days)).isoformat(),
        "status": "active",
        "archived_at": None,
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
    # 重复事件：强度/置信度取 max（同一事件被多家转载视为更强信号），
    # 价格确认取或（一旦有确认记录即保留）。
    return {
        "ledger_id": existing["ledger_id"],
        "last_seen_at": candidate["last_seen_at"],
        "headline": str(existing.get("headline", "")).strip() or str(candidate.get("headline", "")),
        "matched_keywords": str(existing.get("matched_keywords", ""))
        or str(candidate.get("matched_keywords", "")),
        "intensity": max(
            _as_float(existing.get("intensity"), default=0.0),
            _as_float(candidate.get("intensity"), default=0.0),
        ),
        "confidence": max(
            _as_float(existing.get("confidence"), default=0.0),
            _as_float(candidate.get("confidence"), default=0.0),
        ),
        "price_confirmed": bool(existing.get("price_confirmed", False))
        or bool(candidate.get("price_confirmed", False)),
        "confirmed_by": str(existing.get("confirmed_by", ""))
        or str(candidate.get("confirmed_by", "")),
        "latest_price": candidate.get("latest_price") or existing.get("latest_price"),
        "occurrence_count": existing_occurrences + 1,
        "duplicate_hits": _as_int(existing.get("duplicate_hits"), default=0) + 1,
    }


def _row_to_record(row: Sequence[object]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for index, column in enumerate(_SELECT_COLUMNS):
        payload[column] = row[index]
    return payload


def _theme_reliability(records: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, dict[str, object]] = {}
    for item in records:
        theme_id = str(item.get("theme_id", "")).strip() or "unknown"
        entry = grouped.setdefault(
            theme_id,
            {"theme_id": theme_id, "events": 0, "confirmed": 0, "effective_3d": 0, "matured_3d": 0},
        )
        entry["events"] = _as_int(entry.get("events"), default=0) + 1
        if bool(item.get("price_confirmed", False)):
            entry["confirmed"] = _as_int(entry.get("confirmed"), default=0) + 1
        raw_effective = _bool_to_float(item.get("effective_3d"))
        if raw_effective is not None:
            entry["matured_3d"] = _as_int(entry.get("matured_3d"), default=0) + 1
            if raw_effective > 0:
                entry["effective_3d"] = _as_int(entry.get("effective_3d"), default=0) + 1
    ranked: list[dict[str, object]] = []
    for theme_id, entry in grouped.items():
        matured = _as_int(entry.get("matured_3d"), default=0)
        hit_rate = (
            _as_int(entry.get("effective_3d"), default=0) / matured if matured > 0 else None
        )
        ranked.append(
            {
                "theme_id": theme_id,
                "events": _as_int(entry.get("events"), default=0),
                "confirmed": _as_int(entry.get("confirmed"), default=0),
                "matured_3d": matured,
                "hit_rate_3d": hit_rate,
            }
        )
    ranked.sort(key=lambda item: (-_as_int(item.get("events"), default=0), str(item["theme_id"])))
    return ranked


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


def _first_non_empty_str(record: Mapping[str, object], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
    return None


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
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_positive_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if parsed > 0.0 else None
    if isinstance(value, str):
        try:
            parsed = float(value)
            return parsed if parsed > 0.0 else None
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
