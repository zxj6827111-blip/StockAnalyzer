"""Cross-process distributed file lock (audit P2-#20).

A :class:`DistributedFileLock` provides mutual exclusion across processes
that share a filesystem volume (compose mounts ``./artifacts`` into every
service).  It generalizes the market-warehouse sync lock pattern:
``O_CREAT|O_EXCL`` atomic creation, mtime heartbeat renewal and stale-lock
takeover, with an owner token guarding renew/release.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4

DEFAULT_STALE_AFTER_SEC = 300
_ACQUIRE_RETRIES = 3


def _hostname() -> str:
    hostname = os.getenv("HOSTNAME", "").strip()
    if hostname:
        return hostname
    try:
        return socket.gethostname().strip()
    except OSError:
        return ""


def _as_int(value: object, *, default: int) -> int:
    if isinstance(value, (int, float, str)):
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return default
    return default


class DistributedFileLock:
    """File-backed lock with owner token, heartbeat and stale takeover.

    Semantics
    ---------
    * ``acquire`` creates the lock file atomically (``O_CREAT|O_EXCL``) and
      records the owner token plus ``stale_after_sec``.  A lock whose mtime
      is older than ``stale_after_sec`` is considered abandoned (crash
      recovery) and may be taken over by any acquirer; this also covers the
      previous instance's own stale lock from an earlier run.
    * While held, a daemon heartbeat thread refreshes the mtime every
      ``heartbeat_interval_sec`` so long-running holders are not stolen.
    * ``renew``/``release`` verify the owner token first: a foreign owner
      can neither extend nor remove the lock file.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        stale_after_sec: int = DEFAULT_STALE_AFTER_SEC,
        owner_token: str | None = None,
        heartbeat_interval_sec: float | None = None,
    ) -> None:
        self._path = Path(path)
        self._stale_after_sec = max(1, int(stale_after_sec))
        self._owner_token = owner_token or f"{os.getpid()}-{uuid4().hex}"
        if heartbeat_interval_sec is None:
            heartbeat_interval_sec = max(5.0, min(30.0, self._stale_after_sec / 4.0))
        self._heartbeat_interval_sec = max(0.05, float(heartbeat_interval_sec))
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._held = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def owner_token(self) -> str:
        return self._owner_token

    @property
    def stale_after_sec(self) -> int:
        return self._stale_after_sec

    def is_held(self) -> bool:
        """Whether this instance currently holds the lock."""
        return self._held

    def acquire(self) -> bool:
        """Try to acquire the lock, taking over a stale lock if present.

        Returns ``True`` when this instance now holds the lock (a heartbeat
        thread keeps it fresh) and ``False`` when another holder is active.
        """
        if self._held:
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._build_payload()
        for _ in range(_ACQUIRE_RETRIES):
            try:
                fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if not self.is_stale():
                    return False
                try:
                    self._path.unlink()
                except FileNotFoundError:
                    continue
                except OSError:
                    return False
                continue
            except OSError:
                raise
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fp:
                    json.dump(payload, fp, ensure_ascii=False, indent=2)
                    fp.write("\n")
                    fp.flush()
                    os.fsync(fp.fileno())
            except Exception:
                try:
                    self._path.unlink()
                except OSError:
                    pass
                raise
            self._held = True
            self._start_heartbeat()
            return True
        return False

    def renew(self) -> bool:
        """Refresh the lock mtime; refuses when the owner token changed."""
        current = self._read_payload()
        if str(current.get("owner_token", "")).strip() != self._owner_token:
            return False
        try:
            os.utime(self._path, None)
        except OSError:
            return False
        return True

    def release(self) -> None:
        """Stop the heartbeat and remove the lock file (owner-guarded).

        A lock file owned by a different token is never unlinked here.
        """
        if not self._held:
            return
        self._stop_heartbeat()
        current = self._read_payload()
        if current and str(current.get("owner_token", "")).strip() != self._owner_token:
            return
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        finally:
            self._held = False

    def is_stale(self) -> bool:
        """Whether the lock file (if any) is older than its stale timeout."""
        try:
            stat = self._path.stat()
        except OSError:
            return False
        stale_after_sec = _as_int(
            self._read_payload().get("stale_after_sec"),
            default=self._stale_after_sec,
        )
        age_sec = max(0.0, datetime.now().timestamp() - stat.st_mtime)
        return age_sec >= stale_after_sec

    def status(self) -> dict[str, object]:
        """Snapshot of the lock file state for observability and tests."""
        payload = self._read_payload()
        exists = self._path.exists()
        stale_after_sec = _as_int(
            payload.get("stale_after_sec"),
            default=self._stale_after_sec,
        )
        age_sec = 0.0
        mtime: float | None = None
        if exists:
            try:
                mtime = self._path.stat().st_mtime
            except OSError:
                pass
        if mtime is not None:
            age_sec = max(0.0, datetime.now().timestamp() - mtime)
        owner_token = str(payload.get("owner_token", "")).strip()
        return {
            "exists": exists,
            "owner_token": owner_token,
            "held_by_me": exists and owner_token == self._owner_token,
            "is_stale": exists and age_sec >= stale_after_sec,
            "age_sec": round(age_sec, 3),
            "stale_after_sec": stale_after_sec,
            "pid": payload.get("pid"),
            "hostname": payload.get("hostname"),
            "created_at": payload.get("created_at"),
        }

    def _build_payload(self) -> dict[str, object]:
        return {
            "owner_token": self._owner_token,
            "pid": os.getpid(),
            "hostname": _hostname(),
            "created_at": datetime.now().isoformat(),
            "stale_after_sec": self._stale_after_sec,
        }

    def _read_payload(self) -> dict[str, object]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
        return {}

    def _start_heartbeat(self) -> None:
        self._heartbeat_stop = threading.Event()

        def _beat() -> None:
            while not self._heartbeat_stop.wait(timeout=self._heartbeat_interval_sec):
                self.renew()

        thread = threading.Thread(
            target=_beat,
            name=f"file-lock-heartbeat-{os.getpid()}",
            daemon=True,
        )
        self._heartbeat_thread = thread
        thread.start()

    def _stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(1.0, self._heartbeat_interval_sec))
        self._heartbeat_thread = None
