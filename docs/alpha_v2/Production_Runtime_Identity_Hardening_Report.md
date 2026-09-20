# Alpha V2.0 — Production Runtime Identity Hardening（BLK-D1 / BLK-D2）实施报告

> 阶段性质：**生产前硬化**，不是 M4，不继续研究 Alpha，不改动模型算法。
> 唯一目标：让已经 PASS 的 M3 Validation Framework 在"不含 Git 仓库与 git binary 的
> 生产 Docker 容器"里安全、可审计地执行 freeze / capture / mature。
> 基线：`M3_ACCEPTED_BASELINE_COMMIT = 33d0f7f97e79215ad8c65f972c0e56eeead10614`
> （`feat(alpha-v2): freeze M3 validated shadow OOS baseline`）。
>
> 纪律：本阶段 `DO_NOT_PUSH = true`、`DO_NOT_DEPLOY = true`、
> `DO_NOT_START_REAL_EPOCH = true`；`alpha_v2.enforce_final_selection` 保持 `false`。

---

## 1. 状态

> **本区块已由下方 "1.1 当前状态（superseding）" 取代。** 下面是 R4 收尾、
> 送 Codex 复核**之前**的状态快照，保留不删、不重写，以便对账当时的判断。

```text
PRODUCTION_RUNTIME_IDENTITY_HARDENING = READY_FOR_CODEX_RECHECK
BLK-D1 = FIX IMPLEMENTED（Codex RECHECK PENDING）
BLK-D2 = FIX IMPLEMENTED（Codex RECHECK PENDING）
commit = NOT CREATED（按要求不提交）
push   = NOT PERFORMED
deploy = NOT PERFORMED
alpha_v2_epoch_001 = NOT STARTED
```

### 1.1 当前状态（superseding，2026-09-20）

```text
R4   RUNTIME_IDENTITY_HARDENING = PASS（2026-09-20 外部独立验收）
BLK-D1                          = CLOSED
BLK-D2                          = CLOSED
R4.1 MODEL_PROVENANCE_BINDING   = PASS（2026-09-20 Codex mini recheck）

FINAL_RUNTIME_HARDENING_COMMIT  = READY_TO_CREATE（本文档定稿后由本批次创建）
PRODUCTION_SHADOW_DEPLOYMENT    = READY_AFTER_NAS_BUILD_PREFLIGHT

Production Shadow               = NOT_STARTED
alpha_v2_epoch_001              = NOT_STARTED
Live Clean OOS Days             = 0
Alpha Verified                  = FALSE
Production Promotion            = LOCKED

commit = NOT CREATED（本轮由 Final Commit 批次创建）
push   = NOT PERFORMED
deploy = NOT PERFORMED
```

> R4.1 的复核结论有独立证据（复核方自建夹具、不复用实施方测试）：
> `%TEMP%/r41_recheck/`（本机临时目录，不进版本库）与
> `artifacts/review_r4_1_20260920/`（证据文件时间戳 12:30–13:15）。
> 其中两项变异对照有牙齿：训练身份门改空操作 → Case 2a 由 rc=5 翻转为 rc=0；
> 工件哈希去掉 code_commit 覆盖 → Case 6b 由 FAILED 翻转为 LOADED。
>
> **该 PASS 的覆盖范围是"冻结模型工件训练 commit 绑定"这一窄域**，它恢复 R4 的
> `FINAL_RUNTIME_HARDENING_COMMIT = UNLOCKED`，但不等于整套 Runtime Identity
> 被重新大验收；R4 的 PASS 仍以其自身验收记录为准。
>
> 验收历史完整保留（本报告与 M3 报告 §17.0）：M3 Round 1 FAIL → Round 2 FAIL →
> Round 3 PASS → R4 PASS → R4.1 首轮 FAIL（修复不在树里）→ R4.1 实施 →
> R4.1 mini recheck PASS。**没有任何一环被改写。**

## 2. 基线

