# StockAnalyzer Alpha V2.0 — ZCode 分阶段施工提示词

> 版本：2026-09-17  
> 用途：放入本地 StockAnalyzer 仓库，供 ZCode 按阶段逐项施工。  
> 上位规格：`StockAnalyzer_Alpha_V2_完整改造方案_20260917.md`  
> 配套验收：`StockAnalyzer_Alpha_V2_Codex_阶段验收门禁_20260917.md`

---

# 0. 使用方式

本文件不是一次性执行提示词集合，而是**分阶段施工控制文件**。

每次只把一个阶段的完整提示词交给 ZCode。  
ZCode 完成当前阶段后必须停止，把施工报告交给 Codex 独立验收。

流程固定为：

```text
ZCode 执行 Sxx
    ↓
生成 Implementation Report + 测试 + 审计工件
    ↓
Codex 按对应 Gate Sxx 独立验收
    ↓
PASS
    ↓
才允许进入下一阶段
```

如果 Codex 返回：

```text
FAIL
```

则必须回到同一阶段修复，禁止跳到下一阶段。

如果 Codex 返回：

```text
CONDITIONAL PASS
```

只有在门禁文件明确允许的情况下才能继续；默认仍视为未通过。

---

# 1. 全局施工纪律

以下规则适用于所有阶段，后文不再重复。

## 1.1 上位文档优先级

施工前必须完整读取：

```text
StockAnalyzer_Alpha_V2_完整改造方案_20260917.md
```

若本文件与上位规格冲突：

```text
以上位规格为准
```

若代码现实与上位规格不一致：

1. 不得静默改写目标；
2. 先记录差异；
3. 选择最小兼容实现；
4. 在施工报告 `Deviation From Blueprint` 中说明。

## 1.2 基线

原始评审基线：

```text
branch = fix/asof-breadth-gate-coverage-0917
commit = 7e9e33bdb9d03506cff5dfff29c78b1c95019541
```

但施工时**不得假设 HEAD 仍停在基线**。

每阶段开始前必须执行：

```bash
pwd
git branch --show-current
git rev-parse HEAD
git status --short
```

如果有未提交修改：

- 不 reset；
- 不 checkout 覆盖；
- 不删除；
- 不擅自 stash；
- 先判断是否属于上一阶段产物或用户自己的修改。

## 1.3 每阶段只做一个主题

禁止“顺手修”下一阶段的问题。

发现其他问题统一写入：

```text
Deferred Findings
```

## 1.4 禁止项

在 P0 全通过、重新生成 Clean OOS 证据以前，禁止：

```text
final_signal_min_threshold 70 -> 其他值
p_lgbm_min 下调
p_xgb_min 下调
p_meta_min 下调
max_diff 放宽
为了出票修改 breadth threshold
为了出票关闭 overextension
扩大 final cap 强行出票
修改 LGBM/XGB/meta 权重
直接 promote 9/15 challenger
对 AUC 0.331 模型做 1-p 后上线
把 news/theme 正式并入 Alpha
把 V2 直接接管正式飞书买入结论
```

## 1.5 默认 Shadow

除非进入后续明确授权阶段：

```text
alpha_v2.enabled = false 或 shadow-only
alpha_v2.enforce_final_selection = false
Legacy 正式结果保持不变
```

## 1.6 不得自动部署

未经用户明确授权：

```text
不得 git push
不得部署生产
不得重启生产容器
不得切 serving model
不得 promote model
不得修改 NAS 生产配置
```

可以：

- 修改本地工作区；
- 新增测试；
- 运行本地测试；
- 生成审计工件；
- 给出建议 commit message。

## 1.7 每阶段必须输出 12 节施工报告

```text
# Sxx Implementation Report

1. Preflight
2. Blueprint Scope
3. Files Changed
4. Behavior Changes
5. Config / Contract Changes
6. Tests Added or Modified
7. Commands Executed
8. Test Results
9. Audit Artifacts
10. Git Diff Summary
11. Deferred Findings
12. Rollback
13. Status: DONE / PARTIAL / BLOCKED
```

没有真实执行的测试不得写 `passed`。

---

# 2. 阶段编号说明

上位蓝图存在一个编号冲突：

- 正文章节中 `P1-07` = Cross Review V2；
- 任务卡中 `P1-07` = Purged Walk-Forward。

本文件不修改原方案，只为执行消歧，使用：

