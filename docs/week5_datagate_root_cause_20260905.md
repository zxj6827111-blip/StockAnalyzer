# Week5 生产数据链诊断：night_scan data_gate 长期 blocked 根因（2026-09-05）

> 现象：night_scan 记录级 `data_gate.status=blocked`（`data_quality_blocked:0.000`、`financial_trust_level: missing`）自 **8/26** 起持续；但 scan 照常产出 buy（9/3 产出 002003/603565）。
> 本诊断定位到完整因果链（三层），证据均为 NAS 实库实测。

## 1. 因果链

### 第一层：gate 的数据源——week6 prewarm 覆盖率 = 0

- `data_quality_blocked:0.000` 由 `_build_data_gate`（service.py:7351）产生：读取 week6 prewarm 报告的 `overall_coverage_ratio`，低于 watch 阈值即 blocked。
- prewarm 对 watchlist 逐票取**最新一根日线**，检查 8 个字段（NAS default.yaml `week6.data_quality_fields`）：`financial_data_complete / roe / debt_ratio`（财务）+ `holder_count / block_trade_net / financing_balance / northbound_net / dragon_tiger_flag`（背景）。

### 第二层：数据根因——7/31 起 daily_bars 全部是"裸行"

market.duckdb daily_bars 字段填充时间线（实库）：

| 日期 | financial_data_complete | roe | background_data_complete | financial_source |
|---|---|---|---|---|
| ≤2026-07-30 | 5,168/5,169 | 5,164 | 5,169 | `tdxgp_heuristic` 等 |
| 2026-07-31 ~ 09-04 | **0** | **0** | **0** | **NULL** |

- 断点精确落在 **7/31**：与"7/31 由同步脚本补齐"的运维记录吻合——sync 链路（`scripts/sync_market_duckdb.py` `_sync_daily`）以 **ZIP 价格 ⋈ vendor delta** 拼 payload，全 schema 列但财务/背景列填 NULL，且按 symbol+date DELETE+INSERT 覆盖写（连旧富集行都会被抹掉）。8/30 起该脚本成为日线唯一每日写入方（cron 21:30/22:30），7/31-9/4 全部为裸行。
- 旧富集链的两条来源均已停摆：`tdxgp_heuristic` 来自 TDX 离线包（`tdxgp.zip`，**NAS 上已不存在**）；背景字段（`tushare_pro`）由旧 updater 的背景步骤打标，该链路在日线改走 delta/sync 后未再运行。

### 第三层：gate 语义缺口——snapshot 模式不执行 blocked 中止

- 引擎的 fail-closed 中止（`week5_selection_engine.py:666`：`gate_status=="blocked"` 且自动调度 → 返回空报告）**只存在于非 snapshot 预过滤分支**。
- 生产 night_scan 走 `snapshot_funnel`（snapshot_light 预过滤）主路径，**不经过该检查**——gate=blocked 仅作为字段记录在报告里，不拦任何东西。
- 结果：每晚 scan 在财务/背景因子残缺的横截面上打分选股（候选 `background_completion_score=0.0`、`completion_component≈0.25`、reasons 含 `financial_data_partial`）。

## 2. 影响评估

1. **生产选股质量**：背景/财务两类因子缺失 → 候选打分的完成度分量系统性偏低，排序退化为纯价格量/趋势因子。
2. **回测与训练**：Phase 1 评估的特征来自快照库（同样残缺）；财务/背景因子从未进入近期样本——修复后特征分布会变化，Phase 2 的 PIT universe 快照生成必须与本次修复同源，否则训练/评估再错配。
3. **fail-closed 承诺**：gate 语义缺口意味着"数据质量门禁"在生产主路径上是空的，与方案"数据零容忍"原则不符。

## 3. 修复方案（待拍板）

