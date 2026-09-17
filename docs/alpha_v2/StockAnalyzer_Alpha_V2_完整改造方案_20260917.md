# StockAnalyzer Alpha 选股体系 V2.0 完整改造方案

> 版本：V2.0 Engineering Blueprint  
> 基线日期：2026-09-17  
> 适用仓库：`zxj6827111-blip/StockAnalyzer`  
> 评审基线分支：`fix/asof-breadth-gate-coverage-0917`  
> 评审基线 commit：`7e9e33bdb9d03506cff5dfff29c78b1c95019541`  
> 目标：在不停止现有生产夜扫、不强行降低风险阈值、不引入新的 DB/MQ/第三方基础设施的前提下，把 StockAnalyzer 从“综合打分+硬阈值筛选器”升级为“可审计、可回测、可灰度、可持续验证的 A 股 Alpha Ranking 系统”。

---

## 0. 给本地 AI / Codex 的执行指令（必须先读）

本文件不是讨论稿，而是施工蓝图。执行本方案的 AI 必须遵守以下规则。

### 0.1 不允许一次性重写系统

必须按本文 `P0 -> P1 -> P2 -> P3` 顺序施工，每一个任务独立提交、独立测试、独立可回滚。

### 0.2 V2 默认只能 Shadow

在明确写出“允许切换”的验收门之前：

- 不替换 Legacy 正式夜扫；
- 不修改 Legacy `final_signal_min_threshold=70`；
- 不为了增加出票数量降低 Cross Review 阈值；
- 不调整 LGBM/XGB/meta 权重；
- 不把 theme/news 正式并入 Alpha；
- 不自动 promote 新模型；
- 不因为单只股票表现好坏反向调参。

### 0.3 每个任务必须留下 6 项证据

每个任务完成后，必须输出：

1. 修改文件清单；
2. 行为变化说明；
3. 新增/修改测试；
4. 测试命令和测试结果；
5. 生成的审计工件路径；
6. 回滚方法。

建议在仓库增加：

```text
artifacts/alpha_v2/audit/
docs/alpha_v2/PROGRESS.md
```

### 0.4 如果当前 HEAD 已经不是基线 commit

先生成差异报告，再施工：

```text
baseline_commit = 7e9e33bdb9d03506cff5dfff29c78b1c95019541
current_commit  = <当前 HEAD>
```

必须重点 diff 本文列出的关键文件。不得假设 2026-09-17 之后的代码仍与本方案完全一致。

### 0.5 施工基本原则

- 正确性优先于出票数量；
- 研究口径优先于模型复杂度；
- 先证明排序有 Alpha，再讨论阈值；
- 生产和历史必须使用同一数据契约、同一漏斗契约；
- “0 只”允许是正确答案；
- 无法证明 Point-in-Time 的数据不得进入历史训练/回测；
- 无法找到合法历史模型的日期必须标记 `unscorable`，禁止悄悄回退当前模型；
- 所有模型输出必须说明“预测的到底是什么”，禁止把任意 score 称为“上涨概率”。

---

# 1. 改造目标

## 1.1 不再把“每天选出股票”作为系统目标

V2 的目标不是：

> 每天必须推荐 1～5 只。

而是：

> 在 T 日收盘后，只使用 T 时点已经可获得的信息，对 T+1 可实际买入的股票进行横截面排序，使高排名股票在未来 3/5/10/15 个交易日的可实现净收益、超额收益和风险收益表现，稳定优于同日可选股票池；没有足够优势时允许输出 0 只。

## 1.2 V2 的首要业务指标

最终系统不以 `score >= 70` 作为主要成功标准，而以以下指标为核心：

1. **Top3 / Top5 未来净超额收益**；
2. **Rank IC / Spearman IC**；
3. **分位收益单调性**；
4. **Winner Recall（各漏斗阶段保留未来赢家的能力）**；
5. **可成交率 / no-fill / delayed-fill**；
6. **MAE / MFE / 尾部损失**；
7. **相对 Quality Pool 的增量 Alpha**。

## 1.3 V2 最终结构

```text
全市场证券索引
      │
      ▼
Point-in-Time Eligible Universe
      │
      ▼
Quality Pool 300
  （资格 + 基础质量）
      │
      ▼
Light 100
  （高召回，少做强判断）
      │
      ▼
Deep 50
  （共享 Feature Matrix）
      │
      ├──────────────┬──────────────┬──────────────┐
      ▼              ▼              ▼              ▼
 Alpha Rank       Return Head    Direction Head   Risk Head
 相对排序         预期收益       方向/概率        MAE/尾部风险
      └──────────────┴──────────────┴──────────────┘
                         │
                         ▼
                V2 Decision Policy
                         │
      ┌──────────────────┼───────────────────┐
      ▼                  ▼                   ▼
 Data Health Gate   Execution Gate      Market/Risk Gate
      │                  │                   │
      └──────────────────┴───────────────────┘
                         │
                         ▼
                    Final 0~5
                         │
                         ▼
              Shadow / Advisory / Legacy
```

---

# 2. 2026-09-17 生产基线：已经验证的事实

本节是施工基线，不是推测。

证据包主要文件：

```text
prod_evidence_20260917/README.md
prod_evidence_20260917/KEY_FINDINGS.md
prod_evidence_20260917/01_config/effective_config.redacted.json
prod_evidence_20260917/01_config/effective_config_blocks.redacted.json
prod_evidence_20260917/02_model_identity/model_registry_explicit_columns.json
prod_evidence_20260917/02_model_identity/model_files_sha256.json
prod_evidence_20260917/02_model_identity/dataset_manifests_timeline.json
prod_evidence_20260917/06_derived/model_ledger.json
prod_evidence_20260917/06_derived/backtest_daily_funnel.csv
prod_evidence_20260917/06_derived/nightly_funnel_by_run.csv
```

## 2.1 生产代码身份

生产实际运行：

```text
branch = fix/asof-breadth-gate-coverage-0917
commit = 7e9e33bdb9d03506cff5dfff29c78b1c95019541
```

仓库 tracked tree 干净；`.build_commit` 与运行镜像一致。

## 2.2 生产漏斗

夜扫真实口径：

```text
全市场约 5487
  -> Quality 300
  -> Light 100
  -> Deep 50
  -> Final threshold 70
  -> final cap 5
```

但历史 Week5 回测当前实际使用：

```text
Quality 100
  -> 100
  -> 100
```

因此生产和历史研究口径不一致。

## 2.3 Final = 0 不是单一“70 分问题”

9/1～9/16 回测及生产夜扫显示：

- `below_min_threshold` 经常拒绝 99～100% 候选；
- `cross_review_failed` 同时拒绝约 96～100% 候选；
- 两者是联合门，不是单独把 70 调低即可解决。

因此 V2 禁止先调阈值。

## 2.4 当前有效配置中的关键值

当前生产有效配置（不是代码默认值）：

```text
models.inference_score_source = calibrated
models.cross_review.p_lgbm_min = 0.60
models.cross_review.p_xgb_min  = 0.55
models.cross_review.p_meta_min = 0.54
models.cross_review.max_diff   = 0.18

week5.night_quality_target = 300
week5.night_light_candidate_target = 100
week5.night_deep_candidate_target = 50
week5.universe_quality_target_size = 100
week5.light_candidate_target = 100
week5.deep_candidate_target = 20
week5.final_signal_cap = 5
week5.final_signal_min_threshold = 70
week5.allow_zero_signal = true

training.enabled = false
training.artifact_path = artifacts/model_v1.json
training.model_archive_retention_count = 5
training.embargo_days = 0

auto_promotion.enabled = false
auto_promotion.auto_load_predictor = true

theme.mode = shadow
evolution.news_risk_mode = shadow
app.mode = simulation
app.advisory_only = true
```

