# StockAnalyzer

A-share quantitative analysis scaffold based on `implementation_plan.md` v7.

## Quick Start

```bash
python -m venv .venv
. .venv/Scripts/activate
pip install -e .[dev]
make test
uvicorn stock_analyzer.main:app --reload
```

## V1.3 Ops Docs

- `docs/v13_deployment_guide.md`
- `docs/fn_nas_deployment_runtime_localvol.md`
- `docs/vendor_zip_overlay_nas_deployment.md`
- `docs/signal_tuning_v1_v2_20260320.md`
- `docs/v13_training_and_acceptance.md`
- `docs/v13_runtime_operations.md`
- `docs/pre_release_checklist.md`
- `docs/rollback_checklist.md`
- `docs/phase_d_extension_backlog.md`

## Feishu Long Connection

Use this mode when you do not have a public callback URL. The application keeps a persistent outbound connection to Feishu and receives message events directly.

1. In Feishu Open Platform, open your self-built app and enable the bot capability.
2. In Event Subscriptions, choose the long connection mode instead of Request URL mode.
3. Subscribe the message receive event `im.message.receive_v1`.
4. Grant at least these permissions: `im:message.p2p_msg:readonly` and `im:message:send_as_bot`.
5. Publish the app version and make sure the target user can start a private chat with the bot.
6. Configure the runtime:

```env
SA__FEISHU_INTERACTION__ENABLED=true
SA__FEISHU_INTERACTION__SUBSCRIPTION_MODE=long_connection
SA__NOTIFICATIONS__FEISHU_APP_ID=cli_xxx
SA__NOTIFICATIONS__FEISHU_APP_SECRET=xxx
```

7. Restart the service and verify status with `GET /feishu/long_connection/status`.

## Scope Implemented In This Iteration

- Config system aligned with plan section 19 (`config/default.yaml`)
- Data provider abstraction + resilient fallback/degrade switch
- Cache layer (`memory` / optional `redis`)
- Data health monitor (success-rate / latency degradation)
- T-1 feature engineering to avoid future leakage
- Double-model cross review gate + scoring engine
- Risk controls: degrade stop-new-buy, capital-curve guard, circuit breaker
- Soup strategy decision module
- Signed command channel (HMAC + idempotency)
- Daily scheduler (`08:30` / `09:26` / `15:30`)
- Primary/backup notifier channels (console/pushplus/wecom/feishu/feishu_app/telegram/email/custom_webhook)
- WeCom callback interaction bridge (`/wecom/callback`) supporting plain + safe mode
- Feishu app interaction bridge supporting webhook and long-connection event subscription
- Label alignment (`TP before SL`, horizon-based)
- Dual-model training pipeline + isotonic calibration + artifact persistence
- Walk-forward backtest with matcher (`T+1`, limit-up/down checks, cost model)
- Portfolio book (max holdings / max hold days / trade audit log)
- Intelligent notification filter (score threshold + cooldown de-dup)
- Command-to-portfolio linkage (`SET_POSITION` / `CLOSE_POSITION`)
- Manual pause/resume of new buys (`PAUSE_NEW_BUY` / `RESUME_NEW_BUY`)
- Dual-channel reconciliation (strategy book vs broker snapshot)
- 15:30 forced reconcile task with mismatch alert + weekly summary
- Dashboard portfolio snapshot API (positions/equity curve/execution quality)
- Runtime SLA metrics (latency p50/p95/compliance)
- Built-in stress scenario suite (2015/2016/2018/2020/2023/2024)
- Dockerized runtime (`api + scheduler + redis`)
- Backtest matcher upgrades (gap stop-loss, deferred exit on non-tradable bars, dynamic slippage)
- End-to-end audit stream and trace replay (`signal -> notify -> command -> reconcile`)
- Dashboard command deck for one-click pause/resume/reconcile/manual position ops
- News component preview chain (`/news/score` + batch/watchlist/history + cache state/clear + WeCom `news` / `newslist` / `newscache`)
- Dashboard news panel upgrades (watchlist snapshot + cache controls with optional symbol/strategy filter + latest score history)
- Week4 acceptance automation (`SLA + stress + matcher + scheduler + Docker assets`)
- Acceptance report export (`JSON + CSV`) and scheduled auto-run with notifications
- Week5 scan engine (`first-board/leaderboard + anomaly alerts + empty-signal + monster isolation`)
- Dashboard Week5 visualization (first-board candidates, anomalies, empty-signal/isolation status)
- Week6 factor engines (main-force tracking + multi-strategy allocation + calendar/global/regulatory factors)
- 08:30 premarket auto global snapshot collection (with correlation estimation and history)
- Week6 state persistence (`global snapshot/history + regulatory watchlist`)
- Week7 strategy kill switch v1 (manual monthly performance input, consecutive underperform trigger, execution block)
- Week7 cloud backup v1 (heartbeat ping, offline watchdog, 15-min outage alert with portfolio snapshot)
- Week7 factor lifecycle v1 (monthly top-factor drift + PSR observation monitor)
- Week7 sim-vs-broker weekly report (bias attribution + account/strategy drill-down + trend + artifact export)
- Week8 evolution gate dashboard v1 (preflight/window checks + blocking release gate attempt)
- Week8 release workflow v6 (ticket timeline + status filter dashboard + lifecycle view)
- Evolution off-hours dry-run loop v1 (M9 -> DAG/Fusion -> Proposal -> Governance -> Compliance/Manifest)
- Evolution M5 label optimization scoring (coverage/balance/seed-consistency/alignment diagnostics)
- Evolution M8 six-gate enhancement (PCV + DeflatedSharpe/FDR + LLM + noise + random-walk + registry)
- Evolution M10 model health scoring (prediction conflict/calibration diagnostics)
- Evolution M11 shadow portfolio checks (three redlines + attribution report)
- Evolution production preflight checks (dependencies + writable paths + critical config sanity)
- Evolution validation window report (run count + artifact integrity + compliance flow checks)
- Learning governance workflow v1 (proposal -> approval -> release ticket -> rollback/watchdog -> status)
- Evolution Batch2 foundation (M1 dual learning, M2 regime cooldown, M3 pattern memory/safe snapshot delete)
- Idle Queue v3.2 implementation (workday chain + full weekend WE-P0/WE-P1/WE-P2 tasks, weekend rotating/defer/force-run scheduling, path whitelist+blacklist guardrails, checkpoint + fallback TTL, TimeGuard sync fail-fast, manual-ack recovery, on-disk idle history + staging growth SLA metrics, /idle/state observability + dashboard panel + one-click ACK operations)
- Runtime service orchestration + FastAPI endpoints + CLI
- Advisory-only execution mode (`app.advisory_only=true`: keep analysis/push, skip auto portfolio signal apply)
- Manual fill tracking for advisory mode (`SET_POSITION` supports `entry_price/quantity/fee/account/trade_time/note` and persists into position/trade records for follow-up tracking)
- Manual close fill tracking (`CLOSE_POSITION` supports `exit_price/quantity/fee/account/trade_time/note` and persists sell-fill details into trade records)
- Manual `SET_POSITION` auto-tracks symbol into runtime watchlist (deduplicated) to ensure later analysis/push keeps following manually entered holdings
- Recommendation lifecycle tracking (`recommended/bought/watching/dropped`) linked with pipeline signals and manual dashboard commands
- Manual cost-basis holding alerts (stop-loss/take-profit/max-hold warning) surfaced in dashboard and pipeline payload, with deduplicated warn push
- Recommendation-vs-manual execution bias analytics (position/price deviation summary and dashboard drill-down)
- Reconcile supports optional broker `quantity/account` dimensions in addition to target position; mismatch report now includes `quantity_diffs/account_diffs`
- Close-time daily digest push template (reconcile status + buy recommendations + holding alerts + 7-day execution bias summary, same-day deduplicated)
- Dashboard enhancements for lifecycle/alerts/bias panels (filter + sort + pagination + CSV export)
- Runtime history archive + retention policy (daily snapshot at close with configurable `retention_days/max_records`)
- Pytest baseline tests

