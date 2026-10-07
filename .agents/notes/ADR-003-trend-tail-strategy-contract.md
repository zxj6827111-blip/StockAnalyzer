# ADR-003 trend 尾盘策略契约（线上 / 标签 / 回测共用）

Status: Draft

As-of: 2026-10-08 @ HEAD `16d6b96`（本 ADR 与 `contracts/trend_strategy.py`、
`labels/tail_net_profit.py`、`research/trend_data_readiness.py`、
`research/funnel_trace.py` 同批提交）

## 1. Status 为什么是 Draft

契约本身已定稿并有专属测试；**消费方还没接完**：

```text
已接：  契约模块自身（入场/成交/出场/成本/准入排序）、净盈利标签构造、
        数据就绪审计、漏斗留档。
已接（影子）：盘中的 run_live_runtime 现在额外跑一条尾盘影子链路并逐日留档
        （不改旧输出）—— 这是 §4 未来影子验证的证据来源。
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
成交"无法从历史数据重建 → `research/trend_data_readiness.py` 的
`tail_window_minute_bars` 判 **blocked**，退出码 5。

结论：**尾盘策略的历史验证与 `p_net_profit_5d_tail` 的真实模型训练目前不可执行**。
必须先采集带时刻的分钟行情（并让 `scripts/sync_index_daily.py` 真正进调度 —— 它没有
调度入口，`index_daily` 两个库停在 2026-08-14），再谈 §4 的选股质量验收。

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
- `src/stock_analyzer/research/funnel_trace.py` —— 分层留档与最终推荐留档
- `tests/test_trend_strategy_contract.py`（53）、`tests/test_tail_net_profit_label.py`（18）、
  `tests/test_trend_data_readiness.py`（13）、`tests/test_funnel_trace.py`（16）

## 8. 尚未接线的调用方（升级 Accepted 前必须改完）

| 位置 | 现在的口径 | 状态 |
| --- | --- | --- |
| `runtime/services/week5_automation_service.py:1241 run_live_runtime` | 全时段雷达节奏，无尾盘窗口判定 | **已接（影子）**：`_trend_tail_shadow_report()` 调 `TrendTailShadowService`，内部用 `evaluate_tail_entry` + `rank_final_recommendations`；只新增 `report["trend_tail_shadow"]`，**不改写 `actionable_signals`**（AST 守卫测试钉住）。旧路径继续服务真实推送 |
| `runtime/service.py:7267 _final_signal_selector` | `funnel_score` + `final_signal_min_threshold=70`、cap 5 | **未接**。它是旧路径的唯一出口，接管即等于生产切换，必须等证据达标后单独发布（§5） |
| `labels/soup.py` + `LabelsConfig` | 开盘入场、8%/5%/10d | **不改写**（按计划要求旧标签保留原语义）；新路径已有 `labels/tail_net_profit.py` |
| `backtest/holding_curve.py` / `AsofBacktestConfig` | horizon 10、开盘口径 | **未接**，且当前无法接：`simulate_tail_exit` 需要带时刻的分钟 bar（§6 阻塞）。在此之前它是"另一个策略"的验证器，不能给尾盘背书 |

影子链路当前必然输出 0 只，原因是真实的：既没有 `p_net_profit_5d_tail` 的生产者
（`no_tail_probability_available`），也没有带时刻的分钟行情
（`minute_bars_unavailable`）。这两个原因都由留档写下来，不是静默空结果。