## 2.5 当前实际在服模型

当前 `training.artifact_path=artifacts/model_v1.json`：

```text
sha256 = 71f64a21c131fe594d871bf4eaa87e75d8667fb722556de0f2f85a8607cb2559
artifact_created_at = 2026-08-16
registry model_id = model_v3_6d7486bc1af6
role = challenger
lifecycle_state = trained
```

当前 Registry **没有 champion 行**。

## 2.6 当前在服模型指标是严重警报

Registry 对该 hash 的训练记录：

```text
AUC = 0.331355
accuracy = 0.661077
precision_at_k = 0
recall_at_k = 0
mean_prob_spread = -0.158217
embargo_days = 0
```

这不能直接得出“把概率倒过来就行”的结论，但足以得出：

> 当前模型不能被默认为一个已经证明有效的 Alpha 模型。

V2 必须把它作为 Legacy 对照，而不是继续以它的绝对分数为事实基准。

## 2.7 新 challenger 也不能直接上线

9/15 新 challenger 的部分离线指标约为：

```text
AUC calibrated blend ≈ 0.5482
AUC raw blend        ≈ 0.5436
AUC raw XGB          ≈ 0.5525
precision@k          ≈ 0.5850
```

但：

- 没有 champion；
- 没有完成 V2 Point-in-Time / execution / OOS 体系验证；
- 当前历史链不完整；
- `embargo_days` 仍为 0；
- 当前生产并未加载该 challenger。

因此严禁仅凭 AUC 直接替换生产模型。

## 2.8 当前“配置标签”和“在服模型标签”语义不一致

当前有效配置：

```text
labels.primary = soup_10d_tp8_before_sl5
labels.pnl_price_basis = next_tradable_open
labels.basis = return_rank
labels.horizon_days = 10
```

但当前在服模型绑定：

```text
label_policy_v1_e2afc1135a3f
label = soup_10d_tp8_before_sl5
price_basis = next_tradable_vwap
TP = +8%
SL = -5%
conflict_policy = bar_shape_heuristic
```

而新 challenger 使用：

```text
label_policy_v3_b0b3724553b5
label = label_return_rank
price_basis = next_tradable_open
conflict_policy = rank_quantile
```

因此当前系统存在**模型输出语义、当前配置语义、研究语义三者未严格绑定**的问题。

这必须在 P0 修复。

## 2.9 历史模型身份报告目前不可信

`week5_historical_runner._resolve_model_info()` 优先找 Registry champion；找不到时会从 training bootstrap status 补 `trained_at`。

但实际 `AnalyzerPipeline` 始终直接：

```text
_load_predictor(config.training.artifact_path)
```

于是会出现：

- 报告称 `model_trained_at = 2026-09-15`；
- 实际加载的文件却是 2026-08-16 的 `artifacts/model_v1.json`。

因此 P0 必须把“真实加载工件身份”作为唯一真相源。

### 对 9/1～9/16 区间的更正

由于当前实际在服 artifact 创建于 8/16，所以**不能再把 9/1～9/16 这段回测直接描述为“用了 9/15 的未来模型”**。

准确表述应为：

> 当前架构没有 HistoricalModelResolver，报告元数据错误，并且未来只要 serving alias 更新就可能产生历史模型泄露；但本次 9/1～9/16 实际加载的 artifact 创建于 8/16，从 artifact 创建时间本身看不晚于这些 as-of 日期。

仍需进一步验证该 artifact 的训练样本、Outcome maturity 和 timestamp 语义，不能因此宣称回测已完全 PIT。

## 2.10 历史模型归档链不完整

当前磁盘仅剩 5 个 model artifact。

历史 Registry 很多 `artifact_uri` 指向 dataset manifest，不是真模型。

因此：

- 8/16 之前严格历史模型回放基本不可做；
- 当前不能重建“当时生产 champion”的完整轨迹；
- 这属于系统本身历史治理缺失，不是证据包没采全。

## 2.11 Historical Universe 仍有 Point-in-Time 缺陷

`AsOfMarketDataProvider.list_symbols()` 当前直接透传完整 provider index。

因此未来上市股票、当前索引状态、历史退市存续等问题仍没有严格 historical membership 语义。

## 2.12 当前 holding curve 入场实现错误

历史决策时间固定为 T 日 15:30。

但 `backtest/holding_curve.py` 当前：

```text
entry_date = as_of
entry_price = entry_date 对应 bar 的 close
```

因此它在“盘后决策”场景中使用已经过去的 T 日收盘价作为入场价。

V2 必须改成 T+1 实际可成交语义。

## 2.13 特征价格与执行价格存在潜在口径冲突

生产数据源：

```text
data_source.vendor_zip_price_series_mode = qfq
```

而 evolution execution spec：

```text
execution_spec.price_series_mode = raw
execution_spec.dividend_treatment = explicit_cashflow
```

V2 必须明确：

- 特征可以使用稳定定义的复权序列；
- 真实成交、涨跌停、滑点、金额、手续费必须使用 raw execution price；
- 不得用 qfq open/close 直接当成交价格。

## 2.14 Breadth 两条路径目前方向相反

历史曾出现：

```text
coverage < 0.95 -> breadth_score_unavailable -> fail closed
```

`7e9e33b` 已修复其中“健康 breadth score 因 coverage 边缘不足被全拒”的问题。

但生产环境：

```text
market_breadth.json = MISSING
```

Production live path 在缺文件时 `breadth_unavailable -> fail open`。

因此当前是：

> 历史曾过严，生产却静默失效。

V2 必须把 Data Health 与 Market Breadth 分离。

## 2.15 分钟数据当前不能作为 V2 主模型前置条件

分钟 summary 已长期未更新；9 月回测没有可靠分钟覆盖。

因此 V2 基础 Alpha 第一阶段只依赖日线和可证明 Point-in-Time 的慢变量。

Intraday 作为后续增强模块独立恢复。

---

# 3. V2 的核心设计原则

## 3.1 Universe、Alpha、Risk 三层职责必须分离

### Universe / Quality

只回答：

> 这只股票是否值得进入研究候选池？

不要承担“未来一定上涨”的职责。

### Alpha

回答：

> 在同一天可选股票中，谁的未来可实现超额收益更高？

### Risk / Execution

回答：

> 即使 Alpha 看好，现在是否因为数据、成交、市场状态、极端风险而不能做？

Risk 只能减少交易，不允许被用于制造 Alpha。

## 3.2 Ranking 优先于绝对分数

V2 第一核心是：

```text
score 高 -> 未来超额收益更高
```

而不是：

```text
score 70.1 = 买
score 69.9 = 不买
```

## 3.3 70 分降级成 Legacy / 展示口径

在 V2 证明排序能力之后，可将 Alpha Rank、Return、Risk 映射为 0～100 展示分。

但 0～100 分不能再被误解成：

- 上涨概率；
- 固定收益率；
- 跨模型版本可直接比较的绝对量。

## 3.4 News / Theme 不进入 V2 Base Alpha

第一阶段：

```text
news = shadow
theme = shadow
```

只有证明独立增量后才能 promote。

## 3.5 Intraday 不进入 V2 Base Alpha

直到分钟链重新具备：

- 完整性；
- freshness；
- historical coverage；
- Point-in-Time 可证明性。

---

