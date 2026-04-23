"""DAG definition for off-hours evolution modules."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retry policy for one DAG node."""

    max_retries: int = 0
    retry_delay_seconds: int = 0
    reset_on_success: bool = False


@dataclass(frozen=True, slots=True)
class DagNode:
    """Static DAG node definition."""

    name: str
    upstream: tuple[str, ...]
    retry_policy: RetryPolicy = RetryPolicy()


class EvolutionDag:
    """In-memory DAG and retry counters."""

    def __init__(self, nodes: Iterable[DagNode] | None = None) -> None:
        node_list = list(nodes) if nodes is not None else _default_nodes()
        self._nodes = {node.name: node for node in node_list}
        self._downstream = _build_downstream(self._nodes)
        self._retry_counters: dict[str, int] = {}

    def node(self, name: str) -> DagNode:
        """Return one DAG node by name."""
        if name not in self._nodes:
            raise KeyError(f"unknown DAG node: {name}")
        return self._nodes[name]

    def upstream(self, name: str) -> tuple[str, ...]:
        """Return upstream dependencies for one node."""
        return self.node(name).upstream

    def downstream(self, name: str) -> tuple[str, ...]:
        """Return downstream nodes for one node."""
        return self._downstream.get(name, ())

    def should_retry(self, name: str, success: bool) -> bool:
        """Update retry counters and tell whether the node should retry now."""
        policy = self.node(name).retry_policy
        if policy.max_retries <= 0:
            return False

        if success:
            if policy.reset_on_success:
                self._retry_counters[name] = 0
            return False

        current = self._retry_counters.get(name, 0) + 1
        self._retry_counters[name] = current
        return current <= policy.max_retries

    def retry_count(self, name: str) -> int:
        """Return current retry counter for node."""
        return self._retry_counters.get(name, 0)

    def runnable_nodes(self, completed: set[str]) -> list[str]:
        """Return nodes whose upstream dependencies are all satisfied."""
        ready: list[str] = []
        for node in self._nodes.values():
            if node.name in completed:
                continue
            if all(dep in completed for dep in node.upstream):
                ready.append(node.name)
        return sorted(ready)


def _build_downstream(nodes: dict[str, DagNode]) -> dict[str, tuple[str, ...]]:
    downstream: dict[str, list[str]] = {name: [] for name in nodes}
    for node in nodes.values():
        for parent in node.upstream:
            if parent not in downstream:
                downstream[parent] = []
            downstream[parent].append(node.name)
    return {name: tuple(sorted(children)) for name, children in downstream.items()}


def _default_nodes() -> list[DagNode]:
    return [
        DagNode(
            name="M9",
            upstream=(),
            retry_policy=RetryPolicy(max_retries=3, retry_delay_seconds=120, reset_on_success=True),
        ),
        DagNode(name="M4", upstream=("M9",)),
        DagNode(name="M2", upstream=("M9",)),
        DagNode(name="M1", upstream=("M4",)),
        DagNode(name="M3", upstream=("M4",)),
        DagNode(name="M8", upstream=("M3",)),
        DagNode(name="M6", upstream=("M4",)),
        DagNode(name="M10", upstream=("M4",)),
        DagNode(name="M11", upstream=("M4",)),
        DagNode(name="M5", upstream=("M2",)),
        DagNode(name="M7", upstream=()),
        DagNode(
            name="SCORE_FUSION",
            upstream=("M1", "M3", "M4", "M5", "M6", "M7", "M8", "M10", "M11"),
        ),
        DagNode(name="PROPOSAL", upstream=("SCORE_FUSION",)),
    ]
