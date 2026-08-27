# 实施计划：历史日期回溯选股 + 持有期走势分析

> 本文档自包含，执行者无需前置对话上下文。所有标注「实测」的结论均已在 NAS 生产环境验证。
> 创建时间：2026-08-27　｜　计划状态：待实施

## 一、目标

在 `http://192.168.10.26:18001/ui` 新增「历史回测」栏目：手动选择历史日期（或日期区间），
用当前代码与模型回溯计算该日会选出哪些股票，并展示这些股票之后 10 个交易日
（或截止今日）的每日走势、最优卖点与收益统计。

附带修复两条数据链路故障（新闻抓取、intraday 同步）。

## 二、环境与访问方式

### NAS（飞牛 FN）

- 8 核 / 15.5G 内存，可用约 10.7G，swap 已用 938M
- 项目目录 `/vol1/docker/StockAnalyzer`，API 端口 `18001`
- 历史数据 `/vol1/1000/股票历史数据`（78G），已挂载进容器为 `/data/vendor_history`
- 容器：`stock-analyzer-api`、`stock-analyzer-scheduler-heavy`、
  `stock-analyzer-scheduler-critical`、`stock-analyzer-redis`
- compose 组合（**注意四个文件叠加**）：

```
docker-compose.yml + docker-compose.runtime.yml
+ docker-compose.advisory.yml + docker-compose.vendor-overlay.yml
```

### 远程执行工具（已就绪）

- `scripts/nas_exec.py`：paramiko 封装，凭证读 `~/.kiro/nas_credentials.json`
  （项目外，不进 git）
- 用法：

```bash
python scripts/nas_exec.py "docker ps"
python scripts/nas_exec.py --script probe.sh --timeout 900 --out result.txt
```

- 三个已知坑：
  1. 多行脚本用 `--script`（`--file` 会按行拆散多行 Python）
  2. 输出务必用 `--out`（PowerShell 重定向会破坏 UTF-8 编码）
  3. NAS 宿主**无 pandas**，需 pandas 的诊断要 `docker exec` 进容器跑

## 三、关键实测事实

### 3.1 数据可用性

| 数据 | 覆盖范围 | 状态 |
|---|---|---|
| `全A日K/*.zip`（2000~2026，**真实数据源**） | 到 **2026-08-26** | 正常，每日更新 |
| `复权因子_前/后复权.zip` | 到 2026-08-27 | 正常 |
| `market_delta.duckdb`（delta 增量） | — | 正常 |
| `vendor_intraday_summary.duckdb` | **只到 2026-07-17** | **故障，待修** |
| `m7_news_latest.jsonl` | 仅 87 条，4-22~4-24 | **故障，待修** |
| `artifacts/warehouse/market.duckdb`（597MB） | 到 2026-07-30 | **遗留死数据** |

- 抽样 400 只，398 只有 2026-07-31 日线数据。600000/000001/300750 均已逐行核对
- 日线 CSV 列：`code, datetime, open, high, low, close, pre_close, change, pct_chg,
  volume, amount, turnover, turnover_free, volume_ratio, pe, pe_ttm, pb, ps, ps_ttm,
  dv_yield, dv_ttm, total_share, float_share, free_share, total_mv, circ_mv`
- 日期格式 `YYYY-MM-DD`（带横线）
- zip 内含 `__MACOSX/` 垃圾条目，需按 `.csv` 后缀且排除 `__MACOSX` 过滤
  （真实条目 5570 个）

### 3.2 as-of 地基已验证可用

生效配置：`SA__DATA_SOURCE__PRIMARY=vendor_zip_overlay`，provider 实际类型 `CachedProvider`。

已生效的 `end_date` 截断：

- `vendor_zip_overlay.fetch_daily_bars` — `vendor_zip_overlay.py:298-336`，
  baseline 截断在 `:320-326`
- `MarketWarehouse.fetch_daily_bars` — `market_warehouse.py:2616`，
  SQL `date <= ?` 在 `:2622-2624`
