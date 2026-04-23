# E-Prime 审核方案 v1.3.7（稳态运行再加固版）

- 版本日期：2026-03-04
- 基于版本：`E_Prime_审核方案_v1.3.6.md`
- 本版目的：补齐“线上误触发/漏触发、Universe 漂移、执行微口径分叉、收益语义漂移”四类最后一公里风险。

---

## 0. 本版新增项（相对 v1.3.6）

1. 修正 `data_latency_sec` 为“最慢关键输入延迟”（max-latency），新增 `latency_by_input` 逐输入延迟审计。
2. 新增 `Universe As-Of` 口径：`universe_snapshot_id` + 交易日快照规则，并纳入原子包。
3. 执行微规则补齐：`share_rounding_rule/min_notional_per_order/residual_order_policy`，并纳入 `execution_spec_hash`。
4. 补齐 `price_series_mode` 与 `dividend_treatment` 的收益语义绑定，防止历史对比失真。
5. 新增 `vwap_proxy` 早盘敏感性影子对照（`open_window` vs `day_window`）用于执行偏差归因。
6. 新股/稀疏样本处理固化：`ADV60` 不可得时的分层与映射回退策略写死。
7. 在线 `partial_fit` 样本顺序确定性固化，新增 `online_samples_used_hash`。

---

## 1. 时间一致性与数据口径（硬约束）

## 1.1 时区与交易日历

1. 系统业务时区固定为 `Asia/Shanghai`。
2. 交易日历依赖上交所/深交所交易日历数据，不允许用系统自然日推断交易日。
3. 所有时间字段统一存储 ISO8601（含时区偏移）。

## 1.2 四时间戳语义

1. `event_time`：数据在外部世界发生/发布的时间。
2. `available_time`：数据进入本系统并可被下游读取的时间。
3. `decision_time`：策略做交易决策的时间。
4. `label_mature_time`：标签可确认且允许用于训练的最早时间。

## 1.3 时间不变量（必须满足）

1. `available_time >= event_time`
2. `decision_time > max(feature.available_time)`
3. `label_mature_time >= label_anchor_time + holding_horizon + settlement_lag`
4. 任何违反上述条件的样本不得用于训练/推理/评估。
5. `settlement_lag` 默认值：
   - `settlement_lag = 1`（交易日，A 股默认）。
   - 该参数必须纳入 `execution_spec_hash`，不得使用隐式默认值。

## 1.4 跨频率规则（日频与分钟频）

1. 日频策略默认不得使用“当日收盘后才可得”的字段进行当日盘中决策。
2. 分钟级执行若引用日频特征，需确保该日频特征来源于前一交易日或当前时点已可得快照。
3. `decision_time` 默认绑定订单触发时刻，不绑定 bar 结束时刻。

## 1.5 数据可得性延迟 SLO（v1.3.7 固化）

1. 关键输入：
   - 行情主数据、停牌状态、成分股/行业映射、复权因子。
2. 默认延迟阈值：
   - `max_data_latency_sec = 120`。
3. 延迟违约定义（最慢输入优先）：
   - 对每个关键输入 `i` 计算：`latency_by_input[i] = now - available_time_i`；
   - `data_latency_sec = max_i(latency_by_input[i]) = now - min(available_time_of_required_inputs)`；
   - `latency_worst_input = argmax_i(latency_by_input[i])`；
   - 当 `data_latency_sec > max_data_latency_sec` 记为一次违约。
4. 延迟分级动作（默认）：
   - 轻度延迟：`max_data_latency_sec < data_latency_sec <= 2*max_data_latency_sec`
   - 动作：进入 `latency_watch`，`block_online_update=true`，`raise_u_threshold_bp` 至少上调 `+10bp`。
   - 严重延迟：`data_latency_sec > 2*max_data_latency_sec`
   - 动作：当日进入 `limited_observability`，`block_online_update=true`，`raise_u_threshold_bp` 至少上调 `+30bp`。
   - 持续延迟：`data_latency_breach_ratio_20d > 0.15`
   - 动作：进入 `limited_observability`，并允许 `force_champion_only=true`。
5. 版本化：
   - `max_data_latency_sec/latency_required_inputs/latency_formula_version/latency_violation_policy/latency_watch_policy`
   - 必须纳入 `runtime_config_hash`。

---

## 2. 默认执行与评估规格（Execution/Evaluation Spec，v1.3.7 固化）

## 2.1 交易频率与触发时点

1. v1.3.7 固化为：`日频 T+1 执行`。
2. 触发时点：信号在 `T` 产生，订单在 `T+1` 首个可交易时段执行。

## 2.2 订单与成交口径

1. 默认订单类型：限价参与，超时未成交部分按规则撤单/延后。
2. `next_tradable_vwap` 定义（默认）：以信号日 `T` 之后首个可交易日 `D` 为锚，`vwap_proxy` 取 `D` 的评估窗口成交额/成交量；默认窗口为 `09:35-14:55`。
3. 若 `D` 不可交易（停牌/一字限制/无可成交量），顺延到下一可交易日；最多搜索 `max_search_days=8`，超出则记 `no_fill`。
4. 限价参与撮合代理（默认）：
   - `buy_fill_price = min(limit_price, vwap_proxy * (1 + slippage + impact))`
   - `sell_fill_price = max(limit_price, vwap_proxy * (1 - slippage - impact))`
5. 部分成交默认规则：
   - `fill_ratio = min(1, participation_cap * market_amount / order_amount)`
   - 当 `fill_ratio < min_fill_ratio(默认 0.10)` 时记 `no_fill`，按撤单/延后策略处理。
6. 不可交易场景（停牌、涨跌停、T+1 限制）不得记为已成交收益。
7. 标签与评估同源口径（硬约束）：`label_entry_price` 与 `evaluation_entry_fill` 默认使用同一撮合代理版本 `next_tradable_fill_proxy_v1`，并写入版本字段。
8. `limit_price/slippage/participation_cap/order_amount` 的来源与默认值必须以 2.7 为准，禁止业务代码覆盖。
9. Entry/Exit `no_fill` 差异化规则（v1.3.7 延续）：
   - Entry（建仓）`no_fill`：该次建仓记 `R_net=0`，并记录 `entry_no_fill=true`。
   - Exit（平仓）`no_fill`：不得作废；必须进入“强制延后平仓”流程。
   - 强制延后平仓：最多追踪 `max_exit_carry_days=8` 个交易日，按首次可成交价完成平仓并回算真实 `R_net` 与 `MDD`。
   - 若超过 `max_exit_carry_days` 仍不可成交，使用 `forced_liquidation_proxy_price`（`close_t` 定义见 2.7.7）计入损益，并记录 `forced_exit=true`。
10. 价格序列口径（v1.3.7 固化）：
   - `price_series_mode = raw | qfq | hfq`（默认 `qfq`）。
   - `open/close/vwap_proxy/forced_liquidation_proxy_price/R_net/TailLoss` 必须使用同一 `price_series_mode`。
   - 标签生成与评估回放的 `price_series_mode` 必须完全一致，否则任务失败。
   - `dividend_treatment = implicit_by_qfq | explicit_cashflow`（默认 `implicit_by_qfq`）。
   - 当 `price_series_mode=qfq/hfq` 时默认 `dividend_treatment=implicit_by_qfq`；当 `price_series_mode=raw` 时必须显式声明 `dividend_treatment`，并在收益计算中保持一致。
11. 早盘敏感性影子对照（仅 shadow）：
   - 主口径窗口保持 `day_window=09:35-14:55`；
   - 并行计算 `open_window=09:35-10:30` 的 `vwap_proxy_open`；
   - 当 `|vwap_proxy_open - vwap_proxy_day| > 30bp` 连续 5 日时，写入 `execution_sensitivity_alert=true` 并要求归因备注。
   - `open_window/day_window/sensitivity_threshold_bp/sensitivity_days` 必须版本化并纳入 `execution_spec_hash`。

## 2.3 成本与冲击

1. 成本项：佣金、印花税、过户费、滑点、冲击成本。
2. 冲击成本函数（默认）：`impact = k * participation_rate^eta`。
3. 冲击成本默认参数（v1.3.7 固化）：
   - `impact_k` 按流动性分层：大盘 `0.08`，中盘 `0.12`，小盘 `0.18`。
   - `impact_eta` 默认 `0.60`（全层一致，可版本化覆盖）。
