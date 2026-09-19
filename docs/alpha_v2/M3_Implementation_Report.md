# Alpha V2 M3 Implementation Report（Production Shadow Validation & Clean OOS）

> 批次：`M3`（冻结 → 生产 Shadow → 真实未来检验的前置工程）
> 基线：`ALPHA_V2_M1_M2_FROZEN_COMMIT = 55c7592fe88ac3b3ab5b233df422b0f528141074`
> 授权状态：`PRODUCTION_DEPLOYMENT_AUTHORIZED = false`、`GIT_PUSH_AUTHORIZED = false`
> 生成日期：2026-09-18（本机）

---

## 1. M3 总锁定（什么是"验证的纪律"）

```text
1. 一个时刻只一个 open epoch；数据按 epoch 分桶（不同 epoch 分开报告）
2. 冻结清单在 shadow 之前写；任何一处"模型/代码/配置/基准"漂移 => 关旧 epoch、开新 epoch
3. T 日快照只在 T 日写；同键改写必须逐字段一致（否则 ShadowTamperError）
4. 快照缺失 => missing_prediction_day（被 KPI 单列、从 clean OOS 剔除），不得事后补漂亮结果
5. outcome 只按"真实 bar 已溢出 horizon"成熟；信号当天写 0 行；改写已成熟字段 => OutcomeRestatementError
6. 没有 OOS isotonic 校准器时，方向分只写"方向分（未校准）"，绝不写"上涨概率"
7. alpha_verified / production_promotion 在样本门之内恒为 False / LOCKED
```

## 2. 交付清单（新增）

```text
src/stock_analyzer/alpha_v2/validation/
    __init__.py                M3 阶段纪律总纲
    freeze.py                  Validation Freeze Manifest（字段/哈希/完整性/门禁）
    epoch.py                   validation epoch 注册表（单开/关闭/身份对账）
    frozen_model.py            冻结 Shadow 模型工件（训练/持久化/加载/推理）
    shadow_capture.py          T 日预测冻结 + 防篡改 + missing day
    outcome_maturation.py      每日成熟任务（S11 契约 + 冻结基准 + 重述防护）
    validation_kpis.py         M3 KPI 汇总（Top1/3/5、Rank IC、样本门）
    feature_diagnosis.py       DF-M2-003 五分类诊断
    runtime_identity.py        git/config/价格口径 身份采集
scripts/
    alpha_v2_validation_freeze.py      冻结清单 + epoch 开关
    alpha_v2_shadow_model_freeze.py    冻结模型训练
    alpha_v2_shadow_capture.py         每日快照采集
    alpha_v2_shadow_mature.py          每日成熟
    alpha_v2_validation_report.py      KPI 日报/周报
    alpha_v2_feature_diagnosis.py      DF-M2-003 跑批
tests/
    test_alpha_v2_m3_freeze.py               （7 例）
    test_alpha_v2_m3_epoch.py                （7 例）
    test_alpha_v2_m3_shadow_capture.py       （8 例；含篡改拒绝/closed 拒绝）
    test_alpha_v2_m3_frozen_model.py         （8 例；含确定性 + 哈希对不上拒绝）
    test_alpha_v2_m3_outcome_maturation.py   （7 例；含信号当天 0 行/重述拒绝/closed epoch 成熟拒绝）
    test_alpha_v2_m3_validation_kpis.py      （5 例；含 clean 治理/gates + alpha_verified=False 护栏 + 严格 JSON）
    test_alpha_v2_m3_benchmark_block_bug.py  （4 例 对抗回归）
    test_alpha_v2_m3_enforcement.py          （26 例；M3 修复轮 F1-F7 全部对抗证据）
补充夹具：_alpha_v2_m3_fixtures.py（写清单 → 开 epoch 的标准链）
```

## 3. 关键设计取舍

### 3.1 为什么 M3 必须有自己的"冻结模型工件"，而不是复用在服 legacy 模型

在服 `model_v1.json` 是 Legacy 产物，它的 `alpha_rank_score / expected_*` 语义
在 V2 的定义下**不存在**；若用它"假装 V2"，Rank IC 等就成了另一模型的证据。
M3 给 V2 单独的冻结工件（`model/<model_id>/`），字段级哈希；Shadow 每天只加载它。

### 3.2 与 M2 的关系（接口复用不重写）

