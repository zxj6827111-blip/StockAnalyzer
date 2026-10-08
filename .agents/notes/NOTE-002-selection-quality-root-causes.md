# NOTE-002 选股质量根因清单（已证实缺陷 / 假设 / 禁止的推断）

Status: Draft

As-of: 2026-10-08 @ HEAD `ce19b76`（新增 D15：流通市值被 provider 兜底常量静默填充，
浮盈市值硬门因此整天失效；读侧带版本解释规则与防御见 ADR-004）

回答的问题是改进计划 §2 的那句"整条链路在哪里损失选股质量"。本文只列**可核实的事实**
与**明确标注为假设的猜测**，不给因果结论——因果需要 §4 的对照实验
（逐层移除预测性规则）跑完才有资格写，而现在那批实验被分钟行情阻塞卡着。

## 1. 已证实缺陷（代码/数据层面可直接核实）

| # | 事实 | 证据位置 | 它损失了什么 |
| --- | --- | --- | --- |
| D1 | 尾盘确认层**不存在**：`entry_mode="tail_confirm"` / `entry_window=["14:30","14:50"]` 全仓库零消费者 | `config.py:352-354`、`default.yaml:246-249`；`src/` `scripts/` `frontend/` 无引用 | 配置声明与真实动作脱节：所谓"尾盘确认"从未生效，盘中走的是统一雷达节奏（`runtime/service.py:296-302`） |
| D2 | 交易规则有**三份不一致副本**（线上/标签/历史验证），TP-SL 与持有期口径互斥 | `labels/soup.py:10-14`(5/5/5d)、`config.py:1203`(8/5/10d)、`config.py:1379-1382`、`backtest/holding_curve.py:37-38`、`alpha_v2/research/outcomes.py:66-68` | 训练目标 ≠ 生产动作；任何"胜率"数字跨路径不可比 |
| D3 | 标签是**开盘入场**口径（`pnl_price_basis="next_tradable_open"`），`soup.py:161` 在 `exclude_untradable=False` 时静默回退到 T 日收盘价 | `labels/soup.py:137-161` | 用另一个策略的结果评价尾盘策略；不可成交还照计盈亏 |
| D4 | `max_hold_days` 声明 10、消费方 fallback 5 | `default.yaml:255` vs `runtime/service.py:14348`、`:16252` | 同一信号在不同路径对应不同平仓日 |
| D5 | **1 万元参考金额此前不存在**，最近的是 100 000 | `alpha_v2/research/outcomes.py:75` | 差一个数量级会改变最低佣金占比 → 改变"扣费后是否盈利"的标签 |
| D6 | 按日期冻结的成本表**只对印花税分段**，佣金/最低佣金/过户费/滑点没有历史档 | `config.py:821-823`（本次已扩展，见 ADR-003 §5） | 历史净收益其实按当前成本补算，无法与当时口径区分 |
| D7 | 分钟行情落库**只有日级聚合列、没有 bar 时刻列** | `market_warehouse.py:274-309`（12 个聚合列） | 14:30-14:50 逐 5 分钟确认与"确认后下一根成交"**不可重建** → 尾盘验证与净盈利标签训练 blocked。2026-10-08 修正根因定位：时刻在**源**里是有的（`normalize_vendor_minute_frame` 以 `datetime` 为索引），是 `summarize_minute_bars` 落库前折成一天一行；采集通路见 `scripts/sync_tail_minute_bars.py` + ADR-003 §6 |
| D8 | `scripts/sync_index_daily.py` **没有调度入口**，`index_daily` 两个库停在 2026-08-14 | 该脚本 `:4-8` 自述 `market_warehouse_sync` never ran | 相对强弱特征在近期窗口上是滞后/缺失的 |
| D9 | 指数缺失可被静默变成"空帧"，进而让 rs 族失去意义 | `feature/snapshot.py:1949-1968`（异常返回空帧）、`engineer.py:371` 只在**全 NaN** 时给 NaN | 部分过期的指数会让横截面 pct_change 错位，但仍被当有效特征 |
| D10 | 分层截断配置**互相矛盾**：夜间 quality 300、非夜间 100，`week5.night_*` 键不在 YAML 里 | `config.py:501-503` vs `default.yaml:398` | 同一层名在不同 profile 下代表不同规模，跨日比较失去意义 |
| D11 | Light100 层的行级拒绝**只留 `skipped_count`**，不记原因 | `runtime/service.py:6877-6903`、`:7100` | 无法回答"前置筛选是否过早淘汰了适合短期上涨的股票" |
| D12 | 最终推荐没有独立留档，只有一个候选快照 | `runtime/service.py:6566` → `artifacts/runtime/universe_quality_snapshot.json` | "候选快照代表最终推荐"这个前提不成立（尾盘后的判定完全没落库） |
| D13 | `soup_strategy.max_holdings` YAML 值是 1，而契约/新路径要求 3 | `default.yaml:257` | 声明与意图不一致，`audit_strategy_contract_conflicts()` 现在会报出来 |
| D14 | §2 的九层漏斗留档仍不完整。夜扫半段（Quality300/Light100/Deep50）自本提交起**有生产者**（`research/night_scan_funnel_trace.py`，夜扫落定后写 `funnel_trace_<date>_night.json`），但它记的是成员+落差+数据时间，**逐只截断原因仍缺**（全部挂 `night_truncation_reason_not_recorded`）；`universe` 与 `hard_eligibility` 的**生产者已就位**（`night_scan_funnel_trace.build_universe_stage_traces()`，从 `AsofUniverseSnapshot` 的符号级事实构造、原因逐只来自 `resolve_asof_universe`），但**尚未接线**：夜扫报告里的 `universe_snapshot` 是 `to_payload()`，只有计数与 ≤50 样本，没有 `eligible_symbols` 清单 | `research/selection_funnel_view.py` 的 `coverage.layers_from_trace` / `layers_without_reasons` / `layers_unrecorded`；`scripts/audit_selection_funnel.py` 退出码 3 | 前两层（全市场→硬性资格）没有留档，"前置筛选是否过早淘汰了适合短期上涨的股票"仍只能靠印象回答；视图机器判为"凭现有证据答不了"（缺记录不折算成零淘汰，占位原因也不算有原因）。补齐需要夜扫把每只被挡下的原因落成结构化记录；前两层的具体障碍是 `data/asof_universe.to_payload()` 只带**计数**与 ≤50 的 `excluded_reasons_sample`，**不带 eligible 符号清单**，而 `StageTrace` 的 `advanced == len(advanced_symbols)` 恒等式不允许用计数冒充成员——要落这两层必须先决定让夜扫报告携带符号级名单（改生产报告体积与形状），属需确认的设计决策，不是可顺手补的小改 |

