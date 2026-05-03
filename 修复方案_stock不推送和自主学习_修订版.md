# 股票不推送与自主学习修复方案（修订版）

> 修订时间：2026-05-03  
> 修订依据：对原方案和当前仓库实现的代码审阅  
> 结论：原方案方向部分正确，但不能直接照抄实施。推送部分需要补齐策略级阈值和通知通道排查；自主学习部分需要重写为接入现有学习治理链，而不是简单新增一个未挂载的训练任务。

---

## 一、总体判断

### 可保留的判断

原方案识别到以下机制会影响股票推送，这是合理的：

- `notification_filter.min_score`
- `notification_filter.max_signals_per_run`
- `notification_filter.cooldown_sec`
- `notification_filter.quiet_windows`
- `notification_filter.t_day_entry_silence_enabled`
- `score.thresholds.a/b`

原方案识别到周末空闲队列可作为自主学习兜底入口，这个方向也可以保留。

### 必须修正的判断

1. `idle_queue.enabled=false` 和 `idle_queue.auto_run=false` 不一定代表运行时禁用。当前代码有 `enabled_policy: "auto"` 和 `auto_run_policy: "auto"`，在 `simulation/staging` 模式下会被策略自动启用。
2. 不能把 `idle_queue.enabled`、`idle_queue.auto_run` 直接改成 `true` 作为默认方案，这会绕过 policy 保护。
3. 不能直接把 `production` 加进 `enabled_modes/auto_run_modes`。当前逻辑下，一旦 `production` 在 modes 里，生产环境会全量启用，而不是走 canary。
4. 原方案新增 `WE-TRAIN-01` 只改 manifest 和 weekend service 不够。当前调度和 dispatch 都是硬编码列表，不补挂载点会导致任务永远不运行。
5. 原方案示例代码调用 `service.train_models(full_market=False, preferred_symbols=...)` 是错误的。当前 `train_models` 在 `full_market=False` 时必须提供单个 `symbol`，否则会抛 `symbol is required when full_market=false`。
6. 当前系统已有更完整的学习治理链：`build_learning_trainable_manifest -> run_learning_manifest_shadow_proposal -> shadow validation / promotion gate`。所谓“自主学习”应优先复用这条链，而不是直接重训并加载基础模型。

---

## 二、问题一：股票不推送

### 原方案合理处

可以考虑降低推送过滤阈值和提高每轮推送数量：

```yaml
notification_filter:
  min_score: 60
  max_signals_per_run: 5
```

也可以考虑调整全局评分阈值：

```yaml
score:
  thresholds:
    s: 78
    a: 60
    b: 50
```

### 原方案遗漏

#### 1. 策略级阈值也会拦截买入推送

买入推送使用 `_buy_notification_min_score(strategy)`，如果信号带有 `trend` 或 `monster` 策略，会优先读取：

```yaml
strategy_scores:
  trend:
    thresholds:
      a: 65
      b: 55
  monster:
    thresholds:
      a: 64
      b: 54
```

因此，如果目标是“60 分以上买入候选都能推送”，需要同步修改：

```yaml
strategy_scores:
  trend:
    thresholds:
      s: 78
      a: 60
      b: 50
  monster:
    thresholds:
      s: 76
      a: 60
      b: 50
```

否则会出现全局 `a=60`，但策略级 `trend.a=65` 仍然拦截买入通知。

#### 2. `b=50` 与 `notification_filter.min_score=60` 存在语义冲突

如果保留：

```yaml
notification_filter.min_score: 60
score.thresholds.b: 50
```

则 50-59 分的 watch 信号虽然可能被判为 B 级观察，但仍不会进入 `actionable_signals` 推送。

需要明确目标：

- 如果只希望推送较强信号：`min_score=60` 合理。
- 如果希望 B 级观察也推送：`min_score` 应同步降到 50，或把 watch 与 buy 拆成不同阈值。

建议更稳妥的方案是新增分动作阈值，例如：

```yaml
notification_filter:
  min_score: 60
  min_score_by_action:
    buy: 60
    watch: 50
```

如果不想改配置结构，先保持 `min_score=60`，并在方案里明确“50-59 的观察信号不会推送”。

#### 3. 不建议直接关闭 T 日静默

当前 T 日静默不是静默所有 T 日持仓，而是只在：

- `t_day_entry_silence_enabled=true`
- signal symbol 在 T 日入场集合里
- reasons 命中 `take_profit` 或 `stop_loss`

时才静默。

所以直接改成：

```yaml
t_day_entry_silence_enabled: false
```

可能会让 T 日刚入场后的止盈/止损类噪音重新出现。

建议改成以下二选一：

1. 保持 T 日静默，新增过滤诊断，输出被静默的 symbol/action/reason。
2. 如果确认确实误杀有效信号，再缩小 `t_day_silence_reason_keywords`，而不是直接全关。

#### 4. 需要排查真实通知通道

当前默认通知通道是：

```yaml
notifications:
  primary: console
  backup: console
```

外部通道 token/webhook 默认为空。  
如果用户说的“不推送”是微信、飞书、PushPlus、Telegram 或邮件收不到，单纯调整过滤阈值无效，必须确认：

- `notifications.primary` 是否指向真实通道
- 对应 token/webhook 是否配置
- audit event 里的 `notification.delivery` 是否成功
- 是否被 `quiet_windows` 静默

