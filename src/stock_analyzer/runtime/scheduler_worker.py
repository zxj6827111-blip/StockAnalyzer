"""Polling worker that executes due scheduler jobs continuously."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime

from stock_analyzer.config import get_config
from stock_analyzer.runtime.service import StockAnalyzerService


def main() -> None:
    config = get_config()
    service = StockAnalyzerService(config=config)
    interval_sec = _poll_interval()
    while True:
        now = datetime.now()
        results = service.run_due_jobs(now=now)
        executed = [item for item in results if bool(item.get("ran", False))]
        if executed:
            print(
                json.dumps(
                    {
                        "timestamp": now.isoformat(),
                        "executed": executed,
                    },
                    ensure_ascii=False,
                )
            )
        time.sleep(interval_sec)


def _poll_interval() -> int:
    raw = os.getenv("SCHEDULER_POLL_SEC", "30").strip()
    try:
        return max(5, int(raw))
    except ValueError:
        return 30


if __name__ == "__main__":
    main()