```text
M3_ACCEPTED_BASELINE_COMMIT = 33d0f7f97e79215ad8c65f972c0e56eeead10614
施工分支 = feat/alpha-v2-production-runtime-identity（从上述 commit 切出）
未跟踪文件 docs/system_issues_for_review_20260917.md = 全程未删/未 stash/未提交/未覆盖
```

## 3. BLK-D1（生产容器内 production freeze 不可执行）

```text
Root Cause =
  旧 freeze CLI 在**判定运行环境之前**先要 git 工作区证据：
  resolve_code_commit → assert_worktree_clean(git status) → assert_build_identity。
  容器里 git_worktree_dirt() 返回 None（git 不可得），而生产模式把 None 判为
  "无法证明干净"→ exit 5。于是容器永远到不了"用构建身份自证"那一步。
  根因不是门太严，而是**顺序错了**：拿一个在该环境里不适用的证据来源去否决它。

Implementation =
  1) runtime_identity.resolve_runtime_code_identity()（唯一入口）：先判 runtime context，
     再套该 context 的规则。
     - git_checkout：git HEAD 可读即可用；override 与 HEAD 矛盾仍硬拦；工作区门由
       freeze 门在**该形态下**单独执行（不变）。
     - container_build_identity：.build_commit 与 build_manifest.commit 必须存在、可证、
       逐位相等，且 trusted=true / dirty=false；此形态**不查 git**（容器里没有 git
       checkout 是设计内），code_commit 取自构建身份。
     - 非 production（rehearsal/test）：不做身份硬门（清单如实标 validation_mode）。
  2) freeze_precheck.assert_runtime_identity()：四个 CLI 共用的唯一身份门；
     仅当 identity_source == git_checkout 时才追加工作区干净门。
  3) assert_build_identity（R3 四值一致门）保留入参签名，实现改为调用唯一的判定函数
     runtime_identity.build_identity_violations（不出现第二套规则）。
  4) 镜像构建阶段写入身份：Dockerfile 的 RUN 同一次调用产出
     /app/build_manifest.json 与 /app/.build_commit（两源相等是构造性的）。
  5) 部署脚本构建前用 git 取证并传递（commit/short/dirty/built_at），构建后**从镜像里
     读出这两个文件复核**（scripts/verify_container_build_identity.py --expect-commit）。

Result =
  沙箱（无 .git）实测：freeze CLI exit 0，写出清单并开启 epoch；
  manifest.build_identity.identity_source = container_build_identity，
  worktree_dirty_entries = null（该形态不适用），code_commit == 构建身份。
  破坏身份（两源不一致）→ exit 5，不落任何清单。
```

## 4. BLK-D2（容器内 capture / mature 的运行身份对账必然失败）

```text
Root Cause =
  capture / mature / shadow model freeze 各自直接调用 git_head(REPO_ROOT)，
  容器内恒为 "unknown"，与 epoch 冻结的真 SHA 不符 → 必然 identity mismatch。

Implementation =
  四个 CLI 全部改为：resolve_runtime_code_identity(...) → assert_runtime_identity(...)，
  用同一个 runtime_code_commit（容器内 = 构建身份）参与 epoch 对账。
  - capture：身份门已前移到"只依赖代码身份"的位置（面板/模型加载之前，fail-fast），
    并把 identity_source 等审计字段写进当日 shadow_day manifest。
  - mature：身份门在同一位置（读 epoch 的 validation_mode 决定是否启用硬门）。
  - shadow model freeze：身份门前移到面板加载之前；provenance 增加
    code_commit_source / identity_source，保证"模型 provenance code_commit ==
    validation freeze code_commit == capture == mature == 部署 commit"。
  - 新增 AST 级回归闸门：四个 CLI 的源码里不允许再出现 git_head/git_branch 调用。

Result =
  沙箱实测：mature exit 0（身份门过后正常空跑）；capture 走到冻结模型校验才中止
  （排演工件不是可推理模型，与身份无关）；两者在破坏身份的沙箱里都 exit 3。
```

## 5. Runtime Identity 架构

### 5.1 统一入口与产物

