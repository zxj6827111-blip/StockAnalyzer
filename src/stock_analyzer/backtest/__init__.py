"""Backtesting components."""

from stock_analyzer.backtest.matcher import ExecutionMatcher, ExitSimulation
from stock_analyzer.backtest.walk_forward import WalkForwardEngine, WalkForwardReport

__all__ = ["ExecutionMatcher", "ExitSimulation", "WalkForwardEngine", "WalkForwardReport"]
