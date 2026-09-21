# M4-L Production Live Bootstrap 报告

阶段：M4-L（Alpha V2 Production Live Bootstrap）
分支：`feat/alpha-v2-m4l-live-bootstrap`
基线：`origin/main` @ `29ece32`（包含 M4-H PR #84 合并 `0278d84`）
状态：`M4L_ENGINEERING_STATUS = PASS`｜`PRODUCTION_DATA_PREFLIGHT = NOT_RUN`（本机无生产数据，见 §7）｜`ALPHA_V2_EPOCH_001 = NOT_STARTED`｜`LIVE_CLEAN_OOS_DAYS = 0`｜`PRODUCTION_PROMOTION = LOCKED`

本阶段冻结边界保持不变：`M4H_STATUS = COMPLETE`、`Historical Evidence = MIXED`、
`RANKING_ABILITY = SUPPORTED_HISTORICALLY`、`ABSOLUTE_DIRECTIONAL_PROBABILITY = NOT_SUPPORTED`、
`ALPHA_VERIFIED = FALSE`。M4-L 只做 **Production Live OOS Data Infrastructure**，
不动模型/特征/标签/选择契约/legacy 阈值/cross review。

---

## 1. Architecture

M4-L 把"影子预测"从"Alpha 自己造的 cohort"改成"生产系统当天真实选出的 cohort 的观察者"：

```text
数据就绪 (nightly_data_ready, ops/nightly_readiness v2)
        ↓
Week5 生产夜扫（snapshot_funnel: Quality300 → Light100 → Deep50）
        ↓
生产漏斗工件 artifacts/runtime/production_funnel/<trade_date>/funnel_snapshot.json
        ↓（晚报发布时链接 report_id + 报告文件 sha256）
alpha_v2_shadow_cycle（正式 scheduler job，22:00–23:55 每 5 分钟）
   ├─ data_health_snapshot → artifacts/runtime/data_health.json
   ├─ shadow capture（cohort = 真实 Deep50；Alpha 只在其内排序）
   ├─ outcome mature（3/5/10/15D，含历史待成熟日）
   └─ validation KPI 报告
```

单向关系（§31）：`Production Funnel → Alpha V2 Shadow observes`，Alpha 侧**从不**回写生产漏斗或选股结果。
写入侧实现为 `live_shadow_cycle_service`（runtime 侧唯一消费 V2 flag 的模块），
生产入口 `runtime/service.py` 与 `week5_automation_service.py` 只保留中性委托、字面零提及 V2
（架构守卫 `tests/test_alpha_v2_baseline.py` 继续原地生效）。

## 2. Production Funnel Source

| 级别 | 生产来源（代码事实） | 名次来源 |
|---|---|---|
| Quality300 | `UniverseCandidateSelector.select()`（`service._select_universe_quality_candidates`） | `selected` 列表顺序（`rank_source=stage_order`） |
| Light100 | `_light_stage_from_snapshot`（baseline_score 排序）→ `prefilter.shortlisted` | 同上 |
| Deep50 | `_deep_stage_from_snapshot` / `_run_live_deep_stage`（funnel_score 排序）→ `prefilter.deep_stage.selected` | 同上 |

生产链此前**不落成员级工件**（正式晚报只有 counts），所以 M4-L 新增按日漏斗工件；
pinned/主题注入票在 `prefilter.pinned_symbols` 里单独成列（contract 的
`pinned_override_members`），**绝不**伪装成 Deep50 成员——它们在生产引擎里本就绕过 deep stage
直达 final pipeline。

**Does production shadow still synthesize its own Deep50?** → **NO**（生产模式直接拒绝
`research_proxy`，exit 10；`research_proxy` 只保留给 rehearsal/test，且恒 `clean_oos_eligible=false`）。

## 3. Funnel Provenance Contract

工件：`<funnel_root>/<trade_date>/funnel_snapshot.json`，schema `alpha_v2_production_funnel.v1`。

字段（§5 全覆盖）：`schema / signal_date / trade_date / created_at / source(=production_selection_engine) /
selection_contract_id / quality|light|deep_target / night_scan_report_id / night_scan_trace_id /
scan_status / funnel_policy / selector_mode / degraded{...} / quality|light|deep_members[{symbol,rank,rank_source,score}] /
quality|light|deep_count / pinned_override_members / pinned_added_count /
source_artifact_path / source_artifact_sha256 / funnel_snapshot_hash`。

