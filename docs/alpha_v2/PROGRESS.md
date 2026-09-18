# StockAnalyzer Alpha V2.0 — PROGRESS

> 用途：记录 Alpha V2.0 改造过程中的施工进度、测试结果、审计工件、Codex 验收结果与回滚信息。  
> 维护原则：只追加，不覆盖历史。  
> 上位方案：`StockAnalyzer_Alpha_V2_完整改造方案_20260917.md`

---

# 0. 当前总状态

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
