# Week5 Phase 3 子线① 实施细节：return_rank 切生产训练

日期：2026-09-10
状态：**待用户过目后动手**（本文件为实施前方案，不含代码改动）
前置：18-fold 硬门已过（IC +0.0658，方案 A 归因 → 双口径诊断 → 方向一' 复核）

## 1. 事实核验（本地代码确认）

| 事实 | 位置 / 证据 |
|---|---|
| `labels.basis` 默认 `"soup"`，**无生产消费方** | config.py:1056；全仓 grep 仅 config / labels 模块 / registry 三处引用 |
| return_rank label 函数已实现 | `labels/return_rank.py:29 build_return_rank_labels`（TOP30%→1 / BOTTOM30%→0 / drop_middle，ties 平均秩） |
| return_rank registry 契约已实现 | `learning/label_policy_registry.py:37 build_return_rank_policy_record` / `:278 register_return_rank`（schema v3，tp/sl=0 占位） |
| **PIT 数据集链已支持 return_rank** | `backtest/pit_dataset.py:478 _apply_return_rank_labels`（月度块合并阶段逐日横截面）+ `label_basis` 参数贯穿 `generate_pit_dataset`（:277）→ `_finalize_pit_dataset`（:522）→ 调用点 :567 |
| PIT 链消费方是**离线研究脚本**，不接生产 | `scripts/week5_label_remediation_run.py:42` 调用 `generate_pit_dataset`（18-fold harness 入口） |
| 生产训练链仍是 soup（硬编码） | `models/trainer.py:144` `train_on_bars` 直接 `build_soup_labels(...)`，不读 `labels.basis` |
| 生产主训练路径 | `train_on_sample_store`（trainer.py:156）→ `DatasetManifestBuilder` 从 sample_store 取 label |
| label 契约注册点 | `learning/backfill.py:122` `register_from_config(self._config.labels)`（soup v2 契约） |
| **outcome 表有横截面所需的收益字段** | `sample_store.py:564 outcome_records`：`realized_return DOUBLE` + `label_anchor_time`（可提 trade_date）+ `maturity_status` |

**结论**：这不是"改个 yaml 就切换"——需要 1 个核心开发点（生产 label 构造支持横截面语义）+ 1 个契约注册分支，因为 soup 是**逐股可算**、return_rank 是**同日全市场横截面分位**，两者数据结构要求不同。

## 2. 开发点（3 处）

### 2.1 核心：生产 label 构造按 basis 分支（含横截面后处理）

**现状**（`models/trainer.py:144`，`train_on_bars` 单标的路径）：
```python
labels = build_soup_labels(
    bars=filtered_bars,
    take_profit_pct=self._labels.take_profit_pct,
    stop_loss_pct=self._labels.stop_loss_pct,
    horizon_days=self._labels.horizon_days,
    price_basis=self._labels.pnl_price_basis,
    exclude_untradable=self._labels.exclude_untradable,
    conflict_policy=self._labels.conflict_policy,
    conflict_soft_label_value=self._labels.conflict_soft_label_value,
)
```

**改造原则**（保持最小改动 + 单一实现）：
1. 从 `backtest/pit_dataset.py::_apply_return_rank_labels` **提炼公共函数**到 `labels/return_rank.py`
   （如 `apply_return_rank_labels_by_day(frame, *, fwd_return_col, date_col, top_q, bottom_q, drop_middle, min_cross)`），
   pit_dataset 与生产链**共用同一实现**——避免两处逻辑漂移（这是本子线最大的正确性风险）。
2. 生产链在**数据集构建阶段**（`build_dataset_from_sample_store` 的 manifest 组装后、训练前）按 `labels.basis` 分支：
   - `soup`：现状不变（逐行 soup label）；
   - `return_rank`：用 `outcome.realized_return` 作 fwd_return、`label_anchor_time` 的日期作截面分组键，调公共函数生成 label；`drop_middle=True` 时中间段行剔除。
3. `train_on_bars`（单标的、无横截面）在 `basis=return_rank` 时**显式 fail-closed**：
   抛 `ValueError("return_rank_basis_requires_cross_section")`，不静默退回 soup（避免口径混用）。

### 2.2 label 契约注册分支

`learning/backfill.py:122`：
```python
# 现状：恒定 soup（v2）
label_policy = self._label_policy_registry.register_from_config(self._config.labels)

# 改造：按 basis 分支
if self._config.labels.basis == "return_rank":
    label_policy = self._label_policy_registry.register_return_rank(
        horizon_days=self._config.labels.horizon_days,
        price_basis=self._config.labels.pnl_price_basis,
        top_quantile=self._config.labels.return_rank_top_quantile,
        bottom_quantile=self._config.labels.return_rank_bottom_quantile,
        drop_middle=self._config.labels.return_rank_drop_middle,
        min_cross_section=self._config.labels.return_rank_min_cross_section,
    )
else:
    label_policy = self._label_policy_registry.register_from_config(self._config.labels)
```

注意：`register_return_rank` 是否接受全部参数需在实施时核对签名（当前已知参数：horizon_days / price_basis / schema_version）。