- 标签/outcome 计算、价格契约、基准风格、指标模块 → 全套 S11/S12/S14 直接 import；
- 主动改代码只有一处：`benchmarks._style_control_group` 的分块越界修复（§5）；
- M2 的"能力态"模块（shadow_dual_run 等）在 M3 未动：M2 的意图在 M3 是"被冻结的口径"，
  不是再开发的代码库。

### 3.3 missing day 与补写窗口

调度失败 / 上游缺数据的当天只在 missing ledger 记一行（含原因 + recorded_at），
KPI 统计的是"有 shadow 的日期"，缺失日**不进入 clean OOS**，更不会以旧/新模型重算。
语义上它和 T 日快照一样：只允许 "not_available"，不允许 "补"。

### 3.4 统计单位与样本门

M3 §10 允许"逐日矢量"的原因是：IC 的独立性检验在 metrics.ic_summary 里以
date 为块做了 moving-block bootstrap；KPI 汇总不新造一个平行口径。

## 4. 本轮发现并修复的**真实缺陷**（M3 §19 语境）

**DF-M3-B1**：`benchmarks.py::_style_control_group` 在「同 decision_date + 同板块」组
规模大于 block（512）时，自配对置 `inf` 用了**组内全局行号**（`dist[row_index, row_index]`），
而 `dist` 的第 0 维只有 chunk_rows——越界 IndexError。M2 测试全用 ≤512 的组，
所以整条生产全市场路径此前从未在真实数据上被走过。修复为 `dist[offset, row_index]`
并补 4 条对抗回归（含 block 不变性、暴力法对照、自身恒非自身）。

**DF-M3-B2**：`frozen_model.fit` 首个版本的特征列推导把 `mae_le_5pct_{10,15}d` 这类
"非 SHORT horizon 的目标派生列"也当特征（因为它们不匹配 S14 的 outcome 泄露前缀）。
已修：`_NON_FEATURE_PREFIXES` 显式涵盖这类派生列，并在测试里加了直接对抗样本。

## 5. 测试与验证（含修复轮增量；数字以 junit 实测为准）

```text
M3 定向全部测试 : 10 文件 / 112 collected / 112 passed / 0 failed / 0 skipped   ← R3 最终口径
    freeze          7  epoch          7  shadow_capture  8  frozen_model  8
    outcome_maturation 7  validation_kpis 5  benchmark_block_bug 4
    feature_diagnosis 4  enforcement   26  r3_final_blockers 36
                                             ↑ enforcement 为 F1-F7 修复轮新增；
                                               r3_final_blockers 为 R3 修复轮新增
ruff                 : M3 改动文件 All checks passed（仓库 format 基线不净，M3 不扩大差距）
全量回归             : 3610 collected / 3608 passed / 0 failed / 0 errors / 2 skipped（junit 实测）
                       （2 skipped = NAS bash 语法检查，仅 Linux CI 执行；
                        历史口径 3546 → 3572 → 3610，只有 3610 是当前数）
```

> 历史口径（保留，不覆盖）：F1–F7 修复轮时记为 9 文件 / 76 passed，全量 3574 collected
> ——那是 R2 时代快照，已被 R3 的 10 文件 / 112 与全量 3610 取代。

>（回溯注：原版这个表把 shadow_capture 记 9、validation_kpis 记 5（实际 8/4），
> 合计 50；读旧档案请按本轮真实计数。2026-09-19 修复轮已把计数向 junit 校准。）

## 6. 全量回归

`python -m pytest -n 4 --dist loadfile`（在完成本批全部改动之后运行）：
见 `artifacts/alpha_v2/audit/m3_batch_regression.json`（由收尾脚本生成）。

## 7. Deferred Findings 处置总表（M3）

| ID | 状态 | 去向 |
|---|---|---|
| DF-M2-001 研究代理 vs 生产真实漏斗 | OPEN（不变） | 部署后由 capture 收到的生产 funnel 真实成员关闭 |
| DF-M2-002 Windows peak RSS 不可测 | 不变（NAS 侧部署后复核） | NAS 部署 |
| DF-M2-003 101 列近常数 | **本批处理**：五分类诊断落盘（见 §9 of Readiness Report） | 数据侧治理（不在本批改） |
| DF-M2-004 alpha_target vs return_rank 语义 | OPEN | M4 调研 |
| DF-M2-005 S18 Deep50 是研究代理 | OPEN（不变） | 部署后由生产 funnel 关闭 |
| DF-S06-001 strict replay 证据稀缺 | OPEN | 不阻塞 Shadow；S19 已提供 pit_research 口径 |
| DF-S06-002 asof 闸门未接 | OPEN | 同 DF-M2-002 |
| DF-S07-001 生产执行价核验 | **CLOSED**（生产 = raw；见 Readiness §3） | — |
| DF-S07-002 corporate action 治理 | PARTIAL（S11 有 suspected 标记） | 后续治理 |
| DF-S08-001 / S08-002 生产接线 | OPEN | 部署期灰度（不阻塞 Shadow） |
| DF-S09-001 生产 label basis 未登记 | **不变**（语义仍 unknown；V2 用自有 label 口径） | 后续治理 |
| DF-S09-002 文案层 | CLOSED（S21 已落地） | — |
| DF-S10-001/002 生产接线 | OPEN | 部署期（本批已把接线器材做完） |

