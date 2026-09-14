# 学习链 C4：行数 cap 策略改按日分层（2026-09-14）

> 依据：`learning_protocol.duckdb` 只读查询 + 两次 manifest 逐分割逐日 items 对比 + NAS `.env` 实测。
> 决定：用户 2026-09-14 选定「cap 改按日分层」。

---

## 1. 症状

9/14 在 NAS（镜像 `e00371d`）跑 `train-models --full-market --lookback-days 240`，
训练**根本无法开始**：

```
learning_protocol_failed:ValueError
dataset manifest blocked by manifest quality flags ['test_window_too_narrow']
return_rank_basis_requires_cross_section: bars fallback disabled   ← 9/12 加的 fail-closed 正确拒绝兜底
```

manifest 记录：`test_split_window_days=16`，低于 `min_test_split_window_days=20`（config 默认，NAS 未覆盖）。

## 2. 根因（不是 A1 的错）

| 观测 | 值 |
|---|---|
| NAS `.env` `SA__TRAINING__BOOTSTRAP_DATASET_MAX_ROWS` | 40000（代码默认 500000） |
| 每日候选截面 | 约 334 只 |
| manifest `decision_days_total` | 120 ≈ 40000 / 334 |
| A1 purge | 26 个决策日 / 8,694 行 |
| 9/14 manifest 的 test 段 | 12 个决策日 = 16 个自然日 |
| 9/13（A1 之前）manifest 的 test 段 | 日历跨 23 天，看似达标 |

**关键对照**：9/13 那份 manifest 的 test 段里，8/6~8/12 每天**只有 1~2 行**，而且这些日期
同时出现在 calibration 段——即 calibration/test **同日重叠**的泄漏。那 5 天正是把窗口
从 16 天虚撑到 23 天的东西。A1 没缩小窗口，它**暴露了真实测试窗只有 16 天**。

叠加 keep-last-N 的语义（延长 lookback 是**滑动窗口**而非扩展历史）：
决策日数被钉死在 `max_rows / 每日截面 ≈ 120`，purge 后 test 永远 < 20 天
→ 学习协议在任何 lookback 下都产不出可训练 manifest。

## 3. 修法：按日分层 + 整日滑窗

新增 `training.bootstrap_row_cap_strategy`：

- `keep_last`（**默认**）：历史语义，逐行 keep-last-N，本改动完全惰性。
- `per_day`：每个决策日只留 `per_day` 条（按 snapshot_id 排序取前 N，等价于对当日截面
  做确定性随机抽样，不与标签系统相关），全局 `bootstrap_dataset_max_rows` 退化为
  **整日滑窗**（从最新决策日往回累加，放不下的整日丢弃，另留一条「连最新整日都放不下」
  时回退按行截断的兜底）。

每日条数 `bootstrap_max_rows_per_day`：>0 用配置值；**0 = 自动**，用**最小裁剪**取最大的
每日上限使 `Σ min(n_d, cap) ≤ max_rows`，预算几乎全部花在「保住多少天」上。

效果（NAS 实测候选集：256 个决策日 / 156,520 行，其中 229 个稠密日均约 680 行、
27 个稀疏日合计 873 行）：

| | 每日上限 | 保留决策日 | 总样本 |
|---|---|---|---|
| 旧 keep-last-N | — | ≈ 120 | 40,000 |
| 均摊（**已废弃**，见 §3.1） | 177 | ≈ 150 | 17,423 |
| 最小裁剪 | ≈ 156 | **256** | ≤ 40,000 |

总样本数不超预算 → **B4 的 4 GiB 内存前提不被破坏**（载荷只对最终 ≤cap 的样本物化，
见 `docs/learning_chain_b4_memory_plan_20260914.md`）。

### 3.1 第一版自动档被实测证伪（保留记录）

第一版按「`max_rows // 可用天数`」均摊。NAS 实跑（lookback 2500、`.env` 已确认吃到
`per_day`）结果：

- 保留样本 31,306 → **17,423**（-44%），test 每决策日从约 334 行掉到 **177 行**；
- `test_split_window_days` 只从 16 天挪到 **17 天**，仍 `test_window_too_narrow`。

