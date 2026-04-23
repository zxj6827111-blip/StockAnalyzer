from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from stock_analyzer.evolution.ops.disk_sentinel import DiskSentinel


def _touch(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _set_mtime(path: Path, moment: datetime) -> None:
    timestamp = moment.timestamp()
    os.utime(path, (timestamp, timestamp))


def test_disk_sentinel_marks_targets_with_safe_rename(tmp_path: Path) -> None:
    now = datetime(2026, 3, 1, tzinfo=UTC)
    old_time = now - timedelta(days=10)
    recent_time = now - timedelta(days=2)

    shadow_file = tmp_path / "shadow_logs" / "shadow.log"
    faiss_file = tmp_path / "faiss_snapshots" / "idx.bin"
    old_suggestion = tmp_path / "suggestions" / "old.json"
    fresh_suggestion = tmp_path / "suggestions" / "fresh.json"

    _touch(shadow_file)
    _touch(faiss_file)
    _touch(old_suggestion)
    _touch(fresh_suggestion)

    _set_mtime(shadow_file, old_time)
    _set_mtime(faiss_file, old_time)
    _set_mtime(old_suggestion, old_time)
    _set_mtime(fresh_suggestion, recent_time)

    sentinel = DiskSentinel(base_dir=tmp_path, high_watermark=0.0)
    report = sentinel.enforce(now=now)

    assert report.triggered is True
    assert len(report.marked_for_deletion) >= 3
    assert shadow_file.exists() is False
    assert faiss_file.exists() is False
    assert old_suggestion.exists() is False
    assert fresh_suggestion.exists() is True


def test_disk_sentinel_purges_pending_after_24h(tmp_path: Path) -> None:
    now = datetime(2026, 3, 1, tzinfo=UTC)
    pending = tmp_path / ".delete_queue" / "payload.pending.20260228000000"
    _touch(pending)
    _set_mtime(pending, now - timedelta(hours=25))

    sentinel = DiskSentinel(base_dir=tmp_path, high_watermark=100.0)
    report = sentinel.enforce(now=now)
    assert pending in report.purged
    assert pending.exists() is False
