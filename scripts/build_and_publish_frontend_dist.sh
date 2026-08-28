#!/usr/bin/env bash
# 单独构建并发布前端产物到 frontend_dist 挂载卷目录，免重建整个镜像。
#
# ⚠️ 部署顺序风险（必读，返工第 4 项）：
#   docker-compose.yml 已把宿主 ${SA_FRONTEND_DIST_HOST_ROOT:-./frontend_dist}
#   挂载到容器 /app/frontend_dist。如果在**从未运行过本脚本、宿主
#   frontend_dist 目录不存在或为空**的情况下先执行
#   `docker compose up -d --force-recreate api`，Docker 会自动创建一个
#   **空目录**并挂载进容器，这个空目录会覆盖镜像内构建好的前端产物
#   （镜像内 /app/frontend_dist 原本是有内容的），导致 `/ui` 返回 404，
#   且直到运维人工排查前不会有任何其它明显报错。
#
#   正确顺序必须是：
#     1. 先在**任意时刻**（容器 recreate 前或后都可以，但顺序上必须早于
#        容器第一次因为这次改动而 recreate）执行本脚本，确保宿主
#        frontend_dist 目录里已经有构建产物（index.html + assets/）；
#     2. 再执行 `docker compose up -d --force-recreate api` 完成首次挂载。
#
#   简言之：**先跑本脚本产出 frontend_dist，再 recreate 容器**，不要反过来。
#   后续的前端迭代发布（挂载卷已经生效之后）不受此顺序限制，重新跑本脚本
#   覆盖发布即可，无需再 recreate 容器。
#
# 背景（PLAN docs/plan_asof_backtest_holding_curve.md Task 7）：
#   当前 Dockerfile 是多阶段构建，前端产物在镜像构建阶段被 COPY 进
#   /app/frontend_dist（镶入镜像，不可单独替换）。本脚本配合
#   docker-compose.yml 新增的 frontend_dist 卷挂载（映射到宿主
#   ${SA_FRONTEND_DIST_HOST_ROOT:-./frontend_dist}），使前端后续迭代只需
#   替换这个目录下的静态文件即可生效（浏览器强缓存的是带 hash 的资源文件名，
#   index.html 走协商缓存，见 main.py 的 _UI_ASSET_CACHE_CONTROL 注释），
#   不需要重建/重启容器（除了首次启用该挂载卷时需要一次 recreate，因为新增
#   volume mount 本身需要容器重建才能生效——这一步需要与运维确认时间窗口，
#   本脚本本身只负责构建产物、不做任何容器操作）。
#
# 用法（在项目根目录执行）：
#   bash scripts/build_and_publish_frontend_dist.sh
#   bash scripts/build_and_publish_frontend_dist.sh --output-dir /custom/path
#
# 前置条件：
#   - 本机或执行环境已安装 Node.js（若没有，可改用 --docker 参数走容器内构建，
#     不依赖宿主 Node 版本，见下方 --docker 分支）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${ROOT}/frontend_dist"
USE_DOCKER=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir)
      OUTPUT_DIR="${2:?missing value for --output-dir}"
      shift 2
      ;;
    --docker)
      USE_DOCKER=1
      shift
      ;;
    -h|--help)
      sed -n '1,25p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

cd "${ROOT}"

BUILD_STAGE="$(mktemp -d)"
trap 'rm -rf "${BUILD_STAGE}"' EXIT

if [[ "${USE_DOCKER}" -eq 1 ]]; then
  echo "[1/3] building frontend inside a throwaway node:22-slim container"
  # 源码目录保持 :ro（一次性构建不应污染宿主 frontend/ 源码树）；但 npm ci 需要
  # 写 node_modules、vite build 需要写 dist，只读挂载下两者都会直接失败
  # （返工第 3 项 bug）。用匿名卷分别覆盖这两个子目录：写入落在容器生命周期内的
  # 匿名卷而非宿主机磁盘，构建产物通过 cp 显式拷到 /output，容器退出后
  # node_modules/dist 的匿名卷随之销毁，不会残留或污染宿主环境。
  docker run --rm \
    -v "${ROOT}/frontend:/frontend:ro" \
    -v "/frontend/node_modules" \
    -v "/frontend/dist" \
    -v "${BUILD_STAGE}:/output" \
    -w /frontend \
    node:22-slim \
    bash -c "npm ci --prefer-offline --no-audit --fund=false && npm run build && cp -r dist/. /output/"
else
  echo "[1/3] building frontend with local Node.js (npm ci && npm run build)"
  (
    cd "${ROOT}/frontend"
    npm ci --prefer-offline --no-audit --fund=false
    npm run build
  )
  cp -r "${ROOT}/frontend/dist/." "${BUILD_STAGE}/"
fi

if [[ ! -f "${BUILD_STAGE}/index.html" ]]; then
  echo "ERROR: build output missing index.html; refusing to publish a broken bundle." >&2
  exit 1
fi

echo "[2/3] validating build output is non-empty and has hashed assets"
ASSET_COUNT="$(find "${BUILD_STAGE}/assets" -maxdepth 1 -type f 2>/dev/null | wc -l | tr -d ' ')"
if [[ "${ASSET_COUNT}" -lt 1 ]]; then
  echo "ERROR: build output has no files under assets/; refusing to publish." >&2
  exit 1
fi

echo "[3/3] publishing to ${OUTPUT_DIR} (atomic swap, old build kept as .previous for one cycle)"
mkdir -p "$(dirname "${OUTPUT_DIR}")"
if [[ -d "${OUTPUT_DIR}" ]]; then
  rm -rf "${OUTPUT_DIR}.previous"
  mv "${OUTPUT_DIR}" "${OUTPUT_DIR}.previous"
fi
mv "${BUILD_STAGE}" "${OUTPUT_DIR}"

echo
echo "OK. Published $(find "${OUTPUT_DIR}" -type f | wc -l | tr -d ' ') files to ${OUTPUT_DIR}"
echo "Previous build kept at ${OUTPUT_DIR}.previous (safe to delete once you confirm the new build works)."
echo
echo "=================================================================="
echo "  部署顺序提醒（返工第 4 项，务必确认）"
echo "=================================================================="
echo "如果 frontend_dist 挂载卷此前还从未在 docker-compose.yml 里生效过，"
echo "现在（本脚本已产出内容之后）才是安全执行下面这一步的时机："
echo
echo "    docker compose up -d --force-recreate api"
echo
echo "⚠️  绝不要反过来：如果先执行了上面这行 recreate 命令，而 frontend_dist"
echo "    目录当时是空的/不存在，Docker 会把一个空目录挂载进容器，覆盖掉"
echo "    镜像内原本构建好的前端产物，导致 /ui 返回 404。若你不确定 recreate"
echo "    是否已经先于本脚本执行过，请先用以下命令确认 /ui 当前能否正常访问，"
echo "    再决定是否需要额外的 --force-recreate："
echo
echo "    curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18001/ui"
echo
echo "已经生效挂载卷之后的后续前端迭代发布，重新跑本脚本覆盖即可，无需再"
echo "recreate 容器（StaticFiles 直接从磁盘读取，不需要重启进程）。"
echo "=================================================================="
