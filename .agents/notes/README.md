# StockAnalyzer Project Notes

> 本目录记录 StockAnalyzer 中无法仅从代码本身安全推导出的长期决策、业务规则、事故经验和设计边界。

代码回答：

> 系统现在怎么实现？

Notes 回答：

> 为什么这样实现？
> 什么不能随便改？
> 哪些方案以前已经证明有问题？

---

## 1. AI 阅读规则

处理较大任务时：

1. 先读项目根目录 `AGENTS.md`
2. 再读本 `README.md`
3. 根据任务只读取直接相关的 1～2 篇 Note / ADR

不要全量加载所有文件。

---

## 2. Note 分类

### ADR

重大架构或业务决策。

命名：

`ADR-XXX-title.md`

例如：

`ADR-001-runtime-identity.md`

---

### Incident

已经实际发生过的重要问题或事故。

命名：

`INC-XXX-title.md`

用于记录：

- 发生了什么；
- 根因；
- 为什么原测试没有发现；
- 如何修复；
- 如何防止再次发生。

---

### Business / Contract Note

核心业务语义或数据契约。

命名：

`NOTE-XXX-title.md`

例如：

- PIT 定义；
- 双价格规则；
- freeze 合约；
- marker 语义。

---

### 统一头部字段

三类文档都必须带：

```text
Status:  Draft | Accepted | Superseded
As-of:   最后一次对照代码核实的日期 + 当时的 HEAD SHA
```

`Status` 只能取这三个值。证据不足就写 Draft，不要为了"看起来完整"写 Accepted。
`As-of` 的用途是让你判断这篇 Note 相对于当前代码是否还能直接采信——它会过期，
但不会静默过期。


## 3. 当前重点知识领域

StockAnalyzer 当前重点决策领域包括：

### Runtime Identity

涉及：

- training commit；
- runtime commit；
- freeze manifest；
- epoch；
- model artifact；
- integrity；
- fail-closed。

---

### Dual Price Freeze

涉及：

- QFQ；
- RAW；
- PIT universe；
- execution availability；
- 停牌；
- 数据缺失；
- freeze validation。

---

### Alpha V2

涉及：

- Shadow；
- Freeze；
- Production Preflight；
- scheduler；
- 模型生命周期。

---

### Production Runtime

涉及：

- NAS；
- Docker；
- api；
- scheduler-heavy；
- scheduler-critical；
- redis；
- 部署与回滚。

---

## 4. 索引