### 修订后的推送方案

建议分两步做。

#### 第一步：加诊断，确认到底被谁拦截

在 pipeline 输出或 audit event 中增加 `notification_filter_diagnostics`，至少包含：

- 输入信号数
- 按 action 拒绝数量
- 按 min_score 拒绝数量
- T 日静默数量
- cooldown 去重数量
- quiet window 是否命中
- 最终 accepted 数量

这样可以避免盲目降阈值。

#### 第二步：按目标调整阈值

如果目标是“更多买入候选被推送”，建议配置为：

```yaml
notification_filter:
  enabled: true
  cooldown_sec: 300
  min_score: 60
  allowed_actions: [buy, watch]
  max_signals_per_run: 5
  quiet_windows: []
  dedup_by_symbol_action: true
  t_day_entry_silence_enabled: true
  t_day_silence_reason_keywords: [take_profit, stop_loss]

score:
  thresholds:
    s: 78
    a: 60
    b: 50

strategy_scores:
  trend:
    thresholds:
      s: 78
      a: 60
      b: 50
  monster:
    thresholds:
      s: 76
      a: 60
      b: 50
```

如果后续确认 T 日静默确实误杀，再单独调整。

---

## 三、问题二：没有自主学习

### 原方案合理处

把周末空闲队列作为“自主学习兜底调度器”是合理方向。周末比交易时段更适合跑训练、验证、回归和模型提案。

### 原方案主要问题

#### 1. 空闲队列默认配置判断不准确

当前默认：

```yaml
idle_queue:
  enabled: false
  auto_run: false
  enabled_policy: "auto"
  auto_run_policy: "auto"
  enabled_modes: ["simulation", "staging"]
  auto_run_modes: ["simulation", "staging"]
```

在 `simulation/staging` 下，policy 会让它有效启用。  
所以不应直接得出“空闲队列默认禁用，因此没有自主学习”的结论。

真正需要排查的是：

- 当前运行模式是否是 `production`
- scheduler 是否注册了 `idle_queue_tick`
- `idle_queue_state()` 中 effective enabled/auto_run 是什么
- 周末是否处于 idle window
- 最近 `idle_queue_history` 是否有任务运行
- 是否被 resource pause、manual ack、time guard mismatch 阻断

#### 2. 不建议默认全量打开 production

原方案建议：

```yaml
enabled_modes: ["simulation", "staging", "production"]
auto_run_modes: ["simulation", "staging", "production"]
```

这会让 production 在 auto policy 下全量启用。  
如果要在生产环境启用，建议使用 canary：

```yaml
idle_queue:
  enabled: false
  auto_run: false
  enabled_policy: "auto"
  auto_run_policy: "auto"
  enabled_modes: ["simulation", "staging"]
  auto_run_modes: ["simulation", "staging"]
  production_canary_ratio: 0.1
  production_canary_key: "stable-machine-or-env-key"
```

如果明确要生产全量启用，应作为部署配置，而不是默认配置。

#### 3. `WE-TRAIN-01` 没有完整挂载

如果保留新增周末任务，需要至少改这些点：

- `idle_queue_manifest_service.py`：增加 manifest。
- `idle_queue_registry_service.py`：把任务加入周末 due list 或动态从 manifest 读取。
- `idle_queue_registry_service.py`：在 `run_idle_task()` 增加 dispatch 分支。
- `idle_queue_service.py`：增加 facade 方法。
- `runtime/service.py`：增加主 service wrapper。
- `tests/test_service_idle_queue.py`：增加周末任务顺序、min interval、dispatch、输出文件测试。

只改 manifest 和 `RuntimeIdleQueueWeekendService` 方法不会生效。

#### 4. 原训练调用参数错误

原方案代码：

```python
service.train_models(
    full_market=False,
    lookback_days=lookback_days,
    max_symbols=symbol_cap,
    preferred_symbols=symbol_list,
)
```

这会失败。当前 `full_market=False` 只支持单标的训练，必须传 `symbol`。  
如果确实要按 symbol universe 训练，必须使用：

```python
service.train_models(
    full_market=True,
    lookback_days=lookback_days,
    max_symbols=symbol_cap,
    preferred_symbols=symbol_list,
)
```

但仍不建议作为首选，因为它会走基础训练路径，并可能 reload predictor。

#### 5. 原方案绕开了现有学习治理链

当前盘后流程已经有：

```text
build_learning_trainable_manifest
run_learning_manifest_shadow_proposal
run_learning_manifest_shadow_promotion_gate
run_learning_manifest_shadow_validation
train_learning_manifest
```

这条链会处理：

- 可训练样本 manifest
- shadow 模型
- promotion gate
- auto promotion 配置
- 学习结果通知
- audit event

因此，周末自主学习任务更应该触发这条链，而不是直接写 `artifacts/model_v1.json`。

---

## 四、修订后的自主学习方案

### 推荐目标

新增一个周末任务，例如：

```text
WE-LEARN-01
```

职责是“周末学习治理兜底”，而不是“直接训练并覆盖模型”。

### 推荐任务行为

1. 检查 min interval，默认 7 天。
2. 构建学习 manifest：

```python
manifest_payload = service.build_learning_trainable_manifest()
```

3. 如果 manifest 可用，运行 shadow proposal：

