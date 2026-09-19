# Alpha V2 M3 — Production Readiness Report

> 生成时间：2026-09-18（本地时）
> 阶段：`CURRENT_PHASE = M3`，`PRODUCTION_DEPLOYMENT_AUTHORIZED = false`，`GIT_PUSH_AUTHORIZED = false`
> 冻结基线：`ALPHA_V2_M1_M2_FROZEN_COMMIT = 55c7592fe88ac3b3ab5b233df422b0f528141074`
> 核验方式：全部为**只读**（paramiko SSH ls/cat/duckdb read_only；无重启、无配置修改、无部署）

---

## 1. Accepted Baseline

```text
code_commit      = 55c7592fe88ac3b3ab5b233df422b0f528141074（M1 M2 合并冻结）
分支锚点         = feat/alpha-v2-m1-0917（未 push；M3 代码仍在其上）
M1 Acceptance    = PASS（Codex 第三轮，2026-09-18 转录）
M2 Acceptance    = PASS（工程实现层；研究门 60D AVAILABLE / 120D·250D AWAITING_DATA）

M3 新增（本批次 + F1-F7 修复轮）：
- src/stock_analyzer/alpha_v2/validation/{freeze, freeze_precheck, epoch, frozen_model, shadow_capture, outcome_maturation, validation_kpis, feature_diagnosis, runtime_identity}.py
- scripts/alpha_v2_{validation_freeze, shadow_model_freeze, shadow_capture, shadow_mature, validation_report, feature_diagnosis}.py
- tests/test_alpha_v2_m3_*.py（**10 个测试文件 / 112 例**：112 collected / 112 passed / 0 failed /
  0 skipped，junit 实测——F1-F7 修复轮时曾记 9 文件 / 76 例，
  再早的"7 文件 52 例"是笔误）
- 改动 M2 一点：research/benchmarks.py::_style_control_group 分块越界修复
  （见 §7 Blocking Findings R1）
- benchmarks 冻结口径 = 3 层（eligible_ew / quality_pool_ew / style_matched），
  simple_baseline 在 KPI 层做同日配对——修正"4 层冻结"的笔误
```

**冻结对象字段**（`validation_freeze_manifest.json`，schema = `alpha_v2_validation_freeze.v1`）：
`validation_epoch_id / code_commit / git_branch / config_hash / model.{id,hash,created_at} /
feature_schema_{id,hash} / label_policy_{id,hash} / selection_contract_id /
quality_target=300 / light_target=100 / deep_target=50 / final_cap=5 /
execution_price_mode / feature_price_mode / primary_business_horizon=5 /
confirmation_horizon=3 / horizons=[3,5,10,15] / benchmarks（**3 层冻结**：
eligible_ew / quality_pool_ew / style_matched，primary=quality_pool_ew；
simple_baseline 由 KPI 同日配对，不属于 frozen benchmark layer）/
sample_gates={20,60,120,250} / validation_start_date / created_at / freeze_manifest_hash`。

## 2. NAS Registry Reconciliation（§15.A）

只读对账（`duckdb connect(read_only=True)` 直读，未写任何生产状态）：

```text
registry 行数           = 21
lifecycle trained       = 4（含 serving）
lifecycle revoked       = 17（全部 artifact_uri 指向 dataset manifest 或旧根——历史污点未治理）
champion 行数            = 0
serving alias           = /app/artifacts/model_v1.json
serving sha256          = 71f64a21c131fe594d871bf4eaa87e75d8667fb722556de0f2f85a8607cb2559
serving created_at      = 2026-08-16T19:06:43
serving feature_schema  = feature_schema_v1_285626b6fdd6
serving label_policy    = label_policy_v1_e2afc1135a3f（soup_10d_tp8_before_sl5, 即 DF-S09-001 未登记口径）
serving dataset_manifest= dataset_manifest_v1_50ec7236be71
serving 对账             = serving hash 与 registry 中 model_v3_6d7486bc1af6（trained）artifact 一致
```

