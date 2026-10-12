"""CLI：trend 尾盘链路的选股质量滚动验证（改进计划 §4 选股质量验收）。

样本来自历史重建或真实观察（``capture_mode`` 必须逐行声明，两类分开计数）；
折边界、训练、校准、排序、判定全部复用线上那套权威实现。

退出码是**真实退出码**，不是打印出来的字面数字：

- ``0`` 质量门通过（还要求 ≥120 个完整测试日、每折 ≥30 日、每臂 ≥300 笔成熟成交）
- ``3`` 样本不足 → 记为 blocked，继续采集带时刻的分钟行情
- ``4`` 跑完了但质量门不过
- ``5`` 输入/训练/身份失败（包括要求 LightGBM 却没有原生 booster，以及
  ``--features`` 里出现四组可复现行情信息之外的列）
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.contracts.trend_strategy import (  # noqa: E402
    DEFAULT_TREND_CONTRACT,
    TrendContractError,
)
from stock_analyzer.feature.trend_candidate_contract import (  # noqa: E402
    assert_training_features,
)
from stock_analyzer.labels.tail_net_profit import (  # noqa: E402
    CAPTURE_OBSERVED,
    CAPTURE_REPLAYED,
)
from stock_analyzer.models.tail_net_profit_trainer import (  # noqa: E402
    MIN_TEST_FOLDS,
    TailTrainingError,
)
from stock_analyzer.research.tail_walk_forward import (  # noqa: E402
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    run_walk_forward,
)

RC_PASS = 0
RC_BLOCKED = 3
RC_GATE_FAILED = 4
RC_ERROR = 5


def _as_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, date, date, str]] = set()
    capture_modes: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"sample line is not an object: {payload!r}")
        for field in ("decision_date", "entry_date"):
            day = _as_date(payload.get(field))
            if day is None:
                raise ValueError(f"sample row has no parseable {field}: {payload!r}")
            payload[field] = day
        symbol = str(payload.get("symbol") or "").strip()
        if not symbol:
            raise ValueError(f"sample row has no symbol: {payload!r}")
        capture_mode = str(payload.get("capture_mode") or "").strip()
        if capture_mode not in {CAPTURE_OBSERVED, CAPTURE_REPLAYED}:
            raise ValueError(f"sample row has invalid capture_mode: {capture_mode!r}")
        key = (symbol, payload["decision_date"], payload["entry_date"], capture_mode)
        if key in seen:
            raise ValueError(f"duplicate sample decision key: {key!r}")
        seen.add(key)
        capture_modes.add(capture_mode)
        rows.append(payload)
    if not rows:
        raise ValueError(f"sample file {path} is empty")
    if len(capture_modes) != 1:
        raise ValueError(
            f"sample file mixes observed and replayed capture modes: {sorted(capture_modes)}"
        )
    return rows


def _load_calendar_sets(path: Path) -> tuple[list[date], list[date]]:
    """Read open exchange dates and optional dates with known decision coverage."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"trading calendar {path} needs a header row")
        date_column = next(
            (name for name in ("trade_date", "date") if name in reader.fieldnames), None
        )
        if date_column is None:
            raise ValueError("trading calendar needs a trade_date or date column")
        if "is_open" not in reader.fieldnames:
            raise ValueError("trading calendar needs an explicit is_open column")
        days: set[date] = set()
        decision_days: set[date] = set()
        for line_number, row in enumerate(reader, 2):
            raw = str(row.get(date_column) or "").strip()
            try:
                day = date.fromisoformat(raw[:10])
            except ValueError as exc:
                raise ValueError(
                    f"trading calendar line {line_number} has an invalid date: {raw!r}"
                ) from exc
            is_open = str(row.get("is_open", "")).strip().lower()
            if is_open in {"1", "true", "yes", "y", "open"}:
                days.add(day)
                if "is_decision_session" not in reader.fieldnames:
                    decision_days.add(day)
                else:
                    decision_flag = str(row.get("is_decision_session") or "").strip().lower()
                    if decision_flag in {"1", "true", "yes", "y", "open"}:
                        decision_days.add(day)
                    elif decision_flag not in {"0", "false", "no", "n", "closed"}:
                        raise ValueError(
                            f"trading calendar line {line_number} has an invalid "
                            f"is_decision_session value: {decision_flag!r}"
                        )
            elif is_open not in {"0", "false", "no", "n", "closed"}:
                raise ValueError(
                    f"trading calendar line {line_number} has an invalid is_open value: "
                    f"{is_open!r}"
                )
            elif "is_decision_session" in reader.fieldnames:
                decision_flag = str(row.get("is_decision_session") or "").strip().lower()
                if decision_flag in {"1", "true", "yes", "y", "open"}:
                    raise ValueError(
                        f"trading calendar line {line_number} marks a closed date "
                        "as a decision session"
                    )
                if decision_flag not in {"0", "false", "no", "n", "closed"}:
                    raise ValueError(
                        f"trading calendar line {line_number} has an invalid "
                        f"is_decision_session value: {decision_flag!r}"
                    )
    if not days:
        raise ValueError(f"trading calendar {path} contains no open sessions")
    if not decision_days:
        raise ValueError(f"trading calendar {path} contains no covered decision sessions")
    return sorted(days), sorted(decision_days)


def load_trading_calendar(path: Path) -> list[date]:
    """Return all explicitly open sessions from the exchange calendar."""
    return _load_calendar_sets(path)[0]


