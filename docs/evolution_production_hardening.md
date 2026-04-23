# Evolution Production Hardening (Step 1)

## Objective
Close production prerequisites before enabling strict off-hours evolution checks.

## Checklist
1. Install required dependencies on target machine:
   - `cpulimit` (CLI, Linux/macOS). On Windows rehearsal host, preflight accepts `pwsh/powershell` fallback.
   - `duckdb` (Python package)
   - `faiss` (Python package)
2. Pin evolution runtime identifiers:
   - `evolution.code_commit_id` must be real release commit id.
   - `evolution.active_champion_id` must match current champion.
3. Validate writable paths:
   - `evolution.suggestions_dir`
   - `evolution.manifest_path` parent
   - `evolution.compliance_db_path` parent
4. Run preflight and require pass:
   - `python -m stock_analyzer.cli evolution-preflight --fail-on-not-ready true`
5. After preflight passes in pre-production:
   - keep `dry_run: true` for 5-10 trading days
   - keep `strict_dependency_check: true`
   - run validation report:
     `python -m stock_analyzer.cli evolution-window-report --days 10 --min-runs 5 --fail-on-fail true`

## Recommended Config Baseline
Use:
- [evolution.production.example.yaml](D:/软件开发/谷歌反重力开发/StockAnalyzer/config/evolution.production.example.yaml)

Merge the `evolution:` section into your runtime config and replace placeholders.