### 2.3 配置切换（default.yaml + NAS .env）

```yaml
labels:
  basis: return_rank            # 由 soup 切换
  return_rank_top_quantile: 0.3
  return_rank_bottom_quantile: 0.3
  return_rank_drop_middle: true
  return_rank_min_cross_section: 30
```
NAS 侧：`SA__LABELS__BASIS=return_rank`（或改 default.yaml——但按 NAS 部署约定，NAS 特有值走 .env，见 memory「NAS 部署配置落点」）。

**切换时机**：代码合入 + 本地验证通过后、周末重训窗口前。工作日保持 soup 不影响 night_scan（第 2 周的 basis 只在新样本写入时生效）。

## 3. 双入口一致性测试（新增 `tests/test_label_basis_matrix.py`）

| 测试 | 断言 |
|---|---|
| soup 回归（`basis=soup`） | 生产链 label 与改动前**逐字节一致**（黄金样本对比） |
| return_rank 截面语义 | 同日 top30%→1 / bottom30%→0 / 中间 NaN（drop_middle）；ties 平均秩 |
| 无跨日泄漏 | 打乱其他日期的 fwd_return 不影响本日 label（与 `test_week5_label_remediation.py::test_no_cross_day_leakage` 同法） |
| thin cross-section | 当日有效样本 < min_cross_section → 整日 NaN |
| **两入口一致性（关键）** | 同一 (symbol, trade_date) 上，生产链 label == PIT 链 label（共用函数后应恒等；这是防漂移的主测试） |
| fail-closed | `train_on_bars` + `basis=return_rank` → 抛 `return_rank_basis_requires_cross_section` |
| registry 契约 | basis=return_rank 时注册的是 v3 记录（schema_version=3、tp/sl=0、conflict_policy=rank_quantile） |

## 4. 周末重训 checklist

前置：代码合入 main + NAS 部署 + `SA__LABELS__BASIS=return_rank` 已生效。

1. **切换前快照**：记录当前 champion artifact hash、registry label_policy_id/hash、`m5_positive_ratio` 基线值（8.9%→28.0% 是 B4 口径，实施时以生产实测为准）。
2. **重训窗口**：`week5_weekend_learning_time=12:00` 触发（或手动 kick 训练任务）。
3. **样本量核查**：训练前确认可用样本数（return_rank 的 drop_middle 会剔除中间 40%——8.9%→28.0% 的正样本率变化正源于此，样本量是否仍 ≥ `training.min_samples=200` 需实测）。
4. **promotion gate 语义复核（关键）**：
   - `m5_positive_ratio_low/high = 0.30/0.70`（config.py:1343-1344，orchestrator.py:1294 消费）是按 **soup TP/SL** 标定的；
   - return_rank 下正样本率理论上限受 top_quantile 约束（drop_middle 后 ≈ top/(top+bottom) = 50%，而非 soup 的低正样本率）；
   - **动作**：重训后先看实测正样本率，若落在 [0.30, 0.70] 之外或贴近边界，需重新标定并在 `docs/` 记录新依据与理由；**不得无记录调整**。
5. **两阶段发布**：走 champion/challenger（`training.artifact_path` 别名原子切换），人工确认 registry hash 一致、manifest 完整。
6. **验收硬门**：
   - 18-fold 硬门不回退（IC +0.0658 为基线，各跑一次前后对比）；
   - lr1/lr2 生产零回归（次一交易日自然窗口确认）；
   - 新 champion 的预测分布合理性（分数中位是否变化——为子线② 终门校准提供输入）。

## 5. 风险与回滚

| 风险 | 说明 | 应对 |
|---|---|---|
| **两入口实现漂移** | 生产链与 PIT 链各写一份横截面逻辑 → label 口径不一致 | 强制提炼公共函数 + 一致性测试（§3 主测试） |
| **样本量骤减** | drop_middle 剔除中间段，训练样本可能不足 | 重训前核对样本数；不足时评估 `drop_middle=False`（soft 0.5）或放宽 min_cross_section |
| **gate 语义失配** | m5_positive_ratio 按 soup 标定，return_rank 下误判 pass/fail | §4.4 强制复核，新标定须留档 |
| **backfill 增量与训练口径不一致** | 追补样本（backfill）与新链 label 不同源 | §2.2 registry 分支 + 一致性测试覆盖 backfill 入口 |
| **回滚** | basis 切回 soup + 重训 | champion 回退到切换前 artifact；注意 registry 身份与文件脱钩的既有风险，回滚以 registry 为准 |

## 6. 实施顺序（建议）

1. 提炼公共函数 `apply_return_rank_labels_by_day` + pit_dataset 改为调用它（**纯重构，测试须全绿**）
2. §2.1 生产链分支 + §2.2 registry 分支
3. §3 测试集（含两入口一致性）
4. 本地 `basis=return_rank` 小样本演练（不上生产）
5. 合入 + 部署 + 配置切换
6. 周末重训 + §4 checklist + §4.6 验收
