# 选股质量改进计划：交付状态与证据台账

As-of: 2026-10-12（§0–§9 保留原时间点记录；数据证据见 §10–§14；系统错误后的续接见 §15，本轮最新工程验证及尚未完成任务见 §16）

历史状态（2026-10-09）：分支 `feat/stock-selection-quality-overhaul` 已推送（到 `3522cf8`）
（PR #105 OPEN。故意不把 As-of 写成单个 SHA：那会让这条事实每次改文档都过期一次。
本轮另把该分支经 `scripts/nas_deploy_update.sh --branch` 向生产做**快进部署**——
生产镜像原提交 `1eda512` 是本分支祖先，不是分叉）

每条都按仓库规约的四级状态标注（AGENTS.md §9.1）：
**代码完成 ≠ 测试完成 ≠ Freeze Ready ≠ Production Ready**。
"证据"列只写仓库里真的存在、能复查的东西（模块 / CLI / 测试名 / 文档）。

---

## 0. 交接：2026-10-09 这一轮把三件事从"只能远端"变成"已经落地"

### 2026-10-09 工作树增量（未提交、未部署、未运行测试）

本轮已改代码，但没有重建研究库或产生新的收益证据。该状态优先于下方以 2026-10-09
为时间点的历史台账，不改写此前的实测结论：

| 项目 | 工作树改动 | 当前状态与阻塞 |
| --- | --- | --- |
| 验证统计 | `tail_net_profit_trainer.py` 使用真实折 / session 身份做配对移动块 bootstrap（主块 10 日、敏感性 5/20 日、20,000 次、PCG64、seed 20261010）；样本门要求 120 个配对测试日、每折 30 日、每臂 300 笔成熟成交；两臂缺失 outcome 不能静默丢弃。样本拼接与验证拒绝重复决策键；每次验证只接受一种 `capture_mode` | 代码未测试。仍是单候选 + 单同池旧排序开发比较；报告硬标 `release_eligible=false`。三对照、现金/持仓账本、80% 功效分析、8 项确认检验和独立窗口均未实现 |
| 交易日与输入证据 | 验证器强制输入开放交易日历 CSV，缺 `is_open` 或无效日期即拒绝；缺候选日作为显式零推荐；样本与日历的 SHA-256 写入报告 | 日历文件本身仍需调用方提供并核实来源、覆盖区间；未运行历史重验证 |
| 数据缺失 / PIT | provider 与 TDX 离线包不再为缺失市值、未取数背景量填入固定常数；未验证时点的 TDX 背景值不再回填到历史日；背景净额缺任一组成项不产部分合计；TDX 缺失 OHLCV 保留缺失；TDX 报告期股本缺公告有效时间，不再用于派生历史市值；无公告可用时间的 TDX / AKShare 历史财务快照不再作为 PIT 财务值；市值硬门消费只读同日观测，缺失走现有 `invalid_float_market_cap` | 代码未测试，未重建标签。全量证券状态 PIT、字段级来源 / 可用时刻记录仍未完成；其它遗留评分路径仍有派生市值兜底 |
| 标签退出 | 本轮未改 `tail_rebuild` 的出场算法；现状仍由日线 OHLC 判定止盈止损，研究库没有已接线的公司行动现金 / 股数账本 | **阶段一仍未完成**。RAW 分钟退出、公司行动数据源、整手 / 现金变化和实际退出时刻须先实现并核实；此前标签不能当成已修复的新证据 |

本轮依据见新建的 ADR-005 与更新后的 ADR-004 / NOTE-002。用户要求另一个会话验收；本轮没有执行测试或 NAS 操作，也没有提交或部署。

| 还差什么 | 卡在哪 | 现在的事实与下一步 |
| --- | --- | --- |
| §4 选股质量验收（≥4 折、+5pp 且分块 CI 下界>0、平均净收益为正、尾部不明显恶化） | **已完整测量、未通过**（不再是"跑不进测试段"）：2025 全市场段 242 决策日 / 72,600 请求 / **71,568 条成熟标签**（成交率 0.8693、池内净盈利率 **0.4376**），四折校准 raw AUC 0.6299/0.5087/0.5441/0.6190 **全部过方向门并进入测试段**。三项失败门：improvement **−5.44pp**（处理臂净盈利率 0.4085 / 284 笔 vs 同池容量基线 0.4629 / 525 笔）、交易日分块 bootstrap CI **[−11.16, +5.22]** 跨 0、处理臂平均净收益 **−0.40%**（基线 +0.05%）；尾部 p05 −5.18% vs −5.33% ⇒ **没有**明显尾部恶化 | 读法写在质量报告 §4.14.3：**损失不在排序层把对的排错，而在这个概率模型没有相对容量基线的增量**。单特征剖面（§4.14.4，39 个 12 日窗）只有 `gap_up_pct` 22/39 与 `avg_turnover_20` 20/39 方向过一半，而容量正是基线已在用的东西；`market_relative`/`trend_position` 两族在 2025 与 2026 **两段独立历史上都系统性 <0.5** ⇒ 继续调这四类的权重不构成对 §4 的可行路径。剩下的两条腿：**第五类信息**（moneyflow/top_list 2025 全市场符号面已采回本地，尚未落库、尚未 OOS 检验）与**影子观察**（仍 0 完整交易日 / 0 笔，只能逐日累积）。另需重跑 2026 的 125 日段（D17 修后，已排队）看两段是否同向 |
| 2025 窗口的输入是否**可证明**可用（§3.1） | 曾担心分钟源与 RAW 口径 | 已逐项证明：40 只预检 2,336,977 行 / 243 天、尾盘窗 `complete/symbol_days=9,697/9,697`、`status=ok`；分钟末 bar 收盘与 vendor 日线 RAW **88.9% 逐分一致**、其余差 0.01~0.07 元（集合竞价边界，不是复权因子量级）；`ref_daily_bars_raw` 2025 落 1,311,230 行 / 5,718 只（只能来自 vendor 包——仓库 `price_series_mode` 全 NULL，同步器按 ADR-002 拒绝搬运是对的）。**边界照写**：仓库 `financial_report_date` 在 2025 的行上最大到 2026-03-02 ⇒ `roe`/`debt_ratio` 是当前快照贴历史行，任何把它们当 2025 硬门用法的做法都是前视，本轮没用 |
| D17 精确涨跌停（曾写成"只能远端补采"） | **根因判错**：全市场 `stk_limit_2026.csv`（776,108 行 / 5,633 只 / 2026-01..07）一直在本地，是上一次同步传了池内那份 209 只的 CSV | 已重刷：6 月 117,724 行 / 5,613 只、7 月 129,003 行 / 5,621 只、Jun/Jul 每日最少 5,603 只、`missing_sources=[]`。远端另独立补采 36 天 / 201,893 行，与本地 CSV 的 `(trade_date,ts_code)` 差集 **0 对** ⇒ 交叉验证而非新数据。**尚未**跟着重跑 2026 的 124 决策日标签，所以"阻塞 5,464 条"仍是修复前口径，不得当成已消除 |
| 第五类信息（§3.2 允许它作为特征/风险信息，但要有独立证据） | 本地副本里 `moneyflow` 113 行 / 2 个符号、`top_list` 40 行 / 2 个符号 —— 只是抽样存在，进不了 `--features` | 已补采到位（生产容器内 tushare，新入口 `scripts/collect_market_events_history.py`）：`moneyflow` 2025 **243/243 天 / 1,248,109 行 / 0 失败 0 截断**，`top_list` 2025 同口径在跑。**下一步是把它落进研究副本并做样本外判别力检验** —— 在它自己站住之前不进正式候选 |
| 市值真值（§3g） | 曾以为污染从 2026-03 中旬开始 | 按年实测把范围收窄成**2026 年**：占位常量占比 2024=0.03%、2025=1.4%、2026=**44.8%**。`float_market_cap_ref` 已用 2025+2026 两份 CSV 一次 `--replace` 重建为单一权威加载（2,021,945 行 / 372 个交易日 / 5,578 只 / 表内占位只剩 5 行）。残留仍是写侧要不要改 NULL（需另开 ADR） |
| 前两层接进生产夜扫（`universe`/`hard_eligibility`）的**真实留档** | 21:45 那跑（build `3522cf8`）作业 success、`verify_trace()` 也 OK，但那份留档**只有三层** | 根因不在数据：成员清单正常产出（`considered 5,478 / advanced 3,676`），只是放在 `source_report/prefilter/universe_quality_selection/hard_gate_membership`，而消费端只认另两条路径 ⇒ 读到空、这两层被**静默**跳过；本地 13 条测试全绿是因为 fixture 按消费端假设造（登记为 NOTE-002 **D22**）。修法与部署见质量报告 §4.15：声明式 5 条候选路径 + 命中路径写进 notes + `universe_membership_status()` 让三种跳过原因各自开口；部署 `2b0ab09` 后**用当晚生产报告重放**验证到五层齐全、`verify_trace()` OK。**真实那一跑仍要看明天 21:45**（今晚跑在修复之前），验收判据：生产证据目录那份 `_night` 留档有 5 层且 `verify_trace()` 通过。历史两次部署事实保留：`1eda512 → 3522cf8 → 2b0ab09` 均为快进，入口始终是 `scripts/nas_deploy_update.sh --branch` |


---

## 1. 结论


- **工程验收：通过**（计划 §4「工程验收」13 个场景 + 线上/历史一致性，逐条有测试钉住）。
  其中**「特征缺失」这一项此前是虚的**：训练器遇 null 会抛裸 `TypeError`，既不归因也不留痕；
  现已改成"排除并计入 `artifact["feature_completeness"]`，整列不可用才 raise"，
  由 `tests/test_tail_net_profit_trainer.py` 两条用例钉住。
  **§4 那句"涉及策略和时间语义的变更同步 Note/ADR"本轮补齐**：资格层留档的可信度
  （词汇表闭合、归因顺序是契约事实、无判别力的门不落档、占位常量按带版本解释规则读）
  写成 `.agents/notes/ADR-004-eligibility-evidence-integrity.md`（Status: Draft，两条未决项
  写在它 §6），根因清单同步 `NOTE-002` 的 D15，README 索引两行同步。
  `clean-scope` 门本轮实跑：rc=2，`blocking_failures` 只有 `mypy_blocking`
  （numpy stub + `python_version="3.11"`，与 HEAD 基线一致），`ruff_clean_scope` rc=0。
- **选股质量验收：已测量、未通过**（不再是"缺数据所以测不了"）。带时刻的尾盘分钟行情
  其实一直在 NAS 上（vendor `Stock_1min_2000-now` 与 `qq_minute_raw`），精确涨跌停也从
  tushare `stk_limit` 补采到位，于是 §4 第一次真跑：94 个决策日、5,613 条标签、
  成交率 48.9%、池内净盈利率 **37.78%**、四组特征逐组 + 合并的校准窗口 raw AUC
  **全部 < 0.5**（0.3657 / 0.4629 / 0.4462 / 0.4996 / 合并 0.4680）→ 训练器按 §3.3 停机，
  4 折命中率数字因此不存在；10 个排序字段的平均净收益**全部为负**，最好的 Top-3 也只比
  池内基线高 +4.42pp 且分块 CI 覆盖基线。逐条判定见
  `docs/trend_tail_selection_quality_report.md`。**不用开盘回测顶替**，也不拿重建样本
  冒充影子观察。
- **影子验证：blocked（0 天 / 0 笔）**。门槛的输入生产者已就位（R12），但一分钟真实尾盘
  观察都还没开始累积。
- 阈值 **0.60 是初始选股规则，不是已证明的命中率**；项目自身锁定 OOS 的绝对命中率是 **43.3–45.0%**
  （Top1/Top3/Top5 的 5D：`docs/alpha_v2/M4H_Historical_Locked_OOS_Report.md:201-203,591`，
  同一处还记着 TopK 绝对净收益为负）。

## 2. 逐项台账

