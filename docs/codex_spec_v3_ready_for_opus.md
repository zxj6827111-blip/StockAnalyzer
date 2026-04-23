# StockAnalyzer 优化规格书 v3（可交 Opus 复核）

> 状态：在 v2 基础上继续收敛，已针对 Codex 最新 review findings 完成修订
>
> 目标：保留 v2 已经修正正确的方向，同时把仍然不够落地的部分改成“可以直接开工”的施工稿

---

## v3 修订摘要

v3 重点解决以下 5 个问题：

1. T4 自动审批 / 自动发布必须显式绑定 `proposal_id` 与 `ticket_id`，不能依赖 latest 记录。
2. `run_post_market_warehouse_followup()` 不能把 proposal payload 当成 training payload 直接替换，必须保留现有 `model_id` / `artifact_path` / Phase-D 契约。
3. T8 不能再写“漏改调用点也不会报错所以问题不大”；v3 改为把特征口径一致性当成上线门禁。
4. T8 必须给出基准指数、日期对齐、训练 / 回填 / 实时三条链路的统一设计。
5. T5 不能删除；v3 改成“训练摘要 + gate 结果 + 发布执行”三段式通知。

额外收敛：

1. T6 的每日摘要不再新造平行通知方法，而是扩展已有 `_notify_daily_digest_if_needed()` / `_build_daily_digest_payload()`。
2. T9 本轮不再把 `regime_state` 直接并入 `FeatureEngineer.transform()`，避免历史训练样本误用“当前 M2 状态”导致时间错位；先保留在运行时门控层，待有日期级状态序列后再进入特征工程。

---

## 与 v2 一致、无需大改的部分

### T1：标签参数调整

保留 v2 方案：

- `take_profit_pct: 0.05 -> 0.08`
- `horizon_days: 5 -> 10`
- `primary: "soup_5d_tp5_before_sl5" -> "soup_10d_tp8_before_sl5"`
- `conflict_policy: "conservative_zero" -> "bar_shape_heuristic"`
- `SoupStrategyConfig.max_hold_days: 5 -> 10`

### T2：全市场训练参数

保留 v2 方案：

- `min_samples: 80 -> 200`
- `bootstrap_batch_size: 200 -> 50`
- `bootstrap_dataset_max_rows: 0 -> 500000`
- `bootstrap_per_symbol_rows_cap: 0 -> 500`
- `bootstrap_seed_watchlist_size: 50 -> 200`
- 新增 `bootstrap_full_market: bool = True`

### T3：自动晋升配置

保留 v2 的 `AutoPromotionConfig`，但补充字段定义：

```python
class AutoPromotionConfig(BaseModel):
    enabled: bool = False
    auto_load_predictor: bool = True
    notify_on_rejection: bool = True
    notify_on_training_summary: bool = True
    notify_on_manual_release_pending: bool = True
```

---

## T4：自动晋升逻辑（v3 重写版）

### 4.0 设计原则

1. 不绕过现有治理主链路。
2. 不用“latest proposal / latest ticket”做隐式目标。
3. 不破坏 `post_followup` 现有结果结构。
4. 自动晋升失败不等于训练失败；需要区分“训练成功但 gate 未通过”和“训练流程本身失败”。

### 4.1 当前仓库真实可复用链路

现有链路仍然是：

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

### 4.2 改动点：扩展 proposal 流程，但必须绑定 ID

扩展 `service.py` 与 `learning_governance_service.py` 中的 `run_learning_manifest_shadow_proposal()` 签名：

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
    auto_approve: bool = False,
    auto_release: bool = False,
    auto_reload_predictor: bool = True,
    notify_on_rejection: bool = False,
) -> dict[str, object]:
```

### 4.3 自动晋升伪代码（v3 版）

关键区别：所有后续步骤都绑定当次 proposal / ticket 的 ID。

```python
workflow_payload = self._service.run_learning_manifest_shadow_promotion_gate(...)
proposal_result = self._create_learning_model_proposal_from_gate_payload(...)

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
                release_payload = _mapping(_mapping(exec_result.get("ticket")).get("release_payload"))
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

### 4.4 `post_followup` 集成方式（v3 版）

这里不能“简单替换 training payload”，必须保留兼容字段。