## 8. 已知非对齐口径（如实标注，不算 deviation）

- **研究窗口 vs 生产**：M2 的 60D 门是"本地开发窗口"；M3 的 clean OOS 判定**严格按生产新积累天数**走（两口径已在冻结清单里显式记录）。
- **quality_pool_source**：本机排演 = `research_proxy:alpha_v2_quality_v1`；生产部署后应切换为 `production_selection_engine`。快照行里原样写，不做文字游戏。

---

## 9. Blocking Fix Round（F1–F7；CRW-001 后将接受独立复核）

本次修复对应 Codex 独立验收（M3 首轮 = FAIL）的全部 8 项 Blocking。

### 9.1 F1（B1）T 日写入窗口 — `validation/shadow_capture.py`

- `write_shadow_snapshot(..., capture_date=?, allow_backfill=False, backfill_reason="")`；
  默认同一交易日规则：``signal_date != 写入日`` 抛 `ShadowLateWriteError`；
  ``signal_date < validation_start_date`` 一律拒绝；
- 显式 backfill 才能写，行落 ``backfilled=true`` + ``clean_oos_eligible=false`` +
  ``backfill_reason``；missing 台账同日冲突（双向）``ShadowMissingDayConflictError``；
- 行级新增核心字段并被防篡改保护：``backfilled`` / ``backfill_reason`` /
  ``clean_oos_eligible`` / ``quality_pool_source`` / ``deep_rank_pct``（``PREDICTION_CRITICAL_FIELDS``）。

### 9.2 F2（B2）Epoch 身份可信链 — `validation/epoch.py`、写入三件套

- ``FROZEN_IDENTITY_KEYS`` 扩到 8 键（增加 ``execution_price_mode``）；
- ``epoch_identity_matches`` 语义收紧为 **缺失即违例**（原"两边都有才比" fail-open 移除）；
- 新增 ``require_epoch_identity_match``（open + manifest 锚定 + 完整性 + 身份逐项），
  被 `write_shadow_snapshot` / `record_missing_prediction_day` / `mature_epoch_outcomes`
  从上到下调用；
- 冻结清单会话冒名（把磁盘 manifest 换成新 hash/同 hash 换内容）在写入侧全部拒绝。

### 9.3 F3（B3）closed epoch 不再可从成熟侧进入 — `validation/outcome_maturation.py`

- ``mature_epoch_outcomes`` 入口先进 ``require_epoch_identity_match``；
- 「影子外行」在成熟合并处 ``ShadowSnapshotIntegrityError`` 拒写；
- ``capture``/``mature`` CLI 在面板加载前先做闸，失败秒出而非跑完重活才报错。

### 9.4 F4（B5/B4)frozen model 与 freeze CLI 硬门 — `scripts/alpha_v2_validation_freeze.py`、`validation/freeze_precheck.py`

- 生产模式逐项硬门 + 显式退出码（非 raw=exit 4；脏树/构建身份=exit 5；schema/窗口=exit 6）；
- ``--rehearsal`` 才能跑非 raw / 脏树本机流动，清单如实写 ``validation_mode=rehearsal``；
- 特征 schema 不再能空：默认从冻结模型工件派生（两源互斥校验），并保证
  ``freeze.feature_schema_hash == 模型工件的 feature_schema_hash``（统一用列集合哈希）；
- 强校验 ``--start-date >= today``（生产模式不允许把 epoch 写回历史）。

### 9.5 F5（B8）脏树 / 代码身份 — `validation/runtime_identity.py` + `freeze_precheck`

- ``git_worktree_dirt()``：``None`` = git 不可得（生产 fail-closed）；真正干净 = ``[]``；
  白名单仅豁免部署期身份文件（``.build_commit`` / ``build_manifest.json``）；
