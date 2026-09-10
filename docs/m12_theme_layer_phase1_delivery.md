# M12 宏观事件主题层（Macro Theme Layer）Phase 1 交付文档

日期：2026-09-09
分支：main 工作区（未提交）
状态：Phase 1（MVP）实现完成并验证

## 1. 目标回顾

在"个股负面风险规避"新闻线之外新增**正向主题线**：宏观事件（地缘政治/气候/
政策/供应链）→ 商品/产业链传导 → 板块 → 个股，以「评分加分 + 候选池注入」
双通道接入选股漏斗。Phase 1 范围 = MVP：taxonomy + 抓取 + 规则抽取 + 板块
成分 + 价格确认 + ledger + theme_state + API 预览 + 调度注册，
`theme_mode=shadow`（只记账不改结果，评分/注入全 dry-run）。

## 2. 交付清单

### 新增文件（11 个）

| 文件 | 职责 |
|---|---|
| `src/stock_analyzer/theme/__init__.py` | 包入口 |
| `src/stock_analyzer/theme/taxonomy.py` | 知识库 YAML 加载/校验（pydantic extra=forbid + 引用一致性检查） |
| `config/theme_taxonomy.yaml` | 种子知识库：5 族（geo_oil / enso_agri / heat_power / policy_infra / supply_chip）+ 冻结确认阈值 |
| `src/stock_analyzer/theme/macro_news_adapter.py` | akshare 财联社电报抓取（TTL 缓存 + ak_module 注入 + DataSourceError） |
| `src/stock_analyzer/theme/extractor.py` | 规则抽取：标题命中 1.0 / 正文多关键词 0.7 / 单关键词 0.4；theme_heat 对数压缩 |
| `src/stock_analyzer/theme/board_resolver.py` | akshare 概念/行业板块成分（按日文件缓存 + stale 降级沿用上日） |
| `src/stock_analyzer/theme/price_confirmation.py` | 期货价格确认（新浪 futures_zh_daily_sina + 商品→合约注册表；1d≥1.5% 或 3d≥3%） |
| `src/stock_analyzer/theme/ledger.py` | `m12_theme_ledger.duckdb`：theme 维度 dedup/TTL 归档/hit_rate_1d/3d/5d（改造自 m7） |
| `src/stock_analyzer/theme/scorer.py` | ThemeBoostProvider（读 theme_state，72h 过期 fail-closed）+ Neutral + build_theme_pool |
| `src/stock_analyzer/runtime/services/theme_service.py` | 编排：抓取→抽取→确认→账本→theme_state.json（原子写）+ shadow 升级门槛 |
| `src/stock_analyzer/api/theme.py` | GET /theme/state、/theme/events、/theme/shadow/readiness、POST /theme/sync（带认证门） |

另：`scripts/theme_phase1_smoke.py`（验收脚本）、`tests/test_theme_layer.py`
（30 个）、`tests/test_theme_service.py`（7 个）。

### 修改点（6 处，全部小切口）

1. **config.py**：`MacroThemeConfig`（mode/taxonomy_path/confirmation/pinned_max/
   boost 参数/全部落盘路径）挂根配置 `theme:`；`ScoreConfig.weights` 与
   `StrategyScoreConfig.weights` 经 field_validator 兜底注入 `theme_boost: 0.0`
   （YAML 未配置时也保证键存在）；`SchedulerConfig.theme_daily_sync_time="16:45"`。
2. **default.yaml**：镜像 `theme:` 块 + 三张权重表显式 `theme_boost: 0.0` +
   scheduler 键。
3. **pipeline.py**：`ThemeBoostProviderProtocol` + `__init__` 可注入
   `theme_boost_provider`（默认 Neutral，零行为变化）；components 条件加
   `theme_boost`（照 news 先例，available 才加键）；as_of 模式强制切
   `NeutralThemeBoostProvider`（try/finally 恢复，照 pipeline.py:381 先例）。
4. **service.py**：挂 `RuntimeThemeService`；两个 AnalyzerPipeline（主/实时）
   传入 boost provider；注册 `theme_daily_sync` 调度任务（交易日过滤）；
   暴露 `run_theme_daily_sync/theme_state/theme_events/theme_shadow_readiness/
   theme_pinned_symbols` 委托方法。
5. **week5_automation_service.py**：night_scan 注入通道——boost 模式把激活主题
   成分股传入 `pinned_symbols`（引擎内经 `_resolve_pinned_symbols_after_freshness`
   过滤）；shadow 模式 `would_pin` 清单只入报告（`theme_injection` 字段）。
6. **week5_service.py**：扫描报告旁路标注 `theme` 摘要（mode/dry_run/激活主题/
   pinned_pool/boost 分布），失败不阻断。

## 3. 零污染主线的三层保障

1. **权重层**：三张权重表 `theme_boost` 全部 0.0（field_validator 结构性兜底），
   ScoreEngine 权重表∩components 归一化后 0 权重 = 分数逐字节不变
   （`test_score_engine_theme_boost_weight_zero_neutralizes` 验证）。
