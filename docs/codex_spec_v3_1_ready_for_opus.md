# StockAnalyzer 优化规格书 v3.1（可交 Opus 复核）

> 状态：在 v3 基础上继续收敛，已吸收 v3 review 中成立或部分成立的反馈
>
> 目标：保留 v3 的整体方向，同时把配置落点、`post_followup` 集成边界、通知触发点、门控细节再收紧一层

---

## v3.1 修订摘要

v3.1 重点修正以下问题：

1. 明确 `AutoPromotionConfig` 是本次新增，而不是仓库现有配置。
2. 明确 `run_learning_manifest_shadow_proposal()` 中 `auto_approve` / `auto_release` / `auto_reload_predictor` / `notify_on_rejection` 也是本次新增参数。
3. 明确 `post_followup` 中 `build_trainable_manifest()` 与 fallback `bootstrap_learning_from_runtime_history()` 逻辑保持不变，只替换训练段。
4. 明确 `predictor` 的加载策略，区分“自动晋升开启”和“自动晋升关闭”两种路径。
5. 明确 `_notify_learning_workflow_summary()` 的调用时机。
6. 给 T7 补充 `champion_auc` 影响门控阈值的具体规则。
7. 将 T8 的 feature 开关上移为统一根配置，避免训练 / 回填 / 实时读取不同开关。

本轮不采纳的 review 点：

1. “T6 应改 `soup.py` 为 `pipeline.py`”这一条不成立。`TradeDecision` 的 `cross_review_pass` 分支实际就在 `strategy/soup.py` 的 `SoupStrategy.recommend()` 中；`pipeline.py` 只是传入 `cross_review_pass`。
2. “T8 8 个调用点行号已失效”在当前仓库也不成立；不过 v3.1 仍改成“文件 + 调用模式”描述，降低以后行号漂移的风险。

---

## 与 v2 / v3 一致、无需大改的部分

### T1：标签参数调整

保留既有方案：

- `take_profit_pct: 0.05 -> 0.08`
- `horizon_days: 5 -> 10`
- `primary: "soup_5d_tp5_before_sl5" -> "soup_10d_tp8_before_sl5"`
- `conflict_policy: "conservative_zero" -> "bar_shape_heuristic"`
- `SoupStrategyConfig.max_hold_days: 5 -> 10`

### T2：全市场训练参数

保留既有方案：

- `min_samples: 80 -> 200`
- `bootstrap_batch_size: 200 -> 50`
- `bootstrap_dataset_max_rows: 0 -> 500000`
- `bootstrap_per_symbol_rows_cap: 0 -> 500`
- `bootstrap_seed_watchlist_size: 50 -> 200`
- 新增 `bootstrap_full_market: bool = True`

---

## T3：自动晋升配置（v3.1 重写）

### 3.0 定位

`AutoPromotionConfig` 当前仓库中不存在；它是本次新增，而不是“补充已有字段”。

因此 T3 的真实改动范围是：

1. 新增 `AutoPromotionConfig`
2. 在 `StockAnalyzerConfig` 中注册 `auto_promotion`
3. 在 `config/default.yaml` 中新增 `auto_promotion:` 配置段

### 3.1 配置模型

建议新增：

```python
class AutoPromotionConfig(_StrictModel):
    enabled: bool = False
    auto_load_predictor: bool = True
    notify_on_rejection: bool = True
    notify_on_training_summary: bool = True
    notify_on_manual_release_pending: bool = True
```

并在总配置中注册：

```python
class StockAnalyzerConfig(_StrictModel):
    ...
    auto_promotion: AutoPromotionConfig = Field(
        default_factory=AutoPromotionConfig
    )
```

### 3.2 `default.yaml`

建议新增：

```yaml
auto_promotion:
  enabled: false
  auto_load_predictor: true
  notify_on_rejection: true
  notify_on_training_summary: true
  notify_on_manual_release_pending: true
```

---

## T4：自动晋升逻辑（v3.1 收紧版）