# 4. V2 必须新增的统一数据契约

建议新增模块：

```text
src/stock_analyzer/contracts/alpha_v2.py
```

如果仓库不希望新增 contracts 包，也可放入现有 types 模块，但必须形成稳定 dataclass / Pydantic contract。

## 4.1 DecisionIdentity

每次选股必须写入：

```text
signal_date
signal_time
timezone
code_commit
config_hash
data_snapshot_id
universe_snapshot_id
model_id
artifact_uri
artifact_content_hash
artifact_created_at
feature_schema_id
feature_schema_hash
label_policy_id
label_policy_hash
score_semantics
resolver_mode
```

## 4.2 SelectionContract

```text
contract_id = night_alpha_v2_v1
eligible_universe = PIT
quality_target = 300
light_target = 100
deep_target = 50
final_cap = 5
allow_zero_signal = true
```

历史 night-equivalent 回测和生产 night scan 必须使用同一 contract。

不要通过各处分散读取：

```text
universe_quality_target_size
night_quality_target
light_candidate_target
night_light_candidate_target
deep_candidate_target
night_deep_candidate_target
```

然后让不同路径自行解释。

## 4.3 EntryContract

V2 主研究口径建议：

```text
signal_time = T 15:30
primary_entry = T+1 session open, only if fillable
if suspended = no fill
if one-price limit-up / buy forbidden = no fill
price = raw open + configured slippage/impact
```

另保留 sensitivity：

```text
secondary_entry = next tradable open within <= 3 sessions
```

主指标必须使用 `primary_entry`。

## 4.4 OutcomeContract

每条 prediction 后续成熟：

```text
ret_net_3d
ret_net_5d
ret_net_10d
ret_net_15d
excess_ret_3d
excess_ret_5d
excess_ret_10d
excess_ret_15d
mae_3d
mae_5d
mae_10d
mfe_3d
mfe_5d
mfe_10d
first_hit_tp8_sl5_10d
fillable
entry_delay_days
entry_price_raw
exit_price_raw
```

---

# 5. P0：研究基础设施正确性改造

> P0 全部完成之前，禁止 Alpha 调参、70 分调参、Cross Review 放宽、模型权重调优。

---

## P0-00：建立 Alpha V2 Feature Flag 与基线清单

### 目标

保证后续所有改动都能 Shadow、灰度、回滚。

### 修改建议

涉及：

```text
src/stock_analyzer/config.py
config/default.yaml（或实际配置入口）
```

新增独立配置块，不复用 Legacy 参数：

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

### 禁止

- 不改变现有 `week5.final_signal_min_threshold`；
- 不改变 Legacy Cross Review；
- 不改变通知文案的正式买入结果。

### 验收

`alpha_v2.enabled=false` 时，现有夜扫 JSON 的 Legacy 业务字段必须与基线一致。

建议建立 golden regression fixture。

---

## P0-01：Model Identity Truth——实际加载谁，就报告谁

### 目标

彻底解决“报告一个模型，Pipeline 实际加载另一个模型”。

### 重点文件

```text
src/stock_analyzer/pipeline.py
src/stock_analyzer/models/predictor.py
src/stock_analyzer/runtime/services/week5_historical_runner.py
src/stock_analyzer/runtime/services/asof_backtest_service.py
```

### 改造要求

1. `AnalyzerPipeline` 初始化后提供只读 `model_identity()`；
2. 身份必须来自真实加载 artifact，而不是 bootstrap status；
3. 至少输出：

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

4. Registry metadata 只能用于补充，不能覆盖实际 artifact 事实；
5. 如果 Registry hash 与 actual hash 不一致：

```text
model_identity_status = mismatch
```

V2 research 直接 fail-closed。

### 当前基线验收样例

在不换模型情况下，历史任务应明确报告：

```text
actual_artifact_hash = 71f64a21...
artifact_created_at = 2026-08-16...
```

不能再把 `2026-09-15` bootstrap 时间当成实际模型训练时间。

### 测试

新增：

```text
tests/models/test_model_identity_truth.py
tests/runtime/test_week5_historical_model_identity.py
```

覆盖：

- 有 champion；
- 无 champion；
- bootstrap 比 artifact 新；
- Registry hash 错；
- artifact 缺失；
- serving alias 被覆盖。

---

## P0-02：Registry 与 Model Archive 治理

### 当前问题

- Registry 0 champion；
- 15/21 `artifact_uri` 指向 dataset manifest；
- archive retention 只有 5；
- alias 与真实 immutable artifact 混用；
- 同一旧 hash 在不同 registry row 中生命周期解释不一致。

### 目标

以后每个模型都能做到：

> 谁训练、用什么数据、什么标签、什么代码、什么 artifact、何时 promote、何时 revoke，全部可追。

### 重点文件

```text
src/stock_analyzer/models/registry.py
训练/注册模型的 service
promotion / evolution release service
```

### 要求

新增或严格执行：

```text
artifact_kind = model | dataset_manifest | alias
artifact_content_hash NOT NULL for model
artifact_created_at NOT NULL
promotion_event_id
promoted_at
revoked_at
```

### Serving 不再只依赖可覆盖 alias

建议增加：

```text
artifacts/model_serving_manifest.json
```

内容：

```json
{
  "model_id": "...",
  "artifact_uri": "...immutable archive...",
  "artifact_content_hash": "...",
  "activated_at": "...",
  "feature_schema_id": "...",
  "label_policy_id": "..."
}
```

`artifacts/model_v1.json` 可以继续作为兼容 alias，但不能再是身份真相源。

### Retention

将未来 model archive retention 从 5 提高到至少 50，或者按磁盘配额管理，而不是仅保留最近 5 个。

模型文件当前体积很小，优先保证可追溯。

### 历史坏记录

不要强行修改原始历史事实。

建立：

```text
model_registry_reconciliation_report.json
```

把历史异常行标记为：

```text
legacy_manifest_pointer
legacy_empty_hash
legacy_alias_pointer
unrecoverable_artifact
```

### 验收

新训练出来的每一个模型必须：

- 有 immutable artifact；
- hash 可重算一致；
- Registry artifact URI 指向真实模型；
- dataset manifest 单独引用；
- serving activation 有独立事件。

---

## P0-03：HistoricalModelResolver

建议新增：

```text
src/stock_analyzer/models/historical_resolver.py
```

### 两种模式必须区分

#### strict_production_replay

回答：

> 当天生产真正启用的 champion 是谁？

当前历史数据不足以完整支持。

#### pit_research

回答：

> 在 as_of 时点以前已经真实存在、且训练数据合法成熟的最近一个研究模型是谁？

当前可用于研究，但必须明确它不是“历史生产 champion 回放”。

### Resolver 合法性条件

模型必须同时满足：

```text
artifact exists
actual hash == registry hash
artifact_created_at <= as_of decision_time
feature_schema hash valid
label_policy hash valid
dataset_manifest exists
manifest has no blocking quality flag
training samples/outcomes mature before training cutoff
no future feature availability
```

### 时间语义特别要求

证据包中存在 manifest `time_window_end` 与 `generated_at` 可能有 8 小时时区解释差异的历史记录。

因此必须：

- 所有 timestamp 统一带 timezone；
- 禁止 naive datetime；
- Resolver 不得盲目信任旧 manifest 时间字段；
- 无法证明时标记 `time_semantics_unverified`。

### 绝对禁止

```text
找不到历史模型 -> 回退当前 serving artifact
```

必须：

```text
status = unscorable
reason = no_eligible_pit_model
```

### 当前数据预期