4. 成本压力测试：`x1.5` 与 `x2.0` 必测，且需分流动性分层报告（大盘/中盘/小盘）。
5. 纯选股评估档可配置固定双边成本（例如 `8-12bp`）且将 `impact=0`，但必须记录为独立评估版本，不得与主口径混算。

## 2.4 容量约束

1. 约束口径优先使用成交额参与率（amount participation）。
2. 日频默认：单票成交额参与率不得超过当日成交额阈值（按流动性分层设定）。
3. 若扩展到分钟级，改为分钟成交额参与率约束。

## 2.5 纯选股模式声明

1. 纯选股模式下，本节口径用于“评估与影子盘模拟”，不直接触发实盘下单。
2. 即使不实盘交易，也必须保留统一执行评估口径，以保证收益评估与模型目标一致。

## 2.6 双评估档（v1.3.7 延续）

1. `trading_eval_profile`：
   - 保留全成本、冲击、容量与部分成交规则，用于可成交性下限检查。
2. `stockpick_eval_profile`：
   - 固定双边成本（默认 `10bp`）、`impact=0`、不做成交量参与率硬截断，仅保留流动性过滤。
3. 两档必须同日同时输出，且写入 `eval_profile_id`。
4. 晋升规则默认以 `stockpick_eval_profile` 为主指标，`trading_eval_profile` 作为风控下限指标（阈值见 9.10）。
5. 交易档成交分布门槛（v1.3.7 固化）：
   - `no_fill_ratio <= 0.20`；
   - `partial_fill_ratio <= 0.35`；
   - 或相对基线劣化：`no_fill_ratio_delta <= 0.05`、`partial_fill_ratio_delta <= 0.08`。

## 2.7 执行参数钉死表（v1.3.7 固化）

1. `limit_price`（默认来源）：
   - 参考价 `ref_quote`：`T+1` 首个可交易窗口（默认 `09:35`）最新成交价。
   - 买单：`limit_price_buy = ref_quote * (1 + limit_buffer_bp / 10000)`。
   - 卖单：`limit_price_sell = ref_quote * (1 - limit_buffer_bp / 10000)`。
   - `limit_buffer_bp` 默认按流动性分层：大盘 `10bp`，中盘 `15bp`，小盘 `20bp`。
2. `slippage`（默认）：
   - 按流动性分层常数：大盘 `3bp`，中盘 `6bp`，小盘 `10bp`。
3. `participation_cap`（默认）：
   - 按流动性分层：大盘 `2.0%`，中盘 `1.0%`，小盘 `0.5%`。
4. `order_amount`（默认口径）：
   - 定义为“目标下单金额”，来自组合层 `target_notional`。
   - 计算：`order_amount = abs(target_weight_i - current_weight_i) * portfolio_nav`。
5. `market_amount`（默认口径）：
   - 定义为执行窗口（默认 `09:35-14:55`）的可交易成交额。
6. 参数版本化（硬约束）：
   - `limit_buffer_bp/slippage_tier_table/participation_cap_tier_table/order_amount_mode/ref_quote_rule/max_exit_carry_days/forced_liquidation_discount_bp/impact_k_tier_table/impact_eta/settlement_lag/price_series_mode/dividend_treatment/share_rounding_rule/price_tick_rule/min_notional_per_order/residual_order_policy/open_window/day_window/sensitivity_threshold_bp/sensitivity_days`
   - 以上字段必须纳入 `execution_spec_hash`，不得仅在代码常量中隐式存在。
7. 强制清算代理价（Exit 超期）：
   - `forced_liquidation_proxy_price = close_t * (1 - forced_liquidation_discount_bp/10000)`（卖出）
   - 默认 `forced_liquidation_discount_bp=50`。
   - `close_t` 固定定义为：`exit_signal_date + max_exit_carry_days` 对应交易日的收盘价（若该日停牌/无收盘价，则顺延至下一有收盘价交易日，最多顺延 3 日）。
8. 交易单位与取整规则（A 股默认）：
   - `share_rounding_rule = lot_down_100`（按 100 股整手向下取整，不允许碎股）。
   - `price_tick_rule = exchange_tick`（价格按交易所最小价位单位取整）。
   - `min_notional_per_order = 5000 CNY`（低于该金额的订单默认不发送，记 `trim_reason=min_notional`）。
9. 残单策略（默认）：
   - `residual_order_policy = day_cancel_then_recalc`。
   - 含义：当日窗口结束未成交部分全部撤单；次日按最新 `target_weight/current_weight` 重新计算 `order_amount`，不得跨日保留旧委托。

## 2.8 流动性分层定义（v1.3.7 延续）

1. 分层基准：
   - 指标：`ADV60 = 近60交易日日均成交额（人民币）`。
2. 默认分层阈值：
   - 大盘：`ADV60 >= 20 亿元`
   - 中盘：`5 亿元 <= ADV60 < 20 亿元`
   - 小盘：`ADV60 < 5 亿元`
3. 刷新规则：
   - 每月首个交易日重算一次；
   - 当月分层冻结，中途不可变（除非标的退市/长期停牌触发可交易剔除）。
4. 版本化：
   - `liquidity_tier_metric/liquidity_tier_window/liquidity_tier_thresholds/liquidity_tier_rebalance_rule`
   - 必须纳入 `runtime_config_hash`。
5. 新股与稀疏数据处理：
   - 若 `listing_days < 60` 或 `ADV60` 不可得，则默认不进入交易候选集（由 2.9 Universe 规则统一控制）。
   - 若策略配置允许纳入 `listing_days >= 60` 但 `ADV60` 临时缺失，`liquidity_tier` 强制置为 `small` 并记录 `liquidity_tier_fallback=true`。

## 2.9 Universe As-Of（v1.3.7 新增）

1. 每交易日生成并冻结 `universe_snapshot_id`（as-of `decision_time`），并用于当日候选集、训练集、评估集对齐。
2. 默认纳入范围：
   - 上交所/深交所 A 股（含主板/创业板/科创板），默认不含北交所。
3. 默认剔除规则（as-of 生效）：
   - `listing_days < min_list_days`（默认 `min_list_days=60`）；
   - 已退市或进入退市整理；
   - 长期停牌（默认连续停牌 `>=20` 交易日）；
   - `ST/*ST` 与风险警示标的（可配置，但默认剔除）。
4. 反幸存者偏差约束：
   - 训练/评估必须使用各自交易日的 `universe_snapshot_id`，禁止回填“现存股票池”。
5. 版本化与回放：
   - `universe_ruleset_id/min_list_days/st_filter_rule/suspension_filter_rule/board_scope`
   - `universe_spec_hash = hash(universe_ruleset_id + board_scope + filters)`。
   - 必须纳入 `runtime_config_hash`，且 `universe_snapshot_id/universe_spec_hash` 纳入原子包与审计字段。

---

## 3. 概率口径与标签语义（统一定义）

## 3.1 概率语义

1. 统一定义：`p = Pr(R_net(h) > 0)`。
2. `h` 固定为 5 个交易日（v1.3.6 固化）。
3. `R_net` 为扣成本后收益（双边成本+滑点+冲击）。

## 3.2 概率字段

1. `p_lgbm`：LGBM 概率输出（经本模型校准后）。
2. `p_xgb`：XGB/CatBoost 概率输出（经本模型校准后）。
3. `p_arf`：River ARF/HAT 在线模型输出（启用时必须经过校准层）。
4. `p_seq`：序列模型输出（TFT/EAT/IL-Transformer 等 challenger 轨）。
5. `p_exp`：实验轨输出（DoubleAdapt/GNN/Mamba；仅影子盘默认启用）。
6. `p_corr`：在线纠偏后概率。
7. `p_meta`（v1.3.6 定义）：**等同 `p_final`（融合+最终校准后）**。
8. `p_tft`：兼容字段，若序列模型为 TFT 则 `p_tft = p_seq`。

## 3.3 融合数学链路（v1.3.6 固化）

1. 默认模式：`fusion_mode = weighted_prob_v1`。
2. `weighted_prob_v1` 定义：
   - `p_tree = blend(p_lgbm, p_xgb)`（默认等权，可配置）
   - `p_precal = w_tree*p_tree + w_online*p_arf + w_seq*p_seq + w_exp*p_exp`
   - `p_final = calibrate(p_precal)`，并裁剪到 `[0,1]`。