### 4.0 设计原则

1. 不绕过现有治理主链路。
2. 不用“latest proposal / latest ticket”做隐式目标。
3. 不破坏 `post_followup` 现有结果结构。
4. 自动晋升失败不等于训练失败；需要区分“训练成功但 gate 未通过”和“训练流程本身失败”。
5. 文档必须明确哪些参数和流程是“现有能力”，哪些是“本次新增”。

### 4.1 当前仓库真实可复用链路

现有治理链路仍然是：

```text
train_learning_manifest()
  -> run_learning_manifest_shadow_validation()
  -> evaluate_learning_model_promotion_gate()
  -> run_learning_manifest_shadow_proposal()
  -> record_learning_model_proposal_approval()
  -> issue_learning_model_release_ticket()
  -> execute_learning_model_release_ticket()
  -> confirm_learning_model_release_ticket()
```

其中：

- `service.py` 已有 `run_learning_manifest_shadow_proposal()` 对外包装。
- 真正 proposal / approval / ticket / execute 的实现位于 `learning_governance_service.py`。
- `execute_learning_model_release_ticket()` 已负责 champion/challenger 角色切换与发布通知。

### 4.2 新增参数范围

以下 4 个参数均为本次新增；当前仓库 `learning_governance_service.py` 中的实际签名还不包含它们：

- `auto_approve`
- `auto_release`
- `auto_reload_predictor`
- `notify_on_rejection`

因此本节的真实改动是“扩展现有 `run_learning_manifest_shadow_proposal()` 签名”，而不是“复用现有参数”。

建议目标签名：

```python
def run_learning_manifest_shadow_proposal(
    self,
    *,
    dataset_manifest_id: str = "",
    artifact_path: str | None = None,
    champion_model_id: str = "",
    split_names: Sequence[str] | None = None,
    max_rows: int | None = None,
    include_rows: bool = False,
    preview_limit: int = 5,
    max_samples: int | None = None,
    min_samples: int = 5,
    learning_rate: float = 0.1,
    signal_threshold: float = 0.5,
    load_predictor: bool = False,
    mark_shadow_validated: bool = True,
    min_shadow_v2_minus_champion_return: float = -0.02,
    max_shadow_v2_brier_delta: float = 0.05,
    max_shadow_v2_logloss_delta: float = 0.10,
    max_signal_divergence_ratio: float | None = None,
    approve_if_passed: bool = False,
    block_if_failed: bool = False,
    allow_warn_status: bool = True,
    source_trace_id: str = "",
    auto_approve: bool = False,        # 新增
    auto_release: bool = False,        # 新增
    auto_reload_predictor: bool = True,# 新增
    notify_on_rejection: bool = False, # 新增
) -> dict[str, object]:
```

### 4.3 实现归属说明

本节伪代码中的 `self` 指向：

- `RuntimeLearningGovernanceService`

因此：

- `self._create_learning_model_proposal_from_gate_payload(...)` 指的是同类中的既有 private helper，继续保持 private，不额外对外暴露。
- `self._service._model_registry` 与 `self._service._pipeline` 均为现有 `StockAnalyzerService` 成员，不是本次新增对象。

### 4.4 自动晋升伪代码

