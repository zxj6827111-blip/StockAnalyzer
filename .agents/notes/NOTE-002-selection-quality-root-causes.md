# NOTE-002 选股质量根因清单（已证实缺陷 / 假设 / 禁止的推断）















Status: Draft















As-of: 2026-10-12 @ HEAD `c58da46eaf18defed3adf2ac6ec89ad668dcbf66`（D24–D35 已对照当前工作树、NAS 采集和隔离研究库；宽池 7 个原始 `adj_factor` 缺行均经严格全天停牌证据判定为不适用，三组有效覆盖 100%，未合成因子；证券名称区间经官方证据复核后无缺公告日/晚公告/重叠；流通市值参考扩至 2026-09-30，限定范围 readiness 为 ready，但历史发布时点未认证；资金流 / 龙虎榜历史文件仍未证明 PIT；夜扫留档改动与字段 PIT 审计代码已测，真实五层留档仍为 0、271 个链路字段没有完整 PIT 证据；无合格候选，本轮未部署。）







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

2026-10-11 续接证据落点（D34）：

- `src/stock_analyzer/runtime/universe_candidate_selector.py::UniverseCandidateSelector._build_report`
- `src/stock_analyzer/runtime/service.py::_light_stage_from_snapshot` / `_deep_stage_from_snapshot`
- `src/stock_analyzer/research/night_scan_funnel_trace.py::build_night_scan_funnel_trace`
- `src/stock_analyzer/research/selection_funnel_view.py::_trace_layer_view`
- `tests/test_night_selection_evidence.py`

2026-10-12 字段 PIT 门证据落点（D35）：

- `src/stock_analyzer/research/tail_field_pit.py`
- `scripts/audit_tail_field_pit.py`
- `scripts/validate_tail_independent_confirmation.py`
- `src/stock_analyzer/research/tail_confirmatory.py`
- `tests/test_tail_field_pit.py` / `tests/test_audit_tail_field_pit_cli.py`
- `tests/test_tail_confirmatory.py` / `tests/test_tail_confirmatory_cli.py`
- `artifacts/research/tail_pit_quality_20261012_v1/field_pit_gap_report.json`
- `artifacts/research/tail_pit_quality_20261012_v1/verification_manifest_v4.json`
















- `src/stock_analyzer/contracts/trend_strategy.py` —— 契约与 `audit_strategy_contract_conflicts()`







- `src/stock_analyzer/research/trend_data_readiness.py`、`scripts/audit_trend_data_readiness.py`

- `src/stock_analyzer/research/float_cap_reference.py`、`scripts/load_float_market_cap_research.py` —— 独立市值参考的导入、对账和读取侧覆盖

- `scripts/reconcile_tail_security_status.py` —— hash-bound 官方证据复核与 canonical payload 生成







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

- `scripts/build_financial_pit_research_panel.py` / `tests/test_build_financial_pit_research_panel.py` —— 基于只读 `daily_bars` 与公告日期快照，生成不可覆盖的 PIT 财务研究侧车；异常报告期样本排除并记录

- `scripts/collect_tail_security_status.py` / `tests/test_collect_tail_security_status.py` —— stock_basic L/D/P 全范围 + `namechange(ts_code=...)` 逐证券、可断点续跑的状态历史采集；token 只从环境变量读入客户端，不输出或写入采集证据

- `scripts/audit_tail_pool_adj_factor_coverage.py` / `tests/test_audit_tail_pool_adj_factor_coverage.py` —— 按请求证券各自的最早入场日和 31 日因子回看、RAW 退出窗口及已证明上市日审计宽池因子覆盖；只读输入并拒绝覆盖证据输出

- `scripts/collect_adj_factor_history.py` / `tests/test_collect_adj_factor_history.py` —— 逐证券因子采集支持绑定日期、证券清单、交易日历和采集器哈希的原子 checkpoint；默认复用已响应证券，可显式重采不完整响应

- `scripts/sync_tail_adj_factor_history.py` / `tests/test_sync_tail_adj_factor_history.py` —— 导入校验每个证券的响应时间，并保留续跑批次的行级 `as_of`；只有报告与每行都一致时才接受单一批次时间

