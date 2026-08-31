# Week5 Phase 0 只读审计报告（2026-08-31）

> 审计对象：`main@aca70a4` 之上的工作分支 `feat/week5-remediation-0831`（Phase -1 定稿 commit `43e0f80`）。
> 审计方式：本地仓库只读代码/历史审计 + §0 审计快照（8/31 NAS 实测）引用。
> 结论：**Phase 0 硬门未通过**，存在 10 项阻塞项（B1–B10）；其中 6 项可本地代码修复（本报告同分支后续 commit），4 项需 NAS 运行窗口。未通过前禁止任何正式重训与 walk-forward。

## 1. 分支与代码状态审计（方案 §3.1）

### 1.1 旧分支逐项对照（`feat/learning-protocol-remediation-0823`）

实测命令与结果：

- `git rev-list --left-right --count main...feat/learning-protocol-remediation-0823` = `54 1`
- `git log main..feat/learning-protocol-remediation-0823 --oneline` = 仅 `6fda5ba`
- `git merge-base --is-ancestor 6fda5ba main` = **NO**（非 main 祖先）
- `git cherry main feat/learning-protocol-remediation-0823` = `- 6fda5ba`（patch-equivalent）
- 语义级核验：6fda5ba 的两处改动（`training_enabled` 门控、`load_predictor=False`）已在 main `src/stock_analyzer/runtime/services/idle_queue_weekend_service.py:347/402/491`

旧分支 8/23–8/24 的 12 个 remediation commit 逐项状态：

| commit | 内容 | 状态 |
|---|---|---|
| 5964c5a | P0-b schema-aware label dispatch + soft/hard 指标分离 | 已在 main（祖先） |
| 8169fa3 | P0-a content-addressed bundles + 两阶段 CAS 发布 | 已在 main（祖先） |
| 78ec972 | P1-a manifest v2 去重 + trainer 纵深防御 | 已在 main（祖先） |
| 7a8b715 | P1-b slot NAV 模拟器 + 晋级有效性门 | 已在 main（祖先） |
| acf5b75 | duckdb 惰性创建 + v13 artifact bundle fallback | 已在 main（祖先） |
| 9f3c81a | Commit A+B（e44 NAV 模拟器/晋级硬门/baseline_bootstrap/报告去重等） | 已在 main（祖先，`merge-base --is-ancestor`=YES） |
| 8a0a354 | 晋级硬门阈值可配置化（测试夹具） | 已在 main（祖先） |
| ebd49ea | manifest 门收口 + artifact 覆盖路径 | 已在 main（祖先） |
| f4b0030 | acceptance service 返回类型 | 已在 main（祖先） |
| c0768cf | Docker bootstrap sidecars | 已在 main（祖先） |
| 25f1bff | legacy manifest 质量重估 | 已在 main（祖先） |
| 6fda5ba | weekend learning 门控 + predictor reload 延迟 | **仅 patch-equivalent**（内容已在 main，commit 非祖先） |

处置结论：**分支不删除、不归档、不整体合并**；无"仅旧分支存在"的有效修复需要移植。

## 2. 标签语义审计（方案 §3.2）

### 2.1 已达标项

| 项 | 证据 |
|---|---|
| 版本化 label_policy 注册表（immutable id+hash、按 hash 幂等、id 冲突拒绝） | `src/stock_analyzer/learning/label_policy_registry.py:53-106,123-175` |
| 快照级 label_policy_id/hash 绑定（快照必须携带非空 policy 身份） | `src/stock_analyzer/learning/sample_schema.py:104-105,116-120` |
| 四时间戳不变量（event/available/decision/maturity 公式） | `src/stock_analyzer/time_semantics.py:10-98` |
| manifest 级 embargo = horizon_days + settlement_lag | `src/stock_analyzer/learning/backfill.py:620-624` |
| v2 训练标签 TP/SL 冲突 → 显式 soft label（默认值 0.5） | `src/stock_analyzer/models/trainer.py:1076-1094`（`_conflict_label_for_policy`）；v1 历史口径逐位保留（1097-1114） |
| soft/hard 指标分离（soft_label_count 等） | `src/stock_analyzer/models/trainer.py:906,941` |
| soup 路径标签支持 conflict_policy / conflict_soft_label_value=0.5 | `src/stock_analyzer/labels/soup.py:17-18,169-215` |

### 2.2 阻塞项

