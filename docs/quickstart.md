# StockAnalyzer 快速上手（Quickstart）

面向新用户/部署者的最小上手路径：环境准备 → 安装依赖 → 配置 .env → 启动 → 首次扫描与报告 → 常用 CLI。

## 1. 环境准备

| 依赖 | 要求 | 说明 |
|---|---|---|
| Python | >= 3.11（开发/测试在 3.12 验证） | 见 `pyproject.toml` `requires-python` |
| Git | 任意近期版本 | 拉取代码与分支 |
| Docker + docker compose | 可选，推荐 | 容器化运行（api + scheduler + redis） |
| 通达信（可选） | 仅离线数据源需要 | 生成 `TDX_VIPDOC_HOST_ROOT` 下的离线包 |

A 股交易日历、时区按 `Asia/Shanghai` 处理，部署机建议保持系统时间准确。

## 2. 安装依赖

仓库提供锁文件，先按锁文件装，避免依赖漂移：

```powershell
# 运行时依赖（锁文件）
python -m pip install -r requirements.txt

# 开发依赖（pytest/ruff/mypy/pytest-cov 等，含运行时依赖）
python -m pip install -r requirements-dev.txt
```

也可以直接用 pyproject 的可编辑安装（开发模式）：

```powershell
python -m pip install -e .[dev]
```

> 注意：`requirements.txt` / `requirements-dev.txt` 由 `pip-compile` 生成，
> 如需升级依赖请改 `pyproject.toml` 后重新 compile，不要手改锁文件。

## 3. 配置 .env

把仓库根目录的 `.env.example` 复制为 `.env` 并填写关键项：

```powershell
Copy-Item .env.example .env
```

### 关键变量表（占位脱敏）

| 变量 | 必填 | 说明 |
|---|---|---|
| `SA__APP__MODE` | 是 | `simulation`（默认）或 `live` |
| `SA__APP__ADVISORY_ONLY` | 是 | `true`：只分析与推送，不自动下单 |
| `SA__COMMAND_CHANNEL__SECRET_KEY` | 是 | 指令通道 HMAC 密钥，必须替换为强随机值 |
| `SA__SECURITY__API_TOKEN` | 是 | API 写操作鉴权 token（未设置时按 fail-closed 处理） |
| `SA__DATA_SOURCE__PRIMARY` | 是 | `market_warehouse`（默认）/ `tushare` / `vendor_zip_overlay` 等 |
| `SA__MARKET_WAREHOUSE__TUSHARE_TOKEN` | 按需 | 使用 tushare 在线补数据时的 token，勿提交到 git |
| `SA__TDX_SYNC__VIPDOC_ROOT` | 按需 | 通达信 vipdoc 目录（Windows 本地路径） |
| `SA_API_HOST_PORT` | 否 | compose 对外端口，默认 `8001` |
| `SA__NOTIFICATIONS__PRIMARY` / `BACKUP` | 否 | `console`（默认）/ `pushplus` / `wecom` / `feishu` 等 |
| `SA__NOTIFICATIONS__FEISHU_APP_ID` / `APP_SECRET` | 按需 | 飞书应用凭据，填写真实值后 `PRIMARY=feishu_app` |

安全底线：

- 任何 token / secret 只放本地或 NAS 的 `.env`，**禁止提交到 git**。
- 不要直接改 `config/default.yaml` 里的密钥类配置，用环境变量覆盖（`SA__` 前缀）。

## 4. 启动

### 方式 A：docker compose（推荐，现状命令）

```powershell
# 构建镜像
docker compose build api

# 首次启动用 firstscan 覆盖：通知走 console、不自动跑后台任务
docker compose -f docker-compose.yml -f docker-compose.firstscan.yml up -d redis api

# 查看日志
docker compose -f docker-compose.yml -f docker-compose.firstscan.yml logs -f api
```

调度器（后台定时任务）默认在 firstscan 模式下不启动；确认就绪后再显式拉起：

```powershell
docker compose -f docker-compose.yml -f docker-compose.firstscan.yml --profile scheduler up -d scheduler
```

普通例行启动（本地开发机）优先用脚本：

```powershell
powershell -File scripts/start_runtime_stack_localvol.ps1 -SkipScheduler
```

### 方式 B：本地裸跑

```powershell
python -m pip install -r requirements-dev.txt
uvicorn stock_analyzer.main:app --reload --host 0.0.0.0 --port 8000
```

启动后验证：`GET http://localhost:8000/health`（或按 `SA_API_HOST_PORT` 端口）。

## 5. 首个扫描 / 报告触发

API 健康后做首次受控扫描（`notify_enabled=false` 避免打扰）：

```powershell
$body = @{ symbols = @('600000','000001'); notify_enabled = $false; sync_watchlist = $false; sync_reason = 'quickstart' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri 'http://localhost:8001/week5/scan/run' -ContentType 'application/json' -Body $body
Invoke-RestMethod -Method Post -Uri 'http://localhost:8001/week6/run' -ContentType 'application/json' -Body (@{ symbols = @('600000','000001'); notify_enabled = $false } | ConvertTo-Json)
```

> 写操作端点需要鉴权头：`Authorization: Bearer <SA__SECURITY__API_TOKEN>` 或
> `X-SA-API-Key: <SA__SECURITY__API_TOKEN>`。扫描类长任务返回 `202` + `task_id`，
> 用 `GET /tasks/{task_id}` 轮询结果（详见 docs/troubleshooting.md「202 任务查询」）。

触发复盘报告：

```powershell
# 月度复盘（结构化 JSON 落盘 artifacts/research/phase_d/）
python -m stock_analyzer.cli monthly-review --year-month 2026-03

# 复盘日报（日粒度结构化数据，前端/用户查看用）
python -m stock_analyzer.cli daily-review --date 2026-03-10
```

## 6. CLI 常用命令

```powershell
# 分析流水线
python -m stock_analyzer.cli run --symbols "600000,000001" --strategy trend

# 新闻情绪
python -m stock_analyzer.cli news-score --symbol 600000 --strategy trend

# 组合与对账
python -m stock_analyzer.cli portfolio-positions
python -m stock_analyzer.cli portfolio-trades --limit 50
python -m stock_analyzer.cli reconcile-run
python -m stock_analyzer.cli reconcile-latest

# 复盘报告
python -m stock_analyzer.cli monthly-review
python -m stock_analyzer.cli monthly-review-latest
python -m stock_analyzer.cli daily-review
python -m stock_analyzer.cli daily-review-latest

# 扫描 / 因子 / 周报
python -m stock_analyzer.cli week5-scan-run --symbols "600000,000001" --notify-enabled false
python -m stock_analyzer.cli week6-run --symbols "600000,000001" --notify-enabled false
python -m stock_analyzer.cli week7-sim-broker-run --days 7

# 运维与审计
python -m stock_analyzer.cli scheduler-run-due
python -m stock_analyzer.cli runtime-sla --recent-runs 50
python -m stock_analyzer.cli audit-events --limit 100
```

完整命令清单见 README.md 的 `## CLI` 一节。

## 7. 测试 / 质量门禁（开发态）

```powershell
# 全量测试
pytest

# 单模块
pytest tests/test_research_daily_review_report.py

# 静态检查与类型检查
ruff check src tests
mypy src

# 覆盖率门禁示例（evolution 模块 >= 80%）
pytest tests -k evolution --cov=src/stock_analyzer/evolution --cov-fail-under=80
```

遇到常见问题（401/403、423 参数冻结、202 轮询、artifacts 污染、覆盖率门禁、数据源降级）请先看 [docs/troubleshooting.md](troubleshooting.md)。