```python
# self == RuntimeLearningGovernanceService
workflow_payload = self._service.run_learning_manifest_shadow_promotion_gate(...)

# 现有 private helper，继续保留为 private
proposal_result = self._create_learning_model_proposal_from_gate_payload(
    gate_payload=workflow_payload.get("promotion_gate", {}),
    allow_warn_status=allow_warn_status,
    source_trace_id=source_trace_id,
)

proposal = _mapping(proposal_result.get("proposal"))
proposal_id = str(proposal.get("proposal_id", "")).strip()
gate_status = str(proposal.get("gate_status", "")).strip().lower()

auto_promotion = {
    "enabled": bool(auto_approve or auto_release),
    "proposal_id": proposal_id,
    "approval_id": "",
    "ticket_id": "",
    "auto_approve": False,
    "auto_release": False,
    "predictor_loaded": False,
    "status": "skipped",
    "errors": [],
}

if auto_approve and proposal_id and gate_status == "pass":
    approval_result = self.record_learning_model_proposal_approval(
        approver="auto_promotion_system",
        approved=True,
        proposal_id=proposal_id,
        note="auto-approved by auto_promotion policy",
        timestamp=now,
        source_trace_id=source_trace_id,
    )
    auto_promotion["auto_approve"] = bool(approval_result.get("accepted", False))
    auto_promotion["approval_id"] = str(
        _mapping(approval_result.get("record")).get("approval_id", "")
    ).strip()

    if auto_release and auto_promotion["auto_approve"]:
        ticket_result = self.issue_learning_model_release_ticket(
            operator="auto_promotion_system",
            proposal_id=proposal_id,
            note="auto-issued by auto_promotion policy",
            timestamp=now,
            source_trace_id=source_trace_id,
        )
        ticket = _mapping(ticket_result.get("ticket"))
        ticket_id = str(ticket.get("ticket_id", "")).strip()
        auto_promotion["ticket_id"] = ticket_id

        if ticket_id:
            exec_result = self.execute_learning_model_release_ticket(
                executor="auto_promotion_system",
                ticket_id=ticket_id,
                confirm_window=True,
                note="auto-executed by auto_promotion policy",
                timestamp=now,
                source_trace_id=source_trace_id,
            )
            auto_promotion["auto_release"] = bool(exec_result.get("accepted", False))

            if auto_promotion["auto_release"] and auto_reload_predictor:
                release_payload = _mapping(
                    _mapping(exec_result.get("ticket")).get("release_payload")
                )
                shadow_model_id = str(release_payload.get("shadow_model_id", "")).strip()
                shadow_entry = self._service._model_registry.get_by_id(shadow_model_id)
                if shadow_entry is not None and str(shadow_entry.artifact_uri).strip():
                    auto_promotion["predictor_loaded"] = bool(
                        self._service._pipeline.reload_predictor(
                            artifact_path=str(shadow_entry.artifact_uri)
                        )
                    )

if auto_approve and gate_status != "pass" and notify_on_rejection:
    ...  # 发送 gate fail / warn 摘要

payload = {
    ...,
    "proposal": proposal,
    "proposal_result": proposal_result,
    "auto_promotion": auto_promotion,
}
```

### 4.5 `post_followup` 集成方式

#### 4.5.1 保持不变的上游阶段

`run_post_market_warehouse_followup()` 中以下逻辑保持不变：

1. `build_learning_trainable_manifest()`
2. build 失败时 fallback 到 `bootstrap_learning_from_runtime_history(build_manifest=True)`
3. `manifest_id` 仍由这两步产生

也就是说，本次不是“整段替换训练链路”，而是：

- 保留 `build manifest -> fallback bootstrap -> manifest_id`
- 仅替换拿到 `manifest_id` 之后的训练段

#### 4.5.2 仅替换训练段

```python
manifest_payload = self.build_learning_trainable_manifest()
if not bool(manifest_payload.get("ok", False)):
    bootstrap_payload = self.bootstrap_learning_from_runtime_history(
        build_manifest=True,
    )
    manifest_payload = dict(bootstrap_payload.get("manifest", {}))
    manifest_payload.setdefault(
        "dataset_manifest_id",
        str(bootstrap_payload.get("dataset_manifest_id", "")),
    )
    manifest_payload.setdefault("ok", bool(bootstrap_payload.get("ok", False)))

manifest_id = str(manifest_payload.get("dataset_manifest_id", "")).strip()
if not bool(manifest_payload.get("ok", False)) or not manifest_id:
    raise RuntimeError("trainable_manifest_unavailable")
```

拿到 `manifest_id` 以后，才进入：

