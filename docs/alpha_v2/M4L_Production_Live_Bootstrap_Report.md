# M4-L Production Live Bootstrap 报告

阶段：M4-L（Alpha V2 Production Live Bootstrap）
分支：`feat/alpha-v2-m4l-live-bootstrap`
基线：`origin/main` @ `29ece32`（包含 M4-H PR #84 合并 `0278d84`）
状态（R1 修复后）：`M4L_ENGINEERING_STATUS = PASS`｜`M4L_BLOCKERS_REMAINING = 0`｜`PRODUCTION_DATA_PREFLIGHT = NOT_RUN`（本机无生产数据，见 §7）｜`ALPHA_V2_EPOCH_001 = NOT_STARTED`｜`LIVE_CLEAN_OOS_DAYS = 0`｜`PRODUCTION_PROMOTION = LOCKED`

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

---

# R1 修复轮（M4-L Final Blocking Fix R1）

外部复核（2026-09-21）确认首轮总体方向正确，但列出 7 项 Production Blocking。
以下**保留首轮全部发现历史**，逐条记录：发现 → 修复 → 证据。

## B1 调度层 data_health 输入未接齐 → Live Clean OOS 永远 0（已修）

**发现**：首轮 daily cycle 调 ``alpha_v2_data_health_snapshot.py`` 时只给
``--as-of/--market-db/--out``，S08 七项输入（universe/board/feature/model/breadth）
一个都没传 → 全部 degraded → ``status != healthy`` → 每天 immutable 落
``clean_oos_eligible=false``，**样本门永不推进**。

**修复**：新增 ``alpha_v2/validation/live_data_health_inputs.py``，为 S08 七项各建权威派生：

| S08 输入 | 权威来源（只读） |
|---|---|
| latest_trade_date | ``market.duckdb`` ``max(date)`` |
| universe_snapshot | ``resolve_asof_universe``（S03 唯一实现）；候选名单 = 库内全集，**不用 Quality300 冒充分母** |
| valid_symbol_count | expected_active ∩（as_of 当日有 bar）——分子分母同集合 |
| board_coverage | 同一天同一 expected_active 集合按 board 分组 |
| feature_snapshot | ``features_light/current.json`` + ``snapshot_is_current`` + 交易日均值对齐 |
| model_identity | **active epoch 的 frozen shadow model**：逐文件重算 artifact_hash + 比对 epoch/清单 identity（不是 legacy champion） |
| breadth_artifact | ``compute_market_breadth_from_warehouse``（唯一 builders）现算 → 落**影子证据路径**（``artifacts/alpha_v2/runtime/market_breadth_evidence.json``） |

**广度为什么落影子路径**：生产 ``artifacts/runtime/market_breadth.json`` 缺失使 live
广度门处于 fail-open；直接补写会让"低广度禁买"从静默失效变为生效——那是
**选股语义变更**，M4-L 明令禁止（observer 角色）。因此用同一 builder（同数据/同代码/
同 as_of）把证据写到影子路径，既满足 S08"缺失不得当健康"，又不动生产门；生产
breadth 接线属另一个授权项。

CLI 新增 ``--derive-inputs``（显式路径参数仍优先，便于人工审计单个输入）。

## B2 degraded 的 data_health 仍会触发 immutable capture（已修）

**修复**：``run_daily_cycle`` 改为"生成 → **读回验证** → 才 capture"：

```text
data_health CLI（--derive-inputs）
    ↓ 读回工件并过 data_health_gate_ok（status=ok 且 as_of 同日）
healthy ? capture（本槽位）
        : deadline 前 waiting（不 capture、不落任何快照，下一个 5min 槽位重试）
        : deadline 后 record missing + audit（当日永不 clean）
```

``returncode==0`` 不再作为健康证据（degraded 也返回 0）。等待态返回里带
``data_health`` 诊断块（排障不必翻工件）。

## B3 生产链 prerequisite 未进硬门（已修）

新增 ``check_production_prerequisites``：``week5.enabled`` / ``week5.auto_run`` /
``full_market_automation_enabled`` / ``nightly.enabled`` / ``alpha_v2.enabled`` /
``shadow_only`` / ``enforce=false``，外加**时间窗可达性**（alpha 循环 latest 必须晚于
夜扫最晚起跑，否则晚跑夜扫永远赶不上当天窗口 ⇒ 天天 backfill）。任一不满足 → BLOCKED。