写入纪律（`production_funnel.py`）：

1. **先证后链**：夜扫终态完成 snapshot_funnel 后落"未链接"版本；晚报 `publish()` 之后由
   `link_funnel_to_report` 补 `report_id` + 报告文件 sha256 并重算哈希；
2. **linked 后不可变**：任何改写抛 `FunnelTamperError`（同日双版本同样拒绝）；
3. **不写 ≠ 静默**：夜扫被门拦 / 非 snapshot_funnel / deep 未跑 → 不产出工件；
   capture 在生产模式找不到即 exit 10（fail-closed），调度层再落 missing 台账。

捕获侧硬门（`verify_funnel_for_capture`）：schema/来源/日期/契约 id 逐项对账；
`selector_mode ∈ {quality, quality_all_eligible}`（fallback/degraded 不算 clean）；
`funnel_snapshot_hash` 复算；生产模式下必须已链接正式晚报且报告文件 sha256 一致；
`Deep ⊆ Light ⊆ Quality`；计数自洽；`deep_members` 非空。

第二道闸（写后）：KPI 治理层在 `require_production_funnel=true` 的 epoch 里逐日复核
"当日 shadow 行集合 == manifest 内嵌 funnel 的 deep 成员、逐行 rank 一致、哈希复算一致、
来源为生产"（`validation_kpis._day_funnel_ok`），任一不符 → 该日不计 clean OOS。
旧清单无该键时保持 M3 语义（向后兼容）。

## 4. Scheduler Order

| 项 | 值 |
|---|---|
| job 名 | `alpha_v2_shadow_cycle`（interval job） |
| 注册条件 | `alpha_v2.enabled=true` 且 `shadow_only=true` 且 `enforce_final_selection=false` |
| 窗口 | `alpha_v2.live_cycle_start_time`(22:00) → `live_cycle_latest_time`(23:55)，每 5 分钟 |
| 前置 | 交易日 + 当天 `nightly_data_ready` 就绪 + 当天 funnel 已链接正式晚报 |
| 顺序 | data_health → capture → mature → validation KPI（同一 job 内串行，天然有序） |
| 重试 | 未就绪 → 快速返回 waiting（不占重活预算）；步骤失败 → 调度失败+退避重试；已捕获+已报告 → already_completed |
| 幂等 | 影子快照线程内幂等（同内容不改写、`recorded_at` 不变）；重复 tick 不再起重活；mature 只补未成熟 horizon |
| 分组 | heavy（`scheduler_group_for_job`，与夜扫同组串行），单步子进程超时 300/3600/5400/600s |
| 跨零点 | 窗口不跨 00:00：跨零点捕获即 backfill，当天永失 clean 资格（宁缺勿假） |

失败/边界行为（§18）：无 active epoch = safe skip；active epoch + 交易日 + 窗口末尾仍未就绪 =
missing 台账 + `alpha_v2_cycle_blocked_day` 审计（不静默成功）；capture 内部任一硬门失败 =
非零退出 → step_failed 审计 → 调度失败重试。

## 5. Clean OOS Eligibility

日级 clean 判据（既有 + M4-L 新增）：

1. `backfilled=false`（原语重算，不认行里的自述字段）；
2. 不在 missing 台账；
3. `data_health` 同日且 status=ok；
4. `recorded_at`/`actual_capture_date` == signal_date（真实墙钟兜底闸）；
5. 行身份 == epoch 冻结身份（8 键）；
6. 清单 `execution_price_mode=raw` 且 `validation_mode ∈ {production, test}`；
7. **（M4-L 新增）** 清单 `require_production_funnel=true` 时，当日 manifest 的 funnel 证据
   必须复算一致、日期/来源/selector_mode 权威、生产模式下已链接正式报告、cohort 与名次逐行对账。

行级：`clean_oos_row_eligible`（写入时刻自述）+ production 模式下 funnel 校验失败时**不写行**
（写审计/落 missing），杜绝"用不可信 cohort 冒充 Clean OOS"。

## 6. Production Data Preflight

入口：`scripts/alpha_v2_production_preflight.py`（只读 + 审计工件，不写数据库）。

