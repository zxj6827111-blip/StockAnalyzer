#!/bin/sh
set -eu

RUNTIME_ARTIFACT_DIR="/app/artifacts"
RUNTIME_MODEL_PATH="$RUNTIME_ARTIFACT_DIR/model_v1.json"
SEED_MODEL_PATH="/app/bootstrap_seed/model_v1.json"

mkdir -p "$RUNTIME_ARTIFACT_DIR"

if [ ! -f "$RUNTIME_MODEL_PATH" ] && [ -f "$SEED_MODEL_PATH" ]; then
  cp "$SEED_MODEL_PATH" "$RUNTIME_MODEL_PATH"
fi

exec "$@"