``alpha_v2.enabled`` 的语义（§5.2）：``config_hash`` 覆盖该字段 ⇒ 开 epoch 时关、
之后打开会造成 runtime identity 漂移（capture 每天 exit 3）；因此**生产冻结/开 epoch
前必须 enabled=true**，硬门强制。

本机实测（tracked 默认配置）即命中该门：``nightly_enabled`` / ``alpha_v2_enabled`` /
``full_market_automation_enabled`` 全为 false → BLOCKED（正是外部复核担心的形态）。

## B4 Preflight 未绑定"实际冻结模型"（已修）

- preflight ``--model-dir`` 现在记录完整 ``model_identity``（model_id / artifact_hash /
  feature_schema_hash / model_training_code_commit / provenance.window /
  training_data_fingerprint / artifact_verified），且**不带 ``--model-dir`` 直接 BLOCKED**；
- ``assert_preflight_gate`` 新增 ``model_block`` 参数，**逐项比对**上述字段与训练窗；
  任一不一致 exit 7、不开 epoch（PF-1：同 code 同窗但不同 artifact/schema/指纹全部被拒）。

## B5 特征 fill-zero 假健康（已修）

``check_feature_inputs`` 接入 ``feature_diagnosis`` 四类分类，诊断窗取
**40 个交易日 × ≤300 symbols** 的多日截面（不再单日样本）：

- required 模型特征命中 ``UPSTREAM_NOT_POPULATED`` / ``FILL_ZERO_ARTIFACT`` /
  ``DATA_MISSINGNESS`` → **BLOCKED**（fill-zero 会把 ``notna()`` 刷成 100%，只看覆盖率必假 PASS）；
- ``REAL_CONSTANT`` → **WARN**（按项目现有治理口径不判死，但必须可见）；
- 非 required 列的同名分类 → WARN（如实记录）。

## B6 训练数据无内容身份（已修）

新增 ``training_data_fingerprint``（确定性、内容级）：

```text
sha256(canonical_header(列清单+窗口) || 逐行 canonical(symbol,date,OHLC,volume,turnover))
```

性质（有测试钉住）：同数据同 hash；窗口内改任意价格/量/额 → hash 变；
**窗口外追加交易日 → hash 不变**。链条：
``alpha_v2_shadow_model_freeze.py`` 训练时写入 model provenance →
preflight 对同窗重算并比对（不一致 BLOCKED）→ validation freeze 再核对两处一致。

## B7 volume 判别有价格依赖 + affected 语义错（已修）

- 判别公式改为**价格归一化**：``unit_scale = turnover / (volume × close)``，
  ``< 10`` 判 share（≈1）、否则 lot（≈100）；旧的绝对阈值
  ``turnover/volume > 100`` 会把高价股误判成手、低价股误判成股（VOL-1/VOL-2 钉住）；
- ``affected_symbol_count`` 改回**真正 distinct symbols**，另立
  ``affected_symbol_month_count`` 记 (symbol, month) 对，两个语义不再混用。

## B8 漏斗来源证据名实不符（已修）

- 新增**night-scan source evidence** 工件（``<date>/night_scan_source_evidence.json``，
  当日不可变）：含 Quality/Light/Deep 成员原文 + contract + trace + selector_mode +
  pinned；funnel 快照**从它抽取**；
- 字段名实分离：``source_night_scan_artifact_path/sha256``（成员来源）与
  ``published_report_id/path/sha256``（晚报正式报告）；
- 捕获时复算源证据文件 sha256 并**逐成员对账**；link 时除哈希外做**语义校验**
  （report_id / trade_date / report_kind=formal / scan_status ∈ {completed, empty}）。

## §8 契约补强 / §9 deadline 行为（已做）

- 契约新增：三级成员**不得重复 symbol**；``rank`` 必须为正整数、唯一、与 stage order
  自洽（1..N）；``Deep ⊆ Light ⊆ Quality`` 保持；
- 当天无法捕获（deadline 仍未就绪）时：落 missing 台账 + 审计，但**继续推进历史日的
  mature 与 KPI**（missing 只约束它自己，不阻断既有权重日的 3/5/10/15D 成熟）。

## R1 本机真实数据重跑（§13）

