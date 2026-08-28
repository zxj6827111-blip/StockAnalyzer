#!/usr/bin/env bash
# NAS 生产部署的唯一 compose 文件组合来源（返工第 2 项：本次事故的根本预防）。
#
# 背景：2026-08-28 的配置退化事故中，有人在 nas_deploy_update.sh 之外手动执行了
# 一条不完整的 `docker compose -f docker-compose.yml ... --force-recreate api`
# 裸命令（缺少 -f docker-compose.vendor-overlay.yml / docker-compose.runtime.yml
# / docker-compose.advisory.yml），导致 api 容器静默退化回默认配置
# （SA__DATA_SOURCE__PRIMARY 从 vendor_zip_overlay 掉回 market_warehouse，
# 内存限制也一并丢失）。nas_deploy_update.sh 脚本内部的 -f 列表本身没有问题，
# 问题在于"脚本之外的手动操作"没有一个唯一、显眼、可复制粘贴的正确基线。
#
# 使用方式（任何需要手动对 NAS 生产 api/scheduler 容器执行 docker compose 操作
# 的场合，包括调试、临时重启、手动 up，都必须走这里，禁止自己拼 -f 列表）：
#
#   cd /vol1/docker/StockAnalyzer
#   source scripts/nas_compose_files.sh
#   docker compose --env-file .env "${NAS_COMPOSE_ARGS[@]}" ps
#   docker compose --env-file .env "${NAS_COMPOSE_ARGS[@]}" up -d --force-recreate api
#
# 不要执行 `docker compose -f docker-compose.yml ...`（缺少 overlay 文件，
# 会把 api/scheduler 退化回默认非生产配置）。
#
# docker-compose.memlimit.yml 也纳入本基线。它最初被设计成"可选叠加层"，但
# 2026-08-28 修复上述事故时已经对 NAS 的 api / scheduler-critical /
# scheduler-heavy / redis 四个容器全部应用并验证通过（实测占用远低于上限：
# api 394M/4G、scheduler-heavy 446M/3G、scheduler-critical 411M/2G、
# redis 9M/512M，宿主 swap 用量为 0）。既然生产实际已运行在内存限制之下，
# 把它排除在"唯一基线"之外就会制造新的状态漂移：任何人 source 本文件后
# recreate 都会静默把四个容器的 mem_limit 打回 0（丢掉 OOM 兜底），
# 而这正是本文件要防的同一类退化。基线必须与生产实际状态一致才有意义。
# 首次应用或撤销内存限制需要 recreate 全部四个容器，仍应安排维护窗口。

# shellcheck disable=SC2034  # 供其它脚本 source 后引用，本文件自身不直接使用
NAS_COMPOSE_ARGS=(
  -f docker-compose.yml
  -f docker-compose.runtime.yml
  -f docker-compose.advisory.yml
  -f docker-compose.vendor-overlay.yml
  -f docker-compose.memlimit.yml
)
