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
  # 写 node_modules、vite build 需要写 dist，只读挂载下两者都会直接失败。
  # 用匿名卷分别覆盖这两个子目录：写入落在容器生命周期内的匿名卷而非宿主机磁盘，
  # 构建产物通过 cp 显式拷到 /output，容器退出后 node_modules/dist 的匿名卷随之
  # 销毁，不会残留或污染宿主环境。
  #
  # 末尾的 chown 不可省略：容器内是 root，拷进 /output（宿主临时目录）的文件属主
  # 会是 root:root，宿主上以普通用户执行本脚本时既无法 mv 也无法 rm 这些文件，
  # 发布与 trap 清理都会报 "Permission denied" 并留下无法删除的垃圾目录
  # （2026-08-28 实测踩到）。改回宿主当前 uid:gid 后续步骤才能正常操作。
  docker run --rm \
    -v "${ROOT}/frontend:/frontend:ro" \
    -v "/frontend/node_modules" \
    -v "/frontend/dist" \
    -v "${BUILD_STAGE}:/output" \
    -w /frontend \
    node:22-slim \
    bash -c "npm ci --prefer-offline --no-audit --fund=false && npm run build && cp -r dist/. /output/ && chown -R $(id -u):$(id -g) /output"
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

echo "[3/3] publishing to ${OUTPUT_DIR} (in-place sync, old build kept as .previous for one cycle)"
mkdir -p "$(dirname "${OUTPUT_DIR}")"

# 关键：绝不能用 `mv` 整体替换 OUTPUT_DIR。docker-compose.yml 把宿主
# ${SA_FRONTEND_DIST_HOST_ROOT:-./frontend_dist} bind mount 进容器
# /app/frontend_dist，而 bind mount 绑定的是**目录 inode**，不是路径字符串。
# 一旦 mv 掉原目录换上一个新建的同名目录，宿主看到的是新产物，容器却仍挂着
# 旧 inode（也就是被移走的 .previous 那份数据），于是出现"宿主已更新、容器
# 里还是旧版"的分裂状态——2026-08-28 实测踩到：后端已注入 window.SA_API_TOKEN，
# 浏览器加载到的却是不读该变量的旧 JS，前端 POST 全部 401，且现象极难归因。
# 因此这里改为保持 OUTPUT_DIR 自身 inode 不变的原地同步。
mkdir -p "${OUTPUT_DIR}"

# 备份旧产物：用 cp 而不是 mv，避免动到 OUTPUT_DIR 本身的 inode
rm -rf "${OUTPUT_DIR}.previous"
if [[ -n "$(ls -A "${OUTPUT_DIR}" 2>/dev/null)" ]]; then
  mkdir -p "${OUTPUT_DIR}.previous"
  cp -a "${OUTPUT_DIR}/." "${OUTPUT_DIR}.previous/"
fi

# 发布顺序也有讲究：先放除 index.html 以外的全部资源，最后再原子替换 index.html。
# assets/* 是内容 hash 命名的，新旧文件名天然不冲突可以共存，所以这个顺序保证
# 任意瞬间浏览器要么拿到「旧 index.html + 旧 assets」，要么「新 index.html + 新
# assets」，不会出现 index.html 已指向尚未落盘的 assets 的空窗。
find "${BUILD_STAGE}" -mindepth 1 -maxdepth 1 ! -name index.html -print0 \
  | xargs -0 -I{} cp -a {} "${OUTPUT_DIR}/"

# 同目录内的 mv 是原子 rename，浏览器不会读到写了一半的 index.html
cp -a "${BUILD_STAGE}/index.html" "${OUTPUT_DIR}/.index.html.incoming"
mv "${OUTPUT_DIR}/.index.html.incoming" "${OUTPUT_DIR}/index.html"

# 切换完成后再清理上一版遗留、已无人引用的 hash 资源（此刻新 index.html 已生效）
if [[ -d "${OUTPUT_DIR}/assets" ]]; then
  while IFS= read -r -d '' stale; do
    rel="${stale#"${OUTPUT_DIR}/"}"
    [[ -e "${BUILD_STAGE}/${rel}" ]] || rm -f "${stale}"
  done < <(find "${OUTPUT_DIR}/assets" -maxdepth 1 -type f -print0)