## API

路由处理器按领域拆分在 `src/stock_analyzer/api/`（20 个路由模块 + `deps.py` 共享依赖 +
`models.py` 请求模型），`src/stock_analyzer/main.py` 只负责组装 app、挂载 router 与
前端静态页。总计 **183 个路由**（`api/` 180 个 + `main.py` 的 `/ui` 静态页 3 个）。

认证与约定：

- 写操作端点（POST）经统一依赖 `_verify_api_auth` 鉴权，支持
  `Authorization: Bearer <token>` 或 `X-SA-API-Key: <token>`（无凭证 401、凭证错误 403；
  未显式设置 `SA__SECURITY__API_AUTH_ENABLED` 时 fail-closed 强制开启）。
- 例外：`POST /command/execute` 走签名指令通道（HMAC + 幂等）；`GET/POST /wecom/callback`、
  `POST /feishu/callback` 使用企微/飞书平台签名与 Token 校验。
- GET 端点只读、公开；Dashboard 页面端点与 `/ui` 静态页 `include_in_schema=False`，
  不出现在 `/docs`。
- **202 异步端点**：长耗时写端点（`POST /run/pipeline`、`POST /evolution/run`、
  `POST /train/*`、`POST /research/*/report`、`POST /tdx/sync/run`、
  `POST /warehouse/sync/run` 等）经 FastAPI `BackgroundTasks` 异步执行，立即返回
  `202 Accepted` + `task_id`（`src/stock_analyzer/ops/background_tasks.py` 注册表，
  状态 `queued/running/succeeded/failed`），任务状态经 `GET /tasks` /
  `GET /tasks/{task_id}` 轮询。异步化按模块陆续落地中，未列出的端点仍为同步响应。

### health（`api/health.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活探针（版本/构建信息） |
| GET | `/health/deep` | 深度健康检查（关键依赖与子系统状态） |

### dashboard（`api/dashboard.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/dashboard`、`/dashboard/` | Dashboard 网页（`include_in_schema=False`） |
| GET | `/dashboard/recommendations` | 推荐生命周期网页（`include_in_schema=False`） |
| GET | `/dashboard/stage` | 阶段状态网页（`include_in_schema=False`） |
| GET | `/dashboard/portfolio?days=7&trade_limit=120` | 组合快照（持仓/净值曲线/执行质量） |
| GET | `/dashboard/training-overview?history_limit=6` | 训练进度总览 |
| GET | `/dashboard/ops/state` | 运维开关状态 |
| POST | `/dashboard/ops/toggle` | 一键暂停/恢复新买等运维开关 |
| POST | `/dashboard/command/quick` | 快速指令（暂停买入/清仓/人工持仓等） |
| POST | `/dashboard/reconcile/quick` | 快速对账录入 |

