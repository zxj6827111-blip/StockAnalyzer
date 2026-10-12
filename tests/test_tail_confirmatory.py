from datetime import date, timedelta

from stock_analyzer.research.tail_confirmatory import (
    CONFIRMATORY_ARMS,
    ConfirmatorySession,
    estimate_development_power_sample_size,
    evaluate_independent_confirmation,
)


def _sessions() -> tuple[list[date], list[ConfirmatorySession]]:
    calendar = [date(2026, 10, 11) + timedelta(days=index) for index in range(120)]
    rows: list[ConfirmatorySession] = []
    for index, day in enumerate(calendar):
        fold_id, session_index = divmod(index, 30)
        for arm in CONFIRMATORY_ARMS:
            if arm == "candidate":
                returns = (0.02, 0.01, -0.01)
                daily_return = 0.001
            else:
                returns = (0.01, -0.01, -0.01)
                daily_return = 0.0001
            rows.append(
                ConfirmatorySession(
                    trade_date=day,
                    arm_id=arm,
                    fold_id=fold_id,
                    session_index=session_index,
                    net_returns=returns,
                    account_daily_return=daily_return,
                )
            )
    return calendar, rows


def test_power_sample_size_uses_development_blocks_and_minimum_floor() -> None:
    _, rows = _sessions()
    candidate = [row for row in rows if row.arm_id == "candidate"]
    control = [row for row in rows if row.arm_id == "legacy_same_pool"]
    report = estimate_development_power_sample_size(candidate, control)
    assert report["status"] == "estimated"
    assert report["development_only"] is True
    assert report["required_sessions"] >= 120
    assert report["complete_development_blocks"] == 12


def test_confirmatory_protocol_passes_fixed_eight_test_family() -> None:
    calendar, rows = _sessions()
    report = evaluate_independent_confirmation(
        sessions=rows,
        trading_calendar=calendar,
        candidate_frozen_date=date(2026, 10, 10),
        unopened_window_attested=True,
        candidate_artifact_sha256="a" * 64,
        required_power_sessions=120,
        bootstrap_draws=500,
    )
    assert report["state"] == "quality_passed"
    assert report["scope"] == "statistical_protocol_only"
    assert report["release_eligible"] is False
    assert len(report["tests"]) == 8
    assert all(test["passed_all_sensitivity_gates"] for test in report["tests"].values())


def test_unopened_window_and_candidate_freeze_are_hard_gates() -> None:
    calendar, rows = _sessions()
    report = evaluate_independent_confirmation(
        sessions=rows,
        trading_calendar=calendar,
        candidate_frozen_date=date(2026, 10, 10),
        unopened_window_attested=False,
        candidate_artifact_sha256="a" * 64,
        required_power_sessions=120,
        bootstrap_draws=100,
    )
    assert report["state"] == "engineering_blocked"
    assert "independent_window_not_attested_unopened" in report["blockers"]

    report = evaluate_independent_confirmation(
        sessions=rows,
        trading_calendar=calendar,
        candidate_frozen_date=date(2026, 10, 11),
        unopened_window_attested=True,
        candidate_artifact_sha256="a" * 64,
        required_power_sessions=120,
        bootstrap_draws=100,
    )
    assert "confirmation_window_does_not_start_after_candidate_freeze" in report["blockers"]


def test_missing_arm_session_blocks_instead_of_shrinking_the_calendar() -> None:
    calendar, rows = _sessions()
    rows = [
        row
        for row in rows
        if not (row.arm_id == "legacy_full_policy" and row.trade_date == calendar[7])
    ]
    report = evaluate_independent_confirmation(
        sessions=rows,
        trading_calendar=calendar,
        candidate_frozen_date=date(2026, 10, 10),
        unopened_window_attested=True,
        candidate_artifact_sha256="a" * 64,
        required_power_sessions=120,
        bootstrap_draws=100,
    )
    assert report["state"] == "engineering_blocked"
    assert any(
        item.startswith("incomplete_calendar:legacy_full_policy") for item in report["blockers"]
    )


def test_power_analysis_blocks_when_development_variance_cannot_be_estimated() -> None:
    calendar, rows = _sessions()
    candidate = [row for row in rows if row.arm_id == "candidate"][:10]
    control = [row for row in rows if row.arm_id == "legacy_same_pool"][:10]
    report = estimate_development_power_sample_size(candidate, control)
    assert report["status"] == "blocked"
    assert "fewer_than_two_complete_development_blocks_for_variance" in report["blockers"]