原因：均摊把稀疏老日期也算进分母，于是上限被稀释成一个偏小的值，反过来把稠密日
砍掉一半，却一天也没多保住。**教训：cap 口径的自动值必须建立在「逐日条数分布」上，
而不是「天数」这一个标量上。**

同一次实跑还量出真正的约束（此前把 cap 当主因也是错的）：

| 量 | 值 |
|---|---|
| 样本库总量 | 162,620 快照 / 273 个决策日（2025-09-23 ~ 2026-09-14，约 1 年） |
| 成熟候选集 | **256 个决策日 / 156,520 行** |
| manifest 决策日池（purge 后） | 130 天（purge 前 150） |
| `test_ratio` | 0.1 → test = 13 个决策日 = 17 个自然日 |
| A1 purge | 两处边界各 10 天 |

即：**测试窗宽度 = `test_ratio × 决策日池` 的日历跨度**，purge 与窗口下限都不动时，
cap 只能通过「决策日池从 122 涨到 150/256」间接影响它。cap 改对之后决策日池可涨到
256 天，test 名义约 26 天；若仍差池，下一步该动的是 `test_ratio`（或加深样本库历史），
**不是**继续调 cap。

### 一致性约束

日界统一走 `dataset_manifest.decision_date_shanghai`（= 决策时刻 +8h 取日期），
A1 的 purge/split 与 cap 的分层必须共用这一处定义。`SnapshotRef.decision_time` 是
数据库里的 **UTC ISO 字符串**（B4 为省解析成本刻意保留字符串形态），
`SignalSnapshot.decision_time` 是 datetime，故 cap 侧经 `_decision_day_of` 用
`sample_store` 的同一解析器规范化后再取日——**这是 mypy 基线对比查出来的真实运行期
TypeError（字符串 + timedelta），本地按 datetime 造样本时看不出来**。

### 3.2 结论（2026-09-15 00:34 实测）：cap **不是**这个门的杠杆

最小裁剪版部署后（`53565a9`，`.env` 确认 `per_day`）再跑一次，manifest 结果为：

| 量 | 值 |
|---|---|
| `included_snapshot_count` | **18,599**（budget 40,000 → 两版 cap 都没生效） |
| `decision_days_total` | 130（purge 前 150；两处边界各 purge 10 天） |
| split | train 107 天 / calibration 13 天 / test **13 天** |
| test 内容 | 8/12~8/28，13 个决策日 × 189 行 = 2,457 |
| `test_split_window_days` | **17**（仍 < 20） |

即：**候选集只有 18,599 行，远低于 40,000 预算，`per_day` 与 `keep_last` 都不会裁剪**，
所以 cap 策略对窗口没有任何影响。窗口的算式是纯比例的：

```
test 决策日数 = test_ratio × decision_days_total = 0.1 × 130 = 13
test 日历窗    = 17 天（8/12 ~ 8/28）  <  20
```

**要过 20 天下限需要 test 决策日 ≥ 16 天，即 `test_ratio ≥ 0.123`；留余量取 0.2
则 test 26 个决策日 ≈ 34 个自然日。**

决策日池只有 130 天的原因另有一条：`bootstrap_per_symbol_rows_cap=120` 配上
「每只每日多次捕获（实测约 7.5 次/只/日）」，等于把每只票的历史钉在约 16 个决策日
（120 / 7.5），池子靠不同票的捕获日期错位才凑到 130 天。这正对应此前记录的
「日内重复捕获必须按逻辑键去重」——**要去重之后 per_symbol cap 才不再兼作历史深度上限。**

因此本改动的定位需要更正：它**不能**解开 `test_window_too_narrow`，价值在于
(a) 取消 keep-last-N 的「延长 lookback 只滑动窗口」隐患，
(b) 让 cap 一旦成为绑约束时把预算花在「天数」而非「同一天的重复行」上。
真正要动的是 `test_ratio`（直接、算式确定）与捕获去重（根因）。