```python
auto_promotion_enabled = bool(self._config.auto_promotion.enabled)

proposal_payload = self.run_learning_manifest_shadow_proposal(
    dataset_manifest_id=manifest_id,
    load_predictor=not auto_promotion_enabled,
    approve_if_passed=True,
    auto_approve=auto_promotion_enabled,
    auto_release=auto_promotion_enabled,
    auto_reload_predictor=bool(self._config.auto_promotion.auto_load_predictor),
    notify_on_rejection=bool(self._config.auto_promotion.notify_on_rejection),
    source_trace_id=str(effective_report.get("trace_id", "")).strip(),
)
```

#### 4.5.3 predictor 加载策略

为了同时兼顾“治理一致性”和“当前 `post_followup` 兼容性”，v3.1 明确如下规则：

1. `auto_promotion.enabled == False`
   保持当前行为兼容：
   `load_predictor=True`
   含义：训练完成后仍像现在一样立即热加载 shadow artifact。

2. `auto_promotion.enabled == True`
   改为治理优先：
   `load_predictor=False`
   含义：训练阶段不热加载，只有 release execute 成功后才通过 `auto_reload_predictor` 热加载。

3. `auto_promotion.enabled == True` 但 gate 未过 / proposal 未执行
   保持当前 champion predictor，不提前切换 runtime predictor。

#### 4.5.4 `model_id` 语义说明

当前 `run_learning_manifest_shadow_validation()` 内部本身就是：

```text
train_learning_manifest(...)
  -> training_payload.model_registry.model_id
  -> shadow_model_id
```

因此在 `post_followup` 中：

- 旧 `model_id`
- 新 `shadow_model_id`

在语义上都指向“本次训练产出的 model registry 记录 ID”。

所以：

- `result["model_id"] = shadow_model_id`
- `phase_d_tabular_deep(model_id=...)` 可以继续复用

#### 4.5.5 兼容映射

```python
steps["learning_shadow_proposal"] = proposal_payload
steps["auto_promotion"] = dict(proposal_payload.get("auto_promotion", {}) or {})

workflow_payload = dict(proposal_payload.get("workflow", {}) or {})
shadow_validation_payload = dict(workflow_payload.get("shadow_validation", {}) or {})
training_payload = dict(shadow_validation_payload.get("training", {}) or {})

# 保留旧步骤名，避免下游监控 / 状态页 / phase_d 断裂
steps["train_learning_manifest"] = training_payload

if not bool(workflow_payload.get("ok", False)):
    raise RuntimeError(
        "learning_shadow_workflow_failed: "
        + ",".join(str(item) for item in proposal_payload.get("errors", []) or [])
    )

model_id = str(proposal_payload.get("shadow_model_id", "")).strip()
proposal_id = str(
    dict(proposal_payload.get("proposal", {}) or {}).get("proposal_id", "")
).strip()
ticket_id = str(
    dict(proposal_payload.get("auto_promotion", {}) or {}).get("ticket_id", "")
).strip()
release_status = str(
    dict(proposal_payload.get("proposal", {}) or {}).get("status", "")
).strip()

self._write_post_market_warehouse_followup_state(
    stage="learning_shadow_proposal",
    status="completed",
    payload={
        "dataset_manifest_id": manifest_id,
        "shadow_model_id": model_id,
        "proposal_id": proposal_id,
        "ticket_id": ticket_id,
        "artifact_path": str(training_payload.get("artifact_path", "")),
        "predictor_loaded": bool(
            dict(proposal_payload.get("auto_promotion", {}) or {}).get(
                "predictor_loaded", False
            )
        ) or bool(training_payload.get("predictor_loaded", False)),
        "release_status": release_status,
    },
)

result["model_id"] = model_id
result["learning_proposal_id"] = proposal_id
result["learning_release_ticket_id"] = ticket_id
result["learning_release_status"] = release_status
```

#### 4.5.6 Phase-D 衔接规则

