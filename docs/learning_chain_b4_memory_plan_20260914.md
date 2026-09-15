# 学习链整改 B4：全池训练内存路径分析与改造方案（2026-09-14）

> 材料性质：**本地代码分析 + 改造方案 + 实施记录**。
> 目标：全池（全市场）训练在 **4 GiB 内完成**（B4 验收）。
> 依据：NAS 实测证据（9/13 挑战者 manifest、40k 上限生效）+ 本地代码走查。
> 声明：**峰值 RSS 尚未实测**（§2 的数量级为按代码路径的推算，非测量值）；实测在 NAS 窗口内按 §4 协议执行。
>
> **实施状态（2026-09-14）**：
> - **S1**（引用级投影 `SnapshotRef` / `list_snapshot_refs`，不解析 `feature_vector_json`）、
>   **S2**（选池改引用级读取 + 符号过滤下推 SQL）已实现并测试；
> - **S3 实质达成**：manifest 构建段不再做窗口级全量重读——选池产出的 `snapshot_ids`
>   直接透传 `create_manifest(snapshot_ids=…)`，该段读取已被行数上限约束（≤ cap）。
>   新增行为护栏测试断言训练全程每一次 `list_snapshots` 调用都带**显式 ids**
>   （即不存在未裁剪的窗口级特征载荷读取）；
> - **S4 前半**达成（行上限裁剪改协议约束、特征载荷只在最终样本上取一次）；
>   S4 的「分块消费」与 **S5**（流式接口）**未做**；
> - **NAS 4 GiB 实测未做**——并且**应先实测再决定是否做 S4 后半 / S5**：剩余峰值落在
>   哪一段（装配段快照字典 vs 行载荷循环）需要 `memory.peak` 数据定位，凭空优化会做错方向。
>   §4 的验收协议即为该测量而设计。
> - 另注：集成后发现并修复 B1×B2 交互缺陷（B1 让 trainer 产出输出语义字段 → B2 输出
>   健康门从「legacy 放行」变为激活，误伤 15 个走训练路径的测试），修复见
>   `output_health.MIN_SCORED_FOR_CONSTANT_BLOCK` 与 `TestConstantOutputSampleSizeFloor`。

---

## §1 现状与实测证据

| 事实 | 值 | 来源 |
| --- | --- | --- |
| 样本上限 | `SA__TRAINING__BOOTSTRAP_DATASET_MAX_ROWS=40000`；每票上限 120 | NAS `.env:158-159` |
| 9/13 manifest | `included_snapshot_count=included_outcome_count=40000`（**上限命中**），`sample_selection_rule=…lookback_days=240;symbols=5191;snapshots=40000` | `dataset_manifest_v2_8b250aa25009` |
| 快照表规模 | 历史上限 ~7.1 万条快照，每条含 ~222 维特征（内存记录：71k × 222 → OOM） | 项目历史记录 |
| 已有 OOM 记录 | 容器 2G 触发 OOM（`code -9`，worker 峰值 2.2G） | `scheduler-critical` 8/28–9/7 事故 |

## §2 内存路径（三段物化 + 跨段存活）

```
service._try_train_models_from_learning_protocol      (service.py:9320-9530)
  ① list_snapshots(label_policy_id, 240d)             (service.py:9353)   ← 物化【全部在窗快照】，含 features
  ② list_outcomes(snapshot_ids=[全部 id])             (service.py:9376)   ← 巨型 IN 列表（7 万占位符 + 7 万参数）
  ③ exact_schema_groups / candidate_blueprints①       (service.py:9440-9466) ← 同一批对象按 registry 记录再挂 N 份引用
  ④ _apply_learning_protocol_row_caps → capped(≤40k)  (service.py:9477)
  ⑤ selected_snapshots = best_candidate["snapshots"]  (service.py:9516)   ← 【跨段存活】直到本函数返回
      ↓ 把 snapshot_ids 交给 trainer（②③④ 的本地名仍被引用 ⇒ 对象图不释放）
trainer.train_on_sample_store                          (trainer.py:164)
  └─ DatasetManifestBuilder.create_manifest            (dataset_manifest.py:51)
       ⑥ list_snapshots(snapshot_ids=…)                (dataset_manifest.py:94)  ← 物化【第二份】同一批对象
       ⑦ list_outcomes(snapshot_ids=…)                 (dataset_manifest.py:101) ← 第二个巨型 IN 列表
       ⑧ _select_included_pairs → included_pairs       （(snapshot, outcome) 对列表）
       ⑨ _deduplicate_by_trading_day → deduped_pairs   （又一份对列表）
       ⑩ snapshots={sid: snapshot for …}               (dataset_manifest.py:120) ← 【第三份引用字典】供质量报告
trainer.train_on_dataset_manifest                      (trainer.py:254)
       ⑪ list_snapshots(snapshot_ids=manifest_items)   (trainer.py:254)  ← 【第三段物化】≤40k 条
       ⑫ 逐行 Python 循环组装 row_index/row_payloads/split_labels/weights/returns (trainer.py:288-300)
```

