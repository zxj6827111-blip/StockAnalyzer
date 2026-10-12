#!/usr/bin/env python3
"""开发审计：逐日同池对比未校准模型 raw score 高分 / 低分 Top3。

该审计不输出校准概率，不更改正向校准 fail-closed 门，也不具备候选晋升资格。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import uuid
from datetime import date
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_CODE_FILES = (
    "scripts/audit_tail_score_direction.py",
    "src/stock_analyzer/research/tail_score_direction.py",
    "src/stock_analyzer/research/tail_ablation.py",
    "src/stock_analyzer/research/tail_walk_forward.py",
    "src/stock_analyzer/models/tail_net_profit_trainer.py",
    "src/stock_analyzer/models/fallback.py",
    "src/stock_analyzer/feature/trend_candidate_contract.py",
    "src/stock_analyzer/feature/engineer.py",
    "src/stock_analyzer/contracts/trend_strategy.py",
)

from stock_analyzer.contracts.trend_strategy import DEFAULT_TREND_CONTRACT  # noqa: E402
from stock_analyzer.feature.engineer import FEATURE_COMPUTE_VERSION  # noqa: E402
from stock_analyzer.feature.trend_candidate_contract import assert_training_features  # noqa: E402
from stock_analyzer.labels.tail_net_profit import CAPTURE_REPLAYED  # noqa: E402
from stock_analyzer.models.fallback import LogisticProbModel  # noqa: E402
from stock_analyzer.models.tail_net_profit_trainer import (  # noqa: E402
    DEFAULT_LIGHTGBM_PARAMS,
    KIND_LOGISTIC,
    TailModelSpec,
    TailTrainingError,
    _auc,
)
from stock_analyzer.research.tail_ablation import (  # noqa: E402
    EVENT_FEATURE_COLUMNS,
    holm_adjusted_p_values,
    stage2_arm_registry,
)
from stock_analyzer.research.tail_score_direction import (  # noqa: E402
    DEFAULT_DRAWS,
    DEFAULT_SEED,
    compare_score_directions,
    select_directional_top_k,
    summarize_picks,
)
from stock_analyzer.research.tail_walk_forward import (  # noqa: E402
    build_rolling_splits,
    select_training_features_by_correlation,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _code_identity() -> dict[str, Any]:
    digest = hashlib.sha256()
    for relative in _CODE_FILES:
        path = _ROOT / relative
        if not path.is_file():
            raise FileNotFoundError(f"required audit source file missing: {relative}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    commit_result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=_ROOT, capture_output=True, text=True, check=True
    )
    status_result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    package_versions: dict[str, str] = {}
    for package in ("numpy", "lightgbm"):
        try:
            package_versions[package] = version(package)
        except PackageNotFoundError:
            package_versions[package] = "unavailable"
    return {
        "head_commit": commit_result.stdout.strip(),
        "selected_source_sha256": digest.hexdigest(),
        "selected_source_files": list(_CODE_FILES),
        "working_tree_clean": not bool(status_result.stdout.strip()),
        "package_versions": package_versions,
        "python_version": sys.version.split()[0],
    }


def _day(value: Any) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError(f"invalid ISO date: {value!r}") from exc


def _load_samples(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    keys: set[tuple[str, date]] = set()
    modes: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"sample line {number} is not an object")
        row["decision_date"] = _day(row.get("decision_date"))
        row["entry_date"] = _day(row.get("entry_date"))
        symbol = str(row.get("symbol") or "").strip()
        mode = str(row.get("capture_mode") or "").strip()
        if not symbol or mode not in {"observed_snapshot", CAPTURE_REPLAYED}:
            raise ValueError(f"sample line {number} has invalid symbol/capture_mode")
        key = (symbol, row["decision_date"])
        if key in keys:
            raise ValueError(f"duplicate sample decision key at line {number}: {key!r}")
        keys.add(key)
        modes.add(mode)
        rows.append(row)
    if not rows:
        raise ValueError("sample file is empty")
    if modes != {CAPTURE_REPLAYED}:
        raise ValueError("direction audit accepts replayed development samples only")
    return rows


def _load_calendar(path: Path) -> tuple[list[date], list[date]]:
    import csv

    open_days: set[date] = set()
    decision_days: set[date] = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "is_open" not in reader.fieldnames:
            raise ValueError("calendar requires is_open")
        date_field = next((key for key in ("trade_date", "date") if key in reader.fieldnames), None)
        if date_field is None:
            raise ValueError("calendar requires trade_date or date")
        for number, row in enumerate(reader, 2):
            day = _day(row.get(date_field))
            flag = str(row.get("is_open") or "").strip().lower()
            is_open = flag in {"1", "true", "yes", "y", "open"}
            if not is_open and flag not in {"0", "false", "no", "n", "closed"}:
                raise ValueError(f"calendar line {number} has invalid is_open={flag!r}")
            if is_open:
                open_days.add(day)
                decision_flag = (
                    str(row.get("is_decision_session") or "").strip().lower()
                    if "is_decision_session" in reader.fieldnames
                    else "1"
                )
                if decision_flag in {"1", "true", "yes", "y", "open"}:
                    decision_days.add(day)
                elif decision_flag not in {"0", "false", "no", "n", "closed"}:
                    raise ValueError(
                        f"calendar line {number} has invalid is_decision_session="
                        f"{decision_flag!r}"
                    )
            elif "is_decision_session" in reader.fieldnames:
                decision_flag = str(row.get("is_decision_session") or "").strip().lower()
                if decision_flag not in {"0", "false", "no", "n", "closed"}:
                    raise ValueError(
                        f"calendar line {number} marks a closed date as a decision session"
                    )
    if not open_days or not decision_days:
        raise ValueError("calendar has no open days or decision sessions")
    return sorted(open_days), sorted(decision_days)


def _has_complete_features(row: dict[str, Any], names: tuple[str, ...]) -> bool:
    for name in names:
        value = row.get(name)
        if value is None:
            return False
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(number):
            return False
    return True


def _fit_raw_model(
    *,
    model_kind: str,
    train_rows: list[dict[str, Any]],
    feature_names: tuple[str, ...],
    spec: TailModelSpec,
) -> Any:
    matrix = [[float(row[name]) for name in feature_names] for row in train_rows]
    labels = [float(row["label"]) for row in train_rows]
    if len(set(labels)) < 2:
        raise TailTrainingError("train split is single-class")
    import numpy as np

    x = np.asarray(matrix, dtype=float)
    y = np.asarray(labels, dtype=float)
    if model_kind == KIND_LOGISTIC:
        model = LogisticProbModel(
            learning_rate=spec.learning_rate,
            epochs=spec.epochs,
            l2=spec.l2,
            seed=spec.seed,
        )
        model.fit(x, y)
        return model
    import lightgbm as lgb

    params = dict(DEFAULT_LIGHTGBM_PARAMS)
    rounds = int(params.pop("num_boost_round"))
    return lgb.train(params=params, train_set=lgb.Dataset(x, label=y), num_boost_round=rounds)


def _predict_raw(
    model: Any,
    kind: str,
    rows: list[dict[str, Any]],
    names: tuple[str, ...],
) -> list[float]:
    import numpy as np

    matrix = np.asarray([[float(row[name]) for name in names] for row in rows], dtype=float)
    predict = model.predict_proba if kind == KIND_LOGISTIC else model.predict
    values = [float(item) for item in predict(matrix)]
    if any(not math.isfinite(item) for item in values):
        raise TailTrainingError("raw model emitted a non-finite score")
    return values


def _audit_arm(
    *,
    arm: dict[str, Any],
    rows: list[dict[str, Any]],
    decision_days: list[date],
    folds: int,
    draws: int,
) -> dict[str, Any]:
    original_features = tuple(str(item) for item in arm["feature_names"])
    assert_training_features(original_features)
    splits = build_rolling_splits(
        decision_days,
        folds=folds,
        contract=DEFAULT_TREND_CONTRACT,
    )
    session_indices = {day: index for index, day in enumerate(decision_days)}
    daily: list[dict[str, Any]] = []
    fold_reports: list[dict[str, Any]] = []
    spec = TailModelSpec(kind=str(arm["model_kind"]))

    for fold_id, split in enumerate(splits, 1):
        selected_features, feature_selection = select_training_features_by_correlation(
            rows,
            original_features,
            train_dates=split.train_dates,
            date_field="decision_date",
            label_field="label",
            threshold=0.95,
            minimum_pair_rows=30,
        )
        train_set = set(split.train_dates)
        train_rows = [
            row for row in rows
            if row["decision_date"] in train_set
            and row.get("label") in (0, 1, 0.0, 1.0)
            and _has_complete_features(row, selected_features)
        ]
        if len(train_rows) < spec.min_train_samples:
            raise TailTrainingError(
                f"fold_{fold_id}_train_rows={len(train_rows)} < {spec.min_train_samples}"
            )
        model = _fit_raw_model(
            model_kind=str(arm["model_kind"]),
            train_rows=train_rows,
            feature_names=selected_features,
            spec=spec,
        )
        test_days = set(split.test_dates)
        fold_rows = [row for row in rows if row["decision_date"] in test_days]
        fold_auc_scores: list[float] = []
        fold_auc_labels: list[int] = []
        day_reports: list[str] = []
        for day in split.test_dates:
            pool = [row for row in fold_rows if row["decision_date"] == day]
            scoreable = [row for row in pool if _has_complete_features(row, selected_features)]
            raw_scores = _predict_raw(model, str(arm["model_kind"]), scoreable, selected_features)
            scores = {
                str(row["symbol"]): score
                for row, score in zip(scoreable, raw_scores, strict=True)
            }
            ranked = select_directional_top_k(scoreable, scores, cap=3)
            daily_row: dict[str, Any] = {
                "trade_date": day.isoformat(),
                "fold_id": fold_id,
                "session_index": session_indices[day],
                "pool_size": len(pool),
                "scored_pool_size": len(scoreable),
                "unscorable_rows": len(pool) - len(scoreable),
                "high": summarize_picks(scoreable, ranked["high"]),
                "low": summarize_picks(scoreable, ranked["low"]),
            }
            daily.append(daily_row)
            day_reports.append(day.isoformat())
            for row, score in zip(scoreable, raw_scores, strict=True):
                if row.get("label") in (0, 1, 0.0, 1.0):
                    fold_auc_scores.append(score)
                    fold_auc_labels.append(int(row["label"]))
        fold_reports.append({
            "fold_id": fold_id,
            "split": split.as_dict(),
            "training_rows": len(train_rows),
            "feature_selection": feature_selection,
            "test_days": day_reports,
            "test_scored_rows": sum(
                row["scored_pool_size"] for row in daily if row["fold_id"] == fold_id
            ),
            "raw_test_auc": _auc(fold_auc_scores, fold_auc_labels),
        })

    result = compare_score_directions(daily, draws=draws, seed=DEFAULT_SEED)
    return {
        "status": "completed",
        "interpretation": "uncalibrated_raw_score_rank_only",
        "folds": fold_reports,
        "daily": daily,
        "direction_comparison": result,
    }


def _atomic_write_new(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(payload, encoding="utf-8")
        try:
            os.link(temp, path)
        except FileExistsError as exc:
            raise FileExistsError(f"refusing to overwrite existing report: {path}") from exc
    finally:
        temp.unlink(missing_ok=True)


def _apply_global_holm_adjustment(
    arms: list[dict[str, Any]],
    p_values: dict[str, float | None],
    family_size: int,
) -> None:
    adjusted = holm_adjusted_p_values(p_values, family_size=family_size)
    for arm in arms:
        audit = arm.get("direction_audit")
        if not isinstance(audit, dict):
            continue
        comparison = audit["direction_comparison"]
        metrics = comparison["metrics"]
        arm_id = str(arm["arm_id"])
        metrics["net_profit_rate"]["holm_p_value"] = adjusted[f"{arm_id}:net_profit_rate"]
        metrics["mean_net_return"]["holm_p_value"] = adjusted[f"{arm_id}:mean_net_return"]
        comparison["holm"] = {
            "method": "step_down",
            "family_size": family_size,
            "scope": "all registered arm-by-metric tests; blocked tests contribute p=1",
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", required=True)
    parser.add_argument("--trading-calendar", required=True)
    parser.add_argument("--out", default="artifacts/research/tail_score_direction_audit.json")
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--draws", type=int, default=DEFAULT_DRAWS)
    parser.add_argument("--prior-stage2-report", default="")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    samples_path = Path(args.samples).expanduser().resolve()
    calendar_path = Path(args.trading_calendar).expanduser().resolve()
    if args.folds != 4:
        print("direction audit requires the preregistered 4 test folds", file=sys.stderr)
        return 5
    try:
        if int(args.draws) < 1:
            raise ValueError("draws must be positive")
        rows = _load_samples(samples_path)
        trading_calendar, decision_calendar = _load_calendar(calendar_path)
        sample_dates = {row["decision_date"] for row in rows}
        if not sample_dates.issubset(set(decision_calendar)):
            raise ValueError("sample decision dates fall outside decision calendar")
        if not set(decision_calendar).issubset(set(trading_calendar)):
            raise ValueError("decision calendar contains a closed date")
        splits = build_rolling_splits(
            decision_calendar,
            folds=4,
            contract=DEFAULT_TREND_CONTRACT,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"direction audit inputs blocked: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5

    arms = stage2_arm_registry()
    family_size = len(arms) * 2
    p_values: dict[str, float | None] = {}
    for arm in arms:
        arm_id = str(arm["arm_id"])
        if arm.get("blockers") or any(
            str(name) in EVENT_FEATURE_COLUMNS for name in arm.get("feature_names", [])
        ):
            arm["status"] = "blocked"
            arm["blockers"] = list(arm.get("blockers") or []) or [
                "event_feature_pit_not_proven"
            ]
            p_values[f"{arm_id}:net_profit_rate"] = None
            p_values[f"{arm_id}:mean_net_return"] = None
            continue
        try:
            arm["direction_audit"] = _audit_arm(
                arm=arm,
                rows=rows,
                decision_days=decision_calendar,
                folds=4,
                draws=int(args.draws),
            )
            arm["status"] = "completed"
            comparison = arm["direction_audit"]["direction_comparison"]
            for metric in ("net_profit_rate", "mean_net_return"):
                p_values[f"{arm_id}:{metric}"] = comparison["metrics"][metric][
                    "raw_p_one_sided_low_gt_high"
                ]
        except Exception as exc:  # one registered arm cannot invalidate or hide others
            arm["status"] = "blocked"
            arm["blockers"] = [f"{type(exc).__name__}: {exc}"]
            arm["direction_audit"] = None
            p_values[f"{arm_id}:net_profit_rate"] = None
            p_values[f"{arm_id}:mean_net_return"] = None

    _apply_global_holm_adjustment(arms, p_values, family_size)

    report = {
        "schema": "tail_score_direction_audit.v1",
        "status": "completed",
        "scope": "development_exploration_only",
        "evidence_mode": "replayed_development_un_calibrated_raw_score_rank",
        "release_eligible": False,
        "feature_compute_version": FEATURE_COMPUTE_VERSION,
        "code_identity": _code_identity(),
        "question": "same-day same-scoreable-pool low-score Top3 vs high-score Top3",
        "selection": {
            "top_k": 3,
            "tie_break": "symbol_ascending_for_both_directions",
            "pool": "same fold-specific complete-feature candidates for high and low",
            "unmatured_selected_rows": (
                "remain selected; no replacement; excluded from mature-return denominator"
            ),
        },
        "statistics": {
            "draws": int(args.draws),
            "seed": DEFAULT_SEED,
            "bit_generator": "PCG64",
            "main_block_length_sessions": 10,
            "sensitivity_block_lengths": [5, 20],
            "direction": "low_score_minus_high_score",
            "holm_method": "step_down",
            "holm_family_size": family_size,
            "holm_family_scope": (
                "all 12 registered arms x 2 metrics; blocked tests contribute p=1"
            ),
        },
        "inputs": {
            "samples": {
                "path": str(samples_path), "sha256": _sha256(samples_path), "rows": len(rows)
            },
            "trading_calendar": {
                "path": str(calendar_path), "sha256": _sha256(calendar_path),
                "open_sessions": len(trading_calendar),
                "decision_sessions": len(decision_calendar),
                "first_decision": decision_calendar[0].isoformat(),
                "last_decision": decision_calendar[-1].isoformat(),
            },
        },
        "walk_forward": {
            "folds": [split.as_dict() for split in splits],
            "embargo_sessions": max(1, int(DEFAULT_TREND_CONTRACT.holding_days)),
            "feature_deduplication": "train_window_only_abs_correlation_ge_0.95",
        },
        "arms": arms,
        "notes": [
            "raw scores are not calibrated probabilities and cannot enter the serving manifest",
            "the existing calibration direction gate remains unchanged and fail-closed",
            (
                "replayed 2025 data is already inspected development evidence, "
                "never independent confirmation"
            ),
            "directional comparison is exploratory and cannot select or promote a candidate",
        ],
    }
    if str(args.prior_stage2_report).strip():
        prior = Path(args.prior_stage2_report).expanduser().resolve()
        try:
            report["prior_stage2_report"] = {
                "path": str(prior),
                "sha256": _sha256(prior),
            }
        except OSError as exc:
            print(f"prior Stage 2 report cannot be hashed: {exc}", file=sys.stderr)
            return 5
    output = Path(args.out).expanduser()
    try:
        _atomic_write_new(output, json.dumps(report, ensure_ascii=False, indent=2, default=str))
    except (OSError, FileExistsError) as exc:
        print(f"direction audit report not written: {exc}", file=sys.stderr)
        return 5
    if not args.quiet:
        print(json.dumps({
            "out": str(output.resolve()),
            "arms": {arm["arm_id"]: arm["status"] for arm in arms},
            "holm_family_size": family_size,
            "release_eligible": False,
        }, ensure_ascii=False, indent=2))
    return 0 if any(arm["status"] == "completed" for arm in arms) else 3


if __name__ == "__main__":
    raise SystemExit(main())
