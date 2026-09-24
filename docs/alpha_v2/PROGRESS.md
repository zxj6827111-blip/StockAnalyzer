# StockAnalyzer Alpha V2.0 — PROGRESS

> 用途：记录 Alpha V2.0 改造过程中的施工进度、测试结果、审计工件、Codex 验收结果与回滚信息。  
> 维护原则：只追加，不覆盖历史。  
> 上位方案：`StockAnalyzer_Alpha_V2_完整改造方案_20260917.md`

---

# 0. 当前总状态

> **本区块已由下方 "0.1 当前状态（superseding）" 取代。** 下面这段是 M2 收尾时的
> current-state 快照，保留不删、不重写（只追加指针），以便对账当时的判断。

```text
Project = StockAnalyzer Alpha V2.0
Current Batch = M2（S11-S23 工程实现完成）
Current Stage = S23 DONE
M1 Acceptance = PASS（第三轮独立验收；外部独立验收结论，用户转达，2026-09-18 转录）
M2 Acceptance = PASS（工程实现层；外部独立验收结论，用户转达，2026-09-18 转录）
                 待办：M2 工作区尚未 commit（验收建议按"方式 B"批次固化，需用户授权）
Production Promotion = LOCKED
```

分支与提交（本地；未 push、未部署）：

```text
branch = feat/alpha-v2-m1-0917
review baseline = fix/asof-breadth-gate-coverage-0917 @ 7e9e33bdb9d03506cff5dfff29c78b1c95019541
S00 commit = 83e4fd7 (feat(alpha-v2): add shadow feature flags and no-op baseline)
S00 docs   = f6ed795 (docs(alpha-v2): S00 PROGRESS 记录)
S01 commit = a78a990 (feat(alpha-v2): 真实模型身份链)
S02 commit = 5b1e504 (feat(alpha-v2): 盘后信号改 T+1 真实可成交入场)
```

---

## 0.1 当前状态（superseding，2026-09-19）

> **本区块已由下方 "0.2 当前状态（superseding，2026-09-20）" 取代**，原文保留不删、
> 不重写，以便对账 R4.1 mini recheck 之前的判断。

```text
Project = StockAnalyzer Alpha V2.0

M1 = PASS
M2 = PASS
M3 = PASS

M3_ACCEPTED_BASELINE_COMMIT = 33d0f7f97e79215ad8c65f972c0e56eeead10614
                              （feat(alpha-v2): freeze M3 validated shadow OOS baseline）

Current Phase = PRODUCTION_RUNTIME_IDENTITY_HARDENING

BLK-D1 = CLOSED（R4 独立验收）
BLK-D2 = CLOSED（R4 独立验收）
R4.1 MODEL_PROVENANCE_BINDING = IMPLEMENTATION DONE / CODEX_MINI_RECHECK = PENDING

PRODUCTION_SHADOW_FROZEN_COMMIT = PENDING
Production Shadow = NOT_STARTED
Clean OOS Days = 0

Alpha Verified = FALSE
Production Promotion = LOCKED
```

当前分支与动作边界（本地）：

```text
branch = feat/alpha-v2-production-runtime-identity
          （从 M3_ACCEPTED_BASELINE_COMMIT 切出，用于本硬化阶段）
commit  = NOT CREATED（R4.1 按要求不提交，等 Codex mini recheck）
push    = NOT PERFORMED
deploy  = NOT PERFORMED（NAS 未部署、生产镜像未重建）
epoch   = alpha_v2_epoch_001 NOT STARTED（真实 epoch 尚未创建）
```

本阶段详情见 `docs/alpha_v2/Production_Runtime_Identity_Hardening_Report.md`
（§1–§14 = R4；§15 = R4.1）；
M3 部署顺序修订见 `docs/alpha_v2/M3_Production_Readiness_Report.md` §17.2 / §17.4 / §17.5。

---

## 0.2 当前状态（superseding，2026-09-20）

```text
Project = StockAnalyzer Alpha V2.0

M1 = PASS
M2 = PASS
M3 = PASS

R4   RUNTIME_IDENTITY_HARDENING = PASS（2026-09-20 外部独立验收）
BLK-D1                          = CLOSED
BLK-D2                          = CLOSED
R4.1 MODEL_PROVENANCE_BINDING   = PASS（2026-09-20 Codex mini recheck）

M3_ACCEPTED_BASELINE_COMMIT = 33d0f7f97e79215ad8c65f972c0e56eeead10614
                              （feat(alpha-v2): freeze M3 validated shadow OOS baseline）

FINAL_RUNTIME_HARDENING_COMMIT  = READY_TO_CREATE（本文档定稿后由本批次创建）
PRODUCTION_SHADOW_FROZEN_COMMIT = PENDING（= Final Commit 的 SHA，创建后记录）
PRODUCTION_SHADOW_DEPLOYMENT    = READY_AFTER_NAS_BUILD_PREFLIGHT

Current Phase = NAS_BUILD_PREFLIGHT

Production Shadow   = NOT_STARTED
alpha_v2_epoch_001  = NOT_STARTED
Live Clean OOS Days = 0

Alpha Verified       = FALSE
Production Promotion = LOCKED
```

当前分支与动作边界（本地）：

```text
branch = feat/alpha-v2-production-runtime-identity
          （从 M3_ACCEPTED_BASELINE_COMMIT 切出，用于本硬化阶段）
commit  = NOT CREATED（Phase A 文档定稿后创建；该 SHA 即 PRODUCTION_SHADOW_FROZEN_COMMIT）
push    = NOT PERFORMED（仅在 NAS 部署流程明确需要时执行）
deploy  = NOT PERFORMED（NAS 未部署、生产镜像未重建）
epoch   = alpha_v2_epoch_001 NOT STARTED（真实 epoch 尚未创建）
```

验收历史（完整保留，不覆盖）：M3 Round 1 FAIL → M3 Round 2 FAIL → M3 Round 3 PASS
→ R4 PASS → R4.1 首轮窄复核 FAIL（修复不在树里）→ R4.1 实施 → R4.1 mini recheck PASS。

> R4.1 的 PASS **只覆盖"冻结模型工件训练 commit 绑定"这一窄域**，不等于整套 Runtime
> Identity 被重新大验收；R4 的 PASS 仍以其自身验收记录为准。R4.1 详情见
> `docs/alpha_v2/Production_Runtime_Identity_Hardening_Report.md` §1.1 与 §15；
> M3 部署顺序修订见 `docs/alpha_v2/M3_Production_Readiness_Report.md`
> §17.2 / §17.4 / §17.5。

---

# 1. 批次定义

```text
M1 = S00 ～ S10
     Correctness Foundation

M2 = S11 ～ S23
     Alpha Research & Shadow
```

规则：

- M1 未经 Codex 验收 PASS，不得进入 M2；
- M2 工程实现 PASS，不代表研究证据已经成熟；
- 60D / 120D / 250D 研究门需要真实交易日和成熟 outcome；
- 未经明确授权，不得切换生产 serving model，不得启用正式 V2 Final。

---

# 2. M1 — Correctness Foundation

## Batch Status

```text
Status = IN_PROGRESS
Starting HEAD = 7e9e33bdb9d03506cff5dfff29c78b1c95019541
Ending HEAD = TBD
Codex Acceptance = PENDING
```

> 说明（2026-09-18 追加）：**上面这段 Batch Status 与下面的 Stage Matrix 都是
> 「首轮」（S00–S02 提交后）的快照**：`IN_PROGRESS` / `Ending HEAD = TBD` /
> `Codex Acceptance = PENDING` / S03–S10 的 `NOT_STARTED` 均已被后续施工取代。
> **最终状态以 §12「M1 全量 Stage Matrix」与 §14 验收记录为准**（全部 DONE / PASS）。
> 保留本节是为了让「首轮 FAIL 是完整性判定」这一事实有出处。

## Stage Matrix

| Stage | Task | ZCode Status | Tests | Audit Artifact | Codex Acceptance | Notes |
|---|---|---|---|---|---|---|
| S00 | Alpha V2 Feature Flag / No-op Baseline | DONE | 40 定向 + 3031 全量 / 0 failed | `artifacts/alpha_v2/audit/s00_validation.json` | PENDING | Legacy 零行为变化（结构性 + golden 双向锁定） |
| S01 | Model Identity Truth | DONE | 29 新增 + 447 相关 / 0 failed | `artifacts/alpha_v2/audit/s01_validation.json` | PENDING | 事实（工件）与补充（registry/bootstrap）分离；trained_at 不再取 bootstrap |
| S02 | T+1 Entry Simulation | DONE | 19 新增 + 197 相关 / 0 failed | `artifacts/alpha_v2/audit/s02_validation.json` | PENDING | entry_date > signal_date 或 no_fill；600000 基准在新口径逐项复现 |
| S03 | Point-in-Time Historical Universe | NOT_STARTED | - | - | PENDING | S03 起为 M1 剩余阶段（首轮 Codex FAIL 为完整性判定） |
| S04 | SelectionContract 300/100/50 | NOT_STARTED | - | - | PENDING | |
| S05 | Registry / Archive Governance | NOT_STARTED | - | - | PENDING | |
| S06 | HistoricalModelResolver | NOT_STARTED | - | - | PENDING | |
| S07 | Feature Price / Execution Price Split | NOT_STARTED | - | - | PENDING | |
| S08 | Data Health / Market Breadth Split | NOT_STARTED | - | - | PENDING | |
| S09 | Model / Label Semantic Guard | NOT_STARTED | - | - | PENDING | |
| S10 | Decision Log + Outcome Maturation | NOT_STARTED | - | - | PENDING | |

---

> 说明（2026-09-18 追加）：本节是 **M2 开工前**的快照（`LOCKED` / `Starting HEAD = TBD` /
> `Codex Engineering Acceptance = PENDING` 与整表 `LOCKED` 均已被后续施工取代）。
> **最终状态以 §13「M2 批次」与 §14 验收记录为准**（全部 DONE / PASS）。
> 保留本节是为了让「M2 在 M1 PASS 前不得开工」这一事实有出处。

# 3. M2 — Alpha Research & Shadow

## Batch Status

```text
Status = LOCKED
Prerequisite = M1 Codex PASS
Starting HEAD = TBD
Ending HEAD = TBD
Codex Engineering Acceptance = PENDING
Research Evidence Status = LOCKED
```

## Stage Matrix

| Stage | Task | ZCode Status | Tests | Audit Artifact | Codex Acceptance | Research Status | Notes |
|---|---|---|---|---|---|---|---|
| S11 | Label V2 | LOCKED | - | - | PENDING | - | |
| S12 | Benchmark System | LOCKED | - | - | PENDING | - | |
| S13 | Winner Recall | LOCKED | - | - | PENDING | - | |
| S14 | Feature Availability / Leakage Audit | LOCKED | - | - | PENDING | - | |
| S15 | Simple Factor Baseline | LOCKED | - | - | PENDING | - | |
| S16 | Shared Feature Matrix + Multi-Head | LOCKED | - | - | PENDING | - | |
| S17 | Cross Review V2 | LOCKED | - | - | PENDING | - | |
| S18 | Final Decision Policy V2 Shadow | LOCKED | - | - | PENDING | - | |
| S19 | Purged Walk-Forward / OOS | LOCKED | - | - | PENDING | - | |
| S20 | Legacy vs V2 Shadow Dual Run | LOCKED | - | - | PENDING | AWAITING_DATA | |
| S21 | Daily Alpha Health Report | LOCKED | - | - | PENDING | AWAITING_DATA | |
| S22 | NAS Performance Hardening | LOCKED | - | - | PENDING | - | |
| S23 | Theme / News / Intraday Incremental Framework | LOCKED | - | - | PENDING | AWAITING_DATA | |

---

# 4. 研究证据门

> 本节只能根据真实成熟 outcome 更新，禁止凭代码实现直接标记 PASS。

| Gate | Required Evidence | Status | Evidence Window | Notes |
|---|---|---|---|---|
| 20D Alert Gate | >=20 mature decision dates | AVAILABLE（本地开发窗口） | 197 成熟决策日 | 仅用于发现明显失败；口径见 §13.5 |
| 60D Research Gate | >=60 mature decision dates | AVAILABLE（本地开发窗口） | 197 成熟决策日 | 允许第一轮方向判断；**不等于已验证有效** |
| 120D Advisory Gate | >=120 clean OOS dates | AWAITING_DATA | S19 clean OOS 仅 60 日 | 才允许讨论 Advisory；S16/S21 的 197 日**非** clean OOS |
| 250D Governance Gate | ~250 OOS trading days | AWAITING_DATA | - | 才讨论自动 promotion / 稳定阈值 |

> 更新依据（2026-09-18）：S11 在本地真实数据（400 票 / 202 交易日 / 80,800 决策）上产出了
> **真实成熟 outcome**，故 20D/60D 可标 AVAILABLE——但这是**本地开发窗口**口径，
> 该窗口已被反复用于开发与选择，**不是 clean OOS**；120D 只能依赖 S19 的 walk-forward，
> 当前 60 日 → 仍 `AWAITING_DATA`。
> 结论表述边界：`AVAILABLE` 只描述样本量，不构成「模型有效」；S19 判定为 `INCONCLUSIVE`。

---

# 5. 阶段记录模板

> ZCode 每完成一个 SXX，在本文件末尾追加一份，不得覆盖旧记录。

```markdown
## SXX — <Stage Name>

### Status
DONE / PARTIAL / BLOCKED

### Date
YYYY-MM-DD

### Batch
M1 / M2

### Starting HEAD
<commit>

### Ending HEAD / Working Tree
<commit or dirty state>

### Files Changed
- ...

### Behavior Changes
- ...

### Tests
Commands:
- `pytest ...`

Results:
- XX passed
- XX failed
- XX skipped

### Audit Artifacts
- `artifacts/alpha_v2/audit/...`

### Deviation From Blueprint
NONE / ...

### Deferred Findings
- ...

### Rollback
- ...

### ZCode Conclusion
DONE / PARTIAL / BLOCKED

### Codex Acceptance
PENDING / PASS / FAIL

### Codex Blocking Findings
NONE / ...

### Next Stage
LOCKED / SXX
```

---

# 6. 批次验收记录模板

## M1 Acceptance

```text
Engineering Verdict = PASS
Codex Verdict = PASS（第三轮；外部独立验收结论，用户转达，2026-09-18 转录）
Permission To Proceed To M2 = YES
Reviewed HEAD = 43e91cc266027bca3fa7f5b78a73929d741b03e9
Review Date = 2026-09-18
```

### Blocking Findings
- TBD

### Non-Blocking Findings
- TBD

---

## M2 Acceptance

```text
Engineering Verdict = PASS
Codex Verdict = PASS（工程实现层；外部独立验收结论，用户转达，2026-09-18 转录）
Research Evidence Status = 60D AVAILABLE / 120D·250D AWAITING_DATA
Production Promotion = LOCKED
Reviewed HEAD = 43e91cc 之上的工作区（未 commit；21 项变更）
Review Date = 2026-09-18
```

### Blocking Findings
- TBD

### Non-Blocking Findings
- TBD

---

# 7. Production Promotion 状态

```text
V2 Shadow = NOT_ENABLED
V2 Advisory = LOCKED
V2 Enforced Final Selection = LOCKED
Auto Promotion = DISABLED
```

任何生产切换必须独立记录：

```text
approved_by
approved_at
code_commit
model_id
artifact_hash
config_hash
rollback_point
```

不得只写“已上线”。

---

# 8. Deferred Findings 总表

| ID | Found At | Description | Severity | Owner | Target Stage | Status |
|---|---|---|---|---|---|---|
| DF-S00-001 | S00 | `StockAnalyzerConfig.model_dump()` 无法 `model_validate` 回填（limit_rule alias `from`），影响"配置 dump 后重放"类工具 | low | unassigned | S04 或独立修复 | OPEN |
| DF-S00-002 | S00 | 生产有效 `models.inference_score_source=calibrated` vs 受跟踪默认 `raw`，基线清单需带环境口径 | info | unassigned | S01 | CLOSED（`config_hash_scope` + 双口径记录） |
| DF-S00-003 | S00 | `scripts/` 旧临时脚本与根目录 `tmp_*` 残留较多 | info | unassigned | 批次收尾清理 | OPEN |
| DF-S01-001 | S01 | registry 无 champion → 身份状态恒为 no_champion / match_registered（仅可见，未治理） | info | unassigned | S05 | OPEN |
| DF-S01-002 | S01 | 历史回测仍加载当前 serving 工件（无 HistoricalModelResolver）→ as_of 早于工件 created_at 时存在未来模型风险 | high | unassigned | S06 | OPEN |
| DF-S01-003 | S01 | `asof_scan.model_trained_at` 字段名沿用旧契约（值语义已修正） | info | unassigned | S04 | OPEN |
| DF-S02-001 | S02 | `holding_curve._bar_snapshot` 缺列时注入 `close*1.1/0.9` 估算涨跌停且被当权威值 → 掩盖真实板块涨跌幅（一字涨停在该路径被判可成交，已实测） | high | unassigned | S07 | OPEN |
| DF-S02-002 | S02 | provider 涨跌停价 NaN 被 `_optional_float` 当有值 → `build_price_limits` fail-open（NaN 比较恒 False） | high | unassigned | S07/S08 | OPEN |
| DF-S02-003 | S02 | asof 回测 holding curve 主口径仍为 0 滑点（参数已暴露，服务层未设值） | medium | unassigned | S07 | OPEN |

