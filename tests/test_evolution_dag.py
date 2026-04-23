from __future__ import annotations

from stock_analyzer.evolution.scheduler.dag import EvolutionDag


def test_dag_contains_required_dependencies() -> None:
    dag = EvolutionDag()
    assert dag.upstream("M4") == ("M9",)
    assert dag.upstream("M1") == ("M4",)
    assert dag.upstream("M3") == ("M4",)
    assert dag.upstream("M8") == ("M3",)
    assert dag.upstream("M6") == ("M4",)
    assert dag.upstream("M10") == ("M4",)
    assert dag.upstream("M11") == ("M4",)
    assert dag.upstream("M5") == ("M2",)
    assert dag.upstream("M7") == ()
    assert dag.upstream("SCORE_FUSION") == (
        "M1",
        "M3",
        "M4",
        "M5",
        "M6",
        "M7",
        "M8",
        "M10",
        "M11",
    )


def test_m9_retry_policy_is_three_times_with_reset_on_success() -> None:
    dag = EvolutionDag()
    assert dag.should_retry("M9", success=False) is True
    assert dag.should_retry("M9", success=False) is True
    assert dag.should_retry("M9", success=False) is True
    assert dag.should_retry("M9", success=False) is False

    dag.should_retry("M9", success=True)
    assert dag.retry_count("M9") == 0


def test_runnable_nodes_follow_dependency_completion() -> None:
    dag = EvolutionDag()
    initial_ready = dag.runnable_nodes(completed=set())
    assert "M9" in initial_ready
    assert "M4" not in initial_ready

    after_m9 = dag.runnable_nodes(completed={"M9"})
    assert "M4" in after_m9
    assert "M2" in after_m9
    assert "M10" not in after_m9
    assert "M11" not in after_m9
    assert "M8" not in after_m9
