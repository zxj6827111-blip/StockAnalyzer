# ADR-001 Runtime Identity 与模型工件完整性

Status: Accepted

As-of: 2026-09-23 @ HEAD `42caaca`（详见 §9）

## 1. Status

Accepted。本文所写的每一条决策都有 HEAD 代码、测试或 commit 支撑（见 §7、§8）。

唯一不属于本 ADR 的边界：本机制**没有**密码学签名，只有 sha256 自锚哈希 + 构建期两源互证。
这条边界写在 §4 最后一条，不写成缺陷。

## 2. Context

Alpha V2 的产物链 `train → model freeze → validation freeze → open epoch → shadow capture →
mature → KPI / preflight` 跨两种差异极大的运行环境：

```text
git_checkout              开发机 / NAS 宿主仓库：git HEAD 可自证
container_build_identity  NAS 上的不可变镜像：无 git 二进制、无 .git 目录
```

如果不把"当前运行到底是谁"做成机制，会出现三类真实事故：

1. **换工件**：磁盘上的模型被替换或 manifest 被重写，而证据链仍挂着旧身份（"验 A 冻 B"）；
2. **错环境**：容器里 `git rev-parse` 失败被写成 `unknown`，或反过来把容器当开发机要求
   clean worktree，导致硬门永远过不去；
3. **训练身份与运行身份脱钩**：用 commit A 训出的模型在 commit B 上跑出报告，事后无从分辨。

因此需要一条可验证的身份链，并且缺失 / unknown / malformed / mismatch 一律**不放行**。

## 3. Decision

### 3.1 先判 context，再套规则（顺序本身是设计）

唯一的运行身份解析入口是 `resolve_runtime_code_identity()`。它**先**用
`build_identity_block()` 判定 `git_available`，**再**选择该 context 的信任规则：

- `git_checkout`：`git HEAD` 可读即可用；`.build_commit` / `build_manifest.json`
  **存在时**必须与 HEAD 一致（矛盾要吵、缺席不吵），`require_build_identity=True`
  （生产 freeze）时缺席也吵。
- `container_build_identity`：`.build_commit` 与 `build_manifest.commit` 必须都存在、
  可证、逐位相等，且 `trusted=true` / `dirty=false`；`code_commit` 取自构建身份，
  **不再查 git**。容器里"没有 git"是设计内，不是错误。
- 该函数**不抛异常**：违例收进 `violations` 返回，由调用方按自己的退出码呈现。

### 3.2 禁止第二套身份解析路径

freeze / capture / mature / shadow model freeze 四个 CLI 必须走统一 resolver。
这条由 AST 级回归测试钉住（`test_alpha_v2_clis_use_shared_resolver_not_git_head`：
断言四个脚本调用了 `resolve_runtime_code_identity` + `assert_runtime_identity`，
且**没有**调用 `git_head` / `git_branch`）。

### 3.3 生产模式四值一致

```text
git HEAD（可读时） == code_commit == .build_commit == build_manifest.commit
```

任一项 `missing / unknown / malformed / mismatch` 都是违例；且 `build_manifest.trusted`
必须为 `True`、`dirty` 必须为 `False`。`code_commit` 的合法形态是
`^[0-9a-fA-F]{7,64}$`（`unknown`、空串、非十六进制、长度不符都算不可证）。

### 3.4 工件身份分两层，生产两层都验

- **字段身份**：`model_manifest.json` 里的 identity 字段（`code_commit`、`config_hash`、
  `feature_schema_hash`、`model_id`…）。
- **内容身份**：每个文件的 `sha256` + 重算的 `artifact_hash`，与 manifest 记录值、
  与调用方传入的 `expected_artifact_hash` 双向比对。

生产路径必须 `load_frozen_model(require_sealed_provenance=True)`：要求工件是 **v3** 哈希
形态且封存字段齐全。哈希 schema 版本判定 `artifact_hash_version_of()` 只认 v1/v2/v3，
未知值直接抛错——"不猜、不回退"。

