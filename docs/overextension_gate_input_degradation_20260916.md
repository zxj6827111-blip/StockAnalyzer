# 过热闸输入退化：一条静默否决整条买入路径的 P0

- 日期：2026-09-16
- 状态：**已定位、已加探测（PR #81）；判据本身未改**（改它会改变交易行为，待评审）
- 相关：`src/stock_analyzer/risk/overextension.py`、`src/stock_analyzer/runtime/services/week5_service.py`（`_latest_bar_dict`）、`src/stock_analyzer/runtime/services/week5_selection_engine.py`

## 1. 症状

夜扫长期 0 个 final signal。此前两轮排查给出的两种解释（阈值太高 / 分数没 alpha）都不成立：

- 12 轮夜扫、600 条候选，**录取 0、被拒 600**；
- 拒因计数（同一候选可多因）：`below_min_threshold` 589、**`overextension_reject_new_buy` 584**、`cross_review_failed` 556、`data_gate:blocked` 300；
- 最高分候选 **76.96 / 76.22 / 73.39 都在门槛 70 之上**，全部被过热闸拒掉；
- 9/14 那条 76.22 的闸门证据最完整：`action=buy`，`risk_gate` / `cross_review_gate` / `liquidity_gate` **全部 passed**，唯一拒因是过热。

## 2. 根因（实测取证）

`overextension.level` 在 **600/600** 条候选上都是 `reject`；更重要的是：

```
atr_distance / bias_ma5 == 33.333    （600/600，无一例外）
```

`33.333 == 1 / DEFAULT_ATR14_FALLBACK`，而 `DEFAULT_ATR14_FALLBACK = 0.03`、`DEFAULT_MA5_FALLBACK = 1.0`。这个恒定比值说明**两个 fallback 同时生效**，即喂进去的行既没有 `ma5` 也没有 `atr14`：

```
bias_ma5     = |close / 1.0 - 1|            = close - 1
atr_distance = |close - 1.0| / 0.03         = 33.333 × bias_ma5
```

代入实测值可自洽：`bias_ma5 = 5.06` ↔ 股价 ≈ 6.06 元；`16.11` ↔ ≈ 17.11 元（601919）；`134.91` ↔ ≈ 135.91 元（300866）；最大 `1718` ↔ ≈ 1719 元。全部是可解释的 A 股价格。

于是判据必然命中：

| 判据 | 阈值 | fallback 下取值 | 结果 |
|---|---|---|---|
| `bias_ma5 >= bias_reject_min` | 0.15 | `close - 1`（股价 > 1.15 元即命中） | reject |
| `atr_distance >= atr_distance_reject` | 3.0 | `33.3 × (close - 1)` | reject |

即 **任何股价高于约 1.15 元的票都会被判"过热"**，闸门退化为"无条件否决"。

## 3. 输入为什么是坏的

生产路径（`week5_selection_engine`）这样构造输入：

```python
bars = _bars_from_post_scan_enrichment(str(item.get("post_scan_enrichment", "")).strip())
overextension_decision = _overextension_decision_dict(
    row=_latest_bar_dict(bars), config=config.overextension,
)
```

`_latest_bar_dict` 原样拷贝 bars 的列；而 `_bars_from_post_scan_enrichment` 只要求
`open/high/low/close/turnover` 存在，**不要求 `ma5`/`atr14`**。`risk/overextension.py`
的 docstring 写的是"输入偏好 snapshot 新特征列（ma5/ma10/atr14/bias_ma5/ret5/…）"——
**期望输入与实际输入不是同一份 schema**。

## 4. 为什么之前的结论说"没有过热死锁"

Phase 1.5（9/6）的结论"过热仅拒 0.2~1.9%、死锁证伪"来自
`learning/gate_metrics.py` 那条路径，它从**真实的** `close/ma5/atr14` 算 bias/atr：

```python
atr_distance = abs(close - ma5) / atr14 if atr14 > 0 else 0.0
```

同一份公式、同一个 evaluator，**两套输入 schema**。所以那条结论对 harness 成立、
对生产不成立——这也是为什么"验过的口径"必须和"生产真正喂的那份输入"对齐。

## 5. 已做 / 未做

**已做（PR #81）**：巡检新增 `overextension_inputs`（`SEVERITY_DEFECT`）。主判据取
`|bias_ma5| > 0.5` 的候选占比（乖离 > 0.5 即收盘价高于 MA5 的 1.5 倍，真实行情不会在
大半个池子里同时出现）；**不把 `atr/bias == 33.333` 当主判据**，因为给真实输入时该比值
等于 `ma5/atr14`，低波动票可能正好是 33.33（有专门的防误报测试）。真实产物实跑：
`|bias_ma5|>0.5 的候选 50/50，reject 占比 100.0%` → 红。

**未做（需评审）**：闸门的输入供给与"缺输入时该怎么办"的语义。两条路：

| 方案 | 内容 | 影响 |
|---|---|---|
| A. 修输入 | 把特征快照的 `ma5/atr14` 喂给 evaluator（不是另算一套 ma5/ATR，避免第三套口径） | 闸门开始按真实乖离判 → 夜扫可能开始出信号，**改变交易行为** |
| B. 修语义 | 输入不可用时返回 `insufficient_input` 而非算出一个假值；由调用方决定 fail-open / fail-closed | 同上，且是风险策略选择 |

两者都必须连同**回滚方案**与**验证点**一起评审。注意当前 `SA__APP__ADVISORY_ONLY=true`
（只出建议、不动仓），这降低了风险，但不改变"必须显式决定"的性质。

## 6. 复现方法

```bash
# 巡检（容器内）
docker exec stock-analyzer-api python3 -m stock_analyzer.ops.runtime_invariants
# 期望：overextension_inputs 为 FAIL（除非已修输入供给）
```

```python
# 从产物直接看
# results[0].payload.report.source_report.signal_pool.candidates[*].overextension.metrics
# 若 atr_distance / bias_ma5 恒为 33.333 → fallback 生效中
```