```text
runtime_identity.resolve_runtime_code_identity(root, requested_code_commit,
                                               validation_mode, require_build_identity)
  → RuntimeCodeIdentity(code_commit, identity_source, code_commit_source,
                        git_available, git_head, git_branch, git_worktree_state,
                        build_commit_present, build_commit,
                        build_manifest_present, build_manifest_commit,
                        build_manifest_trusted, build_manifest_dirty,
                        build_manifest_path, identity_verified, violations)
```

调用方：`freeze`（require_build_identity=True）、`model freeze`、`capture`、`mature`
（=False：源码检出里缺这两个文件是常态；容器形态恒按"必须存在"判定）。
纯判定函数 `runtime_identity.build_identity_violations` 是四值一致语义的**唯一**实现，
`freeze_precheck.assert_build_identity` 只是它的 exit-code 呈现层。

### 5.2 两种 context 的规则

```text
git_checkout
  git HEAD 可读才算可用；--code-commit 给了就必须等于 HEAD
  .build_commit / build_manifest.json 存在 ⇒ 必须与 HEAD 一致（矛盾要吵、缺席不吵）
  require_build_identity=True（生产 freeze）⇒ 缺席也吵（R3 语义不变）
  生产 freeze 追加：工作区必须可证干净（未跟踪白名单仍只有那两个身份文件）

container_build_identity
  git 不可用是**事实**，不是错误；code_commit 取自构建身份
  .build_commit 与 build_manifest.commit 必须存在、形态合法、逐位相等
  build_manifest 必须 trusted=true、dirty=false（trusted 由共享解析器按
  "commit 可证 且 dirty 可证"派生，不是清单里的一个可伪造字段）
```

### 5.3 为什么不是"放宽校验"

两种 context 各自使用**本环境可证**的可信来源：源码检出用 git 自证，不可变容器用构建期
写入的不可变产物互证。容器形态的门并不比 git 形态弱——它要求两个独立文件逐位相等，
而 `.build_commit` 与清单在同一层里、只在镜像里存在，构建后又被部署脚本从镜像内复核。
没有引入任何"git 不在就相信某个声明"的路径：`--code-commit` 在容器里若与构建身份不一致
同样被拒（`cli_override_container_build_identity` 标签如实落盘）。

### 5.4 威胁模型边界（如实说明）

容器形态的可信前提是"镜像构建期写入的文件不可被运行期篡改"。在**可写**文件系统上手工
摆放两个互相一致的 JSON/TXT 文件，本门禁无法区分——那需要写权限，且与
"--code-commit + 两源互证"这条 R3 就已声明的路径同级。本阶段没有把它变弱，
也没有假装它更强：`identity_source` 与 `violations` 都进清单，审计看得到来源。

## 6. 构建身份（镜像侧）

```text
.build_commit        构建阶段写入 /app/.build_commit（内容 = SOURCE_COMMIT + \n）
build_manifest.json  同一次 generate_build_manifest.py 调用产出
                     {commit, short_commit, dirty, built_at_utc, config_schema,
                      runtime_state_schema}（沿用既有 schema，未新造第二套）
trusted              派生值 = commit 可证 且 dirty 可证（共享解析器给出）
dirty                生产部署由 nas_deploy_update.sh 在 build 前用 git 现场取证；
                     手工 docker build 不传参 ⇒ commit=unknown/dirty=unknown ⇒ trusted=false
                     ⇒ 生产 freeze/capture/mature 全部 fail-closed
OCI label            org.opencontainers.image.revision/created 同步写入（既有机制）
```

顺带修掉的两个真实缺陷（都属于"trusted 可以无证据成立"）：

1. `generate_build_manifest.py` 把非空但非布尔来源的 `--dirty`（例如默认的 `"unknown"`）
   写成布尔 `False`，于是"只传 commit 不传 dirty"就能得到 `trusted=true`。
   现改为：只接受 1/0/true/false/yes/no，其它非空值如实写 `unknown`，空值才现场探测，
   **探测失败也写 unknown**（绝不把"探不到"写成干净）。