- `scripts/sync_tail_reference_data.py::security_status_frame_from_payload`、`src/stock_analyzer/research/trend_data_readiness.py::_audit_security_status_intervals`、`tests/test_trend_data_readiness.py` —— 将逐证券 namechange 覆盖状态落为研究库审计标记，缺证券或不完整响应 fail-closed
- `src/stock_analyzer/research/tail_reference_store.py`、`tests/test_tail_reference_store.py`、`tests/test_collect_tail_security_status.py` —— 持久化 `namechange.ann_date` 为独立 `announcement_date`，兼容迁移已有研究表并保留原始有效区间；PIT 审计同时报告公告日缺失、公告晚于生效日及区间重叠







- `docs/alpha_v2/M4H_Historical_Locked_OOS_Report.md` —— 锁定 OOS 上限参照








---

## D21（2026-10-09 新增，已证实）：按新概率排序在**独立历史段**上比同池容量基线低 5.44pp

| 编号 | 缺陷 / 事实 | 证据 | 状态与可用范围 |
| --- | --- | --- | --- |
| D21 | 2025 全市场段（242 决策日 / 72,600 请求 / 71,568 条成熟标签 / 成交率 0.8693 / 池内净盈利率 **0.4376**）第一次让 §4 的四折滚动验证**跑到测试段**：四折校准 raw AUC 0.6299/0.5087/0.5441/0.6190 全过方向门。判定**未通过**，三项门：处理臂净盈利率 0.4085 对基线 0.4629（**improvement −5.44pp**）、交易日分块 bootstrap CI [−11.16, +5.22] 跨 0、处理臂平均净收益 **−0.40%**（基线 +0.05%）。尾部 p05 −5.18% vs −5.33% ⇒ **没有**明显尾部恶化，失败不在这项 | 质量报告 §4.14.3 / §4.14.4；`artifacts/research/mw2025_{labels,validation_oos5,validation_first4,feature_direction}.json`；`scripts/validate_tail_selection_quality.py` 退出码 4 | **已证实（2025 段 / 全市场 / replayed_recompute）**。可用的读法是"损失不在排序层把对的排错，而在模型没有相对容量基线的增量"；**不可**读成"§4 已通"（未通过）、不可读成"命中率 0.60 成立"（0.60 仍是规则）、不可与 2026 段的数字互借样本量，影子仍是 0 完整交易日 / 0 笔观察成交。`first4` 臂仍卡校准（AUC 0.4718） |

单特征剖面（同一段，39 个 12 日窗）：只有 `gap_up_pct`（22/39 正向窗，校准 0.5249）与
`avg_turnover_20`（20/39，0.5224）方向过一半；`market_relative` / `trend_position` 两族
在 **2025 与 2026 两段独立历史上都系统性 < 0.5**。⇒ §3.2 的分组加减实验收敛为：
这四类里可预测尾盘 5 日净盈利的部分已被容量基线吃掉，继续调这四类的权重不构成对 §4 的可行路径。

| D22 | 生产链路"接通了"不等于"读得到"：夜扫前两层在 2026-10-09 首跑里缺席，成员清单其实正常产出，但放在 `source_report/prefilter/universe_quality_selection/hard_gate_membership`，而消费端只认另两条路径 ⇒ 读空、静默跳过。本地 13 条测试全绿是因为 **fixture 按消费端假设造，不是按生产形状造** | 质量报告 §4.15；`MEMBERSHIP_PATHS` / `locate_hard_gate_membership()` / `universe_membership_status()`；`tests/test_night_scan_funnel_trace.py`（16 条，其中 `_production_shaped_report()` 从生产 JSON 抄形状）；部署 `2b0ab09` 后用当晚生产报告重放验证五层齐全 | **已修（数据侧无改动，纯消费端）**。行为约束新增两条：① 命中路径必须写进留档 notes；② 两层没落档时 `record_night_scan` 必须回显原因，禁止只让人数 `layers`。以后新增"生产链路留档"类改动，验收必须包含一次**用生产真实报告形状**的重放，不能只跑自造 fixture |