```python
proposal_payload = service.run_learning_manifest_shadow_proposal(
    dataset_manifest_id=manifest_id,
    load_predictor=not service._config.auto_promotion.enabled,
    approve_if_passed=True,
    auto_approve=bool(service._config.auto_promotion.enabled),
    auto_release=bool(service._config.auto_promotion.enabled),
    auto_reload_predictor=bool(service._config.auto_promotion.auto_load_predictor),
    notify_on_rejection=bool(service._config.auto_promotion.notify_on_rejection),
    source_trace_id=f"we-learn-01-{trade_date}",
)
```

4. 输出报告到：

```text
staging/idle_cache/{trade_date}/WE-LEARN-01/model_learning/learning_report.json
```

5. 仅发送摘要通知，不要每一步都刷通知。
6. 如果 manifest 不可用，返回 `degraded/skipped`，并记录原因，不要直接覆盖模型。

### 可选 fallback

如果确实需要“没有 manifest 时也尝试基础训练”，应作为显式配置开关，例如：

```yaml
idle_learning:
  fallback_full_market_training_enabled: false
```

只有启用时才执行：

```python
service.train_models(
    full_market=True,
    lookback_days=lookback_days,
    max_symbols=symbol_cap,
    preferred_symbols=symbol_list,
    artifact_path="staging/idle_cache/{trade_date}/WE-LEARN-01/model_training/model_candidate.json",
)
```

注意：直接调用 `train_models` 会 reload predictor。若不希望候选模型立即影响运行，应优先使用 learning manifest shadow validation。

### 推荐 manifest

```python
"WE-LEARN-01": {
    "task_id": "WE-LEARN-01",
    "priority": "P1",
    "schedule": "weekend",
    "phase": 2,
    "must_run": False,
    "defer_policy": "next_weekend",
    "rotating_priority": 0,
    "max_defer_runs": 2,
    "force_run_on_disk_usage_pct": 100.0,
    "max_wall_time_minutes": 240,
    "symbol_cap": 200,
    "task_output_subdir": "model_learning",
    "write_whitelist": [],
    "min_interval_days": 7,
}
```

不建议设为 P0 + must_run。训练/学习任务可能很重，设为 P0 会阻塞现有周末 P0 任务。

### 推荐调度顺序

周末顺序建议：

1. `WE-P0-01`
2. `WE-P0-02`
3. `WE-LEARN-01`
4. 现有 P1 任务轮转
5. `WE-P2-08`

或者把 `WE-LEARN-01` 放入 P1 轮转列表，由 rotation 和 min interval 控制频率。

---

## 五、需要 Opus 4.7 继续完善的点

请 Opus 4.7 基于真实代码继续完善以下内容：

1. 确认“不推送”的真实定义：是 `actionable_signals` 为空，还是外部通知渠道未送达。
2. 为通知过滤器增加 diagnostics，避免继续盲目调阈值。
3. 如果降低买入阈值，同步修改 `score.thresholds` 和 `strategy_scores.*.thresholds`。
4. 保持 T 日静默默认开启，除非 diagnostics 证明它误杀。
5. 不要把生产环境直接加入 `enabled_modes/auto_run_modes`；生产启用要走 canary 或独立部署配置。
6. 重新设计周末学习任务为 `WE-LEARN-01`，优先复用 learning manifest / shadow proposal 流程。
7. 如果仍保留基础训练 fallback，必须使用 `full_market=True`，并明确是否允许 reload predictor。
8. 补齐 idle queue 的所有挂载点，不能只加 manifest 和任务函数。
9. 增加测试覆盖。
10. 明确 diagnostics 的用户查看方式，例如 API、runtime 状态页、最新诊断 service 方法。
11. 给出阈值调整前后的历史信号回放统计，而不是凭经验估计信号增幅。
12. 明确配置回滚方式和服务重新加载配置的要求。
13. 补充 WE-LEARN-01 运行监控和告警指标。

---

## 六、建议测试清单

至少补充或更新以下测试：

### 推送相关

- `tests/test_config.py`
  - 更新默认阈值断言。
- `tests/test_notification_filter.py`
  - 覆盖 `min_score=60`。
  - 覆盖 T 日静默仍然只命中特定 reason。
  - 覆盖 `max_signals_per_run=5`。
- service 层测试
  - strategy 为 `trend/monster` 时，买入推送阈值来自 `strategy_scores`。

### idle queue / 自主学习相关

- `tests/test_service_idle_queue.py`
  - 周末 due tasks 包含 `WE-LEARN-01`。
  - `WE-LEARN-01` min interval 未到时跳过。
  - dispatch 能调用 `WE-LEARN-01`。
  - 输出 `learning_report.json`。
- `tests/test_service_learning_governance.py`
  - 周末任务调用 `build_learning_trainable_manifest` 和 `run_learning_manifest_shadow_proposal`。
  - manifest 不可用时返回 degraded/skipped，不覆盖模型。
- `tests/test_service_scheduler.py`
  - simulation/staging 下 auto policy 行为不被破坏。
  - production canary 行为不被误改成全量启用。

### 边界条件

- 分数恰好等于阈值
  - `score=60` 且 `min_score=60` 时应通过。
  - 策略级阈值也应使用 `>=` 语义。