2. `nas_deploy_update.sh` 把 `STOCK_ANALYZER_BUILD_DIRTY` 写死 `0`，且 `BUILD_TIME_UTC`
   只 export 未传进 build（镜像里 `built_at_utc=unknown`）。现改为现场取证 + 显式传递，
   并在 build 后新增"镜像内身份复核"步骤（读镜像里的两个文件比对 commit/dirty/期望 SHA）。

## 7. 部署顺序复核（§18 要求）

```text
Old Order（M3 Readiness §17.2 原文）
  1) validation_freeze --model-dir <冻结模型目录> --open-epoch
  2) shadow_model_freeze
  —— 步骤 1 引用尚未产出的目录，顺序自相矛盾。

依赖的真实证据（代码事实，不是文档约定）
  - 生产模式 feature schema 必须非空，来源只能是 --model-dir 的模型工件或
    --feature-columns-file（freeze_precheck.resolve_feature_schema_columns，exit 6）；
  - open_epoch 冻结的 model_id / model_artifact_hash / feature_schema_hash 必须与之后
    capture 的运行期模型身份逐位一致（epoch.epoch_identity_matches，严格模式）；
  - 机器可验证形式（smoke C 步骤）：不给 --model-dir 的生产 freeze → exit 6。

Final Proposed Order
  0) 部署最终代码/镜像 + 构建身份复核（.build_commit == build_manifest.commit、
     trusted=true、dirty=false；镜像内复核）
  1) 生产 Data / Build Identity Preflight（容器内 resolver 自检：
     identity_verified=true、identity_source=container_build_identity）
  2) 冻结 Alpha V2 Shadow Model → model_id / artifact_hash / feature_schema_hash /
     calibration / provenance.code_commit
  3) 生成 Validation Freeze Manifest + --open-epoch（--model-dir 指向步骤 2 的产物）
  4) 当日 Data Health
  5) Shadow Capture
  6) Outcome Mature
  7) KPI Report
  已同步修正 docs/alpha_v2/M3_Production_Readiness_Report.md §17.2（原顺序保留在
  "原版（已作废）"块里以便对账）。
```

## 8. 文件变更

```text
src/stock_analyzer/alpha_v2/validation/runtime_identity.py   统一 resolver + 唯一判定函数
src/stock_analyzer/alpha_v2/validation/freeze_precheck.py    唯一身份门 + 旧 API 适配层
src/stock_analyzer/build_identity.py                         共享 manifest 读取增加 root 参数
scripts/alpha_v2_validation_freeze.py                        BLK-D1
scripts/alpha_v2_shadow_capture.py                           BLK-D2 + 审计字段
scripts/alpha_v2_shadow_mature.py                            BLK-D2
scripts/alpha_v2_shadow_model_freeze.py                      BLK-D2 同类 + provenance
scripts/generate_build_manifest.py                           dirty 语义修复 + --build-commit-file
scripts/verify_container_build_identity.py（新）              部署期镜像内身份复核（仅标准库）
scripts/alpha_v2_runtime_identity_smoke.py（新）              NO_GIT_CONTAINER_SMOKE
scripts/nas_deploy_update.sh                                 build 前取证 + build 后镜像内复核
Dockerfile                                                   构建阶段写入 .build_commit
.gitignore                                                   /.build_commit（部署期产物）
docs/alpha_v2/M3_Production_Readiness_Report.md              §17.2 顺序修订 + §17.3 后续指针 + §17.4
docs/alpha_v2/PROGRESS.md                                    顶部 current-state 更新 + 本阶段记录
docs/alpha_v2/Production_Runtime_Identity_Hardening_Report.md（新，本文件）
```

（详细计数见 §9–§12。）

证据工件（`artifacts/*` 已被 gitignore，不进版本库）：