### news（`api/news.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/news/score?symbol=600000&strategy=trend` | 单标的新闻情绪评分预览 |
| GET | `/news/score/batch?symbols=600000&symbols=000001&strategy=trend` | 批量评分预览 |
| GET | `/news/score/watchlist?strategy=trend&limit=20` | 关注池评分预览 |
| GET | `/news/briefing/latest` | 最新早报摘要 |
| GET | `/news/score/history?limit=50&symbol=&strategy=` | 评分历史 |
| GET | `/news/score/cache/state` | 评分缓存状态 |
| POST | `/news/score/cache/clear` | 清空评分缓存 |

### notifications（`api/notifications.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/notify/test` | 测试通知通道（需 `SA__SECURITY__NOTIFY_TEST_ENABLED=true`） |

> `POST /notify/test` 在 `SA__SECURITY__API_AUTH_ENABLED=true` 时受 API 鉴权保护；
> 裸 `test` / `t` / `c` 通知负载默认被
> `SA__SECURITY__SUPPRESS_PLAIN_TEST_NOTIFICATIONS=true` 抑制。

### commands（`api/commands.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/command/execute` | 执行签名指令（HMAC 指令通道，非 API 鉴权） |
| GET | `/command/state` | 指令通道状态 |

### messaging（`api/messaging.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/wecom/callback` | 企微回调 URL 校验（plain/safe 模式，平台签名校验） |
| POST | `/wecom/callback` | 接收企微文本指令并执行（平台签名校验） |
| GET | `/feishu/long_connection/status` | 飞书长连接状态 |
| POST | `/feishu/callback` | 飞书 webhook 回调（仅 webhook 模式，Token 校验） |

企微文本指令：`help`、`positions`、`trades`、`news`、`newslist`、`newscache state`、
`newscache clear [symbol] [strategy]`、`newshistory [limit] [symbol] [strategy]`、
`mode`、`mode advisory on/off`、`lifecycle [status] [limit]`、
`recstatus <symbol> <recommended|bought|watching|dropped> [note]`、
`holdingalerts [warn|info]`、`bias [days] [limit]`。

### market（`api/market.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/run/pipeline` | 运行分析流水线（symbols/策略） |
| GET | `/risk/status` | 风控状态（降级/熔断/暂停买入） |
| GET | `/signals/latest` | 最新信号 |
| POST | `/scheduler/run_due` | 手动触发到期调度任务（`job`/`jobs` 选择器） |
| POST | `/tdx/sync/run` | 手动触发 TDX 离线包同步 |
| GET | `/tdx/sync/latest` | 最近一次 TDX 同步结果 |
| GET | `/tdx/sync/history?limit=20` | TDX 同步历史 |
| POST | `/warehouse/sync/run` | 手动触发市场仓库同步 |
| GET | `/warehouse/sync/latest` | 最近一次仓库同步结果 |
| GET | `/warehouse/sync/status` | 仓库同步状态 |
| GET | `/warehouse/background/status` | 仓库后台任务状态 |
| GET | `/warehouse/sync/history?limit=20` | 仓库同步历史 |
| GET | `/tasks?limit=50` | 最近提交的后台任务列表（新→旧） |
| GET | `/tasks/{task_id}` | 后台任务生命周期条目（404 表示不存在） |

### idle（`api/idle.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/idle/run` | 手动触发闲时任务队列 |
| GET | `/idle/latest` | 最近一次闲时运行结果 |
| GET | `/idle/history?limit=20` | 闲时运行历史 |
| GET | `/idle/state` | 闲时队列状态（观察性） |
| POST | `/idle/ack` | 手动确认任务（`WD-P0-01` 等） |

### evolution（`api/evolution.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/evolution/run` | 运行进化干跑 |
| POST | `/evolution/drill` | 进化演练 |
| GET | `/evolution/latest` | 最近进化结果 |
| GET | `/evolution/history?limit=20` | 进化历史 |
| GET | `/evolution/preflight` | 生产前置检查 |
| GET | `/evolution/window_report?days=10&min_runs=5` | 验证窗口报告 |
| POST | `/evolution/m3/maintenance` | M3 模式记忆维护 |
| POST | `/evolution/m3/search` | M3 记忆检索 |
| POST | `/evolution/m8/suggest` | M8 六门增强建议 |
| POST | `/evolution/release/attempt` | 发布门禁尝试 |
| GET | `/evolution/release/latest` | 最近发布尝试 |
| GET | `/evolution/release/history?limit=20` | 发布尝试历史 |
| POST | `/evolution/release/approval` | 发布审批 |
| GET | `/evolution/release/approval/latest` | 最近审批 |
| GET | `/evolution/release/approval/history?limit=20` | 审批历史 |
| POST | `/evolution/release/ticket` | 创建发布工单 |
| POST | `/evolution/release/ticket/execute` | 执行发布工单 |
| POST | `/evolution/release/ticket/confirm` | 确认发布工单 |
| POST | `/evolution/release/ticket/rollback` | 回滚发布工单 |
| POST | `/evolution/release/confirmation/watchdog` | 发布确认看门狗 |
| GET | `/evolution/release/ticket/latest` | 最近发布工单 |
| GET | `/evolution/release/ticket/history?limit=20` | 发布工单历史 |
| GET | `/evolution/release/ticket/timeline?ticket_id=&status=&limit=200` | 发布工单时间线 |