- 多个 symbol 同时触发
  - 当 10 个信号都满足条件且 `max_signals_per_run=5` 时，应按 score 降序选择 top 5。
  - 分数相同时需要确认现有排序是否稳定，避免同分信号随机抖动。
- cooldown 边界
  - 第 300 秒再次触发是否仍在冷却内，需要按 cache TTL 实际语义测试。
  - 第 301 秒再次触发应可再次推送。
- 周末时间窗口临界
  - 周一 `08:44` 不应启动超过剩余预算的 `WE-LEARN-01`。
  - 周一 `08:46` 应进入 hard stop 或 off-window 语义，不应再启动重任务。
- learning manifest 边界
  - mature samples 刚好等于最小训练样本数时应通过。
  - mature samples 少 1 条时应返回 skipped/degraded，而不是 error。

---

## 七、遗漏补充与实施约束

以下是二次审阅后需要补进方案的关键约束。它们不推翻前面的修订方向，但会影响最终实现形态和验收标准。

### 1. 先定义“不推送”的症状层级

“不推送”至少有三种不同症状，排查顺序不能混在一起：

| 类型 | 现象 | 优先排查 |
|------|------|----------|
| A | 有原始信号，但 `actionable_signals` 为空 | 评分阈值、`allowed_actions`、T 日静默、cooldown、quiet window |
| B | `actionable_signals` 非空，但没有调用通知 | service 层通知路径、`_notify_actionable_signals`、dedup key、任务触发阶段 |
| C | audit 显示通知已发送，但用户没收到 | 通知渠道配置、token/webhook、外部平台返回、静默窗口 |

因此，修复前应先拿一条最近运行 payload 或 audit event 判断属于 A/B/C 哪一种。  
如果没有这一步，容易把渠道问题误判成阈值问题。

### 2. cooldown 必须纳入诊断

当前通知过滤器会按同一 `symbol + action` 做去重：

```text
notify:{symbol}:{action}
```

默认 `cooldown_sec: 300`，即同一标的同一动作 5 分钟内只推第一次。  
如果用户盯盘时看到同一标的短时间内多次触发，后续信号被静默是预期行为，不一定是 bug。

诊断中需要统计：

- `rejected_by_cooldown_count`
- `cooldown_symbols`
- `cooldown_sec`
- `dedup_by_symbol_action`

### 3. allowed_actions 与 sell 路径要区分

`notification_filter.allowed_actions: [buy, watch]` 只影响 `NotificationFilter.filter()` 输出的 `actionable_signals`。

需要明确：

- `hold` 不推送是预期行为。
- `buy/watch` 受 `min_score`、T 日静默、cooldown 等过滤。
- `sell` 不应依赖 `allowed_actions`，而是由 `_notify_actionable_signals` 单独走 P0 级别通知。

如果用户反馈“卖出也不推送”，不能简单归因于 `allowed_actions`。需要单独检查：

- 是否真的产生了 `sell` action。
- `_notify_actionable_signals` 是否被调用。
- sell 通知 dedup key 是否已存在。
- 通知渠道是否成功送达。

### 4. diagnostics 的落地位置

建议按两层落地。

第一层：在 `NotificationFilter.filter()` 内部生成过滤诊断。

建议返回或暴露类似结构：

```python
{
    "input_count": 12,
    "accepted_count": 3,
    "rejected_by_action": 1,
    "rejected_by_score": 4,
    "rejected_by_t_day_silence": 1,
    "rejected_by_cooldown": 2,
    "quiet_window_hit": False,
    "accepted_symbols": ["600000", "000001"],
}
```

第二层：在 `runtime/service.py` 的 pipeline payload 中挂载诊断，并写 audit event。

建议输出位置：

- `payload["notification_filter_diagnostics"]`
- audit event：`notification_filter_diagnostics`
- runtime 状态页或 API 可查询最近一次诊断

不建议只写日志，因为用户很难从日志里判断当前是 A/B/C 哪一层问题。

### 5. WE-LEARN-01 的数据前置检查

周末学习任务不应在数据状态未知时直接跑训练。执行前建议复用或抽取现有盘后 gate：

```python
gate = service.evaluate_post_market_warehouse_followup_gate(...)
```

至少检查：

- market warehouse 最近同步是否成功。
- `latest_trade_date_coverage_ratio` 是否达到阈值。
- 是否仍有 retry failed symbols。
- 背景数据状态是否可用。
- 学习样本库是否有足够 mature samples。

如果 gate 不通过，`WE-LEARN-01` 应返回：

```text
status: skipped 或 degraded
reason: market_warehouse_gate_failed / insufficient_mature_samples
```

不要在数据质量不足时强行训练。

### 6. 样本成熟度要求

`build_learning_trainable_manifest` 默认只使用：

```text
reconciled
fully_matured
```

如果刚完成数据仓库更新，但样本还没有完成 reconcile 或 label maturity，manifest 为空是合理结果。

方案中应明确：

- manifest 为空不一定是训练失败。
- 应把 maturity breakdown 写进 `WE-LEARN-01` 报告。
- 若 `reconciled + fully_matured` 数量低于训练阈值，应跳过训练并提示需要更多成交/持仓/回测结果沉淀。
- 不建议为了“自主学习”而放宽到 `pending`，除非单独做低置信度 shadow 实验。

### 7. auto_promotion 的语义必须讲清楚

当前默认：

```yaml
auto_promotion:
  enabled: false
```

