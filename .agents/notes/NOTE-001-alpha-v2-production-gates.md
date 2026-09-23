# NOTE-001 Alpha V2 生产门禁链

Status: Accepted

As-of: 2026-09-23 @ HEAD `42caaca`（详见 §11）

> 类型：Business / Contract Note（运行合约），不是架构决策。
> 它回答"哪一道门挡在哪一步之前、失败以什么退出码呈现、什么状态目前根本不可达"。
> 身份与工件完整性的**为什么**在 [[ADR-001-runtime-identity-and-artifact-integrity]]。

## 1. Context

Alpha V2 的门禁语义此前**没有单一权威文档**，分散在各 CLI 的模块 docstring 里
（最完整的一份是 `scripts/alpha_v2_validation_freeze.py:15-45`）。
`docs/alpha_v2/StockAnalyzer_Alpha_V2_Codex_阶段验收门禁_20260917.md` 早于
`7c41fd6` / `d323e54` / `378abc9` 三个提交，属"不完整"而非"错误"。

本 Note 只记录**代码与测试已经钉住**的门禁关系。

## 2. 阶段与入口

```text
train + model freeze  →  validation freeze  →  Production Preflight  →  open epoch
                                                                    →  shadow cycle（capture → mature → report）
```

| 阶段 | 入口 | 职责（来自模块 docstring） |
| --- | --- | --- |
| 训练 + 模型冻结 | `scripts/alpha_v2_shadow_model_freeze.py` | **唯一允许写"模型"的入口**：一次训练、落盘、锚定哈希 |
| 验证冻结 + 开 epoch | `scripts/alpha_v2_validation_freeze.py` | 生成/校验 freeze manifest 并开 epoch |
| Production Preflight | `scripts/alpha_v2_production_preflight.py` | **只读**数据体检；`BLOCKED` → exit 1 |
| 决策日快照 | `scripts/alpha_v2_shadow_capture.py` | T 日快照，写一次不可改 |
| outcome 成熟 | `scripts/alpha_v2_shadow_mature.py` | 按标的自身 bar 序列推进成熟 |
| KPI 报告 | `scripts/alpha_v2_validation_report.py` | clean-OOS 指标；2=参数错 3=epoch/report 缺失 |
| 研究（不接生产） | `scripts/alpha_v2_research_run.py`、`alpha_v2_m4h_run.py` | 研究用途，不参与任何门 |

**不存在的入口**（这是事实，不是遗漏）：没有 `promote` / `activate` / 独立的 `train`
脚本。生产晋升在当前代码里**未实现**，见 §6。

## 3. 门禁关系（哪一步挡哪一步）

| 边界 | 检查 | 失败 |
| --- | --- | --- |
| model freeze → validation freeze | `freeze_precheck.assert_execution_price_raw` | exit 4 |
| 同上 | `assert_runtime_identity` / `assert_build_identity` / `assert_model_training_commit` | exit 5 |
| 同上 | `resolve_feature_schema_columns`（生产空 schema）/ `assert_validation_start_date` | exit 6 |
| 同上（工件内容） | `load_frozen_model(require_sealed_provenance=True)` 重算哈希 | exit 5 |
| preflight → open epoch | `assert_preflight_gate`：BLOCKED / 非法 verdict / 未接受的 WARN / 报告过期（`preflight_max_age_hours`）/ runtime commit 不符 / model identity 逐字段不符 | exit 7；生产缺 `--preflight-report` 同为 7 |
| epoch → capture | `require_epoch_identity_match` → 3；生产拒绝 `--capture-date`（回填窗口）→ 8；拒绝 `research_proxy` cohort → 10；model dir 缺失 → 4；工件哈希不符 → 5；冻结 feature price mode 未声明/不符 → 11；funnel 校验 → 10；写一次/篡改 `ShadowCaptureError` → 9 |
| capture → mature | `require_open_epoch`（重读 registry）+ epoch identity 对账 + execution 面板 `raw+certified`（门 0，在任何计算/写入之前） | exit 4，**0 行 outcome** |
| mature → KPI | `_day_governance` 重算 `clean_oos_eligible` / `data_health_gate_ok` / funnel hash；`SAMPLE_GATES`（`validation/freeze.py`） | 该日不进 clean 样本 |