- `CachedProvider` — 缓存 key **含 end_date**（`cached_provider.py:45-48`），
  不会命中脏缓存
- `ResilientProvider` `:35-60`、`HybridRuntimeProvider` base 分支 `:49-56` 均透传

容器内实测结果：

```
end_date=2026-07-31 → 最后一根 bar = 2026-07-31（无泄露）
7-31 之后可取 18 根 bar

以 7-31 收盘 9.51 买入 600000：
  T+1  2026-08-03  close=9.63  +1.26%（期间最高 +1.47%）
  T+2  2026-08-04  close=9.40  -1.16%
  T+3  2026-08-05  close=9.26  -2.63%
  T+4  2026-08-06  close=9.29  -2.31%
  T+5  2026-08-07  close=9.21  -3.15%
  T+6  2026-08-10  close=9.29  -2.31%
  T+7  2026-08-11  close=9.21  -3.15%
  T+8  2026-08-12  close=9.17  -3.58%
  T+9  2026-08-13  close=9.18  -3.47%
  T+10 2026-08-14  close=9.10  -4.31%
```

#### 两个必须规避的泄露点

1. `HybridRuntimeProvider` 盘中 live overlay 分支**无视 end_date**
   （`hybrid_runtime_provider.py:58-72`），会把「今天」实时 K 线并入结果。
   回测链路禁止使用 realtime provider（`build_realtime_runtime_provider`）
2. `fetch_universe_quality_metrics` **没有 end_date 参数**
   （`market_warehouse.py:2089-2145`、`vendor_zip_overlay.py:336`），
   语义是「每 symbol 最近 N 行」。as-of 路径不得用它做粗筛，否则拿到最新数据即泄露

### 3.3 数据流架构

```
baseline = 全A日K/<year>.zip   ← 历史底座，只读，不解压
delta    = market_delta.duckdb  ← 近端增量
合并：同一 symbol+date，delta 覆盖 zip（_merge_overlay_frames，keep="last"）
```

`market.duckdb` 已无任何代码路径读写：

- `MARKET_WAREHOUSE__ENABLED=false` 只关同步任务
  （`service.py:17126`、`market_sync_service.py:2911`）
- provider 构造链从不读该配置
- 两个 db_path 均被 compose 覆盖指向 delta 库

**陷阱**：`.env` 内容已不反映实际运行配置。`.env:5-6` 仍写 `market.duckdb`、
`PRIMARY=market_warehouse`，全被 `docker-compose.vendor-overlay.yml:21-22,55-56` 覆盖。
改配置务必确认写在生效层。

### 3.4 intraday 依赖（影响回测可行性）

分钟数据被聚合成日频摘要，产出约 40 个 `i1m_*`/`i5m_*` 特征
（`feature/engineer.py:461-481`）：VWAP 偏离与稳定性、上下午分化与反转强度、
早盘/尾盘量能占比、已实现波动、收盘位置、价格路径效率、日内回撤比等。

当前配置 `INTRADAY_RUNTIME_MODE=duckdb_required` + `ZIP_FALLBACK_ENABLED=false`
是 **fail-closed**：

- 缺数据即 `raise RequiredIntradayDataError`（`vendor_zip_overlay.py:568-575`）
- **Week5 全市场扫描**：`fresh_ratio < 0.95` → `return blocked`，**整轮阻断**
  （`week5_service.py:1911,1932`）
- **run_once 单标的**：降级为 `score=0, grade=C, action=hold`，不阻断其他票
  （`pipeline.py:1158-1187`）

`_prepare_intraday_summary`（`engineer.py:485-540`）在 frame 为空时返回全 NaN 标准列
而非丢行，因此 `zip_legacy`/`duckdb_optional` 模式下纯日线可跑通。

注意：由日线派生的 `vwap`(`engineer.py:75-76`)、`intraday_ret`(`:92`)、
`vwap_gap5`(`:171`) **不依赖分钟数据**，纯日线回测仍保留。