- **B1 入场价语义**：计划要求"入场价为下一个交易日开盘价"。现状 `LabelsConfig.pnl_price_basis` 代码默认 `"close"`（`config.py:987`，T 日收盘入场）、`default.yaml:694` 覆盖为 `next_tradable_vwap`（T+1 VWAP/收盘 fallback）——**两者均非 T+1 开盘**；且 `_resolve_entry_price`/`_resolve_entry_position`（`labels/soup.py:137-166`、`learning/backfill.py:1790-1815`）不存在 `next_tradable_open` basis。
- **B2 conflict_policy 记录值非显式 soft_label**：`config.py:988` 与 `default.yaml:695` 均为 `bar_shape_heuristic`（v2 分发下映射 soft 0.5，但 registry 记录的 policy 字符串非显式 `soft_label`，与"统一使用显式 soft label"不一致）。
- **B3 conflict=true 事件标记未持久化**：`OutcomeRecord`（`sample_schema.py:158-173`）无 conflict 布尔字段；outcome 表 DDL（`sample_store.py:563-580`）无对应列。冲突仅训练时由 MFE/MAE 重推导并以 `soft_label_count` 汇总，不能按行追溯。
- **B4 受影响 outcome backfill 与新旧标签差异报告未执行**（机制在位：`last_backfill_at`/`backfill_source`/`backfill_fidelity_tier`；执行属 NAS 数据窗口）。
- `label_anchor_time` 未作为独立字段持久化（现以快照 `decision_time` 承担锚点语义）；`source_data_cutoff` 无字段。→ 并入 B3 的 schema 扩展评估。

## 3. Model Registry 与 Artifact 审计（方案 §3.3）

### 3.1 已达标项

| 项 | 证据 |
|---|---|
| content-addressed bundle（不可变目录 + 原子发布 + 冲突 fail-closed + 幂等重放） | `src/stock_analyzer/models/bundle.py:115-259` |
| bundle_id = f(content_hash)，fail-closed 完整性校验（loadable + sidecar sha256 + 可选内容哈希） | `bundle.py:47-112` |
| alias 自描述身份（registry_model_id + bundle_content_hash 写入 metadata） | `bundle.py:262-302` |
| registry record 字段齐备（model_id/role/lifecycle/manifest/schema/policy/hash/metrics） | `registry.py:34-70,118-159` |
| model_id 重复注册防护（同 id 不同记录拒绝；全等幂等重放） | `registry.py:199-210` |
| CAS 晋级（expected-champion 前置校验 + 事务回滚） | `registry.py:426-493` |
| 受控基线 champion 引导（bundle 完整性 + v2 manifest + 日期/类别/AUC 绝对门 → CAS） | `service.py:811-830`（`bootstrap_baseline_champion`） |
| 旧 alias 兼容入口有 registration + `active_champion_exists` 保护 | `service.py:1243-1268` |
| 主训练流程 model_id = bundle_id（内容寻址、确定性） | `service.py:1054`（`model_id=publication.bundle_id`） |
| verify_artifact_integrity 用于 release/report/governance 路径 | `service.py`、`learning_governance_service.py`、`champion_shadow_report.py` |

### 3.2 阻塞项

- **B5 `_build_model_id` 依赖文件路径**：`registry.py:878-890` 以 `{artifact_uri, artifact_created_at, manifest, schema, policy}` 的 sha256 前 12 位生成——身份绑定文件路径与时间、未绑定 artifact 内容 hash；同一内容换路径/换时间即换身份，文件被覆盖（同路径不同内容）则同 id 不同内容。仅 `register_artifact(model_id=None)` 的 fallback 路径命中（主训练流程已用 bundle_id）。修复方向：内容绑定派生 id（v3），并记录"确定性内容 id vs 随机 UUID/ULID"的取舍决策（随机 id 会破坏 register 幂等重放与 9285 行兜底补注册的去重语义）。
- **B6 APPROVED 不强制 artifact_content_hash**：`_APPROVAL_REQUIRED_FIELDS`（`registry.py:727-733`）不含 hash 字段 → APPROVED/CHAMPION 可空 hash（NAS 全空 hash 的代码根因）。champion bootstrap（`service.py:1200-1204`）注册 APPROVED 时也未传 hash。
- **B7 `auto_load_predictor` 直载路径无 registry/hash 校验**：`pipeline.reload_predictor`（`pipeline.py:835-838`）直接从 artifact_path 加载；`default.yaml:760` `auto_load_predictor: true`。服务层 3 处直调（`service.py:9204/9253/11675`）。计划要求"加载前必须完成 registry、hash、schema 校验"。
- **B8 历史脏数据隔离未做**（空 model_id/空 hash/`artifact_overwritten`/旧 alias 指向记录标记隔离）——属 NAS registry 数据窗口操作，代码侧随 B6 fail-closed 后新记录不再产生空 hash。

