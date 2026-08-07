"""Build the incremental full-market feature snapshot (features_light).

Usage (NAS nightly, after the vendor update and before the week5 scan):

    python scripts/build_feature_snapshot.py --config config/default.yaml

Skips when a current snapshot already exists (source signature unchanged).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_analyzer.config import load_config  # noqa: E402
from stock_analyzer.data.provider_factory import build_runtime_provider  # noqa: E402
from stock_analyzer.feature.snapshot import build_feature_snapshot  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(ROOT / "config" / "default.yaml"),
        help="StockAnalyzer config path",
    )
    parser.add_argument(
        "--symbols-file",
        default="",
        help="Optional symbol list file; defaults to the provider universe",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap the number of symbols processed (0 = all)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=0,
        help="Feature window days (0 = config default)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild even when the current snapshot is fresh",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    provider = build_runtime_provider(config.data_source)
    symbols: list[str] = []
    if args.symbols_file.strip():
        raw_path = Path(args.symbols_file.strip())
        symbols = [
            line.strip()
            for line in raw_path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
    if not symbols:
        list_symbols = getattr(provider, "list_symbols", None)
        if callable(list_symbols):
            try:
                symbols = list(list_symbols())
            except Exception:
                symbols = []
        if not symbols:
            print("no symbols resolved (--symbols-file or provider universe)", file=sys.stderr)
            return 2

    def on_progress(done: int, total: int) -> None:
        print(f"progress: {done}/{total}", file=sys.stderr)

    report = build_feature_snapshot(
        config,
        provider,
        symbols=symbols,
        lookback_days=args.lookback_days or None,
        max_workers=4,
        force=bool(args.force),
        on_progress=on_progress,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if bool(report.get("ok", False)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