### 3.5 新闻链路诊断（根因已定位）

**根因：`m7_live_news_enabled=False`，从未开启。代码本身正常。**

走系统内部链路实测（`_collect_live_m7_news_records`，2 只票）：

```
R: 6 条
D: {'provider':'akshare_em','symbol_count':2,'fetched_symbols':2,
    'raw_items':6,'records':6,'ai_review':{...},'errors':[]}
样例：600000 / 2026-08-27T16:41:00 / 证券时报网 / sentiment=0.0
```

#### akshare 兼容 bug（已有 workaround，勿删）

akshare 1.18.84 的 `stock_news_em` 在 pandas pyarrow string backend 下必崩：

```
akshare/news/news_stock.py:116
temp_df["新闻内容"].str.replace(r"\u3000", "", regex=True)
→ pyarrow.lib.ArrowInvalid: Invalid regular expression: invalid escape sequence: \u
```

RE2 引擎不支持 `\u` 转义。系统用 `pd.option_context("mode.string_storage", "python")`
规避（`news_service.py` 抓取处），有效。**必须保留并加注释。**

#### 链路其余缺陷

- 无独立调度任务，仅在 evolution 流程内触发
  （`evolution_core_service.py:1248`，条件 `m7_live_news_enabled`）
- 存储是「最新 2000 条滚动覆盖」单文件，每次 `"w"` 模式整体重写
  （`news_service.py:979-1001`），**无按日归档，历史不可回溯**
- `ArtifactNewsSignalProvider.score` 硬编码 `_utc_now` 做新鲜度衰减，
  工厂不传 `now_func`（`news_provider_factory.py:12-16`），
  `max_age_days=3` 会把历史新闻全判 stale 丢弃
- 情绪算法是**纯规则**（`_estimate_news_sentiment_heuristic`，
  `news_service.py:1044-1092`，中英文关键词计数），确定性可重现；
  LLM 复核默认关闭（`m7_ai_review_enabled=False`）。
  **只要有原始新闻就能精确重算**
- 数据质量隐患：实测抓到的 content 是「资金流出榜」这类榜单文本，信息价值低
- 相关配置实测值：`m7_live_news_provider=akshare_em`、`max_symbols=24`、
  `per_symbol_limit=5`、`max_age_hours=24.0`、`artifact_max_records=2000`、
  `m7_market_proxy_fallback_enabled=False`、`news_risk_mode=shadow`

**结论**：7 月及之前的历史新闻**无法追回**（数据不存在），回测这些日期必须中性化。
但从修复之日起积累的新闻，未来可真实回溯。

中性化零改动可用：`NeutralNewsSignalProvider.score` 恒返回 0.50
（`pipeline.py:140-152`），且 pipeline 未注入 news_provider 时**缺省即为它**
（`pipeline.py:183-185`）。

### 3.6 前端结构

**React 19 + TypeScript + Vite 独立工程**，不是单 HTML、不是 Jinja。

- 挂载：`main.py:221-236`（SPA fallback），资源 `main.py:210-214`，
  目录解析 `_resolve_frontend_dist_dir()` `main.py:85-95`
  （优先 `frontend_dist`，其次 `frontend/dist`）
- `vite.config.ts:6` 设 `base: '/ui/'`
- 导航：左侧侧边栏 + react-router。栏目定义 `App.tsx:41-47`（core）/
  `:48-52`（research），路由表 `App.tsx:189-198`
- 现有页面：`Dashboard.tsx`(23KB)、`RuntimeStage.tsx`(31KB)、`Portfolio.tsx`(5.9KB)、
  `Recommendations.tsx`(10KB)、`SystemOps.tsx`(5.9KB)、`LearningOverview.tsx`(41KB)、
  `ObservationPoolPage.tsx`(37KB)、`News.tsx`(8.4KB)
