# 选股质量改进计划：交付状态与证据台账

As-of: 2026-10-08，分支 `feat/stock-selection-quality-overhaul` 已推送
（本轮 5 个提交 `5de976b` → `6d336ab` → `ce19b76` → `7ecee08` → `ad7f588` → 本文件所在提交；
PR #105 OPEN 且 mergeable。故意不把 As-of 写成单个 SHA：那会让这条事实每次改文档都过期一次）

每条都按仓库规约的四级状态标注（AGENTS.md §9.1）：
**代码完成 ≠ 测试完成 ≠ Freeze Ready ≠ Production Ready**。
"证据"列只写仓库里真的存在、能复查的东西（模块 / CLI / 测试名 / 文档）。

---

## 0. 交接：剩下的四件事各自要什么

| 还差什么 | 卡在哪 | 需要谁做什么 |
| --- | --- | --- |
| §4 选股质量验收（≥4 折、+5pp 且分块 CI 下界>0、平均净收益为正、尾部不明显恶化） | **124 个决策日**（37,200 请求 / 31,599 标签 / 21,760 笔成交退出 / 成交后净盈利率 **0.4053**），但标签侧退 3：`entry_day_limit_prices` 阻塞 **5,464** 条（=D17，6/7 月涨跌停只剩 209 个符号覆盖面）。四折验证两种特征集读数与 91 日**逐位相同**（0.4696 / 0.4720，退 5）——滚动折永远从最早那段开始校准 | `--rolling` 剖面推翻了「两条反号列拖累」这个解释：**11 条特征正向窗只有 9–12/19**，同一条 AUC 从 0.3753 摆到 0.6321。剩下两条诚实的路：远端补采 stk_limit 拉长真实历史，或引入这四类之外的新信息源（§3.2 的新闻/主题/completion 至今无独立证据）。在那之前不产命中率数字 |
| §4 滚动验证的折数门槛是硬数字：4 折需要 ≥71 个交易日（train≥45/calib≥12/embargo 5），58 天时验证器退 3=blocked；窗口已扩到 1~5 月 91 个交易日 | **上一轮把这条卡点写错了**：分钟 bar 不需要走容器/NAS——vendor 离线包每个交易日就有 5,194 个逐票 CSV，本来就是全市场，208 只这个上限是 ingest 时传了 `--symbols-file` 那份 900 只清单造成的。四月已按全市场重跑：26,305,150 行、109,150 个完整尾盘 symbol-day（体检 ok，见 §3l）；三/五/六/七同口径在补 | 补完即重跑全市场标签重建 + ≥4 折滚动验证（本地，不占 NAS）；影子那 60 天/100 笔仍只能等真实观察逐日累积 |
| 2026-03 中旬之后 `float_market_cap` 的真值（§3g） | **已补采**（2026-10-08 用户授权远端）：129 天 / 708,047 行进研究库独立表 `float_market_cap_ref`；污染窗口按真值重测见 §3j | 剩下的是仓库那一列要不要就地改写（写侧口径，需另开 ADR）与 17,491 行覆盖缺口 |
| 前两层接进生产夜扫（`universe`/`hard_eligibility`） | **已实现**（2026-10-08 用户点头改生产报告形状）：`_hard_filter` 逐只导出成员与第一条命中原因 → 报告 `hard_gate_membership` → `live_universe_facts()` 喂给 `build_universe_stage_traces()`；线上 20 个硬门名全部登记为 HARD（ADR-004 §6.2 闭合） | 剩下的是**部署**：这套改动只在本地测试过，NAS 上还没跑过一次真实夜扫；`known_suspended` 那条语义冲突不在本次范围（线上读的是显式声明字段） |

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