#### 原则

1. `steps["train_learning_manifest"]` 仍然保留，供现有监控和 Phase-D 读取。
2. 新增 `steps["learning_shadow_proposal"]` 与 `steps["auto_promotion"]`。
3. `result["model_id"]` 继续存在，但来源改为当前 shadow model。
4. gate 未通过不应被视为整个 `post_followup` 失败；只有训练 / shadow validation / promotion gate 工作流本身失败时才失败。

#### 具体方案

```python
proposal_payload = self.run_learning_manifest_shadow_proposal(
    dataset_manifest_id=manifest_id,
    load_predictor=False,
    approve_if_passed=True,
    auto_approve=bool(self._config.auto_promotion.enabled),
    auto_release=bool(self._config.auto_promotion.enabled),
    auto_reload_predictor=bool(self._config.auto_promotion.auto_load_predictor),
    notify_on_rejection=bool(self._config.auto_promotion.notify_on_rejection),
    source_trace_id=str(effective_report.get("trace_id", "")).strip(),
)

steps["learning_shadow_proposal"] = proposal_payload
steps["auto_promotion"] = dict(proposal_payload.get("auto_promotion", {}) or {})

workflow_payload = dict(proposal_payload.get("workflow", {}) or {})
shadow_validation_payload = dict(workflow_payload.get("shadow_validation", {}) or {})
training_payload = dict(shadow_validation_payload.get("training", {}) or {})

# 保留旧步骤名，避免下游逻辑断裂
steps["train_learning_manifest"] = training_payload

if not bool(workflow_payload.get("ok", False)):
    raise RuntimeError(
        "learning_shadow_workflow_failed: "
        + ",".join(str(item) for item in proposal_payload.get("errors", []) or [])
    )

model_id = str(proposal_payload.get("shadow_model_id", "")).strip()
proposal_id = str(dict(proposal_payload.get("proposal", {}) or {}).get("proposal_id", "")).strip()
ticket_id = str(dict(proposal_payload.get("auto_promotion", {}) or {}).get("ticket_id", "")).strip()
release_status = str(dict(proposal_payload.get("proposal", {}) or {}).get("status", "")).strip()

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
            dict(proposal_payload.get("auto_promotion", {}) or {}).get("predictor_loaded", False)
        ),
        "release_status": release_status,
    },
)

result["model_id"] = model_id
result["learning_proposal_id"] = proposal_id
result["learning_release_ticket_id"] = ticket_id
result["learning_release_status"] = release_status
```

#### Phase-D 衔接规则

- `phase_d_tabular_deep` 继续使用 `result["model_id"]`。
- 该 `model_id` 改为 `shadow_model_id`。
- 即便 gate 未通过，只要 shadow model 训练成功并已注册，Phase-D 仍可运行。
- 若训练流程失败导致 `shadow_model_id` 为空，Phase-D 才按原逻辑跳过。

### 4.5 T4 通知边界

T4 只负责以下两类通知：

1. gate 未通过时的拒绝 / 阻断通知。
2. release execute 成功后的发布通知。

训练完成摘要不再由 T4 兜底，而交给 T5。

---

## T5：训练 / 门控摘要通知（v3 重写）

### 5.0 为什么不能删除

当前仓库里：

- `train_learning_manifest()` 的完成 / 失败主要体现在 audit event。
- `execute_learning_model_release_ticket()` 才会向用户发“模型发布已执行”通知。

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

### 5.3 配置

使用：

- `auto_promotion.notify_on_training_summary`
- `auto_promotion.notify_on_rejection`
- `auto_promotion.notify_on_manual_release_pending`

---

## T6：信号可见性（v3 保留核心方向，修正摘要实现）

### 6.1 `watch` 生成逻辑

保留 v2 方案：从 `soup.py` 入手，而不是只调通知过滤器。

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

### 6.2 `actionable_signals` 只负责即时通知

保留 v2 方案，但明确两条数据流：

1. `actionable_signals` 来自 `NotificationFilter.filter(signals, ...)`，用于即时 push。
2. 原始 `signals` / `latest_signals_snapshot()["signals"]` 用于每日 top-k 摘要。

即时通知中继续支持：