---

# 9. 关键不变量

整个改造期间持续检查：

```text
Legacy threshold 70 未被擅自降低
Legacy Cross Review 未被擅自放宽
V2 未经授权不接管正式结果
Historical replay 无 future-model fallback
盘后策略无 T-close 假成交
历史 universe 无 future-listed symbol
Execution 使用 raw price
模型输出语义明确
0 只结果合法
所有阶段可审计、可回滚
```

---

# 10. 当前下一步

```text
NEXT ACTION（2026-09-18）:
M1 = PASS（第三轮）；M2 = PASS（工程实现层，见 §14）
M2 工作区尚未 commit —— 待用户授权后按"方式 B"做批次 commit 固化

AFTER COMMIT:
本批结束；生产部署 / 模型切换 / 阈值调整仍需**另行授权**
（M2 验收明确：Production Promotion = LOCKED）
```

---

# 11. 阶段记录（追加，不覆盖）

## S00 — Alpha V2 Feature Flag / No-op Baseline

### Status
DONE

### Date
2026-09-17

### Batch
M1

### Starting HEAD
7e9e33bdb9d03506cff5dfff29c78b1c95019541

### Ending HEAD / Working Tree
83e4fd74c09bfc36d17067b01288ae1973182e7e（S00 代码提交；此后 PROGRESS/审计工件变更见本批次后续记录）

### Files Changed
- `src/stock_analyzer/config.py`：新增 `AlphaV2Config`（9 字段 + 闭集/正数校验 + 两条开关组合 fail-closed）与 `StockAnalyzerConfig.alpha_v2` 字段
- `config/default.yaml`：新增 `alpha_v2` 块（显式默认值 + 合法/非法组合注释）
- `src/stock_analyzer/config_identity.py`（新）：脱敏配置指纹（canonical JSON + sha256，先按字段名脱敏）
- `src/stock_analyzer/alpha_v2/artifacts.py`（新）：`artifacts/alpha_v2/{audit,decisions,outcomes,manifests,reports}` 目录语义 + 幂等 ensure + 原子 JSON 写
- `src/stock_analyzer/alpha_v2/baseline.py`（新）：基线清单构造/写入（代码身份 + 配置身份 + Legacy 行为面快照）
- `src/stock_analyzer/alpha_v2/__init__.py`（新）
- `scripts/build_alpha_v2_baseline_manifest.py`（新）：显式 CLI 生成基线清单
- `tests/test_alpha_v2_config.py`、`tests/test_alpha_v2_baseline.py`（新）：Test A–E + golden/架构守卫/凭据卫生
- `tests/fixtures/alpha_v2/legacy_baseline_contract.json`（新）：冻结的 Legacy 行为面契约
- `docs/alpha_v2/**`：蓝图、阶段施工/验收提示词、本台账（纳入版本管理）

### Behavior Changes
- Alpha V2 新增：独立配置块、审计根目录语义、基线清单生成能力、脱敏配置指纹。
- Legacy 无变化（结构性 + 双向锁定）：
  1. 结构性：Legacy 源码树不得出现 `alpha_v2` 引用（架构守卫测试），运行路径不 import `alpha_v2` 包；
  2. 双向锁定：`legacy_baseline_contract.json` 冻结阈值/门禁/目标，改任一项都会在测试里失败；
  3. 开关不变性：V2 关闭 / Shadow / 接管三种组合下 Legacy 行为面逐字段相同。
- 未改 70 分阈值、Cross Review 四阈值、300/100/50、final cap、risk gates、模型权重或在服模型；未接通知链路。

### Tests
Commands:
- `python -m pytest tests/test_alpha_v2_config.py tests/test_alpha_v2_baseline.py tests/test_config.py` → 40 passed
- `python -m pytest tests/test_config.py tests/test_conftest_path_isolation.py tests/test_theme_layer.py tests/test_nightly_report.py tests/test_alpha_v2_config.py tests/test_alpha_v2_baseline.py` → 123 passed
- `python -m pytest tests/test_pipeline.py tests/test_pipeline_asof.py tests/test_pipeline_model_artifact.py tests/test_inference_score_source.py tests/test_output_semantics_contract.py tests/test_manifest_identity_remediation.py tests/test_artifact_identity.py tests/test_evolution_specs.py tests/test_learning_sample_schema.py tests/test_learning_sample_store.py` → 105 passed
- `python -m pytest tests/test_nightly_delivery.py tests/test_nightly_scheduling.py tests/test_nightly_readiness_authoritative.py tests/test_week5_automation.py tests/test_week5_automation_scheduler.py tests/test_week5_scan_funnel_policy.py tests/test_week5_dual_track.py tests/test_main_week5.py tests/test_scheduler.py tests/test_service_week5.py` → 275 passed
- `python -m pytest -n 4 --dist loadfile`（S00 提交 83e4fd7 上）→ 3031 passed / 2 skipped / 0 failed（9m58s）
- `python -m ruff check`（S00 全部改动文件）→ All checks passed
- `python -m mypy`（新增模块）→ 新增文件 0 error（其余 39 条为基线既有）

Results:
- 定向集 40 passed（0 failed）
- 全量 3031 passed / 2 skipped / 0 failed

### Audit Artifacts
- `artifacts/alpha_v2/audit/baseline_manifest.json`（`config_hash_scope=effective_config_with_env_overrides`）
- `artifacts/alpha_v2/audit/s00_validation.json`（含双口径 config hash、测试清单、Legacy 不变量、Deferred Findings）
- 说明：`artifacts/*` 受 `.gitignore` 管理（运行时产物），基线契约的**受跟踪副本**在 `tests/fixtures/alpha_v2/legacy_baseline_contract.json`

### Deviation From Blueprint
- 蓝图 §14 建议的嵌套结构（selection/point_in_time/execution/outcomes/heads/research/news/theme/intraday）本阶段**只实现 P0-00 任务卡列出的 9 个扁平字段**；嵌套子块留到各自消费者阶段（S02/S04/S06/S07/S11+）再落地，避免先写一批无消费方的配置。
- 蓝图建议 `docs/alpha_v2/PROGRESS.md` 与 `artifacts/alpha_v2/audit/`；实际按仓库 `.gitignore` 约定：PROGRESS 纳入版本管理，audit 工件为运行时产物（可重现生成）。

### Deferred Findings
- DF-S00-001（low）：`StockAnalyzerConfig.model_dump()` 的整份配置无法 `model_validate` 回填（`limit_rule.rule_version_by_date`/`cost_schedule_by_date` 用 alias `from` 建字段，裸 dump 输出 `from_date` 再校验报 extra_forbidden）。影响任何"配置 dump 后重放"的工具；本阶段只在测试里绕开。
- DF-S00-002（info）：生产有效配置 `models.inference_score_source=calibrated`（蓝图 §2.4）而受跟踪默认值为 `raw`；基线清单记录"生成环境有效值"，跨环境比对必须带口径说明（已在清单里加 `config_hash_scope` 与验证工件里的双口径字段）。
- DF-S00-003（info）：`scripts/` 下的旧临时脚本与仓库根 `tmp_*` 残留较多，本阶段未清理（不属 S00 范围）。

### Rollback
- 仅回滚 S00：`git revert 83e4fd7`（或 `git reset --hard 7e9e33bd` 后丢弃该分支）。
- 无生产影响：未 push、未部署、未改 NAS/.env/容器；`artifacts/alpha_v2/**` 为新增目录，删除即完全复原。
- 在服模型与通知链路未被触碰，回滚不需要任何模型/配置恢复动作。

### ZCode Conclusion
DONE

### Codex Acceptance
PENDING

### Codex Blocking Findings
NONE

### Next Stage
S01 — Model Identity Truth

## S01 — Model Identity Truth

### Status
DONE

### Date
2026-09-17

### Batch
M1

### Starting HEAD
f6ed79561159b1d2240ceb524d3e1ed5bbaa1d58（S00 记录提交）

### Ending HEAD / Working Tree
a78a990（S01 代码提交；审计工件随后生成）

### Files Changed
- `src/stock_analyzer/models/identity.py`：新增 `load_artifact_facts`（磁盘事实）、`registry_identity`（registry 补充，生产健康端点与历史路径共用）、`build_model_identity_report`（事实 + 补充 + 六态判定 + `research_fail_closed` / `identity_verified`）
- `src/stock_analyzer/models/predictor.py`：`SignalPredictor` 保存并暴露工件自述契约（created_at / feature schema / label policy / dataset manifest）+ `model_identity_facts()`
- `src/stock_analyzer/pipeline.py`：新增只读 `model_identity_facts()` / `model_identity()`（artifact 缺失时同样给出结构完整的事实）
- `src/stock_analyzer/runtime/service.py`：`artifact_identity_report` 的 registry 收集改为复用同一 helper（响应字段与状态口径不变）
- `src/stock_analyzer/backtest/asof_scan.py`：新增 `model_identity` 入参，caveats 携带身份块
- `src/stock_analyzer/runtime/services/asof_backtest_service.py`：新增 `_resolve_backtest_model_identity`；`model_trained_at` 改取工件 created_at；caveats 增加身份块与"请求级 vs 各日期运行身份"一致性标注
- `src/stock_analyzer/runtime/services/week5_historical_runner.py`：`_resolve_model_info` 改为读 pipeline 事实 + registry 补充；不再用 bootstrap 时间冒充身份
- `src/stock_analyzer/runtime/services/week5_selection_engine.py`：`Week5ModelInfo` 扩展身份字段与 `to_payload()`
- `tests/test_model_identity_truth.py`（新，29 例）

### Behavior Changes
- 新增：pipeline 只读身份 API；Week5ModelInfo / caveats 身份块；研究侧 fail-closed 标记。
- 语义修正：`trained_at` = 实际加载工件 created_at（`trained_at_source=artifact_created_at`）；bootstrap 时间只作 `bootstrap_last_bootstrap_at`；registry `model_id` 仅在身份可验证时报告。
- registry hash ≠ actual hash → `status=mismatch` → `research_fail_closed=true`；`no_champion` / registry 读失败不算 mismatch（不锁死研究链）。
- Legacy 未变：阈值 / Cross Review / 300-100-50 / final cap / 风险门 / 在服模型 / 飞书通知；健康端点响应字段与状态口径不变。

### Tests
Commands:
- `python -m pytest tests/test_model_identity_truth.py` → 29 passed
- `python -m pytest tests/test_pipeline.py tests/test_pipeline_asof.py tests/test_pipeline_model_artifact.py tests/test_artifact_identity.py tests/test_inference_score_source.py tests/test_output_semantics_contract.py tests/test_asof_backtest_service.py tests/test_week5_historical_backtest.py tests/test_api_backtest.py tests/test_model_identity_truth.py` → 158 passed
- `python -m pytest tests/test_service_week5.py tests/test_week5_automation.py tests/test_week5_dual_track.py tests/test_week5_scan_funnel_policy.py tests/test_main_health.py tests/test_main_week5.py tests/test_service_model_registry.py tests/test_model_registry_state_machine.py tests/test_probability_health.py tests/test_model_inference_safety.py tests/test_holding_curve.py` → 225 passed
- `python -m pytest tests/test_runtime_invariants.py` → 35 passed

Results:
- S01 相关 447 passed / 0 failed
- 期间 1 例既有测试（test_artifact_identity 的 registry unavailable）曾拦下我的实现变更（把 "db locked" 文本猜成 registry_busy），已按"不从异常文本猜分类"回退，未放宽断言

### Audit Artifacts
- `artifacts/alpha_v2/audit/s01_validation.json`
- `artifacts/alpha_v2/audit/baseline_manifest.json`

### Deviation From Blueprint
- 蓝图 §4.1 的 `DecisionIdentity` 全字段（data_snapshot_id / universe_snapshot_id / resolver_mode 等）尚未一次到位：S01 先落地"实际加载工件"这一侧（model_id / artifact_uri / content_hash / created_at / schema / label），其余字段随 S03/S04/S06/S10 补齐。

### Deferred Findings
- DF-S01-001（info）：registry 无 champion → 生产身份状态恒为 no_champion / match_registered，S01 只让其可见，治理动作属 S05。
- DF-S01-002（high）：历史回测仍加载**当前** serving 工件（无 HistoricalModelResolver），as_of 早于工件 created_at 时存在未来模型风险；S01 只如实报告身份（见 s01_validation）。
- DF-S01-003（info）：`asof_scan` 的 `model_trained_at` 字段名沿用旧契约（值语义已修正为 artifact created_at），字段重命名留待 S04 contract 统一。

### Rollback
- `git revert a78a990`（仅 S01；S00 不受影响）。
- 无生产影响：未 push / 未部署 / 未改 .env 或容器。

### ZCode Conclusion
DONE

### Codex Acceptance
PENDING

### Codex Blocking Findings
NONE

### Next Stage
S02 — T+1 Entry Simulation

---

## S02 — T+1 Entry Simulation

### Status
DONE

### Date
2026-09-17

### Batch
M1

### Starting HEAD
a78a990（S01 代码提交）

### Ending HEAD / Working Tree
5b1e504（S02 代码提交；审计工件随后生成）

### Files Changed
- `src/stock_analyzer/backtest/matcher.py`：新增 `EntrySimulation` + `ExecutionMatcher.simulate_entry()`（主口径 T+1 / sensitivity 窗口 / 五类 no_fill 原因 / 滑点与成本）
- `src/stock_analyzer/backtest/holding_curve.py`：入场改为 T+1 可成交开盘；`SymbolHoldingResult` 增加信号日/延迟/raw 价格/滑点/成本/no_fill 字段；`HoldingCurveSummary` 增加 no_fill 计数与原因分布
- `src/stock_analyzer/runtime/services/asof_backtest_service.py`：caveats 增加 `execution_contract` 块（entry_mode=next_session_open 等）
- `tests/test_entry_simulation.py`（新，19 例）
- `tests/test_holding_curve.py`（更新 14 例至 T+1 契约）

### Behavior Changes
- 新增：统一入场模拟与 no_fill 语义；holding curve 报告信号日/成交日分离。
- 修正：holding_curve 不再用信号日收盘价当成交价（蓝图 §2.12）；退出模拟从成交日之后起算，T+1 卖出规则在入场层面即被尊重。
- Legacy 未变：ExecutionEngine / ExecutionMatcher 既有 `can_buy` / `can_sell` / `simulate_exit` 语义未改（只新增 `simulate_entry`）；阈值 / 门禁 / 在服模型 / 通知未动。

### Tests
Commands:
- `python -m pytest tests/test_entry_simulation.py` → 19 passed
- `python -m pytest tests/test_holding_curve.py tests/test_asof_backtest_service.py tests/test_week5_historical_backtest.py tests/test_api_backtest.py tests/test_backtest_matcher.py tests/test_execution_engine.py` → 108 passed
- `python -m pytest tests/test_walk_forward.py tests/test_walk_forward_variants_c3.py tests/test_walk_forward_xsec_verdict_gates.py tests/test_backtest_live_consistency.py tests/test_measure_score_return.py tests/test_pipeline_asof.py tests/test_time_semantics.py` → 89 passed

Results:
- S02 相关 197 passed / 0 failed（含 600000 对照基准在新口径下的逐项复现）

### Audit Artifacts
- `artifacts/alpha_v2/audit/s02_validation.json`

### Deviation From Blueprint
- 蓝图 §4.3 的 EntryContract 里 `secondary_entry` 的"<=3 sessions"上限本阶段只作为 `max_entry_sessions` 入参暴露（默认 1 = 主口径）；服务层尚未调用 sensitivity 口径（属 S07 的执行口径工作）。
- 未做完整 P0-06 corporate action 治理（阶段提示词明确本轮不做）。

### Deferred Findings
- DF-S02-001（high）：`holding_curve._bar_snapshot` 在缺列时注入 `close*1.1/0.9` 估算涨跌停，且被引擎当权威 source 值 → 掩盖真实板块涨跌幅（一字涨停在该路径被判可成交，已实测）。
- DF-S02-002（high）：provider 涨跌停价为 NaN 时 `_optional_float` 返回 nan（非 None）→ `build_price_limits` 视为有值，而 NaN 比较恒 False → 涨停门 fail-open。
- DF-S02-003（medium）：asof 回测 holding curve 主口径仍为 0 滑点（参数已暴露，服务层未设值）。

### Rollback
- `git revert 5b1e504`（仅 S02）。
- 无生产影响：未 push / 未部署。

### ZCode Conclusion
DONE

### Codex Acceptance
PENDING

### Codex Blocking Findings
NONE

### Next Stage
S03 — Point-in-Time Historical Universe（本轮未开始）

---

# 12. 批次小结（M1）

## M1 Batch Status

```text
Batch = M1（S00-S10）
Batch Status = DONE（等待 Codex 整批验收）
Stages Completed = S00, S01, S02, S03, S04, S05, S06, S07, S08, S09, S10
Stopped At = （无）
Ending HEAD = be28ff9（S10 提交；批次收尾提交见 git log）
Review Baseline = 7e9e33bdb9d03506cff5dfff29c78b1c95019541
Codex Batch Acceptance = PASS（第三轮；外部独立验收结论，用户转达，2026-09-18 转录）
```

首轮 Codex 验收结果：**FAIL（唯一原因：批次不完整，S03-S10 未实施；S00-S02 判 PASS）**。
本轮按"先验收后继续"的纪律补齐 S03-S10，并逐条落实复审要求（N1-N5、DF-S02-001/002/003）。

## Stage Matrix（M1 全量）

