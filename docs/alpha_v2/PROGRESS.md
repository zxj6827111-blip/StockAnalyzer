# StockAnalyzer Alpha V2.0 — PROGRESS

> 用途：记录 Alpha V2.0 改造过程中的施工进度、测试结果、审计工件、Codex 验收结果与回滚信息。  
> 维护原则：只追加，不覆盖历史。  
> 上位方案：`StockAnalyzer_Alpha_V2_完整改造方案_20260917.md`

---

# 0. 当前总状态

```text
Project = StockAnalyzer Alpha V2.0
Current Batch = M1
Current Stage = S00 DONE（S01 施工中）
M1 Acceptance = PENDING
M2 Acceptance = LOCKED
Production Promotion = LOCKED
```

分支与提交（本地；未 push、未部署）：

```text
branch = feat/alpha-v2-m1-0917
review baseline = fix/asof-breadth-gate-coverage-0917 @ 7e9e33bdb9d03506cff5dfff29c78b1c95019541
S00 commit = 83e4fd7 (feat(alpha-v2): add shadow feature flags and no-op baseline)
```

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

## Stage Matrix

| Stage | Task | ZCode Status | Tests | Audit Artifact | Codex Acceptance | Notes |
|---|---|---|---|---|---|---|
| S00 | Alpha V2 Feature Flag / No-op Baseline | DONE | 40 定向 + 3031 全量 / 0 failed | `artifacts/alpha_v2/audit/s00_validation.json` | PENDING | Legacy 零行为变化（结构性 + golden 双向锁定） |
| S01 | Model Identity Truth | IN_PROGRESS | - | - | PENDING | |
| S02 | T+1 Entry Simulation | NOT_STARTED | - | - | PENDING | |
| S03 | Point-in-Time Historical Universe | NOT_STARTED | - | - | PENDING | |
| S04 | SelectionContract 300/100/50 | NOT_STARTED | - | - | PENDING | |
| S05 | Registry / Archive Governance | NOT_STARTED | - | - | PENDING | |
| S06 | HistoricalModelResolver | NOT_STARTED | - | - | PENDING | |
| S07 | Feature Price / Execution Price Split | NOT_STARTED | - | - | PENDING | |
| S08 | Data Health / Market Breadth Split | NOT_STARTED | - | - | PENDING | |
| S09 | Model / Label Semantic Guard | NOT_STARTED | - | - | PENDING | |
| S10 | Decision Log + Outcome Maturation | NOT_STARTED | - | - | PENDING | |

---

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
| 20D Alert Gate | >=20 mature decision dates | LOCKED | - | 仅用于发现明显失败 |
| 60D Research Gate | >=60 mature decision dates | LOCKED | - | 允许第一轮方向判断 |
| 120D Advisory Gate | >=120 clean OOS dates | LOCKED | - | 才允许讨论 Advisory |
| 250D Governance Gate | ~250 OOS trading days | LOCKED | - | 才讨论自动 promotion / 稳定阈值 |

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
Engineering Verdict = PENDING
Codex Verdict = PENDING
Permission To Proceed To M2 = NO
Reviewed HEAD = TBD
Review Date = TBD
```

### Blocking Findings
- TBD

### Non-Blocking Findings
- TBD

---

## M2 Acceptance

```text
Engineering Verdict = PENDING
Codex Verdict = PENDING
Research Evidence Status = LOCKED
Production Promotion = LOCKED
Reviewed HEAD = TBD
Review Date = TBD
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
| DF-S00-002 | S00 | 生产有效 `models.inference_score_source=calibrated` vs 受跟踪默认 `raw`，基线清单需带环境口径 | info | unassigned | S01 | OPEN |
| DF-S00-003 | S00 | `scripts/` 旧临时脚本与根目录 `tmp_*` 残留较多 | info | unassigned | 批次收尾清理 | OPEN |

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
NEXT ACTION:
ZCode M1 施工中 — S00 DONE，当前 S01（Model Identity Truth）

AFTER M1:
Run Codex CURRENT_BATCH = M1

Only after Codex PASS:
Unlock M2
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