### 3.5 训练代码身份 ≠ 运行代码身份，且必须相等

`model_training_code_commit` 由 model freeze 用同一个 resolver 取得并写进模型 manifest；
运行期 `code_commit` 由当前 resolver 取得。二者由
`freeze_precheck.assert_model_training_commit()` 比对，不等 / 缺失 / `unknown` /
malformed → 拒绝，**不允许用当前运行 commit 回填训练 commit**。
校验点有三处：freeze（写盘前 + `open_epoch` 前）、capture、mature。非 production 跳过。

### 3.6 epoch 是身份的锚定点

`artifacts/alpha_v2/validation/epochs.json` 是**只追加**账本。`open_epoch` 要求
`freeze_manifest_hash` 锚点，且同时只允许一个开放 epoch，id 不得复用。消费方
（capture / mature / KPI）经 `require_epoch_identity_match()` 对账 8 个
`FROZEN_IDENTITY_KEYS`，并**重读磁盘上的 freeze manifest 重新哈希**——不一致
（含"疑似篡改"）即拒绝。`assert_epoch_open()` 拒绝向已关闭 epoch 写入。

### 3.7 违例必须变成真实退出码

身份类 → `FreezeGateError(exit_code=5)`（默认 6）；价格口径契约 → 4；Production Preflight
硬门 → 7。CLI 捕获后 `return exc.exit_code`。"文档写 exit 4 但进程以 1 退出"视为缺陷。

## 4. Invariants（不能随意改）

1. **非 production 跳过身份硬门是有记录的例外**，前提是 `validation_mode` 如实写进清单。
   把 `rehearsal` / `test` 产物当生产证据使用，即破坏本 ADR。
2. `code_commit_source` 的前四个取值是**已落盘的历史标签**，不得改名
   （`git_rev_parse` / `cli_override_verified` / `container_build_identity` /
   `cli_override_container_build_identity`），否则旧清单与新清单不可比。
3. `resolve_runtime_code_identity()` 不抛异常这一契约不得反转——改了会让四个 CLI 的
   退出码语义一起变。
4. 已推送产物的 `artifact_hash` 算法**不得原地改语义**；新形态必须开新版本（v4）并保留
   旧版本的判定能力。
5. 不得引入第二套 git/构建身份解析路径，包括"临时调试用"的 `git rev-parse`。
   要加新 CLI 就把它加进 §3.2 那条 AST 测试的清单里。
6. 部署期校验器与运行期 resolver 必须同一套判定（由
   `test_deploy_verifier_agrees_with_runtime_resolver` 钉住），不允许两套规则各自漂移。
7. **信任边界**：本机制防的是"意外错配、字段缺失、无意的工件改写"。它**不防**能同时重写
   `model_manifest.json` 与其所描述文件的对手——那需要签名体系，属于**新的 ADR**，
   不能靠在本 ADR 里加字段实现。

## 5. Rejected / Avoided Approaches

以下每一条都有 commit 或代码注释作为"曾经走过这条路"的证据，不是假想方案：

| 被否决做法 | 出处 | 为什么不行 |
| --- | --- | --- |
| 各 CLI 自行解析 git HEAD/branch | `1df77e9` | 容器内一律 `unknown`；四处规则必然漂移 |
| 先要 `git status` 再判定运行环境 | `1df77e9`（BLK-D1） | 容器永远过不去，硬门变成"生产不可用" |
| 只比 identity **字段**、不比**内容** | `361f0df`（R4.1.1） | 重写 manifest 的工件能过 freeze，拖到 capture 才炸 |
| "两边都有值才比较"的宽松对账 | `epoch.py` `epoch_identity_matches` 注释 | 缺失被当成"无可比"= fail-open；已收紧为"任一侧缺即违例" |
| 用内存里的 epoch 对象判断是否开放 | `epoch.py` `require_epoch_identity_match` 注释 | 长跑调度器会往已关闭 epoch 里写 |
| preflight 不与实际冻结模型绑定 | `a59429c`（B4） | "验 A 冻 B"：preflight 通过的不是被冻结的那个模型 |
| 训练数据只有行数没有内容指纹 | `92e65df`（B6） | 换一份数据、贴上原指纹即可通过 |
| `dirty` 非布尔输入被强转成 `False` | `generate_build_manifest._resolve_dirty` | 只传 `--commit` 就得到 `trusted=true`；现改为未知 → `"unknown"` |

