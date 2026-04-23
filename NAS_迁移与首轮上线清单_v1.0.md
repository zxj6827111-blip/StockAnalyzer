# NAS 迁移与首轮上线清单 v1.0

> 说明：本文仍保留为旧版 NAS 迁移清单。当前正式运行态已经切到 `runtime localvol`，飞牛 NAS 首次部署请优先查看 `docs/fn_nas_deployment_runtime_localvol.md`。

适用对象：当前已经在本机完成开发、准备把系统迁移到 NAS / 另一台机器并继续保留历史数据、模型状态、运行状态的 StockAnalyzer 项目。

---

## 1. 当前架构结论

当前系统已经切换为以 `market_warehouse` 为主的数据架构：

- 主数据库：`artifacts/warehouse/market.duckdb`
- 运行数据包：`artifacts/warehouse/package/`
- TDX 导入中转目录：`artifacts/imports/tdx_offline_package/`
- 运行态与历史：`artifacts/runtime/`
- 训练与演化产物：`artifacts/training/`、`artifacts/evolution/`
- 策略建议与候选：`suggestions/`

Docker 当前已经把以下宿主机目录挂载到容器中：

- `./artifacts:/app/artifacts`
- `./suggestions:/app/suggestions`

这意味着：

- 只要宿主机上的 `artifacts/` 和 `suggestions/` 保留，核心数据就会保留。
- 容器删掉重建后，核心数据库和运行状态仍然可以继续使用。
- 真正需要重点迁移的是宿主机目录，不是容器内部临时文件。

---

## 2. 迁移时哪些数据会保留

以下内容迁移后会保留：

- 市场主库：`artifacts/warehouse/market.duckdb`
- 运行数据包：`artifacts/warehouse/package/`
- 运行状态：`artifacts/runtime/runtime_state.json`
- 运行历史：`artifacts/runtime/history/`
- 市场仓库同步历史：`artifacts/runtime/market_warehouse_history.jsonl`
- 市场仓库同步进度：`artifacts/runtime/market_warehouse_progress.json`
- 模型产物：`artifacts/model_v1.json`
- 训练状态：`artifacts/training/bootstrap_state.json`
- 演化/升级历史：`artifacts/evolution/history/`
- 股票 universe 缓存：`artifacts/universe/`
- 策略建议：`suggestions/`

以下内容默认不一定保留：

- Docker 命名卷 `redis_data`

说明：

- `redis_data` 主要是缓存，不是核心市场数据库。
- 即使不迁移 `redis_data`，系统通常仍可重建缓存并继续运行。

---

## 3. 推荐迁移方式

### 方案 A：推荐，整项目复制

直接复制整个项目目录到 NAS。

优点：

- 最省心
- 不容易漏文件
- 配置、脚本、模型、数据、前端、Docker 配置一次到位

建议至少复制以下内容：

- `artifacts/`
- `suggestions/`
- `config/`
- `src/`
- `scripts/`
- `frontend/`
- `tests/`（可选，但建议保留）
- `.env`
- `.env.example`
- `docker-compose.yml`
- `Dockerfile`
- `pyproject.toml`
- `README.md`

如果你已经配置了 AI，还要复制：

- `config/llm.local.yaml`

### 方案 B：最小迁移

如果你只想迁移运行所需的最小集合，至少复制：

- `artifacts/`
- `suggestions/`
- `config/`
- `src/`
- `scripts/`
- `.env`
- `docker-compose.yml`
- `Dockerfile`
- `pyproject.toml`

不建议第一次上 NAS 时走最小迁移，容易漏掉前端、文档或辅助脚本。

---

## 4. 迁移前检查清单

迁移前，先确认以下项目：

- [ ] 本机 Docker 正常
- [ ] `artifacts/warehouse/market.duckdb` 存在
- [ ] `artifacts/warehouse/package/` 下已有数据文件
- [ ] `.env` 已填写必要配置
- [ ] `config/llm.local.yaml` 已填写 AI 配置（如果启用 AI）
- [ ] 企业微信 / 推送 webhook 已配置
- [ ] 首轮需要保留的数据已停止写入

建议在复制前先停容器，避免数据库仍在写入：

```powershell
docker compose down
```

如果你还不想停太久，至少也应先停 `api` 和 `scheduler`，再复制数据目录。

---

## 5. 迁移步骤

### 第 1 步：停机

在当前机器项目根目录执行：

```powershell
docker compose down
```

### 第 2 步：复制项目到 NAS

把整个项目目录复制到 NAS，例如：

- `NAS:/volume1/StockAnalyzer/`

复制后建议检查以下路径是否都存在：

- `artifacts/warehouse/market.duckdb`
- `artifacts/warehouse/package/`
- `artifacts/runtime/`
- `suggestions/`
- `.env`
- `config/llm.local.yaml`

