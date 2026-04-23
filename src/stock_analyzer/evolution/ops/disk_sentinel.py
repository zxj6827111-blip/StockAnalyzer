"""Disk watermark monitoring and safe delayed cleanup."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol


class _DiskUsage(Protocol):
    total: int
    used: int
    free: int


@dataclass(frozen=True, slots=True)
class DiskSentinelReport:
    """Disk sentinel execution report."""

    usage_percent: float
    triggered: bool
    marked_for_deletion: tuple[Path, ...]
    purged: tuple[Path, ...]


class DiskSentinel:
    """Protect disk usage by marking low-priority data for delayed deletion."""

    def __init__(
        self,
        base_dir: str | Path,
        shadow_log_dir: str = "shadow_logs",
        faiss_snapshot_dir: str = "faiss_snapshots",
        suggestions_dir: str = "suggestions",
        high_watermark: float = 75.0,
        suggestions_age_days: int = 7,
        delete_delay_hours: int = 24,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._shadow_log_dir = self._base_dir / shadow_log_dir
        self._faiss_snapshot_dir = self._base_dir / faiss_snapshot_dir
        self._suggestions_dir = self._base_dir / suggestions_dir
        self._high_watermark = high_watermark
        self._suggestions_age = timedelta(days=suggestions_age_days)
        self._delete_delay = timedelta(hours=delete_delay_hours)
        self._quarantine_dir = self._base_dir / ".delete_queue"
        self._quarantine_dir.mkdir(parents=True, exist_ok=True)

    def enforce(self, now: datetime | None = None) -> DiskSentinelReport:
        """Run watermark check and safe cleanup procedure."""
        current = _as_utc(now) if now is not None else datetime.now(UTC)
        purged = tuple(self._purge_expired(now=current))
        usage_percent = self._usage_percent()
        if usage_percent < self._high_watermark:
            return DiskSentinelReport(
                usage_percent=usage_percent,
                triggered=False,
                marked_for_deletion=(),
                purged=purged,
            )

        marked: list[Path] = []
        candidates = self._collect_cleanup_candidates(now=current)
        for candidate in candidates:
            renamed = self._safe_mark_for_deletion(path=candidate, now=current)
            if renamed is not None:
                marked.append(renamed)

        return DiskSentinelReport(
            usage_percent=usage_percent,
            triggered=True,
            marked_for_deletion=tuple(marked),
            purged=purged,
        )

    def _usage_percent(self) -> float:
        usage = shutil.disk_usage(self._base_dir)
        total = max(int(usage.total), 1)
        return float(usage.used / total * 100.0)

    def _collect_cleanup_candidates(self, now: datetime) -> list[Path]:
        candidates: list[Path] = []
        candidates.extend(_list_children(self._shadow_log_dir))
        candidates.extend(_list_children(self._faiss_snapshot_dir))

        if self._suggestions_dir.exists():
            cutoff = now - self._suggestions_age
            for path in _list_children(self._suggestions_dir):
                modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
                if modified <= cutoff:
                    candidates.append(path)
        return sorted(candidates, key=lambda path: path.stat().st_mtime)

    def _safe_mark_for_deletion(self, path: Path, now: datetime) -> Path | None:
        if not path.exists():
            return None
        timestamp = now.strftime("%Y%m%d%H%M%S")
        target = self._quarantine_dir / f"{path.name}.pending.{timestamp}"
        suffix = 1
        while target.exists():
            target = self._quarantine_dir / f"{path.name}.pending.{timestamp}.{suffix}"
            suffix += 1
        path.rename(target)
        return target

    def _purge_expired(self, now: datetime) -> list[Path]:
        purged: list[Path] = []
        for pending in _list_children(self._quarantine_dir):
            modified = datetime.fromtimestamp(pending.stat().st_mtime, tz=UTC)
            if now - modified < self._delete_delay:
                continue
            if pending.is_dir():
                shutil.rmtree(pending)
            else:
                pending.unlink(missing_ok=True)
            purged.append(pending)
        return purged


def _list_children(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return [item for item in path.iterdir() if item.exists()]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