- 请求封装 `lib/api.ts`（`apiGet`/`apiPost`），轮询 `lib/useAutoRefresh.ts`（默认 15s）
- **无图表库**（无 ECharts/Chart.js/Recharts），但 `ObservationPoolPage.tsx:555-561`
  已有内联 SVG 折线：`<svg viewBox="0 0 100 100"><path d={trendPath}/></svg>`。
  **画走势曲线直接复用，不要引新依赖**
- `api/dashboard.py:30-43` 的 `/dashboard`、`/dashboard/recommendations`、
  `/dashboard/stage` 只是 **307 重定向到 `/ui`**，是遗留兼容层，新栏目不需要动
- 容器内 `/app/frontend_dist` 是**镜像构建产物**（构建于 8-11）；
  NAS 宿主**没有** `frontend_dist` 目录

### 3.7 现有回测能力与可复用件

- `backtest/walk_forward.py`：单标的、fold 级 AUC/Brier/equity 的**模型验证器**，
  回答「模型准不准」，**不是本需求要的东西**
- `backtest/matcher.py` 的 `ExecutionMatcher`：**可直接复用**。含 `can_buy`、
  `simulate_exit`（涨跌停不可成交、跳空止损、延迟卖出）、
  `dynamic_slippage_ratio`、`estimate_cost`
- `ops/background_tasks.py`：202 异步任务注册表，
  状态 `queued/running/succeeded/failed`，配 `GET /tasks/{task_id}` 轮询
- `api/` 下**没有任何 K 线/bars/ohlc 端点**，需新增

### 3.8 历史选股清单落盘现状

`artifacts/runtime/history/` 有 11 天归档，
路径 `.latest.week5_scan.signal_pool.candidates`（7-30 实测 15 条，8-26 实测 15 条）。

已有日期：`7-28, 7-29, 7-30, 8-03, 8-14, 8-18, 8-20, 8-24, 8-25, 8-26, 8-27`
（**无 7-31**）

week5 report 完整结构（候选清单位置）：

```
report.prefilter.shortlisted          (100 条)
report.signal_pool.candidates         (50 条)
report.first_board.candidates         (5 条)
report.watchlist_sync.symbols         (36 条)
```

**注意**：因 8-26 部署新版代码，8-26 之前的归档数据**已作废**，不作为本功能数据源。
功能一律走 as-of 重跑。

## 四、已确认的口径决策

| 项 | 决策 |
|---|---|
| 未来函数 | **接受 + UI 显著标注**。模型训练于 **2026-08-16**（`train/bootstrap/status` 的 `last_bootstrap_at`），回测早于此日期的结果偏乐观 |
| 前端部署 | **`frontend/dist` 挂载卷**（一劳永逸，后续改前端免重建镜像） |
| 多日回测 | **支持**。但并行策略见下 |
| intraday 排查 | **要做**，且前置 |
| 新闻历史 | 7 月及之前无法追回 → 回测中性化；同时修复链路并加按日归档，使未来可回溯 |

### 并行策略（重要，勿按日期并行）

瓶颈是**内存不是 CPU**。宿主可用 10.7G，常驻已占 3.2G，
且容器当前**无内存限制**（`Memory=0`）——失控任务会连带打死 sing-box 等其他容器。
前人已撞过此墙：全市场特征工程吃 12G 打到 swap 后性能塌陷。

**正确设计**：多个 as_of 日期共用同一批 zip 数据。

```
按标的维度并行（≤ 8 worker，nproc=8）
  每只票一次取出全历史
    对所有目标 as_of 日期做切片复用
```

zip 只解压一次，内存占用与日期数量几乎无关，速度更快，同时用满 CPU。

**禁止**按日期开 N 个并发各自加载数据。

配套约束：粗筛 Top N 限流，避免全市场 5500 只逐票深度特征工程；
`VENDOR_ZIP_MEMORY_CACHE_SYMBOLS=32` 是现有缓存上限，注意并发下的实际内存放大。

---

## 五、任务分解

### Task 1：修复新闻抓取链路

**目标**：新闻每日自动抓取落盘，并按日归档使未来可回溯。

**实现**

