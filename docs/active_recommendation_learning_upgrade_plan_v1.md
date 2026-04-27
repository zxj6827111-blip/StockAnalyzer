# 主动推荐与自学习闭环改造方案 v1.1

## 0. 修正后的前提

- 本地 `artifacts/runtime/runtime_state.json` 只作为开发环境样例，不再作为生产状态判断依据。
- NAS 上的真实运行状态应作为生产诊断依据；本方案先基于代码结构设计改造，不强依赖拉取 NAS 最新数据。
- 若进入实施或调参阶段，优先在 NAS 上直接运行诊断脚本；只有需要本地离线复盘时，才拉取最近 `7/20/60` 个交易日的运行快照。

### 0.1 外部审核意见采纳结论

- 采纳：先补“信号质量基线审计”，否则主动推荐层只会把弱信号包装得更可见。
- 采纳：主动推荐 Brief 必须包含“门控透视”和“距离可执行还差多少”，而不只是给出阻塞原因。
- 采纳：`watch/research` 阈值应比交易阈值更宽松；降低观察阈值不等于降低买入阈值。
- 采纳：Phase 3 与 Phase 4 合并为“学习效果 + 晋升可视化”，复用已有 governance，不重复造模型发布体系。
- 采纳：NAS 快照接入后置为可选，不阻塞主动推荐和学习效果建设。
- 部分采纳：外部工具从“大而全接入”调整为“先用 Tushare/OpenAI/W&B 或本地报告增强；Qlib 先借鉴因子思想；DolphinDB/FinGPT/OpenBB/MLflow 后置或不作为当前阶段重点”。

## 1. 当前系统重新评估

### 1.1 已具备的能力

- 核心信号链路已经存在：`AnalyzerPipeline.run_once()` 负责生成 `PipelineSignal`，再经 `ScoreEngine`、`cross_review`、`SoupStrategy.recommend()` 形成 `buy/watch/hold`。
- 通知链路已经存在：`NotificationFilter` 会按动作、分数、冷却时间筛出 `actionable_signals`。
- 盘中/全市场雷达能力已经存在：`week5` 能做首板、异动、market radar、review pool、watchlist 同步。
- 学习治理能力已经存在：已有训练、shadow validation、模型 proposal、approval、release ticket、rollback 等接口。

### 1.2 仍然存在的核心问题

- **缺少“主动推荐层”**：当前系统输出的是信号和风控结果，不是面向用户的“今日推荐清单 + 操作计划 + 不推荐原因”。
- **推荐可见性不足**：没有买入信号时，用户很难知道是“没有机会”，还是“有候选但被哪个门控挡住”。
- **market radar 与交易链路割裂**：全市场雷达能发现候选，但更多进入 review/offhours，不会稳定转化成白天可见候选清单。
- **自学习效果不可感知**：夜间学习有报告和 shadow 产物，但缺少一句话结论：本次学习是否提升、是否可晋升、为什么不能晋升。
- **模型质量与依赖健康应显式门控**：如果生产模型进入 fallback/degraded 状态，系统应明确降低推荐等级，并在日报中说明。
- **信号质量根因缺少基线审计**：需要先看清模型 AUC、Precision@K、S/A/B/C 分布、门控拦截比例、标签正负样本比例，否则只能解释“为什么不买”，不能提升“该不该买”。

## 2. 推荐目标形态

### 2.1 每日主动推荐输出

每个交易日固定输出四类内容：

1. **可执行候选**：满足 `buy`、风控通过、流动性通过、财务过滤通过、模型质量通过。
2. **重点观察候选**：分数较高但被模型分歧、风控、时机、流动性等门控挡住。
3. **研究候选池**：来自 market radar / week5 / 夜间研究池，用于后续观察，不直接建议买入。
4. **今日不推荐原因**：当没有可执行候选时，也必须推送“为什么没有买入建议”。

### 2.2 推荐内容格式

每只股票至少包含：

- `symbol/name`
- `lane`: `actionable_buy` / `watch` / `research`
- `score/grade/confidence`
- `推荐原因`
- `阻塞原因`
- `gate_proximity`: 距离核心门控通过还差多少，例如 `score_gap_to_A`、`lgbm_gap_to_min`、`model_diff_gap`
- `score_trend`: 最近 `3-5` 天分数/等级变化
- `market_regime`: 当前市场状态及阈值调整说明
- `触发条件`
- `失效条件`
- `建议仓位`
- `止损/止盈/最大持有天数`
- `数据来源`
- `trace_id`

### 2.3 自学习日报输出

每天盘后或夜间学习后输出：