### training（`api/training.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/train/models` | 训练模型 |
| POST | `/train/learning-manifest` | 执行学习清单 |
| POST | `/train/learning-manifest/shadow-validate` | 影子验证 |
| POST | `/train/learning-manifest/shadow-promote` | 影子晋级 |
| POST | `/train/learning-manifest/shadow-proposal` | 影子提案 |
| POST | `/train/execution-risk` | 执行风险评分 |
| GET | `/train/execution-risk/status` | 执行风险状态 |
| GET | `/train/execution-risk/history` | 执行风险历史 |
| GET | `/train/bootstrap/status` | 引导模型就绪状态 |

### learning（`api/learning.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/learning/models/proposal` | 创建模型提案 |
| GET | `/learning/models/proposal/latest` | 最近提案 |
| GET | `/learning/models/proposal/history?limit=20&proposal_id=&status=` | 提案历史 |
| POST | `/learning/models/proposal/approval` | 提案审批 |
| POST | `/learning/models/proposal/revoke` | 提案撤销 |
| GET | `/learning/models/proposal/approval/latest` | 最近审批 |
| GET | `/learning/models/proposal/approval/history?limit=20` | 审批历史 |
| POST | `/learning/models/release/ticket` | 创建发布工单 |
| POST | `/learning/models/release/ticket/execute` | 执行发布工单 |
| POST | `/learning/models/release/ticket/confirm` | 确认发布工单 |
| POST | `/learning/models/release/ticket/rollback` | 回滚发布工单 |
| POST | `/learning/models/release/confirmation/watchdog` | 发布确认看门狗 |
| GET | `/learning/models/release/ticket/latest` | 最近发布工单 |
| GET | `/learning/models/release/ticket/history?limit=20` | 发布工单历史 |
| GET | `/learning/models/release/ticket/timeline?ticket_id=&status=&limit=200` | 发布工单时间线 |
| GET | `/learning/models/governance/status?proposal_limit=20&ticket_limit=20` | 治理状态汇总 |
| POST | `/learning/runtime-history/bootstrap` | 运行时历史引导 |
| GET | `/learning/status` | 学习状态 |
| GET | `/learning/store/status` | 学习存储状态 |
| GET | `/learning/store/metrics` | 学习存储指标 |
| GET | `/learning/manifests/status` | 清单状态 |

### model_registry（`api/model_registry.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/models/registry/promotion-gate` | 模型晋级门禁 |
| GET | `/models/registry` | 注册表条目 |
| GET | `/models/registry/status` | 注册表状态 |
| POST | `/models/registry/register` | 注册模型工件 |
| POST | `/models/registry/bootstrap-active-champion` | 引导活跃冠军 |
| POST | `/models/registry/lifecycle` | 更新模型生命周期 |
| POST | `/models/registry/role` | 更新模型角色 |
| GET | `/models/registry/{model_id}` | 单模型条目 |
| POST | `/models/shadow-dataset` | 构建影子数据集 |
| POST | `/models/champion-shadow-report` | 冠军 vs 影子报告 |
| POST | `/models/shadow-online-v2-report` | 线上 v2 影子报告 |
| GET | `/shadow/v2/status` | 影子 v2 状态 |
| POST | `/models/execution-aware-report` | 执行感知报告 |

### research（`api/research.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/research/signal-quality/run` | 信号质量审计 |
| GET | `/research/signal-quality/latest` | 最近信号质量审计 |
| GET | `/research/signal-quality/history` | 信号质量审计历史 |
| POST | `/research/alphalens/report` | Alphalens 归因报告 |
| POST | `/research/shap/report` | SHAP 稳定性报告 |
| POST | `/research/catboost-shadow/report` | CatBoost 影子报告 |
| POST | `/research/finbert/report` | FinBERT 情绪报告 |
| POST | `/research/qlib-bridge/report` | Qlib 桥接报告 |
| POST | `/research/tabular-deep/report` | 表格深度模型报告 |
| POST | `/research/tft/report` | TFT 时序报告 |
| POST | `/research/finrl/report` | FinRL 强化学习报告 |
| POST | `/research/heavy-ts/report` | Heavy-TS 时序报告 |
| GET | `/research/d6/registry` | Phase-D 研究注册表 |

### acceptance（`api/acceptance.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/backtest/walk_forward` | Walk-Forward 回测 |
| POST | `/acceptance/baseline` | 基线报告 |
| POST | `/acceptance/phase_checkpoint` | 阶段检查点 |
| POST | `/acceptance/v13` | V1.3 验收 |
| POST | `/acceptance/v13/bundle` | V1.3 组合验收 |
| POST | `/stress/run` | 压力测试 |
| POST | `/acceptance/week4/run` | Week4 验收自动运行 |
| GET | `/acceptance/week4/latest` | 最近 Week4 验收 |
| GET | `/acceptance/week4/history?limit=20` | Week4 验收历史 |