| 文件 | 改动 |
|---|---|
| `src/stock_analyzer/config.py` | 新增 `bootstrap_row_cap_strategy`/`bootstrap_max_rows_per_day` |
| `config/default.yaml` | 同上，默认 `keep_last` / `0` |
| `src/stock_analyzer/learning/dataset_manifest.py` | `decision_date_shanghai` 提升为公共名（保留私有别名） |
| `src/stock_analyzer/runtime/service.py` | `_resolve_row_cap_strategy`（未知取值 fail-closed）、`_resolve_per_day_rows_cap`（自动摊平）、`_decision_day_of`、`_stratify_rows_by_decision_day`、`_slide_rows_by_whole_days`；cap 调用与训练结果 payload 增加 `rows_per_day_cap`/`row_cap_strategy`/`decision_days_used` |
| `tests/test_learning_row_cap_stratification.py` | 12 例：自动摊平、旧语义不变、整日不腰斩、确定性、单票上限优先、超大单日兜底、上海日界、ref 字符串形态、策略名 fail-closed |

**没有放宽任何门禁**：`min_test_split_window_days` 仍是 20，`intraday_fresh_ratio_min`
与 cap 数值都没为了让某次运行通过而调整；本改动只是把同样 40,000 的预算**摊到更长的时间轴上**。

## 5. NAS 落地与验收（实测记录）

```bash
# .env（已加）
SA__TRAINING__BOOTSTRAP_ROW_CAP_STRATEGY=per_day
#    MAX_ROWS 保持 40000、PER_SYMBOL 保持 120
bash scripts/nas_deploy_update.sh --branch feat/learning-chain-accept-0914
docker exec -d stock-analyzer-api sh -c 'stock-analyzer train-models --full-market --lookback-days 2500 > /tmp/cap_train.json 2>&1; echo $? > /tmp/cap_train.rc'
```

实测结果（两次，均在 `fc6e019`/`53565a9` 上）：

| | 均摊版 | 最小裁剪版 |
|---|---|---|
| 保留样本 | 17,423 | 18,599 |
| test 每日条数 | 177 | 189 |
| `test_split_window_days` | 17 | 17 |
| 结论 | 仍 `test_window_too_narrow` | 仍 `test_window_too_narrow` |

两次都没有触发 cap（候选集 < 40,000 预算），故**两版都未改变窗口**——
见 §3.2，cap 不是这个门的杠杆。

内存侧的有效观测：训练在容器内峰值约 **1.98 GiB / 4 GiB**（训练前容器 `memory.peak`
522 MiB，`memory.events` 的 `oom_kill` 为 0）。但两次 run 都在协议门中止，
**仍不含模型拟合段**，所以 B4 的「全池训练在 4 GiB 内完成」依旧只能算有条件成立。

**尚未做的关键一次实测**：把 `SA__TRAINING__TEST_RATIO` 由 0.1 提到 0.2（test 26 个
决策日 ≈ 34 自然日）后重跑，预期 `manifest_quality_flags_json=[]`。
这一步会改变 train/test 配比，属**新的口径决策**，未擅自执行。

## 6. 已知残留

- `list_outcomes(snapshot_ids=[窗口内全部 ref])` 仍按窗口全量拉取：lookback 2500 下 ref 约
  16 万条，outcome 对象常驻内存约百 MB 量级。本次实测若内存吃紧，下一步就是把它改成
  「先按成熟度过滤再取 outcome」。
- 自动档的每日条数由可用日数决定，若样本库某天突然只有极少决策日，每日条数会被抬高、
  时间跨度反而变短——`rows_per_day_cap`/`decision_days_used` 已随训练结果落盘，可事后判定。
- `calibration_ratio`/`test_ratio` 仍是 0.1。测试窗宽度最终取决于
  `test_ratio × 决策日池`：实测 130 天池 × 0.1 = 13 个决策日 = 17 个自然日。
  cap 改对后池可到 256 天（test 名义约 26 天 ≈ 34 自然日）→ 应能过 20 天下限；
  **若实测仍不过，下一步动 `test_ratio` 或加深样本库历史（两年 PIT 面板
  2,405,272 行 / 482 交易日），不是继续调 cap，也不是下调窗口下限。**
