# Learning Dedup-Key Evidence (2026-08-23)

## Source and method

The active NAS database was copied inside the `stock-analyzer-api` container before querying to avoid the live DuckDB write lock:

- Database: `/app/artifacts/training/learning_protocol.duckdb`
- Read-only copy: `/tmp/lp_diag_0823.duckdb`
- Evidence log: `tmp_nas_diag_evidence_0823.log`

The query normalized `decision_time` to the Asia/Shanghai calendar date with a fixed UTC+8 offset and grouped `signal_snapshots` by `(symbol, strategy, decision_date_sh)`.

## Results

- Total `signal_snapshots`: **89,010**
- Duplicate logical-key groups: **5,387**
- Rows belonging to duplicate groups: **72,916**
- Duplicate cardinality ranged from 2 to 75+ rows per logical key.
- The largest groups included `monster` strategy rows on `2026-05-26`.
- `300483` on `2026-07-20` contained repeated `pipeline_run_once` snapshots with different snapshot IDs but identical realized outcome values.

## Conclusion

`(symbol, strategy, decision_date)` is not a unique source key. The repeated rows are caused by repeated same-day pipeline writes, not by independent trading decisions. The manifest v2 contract therefore uses:

- Dedup key: `(symbol, strategy, decision_date_sh)`
- Retention rule: `keep_first_by_decision_time`, then `created_at`, then `snapshot_id`
- Upstream guard: skip a new snapshot when the same logical key already exists
- Trainer defense: a v2 manifest containing a duplicate logical key fails closed

A v1 manifest remains readable for compatibility, but it is not eligible for the v2 promotion hard gate because it cannot prove duplicate-free membership.