8/16 之前大量日期会 unscorable。

这是正确结果。

### 测试

至少覆盖：

- as_of 在 artifact 创建前；
- as_of 恰好在创建后；
- hash mismatch；
- manifest missing；
- artifact missing；
- label hash mismatch；
- future-trained model；
- no model。

---

## P0-04：Point-in-Time Universe Resolver

建议新增：

```text
src/stock_analyzer/data/asof_universe.py
```

### 当前问题

`AsOfMarketDataProvider.list_symbols()` 使用当前完整 provider index。

### V2 Universe 定义

对于 as_of=T：

```text
first_available_bar_date <= T
listed_age >= 60 trading/calendar days（与现有规则明确一致）
不是 T 时点已知退市/ST禁入状态
T 时点不是长期停牌不可交易
有足够历史 bar
关键质量数据 available_at <= T 15:30
```

### 不得使用

- 当前时点股票名单直接代替历史名单；
- 当前 ST 状态代替历史 ST；
- 当前财务状态回填历史；
- 当前退市名单过滤历史全时期。

### Survivorship Bias 审计

vendor index 有 5818 只，而生产主索引约 5487。

V2 历史 universe 应优先从可用历史 bar / vendor index 构造，而不是只从当前活跃 `list_symbols()` 构造。

如果历史退市股票在数据源中本来就丢失，报告必须标记：

```text
survivorship_coverage = incomplete_or_unknown
```

禁止把结果称为“全市场无偏历史回测”。

### Expected Active Universe

为 Data Health coverage 新建：

```text
expected_active_symbols(T)
```

不要再用全 provider index 作为单日覆盖率分母。

第一版若无可靠停牌日历，可用：

```text
过去 5 个交易日内至少出现过 1 根合法 bar
且上市日期 <= T
```

作为近似 expected-active，并把“已知停牌”单列。

---

## P0-05：修复 T+1 Entry / Holding Curve

### 重点文件

```text
src/stock_analyzer/backtest/holding_curve.py
src/stock_analyzer/backtest/matcher.py
src/stock_analyzer/runtime/services/asof_backtest_service.py
```

### 当前错误

T 日 15:30 选股，却使用 T 日 close 作为 entry。

### V2 Primary Entry

```text
signal = T close after market
entry candidate = next trading session T+1
entry price basis = raw open
apply buy slippage / fee / impact
if suspended => no fill
if buy forbidden / one-price limit-up => no fill
```

### 新增 EntrySimulation

建议让 `ExecutionMatcher` 同时拥有：

```text
simulate_entry(...)
simulate_exit(...)
```

不要继续让 entry 成交逻辑散落在 holding curve。

`EntrySimulation` 至少输出：

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

### Primary 与 Sensitivity 分离

Primary：

```text
必须 T+1 可成交，否则 no fill
```

Sensitivity：

```text
允许 <= 3 sessions 延迟到 next tradable open
```

两个结果不能混成一个平均收益。

### 测试

必须覆盖：

- 正常 T+1 开盘；
- 停牌；
- 一字涨停；
- 普通涨停但有成交空间；
- IPO 无涨跌停期间；
- 创业板/科创板 20%；
- ST 5%；
- T+1 规则；
- 手续费/印花税日期版本。

---

## P0-06：Feature Price 与 Execution Price 分离

### 原则

```text
Feature Series != Tradable Execution Series
```

### Feature

可以使用 qfq，但必须保证：

- 生成规则固定；
- as-of 无未来 corporate action 信息泄露；
- 技术特征不依赖未来调整因子。

### Execution

必须 raw：

- open/high/low/close；
- up_limit/down_limit；
- tick；
- fee；
- amount；
- fillability。

### Corporate Action

V2 第一版可采用：

1. execution 用 raw；
2. 若持有期跨除权除息，显式处理现金分红/送转；
3. 若当前数据无法可靠处理，则先把 corporate-action window 标记为 `execution_uncertain` 并从主评价样本中剔除，同时保留数量统计。

禁止直接拿 qfq 价格模拟真实订单成交。

---

## P0-07：统一 Production / Historical SelectionContract

### 重点文件

```text
src/stock_analyzer/runtime/services/week5_selection_engine.py
src/stock_analyzer/runtime/services/week5_historical_runner.py
src/stock_analyzer/config.py
```

### 新契约

夜扫和对应历史回放：

```text
Quality 300
Light 100
Deep 50
Final cap 5
```

### 不影响其他 profile

例如 intraday / monster profile 如果确实需要 100/20，可继续使用独立 contract。

### 关键要求

历史回测报告必须写：

```text
selection_contract_id
quality_target
light_target
deep_target
final_cap
```

任何两个结果 contract 不同，不允许直接比较。

### 验收

同一天同一 snapshot：

```text
production night shadow 与 historical night-equivalent
```

应得到相同漏斗候选顺序（除被明确中性化的 news/theme/intraday 外）。

---

## P0-08：Data Health Gate 与 Market Breadth Gate 分离

### 当前问题

- `market_breadth.json` 不存在；
- production breadth 静默 fail-open；
- historical coverage denominator 又曾导致误判。

### 目标架构

```text
Data Health
   │
   ├─ broken -> 禁止 V2 new buy
   ├─ degraded -> shadow / 降权（需验证）
   └─ healthy
        │
        ▼
Market Regime / Breadth
        │
        ├─ weak -> 风险门
        └─ normal/strong -> 正常
```

### Data Health 至少包括

```text
trade_date freshness
expected-active coverage
board-level coverage
feature snapshot coverage
model identity health
price-series availability
```

### Breadth artifact 生成

不能继续依赖 `market_warehouse.enabled=true` 才有机会生成。

生产当前 `market_warehouse.enabled=false`，而主数据实际由 vendor overlay/delta 提供。

因此 breadth producer 应绑定“夜扫数据 ready 后的实际生产数据源”，而不是绑定已经关闭的 warehouse sync 服务。

### 灰度步骤

1. 先生成 artifact，不影响决策；
2. 连续至少 5 个成功生产日核对；
3. 回放至少 60 个历史交易日；
4. 检查 board coverage；
5. 再把真正的数据缺失从 fail-open 改为 fail-closed。

### Coverage 规则

不要继续固定解释为：

```text
valid / all_current_index >= 0.95
```

建议：

```text
coverage = valid_expected_active / expected_active
```

初始灾难底线可保留 0.90；正式阈值由历史 60 日 coverage 分布决定，例如以低分位数加安全边界形成 gate。

### 严禁

```text
coverage 不好，但 breadth score 很高，所以放行
```

Data Health 和 Market Regime 是两件事。

---

## P0-09：Model/Label Semantic Guard

建议新增运行时校验：

```text
src/stock_analyzer/models/semantics.py
```

### 目标

任何模型输出必须带：

```text
label_policy_id
label_name
horizon
price_basis
output_kind = probability | rank_score | expected_return | risk_score
calibration = none | isotonic | ...
```

### 规则

如果模型是：

```text
rank_quantile classifier
```

那么它的 probability 只能解释为：

> 在该训练标签定义下属于正类的模型分值/概率。

不能解释为：

> 股票未来上涨概率。

### 当前 serving model

由于其 AUC、precision@k、recall@k 与标签语义存在严重警报，V2 必须标记：

```text
legacy_model_health = degraded_unverified
```

但不要自动反转概率，也不要自动替换 challenger。

---

## P0-10：建立 Alpha V2 Decision Log 与 Outcome Maturation

### 目的

以后不要再靠“事后找 33 个候选”拼结果。