| 计划条目 | 实现 | 证据 | 状态 |
| --- | --- | --- | --- |
| §1 净盈利概率语义、0–3 只/日、1 万参考额、TP+8%/SL−5%、持有 5 日（入场日为第 1 日）、monster 独立 | `contracts/trend_strategy.py` 单一契约，线上/标签/历史验证共用 | `test_trend_strategy_contract.py`(55)；ADR-003 | 代码+测试完成 |
| §2 九层漏斗逐层可追溯（输入/晋级/原因/特征/原始预测/校准概率/模型身份/数据时间） | `research/funnel_trace.py`（写时计数恒等式 + 读时 `verify_trace()`）、`research/night_scan_funnel_trace.py`（夜扫三层 + 前两层生产者）、`scripts/record_replay_funnel_trace.py`（历史侧接线）、`research/selection_funnel_view.py`（拼成九层视图并判"哪些问题答不了"） | `test_funnel_trace.py`(17)、`test_funnel_trace_verification.py`(7)、`test_night_scan_funnel_trace.py`(10)、`test_record_replay_funnel_trace.py`(3)、`test_selection_funnel_view.py`(3)、`test_shadow_evidence.py`(5)；接线由 `test_week5_automation.py::test_night_scan_writes_the_night_half_funnel_trace` 与 `::test_night_scan_reports_why_the_trace_was_not_written` 钉住；`scripts/audit_selection_funnel.py` | **历史侧 94 个决策日已全部落档**（94/94，`verify_trace()` 通过）；**另加 33 个决策日的全市场口径留档**（`funnel_traces_marketwide_janfeb/`，33/33 通过，inputs 170,776 / 晋级 113,208，见质量报告 §3h）；**本轮再加 19 个干净决策日的全市场重放落档**（`funnel_traces_marketwide_aug/`，19/19 通过，考虑 98,184 / 过完所有硬门 66,509=67.7%，市值门阈值 20.2 亿只由测过的值推出、`days_with_non_evaluable_gate_inputs=[]`，见 §3i）；污染窗口的历史侧也第一次落档：**58 个决策日（3 月 21 / 4 月 20 / 5 月 17）全部 emitted、`verify_trace()` 0 失败**，市值门三个月合计逐只淘汰 6,831 只、`unproven_float_market_cap` 只剩 3 月 18 只（见质量报告 §3m）；生产夜扫侧前两层代码已接通（`hard_gate_membership` → `live_universe_facts`），但 NAS 上还没跑过一次带这两层的夜扫，**本轮把生产侧接上了**：`_hard_filter` 导出符号级成员与逐只第一条命中原因（报告新增 `hard_gate_membership`，形状变化已获授权），`live_universe_facts()` 把它读成前两层的输入，代码+测试完成（`test_night_scan_funnel_trace.py` 10→13），**未部署、NAS 上还没有一份这样的留档**；见下方缺口 |
| §2 最终推荐单独留档并关联特征快照 | `archive_final_recommendations()`；缺快照落成 `feature_snapshot_missing` caveat 而不是省略 | `test_missing_feature_snapshot_is_a_visible_caveat` | 代码+测试完成 |
| §2 逐层消融预测性规则、硬门保留 | `StageTrace.kind ∈ {hard_gate, predictive}` + `compare_traces()`（要求交易日集合完全一致） | `test_funnel_trace.py` 消融对照组用例 | 代码完成；**对照组需真实留档才能跑** |
| §2 根因清单，区分已证实/假设；不把 bronze 占比当根因 | `.agents/notes/NOTE-002-selection-quality-root-causes.md` D1–D15 + H1–H5；资格层证据的可信度规则单独成文 `.agents/notes/ADR-004-eligibility-evidence-integrity.md` | 两个 note 文件 + `scripts/audit_selection_funnel.py` 退出码 3 的机器判定 | 已交付，随实测更新（D15 = 浮盈市值门被 provider 兜底常量填成常数而整天失效，根因已定位到 `tushare_provider.py` 的 fillna + 吞异常） |
| §3.1 新记录时区/交易日/去重/标签成熟；旧记录带版本解释 | `write_trace()` 落带时区时刻；`research/record_time_semantics.py` v1/v2 解释、声明矛盾即撤销证据资格 | `test_record_time_semantics.py`(6) | 代码+测试完成 |
| §3.1 绑定实际模型/manifest/特征版本/运行身份，记录失败可见 | `models/tail_serving_manifest.py`（challenger-only，verify 时重哈希工件）+ `models/tail_model_artifact.py` + `runtime_identity` 共享解析器 | `test_tail_serving_manifest.py`(8)、`test_tail_model_artifact.py`(11)、`test_trend_tail_shadow_runtime.py`(29) | 代码+测试完成 |
| §3.1 补齐校验日历/RAW/精确涨跌停/停复牌/证券状态；复用现有接口、独立研究库 | `research/trend_data_readiness.py` + `scripts/audit_trend_data_readiness.py`；研究库 `artifacts/research/tail_minute_bars.duckdb` 的 `ref_*` 参考表 | `test_trend_data_readiness.py`(22)、`test_tail_reference_store.py`(14) | 代码完成；**就绪审计退出码仍为 5(blocked)**，因本机无库、缺 `trade_status` 列 |
| §3.1 校验指数链路，缺失不填零 | 指数缺口按 insufficient 处理 | `test_index_gap_is_insufficient_not_zero_filled` | 代码+测试完成 |
| §3.1 "缺失不能被填成看起来合理的值后当成有效信息"（本轮由 §3g 缺陷具体化）+ "旧记录保留原值、用带版本的解释规则兼容" | `trend_candidate_contract`：`UNPROVEN_FLOAT_MARKET_CAP` / `FLOAT_CAP_INTERPRETATION_VERSION` / `unproven_float_market_cap_mask()` + 新 HARD 规则名 `unproven_float_market_cap`（进 `HARD_GATE_ATTRIBUTION_ORDER`）；重放与全市场两处消费点都不让占位行参与市值比较，分位阈值只从测过的值推，测过的为空则该天标 `float_cap_gate_evaluable=False` | `test_replay_tail_intraday_features.py::test_placeholder_float_cap_does_not_pass_the_size_gate`、`test_measure_marketwide_gate_coverage.py`(2 条新)；非 vacuity 已核：同一批数据在旧表达式下 3 只里有 2 只会带着没测过的市值晋级 | 代码+测试完成，**读侧已生效**；写侧（ingest 不再写兜底常量）是跨 26 个文件的数据语义变更，**要另开 ADR 才动**；数据本身仍缺（见缺口清单） |
| §2 "前置筛选是否过早淘汰" —— 需要**全市场**口径的资格层读数 | `scripts/measure_marketwide_gate_coverage.py`（同一套登记为 HARD 的规则 + 契约归因顺序，套用到全市场日线；阈值注明是"每天全市场同分位"，不可与池内绝对阈值互校） | `test_measure_marketwide_gate_coverage.py`(5：计数恒等式 / 第一条命中归因 / 清单遮蔽量 / 占位市值不得晋级 / 测过的低市值仍走市值门) | **本轮第一次量出来**：95 个决策日、全市场输入 491,516 symbol-day、过完所有硬门 333,955（恒等式破坏 0 天）；其中 **255,356 个（76.4%）不在归档所用的 900 只清单里**（4,151 只）。所以 §3b 的淘汰分布只代表被预选过的一小撮，**测量范围缺陷已证实**；但这 25.5 万个 symbol-day 一条成熟标签都没有，故**不能**据此说遮蔽了多少盈利机会（见质量报告 §3f）。**本轮补两点**：① 那个 95 天窗口跨 2026-03~07，那段的"过了所有硬门"里有 69.9% 的 symbol-day 市值从未被测过（§3g.1），打上带版本的解释规则后同一窗口只剩 212 个晋级；② 遮蔽率本身在**干净窗口**上重测仍成立（2026-08 二十天：74,285 个全市场合格 symbol-day 里 58,642 个＝**79.0%** 不在 900 只清单内，3,768 只不同的票），所以 §2 第一问的方向性答案不是这个数据缺陷造出来的（§3g.2） |
| §3.2 硬门保留 + 预测性加分/板块配额/探索样本分离；硬门后先算轻量特征再截断 | `feature/trend_candidate_contract.py`（`HARD` vs `predictive`、`assert_training_features()`、`HARD_GATE_ATTRIBUTION_ORDER`） | `test_trend_candidate_contract.py`(17)、`test_replay_tail_intraday_features.py`(11) | 代码+测试完成；**本轮新增两条已证实缺陷（留档词汇表）**：① `insufficient_history_at_asof` 曾被重放脚本写进 `hard_eligibility` 留档却没登记进 `_RULE_KIND`，消融实验按 `classify_rule()` 分组时看不见这条淘汰——已登记为 HARD 并加闭合校验（非 HARD 名字一进 sidecar 就 `SystemExit`）；② "第一条原因"的归因此前取决于 dict 插入顺序，现改为契约里的 `HARD_GATE_ATTRIBUTION_ORDER` 并写进每条留档 notes。详见质量报告 §3b.1/§3b.2 |
| §3.2 新 trend as-of 特征契约，训练与线上同一套；旧 T−1 与 Alpha V2 保持独立；四组特征逐组+消融 | 同一特征入口 + walk-forward 分组门；日内两列由 `replay_tail_candidate_pool.py --minute-db` 从分钟库真算 | `test_tail_walk_forward.py`(20)；特征白名单是训练的第一条前置检查 | 代码+测试完成；**四组时间外已测量**：4 个 OOS 测试窗平均 AUC 0.5012~0.5391（弱到不足以进正式候选）；**已证实缺陷**：33 列里 22 对 Spearman≥0.90，`relative_strength`≡`rank_ret_20`（ρ=1.0）等跨组重复同一信息 |
| §3.3 新独立标签 + `p_net_profit_5d_tail`，不覆盖旧标签 | `labels/tail_net_profit.py` + `label_policy_v4_*` 注册/核验 + `output_semantics` | `test_tail_net_profit_label.py`(22) | 代码+测试完成 |
| §3.3 尾盘每 5 分钟检查、只读已完成 bar、确认后下一分钟成交、未成交不计盈亏、T+1、双触发止损优先、第 5 日顺延、成熟=实际可成交退出、数据末尾强平不出已实现标签、按日期冻结成本、公司行动单列不确定 | 契约内单一实现，线上/历史共用 | `docs/trend_tail_acceptance_evidence.md` §1 表逐场景 → 测试名（13 场景全绿） | 代码+测试完成 |
| §3.3 逻辑回归基线 + 现有 LightGBM 参数 + 独立校准段；训练失败不静默换模型 | `models/tail_net_profit_trainer.py`、`scripts/train_tail_net_profit_model.py`、`scripts/freeze_tail_model_candidate.py` | `test_tail_net_profit_trainer.py`(20)、`test_tail_model_artifact.py`(11) | 代码+测试完成；**本机 lightgbm 不可用（缺 libomp），仅逻辑回归路径实测过** |
| §3.4 按新概率排序、阈值 0.60、代码为同分序；旧综合分/等级/分歧/恢复买入不再决定资格 | `rank_final_recommendations` + 契约准入 | `test_trend_strategy_contract.py`、`test_trend_tail_page_and_feedback.py`(26) | 代码+测试完成 |
| §3.4 数据不足/模型无效/风险不允许/无达标股票 ⇒ 0 只且不补名额 | fail-closed 分支（身份异常、预算、缺特征各自点名） | `test_model_identity_violations_produce_zero_recommendations`、`test_missing_or_dirty_feature_is_refused_not_zero_filled` | 代码+测试完成 |
| §3.4 页面分列候选/最终推荐/成交状态并注明策略·参考额·数据日期 | `page_view()` 缺字段即 raise | R13 用例 + 前端构建 | 代码+测试完成 |
| §3.4 成熟反馈按模型版本/市场状态/拒绝原因；自动学习只出 challenger | `research/tail_mature_feedback.summarize_mature_feedback`（state 只取三种，promotion 固定人工票据） | `test_trend_tail_page_and_feedback.py` | 代码+测试完成 |
| §4 影子验证 60 天 / 100 笔成熟成交 | `research/shadow_evidence.py` + `scripts/audit_shadow_evidence.py` + `TrendTailShadowService.shadow_readiness_summary()`（报告字段 `trend_tail_shadow.shadow_readiness`） | `test_shadow_evidence.py`(5) | 生产者**代码+测试完成**；门槛读数 **0/0 = blocked** |
| §5 发布/回滚清单 | `docs/trend_tail_release_rollback_checklist.md`（R1–R15 + 前置硬门 P0-1…P0-4） | 该文件；P0 硬门当前**未通过** | 已交付 |

## 3. 还缺什么（按性质分三类，不要混为一谈）

1. **需要一次确认的设计决策：`universe` / `hard_eligibility` 两层的接线**

   **新增同一决策项（本轮实测，见质量报告 §3b.3）**：线上侧写入器
   `night_scan_funnel_trace.build_universe_stage_traces()` 会把三个
   `classify_rule()==predictive` 的名字原样写进 `hard_eligibility` 层——
   `future_listed`、`insufficient_history_window_bars`（这两个按含义是资格/数据完整性硬门）、
   `known_suspended`（缺 bar 按契约不等于证明停牌，语义未定）。后果与已修掉的那条同型：
   §2 的消融按 `classify_rule()` 分组，会把资格淘汰当成"可移除的预测规则"。
   本轮试过加"未登记名字就不落档"的守卫，实测会打断两条现有测试并让这两层在真实输入上
   永久不落档，因此**撤回到只检测、不修复**——要先由契约作者给这三个名字定性。
   生产者已就位且有测试，但**生产夜扫路径拿不到符号级事实**：
   `runtime/universe_candidate_selector.py::_hard_filter` 只返回
   `rejected: dict[str, int]`（逐原因**计数**，:447-575），夜扫报告里的
   `universe_quality_selection` 因此也是计数；而 `StageTrace` 不许用计数冒充成员
   （`night_scan_funnel_trace._universe_facts` 只有清单时才落这两层，:99-101）。
   2026-10-08 在 NAS 上核实过：最新一份部署报告 `nr-20260930-01.json` 里
   `universe_snapshot` / `universe_quality_selection` / `night_funnel_trace` **都是 0 hit**，
   所以接线不仅没做，连"改完能对着真报告验一次"的条件也不具备。
   **本轮按"研究侧 sidecar"把历史侧接完了**：`replay_tail_candidate_pool.py --universe-facts`
   逐日导出符号级事实，`record_replay_funnel_trace.py` 落成 94/94 个决策日的
   `universe` + `hard_eligibility` 留档，每条都过 `verify_trace()`。
   **生产夜扫仍然不落这两层**，卡点没变：要接就得让 `_hard_filter` 一并导出被淘汰的符号清单，
   那会改变生产报告的形状与体积（从计数变成 ~数千个代码/日）。这一步还需要你点头，本轮没做。
2. **需要时间，不需要代码**
   - 影子验证 ≥60 个完整交易日且 ≥100 笔成熟模拟成交：**当前 0 天 / 0 笔**。
     输入生产者与门槛判定都在（R12），但只能等真实尾盘观察逐日累积。
   - §4 的 +5pp 与分块 CI：本轮已按 §3.3 停机（四组校准窗 AUC 全 < 0.5），
     要拿到能过关的模型需要**新的信息源**或**制度翻转后的窗口**，不是再调参能解决的。