2. **provider 层**：shadow 模式 theme_state 的 boost 表为空 → `available()`
   恒 False → components 不加键（同 news unavailable 语义）。
3. **回测层**：`run_once(as_of=...)` 强制切 Neutral provider，即使权重被误配
   > 0 也不会被当前时点主题状态污染（双保险）。

18-fold 主链硬门（IC +0.066）不受影响的机制性依据：shadow 期所有落盘产物
（theme_state/ledger/归档）都是旁路文件，主链评分输入不消费它们。

## 4. Phase 1 验收结果

（完整验证矩阵见第 8 节，含调度阻塞事件与 enabled 总门的设计修正。）

- **验收脚本** `python scripts/theme_phase1_smoke.py`：14/14 PASS
  （真实 StockAnalyzerService 装配链路 + fake akshare，不打真实网络）：
  - sync status=ok、mode=shadow、抓取 3 条 → 抽取 2 条 geo_oil 事件；
  - SC 原油近 1 日 +2.55% 超 1.5% 阈值 → geo_oil 价格确认激活；
  - theme_state.json 合法（dry_run=true、boost 表空、pinned_pool=5 只 dry-run 清单）；
  - m12_theme_ledger.duckdb 写入 2 事件；theme_news_latest.jsonl +
    theme_news_daily/2026-09-07.jsonl 双写落盘；
  - /theme/events 预览 2 条；shadow readiness 判定 not ready（门槛数据未积累）。
- **单元/编排测试**：`tests/test_theme_layer.py` + `tests/test_theme_service.py`
  = 37 个全绿。
- **安全**：`tests/test_security.py` 全绿（POST /theme/sync 已挂统一认证门；
  首轮漏挂被动态发现测试逮住后修复）。

## 5. 使用说明

### 开启总开关（NAS 部署后第一步）
```
SA__THEME__ENABLED=true   # .env 或容器环境变量（默认 false，不开启则全链路 skipped）
```
`theme.enabled` 控制：调度任务是否注册 + 手动 sync 是否执行（双保险）。

### 调度（开启后自动）
`theme_daily_sync` 每交易日 16:45 触发（错开 16:30 daily_news_sync）。
交易日过滤双层：调度层 date_predicate + run 内部 `is_a_share_trading_day`。

### 手动触发
```
POST /api/theme/sync?force_refresh=false   # 带 API token
# 或服务层：service.run_theme_daily_sync()
```

### 预览
```
GET /api/theme/state               # 当前激活主题/boost 表/dry-run 清单
GET /api/theme/events?limit=100     # 账本事件 + hit_rate 有效性
GET /api/theme/shadow/readiness    # Phase 2 升级门槛进度
```

### 落盘产物（均在 artifacts/evolution/ 下）
- `inputs/theme_news_latest.jsonl`（滚动去重）+ `inputs/theme_news_daily/YYYY-MM-DD.jsonl`（按日幂等，90 天保留）
- `m12_theme_ledger.duckdb` + `m12_theme_ledger_archive/`（TTL 14 天）
- `theme_state.json`（原子写：tmp + os.replace）
- `theme_board_cache/YYYY-MM-DD/<板块>.json`（按日，失败降级 stale）

## 6. Phase 2（shadow 观察，4-8 周）操作要点

1. NAS 部署后自动每日跑；`GET /theme/shadow/readiness` 看门槛进度。
2. **升级门槛**（`theme_shadow_readiness()` 自动判定）：≥10 交易日、≥30 个
   价格确认激活的主题事件、hit_rate_3d ≥ 55%、人工一致率 ≥ 80%。
3. **人工复核回路**（自建轻量）：向 `artifacts/evolution/theme_review.jsonl`
   追加标注行 `{"theme_id": ..., "title": ..., "agree": true/false,
   "reviewed_at": ...}`——按 title 抽样核对抽取是否正确（该标题确实属于
   该主题族且传导方向正确）。一致率 = agree/总数，readiness 自动统计。
4. 板块成分缓存按日落盘，接口失败自动降级沿用上日（stale 标记）。

## 7. Phase 3（boost 接入）开关清单

> ⚠️ 必须等 shadow 门槛全过 + 用户拍板后才执行

1. `SA__THEME__MODE=boost`（或 default.yaml `theme.mode: boost`）；
2. 权重起步 0.05：`score.weights.theme_boost: 0.05`（同步 trend/monster 三张表）
   ——0.05 × 满分量 1.0 × 100 = +5 分封顶，与方案一致；
3. `pinned_max_per_day=10` 已内置（build_theme_pool 截断）；
4. 验收硬门：18-fold walk-forward 不回退（as_of 回测已中性化，需单独跑
   boost 期的 live A/B 对照）、lr1/lr2 零回归、主题组 vs 对照组收益差为正。

## 8. 验证结果（终版）