与 baseline 一致，无新发现。**V2 模型完全不经由本 registry** ——它走自己的冻结工件 + frozen hash（见 §9）。

## 3. Execution Price Verification（§15.B）

```text
NAS api 容器环境变量（只读 printenv）：
SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE = raw        ← 生产执行口径
SA__EVOLUTION__EXECUTION_SPEC__DIVIDEND_TREATMENT = explicit_cashflow
SA__DATA_SOURCE__VENDOR_ZIP_PRICE_SERIES_MODE = qfq            ← 特征口径（允许）

结论：execution=raw 已成立（不是 qfq 假成交）。blocker 关闭。
DF-S07-001「生产可能是 qfq」的悬念 = 已消除（实证为 raw）。
```

## 4. Data Health / Breadth Verification（§15.C）

```text
market_breadth.json（预期路径 artifacts/runtime/market_breadth.json）→ 不存在（两容器、两卷均已确认）
nightly_data_ready.json → 存在且最新=c2026-09-18T12:32Z（daily 5537 只 / delta coverage=1.0）
S08 的 data_health 七项检查    → 代码在本地，未部署，未见生产落盘
```

含义：生产当前没有"breadth 工件存在性"约束 live path；当扫的 Live Breadth 处于「breadth artifact 缺失即跳过」的通行态。对 M3 Shadow 而言：**V2 捕获端每天应把 breadth / data_health 写入快照标记（缺失须如实 not_available）**，但不影响 Shadow 写账本身（已被 M2 设计为"标注不启用"）。

## 5. Decision Log Scheduler（§15.D）

生产调度器（`runtime_state.json` → `scheduler_state.jobs`）在册 35 个 job，
其中 `week5_night_scan`、`nightly_delivery_tick` 在跑；**没有 alpha_v2 / decision-log / shadow 类 job**（预期内——相关代码尚未部署）。

结论：决策日志接线 = M3 部署前的**待做项**，非 bug。

## 6. Outcome Maturation Scheduler（§15.E）

同上：无 outcome 成熟 job。M1/M2 的函数侧能力已在仓（`build_shadow_decision_rows` / `mature_shadow_outcomes` / `mature_epoch_outcomes`），缺生产开机。部署后接入方式见 §9。

## 7. DF-M2-003（101 列近常数特征）数据诊断结论

本地重算（`scripts/alpha_v2_feature_diagnosis.py`，窗口 2025-06-02→2026-03-31，400 票，
与 M2 证据窗同口径；208 列全覆盖）：

```text
FILL_ZERO_ARTIFACT    = 99（众数=0、zero_ratio≈1.0、coverage=1.000）
                        ↑ 根因：FeatureEngineer 末端 fillna(0.0) 把上游缺数伪装成 0
REAL_CONSTANT         = 2（block_trade_frequency_20 / background_completion_score——真实存在但近恒等）
UNKNOWN               = 107（含全部正常技术特征——不参与治理）
```

上游层探针（单独执行，本地 market.duckdb 只读探针）：

| 上游列 | 在 `daily_bars` 中 | 非空率 | 结论 |
|---|---|---|---|
| `moneyflow_net_amount` | 在 | 270 / 9,881,442（≈0.0027%） | UPSTREAM_NOT_POPULATED |
| `hk_hold_ratio` / `hk_hold_change` | 在 | 4 / 2 行 | UPSTREAM_NOT_POPULATED |
| `inst_net_amount` | 在 | 0 / 9,881,442 | UPSTREAM_NOT_POPULATED |
| `block_trade_amount` | 在 | 8 行 | UPSTREAM_NOT_POPULATED |
| `northbound_net` | 在 | 97.7% 恰为 0.0（9,657,363 行） | FILL 到 0（有效果但不真实） |
| `margin_financing_balance` | 在 | 100% 非空但 16.9% 同值 2.5e9 | REAL_CONSTANT 上游固化 |
| `roe` / `debt_ratio` | 在 | 近乎全集非空；roe=0.08 占 95.2% | REAL_CONSTANT（资产侧上游常数） |