3. 残差模式：`fusion_mode = residual_online_v1`（仅当 Tier-B 为残差纠偏器时允许）：
   - `p_tree = blend(p_lgbm, p_xgb)`
   - `p_online_adj = clip(p_tree + r_online, 0, 1)`（`r_online` 为 Tier-B 残差输出）
   - `p_precal = w_tree*p_tree + w_online*p_online_adj + w_seq*p_seq + w_exp*p_exp`
   - `p_final = calibrate(p_precal)`。
   - 设计意图（硬说明）：该模式是“未校正与校正后预测的有意混合（mixing）”，`w_tree` 与 `w_online` 共同表示对 Tree 系预测的总信任分配，并非实现 Bug。
   - 若需“仅用校正后替换 Tree 槽位”，必须切换到新模式并版本化（不允许在 `residual_online_v1` 内私改公式）。
4. 权重来源：
   - `w_*` 必须来自 4.6 的 cap 裁剪与确定性回填结果。
5. 版本化：
   - `fusion_mode/tree_blend_rule/final_calibrator_spec` 必须纳入 `runtime_config_hash`。
6. 校准器更新节奏（v1.3.6 延续）：
   - `final_calibrator` 默认随每周 Tier-A 重训同步刷新（使用同一训练截止日的成熟标签）。
   - 日间（交易日内）冻结，不做增量更新。
   - 校准器版本必须纳入 `runtime_config_hash`。
   - 校准器随机性参数（如 `calibrator_seed`）必须纳入 `runtime_config_hash`。

---

## 4. M2 完整定义（Regime + Gating）

## 4.1 输入特征计算口径（默认）

1. `atr_ratio`：
   - 每个标的 `TR/close`
   - 取近 `N_atr=20` 交易日均值
   - 市场级聚合用横截面中位数
2. `sector_dispersion`：
   - 以行业分组收益率计算横截面离散度
   - 近 `N_disp=5` 交易日滚动
3. `turnover_zscore`：
   - `log(1+amount)` 的滚动 z-score
   - `N_turn=60` 交易日窗口
   - 市场级聚合默认：`turnover_zscore_mkt = median(z_i)`，可配置为“成交额加权均值”
   - 必须输出日志字段：`turnover_zscore_mkt` 与 `turnover_zscore_mkt_method`
4. 去极值：
   - 横截面 winsorize（1%/99%）+ 缺失率记录。

## 4.2 阈值（默认）

1. `extreme_atr_gate=0.05`
2. `extreme_turnover_z_gate=2.5`
3. `trend_turnover_z_gate=0.8`
4. `trend_dispersion_up_gate=0.30`
5. `trend_dispersion_down_gate=0.25`
6. `switch_confidence_gate=0.70`
7. `switch_confirm_days=2`

## 4.3 confidence 可审计函数（v1.3.6 固化）

1. `margin_score`：样本与阈值边界的归一化距离（按状态规则计算）。
2. `consistency_score`：近 `K_consistency=3` 天同向状态一致度。
3. `quality_score`：输入特征完整性与异常率评分。
4. 组合：
   - `confidence = clip(0.6*margin_score + 0.3*consistency_score + 0.1*quality_score, 0, 1)`
5. 必须记录三个分量日志，便于审计追踪。

## 4.4 切换与防抖（新增硬化）

1. 满足状态切换后，不直接瞬时改满权重，采用 `R=3` 日线性 ramp。
2. 若状态未切换，门控权重变化率受限：`|w_t - w_{t-1}| <= delta_max`。
3. 默认 `delta_max=0.12`；超限触发平滑并记录告警。

## 4.5 M2 输出到门控层

1. 输出字段：
   - `active_state/confidence/confidence_tier`
   - `state_switch_event`
   - `gating_weight_template_id`
2. 模板示例（默认）：
   - trend_up：Tree 偏高
   - range：纠偏与稳健权重提升
   - trend_down：风险收缩
   - extreme：高防守/低杠杆
3. 默认模板数值表（四层权重和为 1）：

| 模板 | w_tree | w_online | w_seq | w_exp |
|---|---:|---:|---:|---:|
| trend_up | 0.60 | 0.20 | 0.15 | 0.05 |
| range | 0.45 | 0.30 | 0.20 | 0.05 |
| trend_down | 0.70 | 0.10 | 0.15 | 0.05 |
| extreme | 0.85 | 0.05 | 0.05 | 0.05 |
4. 模板版本化：
   - `gating_template_table/gating_template_version`
   - 必须纳入 `gating_config_hash`。

## 4.6 cap 裁剪后的确定性再分配（v1.3.6 固化）

1. 输入：
   - `w_template`（来自 M2 模板，四层权重和为 1）
   - `cap = {cap_w_tree, cap_w_online, cap_w_seq, cap_w_exp}`（来自 M10）
2. 第一步：逐层裁剪
   - `w_i = min(w_template_i, cap_i)`。
3. 第二步：计算剩余
   - `res = 1 - sum(w_i)`。
4. 第三步：按固定优先级回填（deterministic）
   - 回填顺序固定：`Tree -> Online -> Seq -> Exp`。
   - 每层回填量：`delta_i = min(res, cap_i - w_i)`，逐层更新直到 `res <= 0`。
   - 禁止“全员等比再归一化”回填。
5. 第四步：异常兜底
   - 若 `res > 0` 且所有层均达到 cap，触发 `force_champion_only=true`，并置 `w_tree=1, 其余=0`。
   - 必须记录 `weight_fallback_reason=cap_exhausted`。
6. 数值稳定性
   - 权重保留 `4` 位小数；舍入残差统一加到 `w_tree`，保证总和严格为 `1`。

## 4.7 重大状态降级事件（v1.3.6 延续）

1. 触发条件（任一满足）：
   - `active_state` 从 `trend_up/range` 切换到 `trend_down/extreme`；
   - `state_switch_event` 且 `confidence >= 0.70` 且目标状态为 `trend_down/extreme`。
2. 输出字段：
   - `regime_major_downgrade=true`
   - `regime_lock_days=holding_horizon(默认 5)`

---

## 5. M10 完整定义（Model Health + Action）

## 5.1 输入与覆盖

1. 输入概率字段：`p_lgbm/p_xgb/p_arf/p_seq/p_exp/p_meta(p_final)`（按启用模型子集计算）。
2. 输入价格字段：`open/close`（用于收益波动估计）。
3. 在线稳定性字段：`drift_warning_ratio`、`online_update_fail_ratio`。
4. M2 联动字段：`regime_major_downgrade`、`regime_lock_days`。
5. 数据可得性字段：`data_latency_sec`、`latency_by_input`、`latency_worst_input`、`data_latency_breach_ratio_20d`、`latency_watch_flag`。
6. 校准指标计算仅允许使用成熟标签样本。
7. 未启用模型字段必须显式记 `null`，并在一致性指标分母中剔除。

## 5.2 指标分组

1. 一致性指标：
   - `mean_model_spread`
   - `high_conflict_ratio`
2. 真实校准指标（新增硬指标）：
   - `ECE_20d`
   - `Brier_20d`
   - `LogLoss_20d`
3. 可观测性指标：
   - `prediction_coverage_ratio`
4. 市场噪声指标：
   - `return_volatility`
5. 在线稳定性指标：
   - `drift_warning_ratio`
   - `online_update_fail_ratio`
6. 数据可得性指标：
   - `data_latency_breach_ratio_20d`

## 5.2.1 指标计算细则（v1.3.7 固化）

1. `return_volatility` 口径：
   - `return_volatility = std(NetReturn_daily, 20d)`。
   - `NetReturn_daily` 默认使用 `stockpick_eval_profile` 组合日收益（非年化）。
2. `ECE_20d` 口径：
   - 默认 `n_bins=10`，`equal_frequency` 分桶；
   - 每 bin 最小样本 `min_bin_samples=30`，不足则自动降到 `n_bins=5`；
   - 样本权重默认等权（`sample_weight=1`）。
3. `Brier_20d/LogLoss_20d`：
   - 默认等权样本；
   - 仅使用 `label_mature_time <= now` 的成熟标签样本。
4. 版本化要求：
   - `ece_bins/ece_binning/ece_min_bin_samples/metric_sample_weight_mode`
   - 必须写入 `runtime_config_hash`，保证阈值判定可复现。
5. 数据延迟指标口径（v1.3.7 固化）：
   - `data_latency_sec` 必须按 1.5 的 max-latency 公式计算；
   - `data_latency_breach_ratio_20d = (#(data_latency_sec > max_data_latency_sec) in 20d) / 20`；
   - `latency_by_input` 必须逐输入落库，支持定位最慢输入来源。

## 5.3 状态判定与阈值表（默认）