| 检查 | 内容 | 失败等级 |
|---|---|---|
| A runtime/build identity | 复用 R4/R4.1 的 `resolve_runtime_code_identity` + violations | BLOCKED |
| B safety flags | `shadow_only=true`、`enforce_final_selection=false`、`training.enabled=false`、`auto_promotion.enabled=false`、`execution_price_mode=raw` | BLOCKED |
| C market db | 存在/可读、最新交易日、广度、重复逻辑键、尾段残缺（窗内=BLOCKED，窗外=WARN） | BLOCKED/WARN |
| D feature inputs | 冻结 schema 每列当天可算：缺列/全空=BLOCKED，低覆盖=WARN；显式跳过=WARN（永不伪装 PASS） | BLOCKED/WARN |
| E volume unit | 训练窗逐自然月 `turnover/volume` 单位诊断（见 §7） | BLOCKED |

产出：`artifacts/alpha_v2/audit/production_preflight_<ts>.json`，含
`verdict / blocking_findings / warnings / facts / runtime_identity / data_identity /
training_window(+hash) / feature_schema_identity / checks / generated_at / preflight_hash`。
exit code：`PASS|WARN → 0`、`BLOCKED → 1`、参数/执行错误 → 2。

**训练窗绑定（§23）**：`--training-start/--training-end` 或从 `--model-dir` 的
`provenance.window` 解析；两者都给必须一致。**硬门（§25）**：`alpha_v2_validation_freeze.py`
生产模式**必须**提供 `--preflight-report`，并校验
① verdict 非 BLOCKED（WARN 需显式 `--accept-preflight-warn`）、② 报告新鲜度
（`alpha_v2.preflight_max_age_hours`，默认 48h）、③ 报告 `runtime_identity.code_commit`
== 本次运行身份、④ 报告训练窗 == 冻结模型 provenance window；不通过 → **exit 7，不开 epoch**。
通过的工件指纹写进 freeze manifest 的 `production_preflight` 块（进 `freeze_manifest_hash` 覆盖）。
rehearsal 不做此门（排演允许骨架数据），但清单 `require_production_funnel=false` 如实落账。

## 7. Volume Unit Result

方法（与 M4-H inventory `_unit_regime_probe` 同源、阈值 100）：`turnover/volume ≈ 当日均价`
→ 股（share-like）；`≈ 100×均价` → 手（lot-like）。逐月统计 share-like 比例，判据：
月内比例落 (0.2, 0.8) = 月内混合；窗口内同时存在 ≥0.8 与 ≤0.2 的月份 = 单位切换 → **BLOCKED**。

**本地副本实测（2026-09-21，非生产数据）**：对本机
`artifacts/warehouse/market.duckdb`（本地副本，数据止于 2026-04-03，988 万行）以
训练窗 2025-06-02→2026-03-31 跑真实 preflight，审计工件
`artifacts/alpha_v2/audit/production_preflight_2026-09-21T074101.916001_0800.json`：

```text
verdict = BLOCKED
blocking = volume_units:mixed_volume_units_month:2025-10
warnings = market_db:tail_fragment_after_window

2025-06 share_like=0.9840 | 2025-07 0.9821 | 2025-08 0.9760 | 2025-09 0.8090
2025-10 share_like=0.2893 | 2025-11 0.2916 | 2025-12 0.2912
2026-01 share_like=0.2860 | 2026-02 0.2858 | 2026-03 0.2604

unit_status_by_month: 2025-06..09 = share；2025-10..2026-03 = mixed
mixed_months = [2025-10 … 2026-03]；affected_symbol_count = 5,379
affected_date_range = [2025-10, 2026-03]
```

即：本地副本在 **2025-10 出现单位口径切换**，且此后约 29% 的 (symbol, 月) 仍是"股"口径
——两种量纲在**同一训练窗、同一截面内并存**（跨票不一致），任何按该窗口训练的模型都会
同时看到两种成交量尺度。**这正是 §22 要拦住的形态，判定 BLOCKED 是正确结果。**

**生产数据（NAS）判定 = NOT_RUN_ON_PRODUCTION_DATA**：本机副本不代表生产现状
（NAS 生产数据止于 2026-09-16，且经多次增量/回填），M4-H 的历史结论也不能替代本阶段实测；
NAS 上线前必须用 `--model-dir` 绑定真实训练窗再跑一次 preflight，以其 verdict 为准。

单元级实测（deterministic fixture）：share-like 窗 → PASS、跨月单位切换 → BLOCKED、
月内混合 → BLOCKED、缺列 → BLOCKED。

## 8. Failure Modes

