# trend 尾盘净盈利概率链路：发布与回滚清单 / 影子验证方案

As-of: 2026-10-08 @ HEAD `6900e3a`（分支 `feat/stock-selection-quality-overhaul`）

配套决策见 `.agents/notes/ADR-003-trend-tail-strategy-contract.md`；
根因与阻塞见 `.agents/notes/NOTE-002-selection-quality-root-causes.md`。

本清单**不是**"照做就能上线"的操作手册，而是把"现在还不能上线"的判据写清楚：
第 0 节未通过，后面的节不必看。

---

## 0. 前置硬门（当前状态：**未通过**）

```text
[ ] P0-1 带时刻的分钟行情落库并可回补（当前 intraday_summary_1m/5m 只有日级聚合列）
[ ] P0-2 index_daily 常驻增量进调度（scripts/sync_index_daily.py 至今无调度入口，
       两库停在 2026-08-14）
[ ] P0-3 scripts/audit_trend_data_readiness.py 退出码从 5(blocked) 降到 0/3
[ ] P0-4 至少 4 折、每折标签真实成熟的 walk-forward 结果（models/tail_net_profit_trainer
       .evaluate_selection_quality 判定 passed）
```

依据：改进计划 §5"历史分钟行情不足时，尾盘策略验证明确记为阻塞，继续采集所需数据，
不能用开盘回测代替"。**开盘价回测的结果不得写进任何发布判断。**

---

## 1. 发布前检查（逐条留证据，不允许"应该没问题"）

| 编号 | 检查 | 判据 / 命令 |
| --- | --- | --- |
| R1 | 契约摘要与工件一致 | `rank_final_recommendations` 的 `ModelIdentity.validate()` 返回空串；`contract_digest` 等于 `DEFAULT_TREND_CONTRACT.digest()` |
| R2 | 训练 commit == 运行 commit | 走 `alpha_v2/validation/runtime_identity.resolve_runtime_code_identity()`，禁止 CLI 自己 `git rev-parse`（ADR-001 §7.2） |
| R3 | 模型身份完整 | `artifact_content_hash` / `feature_compute_version` / `label_policy_id` 均非空；缺失即 0 只推荐，不补名额 |
| R4 | 新 label_policy 已登记且未覆盖旧记录 | `label_policy_registry` 里 `schema_version=4`、`price_basis=tail_confirm_next_bar`、`maturity_rule=label_mature_time_tail_exit_v1`；旧 `soup_*` / `return_rank` 记录原样存在 |
| R5 | 输出语义已登记 | `output_semantics_for_basis("net_profit_5d_tail") == "event_probability"` |
| R6 | 配置无残留冲突 | `audit_strategy_contract_conflicts(config) == []`（runtime/asof/labels 三块都改指契约后才会空） |
| R7 | 成交口径是 raw | 数据就绪报告 `raw_price_basis_declared == ok` |
| R8 | 成本按日期冻结 | `cost_profile(date).source == "cost_schedule"`，且 `overridden` 集合被写进留档 |
| R9 | 线上/历史判定一致 | 同一 `confirmation` 谓词 + 同一 bar 序列，`evaluate_tail_entry(quote_as_of=…)` 与 `evaluate_tail_entry(quote_as_of=None)` 结果相同（已有测试；发布前在真实数据上抽查一日） |
| R10 | 漏斗留档完整 | 当日 `funnel_trace_*.json` 每层计数自洽、`model_identity.identity_recorded=true`、最终推荐每行 `caveats` 不含 `feature_snapshot_missing` |
| R11 | 质量验收门 | `evaluate_selection_quality(...).passed == True`（提升 ≥5pp、分块 CI 下界 >0、平均净收益 >0、尾部 p05 不明显恶化、折数 ≥4） |
| R12 | 影子观察达标 | `shadow_readiness(observed_trade_days≥60, matured_simulated_fills≥100).ready_for_release_review == True` |

---

## 2. 发布动作（顺序不可颠倒）

1. **只影子，不接管真推荐。** 新路径产物写到独立目录
   （建议 `artifacts/runtime/trend_tail_shadow/`），旧 trend 输出保持原样。
2. 注册为 challenger：沿用 `runtime/services/learning_governance_service.py` 的
   两阶段票据（proposal → 人工 approve → release ticket → execute → confirm），
   **不新增晋升入口**，也不允许训练流程自己切别名（ADR-001 §7.2）。
3. 生产 compose 一律走 `scripts/nas_compose_files.sh` 给出的文件组合；手工拼 `-f`
   会触发 `SA_NAS_PRODUCTION_GUARD`（AGENTS.md §2，两次同型事故）。内存限制只在
   `docker-compose.memlimit.yml` 里，漏掉 = 没有上限。
4. 发布前确认：当前 commit / image、正在跑的 scheduler（`api`、`scheduler-heavy`、
   `scheduler-critical`、`redis`）、备份点、回滚命令已实测一次。
5. 切换后先观察一个完整尾盘周期（14:30–14:50 + 次日成熟），期间不做第二次变更。

## 3. 回滚

| 触发条件 | 动作 |
| --- | --- |
| 尾盘留档缺特征快照 / 模型身份异常但仍在出推荐 | 立即停新路径 job，旧路径不受影响（旧模型与旧路径保留未删） |
| 契约摘要与工件不符（`strategy_contract_digest_mismatch`） | 阻断推荐输出（代码已 fail-closed），回滚 alias 到上一 champion |
| 连续多日 `blocking_reason` 非空 | 检查 `trend_data_readiness`，必要时回到"只观察不推荐" |
| 净盈利率显著低于影子期估计 | 走 `learning_governance_service.rollback()`，保留证据目录不删 |

回滚不得清理 volume、不得改历史 marker、不得覆盖 artifact（AGENTS.md §2.1）。

## 4. 影子验证方案（未来真值，不接受历史数字替代）

- **观察对象**：夜间观察池 → 次日 14:30–14:50 尾盘确认 → 最终推荐 0–3 只 →
  1 万元参考金额成交 → TP+8%/SL−5%、持有 5 日（入场日为第 1 日）。
- **必须分开记录**：`observed_snapshot`（系统当时真实打分）与
  `replayed_recompute`（事后重算）。本项目已实测两者前向结果差一个量级，
  任何合并统计都视为无效。
- **上报指标**：净盈利率（分母只含已实现样本）、平均/中位净收益、成交率、
  确认率、尾部亏损 p05、推荐覆盖率（有推荐的天数占比）、资金占用、
  以及按拒绝原因的分布（`below_threshold` / `cap_exceeded` / `risk_blocked` /
  `not_filled` / `uncertain`）。
- **对照**：同日同风格基线、同一合格池的旧排序、旧完整链路。旧链路若没有成交样本，
  如实报 `baseline_has_no_fill_samples`，**不得伪造命中率**。
- **成熟度**：逐 horizon 报 n；9 月下旬信号的 5/10 日持有期在数据不足时不得计入。
- **上线判据**：见 §1 R11 + R12。证据不足就保持影子状态。

## 5. 阈值 0.60 的说明

0.60 是**初始选股规则**，用来控制"推荐多少只"，**不代表已证明实际命中率是 60%**。
项目自身锁定 OOS 的绝对命中率区间是 43–45%（`docs/alpha_v2/M4H_Historical_Locked_OOS_Report.md:206,591`）。
若影子期发现 0.60 长期筛不出股票（常见 0 只），那是要重新讨论准入规则的信号，
不是把阈值调低的理由。