3. **仍缺的数据源**
   - `security_status` 源表在生产仓库里是**空的**（NAS `market.duckdb` 实测 rows=0），
     退市/改名历史无法证明 ⇒ 幸存者偏差口径只能是 `incomplete_or_unknown`。
     **本轮已补采并落库**：容器内 tushare `stock_basic(L/D)` + `namechange` → 7,061 行
     证券状态区间（5,572 只在市 / 339 只退市带 delist_date / 1,150 条池内改名史），
     入口是 `sync_tail_reference_data.py --security-status-json`。
     但 `namechange` 响应正好 10,000 行 = **被接口单次上限截断**，那一类逐行
     `coverage_complete=False`：退市覆盖算证明了，ST/改名覆盖没有。
   - 就绪审计因此从 **blocked 翻成 insufficient**（`blocking_gaps == []`），其中还修掉两处误判：
     仓库没声明复权口径时，只要研究库副本可证明为 raw 就不该判死（成交与出场读的就是那份副本）；
     证券状态区间此前只查生产那张 0 行的表，不查副本。
   - **停牌与停更两条硬门在历史侧没有可验证样本**（本轮实测，见质量报告 §3d）：
     重放读的是 `daily_bars.suspended` / `is_delisting_risk`，池内 85,269 行**全为 false**；
     参考库 `ref_suspend_status` 28,929 行里只有 54 行是正向停牌声明、只涉及 2 只股票，
     且都不在这 900 只池内；`stale_market_data` 阈值 30 个日历日，而池内窗口最大 bar 间隔
     是 24 天（春节）——**长假与停更只差 6 天**。所以留档里 `suspended: 0` 的含义是
     "没有声明"，不是"没发生停牌"；§4 的"停牌"一项只能记为"代码路径+单测，历史无样本"。
     要补的是：按池内符号单独拉 `suspend_d` 的 S 类记录，或把 `ref_security_status`
     的上市/退市区间接进重放硬门；影子期则把线上判为停牌/停更的 symbol-day 攒起来反验。
   - **漏斗第一层的输入不是全市场，且其来源不可复现**（本轮实测，见质量报告 §3e）：
     归档 `universe` 层每天输入 894~900 只，而仓库里当天有行情的股票是 **5,135~5,186 只**；
     那 900 只来自研究侧清单 `artifacts/research/tail_symbols.txt`，**仓库内没有产生它的代码**。
     另有 229 个 symbol-day（94×900=84,600 与归档 84,371 之差）当天无 bar，既没算进输入
     也没算进淘汰。所以 §2 的第一个问题"前置筛选是否过早淘汰"仍未回答。
     本轮已做的：重放报告新增 `universe_input_provenance`（路径 + sha256 + 数量 +
     `producer=unknown_not_recorded_in_repo`），并钉住"改一个字节摘要就变"。
     要补的：换成可证的 PIT 全集（指数成分或 `stock_basic` 全量 + 上市区间）再重跑历史侧。
   - **`float_market_cap` 被填成常数 1.2e10，浮盈市值硬门静默失效 —— 根因已定位到代码，读侧已处理**
     （质量报告 §3g/§3g.1/§3g.2）。根因：`tushare_provider.py:1677-1684` 在 `daily_basic`
     拿不到 `circ_mv` 时 `fillna(_DEFAULT_FLOAT_MARKET_CAP)`，而该调用外面
     `except Exception: basic = pd.DataFrame()`（718-719 行）把接口失败**静默吞掉**；
     三个 provider（tushare/akshare/efinance）用同一个字面值 12_000_000_000.0，
     所以这个数就是"没测过"的指纹。库内实测：2026-04 99.7%、05 99.98%、**06 100%（全月只剩
     这一个取值）**、03/07 各约五成；2022-05 起每月 15~46 行，2025-09~2026-02 升到每月
     2,511~5,608 行。它**不是填零**，所以躲过了所有 NaN/0 检查。
     本轮已做（读侧，按 §3.1"旧记录保留原值 + 带版本的解释规则"）：契约新增
     `UNPROVEN_FLOAT_MARKET_CAP` / `FLOAT_CAP_INTERPRETATION_VERSION` /
     `unproven_float_market_cap_mask()` 与 HARD 规则名 `unproven_float_market_cap`
     （归因顺序里排在 `min_float_market_cap` 之后）；重放与全市场两处消费点都不再让占位行
     参与市值比较，分位阈值只从测过的值推，测过的为空则该天记
     `float_cap_gate_evaluable=False`。重测结果：污染窗口 310,142 个 symbol-day 里
     **216,862（69.9%）是"市值从未测过"**、60 天里 40 天整日无从判定、晋级只剩 212；
     干净窗口（2026-08，20 天）只有 1 行占位、市值门正常淘汰 1,242 行，
     而对 900 只清单的遮蔽率仍是 **58,642/74,285 = 79.0%** —— 所以 §2 那条结论不是被这个
     缺陷造出来的假象。
     **仍未做的一条（也是这轮唯一剩下的数据动作）**：用容器内 tushare `daily_basic`
     （`circ_mv`，万元）重刷 2026-03 中旬之后的真实值，拿不到的标 `insufficient`；
     本地重算不成立，因为 `daily_bars` 的 46 列里没有任何股本数字段。
     另有**一项要决策才动的写侧改动**：让 ingest 写 NULL 而不是兜底常量 —— 这是跨模块的
     数据语义变更（`src/` 下 26 个文件出现 `float_market_cap`），按 AGENTS.md §12 需另开 ADR。
   - **全市场清单**（5,194 只，sha256 `929680bd…599099`）已经能喂进重放并落档：干净窗口
     2026-08-03~08-29 的 19 个决策日 19/19 份通过 `verify_trace()`（质量报告 §3i），
     考虑 98,184 / 过完所有硬门 66,509。但这批 symbol-day **一条成熟尾盘标签都没有**：
     分钟库只覆盖 208 只池内票、日期止于 2026-07-17，本次重放没带 `--minute-db`，
     日内两列整列缺失。要把 §2 的"遮蔽了多少盈利机会"真正回答，得先把分钟行情
     从 208 只扩到全市场清单，再重跑标签重建与 ≥4 折滚动验证。
   - **§2 的"每层记录使用的特征与特征计算版本"：`hard_eligibility` 这半条已接通**（本轮）：
     重放按当天真的跑过的规则映射判定输入列（`RULE_INPUT_COLUMNS` → sidecar 的
     `gate_input_columns`），写入器转达成 `features_used`，并把
     `feature_compute_version=trend_asof_v1` 与 `float_cap_interpretation=…v1` 写进 notes；
     重跑的 19 份 8 月留档稳定记着那 11 列且 19/19 过 `verify_trace()`。
     两个残留要一起看：`universe` 层 `features_used` 为空是**合法的**（那层不读任何列），
     而 `feature_compute_version` 字段本身仍是 int、所有层都填默认 0（版本号走 notes 传），
     把它改成字符串是一次跨全部层的归档 schema 变更，**没做**；夜扫三层与尾盘层的
     同一字段也还是 0，同一原因。
   - 带时刻的尾盘分钟行情与 `daily_trade_status` 两项**本轮已解决**：
     分钟 bar 从 vendor `Stock_1min_2000-now` 落进独立研究库（6,464,825 行 / 129 交易日），
     可交易状态由"当天确有成交的 RAW 日线"正向声明，精确涨跌停从 `stk_limit` 补采。
4. **远端授权**：分支 `feat/stock-selection-quality-overhaul` 已推送至 `ad7f588`，PR #105 OPEN 且 mergeable。远端只做过推送与研究库补采，未动生产：本轮新增的 5 个提交（`6d336ab` / `ce19b76` / `7ecee08` / `ad7f588` 等）都是本地代码 + 文档。

## 4. 基线事实（避免把既有问题当成新引入）

- **`ruff check scripts tests src` 全仓跑现在有 75 个错误，全部是既有状态**（本轮实测：
  集中在 `src/stock_analyzer/research/heavy_ts_shadow.py`、`scripts/p1_*` 等本轮未触碰的文件，
  本轮改过的四个文件里 0 个）。`clean-scope` 质量门跑的是更窄的范围（`ruff_clean_scope` rc=0），
  所以"门绿"不等于"全仓 ruff 绿"；提交说明里若写"ruff 全绿"必须限定范围
  （`b2cc522` 那条就这么写错了，这里更正）。日常口径：只对本次触碰的文件跑
  `ruff check <files>`，仓库级数字要用 75 作基线对照，别当成新引入的回归。

- `mypy src` 因 numpy stub 与 `pyproject.toml` 的 `python_version="3.11"` 直接中止 ⇒
  clean-scope 门 rc=2，唯一 blocking 恒为 `mypy_blocking`；`ruff_clean_scope rc=0`。
- 两条测试在基线 `6c7079c` 就红：`test_service_model_registry…can_warn_without_transition`、
  `test_alpha_v2_m4l_e2e_rehearsal::test_attack_a_source_label_without_funnel_artifact_fails`。
- 本机无 `data/*.duckdb`（库在 NAS），`artifacts/*` 被 gitignore ⇒ 运行报告类工件无法入库。
| §4.4 排序层读数与 §4.5 概率模型读数不一致这件事怎么处置 | 规则侧：`turnover`/`avg_turnover_20` 每日前 3 → 0.4250 / 0.4083，Δ +12.88pp / +11.23pp，交易日分块 CI 下界均 >0（评估段 2026-05-20..07-16，窗口内池子仅 0.2961）。概率侧：同一列进 LR 仍在第 1 折校准段 fail-closed（0.4894 / 0.4807，退 5）。契约拒收 `-gap_up_pct` 这种负号语法，是期望行为 | **需要用户决策的岔口**：拉长真实历史走概率模型（要远端补采，解 D17），还是把容量优先做成显式排序规则（流动性同时当硬门与第一因子，牺牲分散度，属策略形态变更）。在 §4 验收未通过前不擅自实现后者 |
| §4.4 的排序读数能不能直接引用 | **不能。** 加 `--pre-registered` 后（方向只由前 40 个决策日定死、评估段只看一臂），22 臂里的三个赢家只剩 `avg_turnover_20` 降序：0.4083 vs 池子 0.2961、Δ +11.23pp、分块 CI 下界 +0.714>0、平均净收益 +0.00045。`turnover` 在方向段是反的（−0.0096）→ 预登记后 Δ −4.69pp；`gap_up_pct` 取 desc → Δ +4.41pp、下界≤0；三条等权组合 Δ +8.89pp、下界 −1.895，反而不如单条 | 引用排序证据时以报告 §4.6 为准；`avg_turnover_20` 同时是硬门输入，「流动性既当资格又当优先级」是策略形态决策，仍未定 |
| 上一行给的「容量优先显式排序规则」这个可选项还成立吗 | **不成立，撤回。** `--walk` 多段预登记（6 段 × 16 日，方向只由该段之前样本决定）显示没有任何一臂 CI 下界为正，`avg_turnover_20` 合并只剩 Δ +1.54pp、CI [−3.952, 7.139]、正向段 3/6 —— 单段那 +11.23pp 是运气。反向证据倒是稳的：趋势极端/高波动/高跳空/相对强弱高值排序，5 条臂 CI 上界全为负 | 有证据的下一步只剩两个，且都在本地之外：(a) 远端补采解 D17 以拉长真实历史；(b) 远端补采第五类信息。另：把那批列改作**风险减分**是目前唯一有多段支撑的方向，但要先登记进契约并用未参与选择的历史复验，复验前不进生产 |
| 上一行末尾提的「过热/波动当风险减分」这条待办成立吗 | **不成立，已收回**（报告 §4.7.1 的方向表：那些段实际取的是**低值**，没有支撑它的证据） | 若仍想检验「过热/波动是风险」，必须用从未参与方向选择的历史（vendor 包有 2024/2025 全年分钟，需新一轮全市场扩采）单独做符号检验 |
| §4 工程验收现在有没有可核对的证据面 | 有，报告 **§4.8** 把 19 组场景逐条对到 `tests/*.py::test_*`（存在性由扫 `^def test_` 实测，非凭记忆；我起草的两条「缺口」核对后不成立，已在原文里如实改掉）。本轮实跑 trend/尾盘全部 17 个测试文件：**243 个测试全部通过**（进度 72/72/72/27，无 failed/error）；`clean-scope` rc=2 唯一阻塞仍是基线 `mypy_blocking`；新脚本 ruff 干净 | 工程验收可陈述为「代码完成 + 目标模块测试完成」；**与选股质量验收无关**，后者仍未通过（§4.7.1） |
| §2 第一问（前置筛选是否过早淘汰）到底有没有答案 | **有了，而且是负答案**：4 月 20 个决策日把容量截断 300 放宽到 3,400 重算标签（同一套确认/成本口径），保留组净盈利率 0.6012、淘汰组 0.5575，差值 −4.36pp、交易日分块 CI [−7.33, −1.28] ⇒ **这一层不是损失来源**；「79% 合格候选没进清单」量的是覆盖面不是质量（我此前混用过，已纠正） | 损失定位因此推向预测与排序层（与 §4.7.1 同向）。要据此动 pool-size 需跨月复跑 + 用户授权 |
| 层消融跨月复跑后 §2 第一问的最终口径 | **条件性答案，不是单向结论**：4 月保留组 +4.38pp 更好、5 月保留组 +6.32pp 更好、**3 月淘汰组 +4.65pp 更好（CI [+0.56, +8.81]）**，每月内都显著 ⇒ 容量截断的质量效应**方向随月翻**，与四类特征信息同一形状 | 上一行写的「这一层不是损失来源」**只适用于 4/5 月**，已按 §4.10 降级；动 `--pool-size` 属候选池形态变更，需跨制度段证据 + 用户授权 |
| §2 第二问（同一信息被反复使用？） | **有硬证据了**：`relative_strength` 与 `rank_ret_20` ρ=**1.0** 却分属两组；`rs_ma20`/`ma20` 0.9994、`rs_ma5`/`ma5` 0.9991；达阈值冗余对 22 个；四组列级时间外 AUC 均值 0.507~0.5141 | 报告 §4.11 / NOTE-002 D19。含义：组数≠独立证据数；下一轮若要把这四类当特征用，得先按 ρ 去重，而不是继续加分组 |
| §2 五个诊断问现在都答上了吗 | 五个里**四个有实测答案**：过早淘汰=条件性（D18，跨月翻符号）；重复使用同一信息=成立（D19，ρ=1.0 且跨组）；偏向已大涨/高波动=成立、偏向难成交=不成立且方向相反（D20）；损失来自哪一层=预测与排序（§4.7.1 + §4.10 同向）。第五问里「数据缺失/滞后/降级影响多少只」只有分段读数（D17 的 5,464 条、17,491 行无真值、109 条 session holes），**没有一张合并的表** | 剩这一项是可做的本地工作：把已散在各报告里的数据质量数字合成一张表 |
| §2 第 4 问（数据缺失/旧缓存/滞后/降级各影响多少）| **已合并成一张表**（报告 §4.13，`scripts/measure_data_quality_impact.py` → `mw_data_quality_impact.json`）：5,601 条请求出不了标签（涨跌停 5,464 为主）、占位常量 17,380 行无真值可替换（占比 30.10%→1.18%）、自写值与真值差 >1% 的 26,013 行、日内两列重放不可复现、概率模型 4 折产出 0 折 | 汇总时发现并修掉一个可审计性缺口：验证器遇训练/身份失败时**不留任何工件**，现在也写 status=blocked 的最小报告（退出码不变、20 条测试通过）|


## 6. 完成度对账（2026-10-08，逐条对到工件；不看叙述看文件）

状态只用这五个词：**已完成** / **已实现·测量未达标** / **阻塞（缺数据或缺授权）** / **未部署** / **待用户决策**。
「分支已推送」不算完成度，所以单列在最后。

