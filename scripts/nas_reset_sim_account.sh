#!/usr/bin/env bash
# One-shot NAS recovery for capital_curve:freeze from stale sim equity/trades.
# Run from project root: /vol1/docker/StockAnalyzer
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="${ROOT}/artifacts/runtime/runtime_state.json"
EQUITY="${1:-1.0}"

cd "${ROOT}"

echo "[1/5] stop api + schedulers"
docker stop stock-analyzer-api stock-analyzer-scheduler stock-analyzer-scheduler-critical stock-analyzer-scheduler-heavy >/dev/null || true

echo "[2/5] patch runtime_state.json equity=${EQUITY}, clear portfolio"
if command -v python3 >/dev/null 2>&1; then
  python3 "${ROOT}/scripts/reset_sim_account_runtime_state.py" \
    --state "${STATE}" \
    --equity "${EQUITY}"
else
  docker run --rm \
    -v "${ROOT}/artifacts:/app/artifacts" \
    -v "${ROOT}/scripts:/scripts:ro" \
    python:3.11-slim \
    python /scripts/reset_sim_account_runtime_state.py \
      --state /app/artifacts/runtime/runtime_state.json \
      --equity "${EQUITY}"
fi

echo "[3/5] start api only"
docker start stock-analyzer-api >/dev/null
sleep 8

echo "[4/5] verify runtime state"
docker exec stock-analyzer-api python -c "
import json
d=json.load(open('/app/artifacts/runtime/runtime_state.json'))
p=d.get('portfolio') or {}
print('equity', d.get('current_equity'))
print('pause', d.get('pause_new_buy'))
print('pos', len(p.get('positions') or []))
print('trades', len(p.get('trades') or []))
"

echo "[5/5] single-symbol freeze probe"
docker exec stock-analyzer-api sh -c '
stock-analyzer week5-scan-run --symbols 002141 --no-notify-enabled > /tmp/w5_probe.json 2>/tmp/w5_probe.err
python -c "
import json
d=json.load(open(\"/tmp/w5_probe.json\"))
es=d.get(\"empty_signal\") or {}
print(\"drawdown\", es.get(\"drawdown_pct\"))
print(\"risk_action\", es.get(\"risk_action\"))
print(\"reasons\", es.get(\"reasons\"))
print(\"summary\", d.get(\"summary\"))
"
'

echo "Done. If risk_action is not freeze, optionally: docker start stock-analyzer-scheduler-critical stock-analyzer-scheduler-heavy"
echo "After image rebuild you can also use: docker exec stock-analyzer-api stock-analyzer reset-sim-account"