```text
artifacts/alpha_v2/audit/no_git_container_smoke.json          沙箱 smoke A–G 判定
artifacts/alpha_v2/audit/docker_identity_smoke.json           真实容器 smoke 判定
artifacts/alpha_v2/audit/docker_identity_smoke_in_container.log   容器内完整输出
artifacts/alpha_v2/audit/docker_identity_smoke_check.sh           容器内执行的检查脚本
artifacts/alpha_v2/audit/docker_identity_smoke_Dockerfile.identity 本地测试镜像 Dockerfile
artifacts/alpha_v2/audit/junit_runtime_identity.xml           定向测试 junit
artifacts/alpha_v2/audit/junit_m3_r4.xml                      M3 定向 junit
artifacts/alpha_v2/audit/junit_full_r4.xml                    全量回归 junit
artifacts/alpha_v2/audit/full_regression_r4.txt               全量回归原始输出
```

## 9. 测试

```text
新增 tests/test_alpha_v2_production_runtime_identity.py：39 collected / 39 passed / 0 failed
  （junit：artifacts/alpha_v2/audit/junit_runtime_identity.xml）
  容器形态：resolver 正例 / "旧路径必然失败 vs 新路径放行"对照 / 11 例 fail-closed 矩阵 /
            清单不可解析按缺失处理 / code_commit 不得为 unknown / override 一致性 /
            非 production 免门
  检出形态：真 git 仓库（git init + commit）正例 / 缺构建产物两种形态语义 /
            脏工作区拒绝并可恢复 / 白名单一致性 / 构建身份与 HEAD 矛盾 / override 一致性 /
            "既无 git 又无构建身份"拒绝
  对账：runtime code_commit 必须等于 epoch 冻结身份（不一致与缺失都算违例）
  接线：四个 CLI 的 AST 级检查（必须调用 resolver/gate、不得调用 git_head/git_branch）
  防漂移：部署期校验器与运行期 resolver 在同一夹具集上判定一致（7 例参数化）
  端到端：no-git 沙箱 smoke（子进程真跑四个 CLI）

M3 定向（pytest -q tests/test_alpha_v2_m3_*.py，10 个文件）
  collected = 112 / passed = 112 / failed = 0 / errors = 0 / skipped = 0
  （junit：artifacts/alpha_v2/audit/junit_m3_r4.xml；与 M3 报告更正后的 112 一致）

相关批次（alpha_v2 M1/M2/S 系列 + scheduler + 部署脚本，pytest -q）
  collected = 466 / passed = 465 / failed = 0 / errors = 0 / skipped = 1
  （junit：artifacts/alpha_v2/audit/junit_related_r4.xml）

全量回归（pytest -q -n 4 --dist loadfile，exit code 0）
  collected = 3649 / passed = 3647 / failed = 0 / errors = 0 / skipped = 2
  （junit：artifacts/alpha_v2/audit/junit_full_r4.xml；3649 = M3 期基线 3610 + 本阶段新增 39）
  ⚠️ 计数口径：xdist 的进度行会被 "\r" 重写，按进度字符数出来的 3605 是**渲染伪影**
      （少算 44）；本报告一律以 junit 为准（项目既有约定：测试数只信 junit）。

ruff（本阶段新增/修改的全部 Python 文件）
  ruff check  → All checks passed（13 个文件）
  ruff format → 新文件全部已格式化；被本阶段改写的 2 个既有文件已按 ruff format 收敛，
                其余既有文件在基线（HEAD）上本来就 would-reformat，本轮不夹带无关格式化
```

## 10. No-Git 容器 Smoke

### 10.1 沙箱形态（本地，可重复运行）

沙箱复刻容器形态：把 `src/scripts/config` 复制到临时根（**不含 .git**），写入构建身份两个
文件，子进程 PATH 去掉 git 所在目录（复刻"容器里连 git 二进制都没有"）。CLI 的
`REPO_ROOT` 由脚本路径推导，所以沙箱就是它们的仓库根。

每个 CLI 跑两次（身份完好 / 身份破坏），用退出码差异证明"身份门确实被执行了"；
破坏身份在各 CLI 上的期望码沿用既有语义（freeze = 5，shadow 三件套 = 3）。