| 计划条目 | 状态 | 可核对的证据 |
| --- | --- | --- |
| §1 范围与规则（trend、14:30–14:50、0–3 只、1 万元、+8%/−5%、5 交易日、monster 独立） | 已完成 | `src/stock_analyzer/contracts/trend_strategy.py` + `tests/test_trend_strategy_contract.py`（`test_contract_pins_first_round_business_terms`）；`docs/trend_tail_selection_quality_report.md` §1 |
| §2 九层留档 | 已完成（夜扫逐只拒绝原因仍缺，见 D11） | `research/funnel_trace.py`、`night_scan_funnel_trace.py`；`artifacts/research/funnel_traces*`（污染窗 58/58、干净窗 19/19 过 `verify_trace()`） |
| §2 诊断问 1 过早淘汰 | 已实现·测量未达标（答案条件性） | 报告 §4.9/§4.10；`ablate_pool_{apr,mar,may}_*`；NOTE-002 D18 |
| §2 诊断问 2 同一信息反复使用 | 已完成（结论：成立） | 报告 §4.11；`mw_feature_stability_125d.json`（ρ=1.0 跨组、22 对冗余）；D19 |
| §2 诊断问 3 偏向大涨/高波动/难成交 | 已完成（前两个成立、第三个相反） | 报告 §4.12；D20 |
| §2 诊断问 4 缺失/旧缓存/滞后/降级各影响多少 | 已完成（合并表） | `scripts/measure_data_quality_impact.py` → `mw_data_quality_impact.json`；报告 §4.13 |
| §2 诊断问 5 损失来自池/预测/排序/规则 | 已完成（指向预测与排序） | §4.7.1 + §4.10 + §4.12 三处一致 |
| §2 逐层消融预测性规则 | 已完成（容量截断层）；夜扫三层仍不可归因 | `replay_tail_candidate_pool.py --pool-size`；D11 的 `night_truncation_reason_not_recorded` |
| §3.1 时间语义 + 带版本解释规则 + 身份绑定 + 五类参考数据 + 指数链 | 已完成；6/7 月涨跌停**阻塞** | `contracts/trend_strategy.py`、`feature_contract_version`、`FLOAT_CAP_INTERPRETATION_VERSION`；D17 与 `mw_data_quality_impact.json` 的 5,464 行 |
| §3.2 硬门保留 / 预测项分离 / 轻量特征前置 / 四组消融 / 新闻主题待独立证据 | 已完成；四组**均无跨段稳定方向**；第五类信息本地无可算输入 | `runtime/universe_candidate_selector.py::_gate_membership`、`measure_tail_feature_direction.py --rolling/--walk`、`mw_feature_direction_125d_rolling.json`；§4.3 |
| §3.3 p_net_profit_5d_tail + LR/LightGBM 候选 + 独立校准 + observed/replayed 分开 | 已实现·**训练门未过**（4 折 0 折产出概率） | `tail_net_profit_label`、`tail_net_profit_trainer`、`mw_labels_125d.json`、`mw_validation_125d_first4_blocked.json`（status=blocked） |
| §3.4 按新概率排序 + 0.60 + 上限 3 + 0 只不补 + 页面三分栏 + challenger-only | 已完成（代码与测试） | `rank_final_recommendations`、`trend_tail_shadow_service.py`、报告 §4.8 的测试映射表 |
| §4 工程验收（13 类场景 + 线上历史同判定 + 静态检查 + clean-scope + Note/ADR） | 已完成 | 报告 §4.8（19 组 → 60 个测试名全部存在、243 测试通过）；`clean-scope` rc=2 唯一阻塞=基线 `mypy_blocking`；NOTE-002 / ADR-004 |
| §4 选股质量验收（≥4 折 + ≥5pp + CI 下界>0 + 平均净收益为正 + 尾部不明显恶化） | **已实现·测量未达标**：四折从未进测试段 | `mw_validation_125d_*.json` 全部 `status=blocked`；报告 §4.7.1 |
| §4 未来影子验证（≥60 完整交易日且 ≥100 笔成熟成交） | **阻塞**（只能等真实观察逐日累积） | 无 `shadow_readiness` 产出（验证未跑完），台账 §0 |
| §5 六项分阶段交付物 | 已完成 | `NOTE-002`（根因清单 D1–D20）、`trend_tail_data_source_findings.md`（数据与契约）、`tail_net_profit_*`+`tail_serving_manifest`（候选模型）、`trend_tail_shadow_service.py`+页面（完整链路）、本报告 §4（验证报告）、`trend_tail_release_rollback_checklist.md`（发布回滚） |
| §5 生产边界（独立库/模型目录、compose 守卫、不改历史 marker） | 本轮研究数据只落本地独立库；NAS 仅作只读 Tushare 采集执行环境 | API 容器 `/tmp` 有短期采集脚本/输出并已清理；未写 NAS 数据库、未修改 marker/artifact/config、未重启或部署服务；`.mimosa/` 未动 |
| 分支状态 | 本地 `feat/stock-selection-quality-overhaul` 领先 origin **26 个提交**，未推送 | `git status -sb`；PR #105 存在但对应旧提交 |

**一句话完成度**：工程侧（契约、留档、诊断、审计、测试、文档）已达可交付；
**目标本身（提高最终推荐的净盈利概率）尚未被证明**——缺的是时间外稳定方向的信息与更长真实历史，
两者都要远端补采授权或真实观察天数，不能用调参或开盘回测顶替。

## 7. 2026-10-10 整改执行续记（本节更新阶段一、二的实际状态）

本节是 2026-10-10 对本轮代码、测试和本地研究库重建的后续记录；前文的历史事实与日期不改写。
本节的实测状态优先用于判断本轮进度。代码与研究数据都在本地工作树 / `artifacts/research/`；
NAS 仅执行了读取 Tushare 的研究采集诊断，并在 API 容器 `/tmp` 使用临时文件；没有修改 NAS 数据库、
生产 marker、线上 artifact、服务配置或运行镜像，也没有提交或部署。
当前分支 `feat/stock-selection-quality-overhaul` 的 `HEAD` 为 `c58da46eaf18defed3adf2ac6ec89ad668dcbf66`，与其 origin 分支头一致；本轮修改留在未提交工作树。
前文列出的 2025 全市场旧模型指标保留为历史诊断；它们使用本轮 RAW 分钟退出和公司行动账本修复前的
旧标签，不能当成本轮新真值下的质量验收结果。2025 因子已采集并导入；本轮新 RAW 全量重建和两段开发矩阵现已完成。

| 计划阶段 | 本轮完成内容 | 当前验收状态与仍缺内容 |
| --- | --- | --- |
| 阶段一 §1 数据来源与 PIT | RAW / 因子 / 公司行动来源已导入隔离研究库；symbol 前导零、请求范围排序和输入 SHA 有记录。2025 adj_factor 共 317,210 行 / 1,129 只；688411 仅对经上市日证明的上市前前缀缺口作解析。2026 v9 与 2025 全量 v2 的请求范围参考数据门已核实 | **本轮请求范围参考数据门通过；完整 PIT 审计未完成**。2026 v9：28,200 请求、0 个参考缺口。2025 v2：72,600 请求、0 个参考缺口；原 1,032 个缺口由 188 个分钟完整交易日 marker、精确 Tushare limit/status 与 688766 官方停牌区间证据补齐。未知状态没有被推定为交易或停牌，停牌与分钟冲突仍标不确定。证券状态全历史覆盖、旧完整选股链路及所有入模/硬门字段的来源与可用时刻尚未逐字段证明；资金流 / 龙虎榜字段因盘中可用时点未证明而没有进入模型实验 |
| 阶段一 §2 交易与标签 | RAW 分钟出场、真实退出时刻、未成交不补选、公司行动现金/股份账本和缺口导出代码已实现；2026 v9 与 2025 全量 v2 已重建 | **标签参考数据门通过；仍有 51 条明确不确定标签**。2025 全量 72,600 请求，62,992 笔成交，62,941 条成熟可训练，27,654 条净盈利；17 条停牌未成交，19 条公司行动核算不确定，8 条退出不可执行，24 条停牌/分钟冲突不确定。以上不确定项不计输赢。账户账本有单测，尚无合格候选可做四臂 NAV 质量确认。旧报告的 `trade_status_source_gap_symbols` 实为请求数；本轮只读计数复核已分开核算：2026 v9 为 7 请求 / 3 只证券 / 5 个证券-交易日，2025 v2 为 4 / 1 / 4。更正报告作为 countercheck 保存，未重算标签或收益指标 |
| 阶段一 §3 验证器与证据计数 | 真实日历折、决策覆盖日、配对移动块 bootstrap、样本唯一键、capture-mode 隔离、固定账户账本及独立确认协议已实现；定向测试已覆盖 | **实现完成，质量验收未通过**。开发窗口短于 120 个配对测试日与功效门槛，不能计算为质量通过 |
| 阶段二 §1 候选链路与逐层消融 | 漏斗留档、历史重放及同日比较代码已实现；既有容量截断消融覆盖 2026-03 至 05，结论跨月翻转（条件性），但未取得完整生产 300/100/50 链路各预测性层的同日反事实留档 | **未完成完整逐层质量归因**。不能把已有单层容量消融当成全部漏斗层消融；夜扫预测层还缺同日对照留档。重放时需要完整市场候选、同口径 RAW 标签与 PIT 字段，目前 2025 / 2026 新 RAW 全量重建只覆盖冻结的每日 300 个请求，不能补算更宽池子的真实结果 |
| 阶段二 §2 固定模型实验 | 已登记最多 12 个 LR / LightGBM 实验臂、训练窗去重、Holm 校正和候选选择规则；2026 v9 与 2025 新 RAW v2 均已完成开发矩阵 | **两段历史开发都没有候选**。2026 v9 的 10 个价格臂首个校准窗 raw AUC < 0.5；2025 v2 的 9 个价格臂同样因 raw AUC < 0.5 停止，唯一完成四折的逻辑回归臂有测试折无成熟候选成交，2 个资金流 / 龙虎榜臂因 PIT 时点未证明而阻塞。没有数据臂通过校准门，因此本轮没有声称 LightGBM 完成真实训练与工件 round-trip。两份报告均为 `no_qualified_candidate` / `release_eligible=false` |
| 阶段三独立确认 | 四臂八项检验、样本功效估计、工件身份与未开封窗口门已实现 | **未启动**。没有合格候选，2025/2026 数据都已被查看，不能重命名为独立数据；旧完整选股链路共同 PIT 重放、全部特征逐字段时点证明也未完成。需要候选冻结后采集新窗口 |
| 阶段四未来影子与上线审查 | 当前没有生产部署动作 | **未启动，不能由本地修改完成**。需先通过独立质量确认，再由未来运行累积至少 60 个完整交易日、100 笔成熟模拟成交和账户净值；符合生产规约的 NAS 检查、回滚方案和部署审批另行执行 |

### 本轮可复核证据

- 阶段一 2026 v9 标签报告：`artifacts/research/tail_rebuild_raw_v9_report.json`；开发消融：
  `artifacts/research/tail_stage2_ablation_raw_v9.json`（0 个参考缺口，但无合格候选）。
- 2025 v1 全量旧真值报告：`artifacts/research/tail_rebuild_raw_2025_v1_report.json`；修复子集 v3 报告：
  `artifacts/research/tail_rebuild_raw_2025_repair_v3_report.json`（1,032/1,032 标签、0 缺口）；新全量 v2
  报告：`artifacts/research/tail_rebuild_raw_2025_v2_report.json`（72,600 标签、0 参考缺口）。新样本与日历：
  `artifacts/research/tail_samples_raw_2025_v2_assembled.jsonl`、`artifacts/research/tail_trading_calendar_2025_raw_v2.csv`；
  阶段二报告：`artifacts/research/tail_stage2_ablation_raw_2025_v2.json`（无合格候选）。
- 2025 官方停牌区间证据：`artifacts/research/official_suspension_evidence_688766_20251125_20251209/`；请求范围数据 manifest：
  `artifacts/research/tail_rebuild_subset_2026jan_may_v4_sorted_manifest.json`。
- 样本与交易日历：`artifacts/research/tail_samples_raw_v4.jsonl`、
  `artifacts/research/tail_trading_calendar_2026_raw_v4.csv`；2026 开发消融报告：
  `artifacts/research/tail_stage2_ablation_raw_v4.json`，明确 `release_eligible=false`。
- 2025 因子采集与导入：`artifacts/research/mw2025_adj_factor_collection_v2/manifest.json`、
  `artifacts/research/mw2025_adj_factor_collection_v2/import_report.json`；
  原始 coverage 审计仍保留作历史基线：`artifacts/research/tail_rebuild_2025_factor_coverage_audit.json`。
  原采集器读取的 symbol 清单与范围 manifest：
  `artifacts/research/mw2025_adj_factor_symbols.txt`、
  `artifacts/research/mw2025_adj_factor_collection_manifest_v2.json`。v2 覆盖
  2024-12-01 至 2026-01-26；原 manifest 的 2026-01-15 截止日不足以覆盖完整退出窗口，已保留但不再作为采集计划。
  manifest 指定仅从运行环境读取 `SA__MARKET_WAREHOUSE__TUSHARE_TOKEN`，不会把 token 写入文件；
  Tushare token 仅从 NAS API 容器运行环境读取；本地采集归档含原始结果、SHA 与导入报告，未把 token 写入文件。
- 2026 历史缺口补充检查：`scripts/augment_tail_daily_refs_from_minutes.py` 在只读 v4 输入上生成
  `artifacts/research/tail_rebuild_subset_2026jan_may_v5_minute_markers.duckdb` 与 manifest；
  由于请求缺口日为零成交占位或分钟缺失，合格 marker 数为 0。v5 只是诊断副本，标签报告仍以
  v4 为准；对应 fail-closed 测试在 `tests/test_augment_tail_daily_refs_from_minutes.py`。

结论：本轮已完成 RAW 分钟退出 / 公司行动账本代码、2026 v9 与 2025 v2 标签重建，以及两段历史上的固定阶段二模型开发矩阵；
请求范围参考缺口为零，但完整字段级 PIT 审计和生产候选漏斗逐层反事实尚未完成，两段模型开发均无合格候选，2025 另有 51 条不确定标签。
阶段三需要先有候选，再用未开封的新窗口确认；阶段四需要其后真实影子观察。不能用已查看的历史、降低门槛或生产部署代替这些证据。

## 8. 2026-10-10 后续：财务 PIT 侧车与证券状态覆盖

本节记录本轮新增的本地证据。输入市场库以只读方式打开；两份财务输出都是新建的隔离 DuckDB
侧车，脚本拒绝覆盖已有输出。没有写 NAS、生产数据库、marker、模型工件或容器。

### 阶段一 §1：已完成的局部 PIT 核对

`scripts/build_financial_pit_research_panel.py` 复用 `financial_pit` 的公告时点契约，以
`ann_date <= daily_bars.date` 选择最近可用财务快照；`end_date > ann_date`、无效或缺失日期的
快照不会参与 join，而是计数并带样例写入 manifest。输出按 `(symbol,date)` 唯一，附来源行数、
输入哈希、键哈希、匹配完整度和同日公告精度边界。

| 侧车 | 日线键 | 命中快照 | 完整财务行 | 未命中 | 未来公告 / 报告期 | 同日公告（仅日期精度） |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2025 | 1,245,745 | 1,245,249 | 1,239,093 | 496 | 0 / 0 | 14,693 |
| 2026 年至 9 月 | 949,026 | 948,928 | 943,234 | 98 | 0 / 0 | 10,970 |

两份侧车均拒绝 1 条源异常（`603400`：报告期 `2026-06-30` 晚于公告日 `2026-04-22`）。
输出 `financial_pit_2025_panel_20261010.duckdb` 与 `financial_pit_2026_panel_20261010.duckdb`
各自 0 个重复证券日键。**局部结果不能外推为完整 PIT 通过**：同日公告无法证明当日尾盘之前
已发布；尚未把该侧车替换接入 300/100/50 完整历史候选重放；`float_market_cap`、证券状态、
资金流和龙虎榜也不由这两个财务快照表证明。

### 当前全市场数据不足与本地准备

只读就绪报告 `artifacts/research/tail_data_readiness_full_market_20261010.json` 为
`insufficient`，没有 hard blocker，但有两项不可用于通过资格：

- `float_market_cap` 在 2,611 个交易日中有 80 天众数占比超过 50%，最坏为 100%，众数是供应商
  占位值 120 亿元。此类日期上的市值硬门和特征不能当作有效观测。
- `ref_security_status` 为 7,061 行、5,911 只证券，`coverage_complete` 行占 83.713%。
  旧的全市场 `namechange` 请求达到 10,000 行上限，不能据此认定未返回的改名/ST 历史不存在。

为解决第二项，工作树新增 `scripts/collect_tail_security_status.py`：重新拉取 Tushare
`stock_basic` 的 L/D/P 三类范围，再逐证券调用支持 `ts_code` 的 `namechange`；每只证券有独立
checkpoint、响应哈希、完整/不完整状态，采集可续跑。导入器将空结果成功也写成逐证券覆盖标记，
就绪审计在有标记时要求全体证券均有完整标记。模拟 API 与 fail-closed 行为已定向测试；截至本节记录时，
**真实采集尚未运行**，本地没有可用 Tushare token / NAS 采集会话，不能把它写成数据已补齐。后续 NAS 采集与复核见 §10、§13。

