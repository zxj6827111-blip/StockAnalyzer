# StockAnalyzer NAS 全量修复与灰度发布手册

## 1. 固定安全边界

- 运行模式保持 `simulation`，并保持 `advisory_only=true`。
- `training_enabled=false`、`auto_promotion=false`。
- execution-risk 与 cross-review 候选仅运行 shadow，不自动改变排序、仓位或生产阈值。
- 到期持仓只生成 `exit_due`，没有 `trade_id`、价格、数量和成交时间不得关闭持仓。
- 不覆盖 NAS 的 `.env`、`artifacts/`、`suggestions/` 和历史 sidecar。

## 2. 发布前证据

在 NAS 宿主机执行：

```bash
cd /vol1/docker/StockAnalyzer
python scripts/export_support_bundle.py \
  --mode host \
  --base-url http://127.0.0.1:18001 \
  --output artifacts/support/nas_support_bundle_before.json
```

支持包不得包含 token、secret、password、webhook 或完整凭据值。`.env` 只记录键名、文件摘要和 `values_included=false`。

## 3. v8 到 v9 迁移

先停止 scheduler，再 dry-run：

```bash
docker compose stop scheduler
PYTHONPATH=src python scripts/migrate_runtime_state_v9.py \
  /vol1/docker/volumes/stock_analyzer_runtime_artifacts/_data/runtime/runtime_state.json \
  --dry-run
```

确认原始 SHA256、历史字段计数和目标 sidecar 后执行迁移：

```bash
PYTHONPATH=src python scripts/migrate_runtime_state_v9.py \
  /vol1/docker/volumes/stock_analyzer_runtime_artifacts/_data/runtime/runtime_state.json
```

验收要求：

- `runtime_state_backups/` 中备份 SHA256 等于迁移报告的 `original_sha256`。
- 主状态 `state_version=9` 且小于 1 MB。
- `runtime_state_history/*.jsonl` 记录数与迁移清单一致。
- 重复执行返回 `already_v9`，文件内容不变化。

回滚时停止 API 和 scheduler 写入，使用迁移报告中的 `backup_path`：

```bash
docker compose stop api scheduler
PYTHONPATH=src python scripts/migrate_runtime_state_v9.py \
  /vol1/docker/volumes/stock_analyzer_runtime_artifacts/_data/runtime/runtime_state.json \
  --rollback --backup-path /path/from/migration/report.json
```

回滚会保留新增 sidecar，并把当前 v9 主状态归档为 `pre_rollback`，不会删除历史。

## 4. 四阶段灰度

### 阶段 A：一致性版本

发布 build manifest、状态机、execution-risk shadow-only 和 v9。使用：

```bash
RELEASE_STAGE=stage-a-consistency \
BRANCH=<release-branch> REQUIRED_HEAD=<full-commit> \
bash scripts/p1_nas_rebuild_and_collect.sh
```

必须满足：repo HEAD、`/health.build.commit`、scheduler heartbeat commit 和 API/scheduler image digest 一致；manifest 为 `unknown` 或 `trusted=false` 时拒绝验收。迁移计数和 SHA256 一致，重启后状态一致，无新增伪成交，关键 API 最大延迟小于 2 秒。

### 阶段 B：可靠性版本

发布 scheduler、财务 provenance、概率健康、行情健康分级和支持包 v2。观察至少 7 个自然日：

- 无漏调度；跨过的 interval 最近槽位每轮最多补一个。
- `scheduler_heartbeat.json` 持续更新，失败状态包含退避和异常类型。
- 行情健康按最终失败率分为 `healthy/degraded/critical`，失败标的进入定向重试计划。
- 财务 `heuristic/default` 不触发 ROE/负债硬处罚。
- 云备份、scheduler watchdog 和任务执行状态分别归因。

### 阶段 C：Shadow 校准

保持生产阈值不变，运行 execution-risk 和 cross-review 邻域 shadow grid。市场观察结果不得当成真实执行结果；成熟记录必须有市场路径结果或明确 `pending_reason`。

### 阶段 D：Go/No-Go

准备 JSON 证据后执行：

```bash
python scripts/evaluate_shadow_promotion.py evidence.json --output promotion_gate.json
```

至少 100 条成熟样本、20 个交易日、精度提升 3 个百分点、最大回撤恶化不超过 2 个百分点，并同时通过概率健康、时间切分、覆盖率、稳定性、状态一致性、调度、provenance 和安全门禁。即使输出 `GO_PENDING_MANUAL_APPROVAL`，仍禁止自动晋级，必须人工批准单项变更。

## 5. 镜像回滚

发布脚本会记录：

- `.release_image`：本阶段不可变标签。
- `.rollback_image`：发布前镜像标签。
- `artifacts/runtime/build_identity_gate.json`：commit 和 image digest 证据。

回滚镜像时先停止写入，再将 `.rollback_image` 指向 `stock-analyzer:latest`，重建 API/scheduler 容器，并重新验证 `/health`、scheduler heartbeat、状态 SHA256 和 advisory-only 安全边界。

## 6. 本地与 NAS 验收边界

本地测试只证明代码和迁移工具可执行。只有 NAS 上的发布前后支持包、容器 digest、API health、scheduler heartbeat、日志、状态校验和回滚演练全部归档后，才能认定对应阶段在 NAS 生效。