```text
A runtime identity resolution   PASS  identity_source=container_build_identity
                                      git_available=false，violations=[]
B shadow model freeze identity  PASS  身份门放行（后续因缺市场库中止）；破坏身份 exit 3
C validation freeze identity    PASS  exit 0；清单 code_commit == 构建身份；
                                      缺 --model-dir → exit 6（模型必须先冻结的机器证据）；
                                      破坏身份 exit 5
D open epoch                    PASS  epochs.json identity.code_commit == 构建身份
E shadow capture identity       PASS  身份门放行（后续因排演工件中止）；破坏身份 exit 3
F mature identity               PASS  exit 0（身份门过后正常空跑）；破坏身份 exit 3
G no-git + no-identity          PASS  rehearsal 也必须干净 exit 5（不落盘、无 traceback）
证据 artifacts/alpha_v2/audit/no_git_container_smoke.json
```

### 10.2 真实 Docker 容器（本地测试镜像）

```text
镜像            stock-analyzer:local-identity-smoke（sha256:fb81702c…）
                 = python:3.11-slim + 仓库 src/config/scripts + **与生产 Dockerfile 逐行同构
                   的身份层**（同一 generate_build_manifest.py、同一 --build-commit-file、
                   同一目标路径）
偏差            省略 frontend-builder 阶段（无 /app/frontend_dist）；未挂载 artifacts
证据            artifacts/alpha_v2/audit/docker_identity_smoke.json + _in_container.log
                + _check.sh + _Dockerfile.identity + _build.log

git present            = NO（command -v git → 空）
.build_commit present  = YES（/app/.build_commit = 33d0f7f9…，41 B）
build_manifest present = YES（commit=33d0f7f9…，dirty=false，built_at_utc 已写入）
identity source        = container_build_identity（git_available=false，code_commit_source 同名）
freeze identity gate   = PASS（freeze CLI exit 0，写出清单并开启 epoch_900；
                         build_identity.identity_source=container_build_identity；
                         code_commit == 镜像构建身份）
capture identity gate  = PASS（capture CLI exit 5 ≠ 身份门 3；停在该排演工件缺 files 段处，
                         其前已打印 data_health，证明身份门已放行）
mature identity gate   = PASS（mature CLI exit 0，"没有 shadow 日；无需运行"）
```

沙箱 smoke 的"无 git 二进制"是用 PATH 剥离模拟的（沙箱本来也没有 .git）；真实 Docker
这一层给出了 `git` 二进制确实不存在的一手证据。两处都没有创建真实生产 epoch
（用 `alpha_v2_epoch_900` + 容器内 `/tmp/isolated` 隔离 root）。

## 11. Fail-Closed 矩阵（容器形态）

```text
.build_commit 缺失                → FAIL
build_manifest.json 缺失          → FAIL
build_manifest.json 不可解析       → FAIL（按缺失处理，不用环境变量兜底）
两源 commit 不同                  → FAIL
manifest.commit 缺失/非法/unknown  → FAIL
.build_commit 非法/unknown         → FAIL
manifest trusted=false            → FAIL
manifest dirty=true / unknown     → FAIL
freeze commit != runtime commit   → FAIL（epoch 对账，缺一即违例）
capture runtime commit != frozen  → FAIL
mature runtime commit != frozen   → FAIL
检出：脏工作区 / HEAD 与构建身份矛盾 / 既无 git 又无构建身份 → FAIL
```

## 12. Legacy 隔离

```text
final_signal_min_threshold = 70        未改动
Cross Review thresholds                未改动
300 / 100 / 50、final cap 5            未改动
serving model / Legacy model registry  未改动
飞书与正式推荐                          未改动
alpha_v2.enforce_final_selection       = false（未改动）
本阶段改动全部落在 alpha_v2 命名空间与部署/构建身份链路，不触碰任何阈值与 serving 工件。
```

## 13. 遗留

> **R4.1 后续（2026-09-20）**：独立 Codex 复核（R4 部署路径验收）曾提出一项 Non-Blocking
> ——"模型训练 commit 与 epoch/运行 commit 不一致时没有任何门禁"（Case A5）。该缺口已在
> R4.1 中关闭，见 §15。下面 1)–5) 是 R4 当时的记录，保留不改。