**根因（按代码结构判定，非推测）**：
1. **特征载荷物化过早且过宽**：选池、去重、切分、质量报告都只需要 `snapshot_id / symbol / decision_time / feature_schema_id / feature_schema_hash` + outcome 的成熟度与收益，但 `list_snapshots` 一次性返回**含全部特征**的 Pydantic 对象（并解析特征 JSON）。
2. **跨段不释放**：`exact_schema_groups`、`candidate_blueprints`、`selected_snapshots` 作为局部名在函数结束前一直存活，而 trainer 的 manifest 构建与装配发生在**同一调用栈内**→ 第二、三段物化是在第一段之上**叠加**，而非替换。
3. **巨型 IN 列表**：`list_outcomes(snapshot_ids=[…7 万…])` 生成 7 万个 `?` 的 SQL 字符串与同长参数列表，且重复两次。
4. **同批对象挂多份引用**：蓝图为每条 registry 记录各挂一份快照列表（14 行记录 → 最多 14 份指针列表）。

峰值量级推算（**待实测**）：单条快照 ~222 个 float 的 dict + Pydantic 开销，按 ~20 KiB/条估算，
7 万条 ≈ 1.4 GB；40k 条 ≈ 0.8 GB。叠加 ⑤③ 存活 + ⑥ 第二份 + ⑪ 第三份 → 3 GiB 量级与 §1 的 OOM 记录一致。

## §3 改造方案（五步，渐进可回滚）

**S1｜新增"引用级"读取（不含特征载荷）** — `learning/sample_store.py`
```python
@dataclass(frozen=True, slots=True)
class SnapshotRef:              # 选池/去重/切分/质量报告只消费这些字段
    snapshot_id: str; symbol: str; decision_time: datetime
    feature_schema_id: str; feature_schema_hash: str; label_policy_id: str

def list_snapshot_refs(*, label_policy_id=None, feature_schema_id=None,
                       time_window_start=None, time_window_end=None,
                       symbols=None) -> list[SnapshotRef]:
    """SQL 投影：不选 features/context JSON 列，不构造 Pydantic 快照。"""
```
同表加 `list_outcome_refs(*, snapshot_ids=None, maturity_statuses=None,
symbols=None)`（投影 `snapshot_id/maturity_status/label_mature_time/realized_return`），
成熟度过滤**下推到 SQL**（现在是 Python 侧 `outcome.maturity_status not in mature_statuses`）。

**S2｜过滤与上限下推到 SQL** — `service.py`
- 符号过滤下推（现在是 Python 侧按 `snapshot.symbol` 过滤，须先取全量）；
- `exact_schema_groups` 分组改为在 `SnapshotRef` 上做（内存为纯值对象，无特征）；
- `_apply_learning_protocol_row_caps` 改为在 ref 上做（每票 120 / 全局 40000）；
- **先算出最终 ≤40k 的 snapshot_id 列表，再去取特征载荷**。

**S3｜打通"已选 id"直通 manifest** — `dataset_manifest.py` + `trainer.py`
- `create_manifest(snapshot_ids=<已选>)` 已支持（trainer.py:184 已传），但 builder 内部**仍按 window 重新全量拉取**；
  改为：传入 `snapshot_ids` 时**只按 id 取**（分块），不再叠加 window 级全量；