- `buy`
- `sell`
- `watch`

并确保：

```yaml
notification_filter:
  allowed_actions: [buy, watch]
```

### 6.3 每日 top-k 摘要不新建平行方法，直接扩展现有 daily digest

v2 的 `_push_daily_signal_summary()` 改为以下方案：

1. 继续复用 `_notify_daily_digest_if_needed()`
2. 继续复用 `_build_daily_digest_payload()`
3. 在 `_build_daily_digest_payload()` 中新增 `top_signal_candidates`

推荐结构：

```python
{
    "date": "...",
    "summary": {...},
    "recommend_buy_symbols": [...],
    "holding_warn_symbols": [...],
    "top_signal_candidates": [
        {
            "symbol": "600000",
            "action": "buy",
            "score": 82.4,
            "grade": "A",
            "lgbm_prob": 0.71,
            "xgb_prob": 0.68,
        },
        ...
    ],
}
```

生成规则：

- 数据源：`latest_signals_snapshot()["signals"]`
- 排序：按 `score` 倒序
- 截断：top 10
- 不使用 `actionable_signals`

通知文案里保留现有“运行/对账摘要”，并追加一段“今日 top-k 候选”。

---

## T7：自适应门控

保留 v2 方案，不再展开重写。核心要求不变：

- `cross_review.py` 增加 `champion_auc`
- 所有 `evaluate_cross_review` 调用点补传 `champion_auc`
- 保持当前门控链路，不新造平行判定器

---

## T8：Market-relative 特征（v3 施工版）

### 8.0 范围

本轮只做 market-relative，不做全市场截面排名。

本轮新增的模型特征建议为：

- `benchmark_ret_1d`
- `benchmark_ret_5d`
- `benchmark_ret_20d`
- `excess_ret_1d`
- `excess_ret_5d`
- `beta_20d`
- `beta_60d`
- `benchmark_above_ma20`

### 8.1 基准指数方案

不新增 `fetch_index_daily` 接口，直接复用现有 `provider.fetch_daily_bars()`：

- 主基准：`000300`
- 兜底基准：`399001`

原因：

1. 仓库已有通用日线拉取接口。
2. 仓库其他位置已经在使用 `000300` 作为 A 股代理。
3. 这样能避免为指数专门再造一套数据接口。

### 8.2 新增统一 helper，避免 8 个调用点各写一套

建议新增：

`src/stock_analyzer/feature/market_context.py`

包含：

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

必须明确如下规则：

1. 统一把个股 bars 和 benchmark bars 归一到 `trade_date` / DatetimeIndex。
2. 以个股 bars 的日期索引为主。
3. benchmark 先计算收益和均线，再 `reindex(bars.index).ffill()`。
4. 对齐后再计算超额收益和 beta。
5. 对无法对齐的头部窗口允许 `NaN`，最后按现有 feature pipeline 的习惯填充。

### 8.4 `FeatureEngineer.transform()` 只新增 `market_index`

v3 不再在本轮把 `regime_state` 放进 `transform()`。

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

### 8.5 8 个调用点的统一要求

以下调用点全部改为通过共享 helper 构造 `market_index`：

1. `pipeline.py:305`
2. `pipeline.py:543`
3. `trainer.py:106`
4. `backfill.py:128`
5. `walk_forward.py:101`
6. `service.py:6589`
7. `training_diagnostics.py:113`
8. `training_diagnostics.py:247`

### 8.6 上线门禁

v3 删除“漏改也不会报错，因此问题不大”这类表述，改成以下门禁：

1. `pipeline`、`trainer`、`backfill` 三条关键路径全部接入 `market_index` 后，才允许打开 market-relative 特征。
2. 训练前后必须比对三条路径的 feature 列集合完全一致。
3. 任何一条关键路径缺失 `market_index` 时，本轮视为未完成，不上线。

可选配置：

```python
class TrainingConfig(BaseModel):
    ...
    market_relative_enabled: bool = False
    market_benchmark_symbol: str = "000300"
    market_benchmark_fallback_symbol: str = "399001"
```

含义：

- 开发阶段先接线、验证
- 只在 3 条关键链路一致后把 `market_relative_enabled` 置为 `True`

---