| Stage | Task | ZCode | 定向测试 | 提交 | 审计工件 | Codex |
|---|---|---|---|---|---|---|
| S00 | Feature Flag / No-op Baseline | DONE | 40 passed | 83e4fd7 | `s00_validation.json` | PASS |
| S01 | Model Identity Truth | DONE | 447 passed | a78a990 | `s01_validation.json` | PASS |
| S02 | T+1 Entry Simulation | DONE | 197 passed | 5b1e504 | `s02_validation.json` | PASS |
| S03 | Point-in-Time Historical Universe | DONE | 162 passed | 3d919c7 | `s03_validation.json` | PASS |
| S04 | SelectionContract 300/100/50 | DONE | 139 passed | 89f08f0 | `s04_validation.json` | PASS |
| S05 | Registry / Archive Governance | DONE | 109 passed | 6243cd0 | `s05_validation.json` | PASS |
| S06 | HistoricalModelResolver | DONE | 135 passed | 8d3248d | `s06_validation.json` | PASS |
| S07 | Feature / Execution Price Split | DONE | 154 passed | 1a199a6 | `s07_validation.json` | PASS |
| S08 | Data Health / Breadth Split | DONE | 58 passed | 053b4b1 | `s08_validation.json` | PASS |
| S09 | Model / Label Semantic Guard | DONE | 55 passed | d0a58f7 | `s09_validation.json` | PASS |
| S10 | Decision Log + Outcome Maturation | DONE | 13 passed | be28ff9 | `s10_validation.json` | PASS |

## Codex 复审要求落实

| 项 | 要求 | 落实 |
|---|---|---|
| N1 | PROGRESS Stage Matrix 重复行 | 已修（单行 S03，且状态随本轮更新） |
| N2 | resolver 对"工件缺失"与 registry 状态无关地硬拒绝 | `historical_resolver._reject_reason` 首条判 artifact_exists，含测试 |
| N3 | `EntrySimulation.slippage` 措辞 | 改为"价格增量（net_entry_price - entry_price_raw）"，含测试 |
| N4 | M1 Implementation Report 落盘 | `docs/alpha_v2/M1_Implementation_Report.md` |
| N5 | 明确哪份工件权威 | serving manifest `authority` 块 + 对账报告 authority_note |
| DF-S02-001 | 删除估算涨跌停注入 | holding_curve / walk_forward 只透传真实列，缺列 fail-closed |
| DF-S02-002 | NaN 涨跌停不得 fail-open | `limit_rule._optional_float` 对 NaN/Inf 返回 None |
| DF-S02-003 | 执行滑点不再默认 0 | asof/week5 回测传策略静态滑点（trend=0.0015） |

## Batch Test Summary

```text
命令：python -m pytest -n 4 --dist loadfile
结果：3202 passed / 2 skipped / 0 failed（633.33s，S00-S10 + 半批审 B1/B2/B3/N1-N4 修复后）
对照：批次开始前基线 3031 passed / 2 skipped / 0 failed（新增 171 例）

过程说明（三次运行的由来）：
1. 首轮（S00-S02）：3079 passed；2. 二轮（S00-S10）：3194 passed，
   其中 1 例间歇失败源自 S06/S07 夹具写共享 learning_protocol.duckdb 与 xdist 并发撞锁
   → 改为进程内 registry 桩（压力复跑 2/2 通过）；
3. 三轮（半批审修复后）：3202 passed / 0 failed，含 B1/B2/B3 与 N1-N3 的全部新回归。
```

## 半批审（第二轮 Codex）修复记录

```text
B1（S06 阻断）：解析结果与加载路径脱钩
  → run_week5_historical_day 把 hist_config.training.artifact_path 绑定到
    resolution.artifact_uri，并在加载后复核 实算哈希 == resolution 哈希 且
    created_at <= decision_time；不符即 unscorable（带 load_verification 落痕）
  → 对抗测试：helper 级 3 例 + runner 级 1 例（config 指向"更新的在服工件"时
    实际加载旧 PIT 工件，报告哈希 = 旧哈希 ≠ 新哈希）

B2（S07 阻断）：analyze_holding_curve 缺 slippage_ratio 参数 → 服务层必崩
  → 补 slippage_ratio / max_entry_sessions 透传；端到端夹具改为真的产生候选
    （放宽终门/共识门只为让合成数据走到 final），并断言"有候选 → holding 段存在
    且 entry_mode=next_session_open"

B3（S07 阻断）：NaN 封堵只修了 limit_rule 一层
  → engine/matcher 的 _optional_numeric 统一过滤 NaN/Inf；补 Case B 形态
    （整列 NaN + 无 pre_close 的一字涨停 → no_valid_price_data）与 Inf 同口径回归

N1（S05）：prune 容量分支 no-op → 重写（预算约束整个归档；预算内不删；
  超预算从最旧非保留 bundle 删到进预算；绝不低于保留下限）+ 两条回归
N2（S08）：valid_symbol_count=None 被当 1.0 判 ok → 改判 degraded + 回归
N3（S10）：compute_outcomes 用裸默认 matcher → 支持 matcher/config 复用运行配置 + 回归
N4（既有）：test_nightly_scheduling 的墙钟敏感用例 → 显式清空 quiet_windows
```



## 本轮不变量核对

```text
final_signal_min_threshold = 70           未改（S00 golden 契约锁定）
Cross Review 四阈值                        未改
night 300/100/50、final cap 5            未改（S04 让历史与夜扫同口径，未改数值）
风险门（breadth/overextension/board）      未改
serving model / challenger                未切换、未 promote
飞书正式通知                              未改、未接 V2
生产部署 / git push / 容器重启 /.env 修改   均未执行
```

## M1 新增的高价值事实（供 M2 决策）

1. 历史回测此前实际使用 100/100/20，与夜扫 300/100/50 不可比（S04 已统一为 night-equivalent）。
2. `holding_curve` 曾注入 ±10% 估算涨跌停 → 一字涨停被判可成交；NaN 涨跌停会让涨停门 fail-open（S07 已修）。
3. 生产 label basis `soup_10d_tp8_before_sl5` 未登记语义 → 在服输出语义为 unknown（DF-S09-001）。
4. 本机受跟踪默认执行口径 = qfq（生产应为 raw）；守卫会把这类配置标 execution_uncertain（DF-S07-001）。
5. registry 无 champion + 历史坏记录：本机 0 行，NAS 需跑对账 CLI 得到权威分类（DF-S05-001）。


## S03 — Point-in-Time Historical Universe

### Status
DONE

### Date
2026-09-18

### Batch
M1

### Starting HEAD
faf0a1e4cb29a3b6b16ca489c8d895ca6956d160

### Ending HEAD / Working Tree
3d919c7（S03 代码提交）

### Files Changed
- `src/stock_analyzer/data/asof_universe.py`（新）：PIT 股票池解析（build_pit_stats / resolve_asof_universe / 快照 id）
- `src/stock_analyzer/runtime/services/week5_selection_engine.py`：历史 universe 走 PIT 解析 + 报告带 universe_snapshot
- `tests/test_asof_universe.py`（新，12 例）；`tests/test_week5_historical_backtest.py`（夹具探针改为窗口 + PIT 快照断言）

### Behavior Changes
- 新增：eligible（窗口内 bar 数 ≥ min_history）/ expected_active（最近 5 交易日有 bar）/ known_suspended（单列，不进分母）/ future_listed 硬排除；覆盖率分母 = expected_active；快照 id 可复现
- 收紧：历史股票池新增"历史充足性"硬门，并与外层 staleness 门取交集（fail-closed）
- 未变：live 路径不经过本模块；阈值/门禁/在服模型/通知未动

### Tests
Commands:
- `python -m pytest tests/test_asof_universe.py` → 12 passed
- `python -m pytest tests/test_week5_historical_backtest.py tests/test_asof_backtest_service.py` → 21 passed
- `python -m pytest tests/test_asof_backtest_service.py tests/test_api_backtest.py tests/test_week5_scan_funnel_policy.py tests/test_pipeline_asof.py tests/test_delisted_symbols.py tests/test_universe_candidate_selector.py tests/test_service_universe_fallback.py tests/test_probe_universe_quality_selector.py` → 129 passed

Results: S03 相关 162 passed / 0 failed

### Audit Artifacts
- `artifacts/alpha_v2/audit/s03_validation.json`

### Deviation From Blueprint
- 第一版用"窗口内 bar 数"代理上市时长（不做全历史扫描）；新上市与长期停牌在窗口口径下不可区分，统一归 `insufficient_history_window_bars`（如实命名，见 DF-S03-001）。

### Deferred Findings
- DF-S03-001（medium）：精确上市日需要 provider 提供 list_date/listing_days（当前批量探针不返回）。
- DF-S03-002（medium）：当日停牌但窗口内有 bar 的票仍进分母（执行层用 no_fill 兜住，完整停牌日历属 S08）。

### Rollback
- `git revert 3d919c7`。

### ZCode Conclusion
DONE

### Codex Acceptance
PENDING

### Next Stage
S04

---

## S04 — SelectionContract 300/100/50

### Status
DONE

### Date
2026-09-18

### Starting HEAD
3d919c7

### Ending HEAD / Working Tree
89f08f0（S04 代码提交）

### Files Changed
- `src/stock_analyzer/contracts/__init__.py`、`contracts/alpha_v2.py`（新）：SelectionContract + resolve_selection_contract
- `week5_selection_engine.py`：一次 run 绑定一个契约（light/deep/quality 取契约值，显式 override 优先）；funnel 段新增 selection_contract 块；历史上下文默认按 night-equivalent 解析
- `week5_historical_runner.py`：默认 scan_profile = historical_night_equivalent
- `tests/test_selection_contract.py`（新，9 例）

### Behavior Changes
- 统一：历史 night-equivalent 改用夜扫契约 300/100/50（此前 100/100/20）
- 新增：契约 id + 三目标 + cap + allow_zero + 来源配置键落报告
- 未变：live 缺省 profile 仍 legacy 目标（未获授权不动生产口径）

### Tests
Commands:
- `python -m pytest tests/test_selection_contract.py` → 9 passed
- `python -m pytest tests/test_week5_historical_backtest.py tests/test_week5_scan_funnel_policy.py tests/test_asof_backtest_service.py tests/test_api_backtest.py tests/test_week5_automation.py tests/test_selection_contract.py` → 139 passed

Results: S04 相关 139 passed / 0 failed

### Audit Artifacts
- `artifacts/alpha_v2/audit/s04_validation.json`

### Deferred Findings
- DF-S04-001（info）：live 周扫仍是 100/100/20，与夜扫不同口径（M2 shadow 双轨再决定是否统一）。

### Rollback
- `git revert 89f08f0`。

### ZCode Conclusion
DONE

### Codex Acceptance
PENDING

### Next Stage
S05

---

## S05 — Registry / Archive Governance

### Status
DONE

### Date
2026-09-18

### Starting HEAD
89f08f0

### Ending HEAD / Working Tree
6243cd0（S05 代码提交）

### Files Changed
- `src/stock_analyzer/models/serving_manifest.py`（新）：在服模型清单（事实 + registry 补充 + authority 口径）
- `src/stock_analyzer/models/registry_reconciliation.py`（新）：六类对账（只读）
- `src/stock_analyzer/models/bundle.py`：归档容量管理（保留下限 + 字节预算）
- `src/stock_analyzer/models/trainer.py`、`runtime/service.py`：接线容量预算
- `runtime/services/learning_governance_service.py`：发布流程步骤⑤ CAS 成功后写 serving manifest（best-effort）
- `config.py` / `config/default.yaml`：model_archive_max_bytes、serving_manifest_path
- `scripts/reconcile_model_registry_artifacts.py`（新）
- `tests/test_registry_governance_s05.py`（新，17 例）

### Behavior Changes
- 新增：serving manifest（`artifacts/model_serving_manifest.json`）；registry↔磁盘对账；归档容量管理
- 未变：registry 表结构与历史行未改（不伪造修复）；alias 仍兼容；发布流程与回滚未改

### Tests
Commands:
- `python -m pytest tests/test_registry_governance_s05.py` → 17 passed
- `python -m pytest tests/test_model_training.py tests/test_registry_governance_s05.py tests/test_model_bundle_release.py` → 37 passed
- `python -m pytest tests/test_service_model_registry.py tests/test_model_registry_state_machine.py tests/test_release_snapshot.py tests/test_manifest_identity_remediation.py tests/test_service_learning_governance.py tests/test_model_bundle_release.py` → 55 passed
- `python scripts/reconcile_model_registry_artifacts.py`（本机实跑：registry 0 行、归档 5 个 bundle）

Results: S05 相关 109 passed / 0 failed

### Audit Artifacts
- `artifacts/alpha_v2/audit/s05_validation.json`
- `artifacts/alpha_v2/audit/model_registry_reconciliation.json`（本机；权威分类需 NAS 侧跑同一 CLI）

### Deferred Findings
- DF-S05-001（info）：本机 model_v1.json 是开发工件，权威对账需在 NAS 跑（Codex N5）。
- DF-S05-002（medium）：历史坏记录的治理动作（重登记/重训）待 M2 拍板。

### Rollback
- `git revert 6243cd0`。

### ZCode Conclusion
DONE

### Codex Acceptance
PENDING

### Next Stage
S06

---

## S06 — HistoricalModelResolver

### Status
DONE

### Date
2026-09-18

### Starting HEAD
6243cd0

### Ending HEAD / Working Tree
8d3248d（S06 代码提交）

### Files Changed
- `src/stock_analyzer/models/historical_resolver.py`（新）：两模式解析 + 合法性条件 + 时区语义 + registry 适配器
- `week5_historical_runner.py`：pipeline 构造前的时间闸门；unscorable 返回零结果报告；可评分时报告带 model_resolution
- `tests/test_historical_model_resolver.py`（新，20 例）；`tests/test_week5_historical_backtest.py`（夹具补 PIT 合法模型登记）

### Behavior Changes
- 新增：as_of 时间闸门与 unscorable 报告；registry/磁盘适配器
- 改变：历史回测在 as_of 早于所有合法模型时不再出结果（fail-closed，DF-S01-002 闭合）
- 未变：live 路径不经过 resolver

### Tests
Commands:
- `python -m pytest tests/test_historical_model_resolver.py` → 20 passed
- `python -m pytest tests/test_week5_historical_backtest.py tests/test_asof_backtest_service.py tests/test_api_backtest.py` → 115 passed

Results: S06 相关 135 passed / 0 failed

### Audit Artifacts
- `artifacts/alpha_v2/audit/s06_validation.json`

### Deferred Findings
- DF-S06-001（medium）：无逐日激活历史 → strict_production_replay 当前几乎恒 unscorable。
- DF-S06-002（medium）：asof（非 week5）回测的 as_of 闸门待 M2 接入。

### Rollback
- `git revert 8d3248d`。

### ZCode Conclusion
DONE

### Codex Acceptance
PENDING

### Next Stage
S07

---

## S07 — Feature Price / Execution Price Split（含 DF-S02-001/002/003、N3）

### Status
DONE

### Date
2026-09-18

### Starting HEAD
8d3248d

### Ending HEAD / Working Tree
1a199a6（S07 代码提交）

### Files Changed
- `src/stock_analyzer/backtest/price_contract.py`（新）
- `src/stock_analyzer/data/limit_rule.py`：NaN/Inf 视为缺失
- `src/stock_analyzer/backtest/holding_curve.py`、`walk_forward.py`：删除估算涨跌停注入
- `src/stock_analyzer/backtest/matcher.py`：static_slippage_ratio 透传 + N3 措辞
- `runtime/services/asof_backtest_service.py`：价格口径 + 执行滑点落 caveats
- `tests/test_price_contract_s07.py`（新，13 例）；`tests/test_holding_curve.py`（夹具补 pre_close）

### Behavior Changes
- 新增：price_contract（feature/execution 双口径 + execution_uncertain）；执行滑点非 0
- 修复：NaN 涨跌停 fail-open；估算涨跌停掩盖真实板块幅度；0 滑点主口径
- 未变：live 决策路径

### Tests
Commands:
- `python -m pytest tests/test_price_contract_s07.py` → 13 passed
- `python -m pytest tests/test_api_backtest.py tests/test_asof_backtest_service.py tests/test_holding_curve.py tests/test_entry_simulation.py tests/test_walk_forward.py tests/test_backtest_matcher.py tests/test_execution_engine.py tests/test_week5_historical_backtest.py tests/test_measure_score_return.py tests/test_backtest_live_consistency.py` → 141 passed

Results: S07 相关 154 passed / 0 failed

### Audit Artifacts
- `artifacts/alpha_v2/audit/s07_validation.json`

### Deferred Findings
- DF-S07-001（high）：本机受跟踪默认执行口径 = qfq（生产按蓝图 §2.13 应为 raw）；NAS 部署时核验。
- DF-S07-002（medium）：corporate action 完整治理留待 M2（当前以 execution_uncertain 标注）。

### Rollback
- `git revert 1a199a6`。

### ZCode Conclusion
DONE

### Codex Acceptance
PENDING

### Next Stage
S08

---

## S08 — Data Health / Market Breadth Split

### Status
DONE

### Date
2026-09-18

### Batch
M1

### Starting HEAD
1a199a6

### Ending HEAD / Working Tree
053b4b1（S08 代码提交）