这意味着系统可以自动训练、自动生成 shadow proposal、自动跑 promotion gate，但默认不会自动上线或替换 champion。

需要把“自主学习”拆成两层定义：

- 自动学习：自动构建 manifest、训练 shadow、生成 proposal、产出报告。
- 自动发布：proposal 通过后自动 approve/release/reload predictor。

如果用户目标只是“系统能自主学习并给出候选”，可以保持 `auto_promotion.enabled=false`。  
如果用户目标是“模型通过门禁后自动上线”，需要新增明确配置和风险提示，而不是隐式打开。

建议不要直接修改默认 `auto_promotion.enabled`。更稳妥的是为 `WE-LEARN-01` 增加独立配置：

```yaml
idle_learning:
  enabled: true
  auto_release_enabled: false
  auto_reload_predictor: false
  notify_summary: true
```

### 8. 与现有学习/演化流程避免重复

系统已有多个学习相关入口：

- 盘后 `post_followup_run_training`
- `build_learning_trainable_manifest`
- `run_learning_manifest_shadow_proposal`
- M1 负样本/漏信号学习
- evolution 模块中的多阶段评估

`WE-LEARN-01` 不应再造一条独立训练链。推荐定位为：

```text
周末兜底调度器：当盘后学习未运行、失败、样本已成熟但未生成 proposal 时，补跑现有学习治理链。
```

需要在任务报告中写清楚：

- 本周是否已经有成功的 learning proposal。
- 本次是否因为已有新近结果而跳过。
- 本次是否复用了盘后 followup 的结果。
- 是否生成了新的 proposal / release ticket / model registry record。

### 9. 时间预算与资源限制

理论周末窗口约为周六 12:00 到周一早上 hard stop，但实际可用时间会被已有任务占用：

- `WE-P0-01`
- `WE-P0-02`
- P1 轮转任务
- `WE-P2-08`

`WE-LEARN-01` 不建议设为 `P0` 或 `must_run: true`。  
应设置独立预算，并在任务开始前检查剩余时间。

建议 manifest 包含：

```python
"max_wall_time_minutes": 240,
"min_remaining_minutes": 270,
"symbol_cap": 100,
"max_dataset_rows": 100000,
```

如果剩余时间不足，返回 deferred，而不是挤占 hard stop 前的报告窗口。

### 10. shadow validation 的资源消耗

shadow proposal 可能涉及：

- 训练或加载 shadow 模型。
- 加载 champion 模型。
- 在测试集上做对比预测。
- 生成 champion-shadow 报告。
- 运行 promotion gate。

`symbol_cap=200` 可能对 CPU、内存和耗时都有压力。  
建议先以较小上限启用，例如：

```yaml
idle_learning:
  symbol_cap: 80
  max_dataset_rows: 100000
  allow_parallel_training: false
```

当前 idle queue 每轮只执行一个任务，但训练内部仍可能消耗较多资源。报告中应记录：

- elapsed seconds
- dataset rows
- symbols used
- memory/resource warning（如果可采集）
-是否触发 timeout 或 partial report

### 11. 失败恢复与 blocked 状态

当前 idle queue 默认：

```yaml
manual_ack_required: true
default_retry_max_retries: 1
default_retry_only_on: ["transient_io_error", "network_timeout", "file_handle_busy"]
```

所以 `WE-LEARN-01` 需要明确 retry policy：

- 数据不足、manifest 为空：不应视为 error，应返回 skipped/degraded，避免进入 blocked。
- 临时 IO 或网络错误：允许默认 retry。
- schema mismatch、训练数据不满足门槛：不 retry，并写清楚原因。
- 连续 fallback/error 后是否需要 manual ack，应在 idle state 中可见。

建议新增任务级 retry policy：

```python
"retry": {
    "max_retries": 1,
    "retry_only_on": ["transient_io_error", "network_timeout", "file_handle_busy"],
    "no_retry_on": ["insufficient_samples", "manifest_empty", "market_warehouse_gate_failed"],
}
```

### 12. 学习结果可观测性与回滚

学习任务完成后，用户需要知道“到底有没有更新模型”。

`WE-LEARN-01` 报告至少应包含：

- `dataset_manifest_id`
- `shadow_model_id`
- `champion_model_id`
- `proposal_id`
- `release_ticket_id`
- `promotion_gate_status`
- `auto_promotion_enabled`
- `predictor_loaded`
- `artifact_path`
- `model_registry`
- `rollback_available`

如果没有自动上线，应明确：

```text
status: proposal_generated
online_effect: none
next_action: manual_review_release_ticket
```

如果自动上线，应记录：

```text
online_effect: predictor_reloaded
previous_champion_model_id
new_champion_model_id
rollback_ticket_or_command
```

---

## 八、排查决策树

### 股票不推送

建议按以下顺序排查：

1. 最近一次 pipeline 是否产生原始 `signals`？
2. 如果有，`actionable_signals` 是否为空？
3. 如果为空，看 `notification_filter_diagnostics`：
   - 是否被 `min_score` 拦截？
   - 是否 action 不在 `allowed_actions`？
   - 是否 T 日静默？
   - 是否 cooldown？
   - 是否 quiet window？
4. 如果 `actionable_signals` 非空，检查是否调用通知。
5. 如果通知已调用，检查 audit event 中 `delivery.success`、`channel`、`error`。
6. 如果 delivery 成功但用户未收到，检查外部平台配置和收件人设置。

