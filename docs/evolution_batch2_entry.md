# Evolution Batch2 Entry

## Goal
Prepare implementation entry points for Batch2 modules without changing Batch1 framework contracts.

## Existing Contracts (must keep)
- `StockAnalyzerService.run_evolution_offhours(...)`
- `StockAnalyzerService.run_evolution_drill(...)`
- `OffhoursEvolutionOrchestrator.run(...)`
- `suggestions/` sandbox output
- Compliance state flow and proposal artifact format

## Batch2 Integration Plan
1. Implement `M1` dual learning (As-Of guard + poison filter + missed-case buckets + shared output).
2. Implement `M2` four-state regime adaptation with confidence tiers and cooldown switching.
3. Implement `M3` pattern memory (streaming memmap, batch=50000, safe snapshot deletion delay 24h).
4. Feed `M1/M2/M3` scores into `ScoreFusionEngine` and keep cache key binding to `active_champion_id`.
5. Keep governance and rollback interfaces unchanged.

## Required Module Interfaces
```python
def run_m1_dual_learning(records: list[dict[str, object]], asof_date: date) -> M1LearningResult: ...
def evaluate_m2_regime(controller: RegimeStateController, observation: RegimeObservation) -> M2RegimeResult: ...
class PatternMemoryStore:
    def append(self, vectors: np.ndarray) -> PatternAppendResult: ...
    def safe_remove_snapshot(self, snapshot_path: str) -> Path: ...
```

## Test Gate for Batch2
- `pytest tests -k "evolution and (m1 or m2 or m3)" --cov-fail-under=80`
- Keep `mypy src` and `ruff check src tests` green.