### portfolio（`api/portfolio.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/portfolio/positions` | 持仓 |
| GET | `/portfolio/trades?limit=100` | 成交记录 |
| GET | `/recommendations/lifecycle?status=&limit=120` | 推荐生命周期 |
| GET | `/portfolio/execution_bias?days=30&limit=200` | 执行偏差分析 |
| POST | `/portfolio/broker_snapshot` | 提交券商快照 |
| POST | `/portfolio/reconcile/run` | 触发对账 |
| GET | `/portfolio/reconcile/latest` | 最近对账 |
| GET | `/portfolio/reconcile/weekly?days=7` | 对账周报 |

### runtime（`api/runtime.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/runtime/sla?recent_runs=50` | SLA 指标 |
| GET | `/runtime/stage` | 运行时阶段 |
| GET | `/runtime/stage/deep` | 深度阶段状态 |
| GET | `/runtime/history/archive/status?limit=20` | 历史归档状态 |
| POST | `/runtime/history/archive/run` | 触发历史归档 |
| GET | `/m3/profile/status` | M3 画像状态 |

### week5（`api/week5.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/week5/scan/run` | 首板扫描运行 |
| GET | `/week5/scan/latest` | 最近扫描 |
| GET | `/week5/scan/history?limit=20` | 扫描历史 |
| GET | `/week5/signal-pool/live` | 实时信号池 |
| GET | `/week5/signal-pool/symbol/live` | 单标的实时信号 |

### week6（`api/week6.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/week6/run` | Week6 因子运行 |
| GET | `/week6/latest` | 最近运行 |
| GET | `/week6/history?limit=20` | 运行历史 |
| POST | `/week6/data-quality/run` | 数据质量检查 |
| GET | `/week6/data-quality/latest` | 最近数据质量 |
| GET | `/week6/data-quality/history` | 数据质量历史 |
| POST | `/week6/global/snapshot` | 设置全球市场快照 |
| GET | `/week6/global/snapshot` | 获取全球市场快照 |
| GET | `/week6/global/history?limit=50` | 全球快照历史 |
| POST | `/week6/regulatory/watchlist` | 设置监管关注池 |
| GET | `/week6/regulatory/watchlist` | 获取监管关注池 |

### week7（`api/week7.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/week7/kill-switch/performance` | 录入月度绩效 |
| GET | `/week7/kill-switch/history?strategy=&limit=60` | 策略自毁开关历史 |
| GET | `/week7/kill-switch/status?strategy=` | 策略自毁开关状态 |
| POST | `/week7/kill-switch/reset` | 重置自毁开关 |
| POST | `/week7/cloud-backup/ping` | 云冷备心跳 |
| GET | `/week7/cloud-backup/status` | 云冷备状态 |
| POST | `/week7/cloud-backup/check` | 云冷备离线检查 |
| POST | `/week7/factor-lifecycle/record` | 因子生命周期录入 |
| GET | `/week7/factor-lifecycle/status?strategy=` | 因子生命周期状态 |
| GET | `/week7/factor-lifecycle/history?strategy=&limit=60` | 因子生命周期历史 |
| GET | `/week7/factor-lifecycle/graveyard` | 因子坟墓（下架因子） |
| POST | `/week7/factor-lifecycle/reset` | 重置因子生命周期 |
| POST | `/week7/sim-broker/run` | 模拟盘 vs 券商周报 |
| GET | `/week7/sim-broker/latest` | 最近周报 |
| GET | `/week7/sim-broker/history?limit=20` | 周报历史 |

### audit（`api/audit.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/audit/events?limit=200&event_type=&trace_id=` | 审计事件流 |
| GET | `/audit/trace/{trace_id}` | 审计链路回放 |

### main 静态页（`src/stock_analyzer/main.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/ui`、`/ui/`、`/ui/{path:path}` | 前端静态页（`include_in_schema=False`） |

`POST /dashboard/command/quick` supports manual fill fields when `action=SET_POSITION`:

```json
{
  "action": "SET_POSITION",
  "payload": {
    "symbol": "600000",
    "strategy": "manual",
    "target_position": 0.2,
    "recommendation_id": "REC-4A2B6C8D9E0F1234",
    "entry_price": 9.88,
    "quantity": 800,
    "fee": 2.5,
    "account": "acc-ui",
    "trade_time": "2026-03-01T10:11:12",
    "note": "manual buy"
  }
}
```

In this mode, the system does not auto-trade; it only records your manual fills and keeps tracking risk/reconcile/alerts on those positions.
`recommendation_id` is optional but recommended to bind your manual execution to a specific generated recommendation snapshot.

Recommendation lifecycle can be manually updated from dashboard quick command too:

```json
{
  "action": "SET_RECOMMENDATION_STATUS",
  "payload": {
    "symbol": "600000",
    "status": "watching",
    "strategy": "manual",
    "note": "wait for better entry"
  }
}
```

`GET /portfolio/reconcile/weekly` now includes:
- `status_breakdown` (e.g. `ok` / `mismatch` / `missing_snapshot` / `disabled` counts)
- `sim_vs_broker` summary (`alignment_rate`, mismatch cause breakdown, top diff symbols)

