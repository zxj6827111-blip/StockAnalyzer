#!/bin/bash
# Managed NAS vendor data updater (single-transaction nightly chain).
#
# Migrated from /vol1/docker/tools/stock_updater.sh and now version-controlled
# in the repository; installed atomically by scripts/nas_deploy_update.sh.
#
# One updater call performs ZIP batch update + daily index rebuild + delta
# DuckDB sync + nightly readiness publication in the SAME transaction, so a
# successful run always releases the 21:45 off-hours selector and a failed
# run never does (fail-closed via --require-readiness).
#
# Failure policy:
#   - "empty" failures (delisted / legacy BSE old codes) are EXPECTED and OK;
#   - any other failure -> retry once after 30 minutes;
#   - readiness not published at the end -> non-zero exit (no selector run).
#
# Date override for one-off backfills: UPDATER_END_DATE=YYYY-MM-DD bash ...
#
# WARNING: any non-dry-run updater invocation invalidates the current
# nightly_data_ready.json first (fail-closed). Do NOT run ad-hoc manual
# updates on production between the 19:45 cron and the 21:45 selector,
# or tonight's run will be blocked with nightly_data_not_ready.
# Log: /vol1/docker/tools/logs/updater.log
set -u

BASE=/vol1/docker/tools
LOG=$BASE/logs/updater.log
ENVFILE=$BASE/tushare.env
TMP=$BASE/logs/updater_last.json
mkdir -p "$BASE/logs"

# Single-instance lock: exit immediately if another updater run is active
# (manual run + cron, or a 30-min retry that outlived the first attempt).
exec 9>"$BASE/logs/updater.lock"
flock -n 9 || { echo "[$(date '+%F %T')] updater already running, skip" >> "$LOG"; exit 0; }

stamp() { date '+%F %T'; }

judge() {
  # $1 = JSON summary file; exits 0 if no failures or all are "empty"-type.
  python3 - "$1" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(2)
fails = d.get("failures") or []
bad = [f for f in fails if "empty" not in f]
print(f"  failures={len(fails)} real={len(bad)}")
sys.exit(0 if not bad else 1)
PY
}

run_update() {
  local end=$1
  docker run --rm --env-file "$ENVFILE" \
    -v /vol1/1000/股票历史数据:/data:rw \
    -v stock_analyzer_runtime_artifacts:/app/artifacts \
    stock-analyzer:latest \
    python3 /app/scripts/update_vendor_daily_from_tushare.py \
      --vendor-root /data --end-date "$end" \
      --checkpoint /data/update_checkpoint.json \
      --batch \
      --index-path /app/artifacts/vendor_overlay/daily_index.json \
      --sync-vendor-delta /app/artifacts/vendor_delta/market_delta.duckdb \
      --require-readiness \
    > "$TMP" 2>&1
  return $?
}

END="${UPDATER_END_DATE:-$(date +%Y-%m-%d)}"
echo "[$(stamp)] ==== updater start (end=$END) ====" >> "$LOG"

# attempt 1
run_update "$END"
RC=$?
if [ $RC -ne 0 ]; then
  judge "$TMP" 2>/dev/null
  JRC=$?
  if [ $JRC -eq 0 ]; then
    echo "[$(stamp)] attempt1: only expected empty failures, OK" >> "$LOG"
    RC=0
  else
    echo "[$(stamp)] attempt1 rc=$RC -> retry in 30min" >> "$LOG"
    sleep 1800
    run_update "$END"
    RC=$?
  fi
fi

if [ $RC -eq 0 ]; then
  # Production contract: the unified call must have published readiness.
  # Double-check the summary JSON so a partial success can never masquerade
  # as a releasable night.
  if python3 - "$TMP" <<'PY' >> "$LOG" 2>&1
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(2)
readiness = d.get("readiness") or {}
if not d.get("ok") or not readiness.get("written"):
    print(f"[verify] NOT releasable: ok={d.get('ok')} "
          f"readiness.written={readiness.get('written')} "
          f"readiness.error={readiness.get('error')}")
    sys.exit(1)
print(f"[verify] ok=true, target={d.get('target_trade_date', '')}, readiness published")
PY
  then
    :
  else
    echo "[$(stamp)] readiness verification FAILED rc=1 (summary in $TMP)" >> "$LOG"
    # empty-only trade-date failures (e.g. holiday backfill via
    # UPDATER_END_DATE) also block readiness by design: no complete data,
    # no release. Check updater_last.json errors before assuming a bug.
    echo "[$(stamp)] hint: nightly_data_not_ready tonight is expected if" \
      "errors are empty-only or the date had no market data" >> "$LOG"
    echo "[$(stamp)] ==== updater end rc=1 ====" >> "$LOG"
    exit 1
  fi
fi

echo "[$(stamp)] ==== updater end rc=$RC ====" >> "$LOG"
exit $RC