- 本次是否学习成功
- 新增成熟样本数
- 模型是否真实重训
- 是否产生 challenger
- challenger 是否跑赢 champion
- 推荐命中率、转化率、收益回撤是否改善
- 是否允许晋升
- 如果不能晋升，列出前三个阻塞原因

## 3. 总体架构设计

### 3.1 新增 `RecommendationAdvisor`

新增模块：

- `src/stock_analyzer/recommendation/schema.py`
- `src/stock_analyzer/recommendation/advisor.py`
- `src/stock_analyzer/recommendation/explainer.py`

职责：

- 汇总 `pipeline.signals`、`week5_market_radar`、`week5_scan`、`portfolio`、`risk`、`learning_health`。
- 将原始信号归入三条通道：`actionable_buy`、`watch`、`research`。
- 对每只候选生成可读解释、触发条件和失效条件。
- 即使没有 `buy`，也生成 `no_trade_reason` 和 Top 观察清单。

### 3.2 新增 `LearningEffectScorecard`

新增模块：

- `src/stock_analyzer/learning/effect_scorecard.py`
- `src/stock_analyzer/learning/effect_thresholds.py`

职责：

- 聚合模型训练指标、shadow 指标、推荐生命周期结果、真实/模拟成交反馈。
- 输出 `PASS/WARN/FAIL`。
- 用业务语言解释“学习有没有效果”。
- 给 `RecommendationAdvisor` 提供 `learning_health`，用于调整推荐置信度。

### 3.3 新增 `SignalQualityAuditor`

新增模块：

- `src/stock_analyzer/research/signal_quality_auditor.py`
- `src/stock_analyzer/research/gate_attribution.py`

职责：

- 统计最近 `20/60/120` 个交易日的信号分布：`S/A/B/C` 占比、`buy/watch/hold` 占比。
- 统计门控拦截原因：`cross_review`、`risk_gate`、`liquidity_gate`、`financial_gate`、`strategy_decision`。
- 输出候选距离可执行门槛的差距：分数差、概率差、模型分歧差、流动性差。
- 检查训练标签质量：正样本比例、成熟样本数、TP/SL 标签冲突、不同市场状态下的标签覆盖。
- 对比 `market_relative_feature` 开关前后的离线指标，先做 shadow 评估，不直接启用生产。

### 3.4 NAS 生产诊断/快照接入层（后置可选）

新增模块/脚本：

- `scripts/pull_nas_runtime_snapshot.ps1`
- `src/stock_analyzer/ops/nas_snapshot.py`
- `src/stock_analyzer/ops/runtime_snapshot_loader.py`

职责：

- 优先支持在 NAS 上直接运行诊断脚本，避免把快照同步作为前置阻塞。
- 在需要本地离线复盘时，从 NAS 拉取生产运行快照到本地只读目录。
- 标准化读取 `runtime_state.json`、最新 `evolution/history/*.json`、模型 artifact、training report、week5 report。
- 本地开发环境不再直接判断生产新鲜度，只判断 NAS 快照新鲜度。

建议快照目录：

```text
artifacts/nas_snapshots/YYYYMMDD_HHMMSS/
  runtime/runtime_state.json
  evolution/history/*.json
  training/*.json
  model/*.json
  logs/*.log
```

## 4. 分阶段实施方案

### Phase 0.5：信号质量基线审计

目标：

- 在做主动推荐 Brief 之前，先知道“没有推荐”的根因到底是模型弱、阈值严、门控严、样本标签弱，还是市场状态不允许。

改造点：

- 新增 `SignalQualityAuditor.build_report(...)`。
- 在 `StockAnalyzerService` 增加：
  - `run_signal_quality_audit(...)`
  - `latest_signal_quality_audit()`
  - `signal_quality_audit_history(limit=...)`
- 在 `main.py` 增加：
  - `POST /research/signal-quality/run`
  - `GET /research/signal-quality/latest`
  - `GET /research/signal-quality/history`
- 输出：
  - 模型指标：`AUC`、`Precision@K`、`Brier`、正样本率、成熟样本数。
  - 信号分布：`S/A/B/C`、`buy/watch/hold`、分策略统计。
  - 门控归因：各门控拦截数量、占比、Top 阻塞原因。
  - 门槛距离：最接近买入的 Top N 候选及其 `gate_proximity`。
  - 特征实验建议：是否值得打开 `market_relative_feature` 做 shadow 对比。

验收标准：

- 能回答“最近为什么没有买入信号”：分数不够、模型分歧、风控冻结、流动性不足、财务过滤、市场状态不佳等。
- 能输出 `Top 10 near-miss`，显示每只候选距离可执行还差多少。
- 能输出标签与样本健康度，避免在劣质标签上继续自学习。

