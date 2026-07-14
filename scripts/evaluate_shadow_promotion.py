from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_analyzer.research.promotion_gate import evaluate_shadow_promotion  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate a shadow candidate without promoting it."
    )
    parser.add_argument("evidence")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    payload = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    report = evaluate_shadow_promotion(payload)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["decision"] == "GO_PENDING_MANUAL_APPROVAL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
