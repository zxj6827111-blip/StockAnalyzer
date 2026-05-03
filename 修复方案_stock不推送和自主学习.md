# 股票分析系统修复方案

> 生成时间：2026-05-03
> 分析版本：基于 main 分支最新代码

---

## 问题一：股票不推送

### 根因分析

系统有多个机制会阻止股票推送：

1. **通知分数阈值过高** — 股票评分必须 ≥65 分才会被推送
2. **策略级买入阈值** — 每个策略都有独立的买入阈值（默认 a=65）
3. **T日入场静默** — T日刚买入的股票会被静默不推送
4. **冷却期机制** — 同一标的同一动作有 5 分钟冷却期
5. **静默窗口** — 可能配置了特定不推送时间段

### 配置修改

**文件：** `config/default.yaml`

| 配置项 | 当前值 | 建议值 | 说明 |
|--------|--------|--------|------|
| `notification_filter.min_score` | 65 | 60 | 降低通知阈值 |
| `score.thresholds.a` | 65 | 60 | 降低A级买入阈值 |
| `score.thresholds.b` | 55 | 50 | 降低B级观察阈值 |
| `notification_filter.t_day_entry_silence_enabled` | true | false | 禁用T日静默 |
| `notification_filter.max_signals_per_run` | 3 | 5 | 增加每轮推送数量 |

### 具体修改

```yaml
# config/default.yaml 第 423-432 行

notification_filter:
  enabled: true
  cooldown_sec: 300
  min_score: 60          # 原值 65
  allowed_actions: [buy, watch]
  max_signals_per_run: 5 # 原值 3
  quiet_windows: []
  dedup_by_symbol_action: true
  t_day_entry_silence_enabled: false  # 原值 true
  t_day_silence_reason_keywords: [take_profit, stop_loss]
```

```yaml
# config/default.yaml 第 146-149 行

score:
  weights:
    lgbm: 0.30
    xgb: 0.25
    meta: 0.15
    news: 0.10
    board: 0.10
    completion: 0.10
  thresholds:
    s: 78
    a: 60   # 原值 65
    b: 50   # 原值 55
```

---

## 问题二：没有自主学习（模型训练）

### 根因分析

1. **空闲队列默认禁用** — `idle_queue.enabled = false`，`idle_queue.auto_run = false`
2. **仅支持 simulation/staging 模式** — `enabled_modes` 不包含 `production`
3. **训练任务不在空闲队列中** — 周末任务清单只有分析类任务，没有模型训练任务
4. **训练依赖盘后流程** — 训练只在 `MarketWarehouse` 盘后完成后触发

### 修复方案

需要修改 3 个文件：

#### 1. `config/default.yaml` — 启用空闲队列

**文件：** `config/default.yaml` 第 765-772 行

```yaml
idle_queue:
  enabled: true                                           # 原值 false
  auto_run: true                                         # 原值 false
  enabled_policy: "auto"
  auto_run_policy: "auto"
  enabled_modes: ["simulation", "staging", "production"]  # 添加 production
  auto_run_modes: ["simulation", "staging", "production"] # 添加 production
```

#### 2. `src/stock_analyzer/runtime/services/idle_queue_manifest_service.py` — 添加训练任务

**文件：** `src/stock_analyzer/runtime/services/idle_queue_manifest_service.py`

在 `build_idle_task_manifests()` 方法中，在 `"WE-P2-08"` 定义之后添加：

```python
# src/stock_analyzer/runtime/services/idle_queue_manifest_service.py
# 在 build_idle_task_manifests() 方法的返回值中，WE-P2-08 之后添加

"WE-TRAIN-01": {
    "task_id": "WE-TRAIN-01",
    "priority": "P0",
    "schedule": "weekend",
    "phase": 2,
    "must_run": True,
    "defer_policy": "none",
    "rotating_priority": 0,
    "max_defer_runs": 0,
    "force_run_on_disk_usage_pct": 100.0,
    "max_wall_time_minutes": 480,
    "symbol_cap": 200,
    "task_output_subdir": "model_training",
    "write_whitelist": [
        {
            "task": "WE-TRAIN-01",
            "paths": ["artifacts/model_v1.json"],
            "actions": ["write"],
        }
    ],
    "min_interval_days": 7,
},
```

#### 3. `src/stock_analyzer/runtime/services/idle_queue_weekend_service.py` — 实现训练任务

**文件：** `src/stock_analyzer/runtime/services/idle_queue_weekend_service.py`

在 `RuntimeIdleQueueWeekendService` 类中添加方法：