## 4. Universe 对齐审计（方案 §3.4）

- 训练样本 universe = snapshot 库历史候选池：`build_trainable_manifest` 按 `symbols` 过滤（`learning/backfill.py:467,508-518`）；§0 实测单 manifest 覆盖 152–496 只。
- 评估 universe = 全市场 PIT 质量筛选（`backtest/asof_scan.py` 全市场粗筛，5479→5203→100）。
- **Universe 错配实锤**（结构层面：训练数据来源为历史候选快照，非 PIT 质量筛选全市场）。
- **B9 universe 统计字段缺失**：`training_universe_size`/`evaluation_universe_size`/`symbol_overlap_ratio`/`trade_date_coverage_ratio`/`universe_snapshot_hash`/`selection_ruleset_id` 在代码中不存在（rg 全仓无命中）——随 Phase 2 harness 新建落地。
- **B10 "扩样本 or 缩评估"书面决策未落盘** → 见本报告 §7 决策。

## 5. 数据覆盖审计（方案 §3.5）

- 本地仓库无生产 `market.duckdb`（仅 pytest fixtures；`artifacts/warehouse/` 不存在）——实库覆盖核验属 NAS 窗口。
- 引用 §0 审计快照（8/31 NAS 实测）：日线 3–8 月完整（7/30 半日 2,354 行；7/31 由同步脚本补齐）；分钟线 7/20–7/30 仅 36 票/日、7/31–8/3 为零、8/4 起正常；delta 日线 max=8/28（8/29–30 周末，非缺口）；updater 无缺口。
- **B11**：7/30 半日 degraded 规则与分钟空洞 degraded 规则已在方案定稿（Phase -1），尚未落地为评估链路的可执行标记（Phase 1/2 harness 实现时落地 `degraded_reasons` 字段）。
- **B12**：交易日历 / symbol-date coverage / feature coverage / label maturity coverage 报告未生成（NAS 窗口）。
- **B13**：updater 8/28 后 no-op 状态与 readiness 记录核验（NAS 窗口）。

## 6. 资源约束现状（方案 §10）

- 重训/回测资源上限（4GiB / 3.2GiB soft / 3.6GiB hard stop、单重型任务、避开 21:30–23:00 cron）为执行约束，不涉及代码改动；单 fold benchmark 未做（Phase 2 前置，NAS/本地均可）。

## 7. Phase 0 决策产物

### 7.1 Universe 决策（B10，书面决策）

**决策：扩训练样本 Universe（方案 §3.4 默认方向），评估口径保持全市场 PIT 质量筛选。**

理由：
1. 评估侧（night_scan/asof 回测）的消费分布是全市场质量筛选后的候选流，训练分布必须对齐该分布，否则模型学习的是"历史候选池"的条件分布（152–496 只），与评分时的横截面分布错配——这是 8 月回测分数-收益负相关的候选根因之一。
2. snapshot 库已有 90,103 条 / 1,557 只历史样本，扩样本有数据基础；缺口在于"如何产生覆盖全市场的训练快照"。
3. 缩评估（watchlist scope）会让 Phase 2 结论失去生产外推效力，且不解决根因。

实施路径（Phase 2 harness 承接）：
- 训练快照生成改用 PIT 质量筛选 Universe 的逐日截面快照（而非候选池回填）；
- 每个训练窗口记录 `training_universe_size / evaluation_universe_size / symbol_date_count / symbol_overlap_ratio / trade_date_coverage_ratio / universe_snapshot_hash / selection_ruleset_id`；
- 在扩展 Universe 数据就绪前，任何训练结论标注 `watchlist_scope`，不得解释为全市场结论。

### 7.2 model_id 生成策略决策（B5 修复口径）

**决策：model_id 改为内容绑定派生 id（`model_v3_<sha256[:12]>`，payload = {artifact 内容 hash、artifact_created_at、dataset_manifest_id、feature_schema_id/hash、label_policy_id/hash}，剔除 artifact_uri 路径项）。**