`POST /week7/sim-broker/run` now includes:
- `drilldown.accounts` + `drilldown.account_summary` (strategy-book vs broker snapshot)
- `drilldown.strategies` (per-strategy mismatch/exposure/manual-ratio breakdown)
- `trend.points` (latest 12 weekly snapshots for dashboard trend rendering)

## CLI

- `python -m stock_analyzer.cli run --symbols "600000,000001" --strategy trend`
- `python -m stock_analyzer.cli run --symbols "600000,000001" --strategy multi`
- `python -m stock_analyzer.cli news-score --symbol 600000 --strategy trend`
- `python -m stock_analyzer.cli news-score-batch --symbols "600000,000001" --strategy trend`
- `python -m stock_analyzer.cli news-score-watchlist --strategy trend --limit 20`
- `python -m stock_analyzer.cli news-score-history --limit 50 --symbol 600000 --strategy trend`
- `python -m stock_analyzer.cli news-score-cache-state`
- `python -m stock_analyzer.cli news-score-cache-clear --symbol 600000 --strategy trend`
- `python -m stock_analyzer.cli sign-command --action SET_EQUITY --payload "{\"current_equity\":0.98}"`
- `python -m stock_analyzer.cli scheduler-run-due --now "2026-03-01T09:30:00"`
- `python -m stock_analyzer.cli idle-run --now "2026-03-02T20:40:00"`
- `python -m stock_analyzer.cli idle-latest`
- `python -m stock_analyzer.cli idle-history --limit 20`
- `python -m stock_analyzer.cli idle-state`
- `python -m stock_analyzer.cli idle-ack --task-id WD-P0-01`
- `python -m stock_analyzer.cli evolution-run --symbols "600000,000001" --dry-run true --now "2026-03-02T20:40:00"`
- `python -m stock_analyzer.cli evolution-drill --now "2026-03-02T20:41:00"`
- `python -m stock_analyzer.cli evolution-latest`
- `python -m stock_analyzer.cli evolution-history --limit 20`
- `python -m stock_analyzer.cli evolution-preflight --fail-on-not-ready true`
- `python -m stock_analyzer.cli evolution-window-report --days 10 --min-runs 5 --fail-on-fail true`
- `python -m stock_analyzer.cli evolution-llm-compare --symbol 600000.SH`
- `python -m stock_analyzer.cli evolution-release-attempt --days 10 --min-runs 5 --fail-on-blocked true`
- `python -m stock_analyzer.cli evolution-release-latest`
- `python -m stock_analyzer.cli evolution-release-history --limit 20`
- `python -m stock_analyzer.cli evolution-release-approve --approver risk_committee --approved true --note "gate passed"`
- `python -m stock_analyzer.cli evolution-release-approval-latest`
- `python -m stock_analyzer.cli evolution-release-approval-history --limit 20`
- `python -m stock_analyzer.cli evolution-release-ticket-issue --operator release_manager --note "manual order"`
- `python -m stock_analyzer.cli evolution-release-ticket-execute --executor release_manager --confirm-window true --note "release done"`
- `python -m stock_analyzer.cli evolution-release-ticket-confirm --confirmer risk_committee --note "checks passed"`
- `python -m stock_analyzer.cli evolution-release-ticket-rollback --rollback-by risk_committee --note "rollback"`
- `python -m stock_analyzer.cli evolution-release-confirmation-watchdog --now "2026-03-10T20:40:00"`
- `python -m stock_analyzer.cli evolution-release-ticket-latest`
- `python -m stock_analyzer.cli evolution-release-ticket-history --limit 20`
- `python -m stock_analyzer.cli evolution-release-ticket-timeline --status executed --limit 200`
- `python -m stock_analyzer.cli learning-model-proposal-create --model-id model_shadow_test --champion-model-id model_champion_test --split-names test`
- `python -m stock_analyzer.cli train-learning-manifest-shadow-proposal --dataset-manifest-id dataset_manifest_v1_test --split-names test`
- `python -m stock_analyzer.cli learning-model-proposal-latest`
- `python -m stock_analyzer.cli learning-model-proposal-history --limit 20 --status generated`
- `python -m stock_analyzer.cli learning-model-proposal-approve --approver risk_committee --proposal-id LRN-PRP-0001 --note "gate passed"`
- `python -m stock_analyzer.cli learning-model-proposal-revoke --revoked-by risk_committee --proposal-id LRN-PRP-0001 --note "halt rollout"`
- `python -m stock_analyzer.cli learning-model-proposal-approval-latest`
- `python -m stock_analyzer.cli learning-model-proposal-approval-history --limit 20`
- `python -m stock_analyzer.cli learning-model-release-ticket-issue --operator release_manager --proposal-id LRN-PRP-0001 --note "manual release"`
- `python -m stock_analyzer.cli learning-model-release-ticket-execute --executor release_manager --ticket-id LRN-TKT-0001 --note "release done"`
- `python -m stock_analyzer.cli learning-model-release-ticket-confirm --confirmer risk_committee --ticket-id LRN-TKT-0001 --note "checks passed"`
- `python -m stock_analyzer.cli learning-model-release-ticket-rollback --rollback-by risk_committee --ticket-id LRN-TKT-0001 --note "rollback"`
- `python -m stock_analyzer.cli learning-model-release-confirmation-watchdog --now "2026-03-10T20:40:00"`
- `python -m stock_analyzer.cli learning-model-release-ticket-latest`
- `python -m stock_analyzer.cli learning-model-release-ticket-history --limit 20`
- `python -m stock_analyzer.cli learning-model-release-ticket-timeline --status confirmed --limit 200`
- `python -m stock_analyzer.cli learning-model-governance-status --proposal-limit 20 --ticket-limit 20`
- `python -m stock_analyzer.cli train-models --symbol 600000 --lookback-days 600`
- `python -m stock_analyzer.cli walk-forward --symbol 600000 --lookback-days 800`
- `python -m stock_analyzer.cli stress-run`
- `python -m stock_analyzer.cli portfolio-positions`
- `python -m stock_analyzer.cli portfolio-trades --limit 50`
- `python -m stock_analyzer.cli recommendation-lifecycle --status watching --limit 120`
- `python -m stock_analyzer.cli recommendation-status-set --symbol 600000 --status watching --strategy manual --note "wait setup"`
- `python -m stock_analyzer.cli portfolio-holding-alerts --severity warn`
- `python -m stock_analyzer.cli portfolio-execution-bias --days 30 --limit 200`
- `python -m stock_analyzer.cli sign-command --action PAUSE_NEW_BUY`
- `python -m stock_analyzer.cli sign-command --action SET_POSITION --payload "{\"symbol\":\"600000\",\"strategy\":\"manual\",\"target_position\":0.2}"`
- `python -m stock_analyzer.cli sign-command --action SET_POSITION --payload "{\"symbol\":\"600000\",\"strategy\":\"manual\",\"target_position\":0.2,\"entry_price\":9.88,\"quantity\":800,\"fee\":2.5,\"account\":\"acc-ui\",\"trade_time\":\"2026-03-01T10:11:12\",\"note\":\"manual buy\"}"`
- `python -m stock_analyzer.cli sign-command --action SET_POSITION --payload "{\"symbol\":\"600000\",\"strategy\":\"manual\",\"target_position\":0.2,\"recommendation_id\":\"REC-4A2B6C8D9E0F1234\"}"`
- `python -m stock_analyzer.cli sign-command --action SET_RECOMMENDATION_STATUS --payload "{\"symbol\":\"600000\",\"status\":\"watching\",\"strategy\":\"manual\",\"note\":\"wait for better setup\"}"`
- `python -m stock_analyzer.cli broker-snapshot --positions "[{\"symbol\":\"600000\",\"target_position\":0.2}]"`
- `python -m stock_analyzer.cli reconcile-run`
- `python -m stock_analyzer.cli reconcile-latest`
- `python -m stock_analyzer.cli reconcile-weekly --days 7`
- `python -m stock_analyzer.cli dashboard-portfolio --days 7 --trade-limit 120`
- `python -m stock_analyzer.cli runtime-sla --recent-runs 50`
- `python -m stock_analyzer.cli runtime-history-archive-status --limit 20`
- `python -m stock_analyzer.cli runtime-history-archive-run --force`
- `python -m stock_analyzer.cli audit-events --limit 100 --event-type notification`
- `python -m stock_analyzer.cli audit-trace --trace-id cmd-123`
- `python -m stock_analyzer.cli dashboard-quick-command --action PAUSE_NEW_BUY --payload "{}"`
- `python -m stock_analyzer.cli dashboard-quick-command --action CLOSE_ALL_POSITIONS --payload "{}"`
- `python -m stock_analyzer.cli dashboard-quick-reconcile --positions "[{\"symbol\":\"600000\",\"target_position\":0.2}]"`
- `python -m stock_analyzer.cli acceptance-week4-run --sla-recent-runs 100`
- `python -m stock_analyzer.cli acceptance-week4-run --sla-recent-runs 100 --export-enabled true --notify-enabled true`
- `python -m stock_analyzer.cli acceptance-week4-latest`
- `python -m stock_analyzer.cli acceptance-week4-history --limit 20`
- `python -m stock_analyzer.cli week5-scan-run --symbols "600000,000001" --notify-enabled false`
- `python -m stock_analyzer.cli week5-scan-latest`
- `python -m stock_analyzer.cli week5-scan-history --limit 20`
- `python -m stock_analyzer.cli week6-run --symbols "600000,000001" --notify-enabled false`
- `python -m stock_analyzer.cli week6-latest`
- `python -m stock_analyzer.cli week6-history --limit 20`
- `python -m stock_analyzer.cli week6-global-set --us-index-change-pct 0.8 --a50-change-pct 0.2`
- `python -m stock_analyzer.cli week6-global-get`
- `python -m stock_analyzer.cli week6-global-history --limit 50`
- `python -m stock_analyzer.cli week6-regulatory-set --entries "[{\"symbol\":\"600000\",\"tag\":\"inquiry\",\"note\":\"exchange notice\"}]"`
- `python -m stock_analyzer.cli week6-regulatory-get`
- `python -m stock_analyzer.cli week7-kill-record --month 2026-01 --strategy trend --strategy-return -0.03 --benchmark-return 0.01`
- `python -m stock_analyzer.cli week7-kill-history --strategy trend --limit 60`
- `python -m stock_analyzer.cli week7-kill-status --strategy trend`
- `python -m stock_analyzer.cli week7-kill-reset --strategy trend --resume-new-buy true`
- `python -m stock_analyzer.cli week7-cloud-ping --source cloud_monitor`
- `python -m stock_analyzer.cli week7-cloud-status`
- `python -m stock_analyzer.cli week7-cloud-check --now "2026-03-01T08:30:00"`
- `python -m stock_analyzer.cli week7-factor-record --month 2026-01 --strategy trend --psr 0.58 --ic-mean 0.01`
- `python -m stock_analyzer.cli week7-factor-status --strategy trend`
- `python -m stock_analyzer.cli week7-factor-history --strategy trend --limit 60`
- `python -m stock_analyzer.cli week7-factor-reset --strategy trend`
- `python -m stock_analyzer.cli week7-sim-broker-run --days 7 --export-enabled true --notify-enabled true`
- `python -m stock_analyzer.cli week7-sim-broker-latest`
- `python -m stock_analyzer.cli week7-sim-broker-history --limit 20`