```text
S00 ~ S23
```

每个阶段同时保留原方案引用。

---

# S00 — Alpha V2 Feature Flag 与零行为变化基线
**原方案：P0-00**

## 给 ZCode 的提示词

你现在执行 StockAnalyzer Alpha V2.0 的 **S00**，本轮只完成：

> Alpha V2 Feature Flag、审计根目录、Baseline Manifest、Legacy No-op Regression。

### 本轮目标

新增独立 `alpha_v2` 配置，但默认必须：

```yaml
enabled: false
shadow_only: true
enforce_final_selection: false
```

建议语义：

```yaml
alpha_v2:
  enabled: false
  shadow_only: true
  enforce_final_selection: false
  artifact_root: artifacts/alpha_v2
  selection_contract: night_alpha_v2_v1
  model_resolver_mode: pit_research
  entry_mode: next_session_open
  primary_horizon_days: 5
  candidate_output_top_k: 5
```

按现有 Pydantic/config 风格最小实现。

### 必须完成

1. 配置模型与默认配置；
2. `artifacts/alpha_v2/` 根路径支持；
3. 生成 `baseline_manifest.json`；
4. 建立 `docs/alpha_v2/PROGRESS.md`；
5. 建立 `alpha_v2.enabled=false` 的 no-op/golden regression；
6. 证明 Legacy：
   - 70 分不变；
   - Cross Review 不变；
   - 300/100/50 夜扫参数不变；
   - Legacy Final 不变；
   - 正式通知不变。

### baseline manifest 最少字段

```text
generated_at
code_commit
config_hash
alpha_v2 flags
legacy final threshold
legacy cross-review thresholds
night quality/light/deep
final cap
allow_zero_signal
training.artifact_path
models.inference_score_source
app.mode
advisory_only
```

不得写 secrets。

### 不得执行

- P0-01 及后续；
- 模型身份修复；
- T+1 修复；
- 阈值调整；
- serving 切换；
- 生产部署。

### 完成条件

```text
alpha_v2.enabled=false
```

时 Legacy 核心行为与修改前一致。

完成后停止并输出 `S00 Implementation Report`。

---

# S01 — Model Identity Truth
**原方案：P0-01**

## 给 ZCode 的提示词

只执行 **S01：真实模型身份链**。

前提：Codex 已对 S00 给出 PASS。

### 当前必须解决的问题

历史报告可能写：

```text
model_trained_at = 2026-09-15
```

但实际 Pipeline 加载：

```text
artifacts/model_v1.json
sha256 = 71f64a21...
artifact created = 2026-08-16
```

原因是 historical report 身份来源与实际 predictor artifact 分离。

### 本轮目标

建立唯一规则：

> Pipeline 实际加载谁，报告就必须报告谁。

### 重点检查

```text
src/stock_analyzer/pipeline.py
src/stock_analyzer/models/predictor.py
src/stock_analyzer/runtime/services/week5_historical_runner.py
src/stock_analyzer/runtime/services/asof_backtest_service.py
```

### 必须实现

`AnalyzerPipeline` 提供只读 model identity，至少：

```text
model_id
artifact_path
actual_artifact_content_hash
artifact_created_at
feature_schema_id/hash
label_policy_id/hash
content_hash_verified
score_source
output_semantics
```

Registry 只能补充 metadata，不能覆盖实际 artifact 事实。

若：

```text
registry hash != actual artifact hash
```

则：

```text
model_identity_status = mismatch
```

V2 research fail-closed。

### 测试至少覆盖

- 有 champion；
- 无 champion；
- bootstrap 时间比 artifact 新；
- registry hash mismatch；
- artifact missing；
- serving alias 被覆盖。

### 禁止

- 不修 Registry 历史坏记录；
- 不实现 HistoricalModelResolver；
- 不换模型；
- 不 promote；
- 不调阈值。

### Done When

历史/研究输出中 actual artifact hash 与真实文件 `sha256sum` 一致，且不再用 bootstrap 时间冒充真实加载模型身份。

完成后停止。

---

# S02 — T+1 Entry Simulation
**原方案：P0-05；并带入 P0-06 的最小 raw execution 前置契约**

## 给 ZCode 的提示词

只执行 **S02：盘后策略 T+1 入场修复**。

前提：S01 PASS。

### 当前错误

T 日 15:30 产生信号，却可能：

```text
entry_date = T
entry_price = T close
```