```text
OLD PREFLIGHT VERDICT = BLOCKED（首轮 artifact
    artifacts/alpha_v2/audit/production_preflight_2026-09-21T074101.916001_0800.json）
NEW PREFLIGHT VERDICT = BLOCKED（本轮 artifact
    artifacts/alpha_v2/audit/production_preflight_2026-09-21T103054.189598_0800.json）

old volume detector = turnover / volume > 100（绝对阈值）
new normalized detector = turnover / (volume * close) < 10 → share

month      old share_like   new share_like   new unit_scale_median   new status
2025-06        0.9840           1.0000              0.9999              share
2025-07        0.9821           1.0000              0.9996              share
2025-08        0.9760           0.9994              0.9990              share
2025-09        0.8090           0.8355              1.0020              share
2025-10        0.2893           0.3070             100.0000             mixed
2025-11        0.2916           0.3078             100.0000             mixed
2025-12        0.2912           0.3086             100.0000             mixed
2026-01        0.2860           0.3089             100.0000             mixed
2026-02        0.2858           0.3095             100.0000             mixed
2026-03        0.2604           0.2816             100.0000             mixed

affected distinct symbols      = 5170   （旧字段实为 symbol-month 对：5379）
affected symbol-month pairs    = 5170
mixed months                   = 2025-10 … 2026-03
affected_date_range            = [2025-10, 2026-03]
training_data_fingerprint      = 需 --model-dir 才能计算（本轮无生产冻结模型 → 未计算）
```

**解读（不是"为了保持 BLOCKED"）**：归一化后 share 月份的 ``unit_scale`` 中位数
收敛到 **1.0000**（理论值），切换后月份收敛到 **100.0**（理论值），说明新判据测的是
真正的单位语义；而"切换月内两种单位并存"的结论**同时被新旧两种判据独立得出**
（旧 0.289 vs 新 0.307 的比例几乎相同）——混合单位不是绝对阈值造成的假象。
按 §13 要求：若新判据给 PASS 会如实报告，实际仍为 BLOCKED，故如实报告。

## R1 测试证据

```text
pytest tests/ -k "alpha_v2 or m4l"                    : 603 passed / 0 failed（junit）
  · 新增 DH-1 / DH-7 / DH-2,4,5,6 端到端（真实 CLI + 合成库）
  · 新增 PF-1..PF-4、VOL-1/2、FUNNEL-1/2
  · 新增 §8 契约补强（重复成员 / rank 自洽）
pytest tests/（全量，junit）                           : 见下方最终计数
run_quality_gate --stage clean-scope --fail-on-error  : exit 0
run_quality_gate --stage full --fail-on-error         : exit 0
GitHub CI（PR #85 追加提交）                          : 双 run 绿
```

DH-1（核心目的验证）：production prerequisites 就绪 + S08 七项全 healthy +
funnel linked + active epoch → ``alpha_v2_shadow_cycle`` 跑完后
**shadow snapshot exists / data_health.status == ok / clean_oos_eligible == true /
KPI ``clean_oos_days == 1``**。
DH-7（核心回归）：先缺 feature snapshot → 不 capture；同一晚补齐 → clean +1。
DH-2/4/5/6：分别缺 universe / feature / model / breadth → deadline 前 waiting 且
**不写任何快照**，并断言降级项就是那一项。

## 12. NAS Deployment Prerequisites

上线前（需用户授权，不在本阶段执行）：

1. 用生产镜像跑 `scripts/alpha_v2_production_preflight.py`（`--model-dir` 指向待冻结模型
   工件、训练窗与模型 provenance 绑死），拿到 verdict；BLOCKED 即停（先做数据治理）；
2. 确认 `nightly.enabled=true`（funnel 链接依赖晚报发布链）与
   `alpha_v2.production_funnel_root` 落在 artifacts 持久卷；
3. 复核 21:45 夜扫 → 22:00–23:55 循环窗口在实际运行时长下是否足够（重活 heavy 组串行）；
4. 首日观察：`alpha_v2_production_funnel_emitted → linked → cycle_completed` 审计事件链完整；
5. 环境变量启用（不写进受跟踪默认配置）：`SA__ALPHA_V2__ENABLED=true`。

---

# R1.1 修复轮（Training Provenance Sealing）

外部复核（2026-09-21）在 R1 之上指出**最后一个证据完整性缺口**：训练数据指纹只覆盖
OHLCV/turnover 与"决策窗"，而**真正被训练读取的输入**比这更宽；且这些身份字段
（窗口、warmup、指纹）不在工件哈希的受保护集合里——等于允许"换一份数据、贴上原指纹"。
本轮只做这一件事：把训练 provenance 变成**不可事后改写**的身份。

## 旧（R1）

```text
training_data_fingerprint = sha256(v1 | symbol,date,open,high,low,close,volume,turnover
                                   | window_start..window_end)
artifact_hash             = sha256(model_id, 特征列, 参数, 目标, 校准, 文件哈希, code_commit)
```