1. 开启 `m7_live_news_enabled=true`。**必须写在生效层**——`.env` 会被
   `docker-compose.vendor-overlay.yml` 覆盖，先确认覆盖关系再改
2. 在 `_register_default_jobs`（`runtime/service.py`）注册独立每日新闻同步 job，
   调用 `run_m7_live_news_sync`，时间选盘后并与现有 job 错开。
   不再依赖 evolution 流程触发
3. `_persist_m7_news_records`（`news_service.py:992-1001`）改造：
   保留现有滚动文件写入，**追加**按日分区归档
   （建议 `artifacts/evolution/inputs/news_daily/YYYY-MM-DD.jsonl`），
   保留期可配置。同日重复写需幂等
4. 在 `pd.option_context("mode.string_storage", "python")` 处**加中文注释**，
   说明它规避的是 akshare + pyarrow RE2 对 `\u3000` 的 `ArrowInvalid` 崩溃，
   防止后人误删
5. 评估 `m7_live_news_max_symbols=24` 是否够用（当前关注池 36 只）

**测试**

- 单测：按日归档文件正确生成；同日重复写幂等；滚动文件仍受 2000 条上限约束
- 单测：模拟 akshare 抛 `ArrowInvalid`，确认链路降级不崩且记录
  `live_news_fetch_failed` 审计事件
- 集成（NAS）：手动触发一次，确认生成当日归档且 `errors: []`

**验收**：`GET /news/score?symbol=600000` 返回非 0.5 的真实分；
当日归档文件存在且含真实新闻；调度器 job 列表含新闻同步任务。

---

### Task 2：排查修复 intraday 同步链路

**目标**：查明 `vendor_intraday_summary.duckdb` 为何停在 2026-07-17 并修复。
这影响**当前生产选股质量**（约 40 个特征持续缺失），不只是回测。

**实现**

1. 定位现状：文件 `/vol1/docker/StockAnalyzer/intraday_summary/vendor_intraday_summary.duckdb`
   （314MB，2026-08-21 16:44 构建），另有 `build.log`、`monitor.log`、`.manifest.json`。
   表 `intraday_summary_1m`/`_5m` 均为 `2025-04-28 ~ 2026-07-17`
2. 查同步任务是否注册、是否在跑、是否报错。相关模块：
   `data/intraday_summary.py`、`data/intraday_summary_builder.py`、
   `data/intraday_sync.py`、`ops/intraday_freshness.py`
3. 检查上游源：`/vol1/1000/股票历史数据/沪深分钟数据/`（75G）本身
   **只更新到 2026-07-25**，`京市分钟数据` 到 7-25。
   **上游可能就是断的**——若如此，这是数据供给问题而非代码问题，
   需确认 tushare 下载脚本是否覆盖分钟数据
4. 注意 `/vol1/1000/股票历史数据/intraday_summary` 目录**大小为 0**（空），值得怀疑
5. 按根因修复：若上游断→修下载链路；若同步任务未跑→注册/修复调度；
   若代码 bug→定位修复
6. 确认 `SA__DATA_SOURCE__INTRADAY_SUMMARY_PATH` 指向存在的 duckdb+manifest——
   `duckdb_required` 下缺失会**启动即抛错**（`vendor_zip_overlay.py:258-271`）

**测试**

- 修复后确认 duckdb 覆盖到最近交易日，`fresh_ratio >= 0.95`
- 确认 Week5 扫描不再被 `freshness_gate_blocked` 阻断

**验收**：`GET /week5/scan/latest` 的 `prefilter.intraday_freshness` 显示
fresh_ratio 达标；duckdb 最大日期为最近交易日。

> 若根因是上游数据供给缺失且短期无法补齐，则记录结论并转为：
> 回测走 `duckdb_optional` 降级路径（见 Task 3），生产侧另行决策。

---

### Task 3：pipeline 注入 as_of + 防泄露断言 + 多日期切片

**目标**：能用历史时点数据跑选股，且有机制保证不泄露未来数据。

**实现**