fi

echo
echo "OK. Published $(find "${OUTPUT_DIR}" -type f | wc -l | tr -d ' ') files to ${OUTPUT_DIR}"
echo "Previous build kept at ${OUTPUT_DIR}.previous (safe to delete once you confirm the new build works)."

# 自检：确认容器里真的看到了这一版产物。本脚本已改为保持目录 inode 的原地同步，
# 正常情况下无需 recreate；但如果这个 frontend_dist 目录在**过去**曾被 mv 整体
# 替换过（旧版脚本的行为），运行中的容器可能仍绑在那个已被移走的旧 inode 上，
# 此时无论怎么重新发布，容器里都还是旧产物。这种分裂状态从宿主侧完全看不出来，
# 只能这样跨进程比对入口文件名，所以这里主动检查并给出明确处置建议。
HOST_ENTRY="$(grep -oE 'assets/[A-Za-z0-9._-]+\.js' "${OUTPUT_DIR}/index.html" 2>/dev/null | head -1 || true)"
if [[ -n "${HOST_ENTRY}" ]] && command -v docker >/dev/null 2>&1 \
  && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'stock-analyzer-api'; then
  CONTAINER_ENTRY="$(docker exec stock-analyzer-api sh -c \
    "grep -oE 'assets/[A-Za-z0-9._-]+\.js' /app/frontend_dist/index.html 2>/dev/null | head -1" \
    2>/dev/null || true)"
  echo
  if [[ "${HOST_ENTRY}" == "${CONTAINER_ENTRY}" ]]; then
    echo "自检通过：容器内已生效同一版产物 (${HOST_ENTRY})，无需 recreate。"
  else
    echo "=================================================================="
    echo "  ⚠️  自检失败：容器内产物与宿主不一致，前端更新尚未生效"
    echo "=================================================================="
    echo "  宿主 index.html 入口 : ${HOST_ENTRY}"
    echo "  容器 index.html 入口 : ${CONTAINER_ENTRY:-<读取失败/为空>}"
    echo
    echo "  原因：该 bind mount 绑定的目录 inode 与当前 frontend_dist 已不是同一个"
    echo "  （通常是历史上被旧版脚本 mv 整体替换过）。需要 recreate 一次重建绑定："
    echo
    echo "    source scripts/nas_compose_files.sh"
    echo "    docker compose --env-file .env \"\${NAS_COMPOSE_ARGS[@]}\" up -d --force-recreate api"
    echo
    echo "  注意必须用上面的完整 compose 组合，不要用裸 'docker compose -f"
    echo "  docker-compose.yml ...'——那会丢掉 overlay 导致生产配置退化。"
    echo "=================================================================="
  fi
fi

echo
echo "=================================================================="
echo "  首次启用挂载卷时的部署顺序提醒"
echo "=================================================================="
echo "如果 frontend_dist 挂载卷此前还从未在 docker-compose.yml 里生效过，"
echo "现在（本脚本已产出内容之后）才是安全执行 recreate 的时机："
echo
echo "    source scripts/nas_compose_files.sh"
echo "    docker compose --env-file .env \"\${NAS_COMPOSE_ARGS[@]}\" up -d --force-recreate api"
echo
echo "⚠️  绝不要反过来：如果先执行 recreate，而 frontend_dist 目录当时是空的/"
echo "    不存在，Docker 会把一个空目录挂载进容器，覆盖掉镜像内原本构建好的"
echo "    前端产物，导致 /ui 返回 404。确认当前 /ui 是否正常："
echo
echo "    curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18001/ui"
echo
echo "挂载卷已生效后的后续前端迭代，重跑本脚本即可，无需 recreate：本脚本"
echo "保持 frontend_dist 目录 inode 不变，StaticFiles 直接从磁盘读取。"
echo "=================================================================="
