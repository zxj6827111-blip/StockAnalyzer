# ADR-003 trend 尾盘策略契约（线上 / 标签 / 回测共用）

Status: Draft

As-of: 2026-10-08 @ HEAD `0c9f043`（本 ADR 与 `contracts/trend_strategy.py`、
`labels/tail_net_profit.py`、`research/trend_data_readiness.py`、
`research/funnel_trace.py`、`research/tail_mature_feedback.py` 同批演进）

## 1. Status 为什么是 Draft

契约本身已定稿并有专属测试；**消费方还没接完**：

```text
已接：  契约模块自身（入场/成交/出场/成本/准入排序）、净盈利标签构造、
        数据就绪审计、漏斗留档。
已接（影子）：盘中的 run_live_runtime 现在额外跑一条尾盘影子链路并逐日留档
        （不改旧输出）—— 这是 §4 未来影子验证的证据来源。
已接（展示与反馈）：GET /week5/tail-shadow/{latest,history} + 前端"尾盘确认"页
        （候选 / 最终推荐 / 成交状态分列）；research/tail_mature_feedback 按模型
        版本 / 市场状态 / 拒绝原因聚合，输出上限是 challenger 建议。
未接：  runtime 旧路径的最终推荐出口（仍是 week5 综合分 + final_signal_cap=5，
        按计划要等证据达标后单独切换）、历史验证入口（asof_backtest 仍是
        default_horizon_days=10 + 开盘口径）、以及任何真实 p_net_profit_5d_tail
        模型的训练 —— 后两者被 §6 的分钟行情阻塞卡住，不是没做，是做不了。
```

三条消费路径全部改指本契约、且 §4 的"线上与历史判定一致"在真实数据上验过之后，
本 ADR 升 Accepted。

## 2. 决策

**trend 策略的入场时点、成交判定、持仓与出场、成本口径，收敛为一个带版本的契约对象，
由线上推荐、训练标签、历史验证三方共同调用。不再允许任何一侧自己写一份规则。**

契约条款（`trend_tail_v1`，来自 `config/default.yaml: trend_strategy`）：

| 条款 | 取值 |
| --- | --- |
| 确认窗口 | 14:30–14:50，每 5 分钟一个确认点，**不含终点**（终点确认就没有属于本窗口的下一根 bar 可成交） |
| 确认可读数据 | 只读**已完成**的分钟 bar（`bar_end <= 确认时点`） |
| 成交价 | 确认后第 `fill_bar_offset=1` 根已完成 bar 的价（用确认时点那根的 close 自成交是 look-ahead） |
| 参考金额 | 10 000 元/只，整手向下取整，最低佣金照计 |
| 止盈 / 止损 | +8% / −5%；同一根行情双触发时**止损优先**（硬 0，不是软标签） |
| 持有期 | 5 个交易日，**入场日计为第 1 日**；T+1 → 止盈止损监控从第 2 日开盘开始 |
| 退出 | 第 5 日尝试退出；不可卖则顺延，成熟时间 = **实际可成交退出时刻** |
| 不计盈亏 | 未成交 / 数据末尾强平 / 未知交易状态 / 无法核算的公司行动 → 不生成已实现盈亏标签 |
| 价格口径 | `execution_price_basis` 必须是 `raw`；QFQ 只能进特征 |
| 准入 | `p_net_profit_5d_tail >= 0.60`，按概率降序、股票代码升序，最多 3 只，**允许 0 只且不补名额** |

## 3. 为什么不能沿用现状（被否决的方案）

落地前的实际状态，逐条都可直接核实：

1. **尾盘确认层根本不存在。** `soup_strategy.entry_mode="tail_confirm"` 与
   `entry_window=["14:30","14:50"]`（`config.py:352-354`、`default.yaml:246-249`）
   在 `src/` `scripts/` `frontend/` 里**零消费者**。盘中跑的是统一的
   `AUTOMATION_RADAR_PROFILES` 节奏（`runtime/service.py:296-302`），没有任何
   尾盘专属判定。"配置声明了尾盘确认"这件事本身是假的。