每个交易日盘后必须保存当时真实预测快照。

### 推荐目录

```text
artifacts/alpha_v2/decisions/YYYY/MM/decision_YYYYMMDD.jsonl
artifacts/alpha_v2/outcomes/YYYY/MM/outcome_YYYYMMDD.jsonl
artifacts/alpha_v2/manifests/run_*.json
```

### Decision 行最少字段

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
model_identity...
feature_schema...
data_snapshot...
```

### Outcome 后续成熟

不得在 signal 当天提前写未来数据。

T+3/T+5/T+10/T+15 成熟后独立回填到 outcome 文件。

### 不新建新数据库依赖

第一阶段用 JSONL + 现有 DuckDB 能力即可。

如果后续数据量大，再考虑写入现有 learning protocol DuckDB，不引入新数据库产品。

---

# 6. P1：Alpha 研究体系改造

P1 的任务是回答一个核心问题：

> 分数高的股票，是否在未来真实可成交收益上稳定优于分数低的股票？

---

## P1-01：Label V2——从单一 10D TP/SL 转成多目标 Outcome

### 不删除 Legacy label

Legacy 继续保留用于兼容和对照。

新增 V2 research labels。

### 至少生成

```text
net_return_3d
net_return_5d
net_return_10d
net_return_15d
excess_return_3d
excess_return_5d
excess_return_10d
excess_return_15d
mae_3d / 5d / 10d
mfe_3d / 5d / 10d
up_net_5d
up_excess_5d
tp8_before_sl5_10d
```

### 主研究目标

第一版将：

```text
5D executable excess return
```

作为 primary research target。

但这是**研究主目标，不是永久业务结论**。

必须同时做 3/5/10/15D IC decay，验证信号寿命。

---

## P1-02：Benchmark 体系

单独使用“全市场等权”不够。

每个预测至少生成三层 benchmark。

### Benchmark A：Eligible Universe EW

回答整个系统相对全体合格股票是否创造收益。

### Benchmark B：Quality Pool EW

最重要。

回答：

> Alpha Rank 相比“已经进入 Quality300 的股票”是否真的有增量。

### Benchmark C：Style-Matched Control

至少匹配：

```text
行业
流通市值 bucket
20D 波动 bucket
20D momentum bucket
成交额/换手 bucket
```

输出：

```text
residual_excess_return
```

指数（沪深300、中证全指等）只作为背景，不作为唯一 Alpha benchmark。

---

## P1-03：Winner Recall

这是漏斗改造的核心指标。

### 定义

每天对 Quality300 事后按真实 5D executable excess return 排序。

定义赢家集合，例如：

```text
Top10%
Top20
```

分别计算：

```text
Recall@Light100
Recall@Deep50
Recall@FinalCandidate
```

### 目的

如果未来真正最好的股票在 Light 阶段就大量消失，则后面的模型再好也无意义。

### 输出

每日和滚动：

```text
winner_recall_light_20d
winner_recall_deep_20d
winner_recall_light_60d
winner_recall_deep_60d
```

---

## P1-04：Feature Availability / Leakage Audit

当前 feature schema 已包含大量：

- 技术指标；
- 财务；
- 北向；
- 融资；
- 大宗；
- 龙虎榜；
- 股东人数；
- intraday；
- learning protocol / news 派生量。

V2 必须给每个特征分组打标签：

```text
source
available_at_rule
asof_safe
missing_policy
price_series_mode
historical_coverage
```

### 高风险重点检查

```text
bg_roe
bg_debt_ratio
holder_count_*
northbound_*
financing_*
block_trade_*
lp_m*
intraday i1m/i5m
```

必须证明：

```text
available_at <= decision_time
```

而不是“数值所属报告期 <= as_of”就算安全。

### 缺失值

当前历史 schema 存在 `fill_zero_after_shift`。

V2 对重要数据组增加：

```text
feature_group_missing_flag
```

禁止把“数据不存在”与“真实值为 0”完全混为一谈。

### 第一阶段模型建议

先建立一个 **Daily-Only Safe Feature Set**：

- 价格；
- 成交量/成交额；
- 波动；
- 位置；
- 相对强弱；
- 可证明 as-of 的基本面。

暂时排除不能证明历史可用性的特征。

---

## P1-05：Simple Baseline 必须先建立

复杂 ML 模型必须同时与简单模型比较。

建议建立可解释 baseline：

```text
Trend/Reversal
Liquidity
Volatility
Relative Strength
Quality
```

例如每组做截面 rank，方向统一后等权平均。

目的不是长期使用简单模型，而是回答：

> ML 是否真的比一个简单、稳定、无复杂训练链的因子组合更好？

如果 ML 长期不能超越 simple baseline，就停止增加复杂度。

---

## P1-06：V2 多 Head 模型

### 性能原则

只做一次 Feature Matrix。

```text
Feature Matrix
  ├─ Rank Head
  ├─ Return Head
  ├─ Direction Head
  └─ Risk Head
```

不要跑三到四遍完整全市场 Pipeline。

生产证据显示真正昂贵的不是模型 inference，而是数据/snapshot/persistence 等环节。

### Head A：Alpha Rank（最重要）

建议目标：

```text
5D executable excess return 横截面 rank
```

可测试：

- LightGBM ranking；
- regression 后截面 rank。

训练 group 必须按 `decision_date`。

### Head B：Expected Return

预测：

```text
future executable net excess return
```

主要用于判断收益幅度，不要求精确到小数点。

### Head C：Direction

预测：

```text
P(net_return_5d > 0)
P(excess_return_5d > 0)
```

只有该 Head 才允许被称为“上涨概率/正收益概率”，且需要 OOS calibration。

### Head D：Risk

预测：

```text
P(MAE_5d <= -5%)
或 expected_MAE_5d
```

---

## P1-07：Cross Review V2

Legacy Cross Review 保持不变用于对照。

V2 不要继续默认：

```text
LGBM > 0.60 AND XGB > 0.55 AND meta > 0.54
```

### V2 Shadow 第一阶段

把模型分歧变成可观测量：

```text
lgbm_rank_pct
xgb_rank_pct
rank_disagreement
prob_disagreement
```

先验证：

> 分歧大的股票未来是否显著更差？

如果没有证据，不能把分歧直接当 100% 硬否决。

### 后续可能策略

如果数据证明双模型共识有增量，可以变成：

```text
共同 Top X%
```

而不是依赖绝对 probability threshold。

---

## P1-08：Final Decision Policy V2

Shadow 期不要直接调一个“新70分”。

推荐：

```text
Candidate = Deep50

先按 alpha_rank 排序