## T9：M2 状态注入（v3 收敛版）

### 9.0 本轮决策

本轮不把 `regime_state` 注入 `FeatureEngineer.transform()`。

原因：

1. 当前仓库容易拿到“当前 M2 状态”，但还没有一份明确、稳定、可直接供训练样本按日期回放的状态序列接口。
2. 如果训练 / 回填样本误用了当前状态，会产生时间错位。
3. 这类问题比“先不做该特征”风险更高。

### 9.1 本轮保留内容

继续在运行时门控 / 风险层使用现有 M2 状态：

- 阈值平移
- 仓位缩放
- conservative mode

这部分仓库已经存在，不需要并入 `FeatureEngineer.transform()`。

### 9.2 延后到下一轮的内容

待具备“日期级 M2 状态序列”后，再做：

1. `regime_state_by_date` helper
2. 训练 / 回填 / 实时三条路径同时注入
3. `regime_state` one-hot 特征列

结论：T9 不从当前 v3 的模型特征改造批次中上线，只保留为下一轮 item。

---

## v3 完整执行顺序

```text
1. T3  -> AutoPromotionConfig 字段补齐
2. T1  -> 标签参数调整
3. T2  -> 全市场训练参数
4. T8  -> market_context helper + FeatureEngineer.transform(market_index=...) + 8 个调用点补齐
5. T7  -> 自适应门控小修
6. T6  -> watch 生成 + actionable 通知 + daily digest 扩展
7. T4  -> 自动晋升 ID 绑定 + post_followup 兼容接入
8. T5  -> 训练 / gate / 待发布摘要通知
9. T9  -> 延后，不进入本轮模型特征改造
```

---

## 验证清单（v3）

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

验证点：

- approval 记录绑定本次 `proposal_id`
- release ticket 绑定本次 `proposal_id`
- execute 绑定本次 `ticket_id`
- 并发 / 连续两次 proposal 时不会串单

### 4. `post_followup` 兼容性验证

验证点：

- `steps["train_learning_manifest"]` 仍存在
- `steps["learning_shadow_proposal"]` 新增成功
- `result["model_id"]` 正确回填为 `shadow_model_id`
- `phase_d_tabular_deep` 仍能读取 `model_id`

### 5. T8 特征一致性验证

验证点：

- `pipeline` / `trainer` / `backfill` 三条路径 feature 列集合一致
- benchmark 缺失时 feature flag 不打开
- `000300` 拉取失败时能回退到 `399001`

建议最小验证脚本：

```bash
python -m pytest tests/ -x -q
```

并补充针对以下内容的定向测试：

- `learning_governance_service` 自动审批 / 自动发布 ID 绑定
- `post_followup` 新旧步骤兼容
- `market_context` 日期对齐
- `daily_digest` top-k 追加逻辑

---

## 预期效果（v3）

与 v2 相比，v3 的主要变化不是“再加更多需求”，而是把容易误实现的地方收紧：

- 自动晋升从“概念上可行”变成“并发下也不会串单”
- `post_followup` 从“可能破坏下游 Phase-D”变成“保持兼容”
- T8 从“方向正确”变成“具备 benchmark / 对齐 / helper / 门禁 的完整实现方案”
- T5 从“被错误删除”变成“训练 / gate / 发布三段式通知”
- T9 从“高风险地硬塞进特征工程”变成“暂缓上线，避免时间错位”

保守预期：

- 推荐频率：较当前明显提升，但仍以高质量 `buy/watch` 为主
- 可见性：即时 `watch` + 每日 top-k 摘要
- AUC：Phase-1 market-relative 预期提升仍维持在 `0.01 - 0.03`
- 风险：显著降低“训练成功但治理 / 推送 / 特征口径接不上”的落地风险

---

## 给 Opus 的复核重点

建议 Opus 下一轮重点看这 4 件事：

1. T4 的 `proposal_id` / `ticket_id` 绑定是否还有遗漏分支。
2. `post_followup` 的兼容映射是否足够覆盖现有下游依赖。
3. T8 的 benchmark 选择与日期对齐规则是否还需要进一步收紧。
4. T9 本轮延期是否合理，还是仓库里已有足够稳定的日期级 M2 状态历史可直接接入。