| 指标 | healthy | watch | degraded |
|---|---|---|---|
| `mean_model_spread` | `<= 0.15` | `(0.15, 0.25]` | `> 0.25` |
| `high_conflict_ratio` | `<= 0.25` | `(0.25, 0.50]` | `> 0.50` |
| `ECE_20d` | `<= 0.03` | `(0.03, 0.06]` | `> 0.06` |
| `Brier_20d` | `<= 0.20` | `(0.20, 0.25]` | `> 0.25` |
| `LogLoss_20d` | `<= 0.60` | `(0.60, 0.75]` | `> 0.75` |
| `return_volatility` | `<= 0.06` | `(0.06, 0.10]` | `> 0.10` |
| `drift_warning_ratio` | `<= 0.10` | `(0.10, 0.25]` | `> 0.25` |
| `online_update_fail_ratio` | `<= 0.05` | `(0.05, 0.15]` | `> 0.15` |
| `data_latency_breach_ratio_20d` | `<= 0.05` | `(0.05, 0.15]` | 不直接判 degraded（转 limited_observability） |
| `prediction_coverage_ratio` | `>= 0.80` | `[0.60, 0.80)` | 不直接判 degraded（转 limited_observability） |

1. `limited_observability`：
   - `prediction_coverage_ratio < 0.60`，或
   - 成熟标签样本数 `< min_mature_samples(默认 120)`，或
   - `data_latency_breach_ratio_20d > 0.15`，或
   - 当日 `data_latency_sec > 2*max_data_latency_sec`。
2. `latency_watch`：
   - `max_data_latency_sec < data_latency_sec <= 2*max_data_latency_sec`。
3. `no_data`：
   - 成熟标签样本数 `= 0`。
4. 判定顺序（硬约束）：
   - 先判 `no_data`/`limited_observability`。
   - 再判 `degraded`（任一指标达到 degraded）。
   - 再判 `latency_watch`（若满足则至少为 watch）。
   - 再判 `watch`（任一指标进入 watch 且无 degraded）。
   - 否则为 `healthy`。

## 5.3.1 阈值档位（normal/stress，v1.3.6 固化）

1. `threshold_profile_id = normal | stress`。
2. 档位选择规则：
   - 当 `active_state in {trend_down, extreme}` 时使用 `stress`；
   - 其余状态使用 `normal`。
3. `stress` 档阈值调整（相对 5.3 基线）：
   - `mean_model_spread` 上限放宽 `+0.05`；
   - `high_conflict_ratio` 上限放宽 `+0.10`；
   - `ECE_20d` 上限放宽 `+0.01`；
   - `Brier_20d` 上限放宽 `+0.02`；
   - `LogLoss_20d` 上限放宽 `+0.10`；
   - `return_volatility` 上限放宽 `+0.02`。
4. `stress` 档动作加严：
   - 在相同健康状态下，`raise_u_threshold_bp` 额外 `+10bp`；
   - `cap_w_online = min(cap_w_online, 0.30)`。
5. 版本化：
   - `threshold_profile_id/stress_threshold_offsets/stress_action_overrides`
   - 必须纳入 `runtime_config_hash`。

## 5.4 M10 动作接口（必须统一）

统一输出结构：

```yaml
health_action:
  cap_w_tree: 0.00-1.00
  cap_w_online: 0.00-1.00
  cap_w_seq: 0.00-1.00
  cap_w_exp: 0.00-1.00
  online_profile: base|promoted
  threshold_profile_id: normal|stress
  freeze_gating: bool
  raise_u_threshold_bp: int
  online_update_budget_ratio: 0.00-1.00
  block_online_update: bool
  force_champion_only: bool
```

上述 `cap_w_*` 为权重上限，不是最终权重；最终权重由 M2 模板并按 4.6 的确定性回填规则生成。

## 5.5 默认动作矩阵

触发规则：当 `health_state=healthy` 且 `tier_b_promoted=true` 且 `promotion_revoked=false` 时，使用 `promoted_healthy`；否则使用 `healthy`。

1. healthy：
   - `cap_w_tree=1.00`
   - `cap_w_online=0.40`
   - `cap_w_seq=0.30`
   - `cap_w_exp=0.10`
   - `online_profile=base`
   - `online_update_budget_ratio=1.00`
   - `freeze_gating=false`
2. promoted_healthy（healthy 且 Tier-B 晋升后）：
   - `cap_w_tree=1.00`
   - `cap_w_online=0.60`
   - `cap_w_seq=0.20`
   - `cap_w_exp=0.05`
   - `online_profile=promoted`
   - `online_update_budget_ratio=1.00`
   - `freeze_gating=false`
3. watch：
   - `cap_w_tree=1.00`
   - `cap_w_online=0.20`
   - `cap_w_seq=0.15`
   - `cap_w_exp=0.00`
   - `online_profile=base`
   - `online_update_budget_ratio=0.50`
   - `raise_u_threshold_bp=20`
4. degraded：
   - `cap_w_tree=1.00`
   - `cap_w_online=0.00`
   - `cap_w_seq=0.05`
   - `cap_w_exp=0.00`
   - `online_profile=base`
   - `online_update_budget_ratio=0.00`
   - `freeze_gating=true`
   - `block_online_update=true`
   - `force_champion_only=true`
5. limited_observability/no_data：
   - `cap_w_tree=1.00`
   - `cap_w_online=0.00`
   - `cap_w_seq=0.00`
   - `cap_w_exp=0.00`
   - `online_profile=base`
   - `online_update_budget_ratio=0.00`
   - `freeze_gating=true`
   - `block_online_update=true`
   - `force_champion_only=true`
   - `raise_u_threshold_bp=30`
   - 若由数据延迟 SLO 触发，`block_online_update=true` 为强制动作，不可覆盖

latency_watch 覆盖规则：当 `latency_watch=true` 且未进入 `limited_observability` 时，至少应用 watch 档动作并强制 `block_online_update=true`。

stress 档覆盖规则：在上述任一状态动作基础上，叠加 5.3.1 的 `stress` 动作加严项。

## 5.6 恢复条件

1. 恢复评估窗口长度固定为 `recover_eval_window_days=5`（交易日）。
2. 连续 `N_recover=3` 个恢复评估窗口 `healthy` 才解除冻结（默认约 15 个交易日）。
3. 恢复时采用分阶段解冻（先解除更新阻断，再恢复权重上限）。
4. `recover_eval_window_days/N_recover` 必须纳入 `runtime_config_hash`。

## 5.7 M2 重大降级联动锁（v1.3.6 延续）

1. 若 `regime_major_downgrade=true`，立即触发：
   - `block_online_update=true`
   - `online_update_budget_ratio=0`
   - `cap_w_online=min(cap_w_online, 0.10)`
2. 锁定时长：
   - `regime_lock_days` 默认等于 `holding_horizon`（5 个交易日）。
3. 锁定期间：
   - 允许推理，不允许在线 `partial_fit`。
4. 解锁条件：
   - 锁期结束且 M10 非 degraded，才可恢复在线更新。

---

## 6. M11 红线定义（Shadow Guard）

## 6.1 基线口径

1. 基线为同窗口、同成本模型、同执行规则下的 champion。
2. challenger 仅与该基线做 delta 比较。

## 6.2 计算窗口（默认）

1. 主窗口：滚动 20 交易日。
2. 辅窗口：滚动 10 交易日用于早期预警。

## 6.3 红线公式（默认）

1. `drawdown_delta = MDD(challenger) - MDD(champion)`
2. 尾损统一正值口径：
   - `TailLoss = -ES95(returns)`（若返回值为负，取相反数；单位为收益比例）
   - `tail_loss_delta = TailLoss(challenger) - TailLoss(champion)`
3. `execution_divergence_ratio = mismatch_fills / total_signals`
4. `mismatch_fills` 计数定义（满足任一条即计 1）：
   - 成交状态不一致（`full_fill/partial_fill/no_fill`）
   - 两侧均成交但 `|fill_price_challenger - fill_price_champion| > 30bp`
   - `|fill_ratio_challenger - fill_ratio_champion| > 0.20`
5. `total_signals` 定义为同窗口内通过可交易过滤后的共同信号数。

## 6.4 触发阈值与策略

1. `drawdown_delta_limit = 0.05`（基于 20 日主窗口，硬触发，单窗口即生效）
2. `drawdown_delta_fast10_limit = 0.05`（基于 10 日辅窗口，硬触发，单窗口即生效）
3. `tail_loss_delta_limit = 0.03`（连续 2/3 窗口触发）
4. `execution_divergence_limit = 0.35`（连续 2/3 窗口触发）
5. 上述阈值单位均为收益比例（非百分点），例如 `0.03 = 3%`。
6. 任一硬触发或组合触发满足后：
   - challenger 禁止晋升
   - 自动回退 champion
   - 输出归因与审计日志