**治理含义**：这 99 列不是"特征工程差"，是"上游未填充 + 末端 fillna(0.0) 掩盖"。
**任何"删列/换填法"动作都属于改冻结特征集 —— 必须走关门-开新 epoch**。
M3 本轮只诊断、不修。

## 8. M3 排练（本地端到端验证闭环）

为证明整条链路可用，本机以窗口 `2025-06-02 → 2026-02-13`（与 vista 校准分离）完成了：

1. **冻结模型**：`alpha_v2_shadow_model_freeze.py` → `artifacts/alpha_v2/validation_rehearsal/model/alpha_v2_shadow_epoch_001/`
   （训练窗 = 校准窗外所有历史；校准窗 = 尾部 60 个交易日；与 §13 对齐）
2. **冻结清单**：`alpha_v2_validation_freeze.py --epoch-id alpha_v2_epoch_001 --open-epoch`
   → `validation/validation_freeze_manifest.json` + `validation/epochs.json`（hash 锚定）
3. **T 日预测**：`alpha_v2_shadow_capture.py --signal-date 2026-02-13` → `validation/alpha_v2_epoch_001/shadow/2026/02/shadow_20260213.jsonl`
4. **成熟**：`alpha_v2_shadow_mature.py --evaluation-date 2026-03-31` → `validation/.../outcomes/2026/02/outcome_20260213.jsonl`（3/5/10/15D 全成熟）
5. **KPI 日报**：`alpha_v2_validation_report.py` → `validation/.../reports/validation_kpi_alpha_v2_epoch_001_20260331.{json,md}`

排演产物不是 clean OOS（它们用的是本机 PIT 面板与历史窗口）；它们证明的是工程
一次性串通：
- 冻结模型：`validation_rehearsal/model/alpha_v2_shadow_epoch_001/`（17/17 目标全部训练成功，
  direction 4/4 有 OOS isotonic 校准器，artifact_hash=5978ef5d…9a3）
- 冻结清单：`validation_rehearsal/validation_freeze_manifest.json`
  （freeze_manifest_hash=76c13e36…；执行口径核验在本机正确亮起 BLOCKED_FOR_CLEAN_OOS
  ——因为本机受跟踪配置是 qfq；生产环境 env 层已证 raw）
- T 日快照 2026-02-27：50 行（Deep50 cohort），字段含 alpha_rank / p_up_*_calibrated /
  expected_*_3d/5d / risk（p_mae_le_5pct_5d）/ legacy_* 占位 not_available
- 成熟到 2026-03-31：50 行 × 4 horizons 全部成熟（3/5/10/15D）
- KPI 日报：validation_kpi_alpha_v2_epoch_001_20260919.{json,md}——所有块产出，
  alpha_verified=False / production_promotion=LOCKED，样本门 20/60/120/250 全 False（1 个成熟日）

## 9. 生产部署前置（cold path，未获授权，仅成文）

若得到 `PRODUCTION_DEPLOYMENT_AUTHORIZED=true`，以下顺序是唯一的部署方式：

```text
0)  部署代码：git merge 到 main（55c7592 + M3 本批）→ NAS 走 nas_deploy_update.sh
1)  NAS: python scripts/alpha_v2_validation_freeze.py --epoch-id alpha_v2_epoch_001 --open-epoch
    （产物落 artifacts/alpha_v2/validation/；不触碰 Legacy）
2)  NAS: python scripts/alpha_v2_shadow_model_freeze.py（生产真实面板冻结模型；可过夜跑）
3)  调度接两件套（在 week5_night_scan 之后、在 21:45 数据就绪后）：
    - alpha_v2_shadow_capture     （每日 T 收盘后）
    - alpha_v2_shadow_mature      （每日 T 收盘后；实际回填 3/5/10/15D）
    以及周报层：alpha_v2_validation_report（手动/周度）
4)  第一个 clean OOS 决策日后，M3 §14 的 20/60/120/250 样本门通过 KPI 报告自己演进
    ——在那之前整个体系 "alpha_verified=False / production_promotion=LOCKED"
```