两个洞：

1. **列不全**：`float_market_cap`（→ S12 风格维度 → 基准成分 → `excess_return_*` **目标**）、
   `board`/`is_st`/`is_delisting_risk`/`suspended`（涨跌停与可成交语义）、
   `pre_close`/`up_limit`/`down_limit`（`limit_rule` + `ExecutionMatcher`）、
   `price_series_mode`（价格口径认证）都不在指纹里；
2. **窗口不全**：`load_daily_panel(warmup_days=N)` 实际读取
   `window_start - N 自然日` 起的数据，warmup 段参与 rolling/MA/EMA/return/波动/量比等
   **全部技术特征**的构造，却完全在指纹覆盖之外；
3. **provenance 不受保护**：`artifact_hash` 只保护 `code_commit`；改写
   `provenance.window` / `warmup_days` / `training_data_fingerprint` **不会**破坏工件完整性。

## 新（R1.1）

```text
fingerprint v2 = sha256( canonical_json(header) ‖ 逐行 17 列 )
  header = {fingerprint_version, decision_window, source_window, warmup_days,
            requested_source_columns, available_source_columns,
            missing_optional_source_columns, row_count}
artifact_hash v2 = sha256(v1 正文 … ‖ artifact_hash_version, config_hash,
                          training_provenance{window, warmup_days, source_window,
                            training_data_fingerprint(+version), rows, columns, …})
```

## 训练输入依赖审计（先看代码，不照抄清单）

| 环节 | 实际消费的源列 | 影响 |
|---|---|---|
| `load_daily_panel` | **`PANEL_BAR_COLUMNS` 全 17 列**（`trade_date`←`date`），行范围 `window_start - warmup_days 自然日 .. window_end` | 决定训练帧本身 |
| `panel.pit_universe` | `symbol` + `trade_date`（`build_pit_stats`） | 决定**哪些决策行存在** |
| `FeatureEngineer`（经 `daily_feature_frame`） | 面板 bars 的 OHLCV/成交额等 | 特征值 |
| `build_label_v2` + `ExecutionMatcher`/`bar_view`/`limit_rule` | `open/high/low/close/volume/turnover/board/is_st/up_limit/down_limit/pre_close/suspended` | **可成交语义与标签** |
| `compute_style_features`（S12） | `float_market_cap`、`board`、`close`、`turnover`（20 日窗） | 风格维度 → 基准成分 → `excess_return_*` **目标** |
| `certify_price_mode` | `close`、`board`、`is_st`、`price_series_mode` | 价格口径认证 → provenance |

派生列（`prev_close_raw` / `pre_close_source` / `listing_days_lower_bound`）**不要求**
数据库存在：它们由上述源列确定性派生；但**会影响派生路径的源列存在性**必须进身份。
清单本身不再手写——`MODEL_TRAINING_SOURCE_COLUMNS` 从 `PANEL_BAR_COLUMNS` 派生
（面板读什么就 hash 什么），必需列（symbol/date/OHLC/volume/turnover）缺失直接报错。

## Schema presence 进 header

"`pre_close` 列不存在"与"列存在但全空"会走**不同**的派生路径（前者退回上一根 raw 收盘，
语义不同：除权日不对等），因此两者必须能区分——header 记录
`available_source_columns` / `missing_optional_source_columns`，指纹因此不同。

## 工件哈希版本（§6 兼容）

复用既有 manifest schema 做**字段级**演进（不另造平行体系）：manifest 多一个
`artifact_hash_version`，加载时按记录值复算。

| 版本 | 正文 | 用途 |
|---|---|---|
| v1 | 7 键（R4.1 原样，**逐字节不变**） | 历史归档工件仍可加载（磁盘上仍有 v1 模型） |
| v2 | v1 + 版本 + `config_hash` + `training_provenance` | 生产新工件；生产路径拒绝 v1 |

生产拒绝点（`require_sealed_provenance=True`）：preflight `check_model_identity`、
validation freeze 生产分支、capture（production epoch）、S08 `model_identity` 派生
（按 `validation_mode=production` 判定）。**安全方向**：删掉 v2 工件的版本字段并不会
"降级成 v1"——哈希复算会失败（版本进正文）。

## FP-1..FP-10（§10）

`tests/test_alpha_v2_m41_training_provenance_sealing.py`（28 例）：