### Files Changed
- `src/stock_analyzer/ops/data_health.py`（新）：七项检查 + Data Health/Market Breadth 分层门 + 灰度开关
- `src/stock_analyzer/runtime/services/asof_backtest_service.py`：week5 日期条目新增 data_health 观测块（enforce=False）
- `tests/test_data_health_gate_s08.py`（新，16 例）

### Behavior Changes
- 新增：数据健康分层（broken/degraded/healthy）+ 分层门；缺失不得 healthy（含 breadth artifact）；coverage 坏但广度高分不得放行；灰度默认只观测
- 未变：live 决策路径与广度 artifact 生成链路（生产接线属部署灰度期）

### Tests
Commands:
- `python -m pytest tests/test_data_health_gate_s08.py` → 16 passed
- `python -m pytest tests/test_asof_backtest_service.py tests/test_week5_historical_backtest.py tests/test_api_backtest.py` → 42 passed

Results: S08 相关 58 passed / 0 failed

### Audit Artifacts
- `artifacts/alpha_v2/audit/s08_validation.json`

### Deferred Findings
- DF-S08-001（medium）：生产侧 Data Health 数据源接线与 enforce 开启需按灰度（≥5 生产日 + ≥60 日回放）。
- DF-S08-002（high）：live 路径的广度缺失 fail-open 语义接线属部署灰度期。

### Rollback
- `git revert 053b4b1`。

### ZCode Conclusion
DONE

### Codex Acceptance
PENDING

### Next Stage
S09

---

## S09 — Model / Label Semantic Guard

### Status
DONE

### Date
2026-09-18

### Batch
M1

### Starting HEAD
053b4b1

### Ending HEAD / Working Tree
d0a58f7（S09 代码提交）

### Files Changed
- `src/stock_analyzer/models/semantics_guard.py`（新）：output_kind 映射 + 展示口径守卫 + legacy 标记
- `src/stock_analyzer/runtime/services/asof_backtest_service.py`：week5 日期条目新增 model_semantics 块
- `tests/test_semantics_guard_s09.py`（新，13 例）

### Behavior Changes
- 新增：语义声明（label 契约 + output_kind + 展示词白/黑名单）与守卫 API；未登记 basis 不抛异常但禁止概率化文案
- 未变：任何模型输出值、打分、cross review、阈值、在服模型（不反转、不替换）

### Tests
Commands:
- `python -m pytest tests/test_semantics_guard_s09.py` → 13 passed
- `python -m pytest tests/test_asof_backtest_service.py tests/test_week5_historical_backtest.py tests/test_api_backtest.py` → 42 passed

Results: S09 相关 55 passed / 0 failed

### Audit Artifacts
- `artifacts/alpha_v2/audit/s09_validation.json`

### Deferred Findings
- DF-S09-001（medium）：生产 label basis `soup_10d_tp8_before_sl5` 未登记 → 在服语义 unknown（方向安全），M2 登记或扩展前缀规则。
- DF-S09-002（medium）：UI/飞书文案层替换属 M2（S21）。

### Rollback
- `git revert d0a58f7`。

### ZCode Conclusion
DONE

### Codex Acceptance
PENDING

### Next Stage
S10

---

## S10 — Decision Log + Outcome Maturation

### Status
DONE

### Date
2026-09-18

### Batch
M1

### Starting HEAD
d0a58f7

### Ending HEAD / Working Tree
be28ff9（S10 代码提交）

### Files Changed
- `src/stock_analyzer/alpha_v2/decision_log.py`（新）：决策/outcome/manifest 落盘 + 成熟计算
- `tests/test_decision_log_s10.py`（新，13 例）

### Behavior Changes
- 新增：`decisions|outcomes|manifests/YYYY/MM/` 契约；V2 Head 未实现字段恒 `not_available`；outcome 只在成熟日之后写（signal 当天 0 行）；入场用 S02 的 T+1 可成交开盘 + raw 价格；不可成交不伪造收益
- 未变：未接生产夜扫（属 M2 S20）

### Tests
Commands:
- `python -m pytest tests/test_decision_log_s10.py` → 13 passed

Results: S10 相关 13 passed / 0 failed

### Audit Artifacts
- `artifacts/alpha_v2/audit/s10_validation.json`

### Deferred Findings
- DF-S10-001（medium）：决策日志接入生产夜扫属 M2（S20）。
- DF-S10-002（medium）：outcome 成熟调度接入属 M2（S20/S21）。

### Rollback
- `git revert be28ff9`。

### ZCode Conclusion
DONE

### Codex Acceptance
PENDING

### Next Stage
M1 批次验收（Codex）


---

# 13. M2 批次（Alpha Research & Shadow，S11–S23）

## M2 Batch Status

```text
Batch = M2（S11-S23）
Batch Status = DONE（等待 Codex 整批验收）
Stages Completed = S11 S12 S13 S14 S15 S16 S17 S18 S19 S20 S21 S22 S23
Stopped At = （无）
Starting HEAD = 43e91cc266027bca3fa7f5b78a73929d741b03e9（= M1 最终验收基线）
Ending HEAD = 工作区未提交（未 push；见 §13.3 变更清单）
Codex Batch Acceptance = PASS（工程实现层；外部独立验收结论，用户转达，2026-09-18 转录）
Research Evidence Status = 60D AVAILABLE / 120D·250D AWAITING_DATA（见 §13.5）
Production Promotion = LOCKED
```

前提确认：M1 经 Codex 第三轮独立验收 **PASS**，M1 基线 `43e91cc` 未回退、未重做。

## 13.1 Stage Matrix（M2 全量）

| Stage | Task | ZCode | 定向测试 | 审计工件 | Codex | Research |
|---|---|---|---|---|---|---|
| S11 | Label V2（多 horizon 可执行 outcome） | DONE | 34 passed | `s11_validation.json` | PASS | 早期证据 |
| S12 | Benchmark 体系（Eligible/Quality/Style） | DONE | 17 passed | `s12_validation.json` | PASS | 早期证据 |
| S13 | Winner Recall | DONE | 14 passed | `s13_validation.json` | PASS | 早期证据 |
| S14 | Feature Availability / Leakage Audit | DONE | 21 passed | `s14_validation.json` | PASS | 已完成 |
| S15 | Simple Factor Baseline | DONE | 34 passed | `s15_validation.json` | PASS | 早期证据 |
| S16 | Shared Feature Matrix + Multi-Head | DONE | 25 passed | `s16_validation.json` | PASS | 早期证据 |
| S17 | Cross Review V2（分歧观测） | DONE | 19 passed | `s17_validation.json` | PASS | 早期证据 |
| S18 | Final Decision Policy V2 Shadow | DONE | 22 passed | `s18_validation.json` | PASS | N/A |
| S19 | Purged Walk-Forward / Clean OOS | DONE | 24 passed | `s19_validation.json` | PASS | 早期证据 |
| S20 | Legacy vs V2 Shadow Dual Run | DONE | 23 passed | `s20_validation.json` | PASS | AWAITING_DATA |
| S21 | Daily Alpha Health Report | DONE | 23 passed | `s21_validation.json` + `.md` | PASS | AWAITING_DATA |
| S22 | NAS Performance Hardening | DONE | 18 passed | `s22_validation.json` | PASS | 本地实测 |
| S23 | Theme / News / Intraday 增量框架 | DONE | 19 passed | `s23_validation.json` | PASS | AWAITING_DATA |

定向测试合计 **293** 例（0 failed）。

## 13.2 新增文件（按模块）

```text
src/stock_analyzer/alpha_v2/research/__init__.py
src/stock_analyzer/alpha_v2/research/panel.py                PIT 日线面板 + 价格口径认证
src/stock_analyzer/alpha_v2/research/outcomes.py             S11 Label V2
src/stock_analyzer/alpha_v2/research/metrics.py              统一评价指标（共用）
src/stock_analyzer/alpha_v2/research/benchmarks.py           S12 三层基准 + 风格匹配
src/stock_analyzer/alpha_v2/research/winner_recall.py        S13
src/stock_analyzer/alpha_v2/research/feature_audit.py        S14 特征可用性/穿越审计
src/stock_analyzer/alpha_v2/research/factors.py              S15 可解释因子族
src/stock_analyzer/alpha_v2/research/simple_baseline.py      S15 基线组合与 ML 对照
src/stock_analyzer/alpha_v2/research/multi_head.py           S16 共享矩阵 + 四 Head
src/stock_analyzer/alpha_v2/research/cross_review_v2.py      S17 分歧观测
src/stock_analyzer/alpha_v2/research/decision_policy.py      S18 Shadow 策略
src/stock_analyzer/alpha_v2/research/purged_walk_forward.py  S19
src/stock_analyzer/alpha_v2/research/shadow_dual_run.py      S20 双轨 + 台账接线
src/stock_analyzer/alpha_v2/research/health_report.py        S21 八块日报
src/stock_analyzer/alpha_v2/research/perf.py                 S22 性能/预算/determinism
src/stock_analyzer/alpha_v2/research/experiments.py          S23 增量实验框架
scripts/alpha_v2_research_run.py                             M2 全链路跑批（审计工件生成）
tests/_alpha_v2_research_helpers.py                          M2 测试共用夹具
tests/test_alpha_v2_s11..s23_*.py                            13 个阶段测试文件
```

对既有文件的**增量**改动（不改语义）：

```text
src/stock_analyzer/backtest/matcher.py       新增 apply_price_tick 公开透传（研究侧复用同一 tick 规则）
src/stock_analyzer/alpha_v2/decision_log.py  新增 read_decision_rows / read_outcome_rows（S20/S21 复核用）
```

## 13.3 行为变化

**新增**：PIT 研究面板与价格口径认证；多 horizon 可执行 outcome；三层基准 + 风格匹配对照；
winner recall；特征可用性登记表与 Base V2 准入断言；可解释因子基线与 ML 公平对照；
共享矩阵多 Head（Rank/Return/Direction/Risk）+ OOS isotonic 校准；分歧观测量与证据块；
Shadow 策略与双轨报告；purged walk-forward；每日健康报告（八块）；性能/预算/determinism 证据；
Theme/News/Intraday 增量实验门。

**Legacy 不变量（未改）**：`final_signal_min_threshold=70`；Cross Review 四阈值；
night 300/100/50 与 final cap；风险门；在服模型与 challenger；飞书正式通知；
`alpha_v2.enabled=false` / `shadow_only=true` / `enforce_final_selection=false`。

## 13.4 施工中发现并修复的真实缺陷

1. **PIT 股票池列名错配**（`panel.pit_universe`）：向 M1 `build_pit_stats` 传 `trade_date`，
   而后者只认 `date` → stats 静默变空，整个股票池被误判成 `future_listed`（eligible=0）。
   修复：显式改名 `trade_date → date`，并补 4 条 PIT 池回归测试。
2. **基准合并产生 `_x/_y` 后缀**（`merge_primary_excess`）：先按原名取列再 merge，
   `benchmark_return_*d` 已存在 → 合并后被改名，随后 `KeyError`。修复：合并前统一清掉同名旧列。
3. **评价口径对缺列不 fail-closed**（`metrics.usable_mask`）：指标列整体缺失时
   `pd.to_numeric(None)` 变标量 → 抛 `AttributeError`。修复：缺列即返回全 False 掩码。
4. **S19 训练集泄漏 20 行**（本次自查最有价值的发现）：日历口径 purge 假设
   「决策日 + purge 个交易日即成熟」，而真实成熟日按**标的自身 bar 序列**推进，
   停牌/停更的票会越界。修复：在 `run_fold` 增加按真实成熟日的二次 purge
   （`maturity_purged_rows` 如实计数，`purge_adequacy` 标 `calendar_purge_insufficient`），
   独立复核保持原样（`lookahead_violations` 现在是 0 而不是被掩盖）。
5. **特征审计把身份列当特征**：`decision_date`/`symbol` 混进「未登记列」。修复：
   显式 `IDENTITY_COLUMNS` 跳过。

## 13.5 研究样本门状态（本地跑批口径，非生产）

本地证据运行：`artifacts/warehouse/market.duckdb`，窗口 2025-06-02→2026-03-31，
400 只票、202 交易日、132,947 根 bar、80,800 条决策（主样本 79,355）。
价格口径：行内无声明 → 实测一致性探针认证为 `raw`（超限比例 6.8e-05）。

```text
20D Alert       = AVAILABLE（197 成熟决策日 > 20）
60D Research    = AVAILABLE（197 > 60，仅"第一轮方向判断"）
120D Advisory   = 未达（S19 clean OOS 60 日；S16/S21 的 197 日非 clean OOS 口径）
250D Governance = AWAITING_DATA
```

关键读数（诚实记录，不修饰）：

```text
S16/S21  alpha_rank Rank IC：3D +0.049 / 5D +0.052 / 10D +0.048 / 15D +0.040（197 日）
S21      Top5 5D 超额 +0.0041/日（命中率 47.1%）；Top1 5D 超额 −0.0138（命中率 46.4%）
S21      分位单调性 ρ=+0.60，top−bottom +0.0013（弱单调）
S15      Simple Baseline 5D IC −0.022（CI 跨 0，本窗口无效）
S16      ML vs Baseline：ic_delta +0.074、topk_delta +0.0022 → ml_beats_baseline
S19      Purged WF：3 折、泄漏 0、pooled IC +0.016、CI[−0.018,+0.057] → INCONCLUSIVE
S13      Winner Recall：light 0.427 / deep 0.220 / final 0.029（研究代理漏斗）
S14      Base V2 安全特征 120 列 / 排除 88 列 / 未登记 0 / 常数疑似 101 列
S17      分歧高−低 = −0.00065（仅 36 成熟日 → observation_only）
S23      theme 可实验但样本不足；news/intraday 前置条件阻断
```

**结论表述边界**：以上是**本地开发窗口**读数，且该窗口已被反复用于开发与选择，
**不是 clean OOS**；`INCONCLUSIVE` 与 `AWAITING_DATA` 是正确状态，
不得据此宣称模型有效或进入 Advisory/Production。

## 13.6 M2 新增的 Deferred Findings

| ID | 严重度 | 内容 | 目标 |
|---|---|---|---|
| DF-M2-001 | medium | 研究代理漏斗（流动性 top-N）与生产真实 300/100/50 不是同一件事；`quality_pool_source=research_proxy:alpha_v2_quality_v1` 已如实标注，真实成员需生产链路注入 | NAS/部署期 |
| DF-M2-002 | medium | 本机 `peak_rss_mib` 在 Windows 无可读 `/proc` → `unavailable`；内存预算判定需在 NAS(Linux) 复核 | NAS |
| DF-M2-003 | medium | 101/208 特征列在本窗口近乎常数或近全空（背景/资金/分钟组），与 Phase 2 归因的「98 特征全 NaN」同源；这些组已排除出 Base V2，但上游回填仍需治理 | 数据侧 |
| DF-M2-004 | low | `alpha_target_5d` 采用截面 rank 分位作为排序目标；与 `return_rank v3` 生产标签语义（top/bottom 分位阈值）未在本轮统一 | M3 |
| DF-M2-005 | low | S18 Shadow 候选池依赖研究侧 `deep_pool` 名次列；真实 Deep50 成员需从夜扫产物注入后才算生产同口径 | NAS |
| DF-S07-001 | high | **仍未闭合**：本机受跟踪配置 `execution_spec.price_series_mode=qfq`；本轮用实测探针认证了 `market_duckdb` 面板为 raw，但生产 vendor 链路口径仍需在 NAS 核验 | NAS |
| DF-S06-001 | medium | **仍未闭合**：无逐日激活历史 → `strict_production_replay` 仍基本恒 `unscorable`（M2 未涉及该链路） | NAS |
| DF-S06-002 | medium | **仍未闭合**：asof（非 week5）回测的 as_of 闸门未接（M2 未改动该路径） | M3 |
| DF-S08-002 | high | **仍未闭合**：live breadth 缺失 fail-open 的生产接线属部署灰度期 | NAS |
| DF-S10-001/002 | — | 决策日志与 outcome 成熟**接线能力已在 S20 实现**（`build_shadow_decision_rows` / `mature_shadow_outcomes` + 测试），但**尚未接入生产调度**（需部署授权） | NAS |

## 13.7 回滚

- 整批：删除 `src/stock_analyzer/alpha_v2/research/`、`scripts/alpha_v2_research_run.py`、
  `tests/_alpha_v2_research_helpers.py`、`tests/test_alpha_v2_s11..s23_*.py`
  与 `docs/alpha_v2/M2_Implementation_Report.md`（全部为新增文件）；
- 增量改动：`git checkout -- src/stock_analyzer/backtest/matcher.py src/stock_analyzer/alpha_v2/decision_log.py`
  （两处均为纯新增函数，回滚不影响 M1 与 Legacy）；
- **无生产影响**：未 push、未部署、未重启容器、未改 `.env`、未切 serving model、未 promote、
  未开启 `alpha_v2.enforce_final_selection`；`artifacts/alpha_v2/**` 为运行时产物。

## 13.8 Codex Acceptance

PENDING —— 建议重点复核见 `docs/alpha_v2/M2_Implementation_Report.md` §32。


---

# 14. 验收记录（外部独立验收结论的转录）

> **本节的 `PASS` 全部来自外部独立验收，经用户转达后转录**；
> ZCode **没有**、也不能自行把 Codex Acceptance 写成 PASS（两阶段提示词 §15）。
> 转录只做「记录 + 标注来源与日期」，不改变任何施工内容。

## 14.1 M1（第三轮）