---

## 7. Utility Execution 细化（防“数学正确/交易失效”）

## 7.0 单位约定

1. `U` 的默认单位为 `bp`（基点）。
2. `raise_u_threshold_bp` 与 `U` 同量纲，直接做加法阈值提升。
3. 若切换到“收益比例”量纲，必须同时版本化 `U_unit` 与动作接口，不允许混用。

## 7.1 p 到 E[r] 映射

1. 采用分层映射：`regime x liquidity_tier x volatility_tier`。
2. 每层使用单调拟合（isotonic 或分段单调）。
3. 映射更新仅用成熟标签样本。
4. 最小样本门禁（v1.3.7 固化）：
   - `min_samples_per_bucket = 300`（默认）。
   - 低于门槛不得直接拟合当前桶映射。
5. 分层回退策略（deterministic）：
   - 回退顺序固定：`regime x liquidity x volatility` -> `regime x liquidity` -> `regime` -> `global`。
   - 首个满足 `min_samples_per_bucket` 的层级即作为当前映射来源。
6. 新股/稀疏样本处理（v1.3.7 新增）：
   - 若 `sparse_history_flag=true`（默认定义：`listing_days < 120` 或 `ADV60` 缺失），禁止使用最细粒度桶；
   - 该类样本映射起点强制从 `regime x liquidity` 开始回退，防止细桶抖动。
7. 审计字段：
   - 必须记录 `mapping_level_used`、`bucket_sample_count`、`mapping_fallback_steps`。
8. 版本化要求：
   - `min_samples_per_bucket/mapping_fallback_order/mapping_fit_method/sparse_history_rule`
   - 必须纳入 `runtime_config_hash`。
9. 映射稳定化（v1.3.7 固化）：
   - `mapping_update_cooldown_days = 3`（默认），冷却期内不切换拟合层级；
   - `mapping_ema_alpha = 0.30`（默认），对 `E[r]` 映射输出做 EMA 平滑；
   - 冷却或平滑参数变更必须版本化并审计。

## 7.1.1 波动率分层定义（v1.3.7 延续）

1. 指标定义：
   - `mkt_volatility = return_volatility`（使用 5.2.1 的市场级 20 日标准差口径）。
2. 默认分层：
   - `low`: `mkt_volatility <= 0.04`
   - `mid`: `0.04 < mkt_volatility <= 0.08`
   - `high`: `mkt_volatility > 0.08`
3. 刷新频率：
   - 每交易日更新（随 M10 指标计算同步）。
4. 版本化：
   - `volatility_tier_metric/volatility_tier_thresholds/volatility_tier_refresh_rule`
   - 必须纳入 `runtime_config_hash`。

## 7.2 组合构建（默认）

1. 候选集：当日 `U > U_min` 且 `E[r_net] > 0` 的标的。
2. 负效用截断（硬约束）：
   - 若 `U <= 0` 或 `E[r_net] <= 0`，即使排名靠前也必须剔除，不得开仓。
3. 取每日 `TopK`（默认 `K_base=20`）。
4. 配仓：按 `U/风险` 比例分配，叠加单票与行业上限约束。
   - 默认 `risk_i = std(r_i_daily, 20d)`（个股近20日日收益标准差，非年化）。
   - 数值稳定下限：`risk_floor = 0.005`，实际使用 `risk_eff_i = max(risk_i, risk_floor)`。
   - 若切换到 CVaR 等风险口径，必须版本化并记录。
5. 动态 K 规则（容量/换手约束触发时）：
   - `K_dynamic = max(K_min, floor(K_base * alpha))`
   - 默认 `K_min=8`，`alpha` 由约束压力线性映射到 `[0.4, 1.0]`
   - `constraint_pressure = max(turnover_excess_ratio, capacity_excess_ratio)`
   - `turnover_excess_ratio = max(0, turnover / turnover_limit - 1)`
   - `capacity_excess_ratio = max(0, participation / participation_cap - 1)`
   - `alpha = clip(1.0 - constraint_pressure, 0.4, 1.0)`
6. 必须记录审计字段：`k_base/k_dynamic/alpha/trim_reason_codes/negative_u_filtered_count`。
7. 版本化要求：
   - `risk_metric/risk_window_days/risk_floor`
   - 必须纳入 `runtime_config_hash`。

## 7.3 净收益口径（Go/No-Go 固化）

`NetReturn = GrossReturn - Commission - StampTax - TransferFee - Slippage - Impact`

1. `dividend_treatment` 必须与 2.2.10 的 `price_series_mode` 一致：
   - `implicit_by_qfq`：分红收益由复权价格隐含体现，不再重复加现金流；
   - `explicit_cashflow`：价格序列不隐含分红时，必须显式计入现金分红。
2. 标签、评估、风控三处收益计算必须使用同一 `dividend_treatment`。

所有评估、影子盘、晋升评审统一此口径，不允许多版本并存。

---

## 8. 硬门禁测试清单（自动化）

1. 时间一致性测试：
   - 随机样本验证 `available_time <= decision_time`
2. 成熟标签门禁测试：
   - online update 仅能读取 matured 分区
3. 执行价一致性测试：
   - `next_tradable_vwap` 计算窗口、填充代理、标签与评估版本一致
4. 成交可行性测试：
   - 停牌/涨跌停/T+1 场景不记虚拟成交
5. Entry/Exit no_fill 差异测试：
   - Entry no_fill 记 `R_net=0`；
   - Exit no_fill 必须进入延后平仓/强制清算，不得记交易作废
6. M10 阈值状态测试：
   - healthy/watch/degraded/limited_observability/no_data 判定与阈值表一致
7. M11 符号口径测试：
   - `TailLoss=-ES95` 与 `tail_loss_delta` 方向一致，`mismatch_fills` 计数规则一致
8. 权重防抖测试：
   - `|w_t-w_{t-1}|` 超阈触发平滑/冻结
9. 成本压力测试：
   - `x2` 成本下收益不允许由正转负后仍晋升
10. 分流动性压力测试：
   - 大盘/中盘/小盘分层报告必须齐全
11. M10 熔断动作测试：
    - degraded 状态是否正确下发动作接口
12. M11 红线回滚测试：
    - 触发时是否一键回退到 champion
13. 动态 K 审计测试：
    - 触发约束时 `k_base/k_dynamic/alpha/trim_reason_codes` 必须完整落库
14. 在线预算与冷却测试：
    - `online_update_budget_ratio`、`cooldown_days`、`block_online_update` 联动是否正确
15. 模型分层晋升测试：
    - Tier-B/Tier-C/Tier-D 的晋升、降级、退役门槛是否按规则执行
16. 执行参数版本测试：
    - `limit_price/slippage/participation_cap/order_amount` 是否存在版本快照并纳入 `execution_spec_hash`
17. cap 再分配确定性测试：
    - 同一输入在多次运行下输出权重完全一致，且总和为 `1`
18. 在线自反馈降权测试：
    - 指标超阈后样本是否进入“降权学习”路径，并触发 `cap_w_online` 抑制
19. 周更衔接一致性测试：
    - `rebase_then_replay` 后模型版本链可追溯、可回滚
20. 重训防穿越断言测试：
    - 每周重训训练集尾部必须剔除 `holding_horizon + settlement_lag` 天样本
21. M2 重大降级联动锁测试：
    - `regime_major_downgrade=true` 后是否强制 `block_online_update` 5 日
22. 融合公式一致性测试：
    - `fusion_mode` 切换后输出必须符合 3.3 数学链路且可复现
23. 冲击参数版本化测试：
    - `impact_k_tier_table/impact_eta` 是否纳入 `execution_spec_hash`
24. 强制清算价时点测试：
    - `close_t` 是否按 2.7.7 规则取值且可复现
25. 映射样本门禁测试：
    - 当 `bucket_sample_count < min_samples_per_bucket` 时是否正确触发回退
26. 映射回退一致性测试：
    - 同一数据快照下 `mapping_level_used` 与 `E[r]` 输出一致
27. 周更差异审计测试：
    - 每次 `rebase_then_replay` 后必须写入 `replay_diff_p_meta_p50/p90/max` 与 `replay_diff_turnover`
28. 残差融合意图测试：
    - `residual_online_v1` 必须按 3.3 的 mixing 公式执行，不得私自替换 Tree 槽位