- ``build_identity_block()`` 汇总 git HEAD / 脏清单 / ``.build_commit`` / ``build_manifest``；
  生产模式要求三者一文，缺即拒；
- 发现并修复的自我回流缺陷：旧实现把 ``git status --porcelain`` 的空输出当成失败
  （`"" or "unknown"`），干净树会被误报为 1 项"脏"——测试与本节生产演练双证据。

### 9.6 F6（B6）Clean OOS 两层资格 — `validation/validation_kpis.py`

- 行级：``clean_oos_row_eligible``（backfilled 非真 + data_health=ok + execution=raw +
  production mode）；
- 日级：``_day_governance`` 逐日期保持在 missing / backfilled / data_health / identity /
  execution 五项之上，不通过 = ``eligible=False`` 且原因一字不落；
- 治理块新增：captured_days / clean_oos_days / missing_prediction_days /
  backfilled_days / excluded_data_health_days / identity_invalid_days /
  execution_or_mode_invalid_days / coverage_rate；
- 样本门与主证据块（hit_rate / returns / excess / IC / 分位 / recall / 下行 / baseline
  配对）只走 clean 集；执行统计同时报"全快照"与"clean_only"两个口径。

### 9.7 F7（B7/N1-N3）证据与口径

- KPI 报告写前做严格 JSON 自检与清洗（NaN/±Inf 统一到 not_available）；
  产物可被任意严格解析器读开；
- ``deep_rank`` 由"int(percentile) 恒 0/1"修正为深池名次 1..N，原百分位移入
  ``deep_rank_pct``（共享 `deep50_position_records`，CLI 与测试同口径）；
- ``quality_pool_source`` 提升为快照行核心字段（不再落 ``extra`` 里消失；
  成熟期读顶层字段，生产传 "production_selection_engine" 不再被错回退成 "proxy"）;
- 文档计数修正（本报告 §5 / Readiness §1 / PROGRESS §15.6）与统一为 9 个测试文件 / 76 例；
- 收尾脚本 `scripts/alpha_v2_m3_regression_record.py` 生成
  ``artifacts/alpha_v2/audit/m3_batch_regression.json``（不再引用不存在文件）。

## 10. M3 Final Blocking Fix Round R3（2026-09-19，BLK-R2-1 / BLK-R2-2 / N-R2-1）

### 10.0 验收历史（保留，不覆盖）

```text
M3 Acceptance Round 1（外部独立验收）   = FAIL（B1–B8）
M3 Recheck Round 2（独立复核）          = FAIL（两项穿透：BLK-R2-1 伪造 capture_date；BLK-R2-2 构建身份双源未同时校验）
M3 Final Blocking Fix R3（修复落地）     = 完成（BLK-R2-1 / BLK-R2-2 / N-R2-1）
Codex Recheck（Round 3，外部独立）       = PASS（2026-09-19）
```

### 10.1 BLK-R2-1：写入日必须等于真实墙钟

问题：写入窗口比的是**调用方自称的写入日**（``capture_date`` 参数 / CLI ``--capture-date``），
于是今天补写的行可以伪装成"T 日当时写入"，且行上不留补写标记——上一轮实测 20 个历史日
全部落账、``clean_oos_days=20``、``failure_alert(20).reached=True``。

修复（三层）：

1. **API 层**：``write_shadow_snapshot`` 删除 ``capture_date`` 参数——"自称写入日"这个攻击面
   从签名里消失；写入日一律取 ``wall_clock_now()``（生产 = 系统时间）。
2. **授权层**：**production 模式永久不可注入时钟**（没有任何开关能打开它）——
   ``capture_clock_policy`` 的唯一条件是 ``validation_mode != production``；
   生产 freeze CLI 只产 ``production``（``deterministic_clock=false``）或 ``rehearsal``。
   CLI 的 ``--capture-date`` 在生产模式直接 ``exit 8``；rehearsal 允许（但 rehearsal 永不计 clean）。
   M3 测试套件用 ``validation_mode="test"``（与 production 同语义、可进 clean，CLI 产不出该模式）
   在确定性时钟下跑通 clean 流水线。
3. **行与清单**：每行新增 ``actual_capture_date``（真实写入日）与 ``deterministic_clock``；
   补写行固定 ``backfilled=true`` / ``clean_oos_eligible=false`` / ``backfill_reason`` 非空。
4. **KPI 第二道闸**：日级再复算 ``recorded_at``（与 ``actual_capture_date``）必须与
   ``signal_date`` 同日，否则该日 ``clean_oos_eligible=false``，``reason=late_recorded_at``。