```text
M1 Verdict = PASS（第三轮最终）
Permission To Proceed To M2 = YES
Reviewed HEAD = 43e91cc266027bca3fa7f5b78a73929d741b03e9
基线测试 = 3204 passed / 2 skipped / 0 failed（施工提示词记录）
```

## 14.2 M2（第二批）

```text
Batch Verdict = PASS（工程实现层）
Engineering Verdict = PASS
Blocking Findings = 无
Research Gate = 60D AVAILABLE / 120D·250D AWAITING_DATA（与蓝图要求一致）
Production Promotion = LOCKED
```

独立验收做过的三层证据（转述）：

1. **读代码核实不变量**：S11 `executable=False` 全部收益列恒 `not_available`；
   S12 风格窗口止于决策日且排除自身；S13 自证循环防护落在代码里；
   S14 黑名单优先于分组匹配；S16 矩阵指纹断言与校准窗互斥；
   S17/S18/S20/S21 的结构守卫均在实现而非文档层；S19 双口径 purge 真实生效。
2. **重跑测试**（非引用施工方数据）：

```text
M2 定向（13 文件）        = 293 passed / 0 failed
M1 回归定向（7 文件）     = 105 passed / 0 failed
批次级全量（-n 4 loadfile）= 3496 passed / 2 skipped / 0 failed
（验收方临时加入 10 条独立反例探针后为 3506 passed；剔除后与报告数字精确吻合）
独立反例探针              = 10 passed
ruff（M2 全部文件）       = All checks passed
```

3. **自编对抗探针**（按验收门禁 §13 抽查）：预测分数定义赢家必须抛错、
   未证明特征进 Base V2 必须抛错、Head 指纹不一致必须抛错、
   随机切分必须抛错、自动动作必须被拒、`enforce_final_selection=True` 必须抛错、
   `not_available` 不被编造 —— 全部符合预期。

其他核查结论：`matcher.py` / `decision_log.py` 两处改动确认为纯增量；
凭据扫描零命中；审计工件 15 件齐备且关键字段与报告一致；
报告把 197 日标为「开发窗口、非 clean OOS」，`INCONCLUSIVE` / `AWAITING_DATA`
未被包装成 PASS。

## 14.3 非阻塞观察（验收方提出，不要求返工）

| # | 观察 | 本轮处置 |
|---|---|---|
| 1 | M2 成果全部未 commit（工作区 21 项变更）；建议验收通过后按「方式 B」做批次 commit 固化，否则后续改动会与「已验收内容」混在一起 | **待用户授权**（见 §14.4） |
| 2 | M1 基线计数分歧：M1 报告 §21 记 3202 vs 施工提示词记 3204，两者均 0 failed | 已在本节与 M2 报告 §21 双处如实标注；**下批复核一次**（当前树已含 M2 改动，无法回溯复算历史状态） |
| 3 | 本节所在的 §2 首轮矩阵 Codex 列曾是 `PENDING`（文本段落已写 PASS，表格未回写） | **已修**：§12 最终矩阵、§13.1 M2 矩阵、§6 验收记录、§0 状态表全部回写，§2 首轮快照加指针说明 |
| 4 | 遗留 Deferred（DF-S07-001 生产价格口径、DF-S06-001/002、DF-S08-002、DF-M2-001/003/005 等）属 NAS / 数据侧验证 | 本就在 M2 范围外，已在 §8 / §13.6 登记，不因验收 PASS 而消失 |

## 14.4 待决事项（需要用户决定，ZCode 不擅自执行）

```text
1) M2 工作区按「方式 B」做批次 commit 固化 —— 需用户明确授权
   拟纳入（21 项）：docs/alpha_v2/{PROGRESS.md, M2_Implementation_Report.md}
                    scripts/alpha_v2_research_run.py
                    src/stock_analyzer/alpha_v2/research/**（18 个新模块）
                    src/stock_analyzer/alpha_v2/decision_log.py（+2 只读函数）
                    src/stock_analyzer/backtest/matcher.py（+1 透传方法）
                    tests/_alpha_v2_research_helpers.py
                    tests/test_alpha_v2_s11..s23_*.py（13 个文件）
   不纳入：docs/system_issues_for_review_20260917.md（用户未跟踪文件，全程未动）
           artifacts/alpha_v2/**（.gitignore 内，运行时产物）
2) 是否把 DF-M2-003（101/208 特征列近乎常数/近全空）排入数据侧治理
3) 生产部署 / 模型切换 / 阈值调整 —— 仍未授权，不在本轮考虑范围
```

## 14.5 Legacy 与生产不变量（验收后仍成立）

```text
final_signal_min_threshold = 70            未改
Cross Review 四阈值                         未改
night 300/100/50、final cap 5              未改
serving model / challenger                 未切换、未 promote
alpha_v2 三开关                             enabled=false / shadow_only=true / enforce_final_selection=false
git push / 部署 / 容器重启 / .env 修改       均未执行
```

---

# 15. M3 — Production Shadow Validation & Clean OOS（冻结纪律 + 验证链）

> 2026-09-18 起施工；生产部署 / git push / 模型权重阈值调整均未授权未执行。
> 冻结基线 `55c7592fe88ac3b3ab5b233df422b0f528141074`（= M1+M2 合并验收锚点）。

## 15.1 新增能力（全部工程态，证据可复现）

- `alpha_v2/validation/freeze.py`：Validation Freeze Manifest（冻结 22 个基本面 + 嵌套 model/benchmark/policy_freeze）+ canonical sha256。
- `alpha_v2/validation/epoch.py`：epoch 注册表——单开、原因闭、关闭后写入拒绝、身份对账。
- `alpha_v2/validation/frozen_model.py`：冻结 Shadow 模型工件（17 个目标的 lgbm 原生 booster + OOS isotonic 校准器 + `model_manifest.json`，含 per-file sha256 与聚合 artifact_hash；加载逐文件验证）。
- `alpha_v2/validation/shadow_capture.py`：T 日预测冻结（全套 M3 §6 字段）+ 同键改写逐字段一致校验（`ShadowTamperError`）+ `missing_prediction_day` 台账。
- `alpha_v2/validation/outcome_maturation.py`：日更成熟回填（S11 T+1、raw 价、成本、MAE/MFE、四层冻结基准并行落列、信号当天 0 行、重述防护）。
- `alpha_v2/validation/validation_kpis.py`：M3 KPI（Top1/3/5 命中+均值+中位+三层超额、Rank IC 3/5/10/15 与 20D/60D、十分位单调、winner recall、下行 MAE/5%tail/止损命中、T+1 成交/缺口/滑点、样本门 + alpha_verified=False/permanent LOCKED 品牌词）。
- `alpha_v2/validation/feature_diagnosis.py`：DF-M2-003 五分类诊断 + market.duckdb 上游源探针。
- CLI 六件套：`alpha_v2_validation_freeze.py / alpha_v2_shadow_model_freeze.py / alpha_v2_shadow_capture.py / alpha_v2_shadow_mature.py / alpha_v2_validation_report.py / alpha_v2_feature_diagnosis.py`。

## 15.2 施工中发现并修复的真实缺陷

| ID | 位置 | 缺陷 | 证据 |
|---|---|---|---|
| DF-M3-B1 | `research/benchmarks.py::_style_control_group` | 风格 kNN 的"排除自身"把组内全局行号当成块内偏移:组规模 > 512 即 IndexError；M2 的 400 票小窗从未踩到 | `tests/test_alpha_v2_m3_benchmark_block_bug.py`（含暴力对照、block 不变性） |
| DF-M3-B2 | `validation/frozen_model.py` | 派生目标列（mae_le_5pct_{10,15}d）不匹配 S14 黑名单前缀、被误吸为特征 | `fit_frozen_model` 内安全列准入前预过滤 + 回归 |

## 15.3 NAS 只读核验（§15 A–E）

证据文件：`artifacts/alpha_v2/audit/nas_readonly_verification_20260918.json`。

- A Registry：21 行/0 冠军/17 revoked 指 dataset manifest；serving=model_v1.json hash 71f64a21c131…= registry 中 `model_v3_6d7486bc1af6`；**serving_manifest.json 在生产不存在**（M1 能力未部署）。
- B 执行价：**SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE=raw ✓**（DF-S07-001 生产侧闭合证据）。
- C Breadth：`market_breadth.json` 当前生产不存在（fail-open 态：缺失≠健康）；`nightly_data_ready.json` 2026-09-18 12:32Z 正常。
- D/E 决策日志 + 成熟调度：生产无对应 job（预期内，部署后接通）。

## 15.4 DF-M2-003 诊断结果（本批只做诊断，不改特征）

窗口 2025-06-02→2026-03-31 / 400 票 / 208 列：

```text
FILL_ZERO_ARTIFACT   = 99（模式 0、占比≈1、零覆盖=1.000）
REAL_CONSTANT        = 2  （block_trade_frequency_20 / background_completion_score）
UNKNOWN              = 107（含全部正常技术特征）
```

上游探针：`moneyflow_net_amount / hk_hold_* / inst_net_amount / block_trade_amount` 在
`daily_bars` 里近乎零填充（270 / 4 / 0 / 8 行），`northbound_net` 97.7% 恰为 0 值——
99 个"常数"的本质是上游未填充被工程师末端 `fillna(0.0)` 伪造成 0。
数据侧治理决定（回填或删除）必须走"关 epoch → 新 epoch"，本批不动。

## 15.5 本地排练（machinery proof，不是 clean OOS）

```text
冻结模型训练窗: 2025-06-02 → 2026-02-13（再 -60 交易日校准窗）
冻结工件:      artifacts/alpha_v2_rehearsal/model/alpha_v2_shadow_epoch_001/
              （artifact_hash=5978ef5d…；17/17 目标全训成功）
冻结清单+epoch: artifacts/alpha_v2_rehearsal/validation/{validation_freeze_manifest.json, epochs.json}
T 日快照:      2026-02-27（Deep50 50 行；预测字段非 not_available 率 100%）
成熟:          evaluation=2026-03-31 → 50 行 × 3/5/10/15D 全部成熟
KPI 报告:      reports/validation_kpi_alpha_v2_epoch_001_20260919.{json,md}，
               rank_ic 5d=+0.19（单日，不构成结论）；
               alpha_verified=False / production_promotion=LOCKED；样本门全 False
```

## 15.6 测试（初版口径有误，已更正为 junit 为准）

```text
M3 定向             = 9 文件 / 76 passed / 0 failed / 0 skipped
                      （初版误写 50：把 shadow_capture 记 9=实 8、kpis 记 5=实 4；
                        Readiness 旧文另有 7 文件/52 例的笔误。修复轮新增 enforcement 26 例。
                        数字以 junitxml 实测为准）
全量回归（-n4 loadfile）= 3574 collected / 0 failed / 0 errors / 2 skipped
                      （3574 = 3498 基线 + 76 M3）
ruff                  = 全部 M3 改动文件 All checks passed
```

> **以上是 F1–F7 修复轮（R2）的历史快照，保留不覆盖。** R3 最终口径（权威）：
> M3 定向 = 10 文件 / **112** collected / **112** passed / 0 failed / 0 skipped；
> 全量 = **3610** collected / **3608** passed / 0 failed / 0 errors / 2 skipped
> （见 §15.10.4 / §15.11.2 与 `artifacts/alpha_v2/audit/m3_batch_regression.json`）。

## 15.9 M3 Blocking Fix Round（2026-09-19）：F1–F7 代码落地

Codex 首轮验收 M3 判 FAIL（含 B1–B8；其中"脏树/旧 SHA 冒充生产 code identity"与
"T 日快照可事后创建"属于明确"不许 CONDITIONAL PASS"级别）。本轮未 commit 未 push，
仅以工作区修复 + 测试 + 演练留证：

```text
F1  shadow_capture.py   T 日写入窗口强制（capture_date/backfill）；补写必落
                        backfilled=true 且 clean_oos_eligible=false；missing/快照冲突双向拒绝
F2  epoch.py            身份严格对账 require_epoch_identity_match（缺失即违例）；
                        shadow/missing/mature 三路径接入
F3  outcome_maturation.py closed epoch 先于面板加载即拒；影子外 symbol 拒绝写入
F4  freeze_precheck.py 生产五项硬门（raw/脏树/build 身份/窗口/schema），
                        rehearsal 模式显式单开（清单落 validation_mode=rehearsal）
F5  runtime_identity.py porcelain 语义修正（空输出=干净）；构建身份对账
F6  validation_kpis.py  Clean OOS 两层资格分块 + 治理计数；严格 JSON（NaN→not_available）
F7  scripts/…           capture 侧 deep_rank 修正（名次 1..N + deep_rank_pct）；
                        quality_pool_source 不再绕回 proxy
```

附证据（$TEMP/m3_acceptance/）：
- `rehearsal_a_freeze_gates.py` → 7 项生产门禁场景全 PASS（干净+raw 才可写清单开 epoch）；
- `rehearsal_b_capture_mature.py` → 10 项 fail-closed 场景全 PASS（含 manifest 替换、
  closed epoch、T+1 补写、missing 冲突、backfill 治理、严格 JSON）。

## 15.7 交付约束边界（本批始终遵守）

```text
- 未 git push、未部署 NAS、未重启容器、未改 .env、未切 serving model、未 promote
- alpha_v2.enabled / shadow_only / enforce_final_selection 仍为 false/true/false
- Legacy 阈值 70 / cross review / 300-100-50 / 风险门零改动
- 用户文件 docs/system_issues_for_review_20260917.md 保持未跟踪、未纳入任何产物
```

## 15.8 验收请求要点（供 Codex M3 独立验收参考）

1. epoch 双开 / 关闭后再写 / 同 id 重开 —— 都必须被拒（test_alpha_v2_m3_epoch.py）
2. 同日快照篡改必须被拒（test_rewrite_with_different_prediction_is_refused）
3. 信号当天写 outcome 必须是 0 行（test_signal_day_writes_nothing）
4. 已成熟 outcome 重算不一致必须抛（test_restatement_is_refused）
5. 冻结清单 hash 被改必须 fail（test_tampered_manifest_fails_integrity）
6. 训练/校准窗重叠必须被拒（test_train_calibration_overlap_is_refused）
7. KPI 的 alpha_verified=False / production_promotion=LOCKED 没有条件可写成 True
8. benchmarks 分块修复的暴力对照回归（刚入库的 R1）

## 15.10 M3 Final Blocking Fix R3（2026-09-19）：BLK-R2-1 / BLK-R2-2 / N-R2-1

### 15.10.0 验收历史（保留，不覆盖）

```text
M3 Acceptance Round 1（外部独立验收）  = FAIL（B1–B8）
M3 Recheck Round 2（独立复核）         = FAIL（BLK-R2-1 伪造 capture_date / BLK-R2-2 构建身份双源未同时校验）
M3 Final Blocking Fix R3（修复落地）   = 完成（BLK-R2-1 / BLK-R2-2 / N-R2-1）
Codex Recheck（Round 3，外部独立）      = PASS（2026-09-19）
```

> Round 1 FAIL / Round 2 FAIL 是历史结论，保留不覆盖；Round 3 复核的权威计数与
> 封存状态见 §15.11。

### 15.10.1 BLK-R2-1 写入日 = 真实墙钟

- `write_shadow_snapshot` **删除** `capture_date` 参数（攻击面消失）；写入日取 `wall_clock_now()`；
- 每行落 `actual_capture_date`（真实写入日）与 `deterministic_clock`（是否用了确定性时钟）；
- **production 永久不可注入时钟**（无任何开关）：`capture_clock_policy` 唯一条件 = `validation_mode != production`，
  否则 `ShadowClockNotAuthorizedError`；生产 freeze CLI 只产 production / rehearsal；
  测试套件用 `validation_mode="test"`（与 production 同语义、可进 clean，CLI 产不出）；
- capture CLI：生产模式 `--capture-date` → exit 8；无 flag 且 `signal_date != 真实今天` → exit 8；
- 显式 backfill 固定 `backfilled=true / clean_oos_eligible=false / backfill_reason` 非空，
  并保留 `signal_date / recorded_at / actual_capture_date`；
- KPI 新增第二道闸：行 `recorded_at`（与 `actual_capture_date`）必须与 `signal_date` 同日，
  否则该日不进 clean 且 `reason=late_recorded_at`；治理块新增 `late_recorded_days`。

### 15.10.2 BLK-R2-2 构建身份四值一致

- `assert_build_identity` 改为四值硬门：`git HEAD == requested == .build_commit == build_manifest.commit`；
- 两源**都必须存在且可解析**；`build_manifest` 必须 `trusted=true`、`dirty=false`；任一不满足 exit 5；
- `runtime_identity.read_build_manifest_file`（存在性即证据，不再环境变量兜底）+ `build_manifest_present`；
- freeze CLI 把 `build_identity` 段写进清单（纳入 `freeze_manifest_hash`）；
- 唯一例外：容器内 git 不可得时必须 `--code-commit` 显式给值 + 两源互证，来源记
  `cli_override_git_unavailable`。

### 15.10.3 N-R2-1 capture 侧 data_health 接线

- 新增 `validation/data_health_capture.py`：`capture_data_health_block`（同日 + 状态映射）与
  `data_health_gate_ok`（**唯一一份** clean 判定，捕获与 KPI 共用）；
- 新增 `scripts/alpha_v2_data_health_snapshot.py`：按 S08 契约产当天工件（拿不到的输入不喂 →
  S08 判 degraded → 不进 clean）；
- capture CLI 新增 `--data-health`；行与当日清单写结构块；缺失/陈旧/降级 → 记录照写、clean=false、
  样本门不推进。

