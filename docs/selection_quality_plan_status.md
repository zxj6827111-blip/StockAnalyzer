# 选股质量改进计划：交付状态与证据台账

As-of: 2026-10-08 @ HEAD `b33b581`（分支 `feat/stock-selection-quality-overhaul`，未推送）

每条都按仓库规约的四级状态标注（AGENTS.md §9.1）：
**代码完成 ≠ 测试完成 ≠ Freeze Ready ≠ Production Ready**。
"证据"列只写仓库里真的存在、能复查的东西（模块 / CLI / 测试名 / 文档）。

---

## 1. 结论

- **工程验收：通过**（计划 §4「工程验收」13 个场景 + 线上/历史一致性，逐条有测试钉住）。
  其中**「特征缺失」这一项此前是虚的**：训练器遇 null 会抛裸 `TypeError`，既不归因也不留痕；
  现已改成"排除并计入 `artifact["feature_completeness"]`，整列不可用才 raise"，
  由 `tests/test_tail_net_profit_trainer.py` 两条用例钉住。
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
| §2 九层漏斗逐层可追溯（输入/晋级/原因/特征/原始预测/校准概率/模型身份/数据时间） | `research/funnel_trace.py`（写时计数恒等式 + 读时 `verify_trace()`）、`research/night_scan_funnel_trace.py`（夜扫三层 + 前两层生产者）、`research/selection_funnel_view.py`（拼成九层视图并判"哪些问题答不了"） | `test_funnel_trace.py`(17)、`test_funnel_trace_verification.py`(7)、`test_night_scan_funnel_trace.py`(8)、`test_selection_funnel_view.py`(3)、`test_shadow_evidence.py`(5)；接线由 `test_week5_automation.py::test_night_scan_writes_the_night_half_funnel_trace` 与 `::test_night_scan_reports_why_the_trace_was_not_written` 钉住；`scripts/audit_selection_funnel.py` | 前 5 层**代码+测试完成但未接线**，后 4 层已在影子链路；见下方缺口 |
| §2 最终推荐单独留档并关联特征快照 | `archive_final_recommendations()`；缺快照落成 `feature_snapshot_missing` caveat 而不是省略 | `test_missing_feature_snapshot_is_a_visible_caveat` | 代码+测试完成 |
| §2 逐层消融预测性规则、硬门保留 | `StageTrace.kind ∈ {hard_gate, predictive}` + `compare_traces()`（要求交易日集合完全一致） | `test_funnel_trace.py` 消融对照组用例 | 代码完成；**对照组需真实留档才能跑** |
| §2 根因清单，区分已证实/假设；不把 bronze 占比当根因 | `.agents/notes/NOTE-002-selection-quality-root-causes.md` D1–D14 + H1–H5 | 该文件 + `scripts/audit_selection_funnel.py` 退出码 3 的机器判定 | 已交付，随实测更新 |
| §3.1 新记录时区/交易日/去重/标签成熟；旧记录带版本解释 | `write_trace()` 落带时区时刻；`research/record_time_semantics.py` v1/v2 解释、声明矛盾即撤销证据资格 | `test_record_time_semantics.py`(6) | 代码+测试完成 |
| §3.1 绑定实际模型/manifest/特征版本/运行身份，记录失败可见 | `models/tail_serving_manifest.py`（challenger-only，verify 时重哈希工件）+ `models/tail_model_artifact.py` + `runtime_identity` 共享解析器 | `test_tail_serving_manifest.py`(8)、`test_tail_model_artifact.py`(11)、`test_trend_tail_shadow_runtime.py`(29) | 代码+测试完成 |
| §3.1 补齐校验日历/RAW/精确涨跌停/停复牌/证券状态；复用现有接口、独立研究库 | `research/trend_data_readiness.py` + `scripts/audit_trend_data_readiness.py`；研究库 `artifacts/research/tail_minute_bars.duckdb` 的 `ref_*` 参考表 | `test_trend_data_readiness.py`(22)、`test_tail_reference_store.py`(14) | 代码完成；**就绪审计退出码仍为 5(blocked)**，因本机无库、缺 `trade_status` 列 |
| §3.1 校验指数链路，缺失不填零 | 指数缺口按 insufficient 处理 | `test_index_gap_is_insufficient_not_zero_filled` | 代码+测试完成 |
| §3.2 硬门保留 + 预测性加分/板块配额/探索样本分离；硬门后先算轻量特征再截断 | `feature/trend_candidate_contract.py`（`HARD` vs `predictive`、`assert_training_features()`） | `test_trend_candidate_contract.py`(16) | 代码+测试完成 |
| §3.2 新 trend as-of 特征契约，训练与线上同一套；旧 T−1 与 Alpha V2 保持独立；四组特征逐组+消融 | 同一特征入口 + walk-forward 分组门；日内两列由 `replay_tail_candidate_pool.py --minute-db` 从分钟库真算 | `test_tail_walk_forward.py`(20)；特征白名单是训练的第一条前置检查 | 代码+测试完成；**四组 OOS 已测量：校准窗 raw AUC 全 < 0.5，无一进正式候选** |
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

1. **需要数据（不是代码缺陷，也不可绕过）**
   - 带时刻的尾盘窗口分钟行情（14:30–14:50 每 5 分钟一根）；
   - `daily_trade_status` 的 `trade_status` 列（现在没有该列 ⇒ 重建标签 0 条可训练）。
   没有它们，§4 的 ≥4 折、+5pp 且分块 CI 下界 >0、60 天/100 笔**都不可达**。
2. **需要一次确认的设计决策**
   - `universe` / `hard_eligibility` 两层的**接线**：生产者已就位，但夜扫报告里的
     `universe_snapshot` 只有计数与 ≤50 样本，没有 `eligible_symbols` 清单，
     而 `StageTrace` 不许用计数冒充成员。补上要么让生产夜扫报告携带符号级名单
     （报告体积与形状变化），要么在生产路径新增一份 sidecar 工件（新的生产 I/O）。
     两条都超出"顺手改"的范围，需你点头。
3. **需要远端授权**
   - 推送分支 / 开 PR（8 个提交，全部只在本地）；
   - 在 NAS 上实跑 `scripts/audit_trend_data_readiness.py`、`audit_selection_funnel.py`、
     `audit_shadow_evidence.py` 拿到真实读数（尤其确认历史留档不会因 `digest()` 载荷变更被误判）。

## 4. 基线事实（避免把既有问题当成新引入）

- `mypy src` 因 numpy stub 与 `pyproject.toml` 的 `python_version="3.11"` 直接中止 ⇒
  clean-scope 门 rc=2，唯一 blocking 恒为 `mypy_blocking`；`ruff_clean_scope rc=0`。
- 两条测试在基线 `6c7079c` 就红：`test_service_model_registry…can_warn_without_transition`、
  `test_alpha_v2_m4l_e2e_rehearsal::test_attack_a_source_label_without_funnel_artifact_fails`。
- 本机无 `data/*.duckdb`（库在 NAS），`artifacts/*` 被 gitignore ⇒ 运行报告类工件无法入库。
