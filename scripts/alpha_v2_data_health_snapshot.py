"""Alpha V2 M3：生成**当天**的 data_health 工件（S08 契约，供 shadow capture 读取）。

为什么需要它（N-R2-1 的另一半）：治理层只认"同日且 ok"的 data_health，而生产侧
从来没有产出过这个工件——结果是 capture 每天都写 ``not_available``，clean OOS
永远为 0、20/60/120/250 样本门永不推进。本脚本是**唯一**的生产产出点，且：

- 口径不新造：直接调 ``ops.data_health.evaluate_data_health``（S08 实现），
  本脚本只负责把"能证明的输入"喂进去；
- **拿不到就不喂**：缺 universe/board/feature/model 输入时 S08 会把对应检查
  标成 degraded（"缺失不得当健康"），于是 ``status != healthy``，
  capture 侧如实写 not_available → 该日不进 clean OOS（fail-closed，不猜）；
- 不改变任何交易决策，只写一份证据文件。

```bash
python scripts/alpha_v2_data_health_snapshot.py --as-of 2026-09-22
# 容器内（推荐落点）：
python scripts/alpha_v2_data_health_snapshot.py --out /app/artifacts/runtime/data_health.json
```
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from stock_analyzer.alpha_v2.validation.data_health_capture import (  # noqa: E402
    build_data_health_artifact,
    write_data_health_artifact,
)
from stock_analyzer.alpha_v2.validation.runtime_identity import (  # noqa: E402
    price_contract_block,
)
from stock_analyzer.config import load_config  # noqa: E402

DEFAULT_OUT = Path("artifacts") / "runtime" / "data_health.json"
DEFAULT_BREADTH = Path("artifacts") / "runtime" / "market_breadth.json"

# data_health 允许的"当天"宽限：交易日差 ≤ 该值仍算新鲜（S08 默认 3 天）。
DEFAULT_MAX_FRESHNESS_DAYS = 3


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alpha V2 M3：写当天 data_health 工件")
    parser.add_argument("--as-of", default="", help="评估基准日（默认 = 系统今天）")
    parser.add_argument("--market-db", default="artifacts/warehouse/market.duckdb")
    parser.add_argument("--config", default="config/default.yaml")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--breadth-artifact", default=str(DEFAULT_BREADTH))
    parser.add_argument("--universe-snapshot", default="", help="S03 股票池快照 JSON")
    parser.add_argument("--board-coverage", default="", help="板块覆盖 JSON")
    parser.add_argument("--feature-snapshot", default="", help="特征快照清单 JSON")
    parser.add_argument("--model-identity", default="", help="模型身份报告 JSON")
    parser.add_argument("--max-freshness-days", type=int, default=DEFAULT_MAX_FRESHNESS_DAYS)
    parser.add_argument("--min-expected-active-coverage", type=float, default=0.95)
    parser.add_argument("--print-only", action="store_true")
    return parser.parse_args(argv)


def _read_json(path: str) -> dict[str, object] | None:
    text = str(path or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(Path(text).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"[data-health] 输入不可读，按缺失处理: {text}", file=sys.stderr)
        return None
    return payload if isinstance(payload, dict) else None


def _db_facts(market_db: Path, as_of: date) -> tuple[str | None, int | None]:
    """从行情库取"能证明的"两件事：最新交易日、当日有效标的分母分子之一。"""
    try:
        import duckdb
    except ImportError:  # pragma: no cover - 环境缺依赖时如实降级
        print(
            "[data-health] 无 duckdb，latest_trade_date/valid_symbol_count 记缺失",
            file=sys.stderr,
        )
        return None, None
    try:
        con = duckdb.connect(str(market_db), read_only=True)
    except Exception as exc:  # noqa: BLE001 - CLI 边界
        print(f"[data-health] 行情库打不开，按缺失处理: {exc}", file=sys.stderr)
        return None, None
    try:
        latest = con.execute("SELECT max(date) FROM daily_bars").fetchone()[0]
        valid = con.execute(
            "SELECT count(DISTINCT symbol) FROM daily_bars WHERE date = ?", [as_of]
        ).fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        print(f"[data-health] 行情库探针失败，按缺失处理: {exc}", file=sys.stderr)
        return None, None
    finally:
        con.close()
    latest_text = (
        latest.isoformat() if hasattr(latest, "isoformat") else (str(latest) if latest else None)
    )
    return latest_text, int(valid) if valid is not None else None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    as_of = (
        date.fromisoformat(args.as_of) if args.as_of else datetime.now().astimezone().date()
    )
    config = load_config(Path(args.config))
    market_db = REPO_ROOT / args.market_db
    latest_trade_date, valid_symbol_count = _db_facts(market_db, as_of)
    breadth_path = REPO_ROOT / args.breadth_artifact
    inputs: dict[str, object] = {
        "latest_trade_date": latest_trade_date,
        "valid_symbol_count": valid_symbol_count,
        "universe_snapshot": _read_json(args.universe_snapshot),
        "board_coverage": _read_json(args.board_coverage),
        "feature_snapshot": _read_json(args.feature_snapshot),
        "model_identity": _read_json(args.model_identity),
        "price_contract": price_contract_block(config),
        "breadth_artifact_present": breadth_path.exists(),
        "max_freshness_days": int(args.max_freshness_days),
        "min_expected_active_coverage": float(args.min_expected_active_coverage),
    }
    payload = build_data_health_artifact(as_of=as_of, **inputs)
    payload["market_db"] = str(args.market_db)
    payload["breadth_artifact"] = str(breadth_path)
    payload["inputs_provided"] = sorted(
        key
        for key, value in inputs.items()
        if value not in (None, {}, "")
        and key not in {"max_freshness_days", "min_expected_active_coverage"}
    )
    if args.print_only:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    target = write_data_health_artifact(payload, REPO_ROOT / args.out)
    print(f"[data-health] 已写: {target}")
    print(
        f"[data-health] as_of={payload['as_of']} status={payload['status']} "
        f"coverage_ratio={payload.get('coverage_ratio')} "
        f"missing_artifacts={payload.get('missing_artifacts')}"
    )
    print(
        "[data-health] 说明：status 只有在全项 ok 时才是 healthy；"
        "任何输入缺失都会被 S08 判 degraded/broken（缺失不得当健康），"
        "capture 会把该日如实标成不进 clean OOS。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