def load_decision_calendar(path: Path) -> list[date]:
    """Return open dates with known decision coverage, including true empty days."""
    return _load_calendar_sets(path)[1]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", required=True, help="JSONL：每行一个候选样本（标签+特征）")
    parser.add_argument(
        "--trading-calendar", required=True,
        help=("权威交易日历 CSV，包含 trade_date/is_open；可选 is_decision_session "
              "用于排除只有标签成熟、没有决策覆盖的末端交易日"),
    )
    parser.add_argument("--features", required=True, help="逗号分隔的特征名，训练与打分共用")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--training-commit", required=True)
    parser.add_argument("--runtime-commit", required=True)
    parser.add_argument("--feature-compute-version", type=int, required=True)
    parser.add_argument("--label-policy-id", required=True)
    parser.add_argument("--baseline-field", default="composite_score",
                        help="匹配基线的排序键（同一合格池、同一上限，只换排序）")
    parser.add_argument("--folds", type=int, default=MIN_TEST_FOLDS)
    parser.add_argument("--out", default="artifacts/research/tail_walk_forward.json")
    args = parser.parse_args(argv)

    feature_names = [name.strip() for name in str(args.features).split(",") if name.strip()]
    if not feature_names:
        print("--features 不能为空", file=sys.stderr)
        return RC_ERROR
    try:
        assert_training_features(feature_names)
    except ValueError as exc:
        # 先于读样本失败：特征清单不合法时解析再久也训不出可用的模型。
        print(f"特征不符合 trend 契约: {exc}", file=sys.stderr)
        return RC_ERROR

    try:
        samples_path = Path(args.samples)
        calendar_path = Path(args.trading_calendar)
        rows = load_rows(samples_path)
        trading_calendar = load_trading_calendar(calendar_path)
        decision_calendar = load_decision_calendar(calendar_path)
        input_manifest = {
            "samples": {
                "path": str(samples_path.resolve()),
                "sha256": _sha256_file(samples_path),
                "rows": len(rows),
                "capture_mode": str(rows[0]["capture_mode"]),
            },
            "trading_calendar": {
                "path": str(calendar_path.resolve()),
                "sha256": _sha256_file(calendar_path),
                "open_sessions": len(trading_calendar),
                "first_open_session": trading_calendar[0].isoformat(),
                "last_open_session": trading_calendar[-1].isoformat(),
                "decision_sessions": len(decision_calendar),
                "first_decision_session": decision_calendar[0].isoformat(),
                "last_decision_session": decision_calendar[-1].isoformat(),
            },
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"样本不可用: {exc}", file=sys.stderr)
        return RC_ERROR

    try:
        report = run_walk_forward(
            rows=rows,
            feature_names=feature_names,
            model_id=args.model_id,
            training_commit=args.training_commit,
            runtime_commit=args.runtime_commit,
            feature_compute_version=args.feature_compute_version,
            label_policy_id=args.label_policy_id,
            trading_calendar=trading_calendar,
            decision_calendar=decision_calendar,
            baseline_rank_field=args.baseline_field,
            folds=args.folds,
            contract=DEFAULT_TREND_CONTRACT,
        )
        report["input_manifest"] = input_manifest
    except (TailTrainingError, TrendContractError, ValueError) as exc:
        # 训练或身份失败就停：不降级、不换模型、不给出「看起来能跑完」的数字。
        # 但停也要留下可核对的工件 —— §2「模型降级影响多少股票」这一行需要的就是这个。
        # 这里只登记失败本身（status=blocked + blockers），不产出任何质量数字。
        failure = {
            "status": STATUS_BLOCKED,
            "metrics_computed": False,
            "blockers": [f"{type(exc).__name__}: {exc}"],
            "model_id": args.model_id,
            "training_commit": args.training_commit,
            "runtime_commit": args.runtime_commit,
            "requested_folds": args.folds,
            "blocked_fold_count": args.folds,
            "sample_file": str(args.samples),
            "input_manifest": input_manifest,
        }
        try:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(
                json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"失败工件已写入 {args.out}", file=sys.stderr)
        except OSError as write_exc:
            print(f"失败工件写盘失败（判定不变）: {write_exc}", file=sys.stderr)
        print(f"training/identity failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return RC_ERROR

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    if report.get("status") == STATUS_BLOCKED:
        # 阻塞状态也要留可核对的工件：否则 §2「模型降级影响多少」这一行永远没有分母，
        # 而台账里只能写一句"验证器退 5"。写盘失败不改判定，只加一条 stderr 说明。
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
        except OSError as exc:
            print(f"blocked 报告写盘失败（判定不变）: {exc}", file=sys.stderr)
        print(f"blocked: {report.get('blockers')}")
        print("选股质量验收记为阻塞，继续采集带时刻的分钟行情，不得用开盘回测代替")
        return RC_BLOCKED
    if report.get("status") != STATUS_COMPLETED:
        print(f"unexpected status: {report.get('status')!r}", file=sys.stderr)
        return RC_ERROR

    quality = report.get("quality") or {}
    print(f"folds = {report.get('fold_count')}")
    print(f"improvement_pp = {quality.get('improvement_pp')}")
    print(f"block bootstrap ci = [{quality.get('block_bootstrap', {}).get('ci_low')}, "
          f"{quality.get('block_bootstrap', {}).get('ci_high')}]")
    print(f"failed_gates = {quality.get('failed_gates')}")
    print(f"报告已写入 {out_path}")
    if quality.get("state") == "sample_insufficient":
        print("sample insufficient: 不作为质量失败，也不得晋升")
        return RC_BLOCKED
    return RC_PASS if quality.get("passed") else RC_GATE_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