阶段二 §1 的宽池逐层消融仍卡在标签输入：现有月度请求集有 3,705–3,897 只证券，但分钟参考库
中的 `adj_factor` 每月仅覆盖其中约 700 只。严格 RAW 标签规则下，不能用宽池旧标签冒充可比较
的 RAW 标签，因此未运行或报告完整链路消融。

### 本节证据与验证

- `artifacts/research/financial_pit_2025_panel_20261010.duckdb`
- `artifacts/research/financial_pit_2026_panel_20261010.duckdb`
- `artifacts/research/tail_data_readiness_full_market_20261010.json`
- `scripts/build_financial_pit_research_panel.py`、`tests/test_build_financial_pit_research_panel.py`
- `scripts/collect_tail_security_status.py`、`tests/test_collect_tail_security_status.py`
- `scripts/sync_tail_reference_data.py::security_status_frame_from_payload`、
  `src/stock_analyzer/research/trend_data_readiness.py::_audit_security_status_intervals`

当前阶段仍是：局部财务 PIT 侧车已生成；逐证券状态采集器已实现但未采集；完整候选链路 PIT 和
宽池逐层消融未完成；2025/2026 两段模型实验没有合格候选。阶段三独立确认与阶段四影子观察
仍未开始，且依赖候选通过和未来新样本。

## 9. 2026-10-10 后续：宽池 RAW 因子覆盖审计

为确定宽池逐层消融的补数范围，`scripts/audit_tail_pool_adj_factor_coverage.py` 以只读方式
读取三份已冻结月度请求文件、排序 v4 因子子库和独立交易日历 / 上市日证据。对每只证券分别
从其最早请求入场日前 31 个自然日统计到契约退出窗口末日；只有上市日证据完整时，才将确证的
上市前因子前缀排除。审计报告绑定请求文件哈希、数据库大小和修改时间，逐证券 CSV 列出缺失日，
输出目录已存在时拒绝覆盖。

| 请求组 | 请求证券 | 因子窗口 | 窗口内完整证券 | 不完整证券 | 预期证券-交易日 | 已覆盖 | 覆盖率 |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 2026-03 | 3,897 | 2026-01-31 至 2026-04-26 | 698 | 3,199 | 198,141 | 36,644 | 18.4939% |
| 2026-04 | 3,768 | 2026-03-02 至 2026-05-26 | 698 | 3,070 | 211,493 | 40,458 | 19.1297% |
| 2026-05 | 3,705 | 2026-04-06 至 2026-06-24 | 698 | 3,007 | 191,742 | 36,981 | 19.2869% |

三组不完整证券并集为 3,526 只。因子源使用修复证券代码前导零后的排序 v4 子库，`ref_adj_factor_history`
含 78,622 行 / 702 只证券；没有把旧 `tail_minute_bars.duckdb` 中证券代码被整数化的因子表当作真值。
旧版审计曾将整组最早起始日套给每只证券，结果已由 `tail_pool_adj_factor_audit_20261010_v2/`
替代；`v3/` 在相同覆盖计算上额外生成了可核验的合并采集输入，以下以 v3 为交付目录。

合并采集包列出 3,526 只不完整证券、157,075 条这些证券对应的原请求记录，以及
2026-01-31 至 2026-06-24 的采集范围；scope manifest 固定请求哈希、证券清单哈希、日期范围和
采集器身份。它可以直接交给 `collect_adj_factor_history.py`，再由
`sync_tail_adj_factor_history.py` 导入新的研究数据库。准备包不是采集成功证明；当前本地没有
Tushare token，且机器配置中没有可用的 NAS SSH / Docker 连接，因此没有发请求或访问生产环境。

为避免长批次中断后重发几千次 API 请求，`collect_adj_factor_history.py` 现支持逐证券原子 checkpoint：
checkpoint 绑定开始 / 结束日期、证券集合、交易日历哈希和采集器文件哈希；成功响应（包括明确不完整的
响应）可在重启后复用，网络/API 错误会自动重试，`--retry-incomplete` 只重试不完整响应。改动后的
采集器定向测试通过。导入器按证券校验和保留响应 `as_of`，报告混合批次时不会把旧响应时间伪装成
新时间。checkpoint 目录必须按采集范围独立使用；若要重新拉取完整范围，应新建目录。

这是覆盖范围核算，不是数据采集或质量结果；没有改研究数据库、没有重建新的宽池 RAW 标签，
也没有运行逐层宽池消融。后续应按每组报告的
`collector_start` / `collector_end` 和对应缺失证券清单采集并导入新的隔离研究库，再逐日复核覆盖后
才重建标签。阶段二 §1 仍为阻塞；阶段三、四仍未启动。

### 本节证据与验证

- `artifacts/research/tail_pool_adj_factor_audit_20261010_v3/tail_pool_adj_factor_coverage_report.json`
- `artifacts/research/tail_pool_adj_factor_audit_20261010_v3/tail_pool_adj_factor_coverage_{mar,apr,may}.csv`
- `artifacts/research/tail_pool_adj_factor_audit_20261010_v3/tail_pool_adj_factor_missing_symbols_{mar,apr,may,union}.txt`
- `artifacts/research/tail_pool_adj_factor_audit_20261010_v3/tail_pool_adj_factor_union_requests.jsonl`、`tail_pool_adj_factor_collection_scope.json`
- `scripts/audit_tail_pool_adj_factor_coverage.py`、`tests/test_audit_tail_pool_adj_factor_coverage.py`
- `scripts/collect_adj_factor_history.py`、`tests/test_collect_adj_factor_history.py`
- `scripts/sync_tail_adj_factor_history.py`、`tests/test_sync_tail_adj_factor_history.py`
- `artifacts/research/tail_rebuild_trade_status_source_gap_countercheck_2026_v9_20261011.json`、`tail_rebuild_trade_status_source_gap_countercheck_2025_v2_20261011.json`
- 定向审计测试通过；Ruff 定向检查通过；`git diff --check` 通过。未访问 NAS、未执行 Tushare 采集、未部署。

## 10. 2026-10-11 后续：NAS 补数、ST 修正与宽池 RAW 标签重建

本节更新 §7–9 中“无 NAS 采集”的旧状态。用户已授权使用 NAS 现有 Tushare 凭证采集所需历史数据；
凭证只在 NAS 运行环境读取，没有复制到本地或写入研究文件。本轮没有修改 NAS 生产数据库、服务配置、
marker、模型工件或容器镜像，也没有重启或部署生产服务。所有导入和标签重算均在本地新的隔离研究库上做。

| 工作项 | 截至本次记录的证据 | 状态与边界 |
| --- | --- | --- |
| ST 日快照与回放 | 58/58 个决策日的 Tushare 全市场 ST 快照已采集并校验哈希；`replay_st_corrected_v2/` 重放 58 日、输出 58 份日事实与 trace。旧回放中 2,397 个被来源确认的 ST 证券日曾推进，修正回放为 0；请求由 196,976 改为 195,077 | ST 覆盖修正完成。只证明 `universe` 与 `hard_eligibility` 两层；尚无完整 300/100/50 预测层的逐层历史 trace，不能声称完成全漏斗归因 |
| RAW 复权因子 | 92 个交易日全市场批次 324,385 行；3,519/3,526 只证券完整。仅 7 只在 2026-02-03 缺因子，独立 Tushare 停复牌记录均确认当日停牌。另为 26 只修正后请求池证券采集 2,028 行，26/26 完整并已导入本地研究库 | 参考数据已按来源分清；未对停牌日合成因子或价格 |
| 宽池 RAW 标签 | v2 的 1,197 条参考不足经 NAS 补采修复；v3 对 195,077 条请求全部产出标签、`insufficient=0`、`reference_gap_records=0`。标签含 179,684 笔成交、179,489 条成熟可训练记录、70,925 个净盈利标签；15 个 `trade_status_source_gap` 请求经逐证券-日反查，0 个影响成熟可训练标签（14 个缺口日在退出成熟后，1 个请求未成交） | RAW 参考缺口关闭；21 条公司行动金额/比例不确定、2 条分数股份不确定、172 条停牌/分钟冲突继续保持不确定，不计作已实现标签。15 条状态来源诊断单独留档，不把它们冒充参考缺口或盈利标签 |
| 历史证券状态 / 名称变更 | NAS 逐证券 `stock_basic(L/D/P)` + `namechange` 采集完成：5,912/5,912 完整，0 个不完整；14,226 条名称记录。5,912 份逐证券 checkpoint 响应 SHA 全部与载荷索引核对通过 | `NaN` 空结束日期识别修复后续跑成功。26,050 行状态载荷已导入单独的 v4 隔离库；339 只退市、5,573 只上市、5,912 只名称覆盖状态均完整，`coverage.gaps=[]`。原始 payload、checkpoint bundle、manifest 与 import report 已归档 |
| 阶段二固定模型实验 | v3 样本已组装为 195,077 条；固定 12 臂矩阵已运行并完成报告，但 10 个价格臂均因只有 58 个决策日（四折最低需 71 日）阻塞，资金流/龙虎榜 2 臂因 PIT 未证实阻塞。既有 2025 v2 和 2026 v9 矩阵也均无合格候选 | v3 `candidate_selection=no_qualified_candidate`、`release_eligible=false`；完整漏斗反事实仍缺 300/100/50 逐层历史 trace。历史开发窗口不可作为独立确认 |
| 阶段三独立确认 | 尚无通过开发门并冻结的候选；2025/2026 历史已查看 | 未启动。需要先有合格冻结候选，再用未开封的新窗口满足四折、至少 120 个完整交易日、每臂至少 300 笔成熟成交及功效门 |
| 阶段四未来影子 | 没有候选通过阶段三，也没有启动正式影子观察 | 未启动。必须在阶段三通过后真实累积至少 60 个完整交易日、100 笔成熟模拟成交和预登记终点证据；本轮没有部署 |

### 本节验证与产物

- Tushare 原始 ST 快照：`artifacts/research/tail_nas_collection_20261011_v1/security_status_st_snapshots/output/`；
  修正回放：`artifacts/research/tail_nas_collection_20261011_v1/replay_st_corrected_v2/`。
- 复权因子日批次与 26 证券补充包：`artifacts/research/tail_nas_collection_20261011_v1/adj_factor_daily_batch_v2/`、
  `artifacts/research/tail_nas_collection_20261011_v1/adj_factor_corrected_pool_26_2026mar_jun/`。
- 隔离重建库：`artifacts/research/tail_nas_collection_20261011_v1/tail_rebuild_corrected_2026mar_may_v2.duckdb`；
  输入请求：`artifacts/research/tail_nas_collection_20261011_v1/replay_st_corrected_v2/requests.jsonl`。
- 标签缺口基线：`artifacts/research/tail_nas_collection_20261011_v1/label_report_corrected_v2.json`、
  `label_gaps_corrected_v2.jsonl`；v2 已完成计算但明确 BLOCKED，缺口请求文件已送至 NAS 启动补采。
- 缺口采集和导入证据：`artifacts/research/tail_nas_collection_20261011_v1/trade_refs_label_gap_repair_v1/`
  （98/98 完整，来源 SHA 核验通过，import report 的 `coverage.gaps=[]`）；v3 独立数据库为
  `tail_rebuild_corrected_2026mar_may_v3.duckdb`。标签报告 `label_report_corrected_v3.json` 显示
  195,077 条请求、0 条参考不足；`label_gaps_corrected_v3.jsonl` 为空。状态 gap 逐项交叉核对在
  `label_status_gap_countercheck_corrected_v3.json`，对 15 个请求确认成熟可训练标签受影响数为 0；再对这 15 条单独运行新版 `--status-gaps` 导出，`status_gaps_detail_corrected_v3.jsonl` 有 15 条逐日明细且 `status_gap_detail_report_corrected_v3.json` 仍为 0 个参考缺口。
- 样本组装报告为 `samples_report_corrected_st_v3.json`（195,077 样本、179,489 可训练）；固定 12 臂
  Stage 2 开发矩阵报告为 `stage2_ablation_corrected_st_v3.json`（10 个价格臂因 58 < 71 个所需决策日阻塞，2 个事件特征臂因 PIT 阻塞；`no_qualified_candidate`，`release_eligible=false`）。
- 历史证券状态补采证据：`artifacts/research/tail_nas_collection_20261011_v1/security_status_namechange_v1/`
  （5,912 份原始逐证券响应哈希通过，14,226 条 namechange，0 个不完整）；状态行导入报告为
  `security_status_import_report.json`，数据库为独立副本
  `tail_rebuild_corrected_2026mar_may_v4_security_status.duckdb`。v3 标签重建仍在运行，未与其并发写库。
- 本轮数据采集 / 导入定向 pytest 共 51 项通过；修复空结束日期识别后，名称状态采集器测试另有 6 项通过，
  对应两个 Python 文件 Ruff 检查通过；`git diff --check` 通过。研究数据库源库只读；
  未写 NAS 生产状态、未部署、未重启服务。
- 为使交易状态来源缺口可逐条审计，`rebuild_tail_labels.py` 新增独立 `--status-gaps` JSONL 输出，不与参考数据不足清单混并；`tests/test_tail_rebuild.py` 定向 25 项通过，两个修改文件 Ruff 检查通过。

当前级别：阶段一的 ST / 因子补数、证券历史状态覆盖、RAW 参考数据修复和 v3 RAW 标签重建已有本地隔离证据；阶段二固定矩阵已执行但修正窗口过短、所有候选臂均阻塞。
阶段二完整逐层归因未完成，且没有合格模型候选；阶段三与阶段四仍未启动。因此本轮结果
不构成 Freeze Ready、Production Ready 或已部署生产。

### 2026-10-11 回归复核

- 针对本轮修改的 34 个测试模块完成回归，全部到达 100%，无失败；覆盖 RAW 标签 / 账本、参考数据采集与导入、PIT 就绪、模型工件、背景数据 provider 和验证器。
- 标签策略 v5 的拒绝原因现使用准确的 v5 错误名。TX 日线回退仍把缺少 PIT 财务和背景值标成缺失；背景数据只在该交易日实际有返回行时标为完整。
- `.venv/bin/ruff check` 对所有本轮修改的 Python 文件通过。全量 `src tests` 检查报 48 条，分布于 24 个未修改文件；没有把无关格式问题混进本次改动。`make lint` 因 shell PATH 没有 `ruff` 命令而未启动，已直接运行项目虚拟环境里的 Ruff。
- 两份状态缺口 countercheck 已只读重算；各自请求数和旧报告请求数一致，唯一证券 / 证券-交易日数分别为 3 / 5 与 1 / 4。研究数据库大小与修改时间在复核前后一致。
- `git diff --check` 通过。没有访问 NAS、执行 Tushare 采集、写生产数据库、部署或重启生产服务。

## 11. 2026-10-11 后续：RAW 零成交占位与公司行动逐条补证

### RAW 分钟退出规则复核