**指标冻结期（部署 ~≥ 60 个成熟决策日期间）严禁事**：
```text
final_signal_min_threshold(仍有 70) / cross_review 四阈值 / LGBM/XGB/meta 权重 /
Base V2 特征集 / 标签口径 / selection contract 300/100/50 / "换个 benchmark"
—— 任一变化都必须：close epoch → 修 → 重验 → 开下一个 epoch（alpha_v2_epoch_002）
```

## 10. Rollback Plan

单点回滚（与 M1/M2 同一纪律）：

```text
生产副作用隔离  : alpha_v2.enabled=false → shadow 立即停；不改 Legacy 任何输出
代码回滚        : git revert 55c7592..HEAD（M3）; M1/M2 各自 revert 命令见 M1/M2 报告 §9
工件清理        : artifacts/alpha_v2/validation/** 是纯增量、可整目录删除；epochs.json 保留作审计
NAS/eup 残留    : 本期未触碰 NAS/.env/容器；不存在"部署撤回"路径
模型工件        : validation/model/<id>/ 删除即可（每个 epoch 的工件互相独立）
```

## 11. Blocking Findings

```text
R1（已修，见 §12）  benchmarks.py::_style_control_group 在组 size>512 时
                    dist[row_index, row_index] 越界；M3 在全市场（5166 只）排练时实锤。
                    已修复并加 4 条对抗回归（含 block 不变性 + 暴力法对照）。
R2（未动生产）    breadth artifact 缺失 = live 广度门静默失效（Legacy 侧既有问题）；
                    M3 Shadow 会如实标 data_health=not_available，不因此而阻塞。
R3（计划内）      无 Alpha V2 调度 job（决策日志/outcome 成熟都需部署后接）。
```

## 12. Non-Blocking Findings

```text
N1  M2 的对照在组 ≤512 时两种索引等价，所以 §R1 的修复不改变 M2 既有结论（100% 兼容）。
N2  S05 的 reconcile CLI 在本批未直接调用（NAS 侧读法已嵌入报告 §2 的事实，
    市场槽位模型身份=已匹配）。
N3  校准上限：isotonic 校准行数下限=50（M3 模块默认）；当 OOS 样本极早（<50 行校准窗）
    时 p_up 按规则标 not_available，不会退化成伪概率。
N4  冻结模型训练样本量：quality_pool（流动性代理池 300 只/日 × 训练天数）远大于
    HeadFitSpec.min_train_rows=200 —— 无"训练不足而静默素颜"风险。
```

## 13. Production Deployment Readiness

```text
Production Readiness = PASS_ENGINEERING（M3 Round 3 外部独立复核 PASS，2026-09-19）
                       部署执行 = LOCKED_PENDING_BLK_D1_D2（见 §17.3）

成立条件（全部满足后才可部署 = READY）：
  1. R1 修复已并入（本批合入 main 后部署即可，不必单作）
  2. 部署顺序遵循 §9（先冻结清单 + epoch，再冻结模型，再接每日两件套）——
     修复轮的硬门还要求：先训出冻结模型（--model-dir）或备好 --feature-columns-file，
     并且 .build_commit/build_manifest 必须与"将要部署的 commit"一致
  3. Shadow 在填写前至少跑过一次 rehearsal（修复轮的 F1-F7 演练见 §16）
  4. `alpha_v2.enforce_final_selection` 在整个 Shadow 阶段严格 false
  5. 若需 breadth/data_health 进 KPI 面板，须先把 S08 生产侧接通（不阻塞 Shadow 本身）

**当前状态 = 工程 PASS / 部署 LOCKED_PENDING_BLK_D1_D2**

本报告不把"Shadow 尚未生产部署"写成 Blocking——因为 M3 的输出正是条件就绪 +
部署计划 + 回滚方案。但容器执行路径的两项实测缺口（BLK-D1 / BLK-D2，见 §17.3）
必须先由后续的 Production Runtime Identity Hardening 处置，方可执行 §17.2。
```