这是不可实现成交。

### 本轮目标

建立统一 `simulate_entry()`，主口径：

```text
signal = T 日收盘后
entry candidate = T+1 session open
execution price = raw open + slippage/fee/impact
suspended = no fill
one-price limit-up / buy forbidden = no fill
```

### 重点文件

```text
src/stock_analyzer/backtest/holding_curve.py
src/stock_analyzer/backtest/matcher.py
src/stock_analyzer/runtime/services/asof_backtest_service.py
```

### EntrySimulation 至少输出

```text
executed
signal_date
entry_date
entry_price_raw
reference_open_raw
slippage
cost
no_fill_reason
entry_delay_days
```

Primary：

```text
必须 T+1 可成交，否则 no_fill
```

Sensitivity：

```text
允许 <=3 sessions next tradable open
```

两者不得混成一个主结果。

### 测试至少覆盖

- 正常 T+1 开盘；
- 停牌；
- 一字涨停；
- 普通涨停但可成交；
- ST 5%；
- 创业板/科创板 20%；
- IPO 特殊期；
- T+1 卖出规则；
- 成本日期版本。

### Done When

所有 historical 主结果满足：

```text
entry_date > signal_date
```

或：

```text
status = no_fill
```

绝不再存在 T close 假成交。

本轮不要继续做完整 P0-06 corporate action 治理。

---

# S03 — Point-in-Time Historical Universe
**原方案：P0-04**

## 给 ZCode 的提示词

只执行 **S03：PIT Historical Universe + Expected Active Universe**。

### 当前问题

`AsOfMarketDataProvider.list_symbols()` 直接透传当前完整 provider index。

### 目标

对 as_of=T 构造可审计历史 universe，不允许未来上市股票或当前状态污染历史。

至少满足：

```text
first_available_bar_date <= T
listed_age >= 系统既定最小上市天数
有足够历史 bars
T 时点已知状态不禁止
关键质量数据 available_at <= decision_time
```

### 新模块建议

```text
src/stock_analyzer/data/asof_universe.py
```

优先复用已有模块。

### Expected Active Universe

coverage denominator 不再使用所有当前 provider index。

第一版在停牌日历不完整时可采用：

```text
过去 5 个交易日出现过 >=1 根合法 bar
且上市日期 <= T
```

并单列 known suspended。

### 必须记录

```text
universe_snapshot_id
as_of
eligible_count
expected_active_count
survivorship_coverage
reason_counts
```

### 测试关键例

```text
A: 2026-01 已上市
B: 2026-10 才上市
```

回测 2026-09：

```text
B 不得进入 universe
B 不得进入 coverage denominator
```

### 禁止

- 用当前 ST 状态回填所有历史；
- 用当前退市名单过滤所有历史；
- 假装数据源已有完整历史退市覆盖。

无法证明时标记：

```text
survivorship_coverage = incomplete_or_unknown
```

---

# S04 — SelectionContract 300/100/50
**原方案：P0-07**

## 给 ZCode 的提示词

只执行 **S04：统一 production night 与 historical night-equivalent 漏斗契约**。

### 当前问题

生产夜扫：

```text
Quality300 -> Light100 -> Deep50
```

历史回测曾使用：

```text
Quality100
```

两者不可直接比较。

### 目标

建立明确 `SelectionContract`：

```text
contract_id = night_alpha_v2_v1
quality_target = 300
light_target = 100
deep_target = 50
final_cap = 5
allow_zero_signal = true
```

### 重点

```text
src/stock_analyzer/runtime/services/week5_selection_engine.py
src/stock_analyzer/runtime/services/week5_historical_runner.py
src/stock_analyzer/config.py
```

不要把所有 profile 强行统一。  
intraday/monster 可有独立 contract。

### 报告必须写

```text
selection_contract_id
quality_target
light_target
deep_target
final_cap
```

### Done When

同一 contract 下：

```text
production night shadow
historical night-equivalent
```

都明确报告：

```text
300 -> 100 -> 50
```

且 contract ID 相同。

---

# S05 — Registry / Archive 治理
**原方案：P0-02**

## 给 ZCode 的提示词

只执行 **S05：Model Registry / Archive 完整性治理**。

### 已知基线

- registry 无 champion；
- 15/21 `artifact_uri` 指向 dataset manifest；
- archive retention=5；
- alias 与 immutable artifact 混用。