| 用例 | 断言 |
|---|---|
| FP-0 | `source_window` **等于** `load_daily_panel` 实际装载的最早 bar（窗口声明与真实读取对齐） |
| FP-1 | 决策窗内改 `close` → 指纹变 |
| FP-2 | 改 `float_market_cap` → 指纹变；**反证**：R1 的 8 列口径对这种改动是盲的（前后指纹相同） |
| FP-3 | 改 `board` / `is_st` / `is_delisting_risk` / `suspended` / `pre_close` / `price_series_mode` → 指纹变；列**存在但全空 vs 不存在**必须不同；必需列缺失 → 报错 |
| FP-4 | 改 warmup 段（`window_start` 之前、`source_window` 之内）→ 指纹变；R1 口径（warmup=0）不变 |
| FP-5 | 改 `source_window` 之前的数据 → 指纹不变 |
| FP-6 | 只在 `window_end` 之后追加交易日 → 指纹不变 |
| FP-7/8/9 | 篡改 `provenance.training_data_fingerprint` / `window` / `warmup_days`（不重算哈希）→ `load_frozen_model` 拒绝 |
| FP-7 家族 | 篡改 `config_hash` 同样被拒（"不要只保护 code_commit"）；v2 版本号但封存项缺失 → 生产加载拒绝 |
| FP-10 | 当前 DB 复算与模型指纹不符 → preflight BLOCKED；真实 freeze CLI **exit 7 且不落盘**；同环境同工件的**一致**报告 → rc=0 并落 `production_preflight` 绑定块 |

## 本机真实数据证据（§11 不重跑研究）

对 `artifacts/warehouse/market.duckdb`、窗口 `2025-06-02..2026-03-31`、`warmup_days=200`：

```text
fingerprint_version          = v2
decision_window              = 2025-06-02 .. 2026-03-31
source_window                = 2024-11-14 .. 2026-03-31      ← R1 完全没覆盖的那一段
warmup_days                  = 200
requested_source_columns     = 17（全部面板源列，派生自 PANEL_BAR_COLUMNS）
available_source_columns     = 16
missing_optional_source_columns = ["pre_close"]              ← 本机库没有交易所前收列
row_count（source_window）    = 1,708,240
row_count（R1 口径：决策窗）   = 1,040,786                    ← 64% 的输入行此前不在覆盖内
fingerprint                  = dbd9c5f685fb765d…
两次独立运行结果一致           = True（确定性）
耗时                          = 12.2 s（1.7M 行 × 17 列，流式）
```

`pre_close` 缺失是**新发现的事实**（此前没有任何工件记录它）：本机 `daily_bars` 没有
交易所前收列，`load_daily_panel` 对每一行都走 `derived_previous_close` 回退——除权日
与真实前收不等。指纹 header 现在把这个事实钉成身份的一部分；是否需要补列属**数据治理**
（M4-L 只发现/记录/阻断，不改数据）。

## 版本兼容实测（磁盘上的真实归档工件）

不是"照公式推导"，而是拿磁盘上实际存在的归档工件分别用两代代码加载：

```text
工件：artifacts/alpha_v2_rehearsal/model/alpha_v2_shadow_epoch_001/model_manifest.json
     created_at = 2026-09-19（早于 R4.1）

R1 基线（8eea663 worktree）      → FAIL: 冻结模型 artifact_hash 与内容不符（5978ef5d… != 468c12a2…）
R1.1（本轮）                     → FAIL: 同一对哈希值，同一结论
独立复算该工件正文（6 键，无 code_commit）→ 5978ef5d… == 记录值（公式可解释）
```

结论：这份工件在**两代代码上都不可加载**，原因是 R4.1 把 `code_commit` 加进哈希正文
（那是 R4.1 已接受的契约变更），**与 R1.1 无关**。R1.1 的作用恰恰相反——它给哈希加了
版本轴，使得 v2 的新增内容**不会**连带废掉 v1 时代工件；能被 R1 加载的工件，R1.1 一律
照旧加载（生产路径则一律要求 v2，见上表）。

## 本机 preflight 重跑：R1（已提交）vs R1.1（§12）

同一份数据、同一组参数（`--market-db artifacts/warehouse/market.duckdb`、
`--training-start 2025-06-02 --training-end 2026-03-31`、`execution_price_mode=raw`、
5 列 feature schema、**不带** `--model-dir`）：

