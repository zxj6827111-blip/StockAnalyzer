#!/bin/sh
set -eu

RUNTIME_ARTIFACT_DIR="/app/artifacts"
RUNTIME_MODEL_PATH="$RUNTIME_ARTIFACT_DIR/model_v1.json"
SEED_MODEL_PATH="/app/bootstrap_seed/model_v1.json"

mkdir -p "$RUNTIME_ARTIFACT_DIR"

# NAS 生产配置防退化 fail-fast（返工第 2 项，2026-08-28 配置退化事故的根本预防
# 之二）：nas_deploy_update.sh 内部的 compose 文件组合本身没有问题，事故根因是
# 有人在脚本之外手动执行了缺少 -f docker-compose.vendor-overlay.yml 等文件的
# 裸 `docker compose up --force-recreate` 命令，导致容器静默用错误的默认配置
# 启动（SA__DATA_SOURCE__PRIMARY 掉回 market_warehouse、内存限制丢失）且没有
# 任何显式报错。部署脚本的渲染校验只在部署那一刻生效，之后任何脚本之外的手动
# recreate 都会绕过它；这里在容器启动的必经入口再做一次校验，任何启动方式都
# 无法绕过。仅当 NAS 的 .env 显式设置 SA_NAS_PRODUCTION_GUARD=1 时才生效
# （本地开发 / CI 构建镜像不设置该变量，默认不受影响，不强制要求
# vendor_zip_overlay 之类的生产专属配置）。
if [ "${SA_NAS_PRODUCTION_GUARD:-0}" = "1" ]; then
  case "${SA__DATA_SOURCE__PRIMARY:-}" in
    vendor_zip_overlay) ;;
    *)
      echo "[entrypoint] FATAL: SA_NAS_PRODUCTION_GUARD=1 but SA__DATA_SOURCE__PRIMARY='${SA__DATA_SOURCE__PRIMARY:-<unset>}' (expected vendor_zip_overlay)." >&2
      echo "[entrypoint] This container was very likely started with an incomplete 'docker compose -f ...' combination." >&2
      echo "[entrypoint] Re-run via: source scripts/nas_compose_files.sh && docker compose --env-file .env \"\${NAS_COMPOSE_ARGS[@]}\" up -d --force-recreate <service>" >&2
      exit 1
      ;;
  esac
fi

# 种子引导（幂等）：优先把 seed 发布为内容寻址 bundle 并原子写运行时别名；
# 引导失败时退回旧的裸拷贝行为，保证容器仍可启动。
if [ -f "$SEED_MODEL_PATH" ]; then
  if ! python /app/scripts/bootstrap_runtime_model_alias.py; then
    echo "[entrypoint] seed bundle bootstrap failed; falling back to raw copy" >&2
    [ -f "$RUNTIME_MODEL_PATH" ] || cp "$SEED_MODEL_PATH" "$RUNTIME_MODEL_PATH"
  fi
fi

exec "$@"