再观察：
expected_excess
P(up)
risk
fillability
data health
market regime
```

Shadow 每天固定输出：

```text
v2_top1
v2_top3
v2_top5
```

即使这些候选尚未达到未来正式 gate，也要保存，方便统计 Alpha。

正式上线后允许：

```text
0~5
```

但绝不能强制每天填满 5 只。

---

# 7. P1/P2：正确的训练与回测方法

## 7.1 禁止随机拆分作为主评估

必须按时间 walk-forward。

## 7.2 Purge + Embargo

当前 serving 模型训练：

```text
embargo_days = 0
```

对于 10D overlapping label，这是高风险。

V2：

```text
purge >= max label horizon + execution delay overlap
embargo >= primary/maximum horizon 对应交易日
```

如果同时评价 15D，主严格实验使用不小于 15 个交易日的隔离边界，具体实现通过 split builder 统一生成，不能由训练器自行猜测。

## 7.3 统计单位是 decision date，不是 300 个股票就等于 300 个独立样本

每天股票收益高度相关，10D forward return 跨日期也重叠。

因此：

- bootstrap 以日期 block 为主；
- block 长度至少接近最大主要 horizon；
- 可同时报告 Newey-West/HAC（lag≈H-1）；
- 另做 non-overlapping anchor robustness。

## 7.4 样本门槛

这些是工程治理门，不是“金融学绝对真理”。

### >= 20 个成熟 decision dates

仅用于发现明显失败，不能宣称模型有效。

### >= 60 个成熟 decision dates

允许做第一轮模型方向判断和 Shadow 继续/停止决策。

### >= 120 个成熟 OOS dates

才允许讨论正式替换 Legacy 排序模型。

### ~250 个交易日 OOS

才允许讨论稳定参数、自动 promotion 或较强结论。

---

# 8. Alpha V2 评价指标定义

## 8.1 Rank IC

每日：

```text
Spearman(predicted_alpha_rank, future_excess_return)
```

报告：

```text
mean IC
median IC
ICIR
20D rolling IC
60D rolling IC
positive IC ratio
block-bootstrap 95% CI
```

## 8.2 Quantile Monotonicity

对 Quality300 或 Deep50：

```text
Q1 ... Q10
```

报告每组未来 5D excess。

核心不是某个分组偶然上涨，而是：

> 分数越高，未来 excess 越高。

## 8.3 TopK Excess

```text
Top1
Top3
Top5
Top10
```

与：

```text
Quality300 EW
Eligible EW
Style-Matched
Simple Baseline
Legacy
```

同日配对比较。

## 8.4 Winner Recall

见 P1-03。

## 8.5 Execution Metrics

```text
next_session_fill_rate
no_fill_ratio
limit_up_no_fill_ratio
suspension_no_fill_ratio
entry_gap
slippage
entry_delay
```

## 8.6 Risk

```text
MAE
MFE
5% tail return
stop-loss hit
max drawdown
```

---

# 9. 模型 Promotion 门

## 9.1 Infrastructure Gate

以下全部通过：

- 真实 model identity 100% 可追；
- historical model 不允许 future fallback；
- T+1 entry 正确；
- raw execution price；
- PIT universe；
- selection contract 一致；
- decision log 可重放；
- same input deterministic；
- data/breadth 状态可追。

任何一项失败，禁止模型 promotion。

## 9.2 Research Gate（60D）

用于“继续 Shadow”而不是正式上线。

最低要求：

```text
mean Rank IC > 0
Top5 mean excess > 0
Top decile > median
无明显 winner recall 崩塌
execution no-fill 未超过现有治理上限
```

同时报告 block-bootstrap CI；若 CI 大幅跨 0，只能称“方向待验证”。

## 9.3 Advisory Promotion Gate（>=120D OOS）

建议要求：

1. primary 5D Rank IC > 0；
2. block-bootstrap 95% CI 下界 > 0，或至少在预先定义的稳健检验组合中一致显著为正；
3. Top5 vs Quality300 的配对 excess > 0；
4. Top decile / top20% 明显优于中位组；
5. 3/5/10D IC decay 有合理信号寿命，不是单日偶然；
6. Simple Baseline 没有稳定压过 V2；
7. V2 Risk/Execution 没有用极差成交性换取纸面 Alpha；
8. 数据质量和模型身份无红灯。

## 9.4 自动 Promotion

在至少约 250 个 OOS 交易日、模型治理稳定以前，保持：

```text
auto_promotion.enabled = false
```

---

# 10. P2：生产 Shadow 双轨改造

## 10.1 Legacy 不动

```text
Legacy -> 原正式通知
```

## 10.2 V2 Shadow 并行

```text
V2 -> artifacts/alpha_v2 + shadow report
```

禁止影响 Legacy action。

## 10.3 每日对照输出

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

## 10.4 Shadow 到 Advisory

只有通过本文 promotion gate，才允许飞书增加：

```text
【V2研究候选】
```

仍不得伪装成已正式替换 Legacy。

## 10.5 切换 Legacy

切换必须由单一 Feature Flag：

```text
alpha_v2.enforce_final_selection = true
```

回滚只需要设回 false，不删除任何 Legacy 代码。

---

# 11. V2 每日最小监控报告

每天至少包含以下 8 块。

## 11.1 Identity

```text
code_commit
config_hash
data_snapshot
model_id/hash
feature_schema
label_policy
```

## 11.2 Data Health

```text
expected active
valid bars
coverage
board coverage
latest trade date
feature snapshot coverage
breadth artifact status
```

## 11.3 Funnel

```text
Eligible
Quality300
Light100
Deep50
V2 Top5
Legacy Final
```

## 11.4 Winner Recall（成熟后）

3/5/10D outcome 成熟后滚动更新。

## 11.5 Score Distribution

```text
rank score quantile
expected return distribution
direction distribution
risk distribution
legacy calibrated score distribution
```

## 11.6 Alpha Quality

```text
Rank IC 20D/60D
Top3 excess
Top5 excess
quantile monotonicity
```

## 11.7 Execution

```text
fillable
no-fill
limit-up no-fill
slippage
gap
```

## 11.8 Drift / Governance

```text
feature drift
prediction drift
model age
schema mismatch
label mismatch
```

### Review Trigger

建议触发人工 Review：

```text
20D Rank IC < 0
AND 60D Rank IC <= 0
AND 60D Top5 excess <= 0
```

触发后：

- 禁止自动 retrain 上线；
- 禁止自动降阈值；
- 进入研究诊断。

---

# 12. P2：性能与 NAS 资源约束

当前 NAS：

```text
8 CPU
16GB RAM
API limit 4GB
heavy scheduler 4GB
critical scheduler 3GB
```

生产证据表明：

- inference 本身不是主要耗时；
- snapshot ensure、bar fetch、learning persistence 等成本更大；
- 因此一次 Feature Matrix + 多 Head 是正确方向。

## 12.1 禁止

```text
完整 Pipeline × 4 个模型 Head
```

## 12.2 推荐

```text
fetch once
feature once
matrix once
predict N heads
persist batch once
```

## 12.3 P2 性能优化优先级

正确性完成后再做：

1. 重用 feature snapshot；
2. 避免同一 symbol 重复 bars fetch；
3. prediction batch；
4. outcome/learning persistence batch；
5. 限制并发峰值。

不要为了速度破坏 deterministic 和审计链。

---

# 13. P3：News / Theme / Intraday 增量实验

P3 之前，Base Alpha 必须已经通过 P1/P2。

## 13.1 Theme

当前已经 shadow，继续保持。

实验：

```text
same date + same pool + same base alpha
control = base
experiment = base + theme
```

至少：

```text
>=60 trading dates
>=30 independent affected dates
>=200 affected symbol-dates
```

看 paired excess，不看“有 theme 后出票变多”。

## 13.2 News

新闻路径完整前，不进入主模型。

## 13.3 Intraday

先恢复分钟数据 coverage/freshness，再作为增量 Head 或执行增强。

不要把不完整分钟列填 0 后直接和完整历史样本混训。

---

# 14. 建议的 V2 配置结构

以下是目标结构示意，不要求第一提交一次实现全部字段。

```yaml
alpha_v2:
  enabled: false
  shadow_only: true
  enforce_final_selection: false
  artifact_root: artifacts/alpha_v2

  selection:
    contract_id: night_alpha_v2_v1
    quality_target: 300
    light_target: 100
    deep_target: 50
    final_cap: 5
    allow_zero_signal: true

  point_in_time:
    model_resolver_mode: pit_research
    fail_on_no_model: true
    universe_required: true
    feature_availability_required: true

  execution:
    primary_entry_mode: next_session_open
    delayed_entry_sensitivity_max_sessions: 3
    execution_price_series: raw
    feature_price_series: qfq

  outcomes:
    horizons: [3, 5, 10, 15]
    primary_horizon: 5
    benchmark: quality_pool
    style_matched_enabled: true

  heads:
    alpha_rank:
      enabled: true
    return:
      enabled: true
    direction:
      enabled: true
    risk:
      enabled: true

  research:
    min_dates_alert: 20
    min_dates_initial_review: 60
    min_dates_promotion: 120
    min_dates_auto_governance: 250
    block_bootstrap: true

  news:
    mode: shadow
  theme:
    mode: shadow
  intraday:
    mode: disabled_until_healthy