理由：随机 UUID/ULID 会破坏 `register()` 幂等重放（同 artifact 重注册产生重复记录）与兜底补注册（`service.py:9285`）的去重语义；内容绑定派生 id 同时满足"独立、不可变、不循环依赖（model_id 仅依赖叶子值 content hash，与 bundle_id 为同源兄弟依赖而非循环）"。唯一性由 sha256 全长保证，12 位截断与现有惯例一致且可后续扩展。

## 8. Phase 0 硬门对照（方案 §3.6）——2026-08-31 再审计（第二轮，修复落地后）

| 硬门 | 状态 | 依据 / 对应阻塞项 |
|---|---|---|
| 标签时间不变量违规 = 0 | ✅ 机制达标（本地）/ ⏳ 实库核验待 NAS | time_semantics 四不变量 + manifest embargo 在位；实库扫描属 B12 |
| 受影响标签已重建或明确排除 | 🔶 代码侧就绪，实跑待 NAS | B1–B3 已修；conflict_flag/anchor/cutoff 回填与差异报告由 `scripts/week5_phase0_backfill_runner.py` 在 NAS 窗口执行 |
| active champion/registry/artifact/hash 完全一致 | 🔶 代码强制已生效，实态待 NAS | B5/B6/B7 已修（model_id v3、champion hash 强制、identity hash 统一、三层核验待做） |
| 新评估产物 model_id 非空 | ⏳ Phase 1 字段标准化时落地 | — |
| 未处理 artifact_overwritten = 0 | ⏳ 待 NAS 核验 | B8 |
| Universe 口径已确定 | ✅ **已决策**（§7.1 扩训练样本） | B10 关闭 |
| 数据缺口已修复/排除/标记 | 🔶 规则已定稿，落地待 harness | B11（degraded_reasons 随 Phase 1/2） |
| NAS runtime 读取并实际使用有效 champion | ⏳ 待 NAS 核验 | B13 |
| 单 fold benchmark 已完成 | ⏳ 未开始（Phase 2 前置） | — |

**再审计结论（本地部分）：B1/B2/B3/B5/B6/B7/B10 七项阻塞的代码侧修复全部落地并通过测试；Phase 0 硬门整体仍未通过**——通过与否取决于 NAS 窗口的实库核验（B4 实跑、B8 隔离、B12/B13、单 fold benchmark）。重训与 walk-forward 禁令维持。

## 9. 修复执行窗口划分

- **本地代码窗口（本分支后续 commit）**：B1/B2/B3（标签语义 + conflict_flag 持久化 + schema 迁移）、B5/B6（model_id v3 + APPROVAL 强制 hash + bootstrap 传 hash）、B7（reload 前置校验）、配套测试。
- **NAS 运行窗口（独立排期，不保证与本审计同周末）**：B4（受影响 outcome backfill + 新旧标签差异报告实跑）、B8（registry 脏数据隔离标记）、B12/B13（数据覆盖报告、updater 状态、champion 三层核验）、单 fold benchmark。

## 10. 本地代码窗口修复落地状态（2026-08-31 同日更新）

| 阻塞项 | 状态 | 落地内容 |
|---|---|---|
| B1 入场价语义 | ✅ 已修复（代码） | 新增 `next_tradable_open` basis（`labels/soup.py` + `learning/backfill.py` `_resolve_entry_position`：T+1 开盘入场、停牌顺延、入场日算第 1 天）；`LabelsConfig` 与 `config/default.yaml` 默认值同步切换 |
| B2 conflict_policy 非显式 | ✅ 已修复（代码） | `LabelsConfig.conflict_policy` 默认 `soft_label`（yaml 同步）；v1/v2 历史契约经 registry 版本化共存，旧快照不受影响 |
| B3 conflict 标记未持久化 | ✅ 已修复（代码） | `OutcomeRecord.conflict_flag`（None=未计算）；outcome 表 DDL + 幂等迁移（`_migrate_outcome_records_columns`）；backfill 以 MFE/MAE 判定写入；`_OUTCOME_COLUMNS`/序列化同步 |
| B5 model_id 路径依赖 | ✅ 已修复（代码） | `_build_model_id` v3：payload 剔除 `artifact_uri`，改为 {内容 hash（按形态自适应），created_at, manifest, schema, policy}；策略决策见 §7.2 |
| B6 champion 空 hash | ✅ 已修复（代码） | `_validate_lifecycle_requirements` 新增 champion 内容身份强制（`_CHAMPION_REQUIRED_FIELDS`）；hash 物化下沉 `register()` 入口 + `update_role`/CAS 晋升路径；legacy 修复路径与服务注册 fallback 均物化 hash |
| B7 auto_load 直载无校验 | ✅ 已修复（代码） | 新增 `service._validated_predictor_reload`（完整性 fail-closed + registry 内容 hash 匹配 + pre-champion 兼容窗口审计），替换 3 处直调 |
| B4 差异报告（代码侧） | ✅ 机制就绪 | `trainer.build_label_policy_diff_report`（纯函数：变更行数/正负比例/冲突比例/成熟时间分布/hash 对）；实跑属 NAS 窗口 |
| B4/B8/B12/B13 | ⏳ NAS 窗口 | 见 §9 |