## 6. Failure Lessons

| 表现 | 根因 | 当时为什么没挡住 | 现在的机制 |
| --- | --- | --- | --- |
| 生产容器内 freeze 恒 `exit 5`，永远冻结不了 | 身份门在判定环境之前就要求 `git status` | 只在源码检出环境测过 | 先 context 后规则；容器矩阵测试 `test_container_fail_closed_matrix` |
| capture / mature / model freeze 报告里 commit 是 `unknown` | 四个 CLI 各自 `git rev-parse` | 无跨 CLI 一致性检查 | 统一 resolver + AST 回归闸门 |
| 被改写 identity 字段的工件通过 freeze，到 capture 才失败 | freeze 只看字段不看内容 | 字段校验与工件加载是两套代码 | 生产 freeze 强制 `require_sealed_provenance=True`（`test_freeze_rejects_artifact_with_rewritten_identity_stale_hash` / `..._corrupted_booster`） |
| preflight 报告与实际冻结模型不是同一个 | 报告未绑定模型块 | 两者由不同步骤产出，无人对账 | 整个 model block 传入 `assert_preflight_gate`，逐字段不符 exit 7 |
| 训练数据身份可被"同规模换库"绕过 | 指纹不含窗口与列 | 指纹只看行数量级 | `training_data_fingerprint` v2：`source_window` / `warmup_days` / `columns` / `rows` 全部进对账键 |

## 7. Implementation Locations

```text
# 身份采集与裁决
src/stock_analyzer/alpha_v2/validation/runtime_identity.py     # resolve_runtime_code_identity / build_identity_violations / FreezeGateError
src/stock_analyzer/build_identity.py                            # get_build_manifest / manifest_sha256 / trusted
scripts/generate_build_manifest.py                              # 构建期同源自写 build_manifest.json + .build_commit
src/stock_analyzer/config_identity.py                            # redacted_config_hash / stable_payload_hash
Dockerfile                                                        # ARG + 写两份身份文件 + OCI label
scripts/verify_container_build_identity.py                        # 部署期复核（与运行期同判定）

# 工件完整性
src/stock_analyzer/alpha_v2/validation/frozen_model.py            # load_frozen_model / artifact_hash_version_of / sealed provenance
src/stock_analyzer/alpha_v2/validation/freeze.py                  # freeze_manifest_hash / verify_freeze_integrity / assert_freeze_complete
src/stock_analyzer/alpha_v2/validation/training_data_fingerprint.py

# 硬门与状态
src/stock_analyzer/alpha_v2/validation/freeze_precheck.py         # assert_runtime_identity / assert_model_training_commit / assert_worktree_clean / assert_execution_price_raw
src/stock_analyzer/alpha_v2/validation/epoch.py                   # epochs.json 账本 / FROZEN_IDENTITY_KEYS / require_epoch_identity_match
src/stock_analyzer/alpha_v2/validation/preflight.py               # check_runtime_identity / check_model_identity / assert_preflight_gate
src/stock_analyzer/alpha_v2/artifacts.py                          # write_json_atomic / manifests 子目录布局

# 数据可用性 marker（身份的"数据侧"对偶）
src/stock_analyzer/ops/nightly_readiness.py                       # nightly_data_ready.json 生产者/消费者
src/stock_analyzer/ops/raw_delta_baseline.py                      # RAW 基线 bootstrap marker 与校验

# 入口 CLI
scripts/alpha_v2_shadow_model_freeze.py
scripts/alpha_v2_validation_freeze.py
scripts/alpha_v2_shadow_capture.py
scripts/alpha_v2_shadow_mature.py
scripts/alpha_v2_production_preflight.py
```

