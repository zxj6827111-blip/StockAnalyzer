"""Week5 Phase 0 B4：受影响 outcome 重建 backfill + 新旧标签差异报告 runner。

计划依据：docs/week5_backtest_remediation_plan_20260831.md §3.2、
审计 docs/week5_phase0_audit_20260831.md §9/§10（NAS 运行窗口项）。

流程（幂等，可重复执行）：
1. 从 sample store 取候选 outcome（默认 PENDING + 成熟态都重建，或 --only-matured）；
2. 用 ``LearningBackfillEngine.repair_backfill`` 以当前 label policy（soft_label /
   next_tradable_open）重建受影响 outcome；
3. 差异报告双口径：
   - ``label_policy_diff_migration``（主口径）：重建前 outcome × 旧 policy vs
     重建后 outcome × 新 policy——真实迁移差，changed_label_rows 为影响面；
   - ``label_policy_diff_same_batch``（参考）：同批 outcome × 双 policy——只捕捉
     conflict 策略/schema 版本差，不捕捉入场 basis 引起的 MFE/MAE 差（偏保守）；
4. 全部产物（backfill payload + 双差异报告 + 输入快照）落盘 JSON，不覆盖
   已有报告文件（时间戳命名）。

用法（NAS 容器内）：
    python scripts/week5_phase0_backfill_runner.py \
        --output-dir /app/artifacts/phase0_backfill [--only-matured] [--limit 500]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from stock_analyzer.config import get_config  # noqa: E402
from stock_analyzer.learning.backfill import LearningBackfillEngine  # noqa: E402
from stock_analyzer.learning.label_policy_registry import (  # noqa: E402
    build_label_policy_record,
)
from stock_analyzer.learning.sample_schema import MaturityStatus, OutcomeRecord  # noqa: E402
from stock_analyzer.learning.sample_store import SampleStore  # noqa: E402
from stock_analyzer.models.trainer import (  # noqa: E402
    build_label_policy_diff_report,
    build_label_policy_migration_report,
)
from stock_analyzer.runtime.service import StockAnalyzerService  # noqa: E402


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _is_lock_conflict(exc: BaseException) -> bool:
    text = f"{exc.__class__.__name__}: {exc}".lower()
    return "conflicting lock" in text or "could not set lock" in text


def _run_with_lock_retry(
    fn: Callable[[], Any],
    *,
    label: str,
    attempts: int = 12,
    base_sleep: float = 20.0,
) -> Any:
    """DuckDB 单写者冲突重试：API 服务进程与调度器会周期性持写锁，
    runner 侧必须 backoff 重试而不是整体崩溃（2026-08-31 首跑即栽在此）。"""

    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if not _is_lock_conflict(exc):
                raise
            if attempt >= attempts:
                raise
            sleep_for = min(base_sleep * attempt, 180.0)
            print(
                f"[phase0-backfill] {label}: lock conflict "
                f"(attempt {attempt}/{attempts}); retry in {sleep_for:.0f}s",
                flush=True,
            )
            time.sleep(sleep_for)
    raise RuntimeError("unreachable")


def _list_outcomes_by_ids(
    store: SampleStore,
    snapshot_ids: list[str],
    *,
    batch_size: int = 2000,
) -> dict[str, OutcomeRecord]:
    """分批 list_outcomes：84,900 条逐条 get_outcome 会开 8 万次读写连接并放大
    锁冲突窗口；这里收敛为每批一次查询，只读用途下锁暴露最小化。"""

    rows: dict[str, OutcomeRecord] = {}
    for start in range(0, len(snapshot_ids), batch_size):
        chunk = snapshot_ids[start : start + batch_size]
        for outcome in store.list_outcomes(snapshot_ids=chunk):
            rows[outcome.snapshot_id] = outcome
    return rows


def _select_outcome_snapshot_ids(
    service: StockAnalyzerService,
    *,
    only_matured: bool,
    limit: int,
    offset: int,
) -> list[str]:
    """选择需重建的 outcome：默认全量（PENDING 会自然跳过不成熟样本）。

    大库下全量 list_outcomes 一次装载 8 万+ 记录会撑爆容器内存
    （2026-09-01 实测 84,900 条直接 OOMKilled），故支持 offset+limit 分块；
    分块顺序稳定（outcome_updated_at, snapshot_id），断点续跑幂等。
    """

    outcomes = [
        outcome
        for outcome in service._sample_store.list_outcomes()
        if outcome.maturity_status != MaturityStatus.PENDING or not only_matured
    ]
    outcomes.sort(key=lambda item: (item.outcome_updated_at, item.snapshot_id))
    if offset > 0:
        outcomes = outcomes[offset:]
    if limit > 0:
        outcomes = outcomes[:limit]
    return [outcome.snapshot_id for outcome in outcomes]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        default=str(REPO_ROOT / "artifacts" / "phase0_backfill"),
        help="报告落盘目录（默认 artifacts/phase0_backfill）",
    )
    parser.add_argument(
        "--only-matured",
        action="store_true",
        help="仅重建非 PENDING 的成熟 outcome（默认全量，PENDING 自然跳过）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="最多重建 N 条（0=不限制）；分批执行时按 outcome_updated_at 升序截取",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="跳过排序后的前 N 条（与 --limit 配合分块续跑）",
    )
    parser.add_argument(
        "--fetch-workers",
        type=int,
        default=1,
        help="日线并行预取线程数（i7-7700 8 核建议 6；写库仍顺序，规避 DuckDB 单写者）",
    )
    args = parser.parse_args()

    started_at = _now_iso()
    config = get_config()
    service = StockAnalyzerService(config=config)

    engine = LearningBackfillEngine(
        config=config,
        provider=service._provider,
        sample_store=service._sample_store,
        feature_schema_registry=service._feature_schema_registry,
        label_policy_registry=service._label_policy_registry,
    )

    snapshot_ids = _run_with_lock_retry(
        lambda: _select_outcome_snapshot_ids(
            service, only_matured=args.only_matured, limit=args.limit, offset=args.offset
        ),
        label="select_candidates",
    )
    print(
        f"[phase0-backfill] candidate outcomes: {len(snapshot_ids)} "
        f"(offset={args.offset}, limit={args.limit})",
        flush=True,
    )

    # 输入快照：重建前的 outcome 原样留存（供差异报告与审计追溯）。
    # 批量分批查询 + 锁冲突重试，避免逐条 get_outcome 放大锁窗口。
    before_map = _run_with_lock_retry(
        lambda: _list_outcomes_by_ids(service._sample_store, snapshot_ids),
        label="before_snapshot",
    )
    before_payload = [
        before_map[snapshot_id].model_dump(mode="json")
        for snapshot_id in snapshot_ids
        if snapshot_id in before_map
    ]

    result: dict[str, Any] = {
        "started_at": started_at,
        "ok": False,
        "mode": "week5_phase0_backfill",
        "candidate_count": len(snapshot_ids),
        "only_matured": args.only_matured,
        "limit": args.limit,
        "offset": args.offset,
    }
    if snapshot_ids:
        repair_payload = _run_with_lock_retry(
            lambda: engine.repair_backfill(
                snapshot_ids=snapshot_ids,
                source="week5_phase0_backfill_runner",
                fetch_workers=args.fetch_workers,
            ),
            label="repair_backfill",
        )
        result["repair"] = repair_payload
        result["ok"] = bool(repair_payload.get("ok", False))
        result["errors"] = list(repair_payload.get("errors", []) or [])
    else:
        result["repair"] = {"ok": True, "skipped": "no_candidates"}
        result["ok"] = True
        result["errors"] = []

    # 差异报告：v1 历史 policy（close/bar_shape_heuristic 时代）vs 当前 policy。
    active_policy = service._label_policy_registry.register_from_config(
        config.labels, schema_version="2"
    )
    legacy_policy = build_label_policy_record(
        label_name=config.labels.primary,
        take_profit_pct=config.labels.take_profit_pct,
        stop_loss_pct=config.labels.stop_loss_pct,
        horizon_days=config.labels.horizon_days,
        price_basis="close",
        exclude_untradable=config.labels.exclude_untradable,
        conflict_policy="bar_shape_heuristic",
        conflict_soft_label_value=config.labels.conflict_soft_label_value,
        schema_version="1",
    )
    after_map = _run_with_lock_retry(
        lambda: _list_outcomes_by_ids(service._sample_store, snapshot_ids),
        label="after_snapshot",
    )
    comparable = [
        after_map[snapshot_id] for snapshot_id in snapshot_ids if snapshot_id in after_map
    ]
    # 口径一（参考）：同批 outcome × 双 policy——只捕捉 conflict 策略/schema
    # 版本差，不捕捉入场 basis 变化引起的 MFE/MAE 差（结果偏保守）。
    diff_report = build_label_policy_diff_report(
        outcomes=comparable,
        old_policy=legacy_policy,
        new_policy=active_policy,
    )
    result["label_policy_diff_same_batch"] = diff_report
    # 口径二（主口径，plan §3.2"新旧标签差异"）：重建前 outcome（旧 basis 时代
    # 的真实 MFE/MAE）× 旧 policy vs 重建后 outcome × 新 policy——真实迁移差。
    before_outcomes_typed = [
        before_map[snapshot_id] for snapshot_id in snapshot_ids if snapshot_id in before_map
    ]
    migration_report = build_label_policy_migration_report(
        before_outcomes=before_outcomes_typed,
        after_outcomes=comparable,
        old_policy=legacy_policy,
        new_policy=active_policy,
    )
    result["label_policy_diff_migration"] = migration_report
    result["new_label_policy_id"] = active_policy.label_policy_id
    result["legacy_label_policy_id"] = legacy_policy.label_policy_id
    result["finished_at"] = _now_iso()

    # 落盘（时间戳命名，不覆盖历史报告）。
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_path = output_dir / f"phase0_backfill_report_{stamp}.json"
    input_snapshot_path = output_dir / f"phase0_backfill_inputs_{stamp}.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    input_snapshot_path.write_text(
        json.dumps(before_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"[phase0-backfill] ok={result['ok']} "
        f"migration_changed={migration_report.get('changed_label_rows')} "
        f"same_batch_changed={diff_report.get('changed_label_rows')} "
        f"report={report_path}",
        flush=True,
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