### Phase 1：主动推荐 Brief

目标：

- 无论有没有 `buy`，每天都主动输出可读候选清单。

改造点：

- 新增 `RecommendationAdvisor.build_brief(...)`。
- 在 `StockAnalyzerService` 增加：
  - `build_recommendation_brief(...)`
  - `latest_recommendation_brief()`
  - `recommendation_brief_history(limit=...)`
- 在 `main.py` 增加：
  - `POST /recommendations/brief/run`
  - `GET /recommendations/brief/latest`
  - `GET /recommendations/brief/history`
- 配置新增：
  - `recommendation_advisor.enabled`
  - `recommendation_advisor.always_notify_no_buy`
  - `recommendation_advisor.buy_top_k`
  - `recommendation_advisor.watch_top_k`
  - `recommendation_advisor.research_top_k`
  - `recommendation_advisor.watch_min_score`
  - `recommendation_advisor.research_min_score`
  - `recommendation_advisor.show_gate_proximity`
  - `recommendation_advisor.show_score_trend_days`
  - `recommendation_advisor.regime_adaptive_thresholds`

候选分层规则：

- `actionable_buy`: `action=buy` 且核心风险门控通过。
- `watch`: `score >= watch_min_score` 或进入 `near_miss`，但被 `cross_review/risk/liquidity/financial/timing` 任一门控挡住。
- `research`: 来自 `week5_market_radar`、`review_pool`、夜间研究池，尚未进入交易执行链；阈值比 `watch` 更宽松。
- `blocked_buy`: 曾满足买入分数但被硬风控阻止，单独展示，不能混同于普通观察。

验收标准：

- 当 `buy=0` 时，也能输出至少 `watch/research` 候选或明确 `no_trade_reason`。
- 每条候选都能解释“为什么推荐/为什么不能买/什么条件下才可以买”。
- 每条 `watch/blocked_buy` 必须包含 `gate_proximity`，说明距离买入还差多少。
- 推荐 Brief 必须展示最近 `3-5` 天信号趋势，避免只看单日噪声。
- 推荐 Brief 必须展示当前 `market_regime`，并解释是否启用了动态阈值。
- 不改变原有下单或模拟盘逻辑，只增强推荐可见性。

### Phase 2：主动通知与看板

目标：

- 用户每天稳定收到“推荐简报”，不再需要自己去翻 dashboard。

改造点：

- 在以下任务中调用 `build_recommendation_brief()`：
  - `premarket_scan`
  - `auction_report`
  - `midday_news_brief`
  - `week5_live_runtime`
  - `close_reconcile`
- 新增推送模板：
  - 盘前固定：今日重点观察 + 风险环境 + 不交易原因。
  - 盘中事件触发：仅当可执行候选出现、观察池状态变化、触发条件满足时推送。
  - 收盘固定：推荐复盘 + 次日观察池 + 学习效果摘要。
- 新增通知优先级：
  - `P0`: 卖出/止损/风控强制动作。
  - `P1`: 可执行买入候选或触发条件满足。
  - `P2`: 观察池变化、学习效果 WARN/FAIL。
  - `P3`: 常规研究候选和收盘复盘。
- Dashboard 新增“主动推荐”面板：
  - 可执行候选
  - 观察候选
  - 研究候选
  - 今日不交易原因
  - 推荐生命周期

验收标准：

- 一个交易日内至少有一条推荐简报或“不推荐原因”。
- 默认只固定推送盘前和收盘；盘中只在状态变化时推送，避免通知疲劳。
- 不重复刷屏：同一阶段同一候选只推一次，除非 `lane`、触发状态、风险等级或建议动作变化。
- dashboard 能看到最近一次推荐简报和历史 Top 候选。

### Phase 3+4：自学习效果日报 + 晋升可视化

目标：

- 把“每天自学习了”变成“每天自学习是否有效、是否该信任、是否具备晋升资格”。

改造点：

- 新增 `LearningEffectScorecard.build(...)`。
- 在训练、夜间 evolution、market warehouse follow-up 后生成 scorecard。
- 在 `main.py` 增加：
  - `GET /learning/effect/latest`
  - `GET /learning/effect/history`
  - `POST /learning/effect/run`
- Dashboard 新增“学习效果”面板。
- 通知新增“学习效果日报”。
- 复用现有 learning model proposal、approval、release ticket、rollback；本阶段重点做整合和可视化，不重建治理体系。

默认门控建议：

- `FAIL`：
  - 模型 artifact 缺失或不可读
  - 生产模型处于 degraded/fallback 且未明确标注
  - `AUC < 0.53` 或连续 `5` 次评估下降
  - `Precision@K` 不高于基准
  - shadow after-cost return 低于 champion 超过 `2%`
  - 正样本比例 `< 5%` 或 `> 70%`
