"""Shared execution engine used by backtest and live simulation."""

from stock_analyzer.execution.engine import ExecutionEngine, MatchDecision, OrderPlan

__all__ = ["ExecutionEngine", "MatchDecision", "OrderPlan"]