1. `AnalyzerPipeline.run_once()`（`pipeline.py:263`）与 `_run_once_prefetched()`
   增加 `as_of: date | None = None`
2. 贯通到内部两处取数（`pipeline.py:498`、`pipeline.py:1086`），传 `end_date=as_of`
3. **防泄露断言（核心正确性保障，不可省）**：取数返回后校验
   `bars.index.max() <= as_of`，违反立即抛错并记录标的与实际最大日期
4. **禁用泄露路径**：as_of 模式下强制不使用 `HybridRuntimeProvider`
   （`build_realtime_runtime_provider`）；不使用 `fetch_universe_quality_metrics`
   做粗筛（无 end_date 参数），改用带截断的替代方案
5. intraday 处理：as_of 模式下走 `duckdb_optional` 语义，摘要缺失时返回全 NaN
   特征列而非 fail-closed。**不得污染生产的 fail-closed 语义**——
   建议通过独立配置对象/独立进程实现，而非全局改开关
6. news 处理：as_of 模式强制使用 `NeutralNewsSignalProvider`（`pipeline.py:140-152`），
   并在结果中标注 `news_neutralized=true`
7. **多日期切片能力**：实现「每只票一次取全历史 → 对多个 as_of 切片」的取数复用层，
   避免按日期重复解压 zip
8. 标的维度并行 <= 8 worker，并加内存监控保护（超阈值降并发或中止）

**测试**

- 单测：`as_of` 截断生效，最后一根 bar 日期正确
- 单测：**注入含未来数据的假 provider，断言必须抛错**（这条比功能本身更重要）
- 单测：intraday 缺失时降级不阻断，特征列为 NaN 而非丢行
- 单测：多日期切片结果与逐日单独取数结果**逐值一致**
- 回归：`as_of=None` 时行为与现状完全一致（防止改坏生产链路）

**验收**：CLI 跑 `as_of=2026-07-31` 输出候选清单；防泄露断言在注入未来数据时正确拦截；
`as_of=None` 回归测试全绿。

---

### Task 4：持有期走势分析模块

**目标**：给定股票清单与买入日，算出每日走势、最优卖点与统计口径。

**实现**

1. 新增 `src/stock_analyzer/backtest/holding_curve.py`
2. 输入：symbols + entry_date + horizon（默认 10 交易日，支持「截止今日」）
3. 单票输出：T+1..T+N 每日收盘收益率、期间最高/最低、最优退出日及其收益、
   期间最大回撤、是否触发止盈/止损
4. 汇总输出：平均最优持有天数、**各持有天数的平均收益分布**
   （直接回答「第几天卖最赚」）、胜率、盈亏比
5. **复用 `ExecutionMatcher`**（`backtest/matcher.py`）：`simulate_exit` 保证
   涨跌停/停牌不可成交的真实性，`estimate_cost` 计入交易成本。
   避免算出无法成交的理想收益
6. 按**交易日**推进，不是自然日
7. 边界：数据不足 N 天时如实返回可用天数并标注；停牌、新股、退市

**测试**

- 单测：构造已知价格序列，逐项核对收益/最优退出日/回撤/胜率
- 单测：涨跌停不可卖出时的延后成交行为
- 单测：数据不足、停牌、退市等边界不崩
- 对照：用 600000 从 2026-07-31 入场的真实数据核对
  （已知期望值：入场价 9.51，T+1 +1.26%、T+2 -1.16%、T+5 -3.15%、T+10 -4.31%）

**验收**：对 Task 3 产出的清单跑分析，输出逐日收益表与「第 N 天卖最优」结论，
且 600000 用例与上述已知值吻合。

---

### Task 5：后端 API

**目标**：把 as-of 选股与走势分析暴露成前端可用接口。

**实现**

1. `POST /backtest/asof-scan`：入参 `date` 或 `start_date`+`end_date`、`top_n`、
   `horizon`。耗时长，用 `BackgroundTasks` 返回 **202 + task_id**，
   复用 `ops/background_tasks.py` 与 `GET /tasks/{task_id}` 轮询