| 场景 | 行为 | 证据 |
|---|---|---|
| 生产 funnel 不存在 | capture exit 10；调度层窗口末尾落 missing 台账 + 审计 | `test_blocked_day_records_missing_after_deadline`、`test_attack_a_source_label_without_funnel_artifact_fails` |
| funnel 是昨天/日期不符 | exit 10（日期逐项对账） | `test_verify_rejects_wrong_date` |
| funnel 契约 id 不符 | exit 10 | `test_verify_rejects_wrong_contract` |
| funnel 已被篡改（改成员/改报告） | 哈希/文件 sha256 抓到，exit 10 | `test_verify_detects_tampered_members`、`test_verify_checks_report_file_hash` |
| selector_mode=fallback/degraded | exit 10（不是当天真实选择） | `test_verify_rejects_non_authoritative_selector_mode` |
| pinned 冒充 deep 成员 | funnel 层就是分列字段；嵌套校验拒绝混入 | `test_extract_members_ranks_and_pinned_separation`、§12 |
| 生产模式写 research_proxy | exit 10 | `test_rehearsal_rejects_alpha_selfmade_cohort_in_production_mode` |
| data_health 缺失/降级 | 行照写但当日不计 clean | 既有 `test_data_health_*`（M3 套件） |
| 事后 backfill | 落 `backfilled=true` + `clean_oos_eligible=false` | 既有 `test_explicit_backfill_is_allowed_and_marked` |
| 同一天跑两次 cycle | 第二次 already_completed，不重复写 | `test_rerun_is_idempotent` |
| epoch 开启后改模型工件 | capture 模型加载门拒绝（exit 5） | `test_attack_h_model_artifact_tamper_after_epoch_is_rejected` |
| 无 active epoch | safe skip（不破坏调度） | `test_no_active_epoch_is_safe_skip` |
| 运行时禁止确定性时钟（生产） | 生产 epoch 不接受注入时钟（R3 既有闸门） | M3 套件 `test_production_never_allows_clock_injection*` |

## 9. Rollback

- **代码**：回滚即 `git revert`（或把 `alpha_v2.enabled` 设为 false）。flag 关闭时
  `registration()` 返回 None → 不注册任何 job；emit/link 为 no-op；
  Legacy/Week5/nightly 行为与 main 逐字段一致（`test_legacy_behavior_surface_*` EXACT MATCH）。
- **工件**：funnel 工件与 shadow/KPI 产物都在独立目录（`artifacts/runtime/production_funnel/`、
  `artifacts/alpha_v2/`），删除/保留均不影响生产选股；linked funnel 不可改的纪律与回滚无关
  （回滚后重新开启会开新 epoch，正是不改历史的口径）。
- **epoch**：一旦开启，任何口径调整都必须走 close-epoch → 新 epoch，绝不允许原地改
  （`freeze_manifest_hash` 锚定 + `ShadowTamperError`）。

## 10. Non-goals

- 不调模型/参数/特征/label/选择契约/legacy 阈值/cross review；
- 不启用 final selection（`enforce_final_selection=false` 恒真）；
- 不做 Production Promotion、不开 epoch、不部署 NAS（本阶段零 NAS 改动）；
- **不修 volume 单位、不改历史数据库、不自动乘除 100**——preflight 只发现/量化/阻断/记录；
- 不把 rehearsal/CI 的 research_proxy 结果算作任何证据。

## 11. Test Evidence

本轮新增测试文件 6 个（69 例）+ 无 git 容器 smoke 更新，全部 deterministic fixture。
实测（2026-09-21，本机）：

| 命令 | 结果 |
|---|---|
| `python -m pytest tests/ -k "alpha_v2 or m4l"`（junit） | **570 passed / 0 failed** |
| `python -m pytest tests/`（全量，junit） | **3739 passed / 0 failed / 0 errors / 2 skipped** |
| `python scripts/run_quality_gate.py --stage clean-scope --fail-on-error` | exit 0（blocking=[]，ruff+mypy 通过） |
| `python scripts/run_quality_gate.py --stage full --fail-on-error`（CI 等价） | exit 0（blocking=[]） |
| Attack A–H 逐条实跑（`artifacts/quality/m4l_attacks.xml`） | 15 cases / 0 failed |
| 本机真实 preflight（非生产数据） | verdict=BLOCKED（见 §7），审计工件 `artifacts/alpha_v2/audit/production_preflight_2026-09-21T074101.916001_0800.json` |

（2 skipped 为已知的 Windows 环境 bash 语法跳过项，与基线一致。）