### 目标

未来新模型必须 100% 可追溯：

```text
model_id
immutable artifact_uri
artifact_kind
artifact_content_hash
artifact_created_at
dataset_manifest_id
feature_schema_id/hash
label_policy_id/hash
promotion_event_id
promoted_at
revoked_at
```

### 必须实现

1. 新写入 registry 的 model URI 必须真指向 model artifact；
2. dataset manifest 单独引用；
3. serving manifest 独立：
   `artifacts/model_serving_manifest.json`
4. serving alias 可继续兼容，但不是身份真相源；
5. archive retention 提升到至少 50 或按容量管理；
6. 生成历史 reconciliation report。

### 历史坏记录

不得伪造历史。

只标记：

```text
legacy_manifest_pointer
legacy_empty_hash
legacy_alias_pointer
unrecoverable_artifact
```

### Done When

新训练模型能从 `model_id` 定位 immutable artifact，并重算 hash 一致。

---

# S06 — HistoricalModelResolver
**原方案：P0-03**

## 给 ZCode 的提示词

只执行 **S06：HistoricalModelResolver**。

前提：S01、S05 PASS。

### 建议模块

```text
src/stock_analyzer/models/historical_resolver.py
```

### 两种模式

```text
strict_production_replay
pit_research
```

必须严格区分。

#### strict_production_replay

回答：

> 当天真正生产激活的模型是谁？

若历史 activation 证据不存在：

```text
unscorable
```

禁止猜。

#### pit_research

回答：

> as_of 以前真实存在且训练数据时间合法的最近研究模型是谁？

### 合法性至少要求

```text
artifact exists
actual hash == registry hash
artifact_created_at <= decision_time
feature schema valid
label policy valid
dataset manifest exists
manifest 无 blocking quality flag
training outcomes 成熟
无 future feature availability
```

### 绝对禁止

```text
no eligible PIT model -> fallback current serving model
```

正确结果：

```text
status = unscorable
reason = no_eligible_pit_model
```

### 时间语义

所有 resolver 时间必须 timezone-aware。

旧字段不可信时：

```text
time_semantics_unverified
```

### Done When

任何 as_of 都不会加载 as_of 之后创建/激活的模型；找不到时明确 unscorable。

---

# S07 — Feature Price / Execution Price 分离
**原方案：P0-06**

## 给 ZCode 的提示词

只执行 **S07：QFQ Feature 与 Raw Execution 分离**。

### 原则

```text
Feature Series != Tradable Execution Series
```

### Feature

允许 qfq，但必须固定规则并证明 as-of 安全。

### Execution

必须 raw：

```text
open/high/low/close
up_limit/down_limit
amount
fee
slippage
fillability
```

### 报告必须同时写

```text
feature_price_mode
execution_price_mode
```

### Corporate Action

第一版若无法完整处理：

```text
execution_uncertain
```

并从主评价样本剔除，同时保留数量统计。

### 禁止

用 qfq open/close 当真实成交价。

---

# S08 — Data Health Gate / Market Breadth Gate
**原方案：P0-08**

## 给 ZCode 的提示词

只执行 **S08：数据健康与市场广度双门拆分**。

### 已知问题

- `market_breadth.json` 生产缺失；
- live 路径缺失时 fail-open；
- 历史 coverage denominator 曾不合理。

### 目标结构

```text
Data Health
  broken -> V2 禁止新买
  degraded -> shadow/记录
  healthy -> 才进入 Market Breadth

Market Breadth
  weak -> 风险门
  normal/strong -> 正常
```

### Data Health 至少包含

```text
trade_date freshness
expected-active coverage
board-level coverage
feature snapshot coverage
model identity health
price-series availability
```

### Breadth producer

绑定真实 night data-ready 数据源，不应只依赖已关闭的 warehouse sync 服务。

### 灰度

1. 先产 artifact，不影响决策；
2. 连续 >=5 个生产日观察；
3. 历史回放 >=60 日；
4. 检查 board coverage；
5. 才考虑真正 fail-closed。

### Coverage

```text
valid_expected_active / expected_active
```

禁止：

```text
coverage 坏但 breadth score 高 -> 放行
```

---

# S09 — Model / Label Semantic Guard
**原方案：P0-09**

## 给 ZCode 的提示词

只执行 **S09：模型输出语义守卫**。

### 目标

每个模型输出必须明确：

