"""TongDaXin source freshness inspection and offline package sync helpers."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from struct import unpack


class TdxSyncError(RuntimeError):
    """Raised when a TongDaXin offline sync cannot be completed."""


_DAY_RECORD_SIZE = 32
_MINUTE_RECORD_SIZE = 32


def inspect_tdx_source_freshness(vipdoc_root: str | Path) -> dict[str, object]:
    root = Path(vipdoc_root).expanduser()
    if not root.exists():
        raise TdxSyncError(f"vipdoc root does not exist: {root}")
    if not root.is_dir():
        raise TdxSyncError(f"vipdoc root is not a directory: {root}")

    daily_files = sorted(root.rglob("*.day"))
    lc5_files = sorted(root.rglob("*.lc5"))
    lc1_files = sorted(root.rglob("*.lc1"))

    daily_latest_path = _latest_mtime_path(daily_files)
    lc5_latest_path = _latest_mtime_path(lc5_files)
    lc1_latest_path = _latest_mtime_path(lc1_files)

    return {
        "vipdoc_root": str(root),
        "daily": _freshness_entry(
            files=daily_files,
            latest_path=daily_latest_path,
            latest_timestamp=_read_day_last_timestamp(daily_latest_path),
        ),
        "minute_5": _freshness_entry(
            files=lc5_files,
            latest_path=lc5_latest_path,
            latest_timestamp=_read_minute_last_timestamp(lc5_latest_path),
        ),
        "minute_1": _freshness_entry(
            files=lc1_files,
            latest_path=lc1_latest_path,
            latest_timestamp=_read_minute_last_timestamp(lc1_latest_path),
        ),
    }


def load_tdx_manifest(package_root: str | Path) -> dict[str, object]:
    root = Path(package_root).expanduser()
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return {"exists": False, "manifest_path": str(manifest_path)}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TdxSyncError(f"failed to read manifest: {manifest_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TdxSyncError(f"manifest is not an object: {manifest_path}")
    payload = dict(payload)
    payload["exists"] = True
    payload["manifest_path"] = str(manifest_path)
    return payload


def run_tdx_offline_package_build(
    *,
    project_root: str | Path,
    vipdoc_root: str | Path,
    output_root: str | Path,
    include_bj: bool = True,
    skip_gp: bool = False,
    max_symbols: int = 0,
    timeout_sec: int = 7200,
) -> dict[str, object]:
    repo_root = Path(project_root).expanduser().resolve()
    script_path = repo_root / "scripts" / "build_tdx_offline_package.py"
    if not script_path.exists():
        raise TdxSyncError(f"sync script not found: {script_path}")

    command = [
        sys.executable,
        str(script_path),
        "--vipdoc-root",
        str(Path(vipdoc_root).expanduser().resolve()),
        "--output-root",
        str(Path(output_root).expanduser().resolve()),
    ]
    if include_bj:
        command.append("--include-bj")
    if skip_gp:
        command.append("--skip-gp")
    if max_symbols > 0:
        command.extend(["--max-symbols", str(max_symbols)])

    started_at = datetime.now()
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(30, int(timeout_sec)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TdxSyncError(f"tdx sync timed out after {timeout_sec}s") from exc

    finished_at = datetime.now()
    stdout_text = completed.stdout.strip()
    stderr_text = completed.stderr.strip()
    result = {
        "command": command,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_sec": round((finished_at - started_at).total_seconds(), 3),
        "returncode": completed.returncode,
        "stdout_tail": _tail_lines(stdout_text, limit=20),
        "stderr_tail": _tail_lines(stderr_text, limit=20),
    }
    if completed.returncode != 0:
        last_stderr = stderr_text.splitlines()[-1] if stderr_text else ""
        detail = f": {last_stderr}" if last_stderr else ""
        raise TdxSyncError(f"tdx sync build failed{detail}")
    return result


def _tail_lines(text: str, limit: int) -> list[str]:
    if not text:
        return []
    return text.splitlines()[-max(1, limit) :]


def _freshness_entry(
    *,
    files: list[Path],
    latest_path: Path | None,
    latest_timestamp: datetime | None,
) -> dict[str, object]:
    latest_mtime = None
    if latest_path is not None:
        latest_mtime = datetime.fromtimestamp(latest_path.stat().st_mtime)
    return {
        "file_count": len(files),
        "latest_path": str(latest_path) if latest_path is not None else "",
        "latest_timestamp": latest_timestamp.isoformat(sep=" ") if latest_timestamp else "",
        "latest_mtime": latest_mtime.isoformat(sep=" ") if latest_mtime else "",
    }


def _latest_mtime_path(files: list[Path]) -> Path | None:
    if not files:
        return None
    return max(files, key=lambda item: item.stat().st_mtime)


def _read_day_last_timestamp(path: Path | None) -> datetime | None:
    if path is None or not path.exists():
        return None
    try:
        with path.open("rb") as fp:
            fp.seek(-_DAY_RECORD_SIZE, 2)
            raw = fp.read(_DAY_RECORD_SIZE)
    except OSError:
        return None
    if len(raw) != _DAY_RECORD_SIZE:
        return None
    date_raw = unpack("<I", raw[:4])[0]
    date_text = f"{int(date_raw):08d}"
    try:
        return datetime.strptime(date_text, "%Y%m%d")
    except ValueError:
        return None


def _read_minute_last_timestamp(path: Path | None) -> datetime | None:
    if path is None or not path.exists():
        return None
    try:
        with path.open("rb") as fp:
            fp.seek(-_MINUTE_RECORD_SIZE, 2)
            raw = fp.read(_MINUTE_RECORD_SIZE)
    except OSError:
        return None
    if len(raw) != _MINUTE_RECORD_SIZE:
        return None
    try:
        date_raw = int(unpack("<H", raw[:2])[0])
        minutes_raw = int(unpack("<H", raw[2:4])[0])
    except Exception:
        return None
    date_part = date_raw % 2048
    year = date_raw // 2048 + 2004
    month = date_part // 100
    day = date_part % 100
    hour = minutes_raw // 60
    minute = minutes_raw % 60
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None