2. **同一套交易规则有三份互不一致的副本。** 线上（`strategy/soup.py:63-73`、
   `runtime/service.py:10809/14324/16252`）、标签（`labels/soup.py:10-14` 默认
   5%/5%/5d，`config.py:1203` 又声明 8%/5%/10d）、历史验证
   （`backtest/holding_curve.py:37-38`、`alpha_v2/research/outcomes.py:66-68`、
   `config.py:1379-1382`）。改一处不影响另外两处 → 训练目标与生产动作不是同一件事。
3. **开盘入场口径与尾盘策略不可混用。** `labels.pnl_price_basis="next_tradable_open"`
   评的是"次日开盘买入"，而策略动作是"次日尾盘确认后买入"。用它训练/验收尾盘策略
   等于用另一个策略的胜率做决策。`audit_strategy_contract_conflicts()` 会把这条
   连同其它冲突声明逐条列出来，而不是静默取其一。
4. **持有期口径分歧会直接改写标签。** `max_hold_days` 声明 10、消费方 fallback 5
   （`runtime/service.py:14348`、`:16252`）。同一个"信号"在两条路径上对应不同的
   平仓日，任何胜率数字都失去可比性。
5. **1 万元参考金额此前不存在。** 最接近的是 `DEFAULT_REFERENCE_NOTIONAL=100_000.0`
   （`alpha_v2/research/outcomes.py:75`）。差一个数量级会显著改变最低佣金占比，
   从而改变"扣费后是否盈利"的答案。

被否决的替代做法：

- **只改排序键 / 只改阈值。** 已实测无效：gold 总体 walk-forward 时间外，现行
  `p_meta` 排序 5d 净盈利 34.3%，还不如候选池本身（48.8%）；in-sample 挑权重能到
  63.3% 但时间外掉到 55.0% 且 10d 翻负。**排序层不是杠杆**。
- **把现有综合分或 `p_meta` 直接当成"扣费后净盈利概率"。** 语义不同：前者是
  TP/SL 路径事件概率或横截面分位，后者是含成本的净收益事件。
  `models/output_semantics.py` 的登记表就是为防止这种混用而存在。
- **用开盘价回测顶替尾盘验证。** 明确禁止（§5、`trend_data_readiness` 的
  `tail_window_minute_bars` 判 blocked 就是这个禁断的代码化）。

## 4. 契约的 fail-closed 规则

`TrendStrategyContract.__post_init__` 直接 raise 的情形（不是告警）：

- `strategy != "trend"` —— monster 第一轮保留既有策略，不走本契约，不共用统计；
- `execution_price_basis != "raw"`；
- `same_bar_conflict_policy != "stop_loss_first"`；
- `holding_days < 2`（T+1 下入场日不可卖，1 日持有没有可执行退出）；
- 窗口短于一个检查间隔（没有可成交的确认点）。

`rank_final_recommendations()` 在模型身份不可验证时输出 **0 只**并写
`blocking_reason`（身份缺失 / 训练 commit ≠ 运行 commit / `feature_compute_version<=0`
/ 契约摘要不匹配），与 ADR-001 的身份纪律一致；旧综合分、S/A 等级、分歧试探、
恢复买入都不参与新路径资格。

停牌判定两侧不对称，是刻意的：

- **入场与出场都认 `trade_status` 里的显式停牌码**（`S/halt/停牌/...`，
  `SUSPENDED_TRADE_STATUS`）以及 `suspended/is_suspended/suspend` 标记。只认显式
  布尔标记会漏掉真实数据里唯一会说停牌的字段，把停牌股当成可买可卖。