```text
label_policy_id
label_name
horizon
price_basis
output_kind
calibration
```

`output_kind` 至少：

```text
probability
rank_score
expected_return
risk_score
```

### 关键规则

`rank_quantile classifier` 的概率只能解释为该标签定义下的正类概率/分值。

禁止显示成：

```text
未来上涨概率
```

除非模型确实训练的是：

```text
P(net_return_h > 0)
```

并有 OOS calibration。

### 当前 legacy

仅标：

```text
legacy_model_health = degraded_unverified
```

不要自动反转、不要自动替换。

---

# S10 — Decision Log + Outcome Maturation
**原方案：P0-10**

## 给 ZCode 的提示词

只执行 **S10：Alpha V2 Decision Log 与 Outcome Maturation**。

### 目标

每天盘后保存当时真实 prediction snapshot，未来 outcome 成熟后独立追加。

### 目录

```text
artifacts/alpha_v2/decisions/YYYY/MM/
artifacts/alpha_v2/outcomes/YYYY/MM/
artifacts/alpha_v2/manifests/
```

### Decision 最少字段

```text
signal_date
symbol
eligible
quality_rank
light_rank
deep_rank
legacy_score
legacy_reject_reasons
v2_rank_score
v2_expected_return
v2_direction_score
v2_risk_score
model identity
feature schema
label policy
data snapshot
selection contract
```

尚未实现 V2 Head 时允许字段为：

```text
null / not_available
```

不得编造。

### Outcome

只在 T+3/T+5/T+10/T+15 成熟后写。

禁止 signal 当天提前写未来数据。

### 不新增新 DB

第一阶段：

```text
JSONL + 现有 DuckDB 能力
```

### Done When

任选一天能完整回答：

- 当时 universe；
- 当时模型；
- 当时候选排序；
- 当时预测；
- 后续真实成熟 outcome。

---

# S11 — Label V2
**原方案：P1-01**

## 给 ZCode 的提示词

只执行 **S11：多 Horizon 可执行 Outcome / Label V2**。

### 保留 Legacy

不得删除旧 label。

### 新增至少

```text
net_return_3d
net_return_5d
net_return_10d
net_return_15d
excess_return_3d
excess_return_5d
excess_return_10d
excess_return_15d
mae_3d/5d/10d
mfe_3d/5d/10d
up_net_5d
up_excess_5d
tp8_before_sl5_10d
```

### 第一版 primary research target

```text
5D executable excess return
```

但必须同时保留 3/5/10/15D，后续测 IC decay。

### 所有 outcome 必须使用

- S02 的真实 entry；
- S07 的 raw execution；
- 明确 benchmark；
- 成本后净收益。

---

# S12 — Benchmark 体系
**原方案：P1-02**

## 给 ZCode 的提示词

只执行 **S12：Alpha Benchmarks**。

每个 prediction 至少有：

### A

```text
Eligible Universe EW
```

### B

```text
Quality300 EW
```

这是最关键增量基准。

### C

```text
Style-Matched Control
```

至少匹配：

```text
行业
流通市值 bucket
20D volatility
20D momentum
成交额/换手 bucket
```

输出：

```text
residual_excess_return
```

指数只能做市场背景，不能替代以上基准。

---

# S13 — Winner Recall
**原方案：P1-03**

## 给 ZCode 的提示词

只执行 **S13：Winner Recall**。

### 定义

每天 Quality300 按真实 5D executable excess return 排序，赢家集合至少支持：

```text
Top10%
Top20
```

计算：

```text
Recall@Light100
Recall@Deep50
Recall@FinalCandidate
```

### 输出

每日 +：

```text
20D rolling
60D rolling
```

### 目的

识别真正未来赢家是否在前级漏斗被过早杀掉。

---

# S14 — Feature Availability / Leakage Audit
**原方案：P1-04**

## 给 ZCode 的提示词

只执行 **S14：特征可用性和穿越审计**。

### 每个 active feature group 建立元数据

```text
source
available_at_rule
asof_safe
missing_policy
price_series_mode
historical_coverage
```

### 高风险优先审计

```text
财务
股东人数
北向
融资融券
大宗
龙虎榜
intraday
news/learning 派生
```

必须证明：

```text
available_at <= decision_time
```

不能只看报告期。

### 缺失值

重要组增加：

```text
feature_group_missing_flag
```

不能把 missing 全部混成真实 0。

