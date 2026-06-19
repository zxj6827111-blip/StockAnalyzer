"""Build a P0 shadow experiment plan from existing analysis artifacts."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build P0 shadow experiment plan")
    parser.add_argument(
        "--analysis-dir",
        default="artifacts/analysis",
        help="Directory containing existing analysis JSON artifacts.",
    )
    parser.add_argument(
        "--output",
        default="artifacts/analysis/p0_shadow_experiment_plan_v1.json",
        help="Output JSON path.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    analysis_dir = Path(args.analysis_dir)
    output_path = Path(args.output)
    planner = importlib.import_module("stock_analyzer.research.shadow_experiment_planner")
    build_shadow_experiment_plan = planner.build_shadow_experiment_plan
    plan = build_shadow_experiment_plan(analysis_dir=analysis_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"shadow experiment plan written: {output_path}")
    print(f"recommended experiments: {len(plan.get('recommended_experiments', []))}")


if __name__ == "__main__":
    main()