| D15 | 流通市值列被数据供应商的**兜底常量** 12,000,000,000.0 静默填进"取不到 `circ_mv`"的那些行：`tushare_provider.py:1677-1684` 的 `fillna(_DEFAULT_FLOAT_MARKET_CAP)`，而它依赖的 `daily_basic` 调用外面 `except Exception: basic = pd.DataFrame()`（`:708-719`）**把失败吞掉**。三个 provider 用同一个字面值（`tushare:23` / `akshare:20` / `efinance:18`），所以这个数就是"没测过"的指纹。库内实测：2026-04 99.7%、05 99.98%、**06 全月只剩这一个取值**、03/07 各约五成；2022-05 起每月 15~46 行、2025-09~2026-02 每月 2,511~5,608 行 | 质量报告 §3g.1（值对上代码）；检测：`trend_data_readiness.column_concentration_<col>`（`MAX_MODAL_VALUE_SHARE=0.50`）与重放侧 `degenerate_gate_inputs()`；读侧规则：`UNPROVEN_FLOAT_MARKET_CAP` / `FLOAT_CAP_INTERPRETATION_VERSION` / HARD 名 `unproven_float_market_cap`，见 ADR-004 | **不是数值不准，是硬门失效**：阈值按同一列取分位 ⇒ 列成常数时阈值=众数 ⇒ `value < threshold` 恒假 ⇒ 这条门那天对全市场一个都不淘汰，留档却读起来像"没有一只票市值不达标"。它不是 NaN/0，所以所有防填零检查都不报警。打上读侧解释规则后重测：污染窗口 310,142 个 symbol-day 里 **216,862（69.9%）"市值从未测过"**、60 天里 40 天整日无从判定、晋级只剩 212；干净窗口（2026-08）只有 1 行占位、门正常淘汰 1,242。真值已于 2026-10-08 补采进研究库（129 天 / 708,047 行，独立表 `float_market_cap_ref`，质量报告 §3j）：同一套规则换上真值后污染窗口的晋级从 212 变成 208,128，市值门按 10 分位每天淘汰 2.7% 的输入——这才是它本来在做的事。仓库那一列**仍未就地改写**（写侧改成 NULL 是跨 26 个文件的数据语义变更，需另开 ADR，ADR-004 §6.1）；另测出 1~3 月有 9%~18% 的非占位行与 `circ_mv` 差出 1% 以上，说明 `float_market_cap` 不是单一定义的序列 |