172 条 `suspension_minute_bar_conflict` 请求对应 187 个证券-交易日；每个日期都有独立
`tushare_suspend_d` 停牌声明，分钟来源为 241 行 `vendor_zip`，每行成交量都是显式数值零且
OHLC 恒定，没有正成交量 bar。`raw_1m_company_action_ledger_v4` 只在这组条件同时成立时把分钟行
视为供应商占位，跳过该日期；缺量、非法值、非有限值或正成交量仍阻塞。172 请求的定向重建产出
119 条成熟可训练标签、53 条因超过最多顺延范围而 `exit_not_executable`，没有把未成交强行记成亏损。

195,077 请求的完整 v4 标签重建已完成：无参考缺口，179,608 条成熟可训练、70,959 条净盈利；
21 条公司行动金额/比例未知、2 条分数股份、53 条超出允许延期范围的退出仍保持不确定。全量结果位于
`artifacts/research/tail_nas_collection_20261011_v1/zero_volume_suspension_fix_v2_full/`，旧 v3
标签不能替代 v4。

### 官方公司行动证据

Tushare 空字段没有被全局改成 0。原先 21 条金额或股份比例不确定请求覆盖 10 个已实施公司行动；
10 个事件均已找到 CNINFO 官方公告，确认现金分红为零并确认股份转增比例；另核对 21 条请求的入场日
均不晚于登记日且早于除权日。原 Tushare 值、审阅清单、PDF 字节数 / SHA-256、官方来源 URL 和逐条复核结果均保存在
`artifacts/research/corporate_action_official_evidence_20261011_v1/`。reconciler 校验原始事件 CSV
哈希、每条事件键和原字段、官方 HTTPS 主机、文档日期、PDF 文件签名及 SHA-256 后，生成单独的新 CSV；
来源 CSV 和 NAS 数据未修改。`688448.SH` 的 0.35 股转增有两个供应商公告日记录，较早一行作为
审阅过的重复别名从 reconciler 输出排除，别名原因和 canonical 行仍有审计记录。

10 个修正事件已导入新的 6.0 GiB 隔离研究库副本；原始 v3 库和生产环境保持不变。21 条对应请求在
修正后全部成为成熟可训练标签（其中 4 条净盈利），每条账本只应用一笔官方来源公司行动；更新后全量
v5 为 179,629 条成熟可训练、70,963 条净盈利，仍有 53 条退出不可执行和 2 条分数股份不确定。
证据文件：`artifacts/research/tail_nas_collection_20261011_v1/official_action_fix_v1/official_action_import_report.json`、
`label_report_official_action_countercheck_v1.json`、`label_overlay_merge_report_v1.json`、
`samples_report_corrected_st_v5.json` 和 `stage2_ablation_corrected_st_v5.json`；官方材料与 21 请求登记日核对见
`artifacts/research/corporate_action_official_evidence_20261011_v1/`。

以 v5 样本重新运行阶段二后，12 臂全部 blocked：10 个价格臂因只有 58 个决策日、少于四折最低 71 日，
2 个资金流/龙虎榜臂因 PIT 时点未证实；`candidate_selection=no_qualified_candidate`、
`release_eligible=false`。28 个 reconciler、参考数据导入和重复披露去重定向测试通过；四个相关 Python
文件的 Ruff 检查与 `git diff --check` 通过。

### 历史 300/100/50 漏斗可重建性

核对了全部 58 份修正回放 trace：它们只记录 `universe`、`hard_eligibility` 两层，最终推荐均为空；
195,077 条回放请求只保留特征快照和证券/日期，没有当时模型分数或模型身份；归档中没有
`quality_300` / `light_100` / `deep_50` 成员或报告文件。用新训练模型重跑会改变历史实验身份，不能伪称
重建出当时的 300/100/50 决策。逐文件哈希和输入字段审计在
`artifacts/research/tail_nas_collection_20261011_v1/official_action_fix_v1/historical_funnel_reconstructability_audit.json`；
这部分仍需未来生产 trace 从起点完整留档后才能验收。

### 2025 新标签上的阶段二重跑

2025 全量 v3 重建已结束：72,600 条请求、0 个参考缺口、62,949 条成熟可训练、27,658 条净盈利；
43 条仍不确定（7 条公司行动金额/比例未知、12 条实施状态未确认、24 条退出超出允许延期范围），
17 条停牌未成交。另有 4 条交易状态来源缺口诊断均为 `000878` 的 2025-05-13 至 05-16，晚于各自
4 月 23 日至 28 日的标签成熟日；逐请求核对结果 `status_gap_maturity_countercheck_raw_2025_v3.json`
显示 0 条成熟标签与状态缺口重叠。2025 新样本上的固定 12 臂矩阵为 1 臂完整计算、9 臂因校准窗 raw AUC < 0.5
停止训练、2 个资金流/龙虎榜臂因 PIT 可用时点未证明而阻塞。

唯一完成计算的臂 `drop_market_relative__logistic_baseline` 在 175 个配对交易日、4 折中有 230 笔
成熟成交；同池容量基线为 525 笔。命中率差 +5.81pp，但交易日配对移动块 bootstrap 95% 区间为
[-7.65pp, +22.00pp]，跨过 0，且处理臂成熟成交低于固定 300 笔门槛。因此质量状态为
`sample_insufficient`，12 臂 `candidate_selection=no_qualified_candidate`，`release_eligible=false`。
该探索结果不能用于阶段三或生产发布；本次运行检测到 LightGBM 4.7.0 可用，但不改变上述结论。
标签、样本和矩阵报告分别是 `artifacts/research/raw_exit_zero_volume_fix_2025_v1/label_report_raw_2025_v3.json`、
`samples_report_raw_2025_v3.json` 和 `stage2_ablation_raw_2025_v3.json`。

该修改只影响本地隔离研究输入。未访问或写入 NAS 生产数据库，未部署、未重启容器，也没有修改模型、
生产 marker 或历史 artifact。阶段二仍无合格候选，阶段三独立确认和阶段四未来影子观察依旧未开始。

### 2026-10-11 资金流 / 龙虎榜历史来源时点审计

本地 `moneyflow_2025.csv.gz` 与 `top_list_2025.csv.gz` 的日期集合均与 2025 开放交易日历完全相等（243 日），报告中无失败日、
空日或可能截断日。独立逐行核验发现：资金流共 1,248,109 行，`(trade_date, ts_code)` 无重复；龙虎榜共
18,033 行中有 1,616 个重复股票日键（1,681 条多余行；1,609 个键有不同上榜理由、7 个键是同一理由重复），
构造 `top_list_count_1d_lag` 时须按唯一股票日去重。