配套验证：新增 `tests/test_week5_phase0_remediation.py`（9 项）；`test_model_bundle_release.py` 夹具升级为真实文件 uri（champion 契约）。回归：registry/bundle/labels/backfill/store/governance/evolution 报告共 111 项通过；全量套件另行回归；ruff check 全过；mypy 与 main 基线对比无新增错误（41=41）。

**hash 口径统一决策（实现备注）**：registry 内容 hash 按"工件形态自适应"——裸文件（旧 alias）= 单文件 sha256；bundle 工件（`model_v2_*/`）= 目录内容哈希。`verify_artifact_integrity` 与注册物化使用同一 `compute_artifact_identity_hash`，消除"注册算文件、校验算目录"的漂移。

### 10.1 第二轮增补（符合性检查响应，2026-08-31）

| 项 | 状态 | 内容 |
|---|---|---|
| B2 补充：label_anchor_time / source_data_cutoff 持久化 | ✅ | `OutcomeRecord` 新增两字段（None=旧数据未回填）+ outcome 表幂等迁移；backfill 写入（anchor=快照 decision_time、cutoff=回填 as_of）；消除"以 decision_time 隐式承担锚点"偏差——Phase 1 maturity 推算 fallback 可直接消费 |
| B7 补充：schema 第三环 | ✅ | `_feature_schema_ring_pass` 共享 helper：registry 记录缺 schema → 拒；artifact 声明 schema 且与记录不一致 → 拒；artifact 无绑定（legacy）仅当记录为 `legacy_production_*` 合成身份时兼容放行。接入 `_validated_predictor_reload` 与 alias 门 |
| 残余风险：release 回滚恢复路径直载 | ✅ | `learning_governance_service` 回滚重载改走 `_reload_alias_predictor_validated`（恢复旧 alias 亦过门），`predictor_restored` 以校验结果为准 |
| B4 前置：backfill runner | ✅ | 新增 `scripts/week5_phase0_backfill_runner.py`（repair_backfill 重建 + 新旧标签差异报告 + 输入快照三件套落盘，时间戳命名不覆盖），NAS 窗口直接执行 |
| alias 门第三环测试 | ✅ | `test_reload_gate_schema_ring_blocks_mismatched_binding`（篡改 artifact schema hash → 拒） |

## 11. 偏差签认（对 plan 文本的正式偏离记录）

| plan 条文 | 实现 | 决策 | 状态 |
|---|---|---|---|
| §3.3 "model_id 使用独立、不可变的 UUID/ULID" | model_id v3 内容绑定派生（`model_v3_<sha256[:12]>`，payload=内容 hash+created_at+manifest+schema+policy） | 随机 UUID/ULID 会破坏 `register()` 幂等重放与兜底补注册去重语义；内容派生 id 同样满足"独立、不可变、三者不循环依赖"（详见 §7.2）。**符合性检查（2026-08-31）评审意见为可接受** | 签认通过（如需改回随机 id 须重开设计评审） |
| §3.2 "固定记录 label_anchor_time / source_data_cutoff" | 第一轮仅隐式（decision_time 承担） | 第二轮已补齐显式持久化（见 §10.1） | 偏差已消除 |
| §3.3 "alias 加载前完成 registry、hash、schema 校验" | 第一轮缺 schema 环 | 第二轮已补齐（见 §10.1） | 偏差已消除 |
