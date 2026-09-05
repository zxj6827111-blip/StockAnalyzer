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
- 今晚（9/5）21:45 night_scan 将在新容器（dab3813）上首跑，预期 gate 仍 blocked（数据未修复），行为与 9/4 一致。