- **缺状态或 `unknown` 不算停牌**（"不把缺 bar 当停牌"）。入场侧因此可能继续，
  出场侧由 `_trade_status_declared()` 兜住：状态未声明 → `unknown_trade_status`
  → 不确定样本，不生成已实现盈亏标签。

影子链路的交易日语义（`TrendTailShadowService.run`）：

- 日期**只能**从 `timestamp`（线上）或显式 `trade_date`（历史重算）推导；两者都没有
  直接 `TrendContractError`，**不接受 `date.today()` 兜底**。留档盖成"今天"会让漏斗
  每一层的时间语义整体失真，而 §4 要求线上与历史路径对相同输入给出一致判定。
- 数据时间同理：线上是当前时钟，历史模式取最后一根已完成 bar
  （`data_as_of`），逐层写进 `funnel_trace`，不是 `datetime.min` 占位。
- 陈旧度门只在有 `timestamp` 时生效（历史 bar 不存在"实时快照过期"这件事）。
  除这一条之外两条路径共用同一判定，
  `test_live_and_history_chain_paths_agree_on_identical_bars` 比较入选集合、
  漏斗计数、拒绝原因与成交（数量/金额/成交时刻），断言二者一致。

## 5. 数据契约侧的同批变更

- `CostScheduleEntry` 从"只对 `stamp_tax_rate` 分段"扩展为可覆盖
  `commission_rate / min_commission_per_order / transfer_fee_rate / slippage_ratio`，
  由 `resolve_cost_profile()` 按日期取档并返回 `overridden` 集合 —— 留档必须能看出
  这笔收益是按冻结历史成本算的还是按当前静态成本补的。旧 YAML 只声明
  `stamp_tax_rate`，行为与扩展前逐元一致。
- 标签成熟时间走 `label_mature_time_tail_exit_v1`（= 实际可成交退出时刻），
  新 basis `net_profit_5d_tail` 在 `output_semantics` 登记为 `event_probability`。

## 6. 已知阻塞（不是本 ADR 的例外，是它的前置条件）

落库的分钟表 `intraday_summary_1m` / `intraday_summary_5m` 只有 12 个**日级聚合列**
（`minute_count / last30_return / close_position` 等），没有任何 bar 时刻列
（`market_warehouse.py:274-309`）。因此 14:30–14:50 的逐 5 分钟确认与"确认后下一根
成交"无法从**落库数据**重建 → `research/trend_data_readiness.py` 的
`tail_window_minute_bars` 判 **blocked**，退出码 5。

**2026-10-08 修正了对根因的判断**：这不是"源数据没有分钟时刻"。
`data/vendor_zip_overlay.normalize_vendor_minute_frame` 读源 CSV 时就带着
`datetime` 列并以它为索引，是 `data/intraday_summary.summarize_minute_bars`
在落库前把它折成了一行一天。也就是说时刻信息**在源里存在，在管道里被丢掉**。
（`read_tdx_minute_bars` 同样能解出带日期的分钟 bar。）

因此采集侧的通路已经建好，且刻意只写独立研究库、不进生产 provider：

```text
scripts/sync_tail_minute_bars.py   本地跑：vendor ZIP → artifacts/research/
                                   tail_minute_bars.duckdb（表 minute_bars_1min）
research/minute_bar_store.py       按分钟存 bar；price_basis / bar_time_semantics
                                   必须显式声明；bars_for() 直接吐契约要的形状，
                                   非 raw 口径拒绝用于成交模拟
scripts/audit_trend_data_readiness.py --minute-db <研究库>
                                   只有研究库里真的有带时刻的 bar，
                                   tail_window_minute_bars 才允许翻成 ok
```

**仍然没解决的两件事**（所以 §4 的选股质量验收现在依旧不可执行）：

1. 本机没有 vendor 离线包（`--root` 指向外部路径），历史分钟**实际能覆盖多少个交易日**
   还没测过；尾盘验证需要 ≥4 个测试折 + 标签真实成熟，覆盖不够就还是 blocked。
   这一步必须在有源包的本机跑，不放 NAS。