2. `GET /backtest/asof-scan/latest`、`GET /backtest/asof-scan/history?limit=`
3. `GET /market/daily-bars?symbol=&start=&limit=`：单标的日线序列
   （当前 `api/` 无此类端点）
4. 结果**落盘持久化**（建议 `artifacts/backtest/`），容器重启不丢
5. **响应必须带口径标注字段**，让偏差在数据里可见而非只写文档：

```json
{
  "caveats": {
    "model_trained_at": "2026-08-16",
    "lookahead_bias": true,
    "intraday_degraded": true,
    "intraday_coverage_until": "2026-07-17",
    "news_neutralized": true
  }
}
```

6. 遵循现有约定：POST 经 `_verify_api_auth`，GET 只读公开
7. 新建 `api/backtest.py` 则需在 `main.py` router 注册区 `include_router`

**测试**

- API 测试：202 提交 → 轮询至 succeeded → 结果结构与 caveats 字段正确
- 边界：非交易日、无数据日期、日期晚于数据覆盖范围（>2026-08-26）、
  日期区间跨度过大 → 明确空态/错误，非 500
- 并发：多任务提交不互相污染

**验收**：curl 提交 7-31 回测，轮询完成，返回清单 + 每票走势 + caveats。

---

### Task 6：前端「历史回测」栏目

**目标**：UI 上选日期即可看结果。

**实现**

1. 新增 `frontend/src/pages/HistoricalBacktest.tsx`
   - 原生 `<input type="date">` 选日期与区间（不引新库）
   - 提交后轮询任务状态并展示进度
   - 股票清单表格（复用 `ObservationPoolPage.tsx` 表格样式）
   - 每票走势 SVG 折线
     （**复用 `ObservationPoolPage.tsx:555-561` 的 `<svg><path>` 写法**）
   - **显著位置展示口径标注**：模型训练于 2026-08-16 故结果偏乐观、
     intraday 降级、news 中性化
2. `App.tsx`：`researchNavItems`（`:48-52`）加导航项，
   `<Routes>`（`:189-198`）加路由，顶部 import
3. 用 `lib/api.ts` 的 `apiGet`/`apiPost`
4. `npm install`（首次）→ `npm run build`

**测试**

- `tsc -b` 无类型错误，`npm run build` 成功
- 手工：选日期能出结果；空态/错误态不崩；走势曲线渲染正确；标注可见

**验收**：浏览器打开 `http://192.168.10.26:18001/ui/historical-backtest`，
选 2026-07-31，看到清单、走势曲线与口径标注。

---

### Task 7：部署与资源保护

**目标**：安全上线，不拖垮 NAS，且前端后续迭代免重建镜像。

**实现**

1. **前端 dist 挂载卷**：在 compose 中把宿主 `frontend/dist`（或独立目录）
   挂载到容器 `/app/frontend_dist`。注意 `_resolve_frontend_dist_dir()`
   优先 `frontend_dist` 后 `frontend/dist`（`main.py:85-95`）。
   NAS 宿主当前**没有** `frontend_dist` 目录，需先建立并放入构建产物
2. **给容器加内存限制**（当前全部 `Memory=0`）。按可用 10.7G 与常驻 3.2G 规划，
   为回测预留额度且不挤压其他容器。
   **需重启容器生效，动手前与用户确认时机**
3. 回测并发 <= 8 worker，粗筛 Top N 限流
4. NAS 上端到端验证

**测试**

- 跑一次完整多日回测，`docker stats` 监控内存峰值不触发 swap 增长
- 确认现有每日选股链路未受影响，
  **尤其 intraday 模式切换未污染生产 fail-closed 语义**
- 确认前端改动后只需替换 dist 即生效，无需重建镜像

**验收**：NAS 上完整跑通多日回测；内存平稳；生产链路照常；前端热替换验证通过。

---

### Task 8：附带清理（低优先）