## TDX Offline Package

Build local offline bars package from TongDaXin files:

```bash
python scripts/build_tdx_offline_package.py \
  --vipdoc-root "D:\\通达信\\vipdoc" \
  --output-root "data/tdx_offline_package" \
  --include-bj
```

Use offline package in runtime config:

```yaml
data_source:
  primary: tdx_offline
  local_data_root: data/tdx_offline_package
```

Run a manual refresh from the current TongDaXin source:

```bash
python -m stock_analyzer.cli tdx-sync-run --force false
```

When `SA__TDX_SYNC__VIPDOC_ROOT` is configured, the scheduler also:

- runs a daily offline-package refresh at `SA__TDX_SYNC__RUN_TIME` (default `18:20`)
- runs the full-market warehouse incremental sync at `SA__MARKET_WAREHOUSE__RUN_TIME` (default `21:45`)
- refreshes the package again before off-hours evolution if the source has newer data
- clears cached daily bars so self-learning uses the rebuilt package immediately

## Docker

```bash
docker compose build api
docker compose -f docker-compose.yml -f docker-compose.firstscan.yml up -d redis api
docker compose -f docker-compose.yml -f docker-compose.firstscan.yml logs -f api
```

- First boot should use `docker-compose.firstscan.yml`: it switches notifications to
  `console`, disables acceptance auto-run/auto-notify, keeps TDX sync auto-run off,
  and moves `scheduler` behind an explicit `scheduler` profile so the initial deploy
  does not immediately start background jobs.