2. `scripts/sync_index_daily.py` **没有调度入口**，`index_daily` 两个库停在
   2026-08-14 → 相对强弱特征滞后（这是另一条链路的缺口，不因分钟库而消失）。

结论不变：**不得用开盘回测顶替尾盘策略验证**。

## 7. Implementation Locations

- `src/stock_analyzer/contracts/trend_strategy.py` —— 契约、确认与成交判定、出场、
  准入排序、配置冲突审计（唯一真相源）
- `src/stock_analyzer/config.py` —— `TrendStrategyConfig`、扩展后的
  `CostScheduleEntry`、Config 根字段 `trend_strategy`
- `config/default.yaml` —— `trend_strategy:` 块（唯一声明处）
- `src/stock_analyzer/data/limit_rule.py` —— `FrozenCostProfile` /
  `resolve_cost_profile()`
- `src/stock_analyzer/execution/engine.py` —— `cost_profile()`、`estimate_cost()`
  消费冻结成本
- `src/stock_analyzer/labels/tail_net_profit.py` —— 净盈利标签构造与分组报告
- `src/stock_analyzer/models/output_semantics.py` —— `net_profit_5d_tail` 语义登记
- `src/stock_analyzer/research/trend_data_readiness.py` + `scripts/audit_trend_data_readiness.py`
- `src/stock_analyzer/research/funnel_trace.py` —— 分层留档与最终推荐留档；
  `write_trace(..., suffix=)` 让"成交与退出"这层落到**另一个文件**，不回头覆盖入场那天的留档
- `src/stock_analyzer/models/tail_net_profit_trainer.py` —— 日期切分 +  embargo、
  LR 基线 / 既有 LightGBM 参数、独立校准段、5pp 分块 bootstrap 判定
- `src/stock_analyzer/feature/trend_candidate_contract.py` —— 硬门/预测规则分类、
  先算后截、四组特征消融与"缺失不填零"
- `src/stock_analyzer/runtime/services/trend_tail_shadow_service.py` —— 影子链路 +
  `page_view()` / `tail_shadow_page()` / `tail_shadow_history()`
- `src/stock_analyzer/research/tail_mature_feedback.py` —— 成熟结果按模型版本 /
  市场状态 / 拒绝原因反馈，输出止于 challenger 建议；`attach_exit_outcomes()` +
  `execution_exit_stage()` 把"最终推荐 ↔ 后来的成熟退出"对上，产出 §2 漏斗最后一层
  `execution_exit`（晋级=退出已实现且可归因；未成交/不确定/未成熟/无标签记录
  一律留在拒绝原因里，净盈利率分母只含已实现样本）
- `scripts/record_tail_exit_funnel.py` —— 从已归档的 shadow report + 标签留档
  落这一层留档；契约摘要与在服契约不一致时直接退出码 5，不用另一套契约的口径写证据
- `src/stock_analyzer/api/week5.py` —— `GET /week5/tail-shadow/latest|history`
  （只有读接口，没有手动 run 入口）
- `frontend/src/pages/TailShadow.tsx` + `frontend/src/App.tsx` —— "尾盘确认"页：
  候选 / 最终推荐 / 成交状态分列，元信息缺失即报错
- `src/stock_analyzer/research/minute_bar_store.py` —— 带时刻的分钟研究库
  （表 `minute_bars_1min/5min`）；`price_basis` / `bar_time_semantics` 强制声明，
  非 raw 拒绝用于成交模拟
- `scripts/sync_tail_minute_bars.py` —— 本地把 vendor 分钟 ZIP 落到研究库
  （`--price-basis` / `--bar-time-semantics` 无默认值，读不到就退出码 5）