- `phase_d_tabular_deep` 继续使用 `result["model_id"]`
- 该 `model_id` 改为 `shadow_model_id`
- 即便 gate 未通过，只要 shadow model 已训练成功并注册，Phase-D 仍可运行
- 若训练流程失败导致 `shadow_model_id` 为空，Phase-D 才按原逻辑跳过

### 4.6 T4 通知边界

T4 只负责以下两类通知：

1. gate 未通过时的拒绝 / 阻断通知
2. release execute 成功后的发布通知

训练完成摘要不由 T4 兜底，而交给 T5。

---

## T5：训练 / 门控摘要通知（v3.1 收紧版）

### 5.0 为什么不能删除

当前仓库里：

- `train_learning_manifest()` 的完成 / 失败主要体现在 audit event
- `execute_learning_model_release_ticket()` 才会向用户发“模型发布已执行”通知

因此，“治理链路存在”不等于“训练通知被覆盖”。

### 5.1 新目标

把 T5 从“单一训练完成提醒”改成三段式摘要通知：

1. 训练失败：`warn`
2. 训练成功但 gate 未通过 / proposal 被拒：`warn`
3. 训练成功且进入人工待发布状态：`info`

发布执行成功的通知继续复用现有治理通知，不重复发第二条相同内容。

### 5.2 推荐实现

在 `service.py` 新增统一摘要方法：

```python
def _notify_learning_workflow_summary(
    self,
    *,
    proposal_payload: dict[str, object],
    trace_id: str,
) -> dict[str, object]:
    ...
```

摘要优先级规则：

- `workflow.ok == False`：发送“训练 / shadow workflow 失败”
- `workflow.ok == True` 且 `proposal.gate_status != "pass"`：发送“训练完成，但门控未通过”
- `proposal.gate_status == "pass"` 且 `auto_promotion.auto_release == False`：发送“训练完成，待人工发布”
- `auto_promotion.auto_release == True`：不再重复发送，交给治理执行通知

### 5.3 调用时机

调用位置明确为：

- 在 `run_post_market_warehouse_followup()` 中
- 在 `learning_shadow_proposal` 状态写入完成之后
- 在进入 `phase_d_tabular_deep` 之前

原因：

1. 训练 / gate 摘要和治理主逻辑保持解耦
2. 即便后续 Phase-D 失败，训练 / gate 摘要仍能及时发出
3. 不把通知逻辑塞回 `RuntimeLearningGovernanceService` 内部

建议调用片段：

```python
self._write_post_market_warehouse_followup_state(...)

if bool(self._config.auto_promotion.notify_on_training_summary):
    self._notify_learning_workflow_summary(
        proposal_payload=proposal_payload,
        trace_id=str(effective_report.get("trace_id", "")).strip(),
    )

# 然后再进入 phase_d_tabular_deep
```

### 5.4 配置

使用：

- `auto_promotion.notify_on_training_summary`
- `auto_promotion.notify_on_rejection`
- `auto_promotion.notify_on_manual_release_pending`

---

## T6：信号可见性（v3.1 澄清版）

### 6.1 `watch` 生成逻辑的真实落点

本轮改动落点仍然是：

- `src/stock_analyzer/strategy/soup.py`
- 具体是 `SoupStrategy.recommend()`

`pipeline.py` 的作用只是：

- 计算 `cross_review`
- 把 `cross_review.passed` 传给 `self._strategy.recommend(...)`

因此 v3 对 T6 的判断保持不变，不改成 `pipeline.py`。

### 6.2 `watch` 生成逻辑

```diff
if not cross_review_pass:
-    return TradeDecision(action="hold", target_position=0.0, reason="cross_review")
+    if scored.grade in {"S", "A", "B"}:
+        return TradeDecision(
+            action="watch",
+            target_position=0.0,
+            reason="cross_review_near_miss",
+        )
+    return TradeDecision(action="hold", target_position=0.0, reason="cross_review")
```

### 6.3 `actionable_signals` 与 daily digest 的双轨

即时通知：

- 数据源：`NotificationFilter.filter(signals, ...)`
- 用途：`buy` / `sell` / `watch` 实时 push