### 自主学习不运行

建议按以下顺序排查：

1. `idle_queue_state()` 中 effective enabled/auto_run 是否为 true？
2. 当前时间是否处于 workday/weekend idle window？
3. `idle_queue_history` 最近是否有任务运行？
4. 是否存在 blocked tasks，需要 manual ack？
5. market warehouse gate 是否通过？
6. learning sample maturity 是否满足训练阈值？
7. 本周是否已经有 learning proposal，是否需要跳过重复训练？
8. shadow proposal 是否因资源、时间、样本不足失败？
9. auto promotion 是否关闭，导致模型只生成候选但未上线？

---

## 九、实施补充清单

这一节用于把前面的原则落成更具体的实现 brief，避免实现者只看到“要加 diagnostics / 要挂载任务”，但不知道最终应改到哪里、用户如何查看、如何评估风险。

### 1. Diagnostics 的展示方式

诊断不应只存在于内部日志里。建议至少提供三种访问路径：

#### pipeline payload

最近一次 pipeline run 的返回 payload 中直接包含：

```python
payload["notification_filter_diagnostics"] = {
    "trace_id": trace_id,
    "timestamp": timestamp.isoformat(),
    "input_count": len(signals),
    "accepted_count": len(actionable_signals),
    "rejected_by_action": 0,
    "rejected_by_score": 0,
    "rejected_by_t_day_silence": 0,
    "rejected_by_cooldown": 0,
    "quiet_window_hit": False,
    "min_score": service._config.notification_filter.min_score,
    "allowed_actions": list(service._config.notification_filter.allowed_actions),
}
```

#### service 方法

新增或复用 runtime service 状态方法，暴露最近一次诊断：

```python
def latest_notification_filter_diagnostics(self) -> dict[str, object] | None:
    return self._last_notification_filter_diagnostics
```

同时可在 runtime status/dashboard payload 中增加：

```text
notification_filter_diagnostics
```

#### API 或页面展示

如果已有 runtime 状态接口，优先扩展现有接口，不急着新增端点。  
推荐展示字段：

- 最近一次运行时间
- 原始 signals 数
- actionable signals 数
- 被 score/action/cooldown/T 日静默/quiet window 拦截数量
- top rejected examples（最多 5 条，避免刷屏）

### 2. WE-LEARN-01 完整挂载路径

当前 idle queue 不是纯 manifest 驱动，任务列表和 dispatch 都有硬编码路径。因此 `WE-LEARN-01` 至少需要以下挂载点。

#### `idle_queue_manifest_service.py`

在 `build_idle_task_manifests()` 返回值中新增：

```python
"WE-LEARN-01": {
    "task_id": "WE-LEARN-01",
    "priority": "P1",
    "schedule": "weekend",
    "phase": 2,
    "must_run": False,
    "defer_policy": "next_weekend",
    "rotating_priority": 0,
    "max_defer_runs": 2,
    "force_run_on_disk_usage_pct": 100.0,
    "max_wall_time_minutes": 240,
    "min_remaining_minutes": 270,
    "symbol_cap": 80,
    "task_output_subdir": "model_learning",
    "write_whitelist": [],
    "min_interval_days": 7,
}
```

#### `idle_queue_registry_service.py`

需要让周末 due task 选择包含 `WE-LEARN-01`。  
建议将它作为 P1 类任务参与轮转，或在 P0 完成后、常规 P1 前加一个受 min interval 控制的学习任务。

注意：不要只在 manifest 增加任务；当前 `idle_weekend_due_tasks()` 使用固定任务列表。

同时 `run_idle_task()` 需要新增分发：

```python
if task_id == "WE-LEARN-01":
    return cast(dict[str, object], service._idle_task_we_learn_01(context=context))
```

这里应继续走 `service._idle_task_we_learn_01()` wrapper，不要在 registry service 里直接访问 weekend service，保持当前分层结构。

#### `idle_queue_service.py`

新增 facade wrapper：

```python
def _idle_task_we_learn_01(self, context: dict[str, object]) -> dict[str, object]:
    return self._weekend_service._idle_task_we_learn_01(context)
```

#### `runtime/service.py`

新增主 service wrapper：

```python
def _idle_task_we_learn_01(self, context: dict[str, object]) -> dict[str, object]:
    return self._idle_queue_service._idle_task_we_learn_01(context)
```

#### `idle_queue_weekend_service.py`

实现 `_idle_task_we_learn_01()`，职责是调用现有 learning governance 链：

```text
market warehouse gate
sample maturity gate
build_learning_trainable_manifest
run_learning_manifest_shadow_proposal
write learning_report.json
notify summary
```

#### `idle_queue_state()`

不一定需要为 `WE-LEARN-01` 写特殊状态字段，但需要确保通用 idle state 能展示：

- task health
- blocked status
- last status
- manual ack required
- last output file

如果状态页只展示固定任务列表，则也要把 `WE-LEARN-01` 加进去。

### 3. 阈值调整影响评估

不要在方案中直接承诺“信号增加 30%-50%”这类固定比例。更稳妥的做法是用历史数据回放估算。

建议提供一个轻量统计报告：