- `src/stock_analyzer/research/tail_walk_forward.py` + `scripts/validate_tail_selection_quality.py`
  —— 滚动前推验证的**编排层**：折边界带 embargo、注入 split 由训练器复核、
  排序仍走 `rank_final_recommendations`（验证器不自带一套选股逻辑）；
  样本不足/身份不通过一律 `blocked`，不产命中率数字
- `scripts/audit_trend_data_readiness.py --minute-db` —— 就绪门多看一个来源，
  判定标准不变
- `docs/trend_tail_acceptance_evidence.md` —— §4 验收证据：工程验收逐场景 → 测试名，
  以及"选股质量验收 = blocked"的实测口径（本文件不产命中率数字）
- 测试（2026-10-08 实测条数）：`test_trend_strategy_contract.py`(55)、
  `test_tail_net_profit_label.py`(19)、`test_trend_data_readiness.py`(13)、
  `test_funnel_trace.py`(16)、`test_tail_net_profit_trainer.py`(19)、
  `test_trend_candidate_contract.py`(13)、`test_trend_tail_shadow_runtime.py`(19)、
  `test_trend_tail_page_and_feedback.py`(25)、`test_minute_bar_store.py`(15)、
  `test_tail_walk_forward.py`(18)、`test_tail_exit_funnel.py`(14)

## 8. 尚未接线的调用方（升级 Accepted 前必须改完）

| 位置 | 现在的口径 | 状态 |
| --- | --- | --- |
| `runtime/services/week5_automation_service.py:1241 run_live_runtime` | 全时段雷达节奏，无尾盘窗口判定 | **已接（影子）**：`_trend_tail_shadow_report()` 调 `TrendTailShadowService`，内部用 `evaluate_tail_entry` + `rank_final_recommendations`；只新增 `report["trend_tail_shadow"]`，**不改写 `actionable_signals`**（AST 守卫测试钉住）。旧路径继续服务真实推送 |
| `runtime/service.py:7267 _final_signal_selector` | `funnel_score` + `final_signal_min_threshold=70`、cap 5 | **未接**。它是旧路径的唯一出口，接管即等于生产切换，必须等证据达标后单独发布（§5） |
| `labels/soup.py` + `LabelsConfig` | 开盘入场、8%/5%/10d | **不改写**（按计划要求旧标签保留原语义）；新路径已有 `labels/tail_net_profit.py` |
| `frontend/src/pages/Recommendations.tsx` | 旧生命周期推荐表（综合分口径） | **未改**。新页面 `TailShadow.tsx` 只读影子留档并独立成页，两页并存、不共用排序键；切换前旧页面仍是用户看到的那一份 |
| `backtest/holding_curve.py` / `AsofBacktestConfig` | horizon 10、开盘口径 | **未接**，且当前无法接：`simulate_tail_exit` 需要带时刻的分钟 bar（§6 阻塞）。在此之前它是"另一个策略"的验证器，不能给尾盘背书 |

影子链路当前必然输出 0 只，原因是真实的：既没有 `p_net_profit_5d_tail` 的生产者
（`no_tail_probability_available`），也没有带时刻的分钟行情
（`minute_bars_unavailable`）。这两个原因都由留档写下来，不是静默空结果。

§2 的 9 层漏斗里，trend 这条 trace 现在覆盖 4 层：`night_watch_pool`、
`tail_confirmation`、`final_recommendation`（影子服务写的入场侧留档）与
`execution_exit`（成熟后由 `record_tail_exit_funnel.py` 另写一份）。
前 5 层（`universe` / `hard_eligibility` / `quality_300` / `light_100` / `deep_50`）
仍由夜扫自己的生产漏斗证据链负责（`alpha_v2/validation/production_funnel.py`：
source evidence + counts + 防篡改哈希）。按计划 Alpha V2 与本契约保持独立，
所以这里**不**把它们并成一条 trace；跨链对齐靠同一个 `trade_date` 与
`contract_digest`，代价是"一次查询看完整个漏斗"目前还做不到。