```text
Blocking：无新增。（BLK-D1 / BLK-D2 实现完成，待 Codex 独立复核 deployment path）

Non-Blocking（记录在案）：
1) 容器形态下 git_branch 恒为 unknown（镜像里没有分支信息）。commit 才是身份，分支只作
   审计；如需分支可在构建期加一个 ARG（本轮刻意不动 manifest schema）。
2) 生产 CLI 的"工作区干净"门只适用于 git_checkout 形态。若在 NAS **宿主仓库**（而非容器）
   执行 freeze，frontend_dist/ 等部署产物会作为未跟踪项被计入脏清单（未跟踪白名单仍只有
   两个身份文件，未放宽）。生产路径按 §7 在容器内执行，不受影响。
3) 仓库 Dockerfile 的**全量**镜像构建在本机未跑通：frontend-builder 阶段（node:22-slim +
   npm ci）在 Docker Desktop 上 35 分钟无缓存增长，已终止；与本次改动无关（本阶段不部署，
   故不阻塞）。身份层本身已用"逐行同构的最小镜像"在真实容器里跑通（§10.2）。
   —— 部署授权后应在 NAS 上按 §7 的顺序重新验证一次全量构建 + 镜像内复核。
4) 生产容器的 3 个服务（api / scheduler-critical / scheduler-heavy）共用同一个 image
   tag，构建身份一致；若将来拆成不同 tag，需各自复核。
5) NAS 现有镜像（7e9e33b）没有任何 alpha_v2 产物，本阶段未部署，故"NAS 容器内实测"仍是
   部署后动作（本轮给的是同形态的本地真实容器证据）。
```

## 14. 未执行的授权动作

```text
git commit / push / merge main        NOT PERFORMED
NAS 部署 / 生产 docker build / restart NOT PERFORMED
.env 修改                              NOT PERFORMED
scheduler 接线                         NOT PERFORMED
alpha_v2_epoch_001（真实）              NOT STARTED
PRODUCTION_SHADOW_FROZEN_COMMIT        PENDING_UNTIL_CODEX_PASS_AND_FINAL_COMMIT
```

---

*编制者：ZCode（Production Runtime Identity Hardening 批次）。
证据：`artifacts/alpha_v2/audit/no_git_container_smoke.json`、
`tests/test_alpha_v2_production_runtime_identity.py`。*

---

## 15. R4.1 — Frozen Model Provenance Commit Binding（2026-09-20）

> 阶段定位：R4 已 PASS（BLK-D1 / BLK-D2 CLOSED）。R4.1 只解决独立复核提出的那一项
> Non-Blocking：**模型训练身份没有被绑定、也没有被完整性保护**。不扩大 scope。

### 15.1 被击穿的场景（修复前，均在真实 CLI 上复现）

```text
Case 2  train=A / runtime=B  → validation freeze rc=0 且 epoch 已 open
Case 3  工件训练 commit missing  → freeze rc=0 且 epoch 已 open
Case 4  工件训练 commit unknown  → freeze rc=0 且 epoch 已 open
Case 5  工件训练 commit malformed → freeze rc=0 且 epoch 已 open
Case 6b 改写工件里的训练身份      → capture rc=0（未被任何完整性检查发现）
```

根因两条：
1. `frozen_model_identity_payload()` 没有把工件的训练身份传播进 freeze 清单的 model 块，
   也没有任何环节把它与运行身份对账（8 个 `FROZEN_IDENTITY_KEYS` 里没有它）；
2. `_artifact_hash` 的哈希体不含 `code_commit`，所以改写工件里的训练身份不破坏工件完整性。

### 15.2 实现的唯一语义

```text
字段名（唯一）: model_training_code_commit
权威来源      : 冻结模型工件 manifest 顶层的 code_commit
              （由 alpha_v2_shadow_model_freeze.py 在训练时从**统一 Runtime Identity
                Resolver** 取得；不提供任何 --xxx-commit 人工入口）
传播路径      : frozen_model_identity_payload() → model_training_code_commit
              → build_validation_freeze() 的 model 块（规范化必须保留该字段）
              → freeze manifest（受 freeze_manifest_hash 覆盖）
              → epoch identity（落账可审计）
完整性        : _artifact_hash / _artifact_hash_from_manifest 均纳入 code_commit
```

