"""Polling worker that executes due scheduler jobs continuously."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from stock_analyzer.build_identity import get_build_manifest
from stock_analyzer.config import get_config
from stock_analyzer.runtime.service import StockAnalyzerService


def main() -> None:
    interval_sec = _poll_interval()
    service: StockAnalyzerService | None = None
    consecutive_failures = 0
    while True:
        now = datetime.now()
        try:
            if service is None:
                service = StockAnalyzerService(config=get_config())
            results = service.run_due_jobs(now=now)
            executed = [item for item in results if bool(item.get("ran", False))]
            consecutive_failures = 0
            payload = {
                "timestamp": now.isoformat(),
                "status": "ok",
                "executed": executed,
                "scheduler_state": service._scheduler.export_state(),
                "build": get_build_manifest(),
            }
            _write_heartbeat(payload)
            if executed:
                print(json.dumps(payload, ensure_ascii=False))
            time.sleep(interval_sec)
        except Exception as exc:
            consecutive_failures += 1
            service = None
            backoff_sec = min(300, max(interval_sec, 2 ** min(consecutive_failures, 8)))
            payload = {
                "timestamp": now.isoformat(),
                "status": "error",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
                "consecutive_failures": consecutive_failures,
                "retry_in_sec": backoff_sec,
                "build": get_build_manifest(),
            }
            _write_heartbeat(payload)
            print(json.dumps(payload, ensure_ascii=False), flush=True)
            time.sleep(backoff_sec)


def _write_heartbeat(payload: dict[str, object]) -> None:
    path = Path(
        os.getenv(
            "SCHEDULER_HEARTBEAT_PATH",
            "artifacts/runtime/scheduler_heartbeat.json",
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, separators=(",", ":"), default=str)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(temp, path)


def _poll_interval() -> int:
    raw = os.getenv("SCHEDULER_POLL_SEC", "30").strip()
    try:
        return max(5, int(raw))
    except ValueError:
        return 30


if __name__ == "__main__":
    main()