## 16. M3 Blocking Fix Round 处置总表（2026-09-19，F1-F7 落地）

| ID | 原问题 | 修复位置 | 验证 |
|---|---|---|---|
| B1 | T 日快照可事后创建 | shadow_capture.py 写窗口闸门 + late-write/backfill 标记 | 测试 4 例 + rehearsal B3/B3'/B4/B4' 全过 |
| B2 | epoch 身份漂移不可检 | epoch.py 严格身份 + require_epoch_identity_match；写/关/成熟三路径全接入 | enforcement 26 例全过；rehearsal B1 证据 |
| B3 | closed epoch 仍可 mature | outcome_maturation.py 入口 require_epoch_identity_match；影子外的 symbol 拒写 | 测试 + rehearsal B2 |
| B4 | freeze feature schema 为空 | freeze.py 同一哈希算法 + CLI 双源校验 + 生产非空硬门 | 测试；rehearsal A/F |
| B5 | 非 raw 仅警告 | --rehearsal 显式分离 + 生产模式 exit 4 | 测试 + rehearsal C |
| B6 | data_health=not_available 计入 Clean OOS | KPI 治理层（governance block）+ clean_joined；样本门只算 clean 日 | 测试 + rehearsal B4c |
| B7 | 引用了不存在的回归证据 | scripts/alpha_v2_m3_regression_record.py 真生成 + junitxml 落盘 | 本报告 §16 后附 |
| B8 | 脏树 / 代码身份可混 | runtime_identity.worktree（None 即拒）+ build identity 对账 | 测试 + rehearsal A/B/D/E |

修复轮顺带自发现自修复：`runtime_identity._git` 把 `"" or "unknown"` 当失败 → 干净树被误判为脏，
已在同一修复窗内纠正并补防回归测试（`test_git_worktree_dirt_empty_output_means_clean_not_dirty`）。

修复轮之外的、已知 Non-Blocking（N1-N11）全部照旧纳入治理清单，不混到 Blocking。

---

## 17. M3 R3 Final Blocking Fix（2026-09-19）

### 17.0 验收历史（保留）

```text
M3 Acceptance Round 1 = FAIL（B1–B8）
M3 Recheck Round 2    = FAIL（BLK-R2-1 伪造 capture_date；BLK-R2-2 构建身份双源未同时校验）
M3 Final Blocking Fix R3 = 完成（BLK-R2-1 / BLK-R2-2 / N-R2-1）
Codex Recheck（Round 3）= PASS（2026-09-19，外部独立复核）
```

### 17.1 修复内容（对应 §16 的三项）

| ID | 修复 | 位置 |
|---|---|---|
| BLK-R2-1 | 写入日 = 真实墙钟；API 删除 ``capture_date``；**production 永久不可注入时钟**（``--capture-date`` exit 8；确定性时钟只属 rehearsal/test）；行落 ``actual_capture_date``；KPI 增 ``late_recorded_at`` 兜底闸 | ``validation/shadow_capture.py``、``scripts/alpha_v2_shadow_capture.py``、``validation/validation_kpis.py`` |
| BLK-R2-2 | 构建身份四值一致硬门（git HEAD == requested == .build_commit == build_manifest.commit）+ present/trusted/dirty 全查 | ``validation/freeze_precheck.py``、``validation/runtime_identity.py``、``scripts/alpha_v2_validation_freeze.py`` |
| N-R2-1 | capture 读当天 data_health 工件（S08 契约），缺失/陈旧/降级 → 不进 clean 但不阻塞记录 | 新增 ``validation/data_health_capture.py``、``scripts/alpha_v2_data_health_snapshot.py`` |

### 17.2 部署前置（比 §9 更严，必须按序）