### 15.10.4 验证实绩

```text
M3 定向   = 10 文件 / 112 collected / 112 passed / 0 failed / 0 skipped（junit 实测；R2 口径为 9 文件 / 76 例）
全量回归   = 3610 collected / 3608 passed / 0 failed / 0 errors / 2 skipped
             （junit 实测；见 artifacts/alpha_v2/audit/m3_batch_regression.json，R3 刷新）
Attack A  = 20 天固定时钟注入 20/20 被拒；生产 CLI --capture-date exit 8；clean_oos_days=0、
            backfilled_days=20、failure_alert(20).reached=False
Attack B  = 四值一致 PASS；缺 .build_commit / 缺 build_manifest / 双源矛盾 / git HEAD 不符 /
            requested 不符 / trusted=false / dirty=true 全部 FAIL（每条 exit 5）
data_health = 同日 healthy → clean；missing / not_available / stale / degraded → 写快照但不进 clean
```

### 15.10.5 新增部署前置（比 §15 原口径更严）

```text
- production freeze 需要 .build_commit 与 build_manifest.json **同时**存在、可解析、
  trusted=true、dirty=false，且与 git HEAD / 冻结清单 code_commit 四值一致；
- capture 之前必须先产当天 data_health 工件（scripts/alpha_v2_data_health_snapshot.py），
  否则该日 data_health=not_available → 不进 clean OOS。
```

### 15.10.6 本轮边界

```text
未 commit / 未 push / 未部署 / 未重启容器 / 未改 .env / 未切 serving model / 未 promote；
未动 feature set / label / benchmark / alpha rank / 模型参数 / Legacy 70 / cross review。
```

## 15.11 M3 外部独立验收结论与封存状态（2026-09-19）

### 15.11.1 验收历史（保留，不覆盖）

```text
M3 Acceptance Round 1（外部独立验收）  = FAIL（B1–B8）
M3 Recheck Round 2（独立复核）         = FAIL（BLK-R2-1 伪造 capture_date / BLK-R2-2 构建身份双源未同时校验）
M3 Final Blocking Fix R3（修复落地）   = 完成（BLK-R2-1 / BLK-R2-2 / N-R2-1）
M3 Round 3（外部独立最终复核）          = PASS
```

### 15.11.2 Round 3 复核的权威计数（junit 实测为准）

```text
M3 定向    = 10 文件 / 112 collected / 112 passed / 0 failed / 0 skipped
全量回归    = 3610 collected / 3608 passed / 0 failed / 0 errors / 2 skipped
              （2 skipped = NAS bash 语法检查，仅 Linux CI 执行；
                见 artifacts/alpha_v2/audit/m3_batch_regression.json）
冻结基准层 = 3（eligible_ew / quality_pool_ew / style_matched）+ primary = quality_pool_ew
              simple_baseline 由 KPI 层做同日配对，**不属于 frozen benchmark layer**
```

### 15.11.3 封存状态

```text
M3 Engineering Acceptance           = PASS
M3 Accepted Baseline Commit         = 本次本地封存 commit（SHA 见该 commit 记录）
Production Shadow Deployment        = NOT_STARTED
Production Deployment Authorization = NOT_EXECUTED

BLK-D1 = OPEN（生产容器无 git、无 /app/.build_commit → production freeze 不可执行；容器形态实测 exit 5）
BLK-D2 = OPEN（capture / mature 的 code_commit 在容器内恒为 unknown → 运行身份闸 exit 3）
  ↑ 二者属后续独立的 Production Runtime Identity Hardening，不在 M3 封存范围内。

Clean OOS Days       = 0
Alpha Verified       = FALSE
Production Promotion = LOCKED
```

### 15.11.4 生产身份口径（禁止混用）

```text
M3_ACCEPTED_BASELINE_COMMIT     = 本批封存 commit（M1+M2+M3 工程基线）
PRODUCTION_SHADOW_FROZEN_COMMIT = PENDING_AFTER_BLK_D1_D2
```

真实验证 epoch（`alpha_v2_epoch_001`）的 freeze manifest `code_commit` 必须指向
**包含 Production Runtime Identity Hardening 的后续最终部署 commit**，
不得用 `M3_ACCEPTED_BASELINE_COMMIT` 冒充生产冻结身份。

---

# 16. Production Runtime Identity Hardening（2026-09-19，BLK-D1 / BLK-D2）

> 阶段性质：生产前硬化，不是 M4，不继续研究 Alpha，不改动模型算法。
> 详细报告：`docs/alpha_v2/Production_Runtime_Identity_Hardening_Report.md`。
> 基线：`M3_ACCEPTED_BASELINE_COMMIT = 33d0f7f97e79215ad8c65f972c0e56eeead10614`。
> 分支：`feat/alpha-v2-production-runtime-identity`（本地，未 commit、未 push、未部署）。

## 16.1 目标

解决 Codex 生产环境实测确认的两项部署域 Blocking，使已 PASS 的 M3 Validation Framework
能在**不含 Git 仓库与 git binary 的生产容器**里安全、可审计地执行
freeze / capture / mature：`BLK-D1`（容器内 production freeze 不可执行，exit 5）、
`BLK-D2`（容器内 capture/mature 运行身份对账必然失败，exit 3）。

## 16.2 根因与修复（共同根因：用错了证据来源）

```text
BLK-D1  旧 freeze CLI 在判定"这是哪种运行环境"之前先要 git 工作区证据，
        容器里 git_worktree_dirt=None → 生产判为"无法证明干净" → 永远 exit 5，
        根本到不了用它自身可证的构建身份。
BLK-D2  capture / mature / model freeze 各自直接调用 git_head()，容器内恒为
        "unknown"，与 epoch 冻结的真 SHA 不符。
```

修复方式（不是放宽校验，而是按 runtime context 选可信来源）：

```text
git_checkout             git HEAD 四值一致 + 工作区可证干净（R3 语义不变）
container_build_identity .build_commit == build_manifest.commit、trusted=true、dirty=false
                         （容器里没有 git checkout，工作区门不适用）
```

四个 CLI 统一走 `runtime_identity.resolve_runtime_code_identity` +
`freeze_precheck.assert_runtime_identity`；新增 AST 级回归闸门禁止它们再调用
`git_head()`；`assert_build_identity` 的实现改为调用唯一的判定函数（不出现第二套规则）。

## 16.3 构建身份（镜像侧）

镜像构建阶段同一次调用产出 `/app/build_manifest.json` 与 `/app/.build_commit`
（两源相等是构造性的）；部署脚本 build 前用 git 现场取证 commit/dirty/built_at，
build 后**从镜像里读出这两个文件复核**（`scripts/verify_container_build_identity.py`），
任一不满足即拒绝继续部署。顺带修掉两个真实缺陷：

```text
1) generate_build_manifest.py 把非布尔来源的 --dirty（如默认 "unknown"）写成布尔 false
   → "只传 commit 不传 dirty" 即可得到 trusted=true；现改为如实写 unknown 且探测失败
   也写 unknown。
2) nas_deploy_update.sh 把 BUILD_DIRTY 写死 0、BUILD_TIME_UTC 只 export 未传进 build
   （镜像里 built_at_utc=unknown）；现改为现场取证 + 显式传递 + build 后镜像内复核。
```

## 16.4 部署顺序修订（原 §17.2 顺序自相矛盾）

原版把"模型冻结"排在"生成冻结清单"之后，而清单步骤又要求 `--model-dir <冻结模型目录>`
——引用一个尚未产出的目录。依赖是代码事实：生产模式 feature schema 必须非空且只能来自
`--model-dir` 工件，`open_epoch` 冻结的 model 身份三键又必须与之后 capture 的运行期身份
一致。**机器可验证形式**：沙箱里不给 `--model-dir` 跑生产 freeze → exit 6。
最终顺序 = 部署/身份复核 → 模型冻结 → 清单+开 epoch → data_health → capture → mature → KPI，
已同步修正 `M3_Production_Readiness_Report.md` §17.2（原顺序保留为"原版（已作废）"）。

## 16.5 证据

```text
NO_GIT_CONTAINER_SMOKE  A–G 七步 PASS（无 .git 沙箱里真跑四个 CLI；每个 CLI 再加一次
                        "破坏身份"对照，按既有退出码被拦下：freeze=5，shadow 三件套=3）
                        证据 artifacts/alpha_v2/audit/no_git_container_smoke.json
真实 Docker 容器        stock-analyzer:local-identity-smoke（身份层与生产 Dockerfile 同构，
                        省略前端阶段）：容器内 command -v git = 空；/app/.build_commit 与
                        /app/build_manifest.json 都在；resolver identity_verified=true、
                        identity_source=container_build_identity；freeze CLI exit 0 并开启
                        epoch；mature exit 0；capture 身份门放行（exit 5 ≠ 3）
                        证据 artifacts/alpha_v2/audit/docker_identity_smoke*.{json,log,sh}
定向测试                tests/test_alpha_v2_production_runtime_identity.py（39 例全过）
M3 定向                 tests/test_alpha_v2_m3_*.py（10 文件 / 112 例全过）
fail-closed 矩阵        容器形态 11 例 + 检出形态 4 例，全部拒绝；CLI 层"无 git 且无构建
                        身份"必须干净 exit 5（不得 traceback）
Legacy 隔离             final_signal_min_threshold / Cross Review / 300-100-50 / cap 5 /
                        serving model / registry / 飞书 / 正式推荐 全部未改动；
                        alpha_v2.enforce_final_selection 仍为 false
```

## 16.6 状态与授权边界

```text
BLK-D1 = FIX IMPLEMENTED, CODEX RECHECK PENDING
BLK-D2 = FIX IMPLEMENTED, CODEX RECHECK PENDING
PRODUCTION_RUNTIME_IDENTITY_HARDENING = READY_FOR_CODEX_RECHECK
PRODUCTION_SHADOW_FROZEN_COMMIT = PENDING_UNTIL_CODEX_PASS_AND_FINAL_COMMIT
                                  （supersedes §15.11.4 的 PENDING_AFTER_BLK_D1_D2）
PRODUCTION_SHADOW_DEPLOYMENT    = LOCKED_PENDING_CODEX_RUNTIME_RECHECK
Production Shadow = NOT_STARTED；alpha_v2_epoch_001 = NOT STARTED；Clean OOS Days = 0

commit = NOT CREATED（按要求本轮不提交）
push = NOT PERFORMED；deploy = NOT PERFORMED；.env = 未修改；scheduler 未接线
```

---

# 17. R4.1 — Frozen Model Provenance Commit Binding（2026-09-20）

> 前情：R4（#16）已由 Codex 独立验收 PASS，BLK-D1 / BLK-D2 CLOSED。
> 独立复核另提一项 Non-Blocking（Case A5）：模型训练 commit 与运行 commit 不一致时没有任何
> 门禁，且改写工件里的训练身份不被任何完整性检查发现。R4.1 只解决这一项。

## 17.1 修复前实测（真实 CLI）

```text
train=A / runtime=B      → validation freeze rc=0 且 epoch 已 open
训练 commit missing/unknown/malformed → freeze rc=0 且 epoch 已 open
改写工件训练身份          → capture rc=0（未被发现）
```

## 17.2 实现（唯一语义，不新增同义字段）

```text
model_training_code_commit  ← 工件 manifest 顶层 code_commit（训练时由统一 Runtime
                              Identity Resolver 取得，无人工入口）
传播：frozen_model_identity_payload → build_validation_freeze 的 model 块（规范化保留）
     → freeze manifest（freeze_manifest_hash 覆盖）→ epoch identity（可审计）
完整性：_artifact_hash 的哈希体纳入 code_commit（改写即工件校验失败）
门禁：freeze CLI（写盘/开 epoch 前，exit 5）、capture / mature（生产形态，exit 3）
非 production（rehearsal/test）不做此门
```

不做：不要求模型每天重训；不支持历史 commit 训练的模型跑到新 commit（需另行设计兼容契约）。

## 17.3 修复后实测（同一套对抗夹具复跑）

```text
Case 1  train=A / runtime=A       → freeze rc=0 + epoch open；身份链 6 项全等（唯一值 1）
Case 2  train=A / runtime=B       → rc=5，epoch_open=False，清单未落盘
Case 3/4/5 missing/unknown/malformed → rc=5，epoch_open=False
Case 6a 篡改 freeze manifest      → freeze_manifest_hash 失锚（保持）
Case 6b 篡改工件训练身份           → artifact_hash 与内容不符 → 拒绝加载（真实工件实测）
```

## 17.4 测试与回归

```text
新增 tests/test_alpha_v2_m41_model_provenance_binding.py（14 例，含真跑 CLI 的 Case2 端到端）
变异测试 3/3 被抓住（哈希不覆盖训练身份 / 门变空操作 / 规范化丢弃字段）
Runtime Identity 定向（39）+ M3 定向（112）= 151 passed / 0 failed
全量回归与 ruff 见下方 §17.5
```


## 17.5 状态

```text
R4   RUNTIME_IDENTITY_HARDENING = PASS（Codex 独立验收）
R4.1 MODEL_PROVENANCE_BINDING   = IMPLEMENTATION DONE / CODEX_MINI_RECHECK = PENDING
FINAL_RUNTIME_HARDENING_COMMIT  = LOCKED_PENDING_MINI_RECHECK
PRODUCTION_SHADOW_DEPLOYMENT    = LOCKED_PENDING_MINI_RECHECK
commit / push / deploy / epoch_001 = 均未执行
```

---

# 18. R4.1.1 — 冻结清单生产门补工件内容完整性校验（2026-09-20，Codex 自审修复）

## 18.1 来源

Codex 在合并 `1df77e9` 前的独立 mini recheck 中判 R4.1 PASS，但给出 1 个中等
（P2）补强点：validation freeze CLI 只读工件 manifest 的**身份字段**、不校验
工件**内容完整性**——实测 Attack Y：把 B 训工件的 manifest `code_commit` 改写成 A
（artifact_hash 留旧、内容不自洽），freeze 会放行并锚进 epoch，capture 才能拒。
虽不可进入 production validation（0 行落盘），但"不一致工件被合法冻结"本身是缺口。

## 18.2 修复内容（未提交，等用户授权后创建 commit）

```text
scripts/alpha_v2_validation_freeze.py
  - 生产模式且给了 --model-dir 时，身份门之外再做 load_frozen_model 内容校验
    （逐文件 sha256 + artifact_hash 复算；rehearsal 不做，骨架工件仍可排演）
  - 审计注释：epoch identity 中 model_training_code_commit 声明为“审计键”
    （经 freeze_manifest_hash 锚定 + capture/mature 独立重读），门禁键保持 8 键语义
scripts/alpha_v2_runtime_identity_smoke.py
  - 骨架工件（假 hash 9*64）改为真实可加载微型工件（走 fit/persist 生产链），
    与新生产门兼容；step E 注释同步
scripts/nas_deploy_update.sh（P3-3）
  - 镜像身份复核结论（verdict/facts JSON）落 artifacts/alpha_v2/audit/，成败均留档
tests/test_alpha_v2_m41_model_provenance_binding.py（+2，合计 16）
  - 工件内容不自洽（身份改写）→ freeze rc=5 且不写清单/不开 epoch
  - booster 文件被剪动 → freeze rc=5 且不写清单/不开 epoch
```

明确不修：冻结清单 model 块透传 `code_commit_source/identity_source`（审计便利，
非安全增益）；`get_build_manifest` 的 `/app` 兜底（预存在且 fail-closed 方向）。
两处都按评审结论记录保持原状。

## 18.3 修复后实测（Codex 自建沙箱，同口径复跑）

```text
Case 矩阵            20/20（Case 1 正例 + 2/3/4/5/6a/6b fail-closed 全保持）
Attack X             rc=5（构建身份双源互证仍拒人工注入）
Attack Y             freeze rc=5「模型工件完整性」（修复前 rc=0）→ 反转成立
正例身份链           unique_commit_count = 1（11 个 commit 字段全等），40 行捕获写入
变异对照             M-A（门空操作→2a 翻转 5→0）/ M-B（哈希脱钩→load 翻转 FAILED→LOADED）
no-git smoke         A–G 七步 PASS（真工件形态）
tests                m41 16/16、runtime_identity 39/39、m3-r3 36/36、M3 定向 112/112、
                     全量 3665 tests / failed=2 / skipped=2（两个失败均为与本次无关的
                     负载性 flaky：market_warehouse 并发锁用例单跑即过、
                     universe_selector 30s 性能预算用例空闲时两次连过 30.15s→<30s）
ruff / bash -n       改动文件全部 PASS
```

## 18.4 状态边界

```text
FINAL_RUNTIME_HARDENING_COMMIT = 1df77e9（已 commit+push）+ bc7d064（clean-scope
                                  质量门回归修复，与本批无文件重叠）
R4.1.1                         = 未提交的工作区增量（本批 5 文件；等用户授权后创建 commit）
NAS_BUILD_PREFLIGHT            = 未开始（本批合入后按 R4 计划执行）
```


---

# 19. P0 双价格序列契约（2026-09-21，hotfix 分支，待外部复核）

> 只追加，不重写上文。本章对应 `docs/alpha_v2/P0_Dual_Price_Series_Contract.md`。

## 19.1 问题