```text
threshold_replay:
  sample_window: last_20_pipeline_runs
  current_min_score: 65
  candidate_min_score: 60
  current_actionable_count: 12
  candidate_actionable_count: 18
  delta_count: 6
  delta_pct: 50.0
  extra_symbols_top10: [...]
  extra_actions_breakdown:
    buy: 2
    watch: 4
```

需要同时统计：

- 全局阈值变化影响
- `strategy_scores.trend/monster` 阈值变化影响
- `notification_filter.min_score` 变化影响
- `max_signals_per_run` 截断影响
- cooldown 去重后实际可推送数量

建议先比较两个候选方案：

```text
方案 A：buy/watch min_score=60
方案 B：buy min_score=60，watch min_score=50
```

如果没有历史 payload 可回放，则先只调到 60，不建议直接降到 55。

### 4. 配置回滚策略

阈值调整属于运行策略变更，应提前写明回滚方式。

建议实施前保存配置快照：

```text
config/default.yaml.bak.20260503
```

或者使用 git 分支/commit 记录配置变更。  
如果效果不佳，回滚步骤应是：

1. 恢复配置文件或回滚对应 commit。
2. 按当前部署方式重新加载配置。
3. 如果服务不支持热加载配置，则重启服务。
4. 运行一次最小验证，确认 `load_config()` 中阈值恢复。

不要在方案中默认写“无需重启，下个周期生效”，除非先确认当前运行环境确实支持配置热加载。

### 5. WE-LEARN-01 监控指标

建议为 `WE-LEARN-01` 报告和告警保留以下指标：

```yaml
idle_learning:
  alert_on:
    duration_exceed_minutes: 200
    manifest_empty: true
    proposal_rejected: true
    data_quality_below: 0.85
    market_warehouse_gate_failed: true
    blocked_requires_manual_ack: true
    predictor_reload_failed: true
```

报告字段建议包含：

- `elapsed_seconds`
- `remaining_minutes_at_start`
- `market_warehouse_gate`
- `maturity_breakdown`
- `dataset_manifest_id`
- `manifest_included_snapshot_count`
- `manifest_included_outcome_count`
- `shadow_model_id`
- `champion_model_id`
- `proposal_status`
- `promotion_gate_status`
- `auto_promotion_enabled`
- `online_effect`
- `blocked_after_run`

告警不应只在失败时发。以下场景也建议通知：

- manifest 连续为空
- proposal 连续被拒
- 学习任务连续被 defer
- 任务进入 blocked 且需要 manual ack
- 自动上线成功或失败

### 6. 边界测试优先级

如果实现时间有限，优先做以下边界测试：

1. `score == min_score` 可通过。
2. 10 个合格信号只推 score top 5。
3. cooldown 到期前后行为符合 cache TTL。
4. `WE-LEARN-01` 未到 `min_interval_days` 时跳过。
5. market warehouse gate 不通过时跳过，不进入 blocked。
6. manifest empty 返回 skipped/degraded，不覆盖模型。
7. production 不会因为默认配置变化而全量启用 idle queue。

---

## 十、最终实施注意事项

### 1. 与 M1 模块的关系

M1 不是 `WE-LEARN-01` 的竞争实现，而是上游学习/负样本/泄漏过滤来源之一。  
两者关系建议明确成下面这样：

- M1 负责产生负样本、漏信号修正、泄漏过滤或相关学习反馈。
- `WE-LEARN-01` 负责在周末把“已成熟的学习材料”补跑成训练/提案闭环。
- 如果本周已经存在成功的 learning proposal 或 release ticket，`WE-LEARN-01` 应优先跳过重复训练，或只做质量复核。
- 如果 M1 刚生成新负样本，但样本仍未 reconcile / fully matured，`WE-LEARN-01` 应等待数据成熟，不要抢跑。

建议在任务前加入检查：

```text
latest_learning_proposal
latest_learning_release_ticket
model_registry.active_champion
learning sample maturity / reconciliation status
```

如果已有有效 proposal，`WE-LEARN-01` 的报告应明确写出：

```text
status: skipped
reason: existing_valid_learning_proposal
```

### 2. 并发与资源约束

当前 idle queue 每轮只执行一个任务，但 `WE-LEARN-01` 仍应视为重任务。

建议约束：

- 独占模式：`WE-LEARN-01` 运行时，其他 P1 学习类任务应延后。
- 内存预算：加载 champion + shadow 时要预留峰值内存。
- CPU 预算：`symbol_cap` 不宜一开始就设太高。
- 磁盘 I/O：避免与大规模同步、清理或重导出任务同窗运行。

建议把这些约束写进任务 manifest 或独立配置：

```yaml
idle_learning:
  allow_parallel_training: false
  symbol_cap: 80
  min_remaining_minutes: 270
  max_wall_time_minutes: 240
```

还要注意一个实现细节：当前 idle 任务超时是线程池 future timeout，超时后并不一定能强杀内部训练线程。  
因此 `WE-LEARN-01` 最好在执行中主动检查剩余预算，并支持早停返回 `degraded` 或 `timeout`。

### 3. 旧有数据兼容性

`WE-LEARN-01` 首次运行时，要优先兼容已有历史状态，而不是假定系统是空仓开局。

应检查：

- 是否已有 `active_champion`
- 是否已有学习 proposal history
- 是否已有 release ticket / approval 记录
- 当前样本是否已成熟到足以训练

建议行为：