- The default `docker-compose.yml` no longer mounts TongDaXin `vipdoc`; runtime stays
  on `market_warehouse` only. Use `docker-compose.firstscan.yml` only for occasional
  one-off TDX seed / base-database builds after setting `TDX_VIPDOC_HOST_ROOT`.
- Trigger a controlled first scan after the API is healthy:

```powershell
$body = @{ symbols = @('600000','000001'); notify_enabled = $false; sync_watchlist = $false; sync_reason = 'docker-firstscan' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri 'http://localhost:8001/week5/scan/run' -ContentType 'application/json' -Body $body
Invoke-RestMethod -Method Post -Uri 'http://localhost:8001/week6/run' -ContentType 'application/json' -Body (@{ symbols = @('600000','000001'); notify_enabled = $false } | ConvertTo-Json)
```

- The default `docker-compose.yml` bind-mounts `./artifacts:/app/artifacts` and
  `./suggestions:/app/suggestions`, so acceptance reports, evolution compliance DB,
  runtime state (`/app/artifacts/runtime/runtime_state.json`) and strategy suggestions
  all survive container restarts.
- The current container runtime defaults to `SA__DATA_SOURCE__PRIMARY=market_warehouse`
  and reads warehouse assets from `/app/artifacts/warehouse/package` plus
  `/app/artifacts/warehouse/market.duckdb`.
- The current default compose file does not mount `${TDX_VIPDOC_HOST_ROOT}` into the
  container. If you need in-container TDX refresh, add an explicit host bind before
  enabling scheduler-side TDX sync.
- The default container deployment now installs `cpulimit` and marks itself as
  containerized, so evolution preflight and Week4 acceptance no longer misreport missing
  Docker assets inside the image runtime.
- Container startup now seeds `/app/artifacts/model_v1.json` from the image when the mounted
  runtime artifact directory is empty, so first boot does not get stuck on a missing bootstrap
  model.
- Verify bootstrap readiness after deployment with `GET /train/bootstrap/status`; when a seed
  model is present, startup self-heals the bootstrap state and clears the runtime gate.
- Only enable `scheduler` after you are ready for automatic background jobs. With the
  safe override still applied, start it explicitly via
  `docker compose -f docker-compose.yml -f docker-compose.firstscan.yml --profile scheduler up -d scheduler`.
- For a routine local runtime bring-up, prefer `scripts/start_runtime_stack_localvol.ps1`.
  It now layers `docker-compose.notifications.local.yml` by default, forces
  local notifications to `console`, disables Feishu / WeCom interactions, waits
  for `GET /health`, supports `-SkipScheduler` for a safer first boot, and on
  Windows will try to start `com.docker.service` automatically when run from an
  elevated PowerShell session.
- Only pass `-EnableLiveNotifications` to `scripts/start_runtime_stack_localvol.ps1`
  when you intentionally want the local stack to use real push channels.