这只证明当前文件和报告的行覆盖，不证明值在 2025 决策时点已经可得。两份采集报告没有逐日请求时间和响应
SHA-256；Tushare 官方 [`moneyflow`](https://tushare.pro/document/2?doc_id=170) 与
[`top_list`](https://tushare.pro/document/2?doc_id=106) 接口文档没有提供可核实的历史版本 / 发布截止时点。
资金流缺少股票日的语义同样未证实，不得把缺行填成零。故 2025 正式阶段二的两条事件特征臂继续阻塞；简单
滞后一交易日无法恢复未留存的历史源版本。

可复跑审计：`python3 scripts/audit_tail_event_feature_provenance.py --out <new-path>`；本次报告为
`artifacts/research/tail_event_pit_provenance_audit_20261011_v3.json`。脚本校验原文件 SHA-256、报告行数、日期数、
缺失 / 截断声明及重复键，但不会将本地文件 mtime 当成供应商采集时间，也不会自动批准 PIT。

## 12. 2026-10-11 后续：宽池 adj_factor 补采与全市场端点复核

用户授权从 NAS 现有 Tushare 凭证采集。生产状态先做只读检查：NAS HEAD 为
`2b0ab092d7bda43a2b8dd2a9df3faeba6fcca4c8`，api 与两个 scheduler 正运行同一
`stock-analyzer:latest` 镜像；本轮没有写生产目录、生产数据库、marker 或 artifact，也没有重启服务。
凭证仅由隔离的一次性研究容器从 NAS 上既有采集容器环境读取到进程内存，没有导出到本机或落盘。

对 3,526 只宽池证券按 2026-01-31 至 2026-06-24 逐证券采集，共返回 324,385 行；3,519 只证券
完整，7 只不完整，且都只缺开市日 2026-02-03：`002462`、`300666`、`300912`、`601969`、
`603616`、`603980`、`688165`。为排除逐证券接口行为造成的假缺失，又按 Tushare `adj_factor`
全市场日查询模式对 2026-02-03 单独复核。完整返回 5,465 只证券、无截断、无接口错误，但 7 只目标证券
仍全部不存在。响应 SHA-256 和采集器 / 镜像身份记录于
`artifacts/research/tail_pool_adj_factor_audit_20261011_v1/collection_attempt_missing7_trade_date_20261011_v1/`；
接口定义见 [Tushare adj_factor 文档](https://tushare.pro/document/2?doc_id=28)。因此这不是缺 token 或全市场批次截断。

独立参考数据确认 7 只证券在 2026-02-03 均有完整 `tushare_suspend_d` 全天停牌状态，市场日历为开市日，
无 RAW 日线、当日公司行动覆盖完整且无行动；2 月 2 日与 2 月 4 日因子值相同。没有合成或沿用因子值。
按现行契约，缺失因子仍保持阻塞；跨日相等与停牌证据没有被擅自转成覆盖豁免。

以已导入的研究库重审月度请求：3 月 161,399 / 161,497 个预期证券日可用（99.9393%），4 月
171,035 / 171,035（100%），5 月 154,761 / 154,761（100%）。3 月审计中 98 个不完整证券日，来自
这 7 只证券在 14 个预期日期上的行级 `coverage_complete=false` 标记；供应商响应实际缺失的是每只证券
一个 2 月 3 日因子。该差异不能靠改审计计数掩盖，需先按现行契约完成覆盖核实或另行审议带证据的适用性规则。

### 本节证据与状态

- `artifacts/research/tail_nas_collection_20261011_v1/adj_factor_daily_batch_v2/`：3,526 证券逐证券采集输入、CSV、报告与 checkpoint。
- `artifacts/research/tail_pool_adj_factor_audit_20261011_v1/tail_pool_adj_factor_coverage_report.json` 及 `tail_pool_adj_factor_coverage_{mar,apr,may}.csv`：使用更完整研究库后的覆盖审计。
- `artifacts/research/tail_pool_adj_factor_audit_20261011_v1/collection_attempt_missing7_v1/collection_evidence_manifest.json`：逐证券补采和独立停牌 / 公司行动核对。
- `artifacts/research/tail_pool_adj_factor_audit_20261011_v1/collection_attempt_missing7_trade_date_20261011_v1/trade_date_probe_evidence_manifest.json`：全市场日端点复核及响应哈希。
- 逐证券批次为 3,519/3,526 完整；3 月仍在现行合同下 blocked；阶段二仍无合格候选，阶段三独立确认及阶段四影子观察尚未开始。

## 13. 2026-10-11 后续：证券名称公告时点落库与重叠区间复核（初始 v5 状态）

> 本节保留 v5 首次导入时的原始审计状态；证券名称区间的证据化整理和市值参考数据补齐
> 已在 §14 完成，当前判断以 §14 的 v6/readiness 报告为准。

根据用户授权，本轮直接 SSH 到 NAS 做只读检查：生产 HEAD `2b0ab092d7bda43a2b8dd2a9df3faeba6fcca4c8`；
`api`、`scheduler-critical`、`scheduler-heavy`、`redis` 均为 running，三个应用容器使用同一
`stock-analyzer:latest` 镜像和仓库规定的五个 compose 文件。本次只读取状态，没有启动采集容器、读取或导出
Tushare token、写 NAS 文件/数据库、部署或重启服务。此前在 NAS 完成的历史证券状态采集已经归档于 §10。

为避免把 Tushare `namechange.ann_date` 丢掉，本地工作树现将公告日与名称生效区间分开保留：
`security_status_frame_from_payload()` 写入 `announcement_date`；`ref_security_status` 对旧研究表作向前兼容迁移；
审计分别检查公告日缺失、公告日晚于生效日和区间重叠。旧 v4 研究副本未覆盖；在它的独立副本上重新导入
原始全市场 payload，生成 v5 PIT 审计库。

| 检查项 | 当前证据 | 状态 |
| --- | --- | --- |
| 全市场采集完整性 | 5,912/5,912 个证券有完整 namechange 覆盖标记；26,050 行；5,912 个证券 | 覆盖采集完成 |
| 公告日期持久化 | `ann_date` 原值映射为 `announcement_date`，v5 重导入保留公告日；旧表迁移测试覆盖保留既有数据 | 代码与定向测试完成 |
| PIT 区间审计 | 全市场报告 `tail_data_readiness_full_market_20261011_post_namechange_pit_v5b.json`：缺公告日 1 行、公告日晚于生效日 0 行、重叠 11 对；全市场覆盖标记 100% | `security_status_intervals=insufficient`，尚未 canonicalize |
| 全市场市值数据质量 | 同一报告仍有 80/2,611 个交易日的 `float_market_cap` 众数占比超 50%，最差 100%，众数为 120 亿元 | `column_concentration_float_market_cap=insufficient` |

当前不删除、不合并任何原始名称事件。重叠集中于 6 个证券（300132、301339、600388、600768、603608、601368）；
另有 689009.SH 一条 namechange 记录缺 `ann_date`。事件中既有拟用简称的公告记录，也有实际生效日期记录；
在逐条保存交易所/公司正式公告、计算证据哈希并生成可复跑的审阅清单前，不能仅凭行形状推断应删除哪条或把
公告时间改成生效时间。v5 库保留原始来源，未用于收益评估。

### 本节代码、产物与验证

- 代码：`scripts/sync_tail_reference_data.py`、`src/stock_analyzer/research/tail_reference_store.py`、
  `src/stock_analyzer/research/trend_data_readiness.py`。
- 测试：`tests/test_collect_tail_security_status.py`、`tests/test_tail_reference_store.py`、
  `tests/test_trend_data_readiness.py`。
- 原始 NAS 载荷与来源 manifest：`artifacts/research/tail_nas_collection_20261011_v1/security_status_namechange_v1/`；
  公告日重导入报告 `security_status_announcement_date_reimport_report.json`；v5 隔离库
  `tail_rebuild_corrected_2026mar_may_v5_security_status_pit.duckdb`。
- 全市场最新审计：`artifacts/research/tail_data_readiness_full_market_20261011_post_namechange_pit_v5b.json`，退出码 3，
  readiness=`insufficient`、无 hard blocker；另有 `security_status_intervals` 与 `float_market_cap`
  两项不足。
- 三个定向测试模块、Ruff 定向检查和 `git diff --check` 已通过；未执行完整研究重放、阶段三/四或部署。

本节记录的当时状态：公告日已进入本地 schema，但初次导入尚有 11 对重叠和 1 条公告日缺失；市值集中问题尚未接入独立参考值。该状态已由 §14 的公告证据重整与参考市值审计更新。

## 14. 2026-10-11 后续：证券状态 PIT 证据化与 NAS 市值补采

本节是当前数据状态的权威记录。用户明确授权后，直接在 NAS `api` 容器内运行已有
Tushare 采集器；凭证只由容器进程从 `SA__MARKET_WAREHOUSE__TUSHARE_TOKEN` 读取，未打印或复制。
采集结果只写入容器 `/tmp`，复制到本地隔离研究目录；没有改生产数据库、marker、模型工件、服务配置，
也没有部署或重启任何服务。

### 证券名称 PIT 整理

原始全市场 `security_status.json` 与 collection manifest 保持不变，并以 SHA-256 固定来源。新增
`scripts/reconcile_tail_security_status.py` 只接受逐行来源选择器、官方证据文件与哈希均匹配的复核清单：
移除 5 条被正式生效公告证明为拟用名/重复状态的重叠记录，移除 601368 仅指向法定名称变更、并非股票简称变更的重复事件；对 689009.SH 的缺失公告日，仅按上交所上市公告补入 2020-10-28。所有原始行和决定仍留在审计报告中，未改写原始载荷或 v5 数据库。

| 检查项 | 当前证据 | 状态 |
| --- | --- | --- |
| 原始覆盖与来源完整性 | 5,912/5,912 证券；26,050 原始行；原始 payload SHA-256 `fae9fbfd…2a48` | 原始来源固定、保留 |
| 证据化区间复核 | 7 条逐项决定；正式公告文件及 hash-bound manifest；canonical payload SHA-256 `2165b303…841c` | 完成 |
| v6 独立副本导入 | `ref_security_status` 26,044 行、5,912 证券，覆盖标记 100% | 完成 |
| PIT 审计 | 缺公告日 0、公告日晚于生效日 0、重叠样例为空 | `security_status_intervals=ok` |

### 流通市值真值补采与审计

NAS API 容器从 Tushare `daily_basic.circ_mv` 补采 2026-07-18 至 2026-09-30 的 52 个交易日，
共 288,177 行，每日 5,523–5,561 行；无失败日、空 `circ_mv`、可能截断日。单位按 Tushare 定义
从万元乘 10,000 换算为元。原始 `daily_bars` 与原 `float_cap_reference.duckdb` 均未修改；先克隆参考库，
再将新行追加到独立 v2 研究库。

- 新参考库覆盖 2025-01-02 至 2026-09-30，共 424 个交易日、2,310,122 行、5,573 只证券。
- 2026-07 至 09 与仓库可对账 281,798 个证券日；非占位值的逐月一致率均为 100%，未匹配仓库行 0。
- 对 `market_copy.duckdb` 运行全量只读 readiness，显式加载 v6 证券状态库、分钟研究库及 v2 市值参考库：2,611 个日线交易日，流通市值替换匹配 2,194,177 行；覆盖后超过 50% 众数占比的交易日为 0，`blocking_gaps=[]`、`insufficient_items=[]`、`readiness=ready`。
- 此 `ready` 表示当前审计项（覆盖、分钟窗、参考数据、证券状态和市值集中度）通过；不证明收益、不证明生产运行路径已接入 v2，也不证明 `daily_basic` 历史值在每个尾盘决策时点可用。市值参考表没有逐交易日发布快照，因此只用于占位/分布核对，不作为 PIT 认证；原始 `daily_bars` 未就地修补。

### 当前仍未完成的验收门

| 项目 | 当前状态 | 不能据此宣称完成的原因 |
| --- | --- | --- |
| 流通市值特征 PIT | 未认证 | 新补采 `daily_basic` 是事后历史查询，未保留当日发布版本/可用时刻。若作为 14:30–14:50 决策输入，需证明可得时间或改用此前已发布值后再重放。 |
| 宽池 RAW 标签的 `adj_factor` | 三个请求组的有效覆盖均为 100%；3 月仍有 7 个原始缺行，但均由严格证据判定为停牌日不适用 | 原始因子行仍不存在，未合成或沿用邻日值。7 个缺口均有完整全天停牌状态、无 RAW 日线/分钟、完整无行动覆盖、前后有效因子相等；任何一项证据不满足仍阻塞。纠正后的审计输出为 3 月 raw missing=7 / suspension exempt=7 / effective missing=0，4 月和 5 月均无缺行。 |
| `moneyflow` / `top_list` 历史 PIT | 当前历史文件覆盖存在，正式事件特征臂仍阻塞 | 没有逐日原始响应时间、响应哈希或当时发布版本；事后当前历史值不能证明 2025 决策时点可用，资金流缺行也不能当零。 |
| 全部硬门/特征字段 PIT | 未完成 | 财务侧车、证券状态和部分行情已有局部证据，但旧完整选股链路尚未逐字段记录来源、采集/发布时刻和决策 cutoff；不能把单列/单源复核外推到全链路。 |
| 历史 `quality_300` / `light_100` / `deep_50` 反事实 | 不可从现存历史重建 | 58 份 trace 只有 `universe`、`hard_eligibility`，没有当时模型分数/身份与三层成员；用新模型重放会改变实验身份。需要未来生产逐层留档后做同日对照。 |
| 阶段二候选 | 2025、2026 历史开发矩阵均 `no_qualified_candidate` | 当前收益/成本/校准验收未通过，不能用参数调整或数据 readiness 代替收益证据。 |
| 阶段三独立确认 | 未启动 | 没有冻结的阶段二候选；已查看的 2025/2026 历史不能再作独立窗口。 |
| 阶段四影子观察 / 上线 | 未启动 | 必须先通过阶段三，再累计至少 60 个完整交易日、100 笔成熟模拟成交并形成账户净值证据；本轮未部署。 |
| 生产夜扫 5 层真实留档 | 等待下一次实际调度 | 当前本地代码/历史报告不能替代部署修复后的真实夜扫报告；需验证 5 层齐全且 `verify_trace()` 通过。至少还要持续积累计划要求的观察期，才能验收夜扫链路稳定性。 |

### 本节可复核产物

- 名称证据清单：`artifacts/research/tail_nas_collection_20261011_v1/security_status_namechange_v1/official_namechange_evidence_manifest.json`；canonical payload 与报告：同目录 `reconciled_v1/`。
- v6 隔离库：`artifacts/research/tail_nas_collection_20261011_v1/tail_rebuild_corrected_2026mar_may_v6_security_status_reconciled.duckdb`。
- 市值原始数据、采集报告、独立研究库、导入报告、完整哈希 manifest 与 readiness 报告：`artifacts/research/float_cap_reference_20261011_v2/`。
- readiness 报告：`trend_data_readiness_post_cap_reference.json`；哈希 manifest：`float_cap_collection_manifest.json`，SHA-256 `f6fd0ee8…3fee8b`。
- 代码与测试：`scripts/reconcile_tail_security_status.py`、`src/stock_analyzer/research/trend_data_readiness.py`、`scripts/audit_trend_data_readiness.py`、`tests/test_reconcile_tail_security_status.py`、`tests/test_trend_data_readiness.py`。
- 验证：`.venv/bin/pytest tests/test_float_cap_reference.py tests/test_trend_data_readiness.py tests/test_reconcile_tail_security_status.py tests/test_tail_reference_store.py tests/test_collect_tail_security_status.py -q` 退出码 0；相关文件 `.venv/bin/ruff check ...` 通过；`git diff --check` 通过；全量 readiness CLI 退出码 0、报告为 `ready`。

#### 宽池因子审计纠正与停牌日适用性核验

续核发现采集器把“证券请求区间是否完整”复用为每一条有效因子行的 `coverage_complete`。7 只证券各自仅缺
2026-02-03，但其余 91 条经来源响应哈希验证的因子行也被误标为不完整。修复后，行级标志只说明该条观测
本身有效；证券区间完整性仍由 coverage 报告单独表达。迁移脚本只允许在隔离研究库中修正已有的完全相同
证券/日期/因子观测，不增删观测值。此次修正了 637 条已有观测，7 个证券区间仍诚实记录为不完整。

对每个原始缺行，只有同时满足完整全天停牌证据、无 RAW 日线、分钟行数为零、除权日公司行动覆盖完整且无行动、
前后有效因子有限且相等时，才作为该交易日的“不适用”处理。该规则不会补造 `adj_factor`；原始缺行、豁免日、
有效缺口分别列报。把纠正后的隔离库用于三个已冻结请求组后，3 月为 161,497/161,497、原始缺行 7、
证据豁免 7、有效缺口 0；4 月 171,035/171,035；5 月 154,761/154,761。逐证券调用
`corporate_action_window()` 再核 7 个缺口均返回 `coverage_complete=true`、原始行不存在、`adj_factor=null`、
无未解决缺口。数据语义与异常边界已由定向测试覆盖。

该修复关闭的是三个请求组的 `adj_factor` 覆盖门，不是收益门。市值、资金流和龙虎榜的 PIT 仍有缺证；固定实验
只有 58 个决策日，短于四折所需 71 日；2025/2026 开发矩阵均无合格候选。故阶段三独立确认和阶段四影子观察
仍不可启动，Production Ready 与部署状态不变。

本次证据位于：

- 隔离研究库：`artifacts/research/tail_adj_factor_row_coverage_20261011_v1/factor_research.duckdb`。
- 观测行纠正清单：`artifacts/research/tail_adj_factor_row_coverage_20261011_v1/row_observation_reconciliation_manifest.json`，包含输入哈希、逐证券响应哈希、637 行修正数和原始缺口日期。
- 纠正后覆盖报告：`artifacts/research/tail_adj_factor_row_coverage_20261011_v1/audit_corrected_v1/tail_pool_adj_factor_coverage_report.json`，SHA-256 `9f6bc0db78691c46fa38768c846d1d57a939eca29c6d09ebca2618851a8bb469`。
- 原始逐证券与全市场端点证据：`artifacts/research/tail_pool_adj_factor_audit_20261011_v1/collection_attempt_missing7_v1/` 和 `collection_attempt_missing7_trade_date_20261011_v1/`。

当前级别：限定范围的数据就绪审计为 `ready`，证券状态 PIT 重叠已由官方证据清零，市值参考值已隔离补齐至仓库最新交易日；市值特征本身仍未取得历史决策时点 PIT 证明。宽池 `adj_factor` 覆盖门在严格停牌适用性规则下已通过，但收益门未通过且事件特征 PIT 缺证，因此**没有合格模型候选、未进入独立确认、未开始正式影子、未达到 Production Ready、未部署**。


## 15. 2026-10-11 系统错误后的续接：夜扫逐层证据与未完成任务

原会话在上下文压缩请求连接失败后中断。本节接续其最后正在执行的夜扫逐证券原因工作；
§0–§9 是历史时间点记录，阶段一不能因旧段落的“工程通过”被认定为完整 PIT 验收通过。
当前数据证据仍以 §14 为准，当前工程增量与待办以本节为准。

### 本轮实际完成

- Quality 报告记录完整硬门合格池、排序分数及未入选成员；留档从完整上游起算，避免忽略合格池到 300 的截断。
- Light 记录按现有短路顺序实际命中的资格复核原因和排名容量原因；Deep 分别记录快照缺失和排名容量，保留所有已打分候选的真实诊断。
- 新增 `selection_evidence.version=1`，读取生产 `source_report` 包装并检查成员一致性。重复输入、同票多因、晋级/拒绝冲突、非法预测及未知输入拒绝留档，扫描继续回显证据失败。
- 部分归因的余项仍写成 `night_truncation_reason_not_recorded`；视图存在任一未归因淘汰时，`reasons_available=false`。
- Deep 只留真实模型的 raw_blend 和校准 meta；默认 0.5 降级值不当成模型预测。选择公式、阈值与容量不因留档而改变。
- ADR-004 §9、Implementation Locations / As-of、NOTE-002 D34 及知识索引同步更新。

### 实际验证与生产只读核查

1. 六个定向测试文件共 **72 个**测试通过：`test_night_selection_evidence.py`（14）、`test_night_scan_funnel_trace.py`（16）、`test_selection_funnel_view.py`（3）、`test_universe_candidate_selector.py`（25）、`test_week5_snapshot_integration.py`（7）、`test_funnel_trace_verification.py`（7）。
2. `test_week5_scan_funnel_policy.py`、`test_week5_historical_backtest.py` 及两个夜扫留档接线用例共 **47 个**测试通过。历史 API 测试出现已有 Starlette/httpx 弃用提示，未产生失败。
3. 相关代码及新增测试 `ruff check` 通过，`git diff --check` 通过。
4. `clean-scope --fail-on-error` 退出码 **2**：ruff 通过；唯一阻塞为 `mypy_blocking`。本地 Python 3.12 环境中的 NumPy stub 使用 type statement，而项目 mypy 目标为 Python 3.11。当前 blocking/informational 日志均与续接前备份逐字相同，未宣称类型门通过；原日志已保留在本轮证据目录。
5. NAS 检出与 API 容器 build commit 均为 `2b0ab092d7bda43a2b8dd2a9df3faeba6fcca4c8`；四个主容器均在运行。实际工件位于容器 `/app/artifacts` 的 Docker named volume，不能把仓库同名空目录当成生产工件目录。
6. 实际夜扫目录只有 `funnel_trace_2026-10-09_night.json`：三层 `quality_300/light_100/deep_50`，NAS 运行代码的 `verify_trace()` 通过，仍有 250 个未归因淘汰。**真实五层有效观察日为 0**，并未因历史报告重放而补计。其 SHA-256 为 `35784ab8dc4a9c800746d741f5b2deee1edbb7f889e0293cf469431eddb65596`。

证据目录：`artifacts/research/night_funnel_continuation_20261011_v1/`。
本轮 NAS 操作为只读检查，未写生产数据库、marker、模型、配置或工件，未部署或重启。

### 仍未完成的任务与推进条件

| 任务 | 状态 | 下一项必要动作 |
| --- | --- | --- |
| 阶段一：所有硬门/特征的字段级来源和可用时刻 | 未完成；现有局部数据 readiness 不能替代 | 逐字段绑定来源、发布/采集时刻、版本及决策 cutoff。市值与事件特征的事后历史查询继续保持未认证，未知输入不通过硬门。 |
| 阶段二：生产漏斗逐层同日反事实与稳定留档 | 本轮记录器工程增量完成；真实生产证据未验收 | 经生产变更审查后接入新记录格式，核验实际五层文件，再按现有规则累积稳定观察与同日对照；不得重构已缺失的旧模型身份和分数。此开发观察不是阶段四正式影子。 |
| 阶段二：有限候选开发验收 | 两段开发矩阵均无合格候选；短窗口与事件 PIT 仍有阻塞 | 在已认证输入和足够决策日上按冻结 12 臂协议执行，不调低门槛或挑选已开封窗口；若仍无候选，停止晋升并保留失败结论。 |
| 阶段三：独立确认 | 未启动 | 必须先冻结合格候选及三个可共同 PIT 重放的对照，再登记未参与设计的新窗口；至少四折、120 个完整测试日、各臂 300 笔成熟成交并满足功效要求。 |
| 阶段四：正式影子和上线审查 | 未启动 | 阶段三通过后才起算；至少 60 个完整交易日和 100 笔成熟模拟成交，并通过净收益/账户收益下界及既有门禁。 |
| 完整 clean-scope 类型门 | 既有环境兼容阻塞 | 在与项目 Python 目标和依赖类型声明一致的验证环境重跑，不能以定向测试或 ruff 代替。 |
| 提交 / 发布 / NAS 部署 | 本轮未执行 | 当前分支仍有前会话的大量未提交整改；需先按阶段复核差异、形成可审查提交，再按 AGENTS §2.1 检查生产与回滚并取得具体部署授权。 |

当前级别：**本轮工程增量代码完成 + 定向测试完成；完整类型门未通过；完整 PIT、收益候选、独立确认和正式影子均未验收；未达到 Production Ready；本轮未部署。**


## 16. 2026-10-12 续接：质量门环境与全链路字段 PIT 强制门

本节是当前状态记录；§15 保留上一轮历史结果。

### 本轮完成

- 本机默认 `.venv` 使用 Python 3.12，而项目 mypy 目标为 3.11；其中 NumPy 2.5.2 的 stub 使用目标 3.11 解析不了的语法，导致此前 `clean-scope` 被环境组合阻塞。没有改仓库依赖或用户 `.venv`，改用已安装的 Python 3.11 解释器创建独立 uv 环境，并以 `uv pip install -e '.[dev]'` 安装项目开发依赖。
- 在该环境运行 `python scripts/run_quality_gate.py --stage clean-scope --fail-on-error`，结果 `ok=true`、`blocking_failures=[]`、`non_blocking_failures=[]`；报告保存在 `artifacts/research/tail_pit_quality_20261012_v1/clean_scope_initial.json`，运行前的既有质量日志副本保存在同目录 `prior_quality_logs/`。复用方式：选择与项目 `requires-python >=3.11`、mypy `python_version=3.11` 对齐的 Python 3.11 解释器，用 uv 创建隔离环境、安装 `.[dev]`，再运行上述门禁。
- 新增 `tail_field_pit.py` 和审计命令，按当前代码枚举硬门、趋势、事件、旧模型特征与快照字段，共 **271 个唯一字段**。审计检查真实输入 SHA、逐日逐臂覆盖、观察快照模式、来源文件 SHA/版本、缺失和未知填补计数，以及带时区的来源可用、采集、计算和决策时钟。
- 独立确认 CLI 已接入 full-chain 字段 PIT 强制门；统计函数本身始终 `release_eligible=false`。缺字段证据、重放输入、错误输入哈希、会话覆盖不匹配或来源时点超过决策截止时均不能进入统计晋升。CLI 输出采用新文件，不覆盖旧报告。
- 新增全字段清单/缺口报告：`artifacts/research/tail_pit_quality_20261012_v1/field_pit_gap_report.json`。完整清单为 271 个唯一字段、0 条决策记录、271 项全部 `blocked`；仓库 `artifacts/research` 下的 JSON/JSONL 没有符合新 schema 的 PIT 证据。报告 SHA-256：`0dd66c49d1cc01230b1e797108673739a5c81375a466df69077ca67e1ff8c8ec`。SHA-256 审计通过不能证明来源方当时确实发布了该值；审计器不提供供应商签名或独立时间戳能力。
- 当前命令、测试、清单和源代码/测试 SHA 固定在 `artifacts/research/tail_pit_quality_20261012_v1/verification_manifest_v4.json`（SHA-256 `ed26ecb4b24afc0403ed92873f267ee7f0cb73fdb26659c7fad24b2b51299461`）；同目录更早的 manifest 是过程快照，以 v4 为准。工件 schema 搜索排除 manifest 文件，因为它们会记录被搜索的 schema 名称。

### 验证

1. 四个定向测试文件 **20 项通过**：`test_tail_field_pit.py`、`test_audit_tail_field_pit_cli.py`、`test_tail_confirmatory.py`、`test_tail_confirmatory_cli.py`。确认 CLI 的成功分支测试用替身统计函数验证门禁编排；它不是实际候选的收益确认，也不代表真实 PIT 数据存在。
2. 新增/修改的 8 个 Python 文件 `ruff check` 通过；4 个核心源码/CLI 文件的定向 `mypy --follow-imports skip` 通过；`git diff --check` 通过。
3. Python 3.11 环境的 `clean-scope` 通过。完整 `full` 质量门、完整生产 Preflight、真实候选训练和正式影子流程未运行。

### 尚未完成及下一步

| 任务 | 当前状态 | 必要动作 |
| --- | --- | --- |
| 阶段一：历史硬门与全部特征的 PIT 证明 | 审计契约已实现；271 个字段的真实证据全部缺失 | 只能使用有决策时点原始快照、来源版本及可核验可用时钟的数据补齐；对 2025/2026 事后接口查询不能倒推出历史 PIT。新数据需按 observed snapshot 采集并生成字段映射与来源清单。 |
| 阶段二：有限候选开发 | 既有两段开发矩阵仍无合格候选；短窗口、事件特征和历史数据 PIT 仍有阻塞 | 保持 12 臂和样本门槛，使用认证输入重跑；若仍无候选则不启动独立确认。 |
| 阶段三：独立确认 | 未启动；新 CLI 会在没有字段 PIT 时阻断 | 先取得合格冻结候选，再收集未开封、全字段同日可用的四臂窗口，满足至少 120 日、每折至少 30 日、每臂至少 300 笔成熟成交及功效要求。 |
| 阶段四：正式影子 | 未启动 | 阶段三真实通过后再开始累计 60 个完整交易日、100 笔成熟模拟成交和账户净值。 |
| 正式发布 / NAS 部署 | 未执行 | 仍需候选、确认和影子逐级通过，再审查当前运行 commit/image、scheduler 及回滚方案；本轮没有生产写入或部署授权。 |

当前级别：**全链路 PIT 审计代码与强制门已实现并完成定向验证；工程 `clean-scope` 已在 Python 3.11 隔离环境通过；实际 271 字段 PIT 证据、合格模型候选、独立确认、正式影子和 Production Ready 均未完成；未部署。**


## 17. 2026-10-12 续接：raw-score 高低分方向探索

本节是当前进度；§16 保留方向审计之前的状态。该探索只回答“同日同池低分 Top3 是否比高分 Top3 更好”，不改变正式排序、校准门或候选选择规则。

### 本轮完成

- 新增 `src/stock_analyzer/research/tail_score_direction.py` 和 `scripts/audit_tail_score_direction.py`，按既有 12 臂矩阵及四折 walk-forward 训练窗，仅用未校准 raw score 对同一可评分池各选高/低分 Top3；未成熟选择不补选，成熟返回用于配对比较。该入口仅允许 `replayed_recompute`，拒绝 observed / 混合输入，不写模型工件，`release_eligible=false`。
- 入口重复键按 `(symbol, decision_date)` fail-closed，即使重复记录的 `entry_date` 不同也拒绝；缺失、非数值和非有限特征不进入可评分池。
- 使用 2025 RAW v3 已查看开发数据：72,600 条样本、242 个决策日、4 折。20,000 次 PCG64 配对移动块抽样，主块长 10 个连续交易日，5 / 20 日敏感性分析；24 个臂 × 指标组成一个 Holm 校正族，两个因事件特征 PIT 未证明阻断的臂以 `p=1` 进入校正。报告将每臂显示的校正范围明确标记为同一全局 family。
- 10 个价格特征臂的低分减高分净盈利率点差全部为负，范围 −1.68 至 −15.46 个百分点；平均净收益点差也全部为负，范围 −0.374 至 −1.796 个百分点。单侧假设为“低分优于高分”，全部 20 个可计算检验的全族 Holm 校正 p 值均为 1.0。此处只是当前已看过开发窗口的方向性估计，不证明高分排序已有效，也不能据此反转/晋升。
- 两个 `moneyflow` / `top_list` 臂维持 `event_feature_point_in_time_availability_not_proven` 阻断。既有 raw AUC fail-closed 规则、12 臂正式阶段二报告、候选选择规则与生产配置均未更改。
- 最终不可覆盖报告：`artifacts/research/tail_score_direction_audit_20261012_v5.json`，SHA-256 `fdd3bfc5a7a21db6ce583ee33bc4c535e1e17f7f8dce73f4d3875751a9cb465b`；输入样本 SHA-256 `ff2d49f7d108a25293717e2491316ea4d39f1319e6a1e9d29573e26241369b98`，交易日历 SHA-256 `04727dad76c3e69b276aa926e116b4fdb69dced3ee0f6d495c273b444554cce4`。所选源码 SHA-256 `5ecccd02ae57208885c1aa45b91e579a388a658d4562951e4f522b7ef259054b`；报告记录 HEAD `c58da46eaf18defed3adf2ac6ec89ad668dcbf66`、工作树不干净及 Python 3.12.14 / NumPy 2.5.2 / LightGBM 4.7.0。
- 更新 ADR-005 与索引，明确此审计的开发证据边界和实现位置。

### 验证

1. `tests/test_tail_score_direction.py` 与 `tests/test_audit_tail_score_direction_cli.py` 定向测试共 **12 项通过**。
2. 4 个本轮触碰的 Python 文件 `ruff check` 通过。
3. CLI 与核心统计模块共 2 个文件使用隔离 Python 3.11（mypy、NumPy 2.4.6、Pydantic、LightGBM）运行 `mypy --follow-imports skip`，通过。主工作区 `.venv` 是 Python 3.12；直接在该环境运行 mypy 会撞到 NumPy stub 的 Python 3.12 类型语法与项目 3.11 target 冲突，因此本轮以目标版本隔离环境的定向类型检查为准。
4. `git diff --check` 通过。§16 的 `clean-scope` 是本轮之前的环境验证，本轮新增诊断未重新运行完整 clean-scope。

### 尚未完成及下一步

| 任务 | 当前状态 | 必要动作 |
| --- | --- | --- |
| 阶段一：历史硬门与全部特征 PIT | 271 字段审计代码与强制门已完成，271 个真实字段证据仍全部 blocked | 取得能证明决策时点来源、版本及可用时钟的原始快照；不能用事后 API 查询补证。 |
| 阶段二：开发候选 | 两个已有阶段二窗口均无合格候选；本轮方向探索也没有形成低分优势，正式选择规则未改变 | 继续先解决可审计输入与 PIT；再按冻结 12 臂协议运行，保持 raw AUC 和成熟样本硬门。不得把 v5 方向报告当成新候选。 |
| 阶段三：独立确认 | 未启动 | 需先有冻结合格候选，再登记未开封窗口；至少 120 个测试日、四折、每臂 300 笔成熟成交和既定功效/PIT 门通过。 |
| 阶段四：正式影子 | 未启动 | 阶段三通过后累计至少 60 个完整交易日、100 笔成熟模拟成交与账户净值。 |
| 完整质量门 / 发布 / NAS 部署 | 本轮未运行完整 clean-scope；未发布、未部署 | 在变更复核后按阶段执行必要门禁；生产操作仍需独立检查运行状态、image、scheduler 与回滚方案。 |

当前级别：**新增方向诊断代码完成、定向测试与针对性类型检查通过；探索报告 release-ineligible，不能证明策略有效；真实 PIT、合格候选、独立确认、正式影子和 Production Ready 均未完成；未部署。**


## 18. 2026-10-12 NAS 历史快照核查与未来输入留存

本节记录只读 NAS 核查及本地准备的采集改动；本轮没有向 NAS 写文件或部署。

### NAS 已有数据与缺口

- NAS 生产检出 HEAD 与 API / scheduler 镜像身份一致，均为
  `2b0ab092d7bda43a2b8dd2a9df3faeba6fcca4c8`；四个主服务均运行。API、
  `scheduler-critical`、`scheduler-heavy` 使用的代码镜像摘要为
  `sha256:de00fdf5c53cc0705b235cfe9d687a8b9d1d800714958b80cbc9910064bfbcb0`。
- 在 NAS named volume
  `/vol1/docker/volumes/stock_analyzer_runtime_artifacts/_data` 找到 2024-09 至
  2026-08 的 PIT 命名 Parquet 数据集、`features_light` 特征快照和尾盘 shadow 报告。
  PIT Parquet 的元数据在 2026-09 生成，样本区间止于 2026-08-28；列里没有逐字段来源、
  可用、采集、计算时钟。它们可用于历史研究 / 回放，但不能证明字段当时可见。
- NAS 没有发现 `tail_field_pit_evidence.v1` 文件。2026-10-12 的 shadow 报告最后修改于
  14:56:54（上海时区），`data_as_of=14:55`；8 个输入符号全部因
  `no_tail_probability_available` 被阻断，challenger 工件未绑定，分钟行情也未传入。
  该报告不是 14:45 决策点的原始输入快照。
- 尾盘策略最后确认槽为 14:45（14:50 是排除的窗口终点）。生产代码的 live runtime
  每 5 分钟运行，普通 shadow / trace 路径按交易日文件名写回；因此 14:55 文件会覆盖
  前一轮普通报告，不能代替逐时点输入归档。

### 已准备的采集改动

- 新增 `tail_observed_inputs.py`：只在带时区的实际运行时间落于交易日 14:45 时，
  把当轮 `watch_pool` 与实际传入的分钟 bars 写到不可覆盖的独立 JSON 文件，并记录
  策略摘要、输入符号数、SHA-256 和运行时钟。写入采用同目录临时文件 + 排他硬链接，
  不覆盖已有日快照。
- `TrendTailShadowService.run()` 在模型读取和逐只决策前写入，以免后续慢运行或失败丢掉
  输入。路径位于
  `artifacts/runtime/trend_tail_shadow/observed_inputs/<交易日>/`；NAS 容器内的
  `/app/artifacts` 映射到上述持久化 named volume。
- 该快照范围只覆盖尾盘影子输入池，不包含全市场硬门候选和逐字段来源 / 发布时钟，
  所以明确标记 `full_chain_pit_evidence=false`，**不会通过 271 字段 PIT 审计**。
  它是开始积累真实观察数据的基础，不是策略有效或 PIT 完整的结论。
- 代码仍在本地未提交工作树；NAS 生产代码比本地 HEAD 旧 26 个提交。本轮没有向 NAS
  写入快照、覆盖生产文件、提交、构建、部署或重启容器。生产启用前必须把这个窄补丁
  单独回移到 NAS 当前基线并审查；不能把整条本地未提交分支部署上去。

### 验证与剩余条件

1. `tests/test_tail_observed_inputs.py` 与 `tests/test_trend_tail_shadow_runtime.py`：
   **33 项通过**；覆盖 14:45 采集、14:40 / 14:55 / 无时区时间跳过、日期不匹配、
   SHA、排他不覆盖及 shadow 服务接线。
2. 四个本轮触碰的 Python 文件 `ruff check` 通过；新增采集模块的定向 mypy 通过。
   影子 service 与采集模块合并检查时，使用 `--follow-imports=skip` 并抑制 3 个本轮
   未改动位置的 `no-any-return` 既有告警后通过。未运行完整 `clean-scope`。
3. `git diff --check` 对本轮文档、源码和测试通过。
3. 新快照只有在生产 scheduler-critical 启用该改动后的下一次 14:45 运行才会生成；
   当前 NAS 没有这份新格式数据。即使开始留存，还需给 271 字段逐项接入来源 / 可用时钟、
   imputation 记录以及全市场硬门输入，才可能满足 full-chain PIT 审计。

当前级别：**本地输入快照写入器与定向验证完成；NAS 生产启用待单独审查；全链路 PIT、候选训练、独立确认、正式影子与 Production Ready 仍未完成。**