```text
OLD（R1 已提交代码 8eea663，git worktree 检出运行）
    artifacts/alpha_v2/audit/production_preflight_2026-09-21T141011.830782_0800.json
NEW（R1.1 工作区）
    artifacts/alpha_v2/audit/production_preflight_2026-09-21T140841.852539_0800.json

verdict            : BLOCKED == BLOCKED
blocking_findings  : 完全一致（prerequisites ×3 / volume_units / model_identity / feature_inputs）
warnings           : 完全一致（market_db:tail_fragment_after_window）
volume_units 事实   : 逐字段一致
feature_inputs 事实 : 逐字段一致（probed_rows=11,387 / 40 个诊断日）
data_identity 差异  : 新增 5 个键（training_data_fingerprint_version / warmup_days /
                      source_window / training_data_rows / training_data_columns）
```

即：R1.1 **不改变任何判定**，只是把训练输入身份写进报告（无 `--model-dir` 时指纹
照旧不参与，`model_dir_required` 仍 BLOCKED）。

> 说明（诚实记录）：R1 报告 §R1 引用的更早工件
> `production_preflight_2026-09-21T103054…json` 里 `feature_inputs` 是 BLOCKED
> （`feature_probe_as_of_not_trading_day`）。那是**修复过程中的中间态代码**产出的
> （10:30，早于 R1 提交 11:18；多日诊断窗 `diagnosis_days * 1.6` 是 a59429c 才引入的），
> 不是 R1 最终代码的行为——上面用已提交代码重跑得到的就是 PASS。该工件对
> **volume 判据对比**这部分结论仍然有效（那部分当时已是最终实现）。

## NO_GIT_CONTAINER_SMOKE（R1.1 工作区，7/7 PASS）

```text
A runtime identity      PASS（container_build_identity / git_available=false）
B model freeze identity PASS（身份放行后因缺市场库中止；破坏身份 exit 3）
C validation freeze     PASS（rc=0：**封存工件** + R1.1 preflight 报告逐项对账通过；
                               缺 --model-dir 时 exit 6 = schema 硬门）
D open epoch            PASS
E shadow capture        PASS（身份完好停在 M4-L 生产漏斗硬门 exit 10；破坏身份 exit 3）
F mature                PASS
G no-git no-identity    PASS（rehearsal 干净 exit 5，不落盘、无 traceback）
verdict = PASS
```

步 C 是本轮的关键回归：生产 freeze 现在要求工件 `artifact_hash_version=v2` 且封存项
齐备，同时 preflight 报告必须携带指纹契约身份——这条链在无 git 容器形态下走通。

## R1.1 测试与门禁

```text
pytest tests/ -k "alpha_v2 or m4l"（junit m4l_r11_targeted.xml）   : 637 passed / 0 failed / 0 skipped
   · 新增 test_alpha_v2_m41_training_provenance_sealing.py（29 例：FP-0..FP-10 + 版本兼容 + CLI 拒绝）
run_quality_gate --stage clean-scope --fail-on-error                : exit 0（ruff + mypy blocking 通过）
run_quality_gate --stage full --fail-on-error                       : exit 0（812.6 s，coverage 80.72% ≥ 75 门槛）
NO_GIT_CONTAINER_SMOKE                                              : verdict PASS（7/7）
pytest tests/（全量串行，junit m4l_r11_full.xml）                   : 3806 passed / 0 failed / 0 errors / 2 skipped（31.5 min，exit 0）
GitHub CI（PR #85 追加提交）                                        : 待本轮 push 后
```

计数可核对：3806 = R1 基线 3772 + 新增封存套件 29 例 + preflight 新增 5 例
（`test_model_identity_rejects_unsealed_v1_artifact` 1 + `test_fp4_warmup_…` 1 +
`test_gate_rejects_missing_fingerprint_contract_identity` 参数化 3）。2 skipped 与基线
一致（Windows 环境跳过 bash 语法用例）。

### 本轮自查发现并修掉的两个真实缺陷（写进来，不藏）

1. **v1 正文被我改成 8 键 → 历史工件全部失效**：初版 `_artifact_hash_body` 把
   `artifact_hash_version` 无条件放进正文，等于把 v1 的字节级公式改掉。用磁盘上
   真实归档工件对拍才发现（见上节"版本兼容实测"），已改为 v1 正文保持 R4.1 原样 7 键。
2. **冻结清单把 `artifact_hash_version` 静默丢掉**：`freeze._normalize_model_block`
   是白名单式规范化，未登记的键会被丢弃——R4.1 已经在 `model_training_code_commit`
   上踩过同一个坑。FP-10 的端到端用例（真实 CLI）把它抓出来，已登记该键。
