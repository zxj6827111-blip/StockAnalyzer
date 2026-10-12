#!/usr/bin/env python3
"""Run the fixed Stage 2 model × feature matrix without hyperparameter search."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.contracts.trend_strategy import DEFAULT_TREND_CONTRACT  # noqa: E402
from stock_analyzer.feature.engineer import FEATURE_COMPUTE_VERSION  # noqa: E402
from stock_analyzer.labels.tail_net_profit import tail_label_policy_record  # noqa: E402
from stock_analyzer.models.tail_net_profit_trainer import (  # noqa: E402
    DEFAULT_LIGHTGBM_PARAMS,
    KIND_LIGHTGBM,
    KIND_LOGISTIC,
    TailModelSpec,
    TailTrainingError,
)
from stock_analyzer.research.tail_ablation import (  # noqa: E402
    holm_adjusted_p_values,
    select_stage2_candidate,
    stage2_arm_registry,
)
from stock_analyzer.research.tail_walk_forward import run_walk_forward  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_tree_identity() -> dict[str, Any]:
    """Hash executable source/config contents for reproducible dirty-tree exploration."""
    paths = set((_ROOT / "src").rglob("*.py"))
    paths.update((_ROOT / "scripts").rglob("*.py"))
    config_path = _ROOT / "config" / "default.yaml"
    if config_path.is_file():
        paths.add(config_path)
    digest = hashlib.sha256()
    source_paths = sorted(path for path in paths if path.is_file())
    for path in source_paths:
        relative = path.relative_to(_ROOT).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return {"sha256": digest.hexdigest(), "files": len(source_paths)}


def _day(value: Any) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError(f"invalid ISO trading date: {value!r}") from exc


def _load_samples(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    modes: set[str] = set()
    keys: set[tuple[str, date, date, str]] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"sample line {line_number} is not an object")
        row["decision_date"] = _day(row.get("decision_date"))
        row["entry_date"] = _day(row.get("entry_date"))
        symbol = str(row.get("symbol") or "").strip()
        mode = str(row.get("capture_mode") or "").strip()
        if not symbol or mode not in {"observed_snapshot", "replayed_recompute"}:
            raise ValueError(f"sample line {line_number} lacks symbol or valid capture_mode")
        key = (symbol, row["decision_date"], row["entry_date"], mode)
        if key in keys:
            raise ValueError(f"duplicate sample key at line {line_number}: {key!r}")
        keys.add(key)
        modes.add(mode)
        rows.append(row)
    if not rows:
        raise ValueError(f"sample file is empty: {path}")
    if len(modes) != 1:
        raise ValueError(f"sample file mixes capture modes: {sorted(modes)}")
    return rows


def _load_calendar(path: Path) -> tuple[list[date], list[date]]:
    days: set[date] = set()
    decision_days: set[date] = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "is_open" not in reader.fieldnames:
            raise ValueError("calendar CSV requires an is_open column")
        date_field = next(
            (name for name in ("trade_date", "date") if name in reader.fieldnames),
            None,
        )
        if date_field is None:
            raise ValueError("calendar CSV requires trade_date or date")
        for line_number, row in enumerate(reader, 2):
            day = _day(row.get(date_field))
            flag = str(row.get("is_open") or "").strip().lower()
            if flag in {"1", "true", "yes", "y", "open"}:
                days.add(day)
                if "is_decision_session" not in reader.fieldnames:
                    decision_days.add(day)
                else:
                    decision_flag = str(row.get("is_decision_session") or "").strip().lower()
                    if decision_flag in {"1", "true", "yes", "y", "open"}:
                        decision_days.add(day)
                    elif decision_flag not in {"0", "false", "no", "n", "closed"}:
                        raise ValueError(
                            f"calendar line {line_number} has invalid "
                            f"is_decision_session={decision_flag!r}"
                        )
            elif flag not in {"0", "false", "no", "n", "closed"}:
                raise ValueError(f"calendar line {line_number} has invalid is_open={flag!r}")
            elif "is_decision_session" in reader.fieldnames:
                decision_flag = str(row.get("is_decision_session") or "").strip().lower()
                if decision_flag in {"1", "true", "yes", "y", "open"}:
                    raise ValueError(
                        f"calendar line {line_number} marks a closed date as a decision session"
                    )
                if decision_flag not in {"0", "false", "no", "n", "closed"}:
                    raise ValueError(
                        f"calendar line {line_number} has invalid "
                        f"is_decision_session={decision_flag!r}"
                    )
    if not days:
        raise ValueError("calendar CSV has no open sessions")
    if not decision_days:
        raise ValueError("calendar CSV has no covered decision sessions")
    return sorted(days), sorted(decision_days)


def _training_identity() -> tuple[str, str]:
    from stock_analyzer.alpha_v2.validation.runtime_identity import (
        resolve_runtime_code_identity,
    )

    identity = resolve_runtime_code_identity(
        _ROOT,
        validation_mode="production",
        require_build_identity=False,
    )
    commit = str(identity.code_commit or "").strip()
    if not identity.identity_verified or not commit or commit == "unknown":
        raise TailTrainingError(f"training_runtime_identity_unverified:{identity.violations!r}")
    return commit, str(identity.code_commit_source or identity.identity_source or "")


def _lightgbm_trainer() -> tuple[Any, str]:
    import lightgbm as lgb

    def train(features: Any, labels: Any, declared: dict[str, Any]) -> Any:
        params = dict(declared)
        rounds = int(params.pop("num_boost_round"))
        return lgb.train(
            params=params,
            train_set=lgb.Dataset(features, label=labels),
            num_boost_round=rounds,
        )

    return train, str(lgb.__version__)


def _load_lightgbm() -> tuple[Any | None, str]:
    try:
        return _lightgbm_trainer()
    except Exception as exc:  # noqa: BLE001 - keep the registered arm blocked with a cause
        return None, f"{type(exc).__name__}: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", required=True)
    parser.add_argument("--trading-calendar", required=True)
    parser.add_argument("--out", default="artifacts/research/tail_stage2_ablation.json")
    parser.add_argument("--baseline-field", default="avg_turnover_20")
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    samples_path = Path(args.samples).expanduser()
    calendar_path = Path(args.trading_calendar).expanduser()
    try:
        rows = _load_samples(samples_path)
        trading_calendar, decision_calendar = _load_calendar(calendar_path)
        training_commit, commit_source = _training_identity()
    except (OSError, ValueError, json.JSONDecodeError, TailTrainingError) as exc:
        print(f"Stage 2 input/identity blocked: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5

    booster_trainer, lightgbm_version_or_error = _load_lightgbm()
    label_policy_id = tail_label_policy_record(DEFAULT_TREND_CONTRACT).label_policy_id
    arms = stage2_arm_registry()
    p_values: dict[str, float | None] = {}
    for arm in arms:
        arm_id = str(arm["arm_id"])
        if arm["blockers"]:
            arm["status"] = "blocked"
            p_values[arm_id] = None
            continue
        if arm["model_kind"] == KIND_LIGHTGBM and booster_trainer is None:
            arm["status"] = "blocked"
            arm["blockers"] = [f"lightgbm_unavailable:{lightgbm_version_or_error}"]
            p_values[arm_id] = None
            continue
        try:
            spec = TailModelSpec(kind=str(arm["model_kind"]))
            result = run_walk_forward(
                rows=rows,
                feature_names=tuple(str(item) for item in arm["feature_names"]),
                model_id=f"stage2-{arm['config_id']}-{arm['model_kind']}",
                training_commit=training_commit,
                runtime_commit=training_commit,
                feature_compute_version=int(FEATURE_COMPUTE_VERSION),
                label_policy_id=label_policy_id,
                trading_calendar=trading_calendar,
                decision_calendar=decision_calendar,
                spec=spec,
                contract=DEFAULT_TREND_CONTRACT,
                baseline_rank_field=str(args.baseline_field),
                folds=int(args.folds),
                deduplicate_correlated_features=True,
                correlation_threshold=0.95,
                correlation_minimum_pair_rows=30,
                booster_trainer=booster_trainer if arm["model_kind"] == KIND_LIGHTGBM else None,
                verify_artifact_roundtrip=True,
                # Stage 2 may inspect a short replayed development window. The quality
                # evaluator still applies the fixed 120-session/30-per-fold/300-fill
                # gates, so underpowered arms can never become the selected candidate.
                allow_underpowered_development=True,
            )
            arm["status"] = str(result.get("status") or "blocked")
            arm["report"] = result
            if result.get("status") == "completed":
                main_bootstrap = ((result.get("quality") or {}).get("block_bootstrap") or {}).get(
                    "main"
                ) or {}
                p_values[arm_id] = main_bootstrap.get("p_one_sided_gt_zero")
            else:
                arm["blockers"] = list(result.get("blockers") or [])
                p_values[arm_id] = None
        except Exception as exc:  # noqa: BLE001 - one failed arm cannot alter other arms
            arm["status"] = "blocked"
            arm["blockers"] = [f"{type(exc).__name__}: {exc}"]
            p_values[arm_id] = None

    adjusted = holm_adjusted_p_values(p_values, family_size=len(arms))
    for arm in arms:
        arm_id = str(arm["arm_id"])
        arm["exploratory_p_one_sided"] = p_values.get(arm_id)
        arm["holm_adjusted_p"] = adjusted.get(arm_id, 1.0)
        if arm.get("report"):
            arm["report"]["exploratory_holm_family_size"] = len(arms)
            arm["report"]["exploratory_holm_adjusted_p"] = adjusted.get(arm_id, 1.0)

    candidate_selection = select_stage2_candidate(arms)

    report = {
        "schema": "tail_stage2_ablation.v1",
        "status": "completed" if all(arm["status"] != "registered" for arm in arms) else "blocked",
        "scope": "development_exploration_only",
        "evidence_mode": "replayed_development; underpowered metrics remain ineligible",
        "release_eligible": False,
        "samples": {
            "path": str(samples_path.resolve()),
            "sha256": _sha256(samples_path),
            "rows": len(rows),
            "capture_mode": rows[0]["capture_mode"],
        },
        "trading_calendar": {
            "path": str(calendar_path.resolve()),
            "sha256": _sha256(calendar_path),
            "open_sessions": len(trading_calendar),
            "first": trading_calendar[0].isoformat(),
            "last": trading_calendar[-1].isoformat(),
            "decision_sessions": len(decision_calendar),
            "first_decision_session": decision_calendar[0].isoformat(),
            "last_decision_session": decision_calendar[-1].isoformat(),
        },
        "runtime_identity": {
            "training_commit": training_commit,
            "commit_source": commit_source,
            "source_tree": _source_tree_identity(),
            "working_tree_dirty_is_not_release_identity": True,
        },
        "feature_deduplication": {
            "threshold_abs_correlation": 0.95,
            "scope": "current_fold_train_only",
            "minimum_pair_rows": 30,
            "keep_rule": "higher completeness, ties use frozen feature order",
        },
        "models": {
            KIND_LOGISTIC: {"status": "available", "params": "fixed LogisticProbModel defaults"},
            KIND_LIGHTGBM: {
                "status": "available" if booster_trainer is not None else "unavailable",
                "version_or_error": lightgbm_version_or_error,
                "params": dict(DEFAULT_LIGHTGBM_PARAMS),
            },
        },
        "holm": {
            "method": "step_down",
            "family_size": len(arms),
            "blocked_arm_p_value_for_adjustment": 1.0,
        },
        "arms": arms,
        "candidate_selection": candidate_selection,
        "notes": [
            "all 12 registered arms are reported; no hyperparameter search is performed",
            (
                "moneyflow/top_list remains blocked until historical point-in-time "
                "availability is proven"
            ),
            "replayed historical rows are development evidence, not independent confirmation",
            (
                "underpowered development exploration can train and report metrics, but "
                "cannot pass the fixed quality sample-size gates or candidate selection"
            ),
            (
                "per-arm quality outcomes do not satisfy the separate three-control "
                "confirmatory protocol"
            ),
        ],
    }
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if not args.quiet:
        print(
            json.dumps(
                {
                    "out": str(out),
                    "arms": {arm["arm_id"]: arm["status"] for arm in arms},
                    "lightgbm": report["models"][KIND_LIGHTGBM],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0 if report["status"] == "completed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