| 文件 | 例数 | 覆盖 |
|---|---|---|
| `tests/test_alpha_v2_m4l_production_funnel.py` | 16 | 契约提取/ranks/pinned 分离/emit-link 纪律/捕获硬门/篡改 |
| `tests/test_alpha_v2_m4l_e2e_rehearsal.py` | 6 | 端到端排演（capture→mature→KPI）、Attack A/D/H |
| `tests/test_alpha_v2_m4l_funnel_emission.py` | 8 | 夜扫 emit / 发布 link 钩子与失败降级 |
| `tests/test_alpha_v2_m4l_kpi_funnel_gate.py` | 8 | KPI 写后复核（哈希/日期/来源/cohort/rank/legacy 兼容） |
| `tests/test_alpha_v2_m4l_shadow_cycle_scheduler.py` | 9 | 注册条件/safe skip/prereq/顺序/失败/幂等 |
| `tests/test_alpha_v2_m4l_preflight.py` | 22 | volume 门/DB 检查/安全开关/身份/特征/preflight 硬门 |

### Red Team A–H（逐条实跑，junit 留档 `artifacts/quality/m4l_attacks.xml`）

| Attack | 构造 | 结果 | 用例 |
|---|---|---|---|
| A | 只写 `quality_pool_source=production_selection_engine`、不给真实 funnel 工件 | **PASS（拒绝）** exit 10，0 行落盘 | `test_attack_a_source_label_without_funnel_artifact_fails` |
| A′ | production epoch 里声明 `research_proxy` | **PASS（拒绝）** exit 10 | `test_rehearsal_rejects_alpha_selfmade_cohort_in_production_mode` |
| B | 用昨天 funnel 冒充今天（date mismatch） | **PASS（拒绝）** | `test_verify_rejects_wrong_date` |
| C | 篡改 funnel JSON 成员（保留 report_id） | **PASS（抓到）** 写时哈希门 | `test_verify_detects_tampered_members` |
| C′ | 保留 report_id 但改正式报告文件 | **PASS（抓到）** 报告 sha256 门 | `test_verify_checks_report_file_hash` |
| C″ | 写时蒙混过关 → 写后 KPI 复核 | **PASS（抓到）** 不计 clean | `test_tampered_funnel_members_are_caught` |
| D | Alpha 自选的 Top（与生产名次相反） | **PASS**：cohort 仍以生产 Deep50 为准，rank 用生产名次 | `test_rehearsal_end_to_end_production_cohort` |
| E | scheduler 当天跑两次 | **PASS**：第二次 already_completed，无第二份快照/clean 日 | `test_rerun_is_idempotent` |
| F | 事后 backfill | **PASS**：`backfilled=true` + `clean_oos_eligible=false` | `test_explicit_backfill_is_allowed_and_marked` |
| G | data_health 缺失/降级/陈旧 | **PASS**：行照写、当日不计 clean | `test_data_health_not_ok_blocks_clean_but_not_capture`（5 组参数） |
| H | epoch 开启后改模型工件 | **PASS（拒绝）** 模型加载门 | `test_attack_h_model_artifact_tamper_after_epoch_is_rejected` |

补充守卫：`NO_GIT_CONTAINER_SMOKE`（无 git 容器形态，真实 CLI）E 步期望已按 M4-L 更新为
"身份完好 → 生产漏斗硬门 exit 10（fail-closed）；身份破坏 → 仍 exit 3"，证明新门没有改变
"身份问题必须表现为身份退出码"的既有契约。

## 12. NAS Deployment Prerequisites

上线前（需用户授权，不在本阶段执行）：

1. 用生产镜像跑 `scripts/alpha_v2_production_preflight.py`（`--model-dir` 指向待冻结模型
   工件、训练窗与模型 provenance 绑死），拿到 verdict；BLOCKED 即停（先做数据治理）；
2. 确认 `nightly.enabled=true`（funnel 链接依赖晚报发布链）与
   `alpha_v2.production_funnel_root` 落在 artifacts 持久卷；
3. 复核 21:45 夜扫 → 22:00–23:55 循环窗口在实际运行时长下是否足够（重活 heavy 组串行）；
4. 首日观察：`alpha_v2_production_funnel_emitted → linked → cycle_completed` 审计事件链完整；
5. 环境变量启用（不写进受跟踪默认配置）：`SA__ALPHA_V2__ENABLED=true`。