| D23 | 真实数据入口仍有来源不明的默认值 / 时点不明的回填：provider / TDX 离线包曾将缺失市值、背景量写成常数；TDX 报告期股本与财务、AKShare 最新快照曾贴到历史行；市值还可能从相邻日期向后填入早期决策 | 本次工作树修改：`data/{tushare_provider,akshare_provider,efinance_provider,background_adapter}.py`、`scripts/build_tdx_offline_package.py`、`runtime/universe_candidate_selector.py`；相关决策见 ADR-004 / ADR-005 | **代码已改，证据未验收**：新记录中的缺失值保留为 NaN；未验证时点的 TDX 背景值不再进入历史评分；缺项不再合成背景净额；TDX 缺失 OHLCV 不补零；TDX 报告期股本因缺公告有效时点而不再派生历史市值；缺公告有效时点的财务字段停用；市值硬门只读当日观测。尚未重建任何研究样本；`tail_rebuild` 的退出仍使用日线 OHLC，完整 RAW 分钟退出、公司行动账本、历史证券状态 PIT 与逐字段来源证明未完成。未跑测试、未部署；不得把旧样本判作已修复或产生新质量结论 |

| D24 | CSV 中的六位证券代码曾被 pandas 推断成整数：`000001` 变成 `1`，导致 adj_factor 研究 join 少掉 115 只请求股票的历史因子；同类风险也适用于带数值裸码的离线源 | `upsert_adj_factors()` 现在优先用保留交易所后缀的 `ts_code` 规范化；同步 CLI 明确把 `symbol` / `ts_code` / `trade_date` 读成字符串；`test_adjustment_factor_prefers_qualified_symbol_to_preserve_leading_zeroes` | **修复已测、2026 子集已重建**：已按原文件 SHA-256 `73fb3696…8e7336` 重导入 78,622 个因子行；正确的排序 v4 子库已生成并用于 2026 RAW 标签重建。v2 不得用于收益结论；修复前后报告不能拼接 |

### D24 后续核实与 D25–D27（2026-10-10）

