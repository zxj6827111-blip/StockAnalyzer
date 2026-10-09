# NOTE-002 选股质量根因清单（已证实缺陷 / 假设 / 禁止的推断）















Status: Draft















As-of: 2026-10-09 @ 3522cf8（D15 补采真值、D16 覆盖面是调用参数、**D17 根因更正为"同步输入用了池内 CSV"并已数据侧修复**（本地全市场 CSV 一直在，远端补采 201,893 行与它差集为 0 对）、D18 层消融跨月翻符号（条件性）、D19 relative_strength 与 rank_ret_20 是同一个秩（ρ=1.0）、D20 这层截断偏向已大涨与高波动（成立），偏向难成交（不成立，方向相反）；**2025 全市场重验证窗口已打开**（分钟 RAW 口径经 vendor 日线逐分对照证明、市值占位常量污染范围收窄到 2026 年、第五类信息 moneyflow/top_list 已按全市场符号面补采）；§2 特征判别力读数已按 --rolling/--walk 三次更正。台账 docs/selection_quality_plan_status.md §0 是当前交接面）







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







| D16 | 研究侧「数据不够」这个结论有三次其实是我自己设的符号上限：分钟库只灌了 208 只、参考库 `ref_daily_bars_raw` 也只灌了 208 只，而 vendor 包本身是全市场的（2026-04 每个交易日 5,194 个逐票 CSV；日线包重灌后 718,796 行 / 5,190 只）。根因都是同一句：ingest/sync 脚本传了 `--symbols-file artifacts/research/tail_symbols.txt`（那份 900 只清单，仓库里没有产生它的代码） | 质量报告 §3l / §3n.1；`scripts/sync_tail_minute_bars.py`、`scripts/sync_tail_reference_data.py` 的 `--symbols/--symbols-file` 参数 | **已证实并已修**：四月分钟 bar 全市场重灌 26,305,150 行 / 109,150 个完整尾盘 symbol-day；日线参考重灌 718,796 行 / 5,190 只、`gaps=[]`。修好后 §3n 那 10,828 条缺 `entry_daily_bar` 的判不动样本才可能出标签。**教训**：说「数据不足」之前先证明限制来自数据源而不是调用参数，否则会把工程缺陷记成数据缺口并白白停摆 |







| D17 | 研究库 `ref_limit_prices` 的覆盖面曾在 6/7 月塌方（7 月只剩 4,793 行 / 209 个符号，而 `ref_daily_bars_raw` 同期 5,176 只）。**根因判定已更正**：不是"本地没有涨跌停数据、只能远端补"，而是上一次同步把 `stk_limit_2026_pool.csv`（209 只）当成了输入，而全市场那份 `stk_limit_2026.csv`（776,108 行 / 5,633 只 / 2026-01..07）**一直躺在本地** —— 与 D16 同一形状的错误 | 质量报告 §4.14；`scripts/sync_tail_reference_data.py --limit-prices-csv`；`scripts/collect_stk_limit_history.py`（新，tushare `stk_limit` doc_id=183 的正规采集入口）；`artifacts/research/sync_d17_fix.json` | **数据侧已修并交叉验证**：重刷后 6 月 117,724 行 / 5,613 只、7 月 129,003 行 / 5,621 只，Jun/Jul 每日最少 5,603 只，`missing_sources=[]`。远端独立补采 2026-06-11..07-31 得 201,893 行，与本地 CSV 的 `(trade_date,ts_code)` 差集为 **0 对** ⇒ 本地覆盖本来就是全的。**尚未**跟着重跑 2026 的 124 决策日标签（`entry_day_limit_prices` 阻塞 5,464 条这个读数因此还是修复前的口径，不得当成已消除）；补采前那些日子的确认判定不得产出标签，也不得用开盘回测顶替 —— 这条约束不变 |







| D18 | §2「前置筛选过早淘汰」的答案是**条件性的、跨月翻符号**：同一口径下把容量截断从 300 放宽到 3,400，4 月保留组净盈利率高 4.38pp（CI [−7.38, −1.29]）、5 月保留组高 6.32pp （CI [−12.05, −0.75]），但 **3 月淘汰组反而高 4.65pp（CI [+0.56, +8.81]）** —— 过早淘汰确实发生过，但不是这层的稳定属性 | 质量报告 §4.9 / §4.10；`scripts/replay_tail_candidate_pool.py --pool-size`；`artifacts/research/ablate_pool_{apr,mar,may}_*` | **已证实（3 个月 / 池子级）**：这层截断的质量效应方向不稳定 ⇒ 不能主张「一律放宽候选面」，也不能主张「截断在伤害选股」。要动 pool-size 需跨制度段证据 + 用户授权。口径纠正仍然有效：「79% 合格候选不在清单」量的是覆盖面，不是质量损失 |