```

---

# 15. 推荐新增文件结构

```text
src/stock_analyzer/
  contracts/
    alpha_v2.py

  models/
    historical_resolver.py
    semantics.py
    alpha_heads.py

  data/
    asof_universe.py

  research/
    alpha_metrics.py
    benchmarks.py
    winner_recall.py
    walk_forward.py

  runtime/services/
    alpha_v2_shadow_service.py

artifacts/alpha_v2/
  audit/
  decisions/
  outcomes/
  manifests/
  reports/

 tests/
  alpha_v2/
  models/test_historical_resolver.py
  data/test_asof_universe.py
  backtest/test_entry_execution.py
  research/test_alpha_metrics.py
```

如仓库已有同类模块，应优先扩展现有模块，避免为了目录美观重复造轮子。

---

# 16. 任务依赖图

```text
P0-00 Feature Flag
   │
   ├── P0-01 Model Identity
   │      └── P0-02 Registry/Archive
   │             └── P0-03 HistoricalModelResolver
   │
   ├── P0-04 PIT Universe
   │
   ├── P0-05 T+1 Entry
   │      └── P0-06 Price Series Split
   │
   ├── P0-07 SelectionContract
   │
   ├── P0-08 Data Health/Breadth
   │
   └── P0-09 Semantic Guard
              │
              └── P0-10 Decision/Outcome Log
                         │
                         ▼
                    P1 Labels
                         │
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
   Benchmarks      Winner Recall      Feature Audit
        └────────────────┼────────────────┘
                         ▼
                    Simple Baseline
                         ▼
                    Multi-Head V2
                         ▼
                    Walk Forward
                         ▼
                    Shadow 60D+
                         ▼
                    OOS 120D+
                         ▼
                   Advisory Promotion