## 4. 退出码约定

| 码 | 含义 | 代表实现 |
| --- | --- | --- |
| 0 | 通过 | — |
| 1 | Preflight `BLOCKED` | `alpha_v2_production_preflight.py:195` |
| 2 | 参数用法错误 | 各 CLI argparse |
| 3 | epoch registry / report 缺失；capture 身份门 | `validation_freeze.py:404`、`shadow_capture.py:289` |
| 4 | **价格序列契约违例**（execution 非 raw+certified） | `freeze_precheck.py:63`、`shadow_mature.py:189` |
| 5 | 运行/构建身份 + 工件完整性 | `freeze_precheck.py:94-131`、`validation_freeze.py:200` |
| 6 | 起始日期 / feature schema 门 | `validation_freeze.py:217` |
| 7 | Production Preflight 硬门 | `validation_freeze.py:252` |
| 8 | 写入窗口 / 回填越界 | `shadow_capture.py:305` |
| 9 | shadow 写入被拒 / 篡改 | `shadow_capture.py:725` |
| 10 | cohort 来源 / funnel 不符 | `shadow_capture.py:340` |
| 11 | 运行期 feature price mode 契约 | `shadow_capture.py:433` |

> 约定：违例必须变成**真实退出码**。`FreezeGateError` 自带 `exit_code`（默认 6），
> CLI 须 `return exc.exit_code`。"文档写 4、进程退 1"算缺陷（ADR-002 §8.2 是一例）。
> 注意：这套编号**没有中心注册模块**，是按脚本形成的惯例，改的时候要全仓对齐。

## 5. Shadow ↔ Production 边界（三层，彼此独立）

1. **结构隔离**：`main.py` / `pipeline.py` / `runtime/service.py` 里**没有** `alpha_v2`
   引用；`tests/test_alpha_v2_baseline.py:81-126` 钉住 `_ALPHA_V2_FLAG_CONSUMERS`
   白名单——新增消费方会变成一个可评审的 diff。
2. **配置交叉校验**：`AlphaV2Config`（`config.py:1873-1877`）默认
   `enabled=False`、`shadow_only=True`、`enforce_final_selection=False`；
   validator（`config.py:1970-1984`）拒绝 `enforce_final_selection=true` 与
   `enabled=false` 或 `shadow_only=true` 同时出现。
   运行期：`live_shadow_cycle_service.enabled()` 要求
   `enabled && shadow_only && !enforce_final_selection`。
3. **Preflight 安全标志门**：`check_safety_flags`（`preflight.py:147-191`）对
   `shadow_only=false`、`enforce_final_selection=true`、`training_enabled`、
   `auto_promotion_enabled` 任一成立即 BLOCKED。

## 6. 当前不可达的状态（重要边界）

- `validation_kpis.py:562-563`：`alpha_verified` 恒 `False`、
  `production_promotion` 恒 `"LOCKED"`（M3 §14/§21：达到样本门 + 人工复核前不得翻）。
- 没有任何代码路径消费 `enforce_final_selection` 去改动线上 final selection——
  **晋升是"未实现"，不是"已实现但被关着"**。
- `epoch.close_epoch()` 已定义并导出，但 `src/` 与 `scripts/` 内**无调用方**：
  按现有入口无法关闭一个 epoch。要产出"已关闭 epoch"证据需要先补入口。

## 7. 调度