- `_select_included_pairs` / `_deduplicate_by_trading_day` 改在 ref 上做；
- `build_manifest_quality_report` 的 `snapshots={…}` 由 ref 字典替代（其消费字段须逐一核对，缺字段从 ref 补齐）。

**S4｜分块读取与显式释放**
- `list_snapshots(snapshot_ids=[…])` 内部分块（建议 5k/块）+ 临时表 JOIN 取代巨型 IN 列表；
- 服务段在把 id 交给 trainer **之前** `del exact_schema_groups, candidate_blueprints, capped_snapshots`（只留 id 列表）；
- `create_manifest` 返回前释放 `included_pairs/deduped_pairs`；
- 装配段（trainer.py:288+）改为**按块增量**构造 float 矩阵（`np.empty((n, d))` 预分配 + 填充），避免 `row_payloads: list[dict]` 中间态。

**S5｜可选流式接口**
`iter_snapshots(batch_size) -> Iterator[list[SignalSnapshot]]`，供影子/报告类调用方（`shadow_dataset_builder`、`execution_aware_report`）按需切换，不影响其语义。

## §4 验证与验收

**本地（每步）**
- `tests/test_learning_dataset_manifest.py` / `tests/test_learning_metrics_separation_b1.py` / `tests/test_model_calibration.py` 全绿（含 A1 的 purge 回归）；
- 新增等价性测试：同一 fixture 下 `SnapshotRef` 路径与旧路径产出**同一** manifest 成员集合、同一 split 划分、同一 label 值（逐行断言）；
- 新增内存行为测试：构造 20k 条带 222 特征的样本，断言 `list_snapshot_refs` 不解析 features（以 `SnapshotRef` 无 features 字段 + 调用计数/对象数断言）；
- `python scripts/run_quality_gate.py --stage full --fail-on-error`；ruff/mypy 与脏基线对比无新增。

**NAS（4 GiB 验收协议，可复现）**
1. 训练容器内**前置采样**：`cat /sys/fs/cgroup/memory.peak`、`cat /sys/fs/cgroup/memory.events`；
2. 以受控手工路径触发全池训练（**训练开关由 compose overlay 钉死为 false**，须走既有受控手工路径，不改 `.env`、不改 `docker-compose.memlimit.yml`）；
3. 运行期间记录 `docker stats --no-stream` 峰值（≥5 次采样）与容器 `memory.peak` 前后差值；
4. **后置判定**：`memory.peak ≤ 4 GiB` 且 `memory.events` 的 `oom_kill` 增量为 0，且训练产出物（manifest + 指标）与本地等价性测试一致；
5. 记录 `ru_maxrss`（进程内峰值）作为交叉核对。

## §5 风险与回滚

| 风险 | 缓解 |
| --- | --- |
| ref 路径与全量路径产出不一致（漏字段） | 等价性测试逐行断言（成员集合/split/label/权重）；不一致即 fail |
| 质量报告依赖尚未核对的快照字段 | 逐字段核对 `build_manifest_quality_report` 消费面，缺字段从 ref 补齐并在 PR 中列出 |
| 分块后 SQL 语义变化（IN 顺序、去重） | 分块结果按 `decision_time, snapshot_id` 全局重排；加确定性测试 |
| 训练结果漂移 | 同一 manifest id 下前后两次训练的指标须**完全一致**（确定性种子）；否则回滚 |

**回滚**：改动集中在 4 个模块且均以"新增 API + 调用方切换"形式落地，回滚 = 还原调用方到 `list_snapshots`；不涉及 schema/registry/DB 迁移，无数据面回滚需求。

## §6 未决 / 需拍板

1. **NAS 窗口**：全池训练时长按 9/13 记录约 25 分钟（扫描）+ 训练约 7 分钟，建议取**非交易日白天**（下一次为 9/19 周六）或交易日 **21:45 之后**（避让 09:25–15:00 radar 槽位与 19:45 updater）。
2. **是否顺带处理 C4 的行数上限（40000）**：本方案不改上限语义，只改内存路径；上限与 A1 purge 的口径关系归 C4。
3. **`_SNAPSHOT_COLUMNS` 投影裁剪范围**：需确认 `enrich_snapshot_contexts` 等写入方是否依赖读取侧全列。