29. Tier-B 晋升撤销测试：
    - 超过 `demotion_tolerance` 时必须撤销 `tier_b_promoted` 并回退 `healthy` 模板
30. 流动性分层冻结测试：
    - 月内分层不可变、月初重算一次
31. settlement_lag 默认值测试：
    - 未显式配置时必须使用 `settlement_lag=1`
32. 风险口径一致性测试：
    - 配仓 `risk_i` 必须按 7.2 定义，`risk_floor` 生效
33. 校准器节奏测试：
    - 校准器仅周更同步刷新，交易日内不做增量更新
34. 恢复窗口语义测试：
    - `recover_eval_window_days=5` 与 `N_recover=3` 的解冻判定一致
35. Precision@K 评估一致性测试：
    - Go/No-Go 评估必须固定使用 `K_base=20`，不随 `K_dynamic` 变化
36. 复现性参数测试：
    - `random_seed/num_threads/deterministic_mode/library_versions_hash` 必须存在且可回放一致
37. 数据延迟 SLO 测试：
    - 轻度延迟进入 `latency_watch`；严重/持续延迟进入 `limited_observability`
38. 价格序列口径测试：
    - `price_series_mode` 在标签、评估、风险计算中必须一致，不一致即失败
39. stress 阈值档测试：
    - `active_state in {trend_down, extreme}` 时必须启用 `threshold_profile_id=stress`
40. 双评估成交分布测试：
    - `no_fill_ratio/partial_fill_ratio` 必须满足 2.6.5 与 9.10 门槛
41. 映射平滑冷却测试：
    - `mapping_update_cooldown_days` 与 `mapping_ema_alpha` 生效且可审计
42. 每日对账一致性测试：
    - `target_weight/filled_weight/end_of_day_position` 差值分布必须落库
43. Runbook 演练测试：
    - 每月至少 1 次演练 M10 或 M11 触发到回滚验证的全流程
44. 波动率分层测试：
    - `volatility_tier` 必须按 7.1.1 阈值每日更新并落库
45. alpha 映射函数测试：
    - `constraint_pressure -> alpha` 必须按 7.2.5 线性公式可复现
46. filled_weight 口径测试：
    - 实盘与影子盘下 `filled_weight_i` 计算口径均满足 8.3.1 定义
47. 对账漂移公式测试：
    - `position_drift_ratio` 必须按 8.3.2 公式计算
48. 数据延迟公式正确性测试：
    - `data_latency_sec` 必须等于最慢关键输入延迟（`max_i(now-available_time_i)`），并输出 `latency_by_input`
49. Universe 快照一致性测试：
    - 训练/推理/评估在同一交易日必须使用同一 `universe_snapshot_id`，且 as-of 可回放一致
50. 交易单位与残单策略测试：
    - `share_rounding_rule/price_tick_rule/min_notional_per_order/residual_order_policy` 必须按 2.7 执行并可审计
51. 分红语义一致性测试：
    - `price_series_mode` 与 `dividend_treatment` 必须匹配，标签/评估/风险三处一致
52. 执行窗口敏感性测试（shadow）：
    - `open_window/day_window` 差异超过阈值时必须触发 `execution_sensitivity_alert`
53. 新股稀疏回退测试：
    - `sparse_history_flag=true` 时映射不得使用最细粒度桶，必须按 7.1.6 回退
54. 在线样本顺序确定性测试：
    - `partial_fit` 样本顺序在重复运行下完全一致，`online_samples_used_hash` 可复现

---

## 8.1 影子盘稳定性验收顺序（v1.3.7 延续）

1. 执行与退出链路：
   - 优先验证 Entry/Exit `no_fill`、延后平仓、强制清算全链路。
2. 确定性复现：
   - 同一输入快照重复运行 5 次，检查 `w_*`、`p_final`、`mapping_level_used` 完全一致。
3. 重大降级联动锁：
   - 强制切换到 `trend_down/extreme`，检查 5 日在线静默与 `cap_w_online` 压制。
4. M10 动作闭环：
   - 构造 degraded 场景，检查 `freeze/block/force_champion_only` 与 4.6 回填兜底。
5. M11 回滚闭环：
   - 构造 `drawdown_delta_fast10` 触发，检查自动回退 champion 与归因日志。

---

## 8.2 事故处置 Runbook（v1.3.7 固化）

1. 触发：
   - M10 `degraded`、M11 红线、或数据延迟 SLO 违约。
2. 首轮检查（5 分钟内）：
   - 查看 `health_state/threshold_profile_id/data_latency_sec/latency_worst_input/regime_major_downgrade`；
   - 查看 `latency_by_input` 与 `universe_snapshot_id` 是否异常漂移；
   - 查看 `replay_diff_p_meta_*`、`mapping_level_used`、`execution_divergence_ratio`；
   - 查看当日 `no_fill_ratio/partial_fill_ratio` 与基线差值。
3. 动作：
   - 先执行 `force_champion_only`；
   - 必要时指针回滚到上一稳定原子包；
   - 锁定在线更新（`block_online_update=true`）。
4. 回滚后验证：
   - 连续 1 个恢复窗口内检查 `M10 非 degraded`、`M11 不再触发`、对账差值恢复。
   - 该步骤仅用于应急确认，不等同正式解除冻结；正式解冻仍以 5.6 的 `N_recover=3` 为准。
5. 结案：
   - 输出事故单（触发原因、动作、耗时、恢复指标）并沉淀 `reason_codes`。

---

## 8.3 交易与持仓对账（v1.3.7 固化）

1. 每日对账核心指标：
   - `target_weight_i` vs `filled_weight_i` vs `end_of_day_position_weight_i`。
   - `filled_weight_i` 定义：
   - `filled_weight_i = (prev_position_value_i + net_filled_value_i_today) / portfolio_nav_today`。
   - 影子盘模式下，`net_filled_value_i_today` 使用撮合代理模拟成交替代真实成交。
2. 汇总统计：
   - 差值分布 `p50/p90/max`；
   - `position_drift_ratio`（组合层总偏离）。
   - 公式：`position_drift_ratio = sum_i(abs(target_weight_i - end_of_day_position_weight_i)) / 2`。
3. 异常门槛（默认）：
   - `position_drift_ratio > 0.05` 触发告警；
   - 连续 3 日超阈触发 `raise_u_threshold_bp +20`。
4. 审计要求：
   - 对账结果必须按 `trading_date + model_bundle_hash` 落库，支持回放比对。

---

## 9. Go/No-Go 审核条款（v1.3.7 固化）

1. 评估窗口：最近 20 个交易日（主），60 个交易日（稳定性辅检）。
2. 命中率：`Precision@K` 相对基线提升 >= 2pp（`K=K_base=20`，按“每日 TopK 排名”统计，不受 `K_dynamic` 影响）。
3. 净收益：扣全成本后净收益 > 基线。
4. 回撤：MDD 不劣化超过 1pp。
5. 换手：不超过基线 1.2 倍。
6. M10：`degraded` 占比不高于基线。
7. M11：红线触发率不高于基线。
8. 门禁测试：全部通过后才允许晋升。
9. 动态 K 触发时，`trim_reason_codes` 审计完整率必须为 100%。
10. 双评估档要求：
    - `stockpick_eval_profile` 必须满足条款 2-5；
    - `trading_eval_profile` 约束 A：`NetReturn_gap >= -0.01`；
    - `trading_eval_profile` 约束 B：`MDD_gap <= 0.01`；
    - `trading_eval_profile` 约束 C：`no_fill_ratio <= 0.20` 且 `partial_fill_ratio <= 0.35`；
    - `trading_eval_profile` 约束 D：相对基线劣化 `no_fill_ratio_delta <= 0.05`、`partial_fill_ratio_delta <= 0.08`。
11. 模型晋升附加条件：
    - Tier-B/Tier-C 晋升前需满足 11.3 条款，且连续 2 个 20 日窗口通过。
12. Exit 风险约束：
    - Exit `no_fill` 交易必须完成延后平仓或强制清算；该流程审计完整率必须为 100%。
13. 负效用约束：
    - 当期开仓集合中 `U<=0` 或 `E[r_net]<=0` 标的数量必须为 0。
14. 冲击参数约束：
    - `impact_k_tier_table/impact_eta` 必须存在于 `execution_spec_hash` 快照中。
15. 映射稳定性约束：
    - `bucket_sample_count < min_samples_per_bucket` 时必须发生层级回退，且 `mapping_level_used` 审计完整率为 100%。