1. `artifacts/warehouse/market.duckdb`（597MB）确认为遗留死数据 →
   **移到备份目录观察，不直接删**
2. `artifacts/training/learning_protocol.corrupt.20260731-010747.duckdb`（1.21GB）与
   `learning_protocol.corrupt.20260815-233515.duckdb`（1.19GB）
   共约 **2.4GB** 损坏备份，可清理
3. **轮换 Tushare token**——`.env` 中明文且已在排查过程中暴露
4. 配置 SSH 密钥登录，删除 `~/.kiro/nas_credentials.json` 中的明文密码
5. 清理 `.env` 中已失效的配置项（`PRIMARY=market_warehouse`、`market.duckdb` 路径等），
   减少后人误判

---

## 六、风险清单

| 风险 | 说明 | 缓解 |
|---|---|---|
| 未来函数 | 模型训练于 8-16，回测更早日期结果偏乐观 | 已决策接受 + UI/API 双重标注 |
| 训练/推理分布不一致 | 模型带 intraday 特征训练，回测时这 40 列为 NaN | 标注；可先用 SHAP 量化 intraday 特征贡献度再评估严重性 |
| 内存耗尽拖垮 NAS | 容器无 limit，宿主仅 10.7G 可用 | Task 7 加 mem_limit；标的维度并行而非日期维度；Top N 限流 |
| 污染生产链路 | as_of 需 intraday 降级，若全局改开关会破坏生产 fail-closed | 用独立配置对象/独立进程，禁止全局改；回归测试 `as_of=None` |
| 静默泄露未来数据 | 两个已知泄露点（Hybrid live overlay、universe_quality_metrics） | 防泄露断言 + 显式禁用这两条路径 + 专门的泄露检测单测 |
| 新闻数据质量 | 抓到的是「资金流出榜」这类榜单文本 | Task 1 中评估；必要时增加内容过滤 |
| intraday 上游本身断供 | 沪深分钟数据只到 7-25，可能不是代码问题 | Task 2 先定性根因再决定修复方向 |

## 七、审核清单（review 阶段逐条核对）

1. **防泄露断言真实有效**：必须存在「注入未来数据 → 断言抛错」的单测，
   且能实际拦截。这是整个功能的正确性根基
2. **`as_of=None` 回归**：生产链路行为零变化，
   特别是 intraday fail-closed 语义未被全局改开关污染
3. **两个已知泄露点被显式禁用**：`HybridRuntimeProvider` live overlay、
   `fetch_universe_quality_metrics`
4. **多日期切片与逐日单独计算结果逐值一致**（证明复用层没算错）
5. **持有期收益计算正确**：600000 从 2026-07-31 入场须复现
   T+1 +1.26% / T+5 -3.15% / T+10 -4.31%（入场价 9.51）
6. **`ExecutionMatcher` 被真实复用**，涨跌停/停牌不可成交被正确处理，
   而非算理想收益
7. **口径标注在 API 响应与 UI 上都存在且准确**
8. **内存限制已配置**，并有实测峰值数据佐证
9. **akshare workaround 保留且有注释**
10. **新闻按日归档幂等**，且不破坏原有滚动文件语义
11. 无新增前端依赖（走势图应复用现有 SVG 模式）
12. 无明文密钥新增进仓库

---

## 八、建议执行顺序

```
Task 1（新闻修复）  ─┐ 独立，可并行先行
Task 2（intraday）  ─┘

Task 3（as_of 注入）→ Task 4（走势分析）→ Task 5（API）
  → Task 6（前端）→ Task 7（部署）

Task 8（清理）：随时，低优先
```

Task 1 与 Task 2 不依赖主线且不影响现有链路，建议先做完并验证，再进入 Task 3。
Task 7 涉及容器重启，需与用户确认时机。

### 不可妥协项

- Task 3 的**防泄露断言**与 **`as_of=None` 回归测试**
- Task 7 加内存限制**需重启容器**，动手前必须与用户确认时间窗口

其余实现细节可灵活取舍。
