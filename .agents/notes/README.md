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