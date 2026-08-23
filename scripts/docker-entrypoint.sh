#!/bin/sh
set -eu

RUNTIME_ARTIFACT_DIR="/app/artifacts"
RUNTIME_MODEL_PATH="$RUNTIME_ARTIFACT_DIR/model_v1.json"
SEED_MODEL_PATH="/app/bootstrap_seed/model_v1.json"

mkdir -p "$RUNTIME_ARTIFACT_DIR"

# 种子引导（幂等）：优先把 seed 发布为内容寻址 bundle 并原子写运行时别名；
# 引导失败时退回旧的裸拷贝行为，保证容器仍可启动。
if [ -f "$SEED_MODEL_PATH" ]; then
  if ! python /app/scripts/bootstrap_runtime_model_alias.py; then
    echo "[entrypoint] seed bundle bootstrap failed; falling back to raw copy" >&2
    [ -f "$RUNTIME_MODEL_PATH" ] || cp "$SEED_MODEL_PATH" "$RUNTIME_MODEL_PATH"
  fi
fi

exec "$@"