### 输出

建立：

```text
Daily-Only Safe Feature Set
```

无法证明 PIT 的 feature 不进入 Base V2。

---

# S15 — Simple Factor Baseline
**原方案：P1-05**

## 给 ZCode 的提示词

只执行 **S15：可解释简单因子 baseline**。

### 建议组

```text
Trend / Reversal
Liquidity
Volatility
Relative Strength
Quality
```

每组横截面 rank、方向统一后组合。

### 要求

- 完全使用 S14 safe features；
- 同样使用 S11 outcomes；
- 同样使用 S12 benchmark；
- 每次 ML 评估必须同时显示 simple baseline。

目的：

> 判断 ML 是否真的比简单稳定因子组合更好。

---

# S16 — Shared Feature Matrix + Multi-Head
**原方案：P1-06**

## 给 ZCode 的提示词

只执行 **S16：共享 Feature Matrix + 多 Head V2**。

### 资源原则

禁止：

```text
完整全市场 pipeline × 4
```

必须：

```text
fetch once
feature once
matrix once
predict multiple heads
persist once
```

### Head A：Alpha Rank

主目标：

```text
5D executable excess return 横截面 rank
```

可实现 ranking 或 regression 后 rank。

训练 grouping：

```text
decision_date
```

### Head B：Expected Return

预测未来 executable net excess return。

### Head C：Direction

仅此 Head 可以用于：

```text
P(net_return_5d > 0)
P(excess_return_5d > 0)
```

需要 OOS calibration。

### Head D：Risk

例如：

```text
P(MAE_5d <= -5%)
expected_MAE_5d
```

### 本阶段仍为 research/shadow

不得接管正式 Final。

---

# S17 — Cross Review V2
**原方案正文：P1-07 Cross Review V2**

## 给 ZCode 的提示词

只执行 **S17：Cross Review V2 Shadow 观测层**。

### Legacy 不动

Legacy 仍保持原绝对概率门。

### V2 第一阶段只观测

```text
lgbm_rank_pct
xgb_rank_pct
rank_disagreement
prob_disagreement
```

### 目标

先回答：

> 模型分歧大的股票未来是否真的显著更差？

没有证据前，不得把 disagreement 变成新的 100% 硬否决。

### 后续可能策略

若有证据：

```text
共同 Top X%
```

但本阶段不要自动上线。

---

# S18 — Final Decision Policy V2
**原方案正文：P1-08**

## 给 ZCode 的提示词

只执行 **S18：Final Decision Policy V2 Shadow**。

### Candidate

```text
Deep50
```

先按：

```text
alpha_rank
```

排序，同时观察：

```text
expected_excess
P(up)
risk
fillability
data health
market regime
```

### Shadow 每日必须保存

```text
v2_top1
v2_top3
v2_top5
```

即使暂时不满足未来正式 gate，也要保存研究候选。

### 禁止

- 不创建“新70分”；
- 不强制每天 5 只；
- 不接管 Legacy；
- 不降低 Legacy 门槛。

---

# S19 — Purged Walk-Forward / OOS 评估
**原方案任务卡 P1-07；正文第 7 节**

## 给 ZCode 的提示词

只执行 **S19：Purged Walk-Forward**。

### 主规则

禁止 random split 作为主要验证。

训练报告必须写：

```text
train date range
validation range
test range
purge days
embargo days
max label horizon
```

### 时间隔离

对于最大评价 horizon：

```text
purge >= label/execution overlap
embargo >= 主要最大 horizon
```

若 15D 参与主实验，严格实验边界至少覆盖相应重叠风险。

### 统计单位

主要单位是：

```text
decision date
```

而不是把同一天 300 股票当 300 独立样本。

实现/报告支持：

```text
date-block bootstrap
HAC/Newey-West（可选但推荐）
non-overlapping anchor robustness
```

### 样本治理门

```text
20 mature dates = 仅失败预警
60 = 第一轮方向判断
120 OOS = 才允许讨论 Advisory 替代
~250 = 才讨论自动治理/稳定参数
```

---

# S20 — Legacy vs V2 Shadow Dual Run
**原方案：P2-01**

## 给 ZCode 的提示词

只执行 **S20：生产双轨 Shadow**。

### 结构

```text
Legacy -> 原正式结果/通知
V2 -> artifacts/alpha_v2 + shadow result
```

V2 不改变正式 action。