```

---

# 17. 开工任务卡（建议直接交给 AI 逐项执行）

## TASK P0-00

**标题：Alpha V2 Feature Flag 与零行为变化基线**

### 指令

- 新增 `alpha_v2` config；
- 默认关闭；
- 建立 artifact root；
- 不改变 Legacy 输出；
- 建立 golden regression。

### Done When

```text
alpha_v2.enabled=false
```

时，Legacy 测试与基线夜扫核心结果一致。

---

## TASK P0-01

**标题：真实模型身份链**

### 指令

重构 historical report model identity，使其直接来自 Pipeline 实际 predictor artifact。

### 必须复现并修掉

当前基线可能：

```text
report model_trained_at = 2026-09-15
actual artifact created = 2026-08-16
```

### Done When

报告中 actual hash 与 `sha256sum artifacts/model_v1.json` 一致。

---

## TASK P0-02

**标题：Model Registry / Archive 完整性**

### 指令

- 修复新写入 registry 的 artifact URI；
- 新增 immutable serving manifest；
- 增强 retention；
- 对历史坏记录只做 reconciliation report，不伪造修复历史。

### Done When

新训练模型 100% 可由 model_id 找到真实 artifact 并重算 hash。

---

## TASK P0-03

**标题：HistoricalModelResolver**

### 指令

实现 `strict_production_replay` / `pit_research` 两模式。

### Done When

任何 as_of 都不会加载 as_of 之后创建的模型；无模型明确 unscorable。

---

## TASK P0-04

**标题：Point-in-Time Historical Universe**

### 指令

历史 universe 不再直接依赖当前完整 `list_symbols()`。

### Done When

构造测试：

```text
A: 2026-01 已上市
B: 2026-10 才上市
```

回测 2026-09 时 B 绝不能进入 universe/coverage denominator。

---

## TASK P0-05

**标题：T+1 Entry Simulation**

### 指令

holding curve 不得用 T close 作为盘后策略 entry。

### Done When

所有 historical result 满足：

```text
entry_date > signal_date
```

或：

```text
status = no_fill
```

不存在盘后决策按 T close 成交的情况。

---

## TASK P0-06

**标题：Raw Execution / QFQ Feature 分离**

### Done When

报告明确同时记录：

```text
feature_price_mode
execution_price_mode
```

execution entry/exit 均使用 raw。

---

## TASK P0-07

**标题：Night SelectionContract 300/100/50**

### Done When

historical night-equivalent 和 production night shadow 都报告：

```text
300 -> 100 -> 50
```

且同一个 contract ID。

---

## TASK P0-08

**标题：Data Health 与 Breadth 双门拆分**

### Done When

- production 能稳定生成 data health + breadth artifact；
- 缺失状态不再静默当作 healthy；
- coverage 分母使用 expected-active；
- market score 不再替代 data completeness。

---

## TASK P0-09

**标题：Model Output Semantic Guard**

### Done When

任何信号报告能明确区分：

```text
rank score
class probability
expected return
risk score
```

禁止任意 score 被错误显示成“上涨概率”。

---

## TASK P0-10

**标题：Decision Log + Outcome Maturation**

### Done When

任选一个交易日，可以从 Decision Log 完整重建：

- 当时 universe；
- 当时模型；
- 当时候选顺序；
- 当时预测；
- 后续成熟收益。

---

## TASK P1-01

**标题：多 Horizon Label V2**

### Done When

同一 prediction 能在成熟后得到 3/5/10/15D executable outcome。

---

## TASK P1-02

**标题：Alpha Benchmarks**

### Done When

任何 TopK 都同时有：

```text
vs Eligible
vs Quality300
vs StyleMatched
```

---

## TASK P1-03

**标题：Winner Recall**

### Done When

每日输出：

```text
winner_recall_light
winner_recall_deep
```

并有 20D/60D rolling。

---

## TASK P1-04

**标题：Feature Availability Audit**

### Done When

每个 active feature group 有：

```text
source
available_at
asof_safe
missingness
```

无法证明安全的特征不能进入 Base V2。

---

## TASK P1-05

**标题：Simple Factor Baseline**

### Done When

ML 评估报告永远同时出现 simple baseline。

---

## TASK P1-06

**标题：Shared Feature Matrix + Multi Head**

### Done When

一次 feature build 能同时给 Rank/Return/Direction/Risk 推理，不能完整跑四遍 Pipeline。

---

## TASK P1-07

**标题：Purged Walk-Forward**

### Done When

训练报告明确：

```text
train date range
validation range
test range
purge days
embargo days
max label horizon
```

且无 overlap leakage。

---

## TASK P2-01

**标题：Legacy vs V2 Shadow Dual Run**

### Done When

每天可比较 Legacy 和 V2，但 V2 不改变正式结果。

---

## TASK P2-02

**标题：Daily Alpha Health Report**

### Done When

自动生成本文第 11 节所有主要监控项。

---

# 18. 严格禁止事项

在 P0 全通过、并重新产生 Clean OOS 数据之前，任何 AI 不得执行以下修改：

```text
final_signal_min_threshold 70 -> 65/60
p_lgbm_min 下调
p_xgb_min 下调
p_meta_min 下调
max_diff 放宽
为了出票修改 breadth threshold
为了出票取消 overextension
为了出票扩大 final cap
依据 300497 单例调参数
直接 promote 9/15 challenger
把 AUC 0.331 模型概率直接取 1-p 后上线
```

理由：这些都会把“研究基础设施错误”伪装成“模型参数问题”。

---

# 19. 第一轮 Clean OOS 实验矩阵

P0 完成后，第一轮实验必须尽量小，禁止同时改变十个维度。

## E0：Simple Baseline

```text
Quality300
Daily-only safe features
Simple factor rank
```

## E1：现有 Legacy predictor，仅测排序

不改模型，只测：

```text
legacy score vs 5D executable excess
```

回答当前 score 是否有任何 rank value。

## E2：新 Rank Model

```text
Daily-only safe features
5D excess rank target
```

## E3：Rank + Return

只增加 Return Head。

## E4：+ Direction

验证 direction 是否带来增量。

## E5：+ Risk

验证 Risk 是否改善 downside，而不是仅减少交易。

### 每一步只在前一步有证据时继续

如果 E2 已经无 Rank IC，不要急着加新闻、主题、LLM。

---

# 20. 当模型仍然无 Alpha 时的替代路线

如果在：

```text
>=120 个 clean OOS decision dates
```

同时出现：

- 3/5/10/15D Rank IC 均 <= 0；
- 分位无单调性；
- TopK 不优于 Quality Pool；
- simple baseline 比 ML 稳定；

则停止继续堆复杂模型。

保留：

- PIT universe；
- clean backtest；
- execution；
- model registry；
- monitoring；

把 Alpha 层暂时替换成可解释因子 ensemble：

```text
中期趋势
短期回踩/反转
相对强度
流动性
波动质量
基本面质量
```

先证明因子 rank 单调，再测试 ML 是否提供增量。

---

# 21. 本方案的最终验收定义

StockAnalyzer V2.0 完成，不是指“代码都写完”，而是满足以下条件。

## 21.1 Correctness

- 无未来模型 fallback；
- 无未来股票 membership；
- 无 T close 假成交；
- raw execution；
- 数据健康与市场状态分离；
- 模型/标签语义可追；
- 生产/历史 contract 一致。

## 21.2 Research

- 可每日保存 prediction；
- 可自动成熟 outcome；
- 有 Rank IC；
- 有 TopK excess；
- 有 Winner Recall；
- 有 style-matched benchmark；
- 有 purged walk-forward；
- overlapping window 有正确统计处理。

## 21.3 Production

- Legacy 始终可一键回滚；
- V2 可 Shadow；
- 不需要停机重构；
- NAS 资源受控；
- 无新 DB/MQ 基础设施依赖；
- 0 只结果合法；
- 每日可解释为什么选/为什么不选。

---

# 22. 当前最应该先做的 5 个提交

如果只允许现在开始 5 个工程任务，严格按以下顺序：

## Commit 1

```text
P0-00 Alpha V2 flags + baseline manifest + no-op regression
```

## Commit 2

```text
P0-01 Model Identity Truth
```

先把“到底用了哪个模型”彻底做对。

## Commit 3

```text
P0-05 T+1 Entry Simulation + raw execution contract
```

先把收益评价的地基做对。

## Commit 4

```text
P0-04 PIT Universe + expected-active coverage
```

## Commit 5

```text
P0-07 SelectionContract 300/100/50 + historical/production parity
```

完成这五个提交后，再进入 HistoricalModelResolver / Registry / Data Health 深化。

---

# 23. 给执行 AI 的最终判断标准

每次准备“优化模型”前，必须先回答：

1. 这个实验使用的模型在当时已经存在吗？
2. 股票在当时已经属于可选 universe 吗？
3. 所有 feature 当时已经可获得吗？
4. 买入价是 T+1 真正可能成交的吗？
5. Benchmark 与候选池口径一致吗？
6. 结果按 decision date 处理了相关性吗？
7. 这个变化是在增加 Alpha，还是只是在放松门槛？
8. 结果是否优于 Quality Pool 和 Simple Baseline？
9. 能否一键回滚？

任何一个答案为“不确定”，优先修研究链，不继续调模型。

---

# 24. 核心结论

StockAnalyzer 当前最大的问题，不是“70 分太高”，也不是“需要再增加更多 AI 新闻和复杂模型”。

当前最需要完成的是：

> **把系统从一个难以解释的综合打分/多门控系统，变成一个可以用真实 T+1 成交口径、Point-in-Time 数据、明确模型身份、同口径候选池，持续证明“高排名股票未来确实比低排名股票更好”的 Alpha Ranking 系统。**

现有工程资产不需要推倒：

- 全市场数据；
- Quality/Light/Deep 漏斗；
- Pipeline；
- ExecutionMatcher；
- Model Registry；
- Learning Protocol；
- 飞书通知；
- Scheduler；
- NAS 部署；

全部可以保留并演进。

真正需要重做的是：

```text
研究可信链
模型身份链
历史 universe
T+1 execution
标签语义
Alpha 评价
Shadow promotion
```

在这条链完成之前，不调 70，不追求每天出票。

完成之后，系统才有资格回答真正的问题：

> “今天高分的股票，未来 5～10 个交易日，是否在可实际成交条件下持续跑赢同日其他可选股票？”

这才是 StockAnalyzer Alpha V2.0 的唯一核心。

---

# Appendix A：关键代码位置（基线）

```text
src/stock_analyzer/config.py
src/stock_analyzer/pipeline.py
src/stock_analyzer/data/asof_provider.py
src/stock_analyzer/models/predictor.py
src/stock_analyzer/models/registry.py
src/stock_analyzer/backtest/holding_curve.py
src/stock_analyzer/backtest/matcher.py
src/stock_analyzer/runtime/services/week5_historical_runner.py
src/stock_analyzer/runtime/services/asof_backtest_service.py
src/stock_analyzer/runtime/services/week5_selection_engine.py
src/stock_analyzer/runtime/services/market_sync_service.py
src/stock_analyzer/ops/market_breadth.py
src/stock_analyzer/signal/cross_review.py
src/stock_analyzer/signal/scoring.py
src/stock_analyzer/feature/engineer.py
src/stock_analyzer/learning/
```

# Appendix B：关键证据文件

```text
KEY_FINDINGS.md
README.md
01_config/effective_config.redacted.json
01_config/effective_config_blocks.redacted.json
02_model_identity/model_registry_explicit_columns.json
02_model_identity/model_files_sha256.json
02_model_identity/model_archive_files_on_disk.json
02_model_identity/dataset_manifests_timeline.json
02_model_identity/dataset_manifests_and_registries.json
06_derived/model_ledger.json
06_derived/backtest_daily_funnel.csv
06_derived/nightly_funnel_by_run.csv
06_derived/vendor_daily_index_summary.json
05_data_health/universe_quality_snapshot.json
05_data_health/vendor_intraday_summary.duckdb.manifest.json
```

# Appendix C：方案版本纪律

当以下任一内容发生变化时，本方案必须更新版本：

- serving model 切换；
- label policy 改变；
- feature schema 改变；
- night selection contract 改变；
- data provider 改变；
- execution semantics 改变；
- V2 从 shadow 进入 advisory/enforced。

更新时保留旧文档，不覆盖历史版本。