| 文档 | Status | 主题 | 什么时候需要读 |
| --- | --- | --- | --- |
| `ADR-001-runtime-identity-and-artifact-integrity.md` | Accepted | 运行/构建身份、模型工件完整性、epoch 锚定、fail-closed 退出码 | 改 `runtime_identity.py` / `frozen_model.py` / `freeze.py` / `epoch.py` / Dockerfile 构建身份 / 任何 CLI 的退出码时 |
| `ADR-002-dual-price-freeze-contract.md` | **Draft** | QFQ / RAW 角色契约、PIT 候选集 ↔ 可交易集、日截面健康门、跨面板双向对称、interior/trailing 形状分桶、连号结构闸、decision 守恒账 | 改 `dual_price_series.py`（含 `assess_decision_session_health` / `max_numeric_symbol_run` 与 6 个 `DEFAULT_*` 阈值）/ `dual_price_freeze.py`（含特征侧 inner merge）/ `pit_universe` / `certify_price_mode` / freeze 对齐逻辑 / 任何"过滤占比或缺失形状"判据时 |
| `NOTE-001-alpha-v2-production-gates.md` | Accepted | train→freeze→preflight→epoch→shadow 的门禁顺序、退出码表、Shadow/Production 边界、当前不可达状态 | 改任一 Alpha V2 CLI、preflight 判定、shadow 配置开关、调度 job 时 |
| `ADR-003-trend-tail-strategy-contract.md` | **Superseded** | 历史尾盘策略入口、准入与影子链路决策；RAW 分钟退出与公司行动细节转由 ADR-006 裁决 | 阅读历史决策；改策略准入、共享契约或影子输出时继续读未被 ADR-006 覆盖的部分 |
| `ADR-004-eligibility-evidence-integrity.md` | **Draft** | 资格层留档可信度：原因名登记 HARD、归因顺序固定、无判别力硬门不落通过；财务 PIT 侧车、ST 重放及证券名称官方证据复核；夜扫 selection_evidence v1 逐证券原因与完整输入池；新增 271 字段 PIT 清单/审计契约，真实历史字段证据仍缺 | 改 `trend_candidate_contract.py` 的 `_RULE_KIND`/`classify_rule`/`HARD_GATE_ATTRIBUTION_ORDER`/`UNPROVEN_FLOAT_MARKET_CAP` / `replay_tail_candidate_pool.py` 的 `daily_gates`/`degenerate_gate_inputs`/`_universe_fact`/`apply_st_snapshot_overlay` / `night_scan_funnel_trace.py` / `trend_data_readiness.py` / `tail_field_pit.py` / provider 数据缺失与 PIT 语义时 |
| `ADR-005-tail-quality-evidence-protocol.md` | **Draft** | 尾盘开发与晋升证据边界：开放日/决策覆盖日、真实折身份、配对移动块统计、最低样本数、唯一键、采集模式与输入哈希；14:45 原始候选/分钟快照先落盘，按时完成的 pipeline 上下文用旁车绑定，跨槽仍阻塞且不构成 271 字段 PIT 证据；逐字段来源/供应商发布时间、独立确认和正式影子仍待真实证据 | 改 `tail_net_profit_trainer.py` / `tail_walk_forward.py` / `tail_confirmatory.py` / `tail_field_pit.py` / `tail_observed_inputs.py` / `week5_automation_service.py` / `pipeline.py` / `runtime/service.py` / `week5_market_snapshot_service.py` / `resilient_provider.py` / `snapshot.py` / `trend_tail_shadow_service.py` / `audit_tail_field_pit.py` / `tail_score_direction.py` / `audit_tail_score_direction.py` / `run_tail_model_ablation.py` / `validate_tail_independent_confirmation.py` / `validate_tail_selection_quality.py` / `assemble_tail_samples.py` / 事件特征 PIT 证据或统计发布门槛时 |
| `ADR-006-raw-minute-exit-and-corporate-actions.md` | **Draft** | RAW 分钟退出、T+1 成交标签成熟时刻、公司行动现金/股份、adj_factor 交叉校验、前导零 symbol join、固定资金账本；新增有严格证据的全天停牌无因子行“不适用”规则，绝不合成因子；完整停牌状态下全零量分钟行才视为供应商占位；公司行动复核与固定账户账本证据 | 改 `simulate_tail_exit_raw_minutes()` / `is_zero_volume_placeholder_session()` / `tail_reference_store.py` / `tail_rebuild.py` / `tail_account_ledger.py` / `rebuild_tail_labels.py` / `reconcile_tail_corporate_actions.py` / `augment_tail_daily_refs_from_minutes.py` / `suspension_evidence.py` / 公司行动与因子采集、标签 maturity 时读 |
| `NOTE-002-selection-quality-root-causes.md` | **Draft** | 选股质量根因清单 D1–D35 / H1–H5；含 RAW 标签、两段开发矩阵均无候选、财务/证券状态/市值/事件 PIT 缺口；夜扫完整输入池证据已修；独立确认原缺少字段 PIT 门现已 fail-closed，但 271 个字段尚无真实完整证据 | 做选股质量诊断、漏斗留档、胜率/净盈利率统计、参考数据同步、字段 PIT 审计或准备开启独立确认之前 |

> ADR-002 是 Draft 的原因写在它 §1：第一段（价格角色口径）已定；第二段（缺 execution
> 当日 bar 时该 fail closed 还是该过滤）**方向已定、判据未定稿** —— `be2e4ef` 落了过滤式
> 裁决，P3.1.1 补了日截面健康门并把"两侧同缺 = 停牌"这个断言从代码与文档里撤掉，
> 但两条比例闸的阈值重设与 NAS 侧 2025-11-17 覆盖缺口仍未收口。
> **读它时先看 §5（裁决）与 §6.1/§6.2（能力边界与真值源现状）。**

新增 Note 时在上表加一行，一行说清"问题 + 机制 + 防御的故障类型"。索引是常驻上下文，
不要往这里复制正文。

### 当前尚无 Note 覆盖的领域

已确认存在高风险决策、但本轮未整理（证据不足或篇幅不足）：

- PIT / 交易日 / 停牌 / 数据可用性的**通用**定义（ADR-002 §3 只覆盖了 Alpha V2
  冻结所需的那一部分）；
- 双价格数据链路（vendor ZIP / RAW delta / QFQ 派生）的上游同步合约；
- NAS 生产部署与回滚（compose 组合守卫、`scripts/nas_compose_files.sh`、部署脚本健康检查时序）；
- 学习链路（label 口径、champion/challenger、校准器）与 Alpha V2 身份边界的关系。

不要为了"文档看起来完整"提前写这些；等相应工作真的需要时再建。

---

## 5. 更新原则

出现以下情况时检查是否需要更新 Note：

- 数据含义改变；
- 核心公式改变；
- 状态机改变；
- fail-closed 规则改变；
- Freeze 合约改变；
- Runtime Identity 规则改变；
- Production Gate 改变；
- 原有 ADR 前提已经失效。

---

## 6. 历史不可覆盖

已有 ADR 被新设计替代时：

不要删除旧 ADR。

旧 ADR 标记：

`Status: superseded`

并填写：

`Superseded-by: ADR-XXX`

新建新的 ADR 描述新决策。

项目历史本身是工程知识。