Alpha V2 的冻结 / 成熟链路此前只有一个 `--market-db`，而生产 NAS 的正式库是
`vendor_delta/market_delta.duckdb`（`price_series_mode=qfq`）——于是**同一份 qfq 序列
同时喂给了特征、label、成交价、MAE/MFE 与全部基准**；`shadow_model_freeze.py` 在
`price_mode_certified=false` 时只打 warning 不 fail。训练目标因此建立在复权价之上。

## 19.2 实现（工程层）

```text
新增  src/stock_analyzer/alpha_v2/dual_price_series.py        契约 + 守卫 + 两条数据身份 + 库解析
新增  src/stock_analyzer/alpha_v2/validation/dual_price_freeze.py  双源训练帧构造（守卫先于重活）
新增  tests/test_alpha_v2_dual_price_series.py                DP-1..DP-10 + 端到端 gate 绑定
新增  docs/alpha_v2/P0_Dual_Price_Series_Contract.md          契约 / 落点 / RAW delta 设计
改    scripts/alpha_v2_shadow_model_freeze.py                 双库参数 + 两条指纹 + v3 provenance
改    scripts/alpha_v2_shadow_mature.py                       双库参数 + 每日重新 certify
改    scripts/alpha_v2_production_preflight.py                双库参数
改    src/.../validation/{preflight,outcome_maturation,validation_kpis,frozen_model}.py
改    src/.../runtime/services/live_shadow_cycle_service.py    capture 不变；mature 两库分开
```

关键语义：

- execution 面板必须 `price_mode == raw` 且 `certified == true`，否则 FAIL CLOSED
  （freeze exit 4 / mature exit 4 且 0 行 outcome / KPI 该日不 clean / preflight BLOCKED）；
- 工件哈希升 **v3**（两条数据身份 + `validation_mode` 进受保护集合）；v1/v2 仍可加载，
  生产只接受 v3；
- `--market-db` 在 freeze / mature 上降级为**仅 `--rehearsal` 可用**的旧参数；
- `build_label_v2` 默认严格；研究回放需显式 `enforce_execution_price_series=False` +
  `research_replay_reason`（生产两个入口由结构测试钉住"无此开关"）。

## 19.3 测试与门禁

```text
定向  tests/test_alpha_v2_dual_price_series.py                 16 passed（DP-1..DP-10 + gate 端到端绑定）
      alpha_v2 相关套件（-k "alpha_v2 or price_contract"）      全绿
质量门 clean-scope（ruff + mypy blocking）                      PASS
```

## 19.4 状态边界

```text
P0_DUAL_PRICE_ENGINEERING_STATUS = PASS（工程层，见 PR 报告）
PR                               = READY FOR EXTERNAL REVIEW（未 merge）
NAS                              = 未操作（未建 raw delta、未部署、未开 epoch）
PRODUCTION_PROMOTION             = LOCKED（不变）
Legacy / Week5 / 生产漏斗 / Cross Review / 阈值 = 未改动
```

> 数据侧动作（建 `/app/artifacts/vendor_delta_raw/market_delta_raw.duckdb`、接通日更、
> 重训与 preflight）**待用户授权**，步骤见 `P0_Dual_Price_Series_Contract.md` §6.4。

---

# 20. P0 Final Live Runtime Hardening R1（2026-09-22，PR #86 追加，待最终复核）

> 只追加，不重写上文。承接第 19 章：本轮关闭外部最终复核提出的 **两个 Live Runtime
> 漏接线**，并把 RAW baseline 的深度口径与测试计数证据一并修正。

## 20.1 两个 blocker

上下文：第 19 章把"训练 / execution / preflight"三处口径固化了，但**运行期**没有复核：
冻结模型声明了 `feature_data_identity.price_series_mode=qfq`，而 capture 加载面板后直接
`pit_universe → 特征 → 预测 → 写盘`，从未验证当天 feature 面板仍是 qfq；且 mature 的
style 面板在不可用时**静默退回 execution 面板**。

```text
BLOCKER 1  Live Capture 没有冻结 Feature Price Mode
BLOCKER 2  口径漂移只会在 capture 内以 exit 11 出现 → 调度器一路 step_failed，
           到 23:55 也不会走 "prerequisites unavailable → record missing day"
BLOCKER 3  Mature 的 feature/style 面板没有与冻结 feature mode 比对
BLOCKER 4  Production Mature 允许退回 execution 面板算 style（改变基准语义）
```

## 20.2 实现落点

| 落点 | 行为 |
| --- | --- |
| `scripts/alpha_v2_shadow_capture.py` | 从**模型工件**取 expected feature mode（production/test 缺失即 exit 11）；面板加载后、`pit_universe`/特征/预测/写盘**之前** `certify_price_mode()` + `require_declared_feature_series(expected_mode=...)`；日清单新增 `feature_price_series` 证据块 |
| `live_shadow_cycle_service._ensure_feature_price_series` | 捕获**前**前置门：`derive_feature_price_series_input()`（复用 preflight 轻量 probe `40d × ≤300 只` + 统一守卫）；窗口内 → `alpha_v2_waiting:feature_price_mode_mismatch`；到 deadline → `recorded_missing:feature_price_mode_mismatch` + 继续推进历史尾部 |
| `live_shadow_cycle_service` capture argv | `--market-db` 改用 `feature_market_db_path()`（`alpha_v2.feature_market_db`，空则回退 `market_warehouse.db_path`） |
| `scripts/alpha_v2_shadow_mature.py` | expected feature mode 取 `freeze.model.provenance.feature_data_identity.price_series_mode`（**不读 config 猜**）；面板缺失/不可读/口径漂移在 production/test 一律 exit 11、0 行 outcome |
| `outcome_maturation.mature_epoch_outcomes` | 新增 `validation_mode`（默认 `production` = fail-closed 默认）；strict + `style_panel=None` → 抛 `OutcomeMaturationError`；rehearsal 允许带标注降级 `style_features_source=execution_panel_fallback_rehearsal` |
| `dual_price_series` | 新增 `feature_mode_of_frozen_model` / `feature_mode_of_freeze_manifest` / `is_live_strict_mode` / `LIVE_STRICT_VALIDATION_MODES` / `FEATURE_PRICE_SERIES_EVIDENCE_SCHEMA` |

**一处有意偏离提示词**：提示词写"rehearsal/test 可保留带标注 fallback"，本实现把严格集合
取为 `{production, test}`，只有 `rehearsal` 允许降级。理由：`test` 在本仓库是 **clean-OOS
合格模式**（`clean_oos_row_eligible` / `_day_governance` 都认它，它只为确定性时钟存在），
放它降级会产生"clean 证据 + 变了语义的 style 基准"。这比要求更严，且已由 LIVE-M4 钉住。

## 20.3 文档与证据修正

- **RAW baseline 改为 coverage-driven**：删除 `--limit-days 400` 作为生产推荐（它只是
  Week5 常规 lookback 的示例，`--limit-days` 是 per-symbol **行数**、不是自然日）；
  新增 §6.1.1 覆盖判据：按 `source_window = 决策窗起点 - warmup 自然日`
  （candidate model 为 `2024-11-14 .. 2026-08-31`）计算所需深度，导入后必须验证
  required symbols 覆盖 / `min(date) <= source_window_start` / 行覆盖，并归档
  `source_window coverage PASS` 证据；不以任何固定整数为准。
- **测试计数纠正**：第 19 章 PR 正文里的 "full = 3705 collected" 是**错的**（由日志里
  数进度点得到，把 warnings/durations 里的点也算进去了）。同环境同命令实测：

  ```text
  BASE 47571c1 : tests/ 裸收集 3806（与 #85 报告的 3806 同量级）
                 full stage 选择（--ignore 12 个慢测文件）3683
  HEAD f5b4dc3 : tests/ 裸收集 3836
                 full stage 选择 3713
  DELTA        : +30（纯新增，未删任何测试文件/测试函数）
  ```

  逐文件差异：新增 `tests/test_alpha_v2_dual_price_series.py`（+26）、
  `test_alpha_v2_m4l_cycle_clean_day_e2e.py` 8→10（LIVE-F5/F6）、
  `test_alpha_v2_s11_outcomes.py` 34→36（拆出 fail-closed 与 research opt-out 两例）。
  `git diff 47571c1..HEAD -- tests/` 中 `-def test_` 与删除文件均为空。

## 20.4 夹具审计（应答"是否给旧 outcome 补 raw/certified 把测试强行变绿"）

做法：对 6 处被 P0 改动过的夹具做**反向变异**（改回缺失/不认证/口径漂移），要求用例
失败；变异后恢复文件、基线复跑通过。

```text
M1 test_alpha_v2_m3_validation_kpis.py      certified True→False      mutated=FAIL baseline=PASS
M2 test_alpha_v2_m3_enforcement.py          certified True→False      mutated=FAIL baseline=PASS
M3 test_alpha_v2_m3_outcome_maturation.py   price_mode_certified→False mutated=FAIL baseline=PASS
M4 test_alpha_v2_m3_r3_final_blockers.py    certified True→False      mutated=FAIL baseline=PASS
M5 clean_day e2e 模型块 feature 身份删掉                                mutated=FAIL baseline=PASS
M6 clean_day e2e 模型块 feature 身份 qfq→raw                            mutated=FAIL baseline=PASS
```

结论：新加的 `price_mode/price_mode_certified` 与 `feature_data_identity` 都是**承重**字段
（缺了/变了用例就红），不是"为了变绿而补的装饰"；negative case（qfq / uncertified /
missing / 漂移）在 LIVE-F3、LIVE-F6、LIVE-M2/M3/M4、DP-3/DP-7 里各自永久保留。

## 20.5 本轮验证（本地实测）

```text
dual-price 定向       26 tests / 0 failed / 0 error / 0 skipped   (junit live_r1_dual_price.xml)
alpha_v2 选择         682 tests / 0 failed / 0 error / 0 skipped  (-k "alpha_v2 or price_contract")
clean-scope 质量门    ruff + mypy blocking rc=0，blocking_failures=[]
tests/ 裸跑           3836 tests / 0 failed / 0 error / 2 skipped  (junit live_r1_bare_tests.xml)
                      （2 skipped = test_nas_*_script 的 Windows bash 语法用例，非本轮引入）
full stage 选择       3713 collected；pytest rc=0；coverage 80.88%（下限 75%）；
                      blocking_failures=[]
GitHub CI            本轮提交的结果见 PR #86 的 checks（同一 commit）。
```

## 20.6 状态边界

```text
P0_LIVE_RUNTIME_BLOCKERS = 0（本轮的 4 项全部关闭并有专项用例）
PR #86                   = 未 merge（等最终复核）
NAS                      = 未操作（未建 raw delta、未部署、未开 epoch）
ALPHA_V2_EPOCH_001       = NOT_STARTED
LIVE_CLEAN_OOS_DAYS      = 0
ALPHA_VERIFIED           = FALSE
PRODUCTION_PROMOTION     = LOCKED
Legacy / Week5 / 生产漏斗 / Alpha final selection / 阈值 = 未改动
```


---

# 21. P1 — RAW Execution Delta 生产接线（2026-09-22，独立 PR，未部署）

## 21.1 目标与边界

把 Alpha V2 所需的 **RAW execution 行情库**正式接进现有 NAS 夜间统一数据事务：每天的
QFQ feature delta 与 RAW execution delta 从同一批 ZIP/index 推进，并且**只有两者都完整、
同日、口径正确时** nightly readiness 才允许发布。

```text
不做：Alpha tuning / 特征 / label / 训练窗口 / 模型冻结 / epoch / Production Promotion
不改：Legacy 选股 / Week5 选择语义 / 生产漏斗 / 交叉复核 / 任何阈值
未做：NAS 部署、真实 RAW 基线构建（合入 ≠ 上线，见 §21.6）
```

## 21.2 落地内容

| # | 落点 | 内容 |
| --- | --- | --- |
| 1 | `update_vendor_daily_from_tushare.py` | 新增 `--sync-vendor-delta-raw`；两个角色走同一条路径，口径由编排写死（`feature→qfq` / `execution→raw`），不再依赖 `config/default.yaml` 默认值 |
| 2 | 同上 | 导入前先验 RAW 基线身份（`_raw_baseline_gate`），不通过则 `raw_delta_baseline_missing` 等四类原因码 fail closed，**绝不用 `--incremental` 偷偷初始化** |
| 3 | 同上 | summary 拆角色：`feature_delta_sync` / `execution_delta_sync` / `raw_delta_baseline`；`delta_sync` 保持历史形状不动；`full_run_ok` 含两个角色 |
| 4 | `import_vendor_zip_to_delta.py` | RAW 口径下因子漂移重写通道**显式关闭**并上报 `factor_drift_detection=disabled_non_qfq_mode`；非 qfq 若产出 drift symbol 直接抛错 |
| 5 | `src/stock_analyzer/ops/raw_delta_baseline.py`（新） | bootstrap marker 身份模型 + 符号集合摘要 + 库事实实测（单调不变量，无阈值） |
| 6 | `scripts/alpha_v2_raw_delta_coverage.py`（新） | 只读覆盖校验器（8.1 DB / 8.2 口径 / 8.3 窗口 / 8.4 符号 / 8.5 行）+ `--write-marker` / `--verify-marker` |
| 7 | `src/stock_analyzer/ops/nightly_readiness.py` | schema v3：新增 `execution_delta` / `symbol_membership` / `raw_delta_baseline`；v2 语义不变；`check_nightly_readiness` 接受 v2+v3，v3 缺 execution 块即不 ready |
| 8 | `scripts/nas_stock_updater.sh` | 同一次调用传两个 delta 目标；verify 段在双 delta 模式下额外要求 execution 角色 updated |

## 21.3 三个判据上的关键选择

**(a) 覆盖判据不用整数、也不引入交易日历。** `--limit-days` 是每 symbol 的行数，
"400 比 240 大"不是覆盖证明。行覆盖用 `(symbol,date)` 逐对比较（引擎内 `EXCEPT`）：

```text
feature 有、raw 没有  → 数据缺口（BLOCKED）
两边都没有            → 停牌 / 未上市（正常）
raw 有、feature 没有  → 正常：qfq 侧因子缺失会被跳过，raw 不需要因子
```

**(b) v3 成员锁步是包含链 `index_expected ⊆ feature ⊆ execution`，不是三方全等。**
raw 侧不依赖复权因子，因因子缺失被 qfq 侧跳过的 symbol 在 raw 侧天然存在；要求全等会把
这条**正常**路径判成故障、每晚误杀。包含链同时封住了旧口径的漏洞：旧判据只比
`symbols_on_target_date` 的**计数**，`{A,B}` 与 `{A,C}` 计数相同、成员不同会被静默放行。

**(c) 基线身份用内容事实 + 单调不变量，不用整库 SHA256。** 库每晚都在长，对数百 MB 的
DuckDB 每晚重算全文件摘要纯属开销。marker 里的取证快照（行数 / 日期区间 / 符号摘要）如实
记录建基线当刻状态但**不参与每日校验**；每日只看不变量：`daily_bars` 在、行数与符号数
**不少于**建基线记录值、当前行内口径仍是 raw。

## 21.4 施工中发现并修掉的两个真实缺陷

都不是"测试写错了"，是实现本身在正常运维场景下会误伤：

1. **无事可做的夜晚会被自己判成败。** 无新增行可推进时 execution 角色被记为
   `not_enabled`，而 `execution_delta_ok` 只按"是否请求"判定 → 整晚不放行。假期、
   重跑、以及任何 ZIP 已追平的夜晚都会中招。
2. **重试无法收敛。** 第一晚 raw 失败、第二晚 ZIP 已追平 → `delta_should_sync` 为假 →
   落后的 raw 角色被整个跳过，永远追不上。修法：两个角色只要**索引进度可信**就跑
   （无事可做时 importer 本身是廉价空转），`index_should_update` 仍只看"本次是否真的
   抓到新行"以保持既有行为。

两条都由 `test_retry_after_execution_failure_converges_without_duplicates` 与
`test_tx1_both_roles_ok_writes_v3_readiness` 钉住。

## 21.5 本轮验证（本地实测 + CI）

```text
分支 / HEAD            feat/alpha-v2-raw-execution-delta @ deb08c5
PR                     #87（base=main）
栈叠关系               **stacked PR**：分支直接派生自 #86 final HEAD 725e943。
                       P1 增量 = 11 个文件（`git diff --name-only 725e943 52ea8f7`）。
                       与 P0 **重叠 1 个文件**：docs/alpha_v2/PROGRESS.md（两阶段都改）。
                       因 P1 commit 建立在 P0 commit 之后，不存在并行冲突；
                       但必须按 #86 → #87 顺序合并。
                       （GitHub 侧 #87 曾显示 41 个文件，正是因为 #86 当时还没进 main。）

定向（spec §26 指定三文件） 58 passed / 1 skipped
  tests/test_update_vendor_daily_from_tushare.py
  tests/test_nightly_readiness_authoritative.py
  tests/test_nas_stock_updater_script.py
关键字选择（-k "vendor_delta or nightly_readiness or alpha_v2"）
                      707 passed / 3177 deselected

新增用例（48）        test_raw_delta_baseline_identity.py      22
                     test_alpha_v2_raw_execution_delta_wiring.py 24
                     test_nas_stock_updater_script.py          +2

tests/ 裸跑（干净串行） 3884 collected / 0 failed / 0 error / 2 skipped / exit 0
                     基线（P0 head 725e943）= 3836 → 3836 + 48 = 3884，数字自洽
                     2 skipped = test_nas_*_script 的 Windows bash 语法用例（既有）

clean-scope 质量门     ruff + mypy blocking rc=0，blocking_failures=[]
full 质量门            pytest rc=0；coverage 80.87%（下限 75%）；blocking_failures=[]
GitHub CI              PR #87 两个 quality job 均 pass（headSha=deb08c5）

一次并发踩坑           同时跑两份全量时 test_alpha_v2_m4l_cycle_clean_day_e2e.py::
                     test_dh7_degraded_first_then_healthy_recovers 因 600s 子进程
                     超时失败；该文件单独跑 10/10 全过，串行重跑全量 0 failed。
                     结论：并发负载所致的超时，非代码缺陷——但"全量测试必须串行跑"
                     这条要记住。
```

