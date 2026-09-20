"""Alpha V2 M3：Validation KPI 日报（汇总一个 epoch 的全部已成熟数据）。

```bash
python scripts/alpha_v2_validation_report.py --epoch-id alpha_v2_epoch_001
```
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from stock_analyzer.alpha_v2.validation.epoch import get_epoch  # noqa: E402
from stock_analyzer.alpha_v2.validation.validation_kpis import (  # noqa: E402
    KpiReportError,
    build_validation_kpi,
    write_kpi_report,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alpha V2 M3：Validation KPI 报告")
    parser.add_argument("--epoch-id", required=True)
    parser.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "alpha_v2"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    root = Path(args.out)
    epoch = get_epoch(root, args.epoch_id)
    if epoch is None:
        print(f"[kpi] epoch 不存在: {args.epoch_id}", file=sys.stderr)
        return 2
    try:
        payload = build_validation_kpi(root=root, epoch=epoch)
    except KpiReportError as exc:
        print(f"[kpi] {exc}", file=sys.stderr)
        return 3
    json_path, md_path = write_kpi_report(root=root, epoch=epoch, payload=payload)
    print(f"[kpi] 已写入: {json_path}")
    print(f"[kpi] 已写入: {md_path}")
    maturity = payload.get("maturity", {})
    gates = payload.get("sample_gate_status", {})
    print(
        f"[kpi] mature_dates_5d={maturity.get('mature_dates_5d')}  "
        f"gates={{'failure_alert': {gates.get('failure_alert', {}).get('reached')}, "
        f"'direction_review': {gates.get('direction_review', {}).get('reached')}}}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