| 项 | 值 | 出处 |
| --- | --- | --- |
| job 名 | `alpha_v2_shadow_cycle` | `live_shadow_cycle_service.py:49` |
| 步骤 | `data_health → capture → mature → report` | 同上 `:50-55` |
| 窗口参数 | `live_cycle_start/latest/interval` | `config.py:1889-1892` |
| 注册 | `runtime/service.py:18387 registration()` | — |
| 承载容器 | `scheduler-critical` / `scheduler-heavy` | `docker-compose.yml:59,114` |
| 上游漏斗 | `week5_night_scan` 写 production funnel 工件（capture 读） | `service.py:18035` |
| 运行前置 | `probe_nightly_readiness(require_dual_delta=True)`，`378abc9` 起 | `live_shadow_cycle_service.py` |

## 8. Invariants

1. 前置硬门失败即停止后续阶段，**不得为了"把流程走完"绕过硬门**。
2. 新增任何消费 `alpha_v2` 配置标志的生产路径 = 边界变更，必须同步本 Note 与
   `_ALPHA_V2_FLAG_CONSUMERS` 白名单。
3. `alpha_verified` / `production_promotion` 不得在没有样本门 + 人工复核的情况下改动。
4. 非 production（`rehearsal`）产物不得作为生产证据引用——它们跳过身份硬门。
5. 退出码语义是跨脚本契约：改一个 CLI 的退出码要检查文档、上游判据、验收脚本三处。

## 9. Implementation Locations

```text
src/stock_analyzer/alpha_v2/validation/freeze_precheck.py    # 冻结前六道门
src/stock_analyzer/alpha_v2/validation/preflight.py          # 只读体检 + assert_preflight_gate + check_safety_flags
src/stock_analyzer/alpha_v2/validation/epoch.py              # epochs.json 账本 / open_epoch / require_open_epoch / close_epoch
src/stock_analyzer/alpha_v2/validation/shadow_capture.py     # 写一次快照 + 篡改拒绝
src/stock_analyzer/alpha_v2/validation/outcome_maturation.py # mature 门 0
src/stock_analyzer/alpha_v2/validation/validation_kpis.py    # clean-OOS 治理 + LOCKED 常量
src/stock_analyzer/alpha_v2/validation/production_funnel.py  # 漏斗绑定 cohort
src/stock_analyzer/runtime/services/live_shadow_cycle_service.py  # 唯一生产 shadow 调度入口（注意：在 runtime/ 下，不在 alpha_v2/ 下）
src/stock_analyzer/config.py                                 # AlphaV2Config 默认值 :1873-1877 / 交叉校验 :1971
```

## 10. Evidence

- `tests/test_alpha_v2_m3_enforcement.py`（manifest 替换/哈希伪造/身份漂移/关闭 epoch
  仍拒写/dirty worktree）
- `tests/test_alpha_v2_m3_epoch.py`、`test_alpha_v2_m3_shadow_capture.py`、
  `test_alpha_v2_m3_outcome_maturation.py`、`test_alpha_v2_m3_validation_kpis.py`
- `tests/test_alpha_v2_m4l_preflight.py`、`test_alpha_v2_m4l_shadow_cycle_scheduler.py`、
  `test_alpha_v2_m4l_cycle_clean_day_e2e.py`、`test_alpha_v2_m4l_e2e_rehearsal.py`
- commit：`a59429c` close M4-L R1 production blockers、`7c41fd6` funnel-bound cohort、
  `f5b4dc3` enforce frozen feature price mode in live runtime、
  `378abc9` require dual-delta readiness for live epoch
- 门禁文档：`docs/alpha_v2/StockAnalyzer_Alpha_V2_Codex_阶段验收门禁_20260917.md`
  （不完整）、`docs/alpha_v2/M4L_Production_Live_Bootstrap_Report.md`
- 状态澄清：commit `d323e54` 自述 `NOT_RUN_ON_PRODUCTION_DATA`；**仓库内没有证据
  表明任一阶段已在生产数据上通过端到端**。

## 11. As-of

- HEAD：`42caaca`（分支 `feat/alpha-v2-raw-execution-delta-r1`）
- 最后对照代码核实：2026-09-23
- 行号会随提交漂移；定位失败时按符号名（`assert_preflight_gate`、
  `require_epoch_identity_match`、`production_promotion`）搜索。