- `WARN`：
  - 成熟样本数不足
  - `AUC < 0.56`
  - NAS 快照过期
  - 新闻/分钟线数据覆盖不足
  - challenger 指标改善但交易收益未改善
  - 连续 `7` 天学习但关键指标没有改善
- `PASS`：
  - 样本数、`AUC >= 0.56`、Precision@K、shadow 收益、回撤均达标
  - 没有关键数据质量阻塞
  - Precision@K 相对 champion 至少提升 `10%`，且 bootstrap 检验显著性达标

学习投入产出比：

- 统计最近 `30` 天新增样本数与 AUC/Precision@K 变化。
- 统计每 `1000` 个新增样本带来的指标改善。
- 当学习次数增加但指标不改善时，输出“应调整标签/特征/样本池”的诊断建议。

自学习方向诊断：

- 特征重要性变化：识别变强/变弱特征。
- 样本分布漂移：识别近期行情是否偏离训练集。
- 标签质量检查：正样本中实际盈利比例、TP/SL 冲突比例、不同 regime 下标签覆盖。

验收标准：

- 用户能一眼看到：`有效 / 有改善但不够晋升 / 无效 / 数据不足`。
- 每次不能晋升时，必须列出阻塞原因和下一步动作。
- 结果写入 runtime state，支持历史查询。
- 模型晋升必须复用现有 governance 流程，生产环境继续人工确认。

### Phase 3+4 内的晋升约束

目标：

- 不让“学习建议”直接影响生产推荐，必须经过 champion/challenger/shadow 审核。

改造点：

- 复用现有 learning model proposal、approval、release ticket。
- 明确四级生命周期：
  - `trained`
  - `shadow_validated`
  - `paper_trading_approved`
  - `champion_promoted`
- `auto_promotion.enabled` 默认仍保持关闭。
- 仅在 `simulation/staging` 可打开自动晋升；生产必须人工确认。

晋升建议门槛：

- `AUC >= 0.56`
- `Precision@K` 相对 champion 提升至少 `10%`，并通过 bootstrap 显著性检查
- shadow after-cost return 跑赢 champion
- 最大回撤不扩大
- 信号分歧率不过高
- 最近 `20` 个交易日推荐漏报率下降

验收标准：

- 任何模型晋升都有 proposal、approval、ticket、rollback 记录。
- 推荐简报会显示当前使用的 champion 与 learning health。
- 生产环境不能静默替换模型。

### Phase 5：外部工具与信号质量增强

目标：

- 先补齐最影响 A 股信号质量的数据和因子，不急于引入重型平台。

建议引入：

- **立即优先**：
  - `Tushare Pro`：优先补资金流、龙虎榜、股东变动、财务质量数据。
  - `OpenAI API`：复用现有 LLM 管线做新闻摘要、公告事件分类、题材归因。
  - `Weights & Biases` 或本地实验报告：轻量记录训练、参数、指标曲线；不强制替换现有 governance。
- **中期参考/增强**：
  - `TA-Lib`：标准化技术指标实现，减少自研指标偏差。
  - `Qlib Alpha 因子思想`：先参考因子设计和评估方法，不直接引入完整 Qlib 框架。
  - `Backtrader/VectorBT`：作为主系统外的快速策略原型验证工具。
- **当前不优先**：
  - `DolphinDB`：当前 DuckDB 对日线/分钟线规模够用，避免额外运维成本。
  - `FinGPT`：当前 OpenAI 管线足够覆盖新闻摘要和事件分类，FinGPT 后置观察。
  - `OpenBB`：A 股生态适配有限，当前不作为主数据源。
  - `MLflow Model Registry`：已有 governance 覆盖注册、审批、回滚，暂不强制引入。

信号质量增强项：

- 将 `market_relative_feature` 先纳入 shadow 实验，比较开启前后的 AUC、Precision@K、回撤和信号分布。
- 增加 regime-aware 推荐阈值：强势市放宽观察，弱势市收紧买入；生产买入阈值变更必须通过回测/模拟盘验证。
- 扩展策略池：在不影响主策略的前提下，shadow 评估 `oversold` 均值回归、事件驱动、板块轮动。

验收标准：

- 外部工具先作为 shadow/辅助，不直接改变生产推荐。
- 任何外部数据源都要记录 `source`、`asof`、`coverage`、`latency`。
- 新闻和 LLM 输出必须带置信度和引用来源，不作为单独买入依据。

## 5. 验证计划

### 单元测试