每日摘要：

- 数据源：`latest_signals_snapshot()["signals"]`
- 用途：原始 score 排序后的 top-k 摘要

### 6.4 每日 top-k 摘要

不新建平行方法，直接扩展：

- `_notify_daily_digest_if_needed()`
- `_build_daily_digest_payload()`

新增字段：

```python
"top_signal_candidates": [
    {
        "symbol": "...",
        "action": "buy",
        "score": 82.4,
        "grade": "A",
        "lgbm_prob": 0.71,
        "xgb_prob": 0.68,
    }
]
```

规则：

- 数据源：`latest_signals_snapshot()["signals"]`
- 排序：按 `score` 倒序
- 截断：top 10
- 不使用 `actionable_signals`

---

## T7：自适应门控（v3.1 补细节）

### 7.0 当前缺口

当前 `cross_review.py` 只有：

```python
evaluate_cross_review(
    lgbm_prob: float,
    xgb_prob: float,
    meta_prob: float,
    config: CrossReviewConfig,
)
```

没有 `champion_auc`，也没有动态阈值逻辑。

### 7.1 配置扩展

建议在 `CrossReviewConfig` 中新增：

```python
class CrossReviewConfig(_StrictModel):
    p_lgbm_min: float = 0.60
    p_xgb_min: float = 0.55
    max_diff: float = 0.18
    p_meta_min: float = 0.54

    champion_auc_low: float = 0.55
    champion_auc_high: float = 0.62
    relax_threshold_delta: float = 0.02
    relax_max_diff_delta: float = 0.03
    tighten_threshold_delta: float = 0.01
    tighten_max_diff_delta: float = 0.02
```

### 7.2 动态门控规则

建议采用简单、可解释的三档规则：

1. `champion_auc is None`
   使用原始门槛，不动态调整

2. `champion_auc < champion_auc_low`
   说明当前 champion 质量偏弱
   放宽门槛：
   - `p_lgbm_min -= 0.02`
   - `p_xgb_min -= 0.02`
   - `p_meta_min -= 0.02`
   - `max_diff += 0.03`

3. `champion_auc > champion_auc_high`
   说明当前 champion 质量较强
   略微收紧门槛：
   - `p_lgbm_min += 0.01`
   - `p_xgb_min += 0.01`
   - `p_meta_min += 0.01`
   - `max_diff -= 0.02`

### 7.3 签名变更

```python
def evaluate_cross_review(
    lgbm_prob: float,
    xgb_prob: float,
    meta_prob: float,
    config: CrossReviewConfig,
    champion_auc: float | None = None,
) -> CrossReviewResult:
```

### 7.4 调用点

当前主调用点在：

- `pipeline.py` 中推理概率后调用 `evaluate_cross_review(...)`

调用前从 registry 获取：

```python
champion = self._model_registry.active_champion(suppress_read_errors=True)
champion_auc = (
    float(champion.metrics_summary.get("auc", 0))
    if champion is not None
    else None
)
```

---

## T8：Market-relative 特征（v3.1 收紧版）

### 8.0 范围

本轮只做 market-relative，不做全市场截面排名。

建议新增特征：

- `benchmark_ret_1d`
- `benchmark_ret_5d`
- `benchmark_ret_20d`
- `excess_ret_1d`
- `excess_ret_5d`
- `beta_20d`
- `beta_60d`
- `benchmark_above_ma20`

### 8.1 基准指数方案

不新增 `fetch_index_daily`，直接复用现有 `provider.fetch_daily_bars()`：

- 主基准：`000300`
- 兜底基准：`399001`

### 8.2 统一 helper

建议新增：

`src/stock_analyzer/feature/market_context.py`

```python
def fetch_market_benchmark_bars(
    provider: MarketDataProvider,
    *,
    lookback_days: int,
    primary_symbol: str = "000300",
    fallback_symbol: str = "399001",
) -> pd.DataFrame:
    ...

def build_market_index_frame(
    *,
    bars: pd.DataFrame,
    benchmark_bars: pd.DataFrame,
) -> pd.DataFrame:
    ...
```