16. 晋升治理约束：
    - Tier-B 若触发晋升撤销条件，`tier_b_promoted` 必须在当日评估后置为 `false`。
17. 分层一致性约束：
    - 流动性分层快照与交易当日使用分层必须一致，审计完整率 100%。
18. 复现性约束：
    - 原子包必须包含 `random_seed/num_threads/deterministic_mode/library_versions_hash`。
19. 数据延迟约束：
    - 轻度延迟必须进入 `latency_watch` 且阻断在线更新；严重/持续延迟必须进入 `limited_observability`。
20. 价格口径约束：
    - `price_series_mode` 在标签、评估、风险计算三处一致，审计完整率 100%。
21. Universe 一致性约束：
    - 当日候选/训练/评估必须使用同一 `universe_snapshot_id`，且回放一致率 100%。
22. 执行微规则约束：
    - `share_rounding_rule/price_tick_rule/min_notional_per_order/residual_order_policy` 必须写入 `execution_spec_hash` 且与回放一致。
23. 分红语义约束：
    - `dividend_treatment` 与 `price_series_mode` 匹配，收益口径审计完整率 100%。

---

## 10. 回滚原子包与恢复流程（v1.3.7 固化）

## 10.1 原子包内容

1. `data_snapshot_id`
2. `feature_spec_hash`
3. `model_bundle_hash`
4. `gating_config_hash`
5. `execution_spec_hash`
6. `runtime_config_hash`
7. `random_seed`
8. `num_threads`
9. `deterministic_mode`
10. `library_versions_hash`（lightgbm/xgboost/river/torch 等关键依赖）
11. `universe_snapshot_id`
12. `universe_spec_hash`

## 10.2 存储策略

1. 原子包写入不可变版本目录（或对象存储版本桶）。
2. 线上仅持有“当前版本指针”，不允许现场散改参数。

## 10.3 回滚步骤

1. 风控触发（M10/M11/人工） -> 锁定新变更。
2. 将版本指针切回上一稳定包。
3. 重载模型与配置。
4. 运行回滚后健康检查。
5. 记录完整审计链路。

---

## 11. 自学习模型协同与节奏（v1.3.7 固化）

## 11.1 模型分层与职责

1. `Tier-A Core（主线）`：`LGBM + XGB/CatBoost`，负责稳定横截面选股能力，默认权重下限最高。
2. `Tier-B Online（在线）`：River `ARF/HAT` 或残差纠偏器，负责快速吸收新漂移，仅做小步增量。
3. `Tier-C Seq Challenger（序列挑战）`：`TFT/EAT/IL-Transformer`，负责中期 regime 切换下的时序补强。
4. `Tier-D Experimental（实验）`：`DoubleAdapt/GNN/Mamba` 等，仅允许影子盘，不得直接进主交易融合。
5. 融合顺序：`Tier-A -> Tier-B 校正 -> Tier-C 补充 -> 最终校准 -> p_meta(p_final)`。

## 11.2 自学习节奏（固定调度）

1. 每日（交易日）：
   - 仅用 `label_mature_time` 已成熟样本执行在线 `partial_fit`。
   - 执行前检查 M10 与 5.7 联动锁；非 healthy/watch 或处于 `regime_lock_days` 不得更新。
   - 样本顺序必须确定性：默认按 `label_mature_time, trade_date, symbol` 升序；禁止无序并行喂样本。
   - 每次更新必须生成并落库 `online_samples_used_hash`（样本主键序列哈希）。
   - 执行后记录 `online_update_budget_ratio` 与更新样本计数。
2. 每周（周末）：
   - 全量重训 challenger（Tier-A/Tier-B），回放最近 3-5 年滚动窗口。
   - 重训防穿越断言：训练截止日必须 `<= now - (holding_horizon + settlement_lag)`。
   - 输出 shadow 结果并进入 M11 对照。
   - 在线衔接默认策略：`rebase_then_replay`。
   - 具体规则：先用周末重训快照替换在线模型基线，再重放最近 `N_replay=5` 交易日成熟样本做小步 `partial_fit`。
   - 必须记录重放前后预测差异：`replay_diff_p_meta_p50/p90/max` 与 `replay_diff_turnover`。
   - 若 replay 失败则回退到“仅 rebase”并打 `online_handoff_warning`。
3. 每月（首个周末）：
   - 刷新 Tier-C 序列模型（或保持 challenger-only）。
   - 执行特征漂移审计（PSI/缺失率/异常率）并更新特征白名单建议。

## 11.3 晋升、降级与退役门槛

1. Tier-B 晋升（提升在线权重上限）：
   - 最近 20 日 `ECE/Brier/LogLoss` 不劣于 Tier-A 基线（容忍度 `promotion_tolerance`）；
   - 默认 `promotion_tolerance`：`ece<=+0.005`、`brier<=+0.005`、`logloss<=+0.010`（相对 Tier-A）。
   - `drift_warning_ratio <= 0.10`；
   - `online_update_fail_ratio <= 0.05`。
   - 晋升生效后，M10 在 `healthy` 状态下切换到 `promoted_healthy` 动作模板（5.5）。
2. Tier-C 晋升（进入正式融合）：
   - 连续 2 个 20 日窗口满足 Go/No-Go；
   - M11 红线 0 次触发；
   - execution 偏差在阈值内（`execution_divergence_ratio <= 0.35`）。
3. Tier-D 退役或降级：
   - 60 日 shadow 无显著增益（Precision@K 提升 < 1pp 且净收益无提升）；
   - 或任一窗口触发 M11 硬红线。
4. Tier-B 晋升撤销条件（v1.3.6 延续）：
   - 当 `tier_b_promoted=true` 时，若最近 20 日内 Tier-B 的 `ECE/Brier/LogLoss` 任一劣于 Tier-A 超过 `demotion_tolerance`，自动撤销晋升。
   - 默认 `demotion_tolerance`：`ece>+0.010` 或 `brier>+0.010` 或 `logloss>+0.020`（相对 Tier-A）。
   - 撤销动作：`tier_b_promoted=false`，并回退到 `healthy` 动作模板。
   - 必须记录 `promotion_revoked=true` 与 `revocation_reason_codes`。
5. 版本化要求：
   - `promotion_tolerance/demotion_tolerance`
   - 必须纳入 `runtime_config_hash`。

## 11.4 在线更新防失控约束

1. 单日更新预算：`max_online_samples_per_day`（默认 1,500）与 `online_update_budget_ratio` 联动。
2. 冷却机制：连续 2 日出现 `degraded`，进入 `cooldown_days=3`，强制 `block_online_update=true`。
3. 失败回滚：在线更新任务失败或指标异常时，自动回滚到前一日在线模型快照。
4. 自反馈抑制代理指标（默认）：
   - `own_participation_ratio = own_filled_amount / market_amount_window`
   - `realized_slippage_bp = abs(fill_price - vwap_proxy) / vwap_proxy * 10000`
   - 影子盘模式下：`own_filled_amount=0`，自反馈抑制检查默认跳过，仅在实盘/模拟盘成交回放模式生效。
5. 自反馈抑制阈值（按流动性分层）：
   - `own_participation_ratio`：大盘 `1.5%`，中盘 `1.0%`，小盘 `0.5%`
   - `realized_slippage_bp`：大盘 `8bp`，中盘 `15bp`，小盘 `25bp`
6. 动作规则：
   - 任一指标超阈值时，样本不得直接丢弃，必须进入“降权学习”路径：
   - `sample_weight_online = clip(1 - lambda_impact*excess_ratio, w_min, 1)`，默认 `lambda_impact=0.7`，`w_min=0.2`。
   - 该样本计入 `online_samples_downweighted`（而非 `online_samples_skipped`）。
   - 对次日 `cap_w_online` 再乘 `0.5`。
   - 连续 3 日超阈值，触发 `block_online_update=true` 1 个评估窗口。
7. 版本化要求：
   - 以上阈值与动作必须纳入 `runtime_config_hash`。

## 11.5 审计字段（新增必落库）