### 第 3 步：在 NAS 安装 Docker / Compose

确保 NAS 满足：

- 支持 Docker
- 能执行 `docker compose`
- 有足够磁盘空间
- 时区建议设置为 `Asia/Shanghai`

### 第 4 步：在 NAS 上启动

进入项目目录后执行：

```bash
docker compose up -d --build
```

### 第 5 步：检查容器

```bash
docker compose ps
docker compose logs api --tail=100
docker compose logs scheduler --tail=100
```

---

## 6. 首轮上线后的验证清单

### 基础服务

- [ ] `api` 容器启动正常
- [ ] `scheduler` 容器启动正常
- [ ] `redis` 容器启动正常

### 数据链

- [ ] `artifacts/warehouse/market.duckdb` 可正常被读取
- [ ] `artifacts/warehouse/package/manifest.json` 已生成
- [ ] `artifacts/runtime/market_warehouse_history.jsonl` 持续更新
- [ ] `artifacts/runtime/market_warehouse_progress.json` 可读取

### 首轮运行

- [ ] 首轮扫描开始执行
- [ ] 候选池 / 观察池被重新生成
- [ ] 开盘前简报可正常生成
- [ ] 盘中扫描可正常执行
- [ ] 晚间增量同步可正常执行
- [ ] 周末深度任务可正常调度

### 消息链

- [ ] 企业微信推送正常
- [ ] 不再重复刷同一条无意义消息
- [ ] 推送内容包含具体股票 / 原因 / 建议动作

### AI / 自学习链

- [ ] `config/llm.local.yaml` 已生效
- [ ] 晚间学习任务可运行
- [ ] 周末升级任务可运行

---

## 7. 首轮建议执行顺序

建议 NAS 上首次上线按这个顺序跑：

1. 先启动 Docker 基础服务
2. 先检查 API、Scheduler、Redis 都活着
3. 先跑 1 只股票冒烟
4. 再跑小批量（例如 20~50 只）
5. 最后再跑全市场首轮

建议命令：

```bash
python -m stock_analyzer.cli warehouse-sync-progress
python -m stock_analyzer.cli warehouse-sync-latest
python -m stock_analyzer.cli warehouse-sync-history --limit 5
```

如果要手工做一次小批量首轮，可以考虑：

```bash
python -m stock_analyzer.cli warehouse-bootstrap-run --max-symbols 20 --daily-only
```

全量首轮建议不要直接在第一次上线后立刻跑满 5000+ 只，最好先做小批量验证。

---

## 8. NAS 迁移后的注意事项

### 不要依赖本机通达信目录

现在的目标架构是：

- 运行主链依赖 `market_warehouse`
- 日常增量依赖在线数据源
- 本地通达信数据只作为可选导入源，不应成为 NAS 运行前提

所以：

- NAS 上不需要再挂载 `D:\通达信\vipdoc`
- 如果未来需要导入旧离线数据，可以单独把数据导入到 `artifacts/imports/tdx_offline_package/`

### 备份重点目录

建议定期备份：

- `artifacts/warehouse/`
- `artifacts/runtime/`
- `artifacts/training/`
- `artifacts/evolution/`
- `suggestions/`
- `.env`
- `config/llm.local.yaml`

### 升级代码前先备份

每次升级版本前，至少备份：

- `artifacts/`
- `suggestions/`
- `.env`
- `config/llm.local.yaml`

---

## 9. 回滚方案

如果 NAS 新环境启动异常，可以这样回滚：

1. 停掉 NAS 上新容器
2. 保留新环境日志
3. 用迁移前备份的项目目录恢复
4. 恢复 `artifacts/` 和 `suggestions/`
5. 重新执行：

```bash
docker compose up -d --build
```

只要 `artifacts/warehouse/market.duckdb` 和 `artifacts/runtime/` 没损坏，通常可以快速恢复。

---

## 10. 最终结论

可以这样理解：

- 现在这套系统已经不是“容器里一关就没”的状态
- 你的核心资产已经落在宿主机目录 `artifacts/` 和 `suggestions/` 中
- 迁移到 NAS 时，只要把项目目录和这些持久化目录一起带过去，数据就还在
- Docker 只是运行壳，真正重要的是宿主机挂载的数据目录

---

## 11. 迁移完成后的建议动作

NAS 上线完成后，建议立刻做这 5 件事：

- [ ] 跑一次 `warehouse-sync-progress`，确认仓库同步状态可见
- [ ] 跑一次小批量 bootstrap，确认在线数据源可用
- [ ] 检查企业微信推送是否正常
- [ ] 检查前端 Dashboard 是否能连上最新数据
- [ ] 再开始安排全市场首轮扫描