### 8.3 日期对齐规则

1. 统一把个股 bars 和 benchmark bars 归一到 `trade_date` / DatetimeIndex
2. 以个股 bars 的日期索引为主
3. benchmark 先计算收益和均线，再 `reindex(bars.index).ffill()`
4. 对齐后再计算超额收益和 beta
5. 对无法对齐的头部窗口允许 `NaN`，最后按现有 feature pipeline 的习惯填充

### 8.4 `FeatureEngineer.transform()` 只新增 `market_index`

本轮不把 `regime_state` 放进 `transform()`。

新签名：

```python
def transform(
    self,
    bars: pd.DataFrame,
    intraday_1m: pd.DataFrame | None = None,
    intraday_5m: pd.DataFrame | None = None,
    market_index: pd.DataFrame | None = None,
) -> pd.DataFrame:
```

### 8.5 调用点描述

实施时优先按“文件 + 调用模式”搜索，不要求机械依赖行号：

1. `pipeline.py`
   在线评分主路径中的两处 `self._feature_engineer.transform(...)`

2. `models/trainer.py`
   训练数据生成路径中的 `self._engineer.transform(...)`

3. `learning/backfill.py`
   回填样本路径中的 `self._feature_engineer.transform(...)`

4. `backtest/walk_forward.py`
   walk-forward 路径中的 `self._engineer.transform(...)`

5. `training_diagnostics.py`
   诊断路径中的两处 `engineer.transform(...)`

6. `runtime/service.py`
   训练 / 诊断辅助路径中的 `engineer.transform(...)`

### 8.6 统一开关位置

`market_relative_enabled` 不再放在 `TrainingConfig`，而是上移为统一根配置。

建议新增：

```python
class MarketRelativeFeatureConfig(_StrictModel):
    enabled: bool = False
    benchmark_symbol: str = "000300"
    fallback_symbol: str = "399001"


class StockAnalyzerConfig(_StrictModel):
    ...
    market_relative_feature: MarketRelativeFeatureConfig = Field(
        default_factory=MarketRelativeFeatureConfig
    )
```

这样：

- `pipeline`
- `trainer`
- `backfill`

三条路径都从同一个根配置读取：

`self._config.market_relative_feature.enabled`

### 8.7 上线门禁

1. `pipeline`、`trainer`、`backfill` 三条关键路径全部接入 `market_index` 后，才允许打开 market-relative 特征
2. 训练前后必须比对三条路径的 feature 列集合完全一致
3. 任何一条关键路径缺失 `market_index` 时，本轮视为未完成，不上线

---

## T9：M2 状态注入（v3.1 维持延期）

### 9.0 本轮决策

本轮不把 `regime_state` 注入 `FeatureEngineer.transform()`。

原因：

1. 当前仓库容易拿到“当前 M2 状态”，但还没有一份明确、稳定、可直接供训练样本按日期回放的状态序列接口
2. 如果训练 / 回填样本误用了当前状态，会产生时间错位
3. 这类问题比“先不做该特征”风险更高

### 9.1 本轮保留内容

继续在运行时门控 / 风险层使用现有 M2 状态：

- 阈值平移
- 仓位缩放
- conservative mode

### 9.2 延后到下一轮的内容

待具备“日期级 M2 状态序列”后，再做：

1. `regime_state_by_date` helper
2. 训练 / 回填 / 实时三条路径同时注入
3. `regime_state` one-hot 特征列

结论：

- T9 不从当前 v3.1 的模型特征改造批次中上线
- 只保留为下一轮 item

---

## v3.1 完整执行顺序