## 8. Evidence

**测试（断言 fail-closed 行为）**

- `tests/test_alpha_v2_production_runtime_identity.py`：
  `test_container_fail_closed_matrix`、`test_container_manifest_unparsable_is_missing_not_trusted`、
  `test_container_code_commit_never_unknown`、`test_container_override_must_match_build_identity`、
  `test_git_dirty_worktree_rejected_by_freeze_gate`、`test_git_build_identity_conflict_is_rejected`、
  `test_git_override_must_match_head`、`test_unknown_git_state_without_container_identity_is_rejected`、
  `test_runtime_commit_must_equal_frozen_epoch_identity`、
  `test_alpha_v2_clis_use_shared_resolver_not_git_head`（AST 闸门）、
  `test_validation_freeze_requires_build_identity`、
  `test_deploy_verifier_agrees_with_runtime_resolver`
- `tests/test_alpha_v2_m41_model_provenance_binding.py`：
  `test_case2_training_a_runtime_b_rejected_by_gate`、`test_case2_freeze_cli_rejects_and_does_not_open_epoch`、
  `test_freeze_rejects_artifact_with_rewritten_identity_stale_hash`、
  `test_freeze_rejects_artifact_with_corrupted_booster`、
  `test_case345_training_commit_not_provable_is_rejected`、
  `test_missing_training_commit_is_not_backfilled_from_runtime`、
  `test_case6b_artifact_training_identity_tamper_breaks_integrity`
- `tests/test_alpha_v2_m3_enforcement.py`：`test_manifest_replacement_rejected_on_write`、
  `test_manifest_hash_spoof_rejected`、`test_identity_drift_on_shadow_write_rejected`、
  `test_identity_missing_key_fails_on_both_axes`、`test_closed_epoch_blocks_mature_and_keeps_files`、
  `test_dirty_worktree_rejected_by_precheck`、`test_assert_worktree_clean_handles_unknown_state`
- `tests/test_alpha_v2_m3_r3_final_blockers.py`：`test_build_identity_fail_closed_matrix`、
  `test_build_identity_single_source_is_not_enough`
- 另见 `tests/test_alpha_v2_m3_epoch.py`、`test_alpha_v2_m3_frozen_model.py`、
  `test_alpha_v2_m4l_preflight.py`、`test_alpha_v2_m41_training_provenance_sealing.py`、
  `test_raw_delta_baseline_identity.py`

**commit**

- `1df77e9` (2026-09-20) feat(alpha-v2): harden production shadow runtime identity — BLK-D1/BLK-D2
- `361f0df` (2026-09-20) fix(alpha-v2): freeze 生产门补工件内容校验并留档部署复核证据（R4.1.1）
- `92e65df` (2026-09-21) fix(alpha-v2): seal M4-L training provenance identity
- `a59429c` (2026-09-21) fix(alpha-v2): close M4-L R1 production blockers

**文档**

- `docs/alpha_v2/Production_Runtime_Identity_Hardening_Report.md`
- `docs/alpha_v2/M3_Production_Readiness_Report.md`
- `docs/alpha_v2/M4L_Production_Live_Bootstrap_Report.md`

## 9. As-of

- HEAD：`42caaca`（分支 `feat/alpha-v2-raw-execution-delta-r1`）
- 最后对照代码核实：2026-09-23
- 本文描述的是**已提交**实现。整理时工作树另有 5 个双价格相关文件处于未提交修改状态
  （见 ADR-002），与本 ADR 无关，未作为依据。