```text
0) 代码合并到 main 并部署；此后 .build_commit 与 build_manifest.json **两个文件都必须存在**，
   且 build_manifest.json 的 commit == .build_commit == git HEAD == 冻结清单 code_commit，
   同时 build_manifest.json 必须 trusted=true、dirty=false（否则 production freeze exit 5）
   —— 只写一个文件不再够用（这是 BLK-R2-2 的修复要求）
1) NAS: python scripts/alpha_v2_validation_freeze.py --epoch-id alpha_v2_epoch_001 \
        --model-dir <冻结模型目录> --start-date <今天或以后> --open-epoch
   （execution=raw 由 env 覆盖；工作区必须干净——未跟踪白名单只有这两个身份文件）
2) NAS: python scripts/alpha_v2_shadow_model_freeze.py（生产真实面板冻结模型；可过夜跑）
3) 每日顺序（21:45 数据就绪后）：
   a. python scripts/alpha_v2_data_health_snapshot.py --out /app/artifacts/runtime/data_health.json
   b. python scripts/alpha_v2_shadow_capture.py --epoch-id alpha_v2_epoch_001 \
        --signal-date <T> --quality-pool-source production_selection_engine
   c. python scripts/alpha_v2_shadow_mature.py --epoch-id alpha_v2_epoch_001 --evaluation-date <T>
   —— 3a 缺失时 capture 仍会写快照，但当天 data_health=not_available → 该日不进 clean OOS
      （样本门不推进）；capture 的写入日必须是系统真实日期，否则直接拒绝
4) 第一个 clean OOS 决策日之后，20/60/120/250 样本门由 KPI 报告自然演进
```

**回滚**：``alpha_v2.enabled=false`` 即可停；R3 改动全是 V2 命名空间内的新增/收紧，
不触碰 Legacy 任一阈值与 serving 工件。

### 17.3 封存状态与部署域 Blocking（2026-09-19 外部独立复核后）

```text
M3 Engineering Acceptance           = PASS（Round 3 外部独立复核）
M3 Accepted Baseline Commit         = 本批本地封存 commit（SHA 见该 commit）
Production Shadow Deployment        = NOT_STARTED
Production Deployment Authorization = NOT_EXECUTED

BLK-D1 = OPEN —— 生产容器内 production freeze 不可执行
  只读实测：api / scheduler-critical / scheduler-heavy 三个容器均无 git 二进制、
  无 /app/.build_commit，仅有 /app/build_manifest.json（commit=7e9e33b…）；
  容器形态复刻跑真实 freeze CLI → exit 5（先撞"无法证明工作区干净"）。
  后果：§17.2 步骤 1 当前不可执行；§17.2 中"容器内 git 不可得时用 --code-commit +
  两源互证"这条声明路径在 CLI 层不可达（安全性成立，但实际是死代码）。

BLK-D2 = OPEN —— 容器内 capture / mature 的运行身份对账必然失败
  二者只取 git_head(REPO_ROOT)，容器内恒为 "unknown"，与 epoch 冻结的真 SHA 不符；
  容器形态跑真实 capture CLI → exit 3。
  后果：§17.2 步骤 3b/3c 当前不可执行（拒绝执行，不会污染 clean OOS）。

处置归属：后续独立的 Production Runtime Identity Hardening，**不在 M3 封存范围内**（本批不修）。
生产身份口径：PRODUCTION_SHADOW_FROZEN_COMMIT = PENDING_AFTER_BLK_D1_D2 ——
alpha_v2_epoch_001 的 freeze manifest code_commit 必须指向硬化后的最终部署 commit，
不得用 M3 Accepted Baseline Commit 冒充生产冻结身份。
```

---

*编制者：ZCode（M3 批次）。全部证据路径：artifacts/alpha_v2/audit/nas_readonly_verification_20260918.json
（NAS 只读核验）、artifacts/alpha_v2/validation/feature_diagnosis/（DF-M2-003）、
validation_rehearsal/（本地排演）。
*
