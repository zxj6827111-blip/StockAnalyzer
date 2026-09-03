#!/bin/bash
# B4 全量 backfill 分块循环 v4：
# - 修复 v3 暂停窗口整点漏网 bug（19:30:00 / 21:15:00 不暂停，反而在生产窗口内开跑）
# - 暂停窗口（含边界）：19:30-20:45 updater、21:15-23:15 sync/night_scan
# - 每个 chunk 开跑前清理容器内孤儿 runner，避免 timeout 断链残留抢写锁
# - 其余时间持续跑；幂等，断点 offset 续跑。
# 用法：OFFSET_START=20000 bash phase0_backfill_wrapper_v4.sh
set -u

LOG=/vol1/docker/tools/logs/phase0_backfill_full.log
LOCK=/tmp/phase0_backfill.lock
CHUNK=10000
OFFSET=${OFFSET_START:-20000}
WORKERS=6

# 是否处于生产避让窗口（含左边界；HH:MM 字符串比较天然按字典序）
in_pause_window() {
  local now_hhmm=$1
  if [[ "$now_hhmm" < "19:30" ]]; then return 1; fi
  if [[ "$now_hhmm" < "20:45" ]]; then return 0; fi
  if [[ "$now_hhmm" < "21:15" ]]; then return 1; fi
  if [[ "$now_hhmm" < "23:15" ]]; then return 0; fi
  return 1
}

# 下一个暂停边界（秒级 epoch）：今天 19:30 / 21:15 / 23:15 / 明天 19:30
next_boundary_epoch() {
  local now_hhmm
  now_hhmm=$(date +%H:%M)
  if [[ "$now_hhmm" < "19:30" ]]; then
    date -d '19:30' +%s
  elif [[ "$now_hhmm" < "21:15" ]]; then
    date -d '21:15' +%s
  elif [[ "$now_hhmm" < "23:15" ]]; then
    date -d '23:15' +%s
  else
    date -d 'tomorrow 19:30' +%s
  fi
}

# 杀掉容器内残留的 runner 进程（timeout 断链时容器侧可能存活并持写锁）
cleanup_orphans() {
  docker exec -i stock-analyzer-api python3 - >/dev/null 2>&1 <<'PYEOF'
import os, signal
me = os.getpid()
for p in os.listdir("/proc"):
    if not p.isdigit() or int(p) == me:
        continue
    try:
        cmd = open("/proc/" + p + "/cmdline", "rb").read().decode("utf-8", "replace")
    except Exception:
        continue
    if "week5_phase0_backfill_runner.py" in cmd:
        try:
            os.kill(int(p), signal.SIGKILL)
        except Exception:
            pass
PYEOF
}

while true; do
  NOW_HM=$(date +%H:%M)
  if in_pause_window "$NOW_HM"; then
    sleep 120
    continue
  fi
  BOUNDARY=$(next_boundary_epoch)
  NOW=$(date +%s)
  WINDOW_SEC=$(( BOUNDARY - NOW ))
  [ "$WINDOW_SEC" -le 0 ] && WINDOW_SEC=300
  cleanup_orphans
  {
    echo "=== chunk start offset=${OFFSET} limit=${CHUNK} workers=${WORKERS} $(date '+%F %T') ==="
    flock -n "${LOCK}" -c "timeout ${WINDOW_SEC} docker exec stock-analyzer-api python3 /app/scripts/week5_phase0_backfill_runner.py --only-matured --limit ${CHUNK} --offset ${OFFSET} --fetch-workers ${WORKERS} --output-dir /app/artifacts/phase0_backfill"
    RC=$?
    echo "=== chunk end offset=${OFFSET} rc=${RC} $(date '+%F %T') ==="
  } >> "${LOG}" 2>&1
  if [ "$RC" -eq 124 ]; then
    echo "=== window cut; resume at offset=${OFFSET} next window ===" >> "${LOG}"
    continue
  fi
  if [ "$RC" -ne 0 ]; then
    echo "=== chunk failed rc=${RC}; sleep 60 and retry same offset ===" >> "${LOG}"
    sleep 60
    continue
  fi
  OFFSET=$(( OFFSET + CHUNK ))
done