| 验证项 | 结果 |
|---|---|
| theme 单元/编排测试 | tests/test_theme_layer.py（30）+ tests/test_theme_service.py（7）= **37 全绿** |
| Phase 1 验收 smoke（`scripts/theme_phase1_smoke.py`） | **14/14 PASS**（真实 service 装配链路 + fake akshare） |
| as_of/pipeline/config 回归子集 | **48 全绿**（含 `test_as_of_none_matches_production`——pipeline 改动零行为变化的直接证据） |
| 安全套件 tests/test_security.py | 全绿（POST /theme/sync 挂统一认证门） |
| 调度套件 tests/test_service_scheduler.py | **46 全绿，30 秒**（修复后） |
| 全量 pytest | 仅 2 个调度测试失败 → 根因修复后全过（见 8.1）；其余全部通过 |
| ruff | M12 触碰文件 0 报错；全仓报错数与 main 基线一致（26=26，均在未触碰文件） |
| mypy | 归一化对比（消除行号漂移）相对 main 基线**零新增**，总数 375→370 |

### 8.1 调度测试阻塞事件与修复（重要设计修正）

首轮全量跑时 `test_service_scheduler` 两个 market_warehouse 测试各阻塞 529 秒
（断言 `elapsed < 1.0` 失败）。根因：**我最初把 theme_daily_sync 的调度注册条件
写成"只查时间字符串"**，默认就注册——测试里 `run_due_jobs(18:21)` 触发 16:45
的 theme job，真实走 akshare 网络抓取（无 fake 注入路径）。

这暴露了一个真实设计缺陷（不只是测试问题）：生产部署后调度器会每天 16:45
无门禁打真实网络。修复照 `m7_live_news_enabled` 成熟先例：

1. `MacroThemeConfig.enabled: bool = False`（总开关，默认关）；
2. 调度注册条件加 `theme.enabled`；`run_theme_daily_sync` 内部同样短路
   （`reason=theme_disabled`）——双保险；
3. NAS Phase 2 观察期显式开启：`SA__THEME__ENABLED=true`。

副作用：全量测试时间恢复正常（scheduler 套件 98 分钟 → 30 秒），且测试/离线
环境彻底不触网。这是 M12 方案"照抄成熟框架"原则的又一次兑现——首轮实现
漏掉了 m7 的 enabled 门是我的疏漏。

## 9. 风险与遗留

1. **akshare 宏观接口无历史**（方案已知硬约束）：shadow 期 4-8 周不可压缩，
   无法历史回测；接口可用性（stock_info_global_cls / futures_zh_daily_sina /
   stock_board_concept_cons_em）**本地未打真实网络验证**——全部测试与 smoke
   用 fake akshare。NAS 开启 `SA__THEME__ENABLED=true` 后第一天的真实验证点：
   POST /api/theme/sync 确认真实接口返回非空。
2. **商品→合约注册表是手工维护**（COMMODITY_CONTRACT_REGISTRY，11 个）：
   未注册商品记 unresolved（不激活、不报错）；知识库新增商品须同步注册表。
3. **板块接口限频**：仅价格确认激活的主题才拉成分（按需拉取）；确认主题
   一次拉 4-5 个板块，若 akshare 限频可能需要节流（Phase 2 实测后决定）。
4. **判定阈值已冻结**：confirmation 阈值在 shadow 数据收集前锁定，迭代
   只能改知识库映射（keywords/boards），不能降阈值（Phase 1.5 多重比较教训）。
5. **boost_max_lift/half_life 参数当前未参与计算**（Phase 1 简化为
   heat×strength 封顶 1.0 + state 72h 过期），Phase 3 接入时若需要时间
   衰减再启用——避免在 shadow 期引入未验证的复杂度。
6. **人工复核回路无 UI**：JSONL 手工标注（现有人工一致率无生产路径的
   最小可用替代），Phase 2 可按需加最小脚本/端点。
7. **未提交**：全部改动在工作区（用户偏好先评审后提交）。

## 9. 与方案的偏差记录

- 新增文件 11 个（方案预估 10）：多出 `api/theme.py` 单独成文件（方案允许
  "api/news.py 或新 api/theme.py"，选后者）+ scripts/theme_phase1_smoke.py。
- 测试文件 2 个（方案列 5 个）：按仓库实际模式合并为 test_theme_layer.py
  （五模块全覆盖）+ test_theme_service.py（编排级），比 5 个碎文件更贴近
  仓库现状（M7 也是 ledger/news 等按模块分）。
- 种子知识库 5 族（方案 ≤6）：geo_oil/enso_agri/heat_power 按草案原文 +
  policy_infra/supply_chip 补足"政策族/供应链族预留扩展位"，未满 6 留迭代空间。
- 板块名与方案草案略有出入（如"糖"→"农牧饲渔"）：akshare 东财概念板块
  实际名称为准，shadow 期按 unresolved/stale 数据迭代。