```text
1. T3  -> 新增 AutoPromotionConfig + 注册到 StockAnalyzerConfig/default.yaml
2. T1  -> 标签参数调整
3. T2  -> 全市场训练参数
4. T8  -> market_context helper + FeatureEngineer.transform(market_index=...) + 三条关键路径统一接线
5. T7  -> champion_auc 动态门控
6. T6  -> watch 生成 + actionable 通知 + daily digest 扩展
7. T4  -> 自动晋升 ID 绑定 + post_followup 兼容接入
8. T5  -> 训练 / gate / 待发布摘要通知
9. T9  -> 延后，不进入本轮模型特征改造
```

---

## 验证清单（v3.1）

### 1. 配置验证

```bash
python -c "from stock_analyzer.config import StockAnalyzerConfig; c = StockAnalyzerConfig(); print(c.labels.take_profit_pct, c.auto_promotion.enabled)"
```

### 2. watch 与每日摘要验证

- 运行一次完整扫描
- 验证 cross review 未通过但评分 >= B 的标的能生成 `watch`
- 验证 `actionable_signals` 中可见 `watch`
- 验证 daily digest 中包含 top-k 原始 signals 摘要
- 验证 top-k 摘要来源不是 `actionable_signals`

### 3. 自动晋升 ID 绑定验证

- approval 记录绑定本次 `proposal_id`
- release ticket 绑定本次 `proposal_id`
- execute 绑定本次 `ticket_id`
- 并发 / 连续两次 proposal 时不会串单

### 4. `post_followup` 兼容性验证

- `build_trainable_manifest()` 保持不变
- fallback `bootstrap_learning_from_runtime_history()` 保持不变
- `steps["train_learning_manifest"]` 仍存在
- `steps["learning_shadow_proposal"]` 新增成功
- `result["model_id"]` 正确回填为 `shadow_model_id`
- `phase_d_tabular_deep` 仍能读取 `model_id`
- 自动晋升关闭时 predictor 仍按旧行为加载
- 自动晋升开启时 predictor 只在 execute 成功后加载

### 5. T7 验证

- `champion_auc is None` 时仍使用原门槛
- `champion_auc < 0.55` 时阈值被放宽
- `champion_auc > 0.62` 时阈值被收紧

### 6. T8 一致性验证

- `pipeline` / `trainer` / `backfill` 三条路径 feature 列集合一致
- benchmark 缺失时 feature flag 不打开
- `000300` 拉取失败时能回退到 `399001`
- 三条路径都读取同一个 `market_relative_feature.enabled`

建议最小验证脚本：

```bash
python -m pytest tests/ -x -q
```

并补充针对以下内容的定向测试：

- `learning_governance_service` 自动审批 / 自动发布 ID 绑定
- `post_followup` manifest 保留 + predictor 加载分支
- `market_context` 日期对齐
- `daily_digest` top-k 追加逻辑
- `cross_review` champion_auc 动态阈值逻辑

---

## 预期效果（v3.1）

与 v3 相比，v3.1 的提升不是新增更多功能，而是把几处最容易误实现的地方彻底钉死：

- T3 从“像是补字段”变成“明确新增配置类和总配置注册”
- T4 从“方向明确”变成“新增参数、对象归属、manifest/predictor 策略都明确”
- T5 从“有方法”变成“有确定触发点”
- T7 从“有方向”变成“有具体阈值规则”
- T8 从“有门禁”变成“有统一开关位置”

保守预期不变：

- 推荐频率：较当前明显提升，但仍以高质量 `buy/watch` 为主
- 可见性：即时 `watch` + 每日 top-k 摘要
- AUC：Phase-1 market-relative 预期提升仍维持在 `0.01 - 0.03`
- 风险：进一步降低“训练成功但治理 / 推送 / 特征口径接不上”的落地风险

---

## 给 Opus 的复核重点

建议 Opus 下一轮重点看这 5 件事：

1. T3 的配置注册范围是否完整
2. T4 的 `post_followup` predictor 分支是否还需要更保守
3. T5 的调用时机是否还有更合适的放置点
4. T7 的 `champion_auc` 三档阈值是否需要再调
5. T9 是否仍应延期，还是已经可以从 runtime history 中稳定提取日期级状态