### 每日输出

```text
legacy_final
v2_top1
v2_top3
v2_top5
legacy_reject_reason
v2_data_health
v2_alpha_rank
v2_expected_return
v2_direction
v2_risk
```

### 回滚

单一 Feature Flag：

```text
alpha_v2.enforce_final_selection = false
```

本阶段必须始终保持 false。

---

# S21 — Daily Alpha Health Report
**原方案：P2-02**

## 给 ZCode 的提示词

只执行 **S21：每日 Alpha V2 健康报告**。

至少包含：

1. Identity
2. Data Health
3. Funnel
4. Winner Recall
5. Score Distribution
6. Alpha Quality
7. Execution
8. Drift / Governance

### Alpha Quality

```text
20D/60D Rank IC
Top3 excess
Top5 excess
quantile monotonicity
```

### Review Trigger

至少支持：

```text
20D Rank IC < 0
AND 60D Rank IC <= 0
AND 60D Top5 excess <= 0
```

触发后只能进入人工 review。

禁止：

```text
自动降阈值
自动上线 retrain
```

---

# S22 — 性能与 NAS 资源加固
**原方案：第 12 节**

## 给 ZCode 的提示词

只执行 **S22：性能/资源硬化**，前提是所有正确性门已经通过。

### 当前资源约束

```text
NAS 8 CPU / 16GB
API 4GB
heavy 4GB
critical 3GB
```

### 优先级

1. feature snapshot reuse；
2. 避免 symbol 重复 fetch bars；
3. prediction batch；
4. outcome/learning persistence batch；
5. 限制并发峰值。

### 必须保持

- deterministic；
- auditability；
- 单次 feature matrix；
- 多 head 共享。

### 验收必须给出

```text
wall time
peak RSS
stage timings
baseline vs after
```

性能优化不得改变结果语义。

---

# S23 — Theme / News / Intraday 增量实验
**原方案：P3**

## 给 ZCode 的提示词

只执行 **S23：增量实验基础设施**。

前提：

- Base Alpha 已通过 P1/P2；
- 至少达到研究门所需样本；
- Codex 明确允许进入 P3。

### Theme

保持 shadow。

固定同日、同 pool、同 base alpha：

```text
control = base
experiment = base + theme
```

至少目标：

```text
>=60 trading dates
>=30 independent affected dates
>=200 affected symbol-dates
```

只看 paired excess，不看出票数量。

### News

新闻路径完整/PIT 可证明前不得进入主模型。

### Intraday

先恢复：

```text
coverage
freshness
historical PIT
```

再测试是否有增量。

禁止把不完整分钟数据填 0 后和完整样本混训。

---

# 3. 阶段完成与推进规则

严格顺序建议：

```text
S00
→ S01
→ S02
→ S03
→ S04
→ S05
→ S06
→ S07
→ S08
→ S09
→ S10
→ S11
→ S12
→ S13
→ S14
→ S15
→ S16
→ S17
→ S18
→ S19
→ S20
→ S21
→ S22
→ S23
```

其中部分阶段在工程上可以并行，但**当前项目不建议并行施工**，因为需要逐门禁建立可信链。

---

# 4. 每次给 ZCode 时的统一开头

可复制下面模板，再附对应 Sxx 正文：

```text
请先读取：
1. StockAnalyzer_Alpha_V2_完整改造方案_20260917.md
2. StockAnalyzer_Alpha_V2_ZCode_阶段施工提示词_20260917.md
3. docs/alpha_v2/PROGRESS.md（如存在）

你现在只执行 Sxx，不得提前执行下一阶段。

开始前必须完成 git preflight。
施工后必须运行对应测试、生成审计工件、更新 PROGRESS.md，并输出完整 Sxx Implementation Report。

未经用户明确授权：
- 不 git push
- 不部署生产
- 不重启容器
- 不切 serving model
- 不 promote model

如果发现超出本阶段范围的问题，只记录到 Deferred Findings，不顺手修改。
```

---

# 5. 最终施工原则

整个 Alpha V2 项目的目标不是：

```text
每天一定选出票
```

而是建立可证明的：

```text
PIT universe
+ PIT model identity
+ T+1 executable outcome
+ 同口径 300/100/50
+ Alpha Ranking
+ clean OOS
+ Shadow governance
```

任何阶段如果通过放松门槛增加出票，但没有增加可验证 Alpha，视为失败。