- 如果已有健康 champion，可作为 shadow 对比基准。
- 如果历史 proposal 已经成功且最近期，则本次可以跳过。
- 如果历史数据不完整，只做 report，不覆盖模型、不释放新 champion。
- 如果样本数低于阈值，返回 `skipped/degraded` 并提示继续沉淀数据。

### 4. 通知频率控制

`WE-LEARN-01` 的通知不应太吵，但要把关键状态发出来。

建议如下：

| 场景 | 建议通知 |
|------|----------|
| 任务开始 | 可选，带 `trace_id` 即可 |
| 任务完成（成功） | 必须，发摘要 |
| 任务完成（skipped/degraded） | 必须，说明原因 |
| 任务失败 | 必须，发错误和建议 |
| 连续 skipped | 建议，累计 5 次后提醒 |
| 模型自动上线 | 必须，附回滚方式 |

推荐级别：

- 成功 / skipped：`info`
- 失败：`warn`
- 自动上线：`info`，但消息里要附风险提示和回滚路径

通知去重建议沿用：

- `trace_id`
- `trade_date`
- `task_id`
- `proposal_id` / `release_ticket_id`

---

## 十一、开工顺序与实施前检查

### 1. 是否还存在明显问题

当前修订版已经覆盖主要设计风险，可以进入实施阶段。  
剩余需要注意的不是方向性问题，而是实施前的确认项：

- 必须先确认“不推送”属于 A/B/C 哪一层，否则可能把渠道问题误当成阈值问题。
- 工时只能作为粗略估计，实际耗时要以代码复杂度、测试运行时间和历史数据可用性为准。
- `WE-LEARN-01` 建议后置实施；如果时间有限，先完成推送 diagnostics 和阈值/通道排查。
- production 环境仍需保持 canary 或单独部署配置，不应因为本方案直接全量打开。

### 2. 推荐实施顺序

建议分两阶段实施。

#### 第一阶段：推送问题闭环

1. 增加 `notification_filter_diagnostics`。
2. 暴露 diagnostics 到 pipeline payload、runtime 状态或 API。
3. 收集最近 pipeline payload / audit event，判断属于 A/B/C 哪一层。
4. 如果确认为阈值问题，做历史信号回放统计。
5. 同步调整 `notification_filter.min_score`、`score.thresholds` 和 `strategy_scores.*.thresholds`。
6. 验证通知通道配置和外部 delivery。

第一阶段收益最大，改动范围相对可控。

#### 第二阶段：WE-LEARN-01 学习兜底

1. 设计 `WE-LEARN-01` manifest。
2. 补齐 due list、dispatch、facade、主 service wrapper。
3. 实现 market warehouse gate、样本成熟度 gate、重复 proposal 检查。
4. 接入 learning manifest / shadow proposal 链。
5. 输出 learning report 和 summary notification。
6. 补齐边界测试和失败恢复测试。

### 3. 粗略工作量参考

以下仅作为排期参考，不作为承诺值：

| 阶段 | 预估工作量 | 优先级 |
|------|------------|--------|
| 推送 diagnostics | 2-3 小时 | P0 |
| 阈值配置与历史回放 | 2-3 小时 | P0 |
| 通知通道排查 | 视渠道配置而定 | P1 |
| WE-LEARN-01 manifest 与挂载 | 4-6 小时 | P1 |
| WE-LEARN-01 学习链实现 | 4-8 小时 | P1 |
| 测试覆盖与回归 | 4-6 小时 | P0 |

如果时间有限，先做推送相关的第一阶段，不必等待 `WE-LEARN-01` 完成。

### 4. 实施前检查清单

- [ ] 确认当前运行模式：`simulation` / `staging` / `production`。
- [ ] 确认 `idle_queue_state()` 当前 effective enabled / auto_run。
- [ ] 获取最近一次 pipeline payload 或 audit event。
- [ ] 判断“不推送”属于 A/B/C 哪一层。
- [ ] 确认通知通道配置：`console`、PushPlus、飞书、企业微信、Telegram、邮件等。
- [ ] 备份当前 `config/default.yaml` 或建立 git commit。
- [ ] 准备配置回滚方式，并确认是否需要重启服务。
- [ ] 确认 market warehouse 最近同步状态。
- [ ] 确认 learning sample maturity 是否足够。
- [ ] 确认 production 环境是否需要 canary，而不是全量启用。

---

## 十二、最终建议

推送问题可以先做配置和 diagnostics 的小修；自主学习问题不要直接按原方案落代码，应重做为“周末触发现有学习治理链”的任务。

优先级建议：

1. 用 A/B/C 症状分层确认“不推送”的真实位置。
2. 先加推送过滤 diagnostics，并提供 payload/status/API 展示。
3. 用历史信号回放估算阈值调整影响。
4. 同步调整全局与策略级阈值，并保留配置回滚路径。
5. 检查真实通知通道配置。
6. 为 `WE-LEARN-01` 增加 market warehouse gate、样本成熟度 gate、时间预算和资源限制。
7. 补齐 `WE-LEARN-01` 的 manifest、due list、dispatch、facade、主 service wrapper 和测试。
8. 用 learning manifest / shadow proposal 做学习闭环。
9. 明确 auto promotion 是“只生成候选”还是“自动上线”。
10. 最后再考虑基础训练 fallback。