**强不变量**（生产）：

```text
runtime code_commit == frozen model training code_commit
```

### 15.3 三个门禁点

| 位置 | 行为 | 退出码 |
|---|---|---|
| `alpha_v2_validation_freeze.py` | schema 门之后、写盘/开 epoch **之前**：缺失/unknown/非法/不一致 → 拒绝；清单不落盘、epoch 不 open | 5 |
| `alpha_v2_shadow_capture.py` | 生产形态：磁盘冻结清单（hash 锚定）里的训练身份必须可证且 == 运行身份 | 3 |
| `alpha_v2_shadow_mature.py` | 同上 | 3 |

非 production（rehearsal / test）不做此门——排演工件本来就不是生产身份。

### 15.4 用例矩阵（真实 CLI，修复后实测）

```text
Case 1  train=A / runtime=A       → freeze rc=0 + epoch open + 全链路一致
Case 2  train=A / runtime=B       → rc=5，epoch_open=False，清单未落盘
Case 3  training commit missing   → rc=5，epoch_open=False
Case 4  training commit unknown   → rc=5，epoch_open=False
Case 5  training commit malformed → rc=5，epoch_open=False
Case 6a 篡改 freeze manifest      → freeze_manifest_hash 失锚（capture rc=3，行为保持）
Case 6b 篡改工件训练身份           → 工件 artifact_hash 与内容不符 → 拒绝加载
```

身份链（Case 1 产物）：

```text
sandbox .build_commit / 工件 manifest.code_commit / freeze manifest.code_commit /
freeze manifest.model.model_training_code_commit / epoch identity.code_commit /
epoch identity.model_training_code_commit = 同一值（唯一值个数 = 1）
```

### 15.5 范围外（明确不做）

- 不做"模型必须每天重训"：epoch 内训练一次、冻结后由 capture/mature 持续复用同一工件；
  本门禁只校验**代码身份一致性**。
- 不支持"用历史 commit 训练的模型跑在新 commit 上"：那需要单独设计的兼容性契约与显式迁移，
  本轮不提供任何 fail-open 通道。
- 不处理 R4 复核列出的其它 Non-Blocking（ShadowTamperError 出口码、p1 构建路径、
  git_branch unknown、CWD manifest 候选、NAS 宿主仓库脏清单）。

### 15.6 状态

> **本区块已由下方 "15.6.1 当前状态（superseding）" 取代**，原文保留不删。

```text
R4   RUNTIME_IDENTITY_HARDENING = PASS（Codex 独立验收）
R4.1 MODEL_PROVENANCE_BINDING   = Implementation DONE / CODEX_MINI_RECHECK = PENDING
commit / push / deploy / epoch_001 = 均未执行
```

#### 15.6.1 当前状态（superseding，2026-09-20）

```text
R4   RUNTIME_IDENTITY_HARDENING = PASS（Codex 独立验收）
R4.1 MODEL_PROVENANCE_BINDING   = PASS（Codex mini recheck，独立复核含 2 例变异对照）
FINAL_RUNTIME_HARDENING_COMMIT  = READY_TO_CREATE
PRODUCTION_SHADOW_DEPLOYMENT    = READY_AFTER_NAS_BUILD_PREFLIGHT
commit / push / deploy / epoch_001 = 均未执行（commit 由 Final Commit 批次创建；
                                     真实 epoch_001 与生产 Shadow 均未启动）
```

> 复核覆盖范围提示：这次 PASS 只回答"工件训练 commit 是否被绑定与完整性保护"。
> R4.1 首轮窄复核曾判 FAIL（修复不在树里），本轮 PASS 是在
> **同一棵工作树**（Reviewed code files 逐字节未变）上重跑对抗脚本得到的，不是重新解释。
> 详见 §1.1。