- `tests/test_signal_quality_auditor.py`
  - 输出 `S/A/B/C` 与 `buy/watch/hold` 分布。
  - 输出各门控拦截占比。
  - 输出 `near_miss` 候选和门槛距离。
- `tests/test_recommendation_advisor.py`
  - 有 `buy` 时进入可执行候选。
  - 无 `buy` 但有高分信号时进入观察候选。
  - 只有 radar 命中时进入研究候选。
  - 所有候选都有 reason、blocker、gate_proximity、score_trend、trigger、expires_at。
- `tests/test_learning_effect_scorecard.py`
  - degraded 模型输出 `FAIL`。
  - 样本不足输出 `WARN`。
  - shadow 跑赢且指标达标输出 `PASS`。
- `tests/test_nas_snapshot_loader.py`
  - 快照新鲜度判断。
  - 缺文件降级。
  - 不读取开发 runtime 作为生产状态。

### 集成测试

- `tests/test_main_recommendation_brief.py`
  - API 返回结构稳定。
  - 空信号也返回 `no_trade_reason`。
- `tests/test_main_signal_quality.py`
  - API 返回模型指标、信号分布、门控归因。
- `tests/test_service_recommendation_notifications.py`
  - 每阶段去重。
  - 候选 lane 变化时允许再次推送。
- `tests/test_service_learning_effect.py`
  - evolution/training 后生成学习效果报告。

### 验收运行

建议命令：

```powershell
pytest tests/test_signal_quality_auditor.py tests/test_recommendation_advisor.py tests/test_learning_effect_scorecard.py
pytest tests/test_main_signal_quality.py tests/test_main_recommendation_brief.py
pytest tests/test_service_week5.py tests/test_service_learning_governance.py tests/test_pipeline.py
```

## 6. 关键配置草案

```yaml
recommendation_advisor:
  enabled: true
  always_notify_no_buy: true
  buy_top_k: 3
  watch_top_k: 5
  research_top_k: 10
  watch_min_score: 45
  research_min_score: 40
  include_cross_review_near_miss: true
  include_market_radar_research: true
  max_candidate_age_hours: 24
  phase_dedup_ttl_sec: 14400
  show_gate_proximity: true
  show_score_trend_days: 5
  regime_adaptive_thresholds: true

signal_quality_audit:
  enabled: true
  lookback_days: [20, 60, 120]
  near_miss_top_k: 10
  include_gate_attribution: true
  include_label_health: true
  include_market_relative_shadow: true

learning_effect:
  enabled: true
  auto_run_after_training: true
  auto_run_after_evolution: true
  min_mature_samples: 300
  min_auc_pass: 0.56
  min_auc_warn: 0.53
  min_precision_lift_pass: 0.10
  precision_lift_bootstrap_p: 0.05
  max_shadow_underperform_pct: 0.02
  min_positive_rate: 0.05
  max_positive_rate: 0.70
  stale_learning_days: 7
  feature_drift_threshold: 0.15

nas_snapshot:
  enabled: false
  source_root: ""
  local_snapshot_root: "artifacts/nas_snapshots"
  max_age_hours: 18
  include_patterns:
    - "artifacts/runtime/runtime_state.json"
    - "artifacts/evolution/history/*.json"
    - "artifacts/model*.json"
    - "artifacts/training/*.json"
```

## 7. 推荐实施顺序

1. **先做 Phase 0.5**：信号质量基线审计，确认模型、标签、门控、阈值问题。
2. **再做 Phase 1**：主动推荐 Brief，加入门控透视、near-miss、趋势和 regime。
3. **合并做 Phase 3+4**：学习效果日报 + 晋升可视化，复用现有 governance。
4. **做 Phase 2 精简版**：盘前 + 收盘固定推送，盘中只推状态变化。
5. **Phase 5 持续迭代**：优先 Tushare、market_relative shadow、策略多元化。
6. **NAS 快照接入后置可选**：需要本地离线复盘时再做，不阻塞核心价值交付。

## 8. 需要确认的决策

- 是否先按 `Phase 0.5` 做信号质量审计，再进入推荐 Brief 实现。
- 推荐推送频率：默认盘前和收盘固定推送，盘中仅状态变化推送，是否接受。
- 推荐语气边界：建议使用“候选/观察/触发条件”，避免表述成保证收益。
- 生产是否永远人工确认模型晋升：建议是。
- 是否愿意把 `market_relative_feature` 先放入 shadow 对照实验。
- 是否已有 `Tushare Pro` token；若有，优先接入资金流和龙虎榜。
- NAS 快照路径和访问方式：后置可选，仅在需要本地离线生产复盘时确认。