## 2. 已实测、但还不足以定性为根因

- **现行 `p_meta` 排序在时间外是反向的。** 2026-07..09 observed 队列日截面
  Spearman IC = −0.0058（60 个有效交易日，IC>0 仅 53.3%）；月度 5d 胜率
  7 月 52.0% → 8 月 44.9% → 9 月 42.2%。复现：`scripts/audit_observed_signal_returns.py`。
- **gold 总体 walk-forward：`p_meta` Top5 的 5d 净盈利 34.3%，低于候选池本身 48.8%；
  换权重也救不回来**（in-sample 63.3% → 时间外 55.0%，10d 翻负）。
  → 支持"损失不在排序层，而在样本总体与标签定义"，但**不构成**"改标签就能提净盈利率"的证明。
- **observed 与 replayed 两个总体的前向结果差一个量级**（5d 47.5% vs 62.8%），
  且 `replayed_recompute` 的 `model_outputs_json` 全空（distinct p_meta = 0）。
  → 任何胜率统计都必须先 `where feature_capture_mode='observed_snapshot'`。
- **项目自己的锁定 OOS 上限**：绝对命中率 43–45%、TopK 净收益为负
  （`docs/alpha_v2/M4H_Historical_Locked_OOS_Report.md:206,591`）。
  → 这是新概率的**现实参照**：0.60 阈值只是选股规则，不是已证明的命中率。

## 3. 仍是假设（需要对照实验，不得当作结论使用）

- H1 硬资格检查（ROE/负债率/流动性/市值）过早淘汰了适合 5 日上涨的股票。
  检验：把该层从 `predictive` 改判或放宽，跑 `compare_traces()` 同交易日对照。
- H2 综合分 / 等级 / 交叉复核反复消费同一批信息（动量+相对强弱），造成错误排序而非独立确认。
  检验：特征组消融 + 分层留档（`funnel_trace` 已支持 `kind=predictive` 标记）。
- H3 推荐偏向已大幅上涨、波动过高或成交困难的股票（过热暴露）。
  检验：留档里的最终推荐特征快照分布（ADR-003 的 `FinalRecommendationRow`）。
- H4 数据缺失/旧缓存/特征滞后/模型降级各自影响多少只股票。
  检验：`trend_data_readiness` + 每层 `model_identity` / `data_as_of` 记录。
- H5 最终推荐变差主要来自候选池、预测、排序还是交易规则。
  检验：分层对照（只换一层），需要 D7 的分钟数据先补齐。

## 4. 明确禁止的推断

- **不得把 bronze 样本占比直接当作当前生产模型的根因。** 样本层级是选择偏差的
  症状之一，当前 champion 的训练总体与生产总体差异尚未量化到能定因果。
- **不得用开盘回测的结果给尾盘策略背书**（D7 是硬阻塞，`trend_data_readiness`
  的 `tail_window_minute_bars=blocked`、退出码 5 就是这条禁断的代码化）。
- **不得把综合分、`p_meta` 或任何 rank_quantile 分数解释成"净盈利概率"**
  （语义登记表 `models/output_semantics.py` 会拒绝未登记 basis）。
- **不得把 `replayed_recompute` 的漂亮数字当成系统信号质量。**

## 5. 这份清单什么时候可以升级为结论

三个前置条件全满足后重写本文并升 Accepted：

1. D7/D8 修复：带时刻的分钟行情与 `index_daily` 常驻增量都进调度并回补；
2. 三层留档在真实夜扫/尾盘链路上跑满 ≥20 个交易日（§1 的 D11/D12 关闭）；
3. §3 的每一条假设都有 `compare_traces()` 的同交易日对照结果，
   且 observed 与 replayed 分开报告。

## 6. Implementation Locations

- `src/stock_analyzer/contracts/trend_strategy.py` —— 契约与 `audit_strategy_contract_conflicts()`
- `src/stock_analyzer/research/trend_data_readiness.py`、`scripts/audit_trend_data_readiness.py`
- `src/stock_analyzer/research/funnel_trace.py` —— 分层留档 / 最终推荐留档 / 对照
- `src/stock_analyzer/labels/tail_net_profit.py` —— 净盈利标签（observed 与 replayed 分开）
- `scripts/audit_observed_signal_returns.py` —— §2 的 observed 队列复现入口
- `docs/alpha_v2/M4H_Historical_Locked_OOS_Report.md` —— 锁定 OOS 上限参照