| 编号 | 缺陷 / 事实 | 证据 | 状态与可用范围 |
| --- | --- | --- | --- |
| D24 follow-up | 2026 v4 曾有 136 个请求被 62 个 symbol-day 日线缺口阻塞；缺口日中 57 天只有 241 根零成交量占位、5 天没有分钟记录，不能据此猜交易状态。v9 通过经过核实的来源修复后重建 28,200 条请求，0 个参考缺口、19,982 条成熟可训练；仍有 17 条停牌/分钟冲突保持不确定。 | `tail_rebuild_raw_v4_report.json`、`tail_rebuild_reference_gaps_raw_v4.jsonl`、`tail_rebuild_raw_v9_report.json`、`tail_rebuild_subset_2026jan_may_v5_minute_markers_manifest.json`；`scripts/augment_tail_daily_refs_from_minutes.py` | 旧 v4 是历史阻塞实测，当前 v9 的参考数据门已通过；0 个参考缺口不是收益验收通过。 |
| D25 | 先前引用的 2025 全市场质量数字来自当前 RAW 分钟 / 公司行动账本修复之前的旧标签版本，不能作为本轮质量证据。2025 因子现已采集并导入 317,210 个逐证券日行；依据上市日为 688411 解释其上市前缺失前缀，其上市后日期逐日完整。修复 1,032 个 v1 缺口后，全量 v2 重建有 72,600 条标签、0 个参考缺口、62,941 条成熟可训练；51 条行动/退出/停牌冲突仍不确定。 | `tail_rebuild_2025_factor_coverage_audit.json`（历史基线）、`mw2025_adj_factor_collection_v2/import_report.json`、`tail_rebuild_raw_2025_v1_report.json`、`tail_rebuild_raw_2025_v2_report.json`、`tail_rebuild_raw_2025_repair_v3_report.json`；官方停牌证据目录 `official_suspension_evidence_688766_20251125_20251209/` | D21 的旧读数仅留历史诊断；新 RAW 标签可用于开发，但 2025 已查看，不能用于独立确认。未成交与不确定记录不得当亏损或盈利。 |
| D26 | 2026 v9 的 12 臂仍无合格候选。2025 新 RAW 标签上的 12 臂也无候选：9 个价格臂首个校准窗 raw AUC < 0.5 而停；唯一跑完四折的 LR 臂至少一折没有成熟候选成交；2 个 `moneyflow` / `top_list` 臂因 PIT 可用时点未证明被阻塞。 | `tail_stage2_ablation_raw_v9.json`、`tail_stage2_ablation_raw_2025_v2.json`；242 个 2025 决策日；`candidate_selection=no_qualified_candidate` | **没有合格候选**。数据完整、四折训练或 LightGBM 可用都不单独表示质量通过。候选冻结后的三对照确认尚不能启动；已查看的 2025 / 2026 数据不能改称独立确认集，未来影子仍需真实观察。 |
| D27 | 本地只读市场库中的历史 `roe` / `debt_ratio` 随日线行重复了未来财报快照，不能直接用于历史硬门或模型；证券状态名称历史全市场查询被 10,000 行上限截断。 | `scripts/build_financial_pit_research_panel.py`；`artifacts/research/financial_pit_2025_panel_20261010.duckdb`、`financial_pit_2026_panel_20261010.duckdb`；`artifacts/research/tail_data_readiness_full_market_20261010.json`；`scripts/collect_tail_security_status.py` | **局部 PIT 证据已补，完整 PIT 仍不足**：2025/2026 侧车分别有 1,245,745 / 949,026 个唯一证券日键，未来公告日和未来报告期均为 0；分别有 14,693 / 10,970 条同日公告行只具日期精度。2025 有 496、2026 有 98 行无可用快照；1 条 `end_date > ann_date` 的异常快照已从可用 join 排除并留证。全市场就绪报告仍为 `insufficient`：流通市值 2,611 个交易日中 80 天众数占比超过 50%，证券状态仅 83.713% 的行标为完整。逐证券采集器和 fail-closed 覆盖标记已在工作树实现并通过定向测试，但未能执行 Tushare 请求或更新研究库；不得将代码入口称为已补齐数据。 |
| D28 | 宽池月度请求集的 RAW 标签输入被历史 `adj_factor` 覆盖不足阻塞；覆盖必须按每只证券自己的最早请求日回看 31 个自然日，不能把全组最早日期误套到所有证券。 | `scripts/audit_tail_pool_adj_factor_coverage.py`、`tests/test_audit_tail_pool_adj_factor_coverage.py`；`scripts/collect_adj_factor_history.py`、`tests/test_collect_adj_factor_history.py`；`scripts/sync_tail_adj_factor_history.py`、`tests/test_sync_tail_adj_factor_history.py`；`artifacts/research/tail_pool_adj_factor_audit_20261010_v3/tail_pool_adj_factor_coverage_report.json`、逐证券 CSV、采集 symbol/request/scope manifest；输入请求文件 SHA 与研究库大小、mtime 均记录在报告 | **已完成只读覆盖审计、采集输入准备和可续跑代码，真实采集未执行**：修正后的 2026 v4 因子子库每月仅有 698 只证券在各自 RAW 标签窗口内完整；3 月 3,897 只请求中 3,199 只不完整（18.4939% symbol-session 覆盖），4 月 3,768 中 3,070 只（19.1297%），5 月 3,705 中 3,007 只（19.2869%）；缺失集合并集为 3,526 只。合并清单含 157,075 条原始请求记录、覆盖 2026-01-31 至 2026-06-24；采集 checkpoint 校验请求范围、证券集合、日历与采集器 SHA，可在中断后复用已响应证券并显式重试不完整响应；hash-bound 导入按证券响应时间保留 `as_of`，避免续跑时伪造采集时间。旧版全组起始日审计输出已被 v2/v3 取代，不用于计数。Tushare 采集仍需可用 token；没有新增因子、RAW 标签或消融结果，阶段二宽池逐层消融仍阻塞。 |
| D29 | 2025 `moneyflow` / `top_list` 原始文件有全年度日期覆盖，但文件覆盖不等于决策时点可用；龙虎榜同一股票日因多个上榜理由重复。 | `scripts/audit_tail_event_feature_provenance.py`、`tests/test_audit_tail_event_feature_provenance.py`；`artifacts/research/tail_event_pit_provenance_audit_20261011_v3.json`（含 2025 交易日历 SHA）；Tushare 官方接口文档：[`moneyflow`](https://tushare.pro/document/2?doc_id=170)、[`top_list`](https://tushare.pro/document/2?doc_id=106) | **正式阶段二事件臂仍阻塞**：交易日历 2025 全年 243 日与两个文件的日期集合完全相等；资金流 1,248,109 行、日期/代码键唯一；龙虎榜 18,033 行中有 1,616 个重复股票日键（1,681 条多余行，其中 1,609 个键包含多个不同理由、7 个键是同一理由重复）。两份采集报告均无失败、空日或报告的截断日，但没有逐日采集时间、逐响应哈希或历史源版本 / 发布截止证据；资金流缺行语义也未证明，不能填零。龙虎榜计数必须按唯一股票日计算。事后取得的当前历史值不能靠 `T-1` 滞后恢复其 2025 年决策时点版本。 |
| D30 | 宽池 `adj_factor` 的 7 个证券日缺口是否来自凭证、逐证券接口或全市场响应截断。 | `scripts/collect_adj_factor_history.py`、`scripts/audit_tail_pool_adj_factor_coverage.py`；`artifacts/research/tail_pool_adj_factor_audit_20261011_v1/collection_attempt_missing7_v1/collection_evidence_manifest.json`、`collection_attempt_missing7_trade_date_20261011_v1/trade_date_probe_evidence_manifest.json`；Tushare [`adj_factor`](https://tushare.pro/document/2?doc_id=28) | **已排除凭证 / 全市场截断原因；原始缺行性质由 D33 续核**：3,526 证券逐证券响应中 3,519 完整、7 只都仅缺 2026-02-03；完整全市场日响应有 5,465 只证券、无截断或接口错误，但仍无这 7 只记录。7 只均有完整全天停牌状态，当日没有 RAW 日线，公司行动日覆盖完整且无事件，2 月 2 日与 4 日因子值相同。此项保留的是原始来源核验事实；适用性结论见 D33。 |
| D31 | 全市场证券状态采集行数完整，但 Tushare `namechange.ann_date` 原先未落库，导入后又发现有效区间重叠。 | `scripts/sync_tail_reference_data.py`、`scripts/reconcile_tail_security_status.py`、`src/stock_analyzer/research/tail_reference_store.py`、`src/stock_analyzer/research/trend_data_readiness.py`；`tests/test_reconcile_tail_security_status.py`；`artifacts/research/tail_nas_collection_20261011_v1/security_status_namechange_v1/official_namechange_evidence_manifest.json`、`reconciled_v1/security_status_reconciliation_review_v1.json`、`reconciled_v1/security_status_reconciliation_report.json`；v6 独立库 | **PIT 区间问题已按证据解决**：官方公告及证据 SHA-256 绑定到 7 项复核决定；移除 6 条被证明为拟用名/非股票简称重复事件的重叠行，补入 689009.SH 公告日 2020-10-28。原始 payload/v5 均不变；v6 有 26,044 行、5,912 只证券，覆盖标记 100%，缺公告日/晚公告/重叠均为 0；完整 readiness 中 `security_status_intervals=ok`。 |
| D32 | `daily_bars.float_market_cap` 中历史提供方兜底常数会让市值门失去横截面判别力；原始列必须留证，独立参考值应按 symbol-day 解释。 | `src/stock_analyzer/research/float_cap_reference.py`、`scripts/collect_float_market_cap_history.py`、`scripts/load_float_market_cap_research.py`、`src/stock_analyzer/research/trend_data_readiness.py`、`scripts/audit_trend_data_readiness.py`；`artifacts/research/float_cap_reference_20261011_v2/float_cap_collection_manifest.json`、`trend_data_readiness_post_cap_reference.json` | **集中度数据缺口已补、审计已接入；市值 PIT 与生产接入未认证**：NAS Tushare `daily_basic.circ_mv` 补采 2026-07-18..09-30，52 日 / 288,177 行，无失败、空值或截断。只在原参考库克隆 v2 追加，覆盖 2025-01-02..2026-09-30 共 2,310,122 行；与 2026-07..09 仓库可对账 281,798 个证券日，非占位行逐月一致率均为 100%。只读 readiness 按匹配的 symbol-day 覆盖后，2,611 个日线交易日中市值众数超 50% 的天数为 0、无阻塞/不足项；原始 `daily_bars` 与原参考库未变。历史 endpoint 查询没有逐日发布快照，不能据此认证 14:30–14:50 的特征可用性，也不能把 readiness `ready` 当作收益验收通过。 |
| D33 | `adj_factor` 采集把区间级不完整状态传播到该证券区间内每条有效观测；同时，7 个真实缺失日是否可按有证据的无交易停牌日豁免尚未核实。 | `scripts/collect_adj_factor_history.py`、`scripts/sync_tail_adj_factor_history.py`、`scripts/reconcile_tail_adj_factor_observation_rows.py`、`scripts/audit_tail_pool_adj_factor_coverage.py`、`src/stock_analyzer/research/suspension_evidence.py`、`src/stock_analyzer/research/tail_reference_store.py`；`tests/test_collect_adj_factor_history.py`、`tests/test_sync_tail_adj_factor_history.py`、`tests/test_reconcile_tail_adj_factor_observation_rows.py`、`tests/test_audit_tail_pool_adj_factor_coverage.py`、`tests/test_suspension_evidence.py`、`tests/test_tail_reference_store.py`；`artifacts/research/tail_adj_factor_row_coverage_20261011_v1/` | **代码与限定请求范围审计已修复**：修正行级/区间级 coverage 语义；仅在隔离库把 637 条已存在且源响应 SHA 一致的观测标为行级有效，不增删因子值，7 个证券区间仍标不完整。7 个原始缺行均同时满足全天停牌、无 RAW 日线/分钟、公司行动覆盖完整且无行动、前后有效因子相等；规则只标明当天“不适用”，不合成因子。三组请求有效覆盖为 100%；3 月原始缺行 7 / 豁免 7 / 未解决 0，4 月和 5 月原始缺行均为 0。纠正后报告 SHA-256 `9f6bc0db…1a8bb469`。其他数据范围的收益标签仍须按该规则重建和核验；2026 阶段二仍因 58 个决策日短于 71 日、PIT 缺证而无候选。 |


### D34（2026-10-11 续接，已证实）：完整输入池与部分归因的可见性

| 编号 | 缺陷 / 事实 | 证据 | 状态与可用范围 |
| --- | --- | --- | --- |
| D34 | Quality 留档从已经入选的成员起算，漏掉硬门合格池到 Quality 的截断；Light/Deep 只记成员和部分计数；视图只要有一条真实原因，就把混有未归因记录的整层误标为原因齐全。 | `runtime/universe_candidate_selector.py`、`runtime/service.py`、`research/night_scan_funnel_trace.py`、`research/selection_funnel_view.py`（均在 `src/stock_analyzer/` 下）；`tests/test_night_selection_evidence.py`；ADR-004 §9；`artifacts/research/night_funnel_continuation_20261011_v1/` | 本轮代码与 119 个定向测试完成，未部署。筛选器同期输出版本化输入、逐证券原因与排序证据；消费者拒绝冲突/重复/非法载荷，旧报告和部分缺口继续保持未归因；模型降级 0.5 不冒充真实预测。NAS 仅有 2026-10-09 三层留档，真实五层有效观察日仍为 0。D11/D14 的历史证据未改写，完整生产归因、逐层质量消融和稳定观察期仍未验收；没有因此产生模型候选或收益通过结论。 |

### D35（2026-10-12 新增，已证实）：独立确认入口原先缺少全链路字段 PIT 强制门

| 编号 | 缺陷 / 事实 | 证据 | 状态与可用范围 |
| --- | --- | --- | --- |
| D35 | 独立确认统计入口原先能依据日历、折数、样本数及人的未开封声明计算 `release_eligible`，但不检查每个 hard gate、行情/事件/旧版特征字段的来源、覆盖与决策截止时点；统计函数结果可能被误读成全链路 PIT 已验证。 | `src/stock_analyzer/research/tail_confirmatory.py`、`src/stock_analyzer/research/tail_field_pit.py`、`scripts/validate_tail_independent_confirmation.py`、`scripts/audit_tail_field_pit.py`；四个定向测试文件；`artifacts/research/tail_pit_quality_20261012_v1/field_pit_gap_report.json`、`verification_manifest_v4.json` | **代码门已修、实际 PIT 仍阻塞**：全链路清单由当前代码生成 271 个字段，涵盖资格硬门、趋势/事件特征、旧版模型输出与原始快照；独立确认 CLI 要求输入 SHA、逐日逐臂观察快照、字段行数、零缺失/零未知填补、来源 SHA/版本及带时区的可用/采集/计算时钟，并拒绝重复 JSON 键。统计函数单独永不授予发布资格。20 个定向测试、Ruff 和四文件 mypy 通过；全仓研究工件没有符合该版本格式的字段级证据，故清单报告将 271 个字段全部 blocked。SHA 不等于供应商签名或可信时间戳；历史重新查询不能补造历史 PIT。无候选，独立确认仍未启动，本轮未部署。 |