## 21.6 状态边界

```text
P0_DUAL_PRICE_CONTAINED  = 代码层生效（分支基于 PR #86 的 HEAD 725e943）
PR #86                   = 已被用户 merge（2026-09-22T05:46:19Z，快进式，main 得 725e943）
P1 PR                    = #87（#86 合入后由用户 merge，mergeCommit 0754faa，2026-09-22T05:46:17Z）
                           两者与 P1 增量的重叠文件 = docs/alpha_v2/PROGRESS.md（仅文档）

RAW_DELTA_PIPELINE_ENGINEERING_STATUS = PASS
RAW_DELTA_NAS_CUTOVER_READY           = READY_FOR_BASELINE_BOOTSTRAP

RAW_PRODUCTION_BASELINE  = NOT_CREATED（NAS 未操作）
ALPHA_V2_EPOCH_001       = NOT_STARTED
LIVE_CLEAN_OOS_DAYS      = 0
ALPHA_VERIFIED           = FALSE
PRODUCTION_PROMOTION     = LOCKED
```

**合入 ≠ 上线**：`nas_stock_updater.sh` 已经是 dual delta，但 NAS 上直接跑会整晚 fail
closed（`raw_delta_baseline_missing`）——这是设计行为。上线顺序（先建基线 → 覆盖 PASS →
再切受管 updater）与回滚路径见
`docs/alpha_v2/RAW_Execution_Delta_Production_Wiring.md` §6/§7。

---

# 22. P1 Final Readiness Hardening R1（2026-09-22，R1 分支，未部署）

## 22.1 前置事实（与指令前提不同，先如实记）

```text
PR #86  = MERGED  2026-09-22T05:46:19Z  mergeCommit 725e943（快进式）
PR #87  = MERGED  2026-09-22T05:46:17Z  mergeCommit 0754faa（base=main）
          → **由用户自行 merge**，不是本轮施工动作；#87 的合并把 P0 的 5 个
            commit 与 P1 的 3 个 commit 一并带进 main。

git merge-base --is-ancestor 725e943 origin/main  →  TRUE（P0_IN_MAIN = TRUE）

因此 R1 指令的两条前提已失效：
  - `git diff --name-only origin/main...HEAD` 现在恒为空（HEAD 已是 main 的祖先）；
  - "继续原 branch / 不要新建 PR"：merged PR 无法再接收 commit。
处置：R1 在新分支 feat/alpha-v2-raw-execution-delta-r1（base=main）上施工。
```

## 22.2 修掉的 P0 blocker：active epoch 会接受 v2 readiness

`check_nightly_readiness()` 为了 Legacy/Week5 向后兼容接受 v2 与 v3 —— 这是对的。
但 `LiveShadowCycleService` 在 active epoch 下复用了同一个**宽松档**，于是"只有 feature
delta 的晚上"照样能进 capture 并记一个 clean day，而 epoch 的 label / 成交价 / 净收益 /
超额 / MAE-MFE 全部取自 execution/raw 库。

修法（不新增第四套判据，只给同一个函数加一个显式参数）：

```python
check_nightly_readiness(..., require_dual_delta: bool = False)   # 默认档不变
Week5AutomationService.probe_nightly_readiness(require_dual_delta=False)  # 透传
LiveShadowCycleService  →  probe_nightly_readiness(require_dual_delta=True)
```

严格档要求 `schema_version >= 3` 且执行侧证据齐全；v2 一律不 ready，原因码
**`nightly_dual_delta_not_ready`**（与 `nightly_data_not_ready` 分开——调度器要靠它区分
"数据没好，重试即可"与"release 里根本没有执行侧证据，重试也不会变"）。

Alpha 侧行为沿用 M4-L 既有纪律，不是新机制：

```text
v2 未到 deadline    → alpha_v2_waiting:nightly_dual_delta_not_ready（不 capture）
同一晚补出 v3       → 下一 slot capture
到 deadline 仍 v2   → alpha_v2_blocked_recorded_missing:...:nightly_dual_delta_not_ready
                      clean_oos_days 不增长
```

## 22.3 封掉 marker certification 旁路

`--write-marker` 与 `--skip-price-mode-certification` 曾经可以同时给 —— 等于让"跳过认证"
的运行产出生产信任凭据。两道门：

```text
CLI  ：两者互斥 → usage error（退出 2），marker 不落盘
函数 ：build_bootstrap_marker() 要求 expected==raw / observed==raw / certified==true
       —— 真正的边界；CLI 之外将来若有别的调用方，也造不出未经认证的 marker
```

`--skip-price-mode-certification` 仍可用于只读诊断。

## 22.4 文档修正（原有三处表述是错的）

```text
错：P0 与 P1 "无文件重叠"
对：重叠 1 个文件 —— docs/alpha_v2/PROGRESS.md（两阶段都改）；因 P1 commit 建立在
    P0 commit 之后，不存在并行冲突，但必须按 #86 → #87 顺序合并。

错：P1 diff "收敛为 7 个文件"
对：P1 增量 = 11 个文件（git diff --name-only 725e943 52ea8f7）。
    GitHub 侧 #87 曾显示 41 个文件，正是 #86 当时还没进 main 所致。

错：回滚到单 delta "Alpha V2 capture 不受影响"
对：只在**没有 active epoch**时成立。有 active epoch 时 v2 对 Week5 仍有效，但 Alpha
    一律拒绝 → waiting/missing → clean_oos_days 不增长；长期回滚必须 close/suspend
    epoch 或恢复 dual updater。已写入 RAW wiring 文档 §7。
```

## 22.5 本轮验证（本地实测 + CI）

```text
分支 / HEAD            feat/alpha-v2-raw-execution-delta-r1 @ 378abc9
PR                     #88（base=main）

定向（spec §16 五文件 + 关键字选择）
  tests/test_alpha_v2_raw_execution_delta_wiring.py
  tests/test_raw_delta_baseline_identity.py
  tests/test_nightly_readiness_authoritative.py
  tests/test_nas_stock_updater_script.py
                      80 passed / 1 skipped
  tests/test_alpha_v2_m4l_cycle_clean_day_e2e.py
                      11 passed（含 ALPHA-RDY-2 真实 CLI：clean_oos_days=1）
  tests/test_alpha_v2_m4l_shadow_cycle_scheduler.py
                      14 passed（含 ALPHA-RDY-1/3）
  关键字 -k "nightly_readiness or raw_delta or alpha_v2"
                      全绿（0 failed）

tests/ 裸跑（干净串行） 3898 collected / 0 failed / 0 error / exit 0
                     基线（#87 合并后）= 3884 → 新增 14 例，数字自洽

clean-scope 质量门     ruff + mypy blocking rc=0，blocking_failures=[]
full 质量门            pytest rc=0；coverage 80.87%（下限 75%）；blocking_failures=[]
GitHub CI              PR #88 checks 全绿（见 PR）

CI 一次 flake 与排除过程（留证，不要当成"影响不大"的推断）
  现象    test_week5_scan_funnel_policy.py::test_week5_offhours_forced_profile_runs_snapshot_funnel
          deep 选出 5 只而非 6（缺 601318）；--reruns 2 用尽
  取证一  同一 commit 378abc9 三次判定：run 35693132201 attempt1 **pass**；
          run 35693154338 attempt1 fail / attempt2 **pass** → 非确定性，同一棵树
  取证二  本地同一棵树：裸全量 0 failed、full 质量门 rc=0、失败文件连续 3 次 24 passed
  取证三  把 check_nightly_readiness / read_nightly_readiness 整体替换为**抛错函数**后
          再跑该用例 —— 仍然通过 ⇒ 该路径根本不触及本次 readiness 改动（直接运行时检验，
          不是"影响不大"的推断）。临时取证用例跑完即删。
  定性    与项目既有记录同型（PROGRESS §18.3「负载性 flaky：单跑即过」）
  处置    不改被测代码；flake 归入既有 backlog，不通过放宽断言掩盖
```
新增用例清单（14）：RDY-1..7 与 `test_rdy_broken_v3_still_reports_data_not_ready_for_week5`
（8）、MARK-1..3（3）、ALPHA-RDY-1/3（2）、ALPHA-RDY-2（1）。

## 22.6 状态边界

```text
P0_IN_MAIN                        = TRUE（725e943）
PR #87                            = 已被用户 merge（0754faa）
R1 分支                           = feat/alpha-v2-raw-execution-delta-r1（base=main）

Alpha active epoch 接受 v2 readiness = NO（本轮修复）
Alpha active epoch 要求 v3          = YES
Week5 v2 readiness 行为变化         = NO

RAW_PRODUCTION_BASELINE  = NOT_CREATED
ALPHA_V2_EPOCH_001       = NOT_STARTED
LIVE_CLEAN_OOS_DAYS      = 0
ALPHA_VERIFIED           = FALSE
PRODUCTION_PROMOTION     = LOCKED
```

遗留 backlog（非本轮范围）：`MEMORY.md` 索引超限（40KB > 24.4KB 预算）——属 agent/tooling
hygiene，与生产管线无关。

# 23. P3.1 — 双价格 freeze 的有效决策集契约（2026-09-23，契约对齐，未部署）

## 23.1 目标与边界

解除 P3 Freeze Preparation 的 `BLOCKED_BY_CODE_DEFECT`：`build_dual_price_training_frame`
要求**每条** PIT 候选在某日都有 execution 当日 bar，而 PIT 合格池按设计包含"最近 5 个
交易日内交易过、当天停牌"的票（`expected_active_lookback_days=5`），于是生产窗口上
6,602/1,650,654 条合法候选被当成"数据缺失"→ fail closed。

**这是 contract alignment，不是 strategy change**：不改 PIT universe、不改
`expected_active_lookback_days`、不改选股/特征/label/target/warmup/training window、
不改价格口径、不动任何数据。改的只是"哪些候选有权进入训练帧"这件事在代码里的表达。

## 23.2 落地内容

| 文件 | 改动 |
| --- | --- |
| `src/stock_analyzer/alpha_v2/dual_price_series.py` | 新增 `filter_decisions_by_execution_availability`（合法停牌→过滤 / 结构缺陷→fail closed）+ `DecisionAvailability` + 原因码与量级闸常量；`assert_decisions_aligned` 保留为**零容忍**版本，语义未变 |
| `src/stock_analyzer/alpha_v2/validation/dual_price_freeze.py` | 对齐步骤改为过滤式；**过滤后的决策集**送进 `build_label_v2` / `compute_style_features` / `daily_feature_frame`；新增 `decision_accounting` 审计块 |
| `scripts/alpha_v2_shadow_model_freeze.py` | 显式捕获 `PriceSeriesContractError`（文档化的 exit 4 不再退化成 traceback exit 1）；打印 `Dual price alignment: before / filtered / after / status` |
| `tests/test_alpha_v2_dual_price_series.py` | 新增 DP-11..DP-18（9 例：前提、Case 1/2、Case 3a/3b/3c、量级闸、量级闸默认语义、不变性、严格版未放松、CLI exit 4 与报告行） |
| `docs/alpha_v2/P0_Dual_Price_Series_Contract.md` | 新增 §2.1「有效决策集：候选 ≠ 可交易」+ §3 守卫落点更新 |

裁决判据是**集合关系**，不是比例感觉：

```text
在 execution 面板里                          → 保留
票在、日在、仅当天无 bar（停牌）             → 过滤（NO_EXECUTION_BAR_ON_DECISION_DATE）
整票不在 execution 面板                      → fail closed
该日不是 execution 的交易日                  → fail closed
feature 有当日 bar 而 execution 没有         → fail closed（跨面板分歧）
```

## 23.3 关键选择（含一处"实测后修阈值"的记录）

1. **过滤发生在构造 label/特征/风格之前**，而不是在最终帧上删行：被过滤的行不会以
   "无 label 的空行"形态混进矩阵（`fit_frozen_model` 的 `labels.notna()` 掩码只是兜底）。
2. **单侧丢失 ≠ 停牌**：`cross_check_panel=feature 面板` 逐键拦截"一侧有、一侧没有"，
   与量级无关——这条同时保证被过滤的行在两侧都不可用，因此不会顺带改变质量池分母。
3. **量级闸先按"每天几只停牌"设定（单日 10%），实测后改成"该日截面被毁"（单日 50%）**：
   首次生产 dry-run 在 2025-11-17 被判 634/5175 = **12.25%** 拦截。逐项取证后确认这是
   **两侧同时**缺 724 个 symbol 的**单日洞**（raw 与 qfq 该日均 4,713 只 vs 前一日
   5,438；抽 200 例形态 200/200 = "前一根 11-14、后一根 11-18、只缺这一天"），即**上游
   链路的覆盖率缺口**，不是跨面板分歧、也不是"整片 bar 丢失"。因此单日闸的语义收紧为
   "**过半**候选被过滤 = 该日截面被毁"，并在证据里新增 `filtered_dates_top`（这类日期
   一眼可见）。阈值来自实测（合法最大 12.25%），不是拍脑袋。
4. **不做 try/except 绕过**：没有"忽略错误继续跑"的分支；被过滤的行数、原因、样例、
   按日分布全部进 `dual_price_evidence.decision_alignment`（受 v3 工件哈希保护）。

## 23.4 实测证据（NAS 生产窗口，只读一次性容器）

```text
镜像/环境   stock-analyzer:latest + /tmp/p3_heavy_env.txt（heavy 等效）
代码身份    module_sha256=83de9367ff9a0f483d9010150f099c35967d24e4751ff55a51e3742f0898d7fc
            （= 本地提交版 dual_price_series.py 的 LF 归一化 sha256，逐位一致）
只读挂载    artifacts 卷 :ro、两个源文件与探针脚本 :ro；未写任何工件（artifacts_written=0）
面板         feature 2,259,889 行 / 5,818 票 / 306 交易日；execution 2,372,392 行（与 P3 实测一致）
口径         feature=qfq（certified=False，探针只认证 raw，属正常）；execution=raw certified=True

决策集合     1,650,654 键（构造 923.6s；P3 实测 944.6s）
旧严格门     RAISED：6602/1650654 找不到对应 bar（P3 blocker 复现，样例 002480@2025-06-03）
新裁决       decision_rows_before          = 1,650,654
            filtered_missing_execution_rows = 6,602（0.399963%）
            decision_rows_after / intersection = 1,644,052
            status                          = PASS
            filtered_dates                  = 306
            max_daily_filtered_ratio        = 12.2512% @ 2025-11-17
保留集复核   kept_all_aligned=True，kept_missing=0（保留的每一条都有当日 bar）
缺失形态     6,602 条中：feature 侧当天有 bar = **0**；execution 侧有 bar = **0**
            整票缺席 = **0**；该日不是交易日的 = **0** → 全部是"两侧都没有当天 bar"
            样例 002480@2025-06-03：前一根 05-23、后一根 06-10（长停）
            样例 002199@2025-06-03：前一根 05-27、后一根 06-05
```

本地真实数据复核（不是夹具）：用仓库 `artifacts/warehouse/market.duckdb` 跑**真实 CLI**
（`--rehearsal`、窗口 2025-10-01..2026-03-31、`--max-symbols 400`）→
`before 46,400 / filtered 65（0.1401%）/ after 46,335 / status PASS`，随后正常训练
17/17 目标（工件落在系统临时目录，**不在**仓库与生产 artifacts 内）。同一库若把窗口
末尾扩到被截断的 2026-04-02/03（当日仅 49/43 只票），单日闸仍然拦下 —— 说明这条闸
没有被这次调整弄成"永远放行"。

## 23.5 本轮验证（本地实测）

```text
tests/test_alpha_v2_dual_price_series.py     35 passed（原 26 → +9）
相邻 alpha_v2 模块定向（S11 outcomes / M3 enforcement / M41 provenance /
  M4L preflight / RAW delta wiring）        157 passed
ruff check（4 个改动文件）                    与基线同 2 条既有告警（E501/I001 均非本次引入）
mypy（2 个 src 文件）                         12 errors = 基线 12（0 新增）
ruff format --diff                            本次新增行无格式漂移（其余为既有漂移）
生产 artifacts 卷                             alpha_v2/ 仅两个 RAW coverage 文件；RAW 库 mtime 未变
```

## 23.6 状态边界

```text
FREEZE_PREPARATION（契约层）  = PASS（生产窗口 dry-run 不再 fail closed）
ALPHA_V2_EPOCH_001           = NOT_STARTED（本任务未生成 model / epoch / production artifact）
NAS_FREEZE_HOST              = 仍不推荐（P3 实测：全窗口帧构造外推 ≈ 13.4 GiB 增量，
                               与本次契约修复无关，属资源约束）
部署                          = 未做（本分支未 push、未部署；NAS 仍跑旧镜像契约）
```

后续动作（**需用户授权**，本任务不做）：把本分支合并/部署后再跑一次真实 freeze；
在此之前 P3 的资源结论不变，见 §P3 Freeze Preparation 记录。
