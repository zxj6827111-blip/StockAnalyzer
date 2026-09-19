"""M3 收尾：把"全量回归"的 junitxml 变成审计工件。

生产数据路径上的"回归过没过"只能来自实测，不允许报告手工写一行
"3546 passed"然后空到没人能复算。因此把两次 junitxml 的结果本地汇总成
`artifacts/alpha_v2/audit/m3_batch_regression.json`（本文件即由脚本生成）。

```bash
python scripts/alpha_v2_m3_regression_record.py \
    --m3-junit /path/to/m3.junit.xml --full-junit /path/to/full.junit.xml
```
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from stock_analyzer.alpha_v2.artifacts import write_json_atomic  # noqa: E402

AUDIT_FILENAME = "m3_batch_regression.json"


def _parse_junit(
    path: Path, *, m3_files: int | None = None, m3_targeted: int | None = None
) -> dict[str, object]:
    suite = ET.parse(path).getroot()
    if suite.tag != "testsuite":
        suite = next(suite.iter("testsuite"))
    tests = int(suite.get("tests", 0))
    payload: dict[str, object] = {
        "junit_path": str(path),
        "junit_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "tests": tests,
        "failures": int(suite.get("failures", 0)),
        "errors": int(suite.get("errors", 0)),
        "skipped": int(suite.get("skipped", 0)),
        "time_seconds": float(suite.get("time", 0) or 0),
    }
    # M3 定向那一跑：文件数由调用方给（junit 不带文件维度），用例数就是本跑自己。
    if m3_targeted is not None:
        payload["m3_tests_targeted"] = int(m3_targeted)
    if m3_files is not None:
        payload["m3_test_files"] = int(m3_files)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M3 全量回归审计工件生成")
    parser.add_argument("--m3-junit", required=True)
    parser.add_argument("--full-junit", required=True)
    parser.add_argument(
        "--m3-files", type=int, default=0, help="M3 定向测试文件数（junit 里没有这一维）"
    )
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "artifacts" / "alpha_v2" / "audit" / AUDIT_FILENAME),
    )
    args = parser.parse_args(argv)

    m3 = _parse_junit(
        Path(args.m3_junit), m3_files=int(args.m3_files or 0), m3_targeted=None
    )
    m3["m3_tests_targeted"] = int(m3["tests"])
    full = _parse_junit(
        Path(args.full_junit),
        m3_files=int(args.m3_files or 0),
        m3_targeted=int(m3["tests"]),
    )
    payload = {
        "schema": "alpha_v2_m3_regression_audit.v1",
        "generated_at": datetime.now().astimezone().isoformat(),
        "workspace": str(REPO_ROOT),
        "head_at_generation": subprocess_head(),
        "command_m3": "pytest -q tests/test_alpha_v2_m3_*.py (junitxml)",
        "command_full": "pytest -q -n 4 --dist loadfile",
        "m3": m3,
        "full": full,
        "notes": (
            "数字以 junitxml 为准（非报告中手写的汇总行）。R3 起 m3_test_files / "
            "m3_tests_targeted 由 CLI 参数与本跑 junit 派生，不再写死——R2 版本曾固定 9/76，"
            "R3 修复轮新增 test_alpha_v2_m3_r3_final_blockers.py 后必须重新生成。"
        ),
    }
    path = write_json_atomic(Path(args.out), payload)
    print(f"[m3-regression-audit] 已写入 {path}")
    print(
        f"  m3={m3['tests']}/fail {m3['failures']}  full={full['tests']}/fail {full['failures']}"
    )
    return 0


def subprocess_head() -> str:
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