```python
# src/stock_analyzer/runtime/services/idle_queue_weekend_service.py
# 在 RuntimeIdleQueueWeekendService 类中添加

def _idle_task_we_train_01(self, context: dict[str, object]) -> dict[str, object]:
    service = self._service
    trade_date = str(context.get("trade_date", "")).strip()
    now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
    output_path = service._idle_output_path(
        trade_date=trade_date,
        task_id="WE-TRAIN-01",
        subdir="model_training",
        filename="training_report.json",
    )

    # 检查最小间隔（每7天最多训练一次）
    min_interval_days = _as_int(
        service._idle_task_manifests.get("WE-TRAIN-01", {}).get("min_interval_days"),
        default=7,
    )
    last_trade_date = service._idle_latest_trade_date_for_task(task_id="WE-TRAIN-01")
    current_trade_date_dt = _parse_trade_date(trade_date)
    if last_trade_date and current_trade_date_dt is not None:
        last_trade_date_dt = _parse_trade_date(last_trade_date)
        if last_trade_date_dt is not None:
            if (current_trade_date_dt - last_trade_date_dt).days < min_interval_days:
                skip_payload: dict[str, object] = {
                    "task_id": "WE-TRAIN-01",
                    "trade_date": trade_date,
                    "generated_at": now.isoformat(),
                    "status": "skipped",
                    "reason": "skipped: min_interval_not_reached",
                    "min_interval_days": min_interval_days,
                    "last_trade_date": last_trade_date,
                }
                service._idle_write_json(output_path, skip_payload)
                return {
                    "status": "skipped",
                    "reason": "skipped: min_interval_not_reached",
                    "output_files": [str(output_path)],
                }

    symbol_cap = _as_int(
        service._idle_task_manifests.get("WE-TRAIN-01", {}).get("symbol_cap"),
        default=200,
    )
    lookback_days = max(600, service._config.training.bootstrap_lookback_days)

    try:
        universe = service._idle_symbol_universe(
            task_id="WE-TRAIN-01",
            max_symbols=symbol_cap,
            min_symbols=50,
        )
        symbol_list = _string_list(universe.get("symbols", []))
    except Exception:
        symbol_list = []

    if not symbol_list:
        fallback_seed = list(service._config.training.bootstrap_seed_symbols or [])
        if fallback_seed:
            symbol_list = fallback_seed[:symbol_cap]
        else:
            payload = {
                "task_id": "WE-TRAIN-01",
                "trade_date": trade_date,
                "generated_at": now.isoformat(),
                "status": "degraded",
                "reason": "no_symbols_available",
            }
            service._idle_write_json(output_path, payload)
            return {
                "status": "degraded",
                "reason": "no_symbols_available",
                "output_files": [str(output_path)],
            }

    try:
        report = service.train_models(
            full_market=False,
            lookback_days=lookback_days,
            max_symbols=symbol_cap,
            preferred_symbols=symbol_list,
        )
        ok = bool(report.get("ok", False))
        status = "ok" if ok else "degraded"
        error_text = "" if ok else str(report.get("status", "unknown_error"))
        model_id = str(report.get("model_id", "")).strip()
        artifact_path = str(report.get("artifact_path", "")).strip()
        training_summary = report.get("training_summary", {})
        if not isinstance(training_summary, dict):
            training_summary = {}
    except Exception as exc:
        ok = False
        status = "error"
        error_text = str(exc)
        report = {}
        model_id = ""
        artifact_path = ""
        training_summary = {}

    payload = {
        "task_id": "WE-TRAIN-01",
        "trade_date": trade_date,
        "generated_at": now.isoformat(),
        "status": status,
        "ok": ok,
        "symbols_requested": symbol_cap,
        "symbols_available": len(symbol_list),
        "lookback_days": lookback_days,
        "model_id": model_id,
        "artifact_path": artifact_path,
        "training_summary": training_summary,
        "error": error_text,
    }
    service._idle_write_json(output_path, payload)

    service._record_audit_event(
        event_type="idle_queue_training",
        level="info" if ok else "warn",
        message=f"WE-TRAIN-01 {'completed' if ok else 'failed'}",
        payload=payload,
    )

    if ok:
        service.notify(
            title="P1 周末训练完成",
            content=f"周末模型训练完成\n标的数量={len(symbol_list)}\n模型ID={model_id or '-'}\n回望天数={lookback_days}",
            level="info",
            trace_id=f"we-train-01-{trade_date}",
        )

    return {
        "status": status,
        "output_files": [str(output_path)],
        "symbols_processed": len(symbol_list),
        "ok": ok,
    }
```

---

## 修改文件汇总

| 文件 | 修改类型 | 修改内容 |
|------|----------|----------|
| `config/default.yaml` | 配置修改 | 降低推送阈值，启用空闲队列 |
| `src/stock_analyzer/runtime/services/idle_queue_manifest_service.py` | 新增任务 | 添加 WE-TRAIN-01 任务定义 |
| `src/stock_analyzer/runtime/services/idle_queue_weekend_service.py` | 新增方法 | 实现 `_idle_task_we_train_01` 训练任务 |

---

## 预期效果

### 问题一修复后
- 股票评分 ≥60 分即可收到买入推送
- T日新入场的股票也会正常推送
- 每次推送最多 5 条信号

### 问题二修复后
- 周末（周六/周日）空闲时会自动执行模型训练
- 每次训练间隔至少 7 天
- 训练完成后会发送通知
- 训练输出保存到 `staging/idle_cache/{trade_date}/model_training/` 目录
