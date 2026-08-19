"""Execute one exact scheduler job in an isolated child process."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from stock_analyzer.build_identity import get_build_manifest
from stock_analyzer.config import get_config
from stock_analyzer.runtime.service import StockAnalyzerService


def run_job_once(
    *,
    service: StockAnalyzerService,
    job: str,
    now: datetime,
    run_id: str,
) -> dict[str, object]:
    results = service.run_due_jobs(now=now, only_jobs=[job])
    direct = [item for item in results if str(item.get("job", "")).strip() == job]
    relevant = direct or results
    ran = any(bool(item.get("ran", False)) for item in relevant)
    failed = [item for item in relevant if not bool(item.get("success", False))]
    if failed:
        status = "failed"
    elif ran:
        status = "success"
    else:
        status = "skipped"
    detail = ";".join(
        str(item.get("detail", "")).strip()
        for item in relevant
        if str(item.get("detail", "")).strip()
    ) or status
    return {
        "job": job,
        "run_id": run_id,
        "timestamp": now.isoformat(),
        "completed_at": datetime.now().isoformat(),
        "status": status,
        "success": not failed,
        "ran": ran,
        "detail": detail,
        "results": results,
        "build": get_build_manifest(),
    }


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, separators=(",", ":"), default=str)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(temp, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--now", required=True)
    parser.add_argument("--result-path", required=True)
    args = parser.parse_args()
    result_path = Path(args.result_path)
    try:
        service = StockAnalyzerService(config=get_config())
        payload = run_job_once(
            service=service,
            job=str(args.job).strip(),
            now=datetime.fromisoformat(str(args.now)),
            run_id=str(args.run_id).strip(),
        )
        exit_code = 0 if bool(payload.get("success", False)) else 1
    except Exception as exc:
        payload = {
            "job": str(args.job).strip(),
            "run_id": str(args.run_id).strip(),
            "timestamp": str(args.now),
            "completed_at": datetime.now().isoformat(),
            "status": "failed",
            "success": False,
            "ran": False,
            "detail": str(exc),
            "error_type": exc.__class__.__name__,
            "build": get_build_manifest(),
        }
        exit_code = 1
    _write_json_atomic(result_path, payload)
    print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