| D19 | §2「旧模型/综合分/等级是否反复使用相同信息」第一次拿到硬证据：124 决策日全市场样本上，`relative_strength` 与 `rank_ret_20` 的 Spearman ρ=**1.0**（横截面上是同一个排序）却登记在**不同信息组**；`rs_ma20`/`ma20`=0.9994、`rs_ma5`/`ma5`=0.9991、`ma5`/`ma10`=0.9978，达阈值的冗余对共 22 对。四组列级时间外 AUC 均值仅 0.507~0.5141（AUC>0.5 占比 0.51~0.67） | `scripts/measure_tail_feature_stability.py`（既有脚本，未新写代码）→ `artifacts/research/mw_feature_stability_125d.json`；质量报告 §4.11 | **已证实**：「有四类独立证据」这个印象是虚的，同时使用 ρ=1.0 的两列等于把同一份信息计两次。它不证明任何一列能提升净盈利率（秩相关是冗余度量，列级 AUC 是测量不是准入） |

| D20 | §2「是否偏向已大幅上涨/波动过高/成交困难的股票」量化答案：**偏向大涨与高波动成立，偏向难成交不成立（方向相反）**。4 月同一横截面上保留组（6,000 symbol-day）与淘汰组（62,000）比：

`excess_ret_60` +12.21% vs +4.66%、`close_to_ma60` +3.95% vs −0.48%（中位 −3.25%）、`range_position_60` 0.4565 vs 0.3838、`realized_vol_20` 0.6257 vs 0.5115、`atr14_pct` 5.03% vs 4.07%、`avg_turnover_20` 6.26e10 vs 1.10e9（57 倍）、`amount_to_float_cap` 中位 2.796 vs 0.0289（约 97 倍） | `scripts/replay_tail_candidate_pool.py --pool-size` 消融输入对比；`artifacts/research/ablate_pool_apr_requests.jsonl` × `mw_apr_requests.jsonl`；质量报告 §4.12 | **已证实**：这层偏好高波动/已上涨，而 §4.7.1 显示这两列无跨段稳定方向 ⇒ 是风险敞口不是预测力；容量维度是**可成交性约束**（保留），大涨/高波动应作**风险减分**——这属策略形态判断，待用户决策，本轮未改 |















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







- **排序层的损失第一次被单独量出来，而且赢的不是趋势信息**（最后 40 个决策日 2026-05-20..07-16，窗口内池子净盈利率仅 0.2961；`scripts/measure_tail_selection_ceiling.py` → `artifacts/research/mw_selection_ceiling_40d.json`）：按 `turnover` 降序每日前 3 → 0.4250（Δ **+12.88pp**，交易日分块 CI [2.889, 23.214]）、`avg_turnover_20` 降序 → 0.4083（Δ +11.23pp，CI [0.714, 21.733]）、`gap_up_pct` 升序 → 0.4417（Δ +14.57pp，CI [5.457, 23.413]）；而趋势位置/市场相对强弱各臂 Δ +2.9～+6.1pp、CI 下界全部 ≤0。覆盖率不是约束（各臂 coverage=1.0、3 个名额都填得上），平均净收益只勉强为正（+0.05%～+0.28%/笔），p05 与最差笔贴着 −5% 止损位。







  → **这条支持已经在 §4.7 的多段预登记口径下整体撤回**（6 段、每段方向只由该段之前样本决定：没有任何一臂 CI 下界为正，`avg_turnover_20` 合并只剩 +1.54pp、CI [−3.952, 7.139]、正向 3/6 段）（22 臂里只有 `avg_turnover_20` 降序活下来：Δ +11.23pp、CI 下界 +0.714pp>0；`turnover` 与 `gap_up_pct` 两臂预登记后分别变成 −4.69pp 与 +4.41pp/下界≤0）；但这不是 §4 验收：容量与硬门流动性下限同源、赢的是可成交性不是预测力，`gap_up_pct` 那臂还是 22 个臂里的方向二次选择（只能记成跳空高开=风险信号的假设），多重比较未校正。读数与限制见质量报告 §4.4。







- **四类已有信息没有任何一列给出可用的稳定排序方向**（6 段 × 16 决策日的预登记 walk-forward，`artifacts/research/mw_selection_ceiling_walk.json`）：方向在中途翻号（`gap_up_pct` a/a/a/d/d/d、`avg_turnover_20` a/a/d/d/d/d、`close_to_ma20` d/a/a/a/d/d、`relative_strength` d/d/d/a/d/a），或方向恒定却多数段跑输池子（`atr14_pct` 恒 asc、6/6 段 Δ<0、合并 −11.45pp；`realized_vol_20` 恒 asc、4/6 段为负）。没有任何一臂的交易日分块 CI 下界为正。







  → 对 §2「是否偏向已经大幅上涨、波动过高或成交困难的股票」的诚实回答是：**现有样本既不能判它加分也不能判它减分**。我先前写「高波动/高跳空有害」，那句按 §4.7.1 的方向表已收回——那些段实际取的是低值。要主张它，必须用从未参与方向选择的历史（2024/2025）单独复验。