1. `model_tier`、`model_role`、`is_shadow_only`
2. `online_update_budget_ratio`、`online_samples_used`、`online_samples_used_hash`、`online_samples_downweighted`、`online_samples_skipped`
3. `promotion_candidate`、`promotion_decision`、`promotion_reason_codes`、`promotion_revoked`、`revocation_reason_codes`
4. `rollback_trigger_source`、`rollback_target_bundle`
5. `online_handoff_mode`、`online_handoff_warning`、`weight_fallback_reason`、`tier_b_promoted`
6. `eval_profile_id`、`execution_spec_hash`、`runtime_config_hash`
7. `regime_major_downgrade`、`regime_lock_days_remaining`
8. `entry_no_fill`、`exit_no_fill`、`forced_exit`
9. `replay_diff_p_meta_p50`、`replay_diff_p_meta_p90`、`replay_diff_p_meta_max`、`replay_diff_turnover`
10. `forced_exit_close_date`、`forced_exit_close_price`
11. `mapping_level_used`、`bucket_sample_count`、`mapping_fallback_steps`
12. `threshold_profile_id`、`data_latency_sec`、`data_latency_breach_ratio_20d`、`latency_by_input`、`latency_worst_input`
13. `price_series_mode`、`dividend_treatment`、`no_fill_ratio`、`partial_fill_ratio`、`execution_sensitivity_alert`
14. `target_vs_filled_weight_p50/p90/max`、`filled_vs_eod_weight_p50/p90/max`、`position_drift_ratio`
15. `random_seed`、`num_threads`、`deterministic_mode`、`library_versions_hash`
16. `latency_watch_flag`、`volatility_tier`、`constraint_pressure`、`alpha`
17. `share_rounding_rule`、`price_tick_rule`、`min_notional_per_order`、`residual_order_policy`
18. `universe_snapshot_id`、`universe_ruleset_id`、`universe_spec_hash`

---

## 12. 关键问点确认（按默认建议落地）

| 问题 | v1.3.7 默认决策 |
|---|---|
| 交易频率与执行时点 | 日频，T+1 首个可交易时段执行 |
| settlement_lag | 默认 `1` 交易日，并纳入 `execution_spec_hash` |
| p 的语义 | `Pr(R_net(5d)>0)` |
| p_meta 语义 | `p_final`（融合+最终校准） |
| 融合模式 | 默认 `weighted_prob_v1`；`residual_online_v1` 为有意 mixing 模式（3.3） |
| 校准器节奏 | 每周随 Tier-A 重训刷新，交易日内冻结 |
| 复现性参数 | `random_seed/num_threads/deterministic_mode/library_versions_hash` 进入原子包 |
| M2 粒度 | 市场级（全市场） |
| M2 阈值策略 | v1.3.7 固定阈值；Optuna-like 仅“建议+审计”，不热切换 |
| M2 模板权重 | 使用 4.5 默认数值表（四层权重和为1） |
| turnover_zscore 聚合 | `turnover_zscore_mkt = median(z_i)`（可配置为成交额加权均值） |
| 流动性分层 | `ADV60` 三档分层，月初重算、月内冻结 |
| M10 阈值档位 | `normal/stress`，stress 由 `trend_down/extreme` 触发 |
| 数据延迟 SLO | 轻度延迟 `latency_watch`，严重/持续延迟进入 `limited_observability` |
| 延迟计算公式 | `data_latency_sec=max_i(now-available_time_i)`，并落库 `latency_by_input/latency_worst_input` |
| U 单位 | `bp`，动作接口 `raise_u_threshold_bp` 与其同量纲 |
| shadow 撮合 | 与实盘同撮合规则 |
| next_tradable_vwap | 评估窗口 `09:35-14:55`，不可交易顺延，最多 `8` 日搜索 |
| vwap 早盘敏感性 | shadow 并行 `open_window=09:35-10:30` 对照并产出 `execution_sensitivity_alert` |
| 价格序列口径 | `price_series_mode` 统一用于标签/评估/风险计算 |
| 分红口径绑定 | `dividend_treatment` 与 `price_series_mode` 强绑定（qfq/hfq 默认 implicit） |
| Entry/Exit no_fill | Entry 记 `R_net=0`；Exit 强制延后平仓，超期走强制清算 |
| 强制清算价时点 | `close_t` 固定为 `exit_signal_date + max_exit_carry_days` 对应收盘价（不可得时最多顺延 3 日） |
| 执行参数来源 | `limit_price/slippage/participation_cap/order_amount` 由 2.7 钉死并纳入 `execution_spec_hash` |
| 交易单位与残单 | `share_rounding_rule=lot_down_100`、`price_tick_rule=exchange_tick`、`min_notional_per_order=5000`、`residual_order_policy=day_cancel_then_recalc` |
| 冲击参数口径 | `impact_k/impact_eta` 钉死并纳入 `execution_spec_hash` |
| 交易档成交门槛 | `no_fill_ratio<=0.20`，`partial_fill_ratio<=0.35` |
| 容量约束口径 | 优先成交额参与率 |
| Universe As-Of | 每日冻结 `universe_snapshot_id`；默认剔除新股(<60日)/退市/长期停牌/ST |
| cap 再分配规则 | 先裁剪后按 `Tree->Online->Seq->Exp` 回填；cap 耗尽触发 champion 兜底 |
| Tier-B 晋升落地 | `healthy` 下切换 `promoted_healthy`，`cap_w_online` 提升至 `0.60` |
| Tier-B 撤销机制 | 超过 `demotion_tolerance` 自动撤销晋升并回退 `healthy` 模板 |
| 重大降级联动锁 | M2 触发 `trend_down/extreme` 时在线更新静默 5 日 |
| 自学习节奏 | 每日增量 + 每周 challenger + 每月序列刷新 |
| 周更衔接策略 | `rebase_then_replay`（重训替换后重放近 5 日成熟样本） |
| 周更差异审计 | 记录 `replay_diff_p_meta_p50/p90/max` 与 `replay_diff_turnover` |
| 自反馈学习策略 | 超阈样本降权学习（非丢弃）+ 在线权重抑制 |
| 自反馈在影子盘 | 默认跳过抑制判定，仅实盘/模拟盘成交回放模式生效 |
| 映射样本门禁 | `min_samples_per_bucket=300`，不足按层级回退 |
| 稀疏样本映射 | `sparse_history_flag=true` 时禁用最细桶，起点回退到 `regime x liquidity` |
| 映射防抖 | `mapping_update_cooldown_days=3` + `mapping_ema_alpha=0.30` |
| 波动率分层 | `volatility_tier` 基于 `mkt_volatility` 三档（low/mid/high） |
| 配仓风险口径 | 默认 `risk_i=std(20d)`，使用 `risk_floor=0.005` |
| 动态K映射 | `alpha = clip(1-constraint_pressure, 0.4, 1.0)` |
| 恢复窗口口径 | `recover_eval_window_days=5`，`N_recover=3` |
| 在线样本顺序 | `partial_fit` 按 `label_mature_time,trade_date,symbol` 升序，记录 `online_samples_used_hash` |
| Precision@K 评估 | 固定 `K=K_base=20`，不受 `K_dynamic` 影响 |
| 运维处置 | 8.2 Runbook 固化“触发→动作→回滚→验证”流程 |
| 每日对账 | 8.3 固化 `target/filled/EOD` 差值分布、`filled_weight_i` 与 `position_drift_ratio` 公式 |
| 模型分层 | Tier-A 主线、Tier-B 在线、Tier-C 序列、Tier-D 实验 |
| 回滚流程 | 原子包不可变存储 + 指针切换回滚 |

---

## 12.1 推送与 Dashboard 协同口径（v1.3.7 补充）

1. 推送体系当前统一执行口径，以 `WeChat_Push_Spec_v1.2.md` 为准；`WeChat_Push_Spec_v1.0.md` 作为全景范围骨架，`WeChat_Push_Spec_v1.1.md` 作为体验优化依据。
2. 企业微信仍为 `P0/P1` 主动告警主通道；`P2/P3` 允许做摘要化、归档化或迁移至 `Dashboard` 展示。
3. `Dashboard` 作为低打扰运行下的本地控制台，负责承接详情查看、对账、确认、暂停/恢复开仓、手工设仓/平仓等低频人工操作，不替代关键告警推送。
4. 快捷操作能力仅允许在模拟盘模式启用；任何必须“立即知道”的风险，不允许只存在于 `Dashboard` 页面。
5. 当前本机 Docker 验证通过的控制台入口为 `http://127.0.0.1:8001/dashboard`；正式部署时以 `/dashboard` 路由和实际反向代理端口映射为准。
6. 上线前需确认三项：中文模板已生效、状态变化去重正常、`Dashboard` 页面与快捷对账入口可访问。

---

## 13. 审核签署区

- 技术负责人：`________________` 日期：`________`
- 风控负责人：`________________` 日期：`________`
- 量化负责人：`________________` 日期：`________`
- 运维负责人：`________________` 日期：`________`
- 结论：`Go / No-Go`