| 方案 | 内容 | 工作量 | 风险 |
|---|---|---|---|
| A. 完整修复（推荐） | ① 背景字段：tushare 按交易日批量接口回填 7/31-9/4（block_trade/margin_detail/moneyflow_hsgt/top_list 均按日 bulk，~25 日×5 接口，限频可行；holder_count 按票较重，单列策略）；② 财务字段：PIT 安全源重建（中报季！禁止 forward-fill，须带 ann_date 语义——akshare 业绩报表按报告期 bulk 或 tushare snapshot 重建）；③ `sync_market_duckdb.py` 日更后追加富集钩子，根除复发；④ gate 语义补丁（snapshot 模式同样执行 blocked 中止），在 A 完成后启用 | 1-2 天 | 中：财务 PIT 语义要做对；背景字段按日 bulk 需处理限频 |
| B. 仅修 gate 语义 | snapshot 模式补 blocked 中止，night_scan 在数据修复前每晚 fail-closed 空报告 | 半小时 | 生产每晚无信号（诚实但空转）；不解决数据残缺 |
| C. 并入 Phase 2 数据工程 | 不单独修复，PIT universe 快照生成时一并建财务/背景 PIT 层；gate 缺口先留观 | 0（并入） | 生产继续残缺运转数周；Phase 2 时间线本就 2 个周末 |

**建议**：A（分两步：先背景字段+日更钩子让 gate 恢复大部分覆盖，财务 PIT 源谨慎做）+ B 的 gate 补丁随 A 一起上；C 仅在不想动生产链时选择。

## 4. 附注

- 9/3-9/4 产生的 buy 信号是在残缺数据下给出的，复核其依据时应打折。
- 9/5 21:45 night_scan（修复完成前）gate 仍 blocked（advisory），行为与 9/4 一致。

## 5. 修复执行记录（2026-09-05 深夜完成，commit ca69979 部署）

第四层根因（执行中追加发现）：gate 的 prewarm 走 **runtime provider**（`SA__DATA_SOURCE__PRIMARY=vendor_zip_overlay`），overlay 帧（ZIP 价格 ⋈ delta 元数据）结构上不含财务/背景列——即使 market.duckdb 修好，provider 路径覆盖率仍为 0。

实际修复（全部已部署验证）：

| 层 | 修复 | 验证 |
|---|---|---|
| 数据回填 | `backfill_financial_snapshots.py` 全市场 PIT 重跑（数据落在 delta 库，已合并进 market.duckdb：56→163,188 行/5,818 只）+ `backfill_background_fields.py` 按 tushare bulk 回填 26 个交易日背景字段 + `enrich_daily_financial_pit` 批量 PIT 物化（143,990 行/2 秒） | daily_bars 7/31-9/4：fin 99.4%、bg 100% |
| sync 钩子 | `sync_market_duckdb.py` 日更 upsert 后自动执行 PIT 财务物化 + 背景 bulk（防复发） | 代码+lint（周一 21:30 首个自然运行） |
| gate 语义 | snapshot_funnel 主路径补上 blocked 中止（原检查只在非 snapshot 分支）| 新增集成测试；snapshot 正常+data_quality blocked → fail-closed 空报告 |
| provider 层 | `VendorZipOverlayProvider.fetch_daily_bars` 从 market.duckdb join 26 列财务/背景（按 delta 同级 warehouse/ 目录约定惰性解析，缺失即降级） | 新增集成测试；prewarm 覆盖率 **0.0 → 0.8889**（8/9 字段，仅可选 holder_count 待回填）≥ pass 0.88 → **gate=ok** |

运维事故记录：23:24 的首次重建在回滚备份编排阶段失败（`rollback: backup container missing` → FATAL），三个 runtime 容器被移除后未重建，系统短暂下线；重跑 deploy（镜像已构建完成）后全部恢复并健康。教训：deploy 脚本 rollback 编排在「容器 rename 后 compose 再编排」之间存在窗口，重建前应确认镜像构建已缓存完成。

周日（9/6）21:00 财务 cron 已换用 universe 文件方案 + 回填后自动物化；周日 21:45 night_scan 预期 gate=ok/watch_only、候选财务/背景分量恢复。