- **项目自己的锁定 OOS 上限**：绝对命中率 43–45%、TopK 净收益为负







  （`docs/alpha_v2/M4H_Historical_Locked_OOS_Report.md:206,591`）。







  → 这是新概率的**现实参照**：0.60 阈值只是选股规则，不是已证明的命中率。







- **全市场合格池上，第一轮四类信息的单特征时间外判别力是分层的不均匀的**（91 个决策日 /







  19,550 笔成熟模拟成交，`scripts/measure_tail_feature_direction.py` →







  `artifacts/research/mw_feature_direction_91d.json`，最后 15 日为时间外段）：







  趋势位置 `range_position_60`=0.6082、`close_to_ma20`=0.5930、`ma20_slope`=0.5717；







  市场相对强弱 `excess_ret_20`=0.5600、`relative_strength`=0.5541；







  而量价/流动性 `avg_turnover_20`=**0.4672** 与波动/过热 `atr14_pct`=**0.4601** 是**反号**的







  （`realized_vol_20`=0.4782 同向）。







  → **上一版这条的结论已经不够了**：把窗口从 91 个决策日扩到 124 个（37,200 请求 / 31,599 标签 /







    21,760 笔成交退出 / 成交后净盈利率 0.4053）再量一次，`--rolling` 剖面显示







    **11 条特征里没有一条能在多数窗上站稳方向**（正向窗 9–12 / 19≈掷硬币，同一条特征 AUC 从







    0.3753 摆到 0.6321），且两种特征集在第 1 折校准段的读数与 91 日**逐位相同**







    （0.4696 / 0.4720）——滚动折永远从最早那段开始校准，往尾部追加月份改不了它。







  → 已证实的是：**2026 上半年这四类已有信息在全市场合格池上方向本身不稳定**，







    不是"两条反号列拖累了线性模型"。`validate_tail_selection_quality.py` 四种组合因此在







    第 1 折校准段全部 fail-closed（raw AUC 0.4384 / 0.4691 / 0.4696 / 0.4720，退出码 5），







    四折测试段根本没跑，±5pp 与交易日分块 bootstrap **没有数**。







  → **还不足以定性**：这解释的是"为什么净盈利率改善没有数"，不是"净盈利率不会改善"。







    单特征 AUC 的分段口径与 walk-forward 折定义不同，且成交率 0.7190 / 成交后净盈利率 0.4121







    仍只是 `replayed_recompute` 总体，不能当 observed 命中率。







    台账与读数见 `docs/trend_tail_selection_quality_report.md` §4.1。















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







- `src/stock_analyzer/research/float_cap_reference.py`、`scripts/collect_float_market_cap_history.py`、`scripts/load_float_market_cap_research.py` —— D15 的真值补采与读取侧接线







- `scripts/sync_tail_minute_bars.py`、`scripts/sync_tail_reference_data.py` 的 `--symbols/--symbols-file` —— D16：覆盖面由调用参数决定，报告必须写清用了哪份清单







- `scripts/measure_tail_feature_direction.py` + `artifacts/research/mw_feature_direction_91d.json` / `mw_feature_direction_125d_rolling.json` —— §2 特征判别力读数的唯一生产者（只读，含 --rolling 剖面）







- `scripts/sync_tail_reference_data.py` 的 `--limit-prices-csv` —— D17：精确涨跌停的取值来源是 tushare `stk_limit`（doc_id=183），**但塌方的原因是这里传了池内那份 CSV**。教训和普通近似值禁令并列：采集面塌了先查**传进去的是哪份文件**，再谈远端补采；不能用开盘价近似
- `scripts/collect_stk_limit_history.py` + `scripts/collect_market_events_history.py` —— 在生产容器内按交易日补采精确涨跌停 / 第五类信息（`moneyflow`、`top_list`）的两个入口；口径与市值补采器一致（token 只读变量名、正好 10,000 行按截断处理、失败日子非零退出、`--days` 与 `--start/--end` 两条腿至少有一条说得通），钉在 `tests/test_collect_stk_limit_history.py`（9 条）与 `tests/test_collect_market_events_history.py`（8 条）







- `scripts/measure_tail_selection_ceiling.py` + `artifacts/research/mw_selection_ceiling_40d.json` —— §2 排序层的唯一生产者（每日前 3、六项指标、交易日分块 bootstrap）







- `src/stock_analyzer/research/tail_walk_forward.py` 的校准段方向门（`calibration window direction is not positive`）—— 四种特征组合全部在这里 fail-closed，四折测试段不产出数字







- `runtime/universe_candidate_selector.py::_gate_membership`、`research/night_scan_funnel_trace.py::live_universe_facts` —— 生产夜扫前两层留档







- `docs/alpha_v2/M4H_Historical_Locked_OOS_Report.md` —— 锁定 OOS 上限参照