### 10.2 BLK-R2-2：构建身份四值一致

问题：门取 ``.build_commit OR build_manifest.commit``，两源矛盾或缺一源都能过。

修复：``freeze_precheck.assert_build_identity`` 改为**四值一致**硬门——生产模式要求

```text
git HEAD（可读时） == requested code_commit == .build_commit == build_manifest.commit
```

且 ``build_manifest.json`` **必须存在可解析**、``trusted=true``、``dirty=false``；
任一项 missing / unknown / malformed / mismatch → exit 5。``runtime_identity`` 新增
``read_build_manifest_file``（存在性即证据，不再用环境变量兜底冒充产物）与
``build_manifest_present`` 字段；freeze CLI 把四值与工作区状态写进清单
``build_identity``（纳入 ``freeze_manifest_hash`` 覆盖）。

唯一显式例外：容器内 git 不可得时无法比较 git HEAD，此时必须 ``--code-commit`` 显式给值，
并由**两源互证**（比"任选一源"更强），来源标记如实写 ``cli_override_git_unavailable``。

### 10.3 N-R2-1：capture 侧 data_health 接线

新增 ``validation/data_health_capture.py``（复用 S08 ``ops.data_health`` 契约，不新造口径）：

- ``capture_data_health_block``：同日判定（``as_of == signal_date``）+ 状态映射
  （``healthy→ok``；``degraded/broken`` 原样降级；``as_of`` 不同日 → ``stale``；
  词表外 → ``invalid``；工件缺失 → ``not_available``）；
- ``data_health_gate_ok``：**唯一一份** clean 判定，捕获侧与 KPI 治理层共用；
- capture CLI 新增 ``--data-health``，把结构块写进快照行与当日清单；读不到就如实写
  ``not_available``——**Shadow 记录照写，但不进 clean OOS**（不因缺工件而停记录）。
- ``scripts/alpha_v2_data_health_snapshot.py``：当天工件生产器（薄包装
  ``evaluate_data_health``，拿不到的输入就不传 → S08 判 degraded → 不进 clean）。

### 10.4 R3 证据

```text
M3 定向   = 10 文件 / 112 collected / 112 passed / 0 failed / 0 skipped（junit 实测）
全量回归   = 3610 collected / 3608 passed / 0 failed / 0 errors / 2 skipped
            （见 §6 与 artifacts/alpha_v2/audit/m3_batch_regression.json，R3 刷新）
对抗复跑   = Attack A（20 天固定时钟注入 20/20 被拒；CLI 伪造 flag exit 8；clean_oos_days=0、
            failure_alert(20).reached=False）
            Attack B（四值一致=PASS；缺 .build_commit / 缺 build_manifest / 双源矛盾 /
            git HEAD 不符 / requested 不符 / trusted=false / dirty=true = 全部 FAIL）
data_health = 同日 ok → clean；missing / not_available / stale / degraded → capture 照写、clean=false、样本门不推进
```

> 部署前提（新增）：production freeze 现在**同时要求** ``.build_commit`` 与
> ``build_manifest.json`` 存在、可解析、trusted 且 clean；capture 之前必须先产出
> 当天 data_health 工件，否则该日不会进 clean OOS。

## 附：证明性产物路径

```text
冻结清单           artifacts/alpha_v2/validation/validation_freeze_manifest.json
epoch 注册表       artifacts/alpha_v2/validation/epochs.json
NAS 只读核验       artifacts/alpha_v2/audit/nas_readonly_verification_20260918.json
DF-M2-003 诊断     artifacts/alpha_v2/validation/feature_diagnosis/
冻结模型           artifacts/alpha_v2_rehearsal/model/alpha_v2_shadow_epoch_001/
首轮排演（修复前）  artifacts/alpha_v2_rehearsal/validation/alpha_v2_epoch_001/**
本轮回放证据       脚本 %TEMP%/m3_acceptance/rehearsal_a_freeze_gates.py +
                   rehearsal_b_capture_mature.py（完整输出见本轮会话记录）
```

> 注：首轮排演产物（``artifacts/alpha_v2_rehearsal/validation/**``）由 F1-F7 之前的代码
> 产生，其 manifest 没有 ``validation_mode``、``feature_schema`` 列为空、execution=qfq ——
> 现在这些都被新口径如实判为非 clean（治理层把这一天记为 not clean），不删除，供审计对照。
