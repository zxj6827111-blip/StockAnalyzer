# ADR-002 双价格冻结契约（QFQ / RAW）

Status: Draft

As-of: 2026-09-23 @ HEAD `42caaca`（详见 §11）

## 1. Status

**Draft，且是有意保持 Draft。**

原因不是证据不足，而是本契约当前处于**两段不同成熟度**的拼接状态，而仓库还没有对第二段
做出裁决：

```text
第一段：价格角色契约（feature 可 qfq / execution 必须 raw+certified）
        → 证据充分、已提交、有专属测试、有事故支撑。这部分本身可视为 Accepted。

第二段：决策集裁决口径（(symbol, decision_date) 在 execution 面板没有当日 bar 时
        该 fail closed 还是该过滤）
        → HEAD 是一种做法，工作区未提交的改动是另一种做法，两者语义相反。
          该段仍在评审中，不能写成已接受决策。
```

第二段定稿（提交并通过验收，或明确否决）后，本 ADR 应升为 Accepted 或删除 §5。

## 2. Context

项目价格契约（`src/stock_analyzer/backtest/price_contract.py`）自始写明：

```text
Feature Series may be QFQ
Execution Series must be RAW
```

Alpha V2 的冻结/成熟链路此前只有一个 `--market-db`，而生产 NAS 的正式库
`/app/artifacts/vendor_delta/market_delta.duckdb` 的 `price_series_mode = qfq`。
于是**同一份 qfq 序列同时喂给了特征、label、成交价、MAE/MFE 与超额基准**。

后果不是"口径不够精确"，而是训练目标本身失真：复权序列在每个除权日产生非交易性跳变，
把"研究口径的调整"写成"真实亏损"；涨跌停判定与可成交性（`ExecutionMatcher`）拿到的也是
复权价。更糟的是原实现在 `price_mode_certified = false` 时**只打 warning**，所以这条
错误路径可以一路跑到冻结完成。

## 3. 设计意图（三个概念必须分开）

这是本契约最容易被写坏的地方。三者是**不同层次的事实**，任何一步把它们当成同一个，
都会产生静默错误：

| 概念 | 含义 | 由谁决定 |
| --- | --- | --- |
| **PIT universe（候选集）** | `as_of` 时点按 ≤`as_of` 的 bar 事实选出的**合格候选** | `DailyPanel.pit_universe()`，`min_history_days=60`、`expected_active_lookback_days=5` |
| **execution availability（可交易集）** | 该 `(symbol, decision_date)` 当天在 execution 面板**确有 bar**，T+1 入场才成立 | execution 面板的 bar 集合 |
| **training frame（训练帧）** | 可交易集 ∩ feature 面板 ∩ 拿得到 label 的行 | `build_dual_price_training_frame` 产物 |

关键设计事实：`pit_universe` 的入选**完全由 ≤`as_of` 的 bar 事实决定**（`index_symbols`
只是候选名单），而 `expected_active_lookback_days=5` 的语义是"最近 5 个交易日内交易过即
算活跃"。因此**"当天停牌但三天前还在交易"的票按设计就在候选池里**——它不是脏数据，
是 PIT 语义的直接产物。这条是 §5 争议的根源。

## 4. Decision：价格角色契约（已提交，证据充分）

1. **两个角色必须显式分开**：`feature` 面板与 `execution` 面板各自独立加载、独立认证、
   独立成块记录。
2. **execution 只允许 raw**：`price_mode == raw` **且** `price_mode_certified == true`，
   否则 FAIL CLOSED（`EXIT_PRICE_SERIES_CONTRACT = 4`）。
3. **qfq 对 feature 是正确口径，不是妥协**：`FEATURE_PRICE_MODE_ALLOWED = (qfq, raw)`，
   但必须**可证**，且必须等于冻结模型声明的 `feature_price_mode`。
4. **口径必须可证，不能靠猜**：认证结论来自"面板行自报"或"经验探针"，二者之一，
   且 `decision_rule` 落进审计证据：
   `panel_rows_declare_raw` / `panel_rows_declared_non_raw` / `empirical_probe_passed` /
   `empirical_probe_failed`（`research/panel.py` `certify_price_mode`）。
   认证证据里进审计的字段是**白名单固定顺序**（`CERT_EVIDENCE_AUDIT_KEYS`），
   否则"探针多打一个统计量"会变成"换一份工件身份"。
5. **绝对收益只来自 raw execution**：`net_return_*` / `excess_return_*` / `up_*` /
   `mae_*` / `mfe_*` 与全部基准序列都由 execution 侧 outcome 派生；feature 侧只贡献
   特征值与风格分组维度。
6. **两条身份都进工件哈希**：`feature_data_identity` / `execution_data_identity` 分开记录、
   分开对账、分开封存。内容对账用 `IDENTITY_CONTENT_KEYS`（`fingerprint_version` /
   `source_window` / `warmup_days` / `fingerprint` / `rows` / `columns`），
   口径对账另走 certify 检查——**不得把口径塞进内容指纹**（指纹是内容指纹，不含口径）。
7. **守卫先于重活**：口径不合法要在构造完整特征矩阵**之前**失败。
8. **研究回放的唯一出口是显式的**：`build_label_v2(enforce_execution_price_series=False)`
   **且** `research_replay_reason` 非空（理由进 diagnostics）。生产入口（freeze / mature）
   **不含该开关**，由结构测试 `test_production_entrypoints_have_no_escape_hatch` 钉住。
9. **只有 rehearsal 允许带标注降级**：`LIVE_STRICT_VALIDATION_MODES = ("production",
   "test")`——`test` 与 `production` 同语义（本仓库既有约定：`test` 只为确定性时钟存在，
   且在 KPI 层是 clean-OOS 合格模式）。rehearsal 降级时 style 来源标
   `execution_panel_fallback_rehearsal`。
10. **旧单库形态允许但必须自曝**：`db_role_binding = legacy_single_db`，
    且 `--market-db` 现在只在 `--rehearsal` 下被接受。

## 5. 未决：决策集裁决口径

### 5.1 HEAD 的实现（零容忍）

`assert_decisions_aligned()`：缺一条 `(symbol, date)` 即抛
`PriceSeriesContractError` → CLI exit 4。生产调用点**只有一个**
（`validation/dual_price_freeze.py` 的 `build_dual_price_training_frame` 第 2 步）。
文档表述是"缺失即 target 不可用，绝不允许退回 qfq 或从 qfq 反推 raw"。

### 5.2 HEAD 实现与 §3 设计意图的冲突

按 §3，PIT 候选池**按设计**含当天停牌的票，这些候选在 execution 面板没有当日 bar 是
预期内事实。零容忍口径下这会让生产窗口无法冻结——这是**已提交实现与设计意图之间的
真实张力**，不是数据问题。

### 5.3 工作区未提交的提案（P3.1，2026-09-23）

> 以下内容**未提交、未通过验收**，记录在此是为了不让它被忘掉或被误当成现状。
> 涉及文件：`src/stock_analyzer/alpha_v2/dual_price_series.py`、
> `src/stock_analyzer/alpha_v2/validation/dual_price_freeze.py`、
> `scripts/alpha_v2_shadow_model_freeze.py`、
> `tests/test_alpha_v2_dual_price_series.py`、`docs/alpha_v2/P0_Dual_Price_Series_Contract.md`。

提案把"缺 bar"分成两类，判据是**可复现的集合关系**而非比例直觉：

| `(symbol, decision_date)` 的观测 | 提案裁决 | 原因码 |
| --- | --- | --- |
| 在 execution 面板里 | 保留 | — |
| 票在、日在，仅当天无 bar（停牌） | 过滤并写审计 | `NO_EXECUTION_BAR_ON_DECISION_DATE` |
| 票在整个 execution 面板都不存在 | fail closed | `SYMBOL_NOT_IN_EXECUTION_PANEL` |
| 该日期在 execution 面板里不是交易日 | fail closed | `DECISION_DATE_NOT_A_SESSION_IN_EXECUTION_PANEL` |
| feature 面板当天**有** bar 而 execution 没有 | fail closed | `FEATURE_PANEL_HAS_BAR_ON_DECISION_DATE` |

后三类的理由值得单独记住：**它们都不是"停牌"，而是两份面板对同一份事实给出了不同答案**。
静默过滤会把真实的断供 / 截断 / 换库伪装成"少了几行训练样本"。
另配两条量级闸（总体 2% / 单日 10%，各带 50 行下限）兜"形态合法但规模异常"。
提案自述的实测数字（生产窗口 2025-06-02..2026-08-31，6,602 / 1,650,654 = 0.400%，
`status=PASS`）**本次未复现验证**。

### 5.4 定稿前必须回答的问题

1. `assert_decisions_aligned` 在提案里被定位为"供研究/回放等必须逐条对齐的路径使用"，
   但工作树里它**只剩测试调用**——所称的研究/回放生产调用点尚不存在。要么补上调用方，
   要么删掉这个定位描述。
2. 量级闸的两个阈值（2% / 10%）来自"实测 0.400% × 5 倍余量"。用当前窗口的观测值反推
   阈值，等于把"这一批数据的样子"写进契约——换窗口/换 universe 规模后是否会误杀？
3. `cross_check_panel = feature` 面板：它假设两份面板同源同窗口。若 feature 侧本身也缺
   某段数据，跨面板一致性检查会**双向失效**（两侧都缺 → 判成合法停牌）。
4. 被过滤行不进训练帧，是否影响质量池/基准分母？提案注释声称"保证被过滤的行在两侧都
   不可用"以免改变排名分母，需测试证明。
5. 该改动方向上属于**放松 fail-closed**（哪怕理由充分）。按项目 AGENTS.md §6.1，
   这类放松需要明确的决策记录，不能只靠 diff 说明。

## 6. Invariants（不论 §5 怎么定都不能改）

1. **绝不从 qfq 反推 raw**，也绝不在 execution 侧退回 qfq。这条与 §5 的裁决选择无关。
2. **"不可用"不得静默消失**：任何被排除的 `(symbol, date)` 必须留下可复现的账
   （前后行数 + 原因码 + 样例 + 按日分布）。既不能一声不响地丢，也不能一声不响地留。
3. **不得为了让 freeze 通过而**：偷换 universe、修改 Alpha 定义、把真实数据缺失改判成
   停牌、把 fail-closed 改成 fail-open。
4. **绝对收益只来自 raw execution**；feature 侧不贡献任何收益口径。
5. **跨面板分歧不是停牌**：两份面板对"这只票这天有没有交易"必须给同一个答案。
6. 主判据必须是**集合关系**；量级/比例只能作为兜底层，不得成为唯一判据。
7. 生产入口不得新增价格口径逃逸开关；研究出口的 `research_replay_reason` 必须非空。
8. 修复执行可用性/对齐问题时，**不得顺手修改 Alpha 信号逻辑**。

## 7. Rejected / Avoided Approaches

| 做法 | 状态 | 证据 |
| --- | --- | --- |
| 单库同时承担 feature 与 execution 角色 | 已否决（`be83f41`），仅保留 `--rehearsal` 下自曝式兼容 | P0 契约文档 §1；生产正式库 `price_series_mode=qfq` 导致训练目标失真 |
| `price_mode_certified=false` 只 warning 继续跑 | 已否决 | `be83f41` commit message；P0 文档明确记为原缺陷 |
| "缺 bar"一律 fail closed（HEAD 现状） | 与 PIT 设计冲突，见 §5.2 | `pit_universe` 的 `expected_active_lookback_days=5` |
| "缺 bar"一律当停牌过滤 | 已否决方向 | 会把断供/截断/换库伪装成样本减少（§5.3 三类缺陷码即为此保留） |
| 用"全窗口行数差不多"当对齐证据 | 已否决 | 代码注释：差的那几条恰好是停牌/退市/次新时，恰恰最需要逐键对账 |
| 把 `price_series_mode` 塞进内容指纹做对账 | 已否决 | `IDENTITY_CONTENT_KEYS` 与口径检查分离，注释说明理由 |
| `pit_universe` 里让上下游各自猜列名 | 已否决 | `trade_date` → `date` 显式改名，注释记录 2026-09-18 静默空 stats 误判 `future_listed` |

## 8. Failure Lessons

1. **复权价当成交价**（2026-09-21 修）
   表现：除权日 -50% 跳变被写成真实亏损，涨跌停判定与可成交性全部失真。
   根因：单一 `--market-db`，且生产库是 qfq。
   为什么之前没挡住：认证失败只 warning，路径可以跑到冻结完成。
   现在：`require_certified_execution_series` 在构造特征矩阵之前硬失败 + `build_label_v2`
   默认强制 + mature 门 0 + KPI 逐日/逐行计数 + preflight 三项检查。

2. **文档承诺的退出码没有真的兑现**（2026-09-23 判定，修复在**未提交**工作树中）
   表现：`scripts/alpha_v2_shadow_model_freeze.py` 对 panel/cert 的调用都捕获了
   `PriceSeriesContractError` 并 exit 4，唯独 `build_dual_price_training_frame` 那处**没有**
   捕获 → 契约违例以未捕获 traceback 形式变成解释器 exit 1。
   影响面：文档、上游判据、验收脚本里的 exit 码全部与实态脱节。
   为什么没挡住：文档与代码分头演进，退出码只在"成功路径"和"更早的那道门"上被验证过。
   **HEAD 当前仍处于 exit 1 状态**，修复未提交。

3. **PIT 候选集与可交易集被混为一谈**（开放中）
   表现：§5.2 的冲突；零容忍口径下生产窗口无法冻结。
   根因：`pit_universe` 的活跃性回看窗口（5 个交易日）与"当天必须有 bar"不是同一件事。
   为什么之前没挡住：候选集从未被要求与 execution 面板逐键对账，直到 freeze 真的跑生产窗口。
   现状：见 §5.3 / §5.4，未定稿。

## 9. Implementation Locations

```text
src/stock_analyzer/backtest/price_contract.py                       # "Feature may be QFQ / Execution must be RAW" 的原始契约
src/stock_analyzer/alpha_v2/dual_price_series.py                    # 角色常量、认证要求、身份对账键、对齐守卫（850 行）
src/stock_analyzer/alpha_v2/validation/dual_price_freeze.py         # build_dual_price_training_frame 四步编排（240 行）
src/stock_analyzer/alpha_v2/research/panel.py                       # DailyPanel / pit_universe / certify_price_mode / PriceModeCertification
src/stock_analyzer/alpha_v2/research/outcomes.py                    # build_label_v2、T+1 可成交性、no_fill 语义
src/stock_analyzer/alpha_v2/validation/outcome_maturation.py        # mature 门 0（计算/写入之前）
src/stock_analyzer/alpha_v2/validation/validation_kpis.py           # 逐日/逐行价格口径证据计数
src/stock_analyzer/alpha_v2/validation/preflight.py                 # check_feature_price_series / check_execution_price_series / check_execution_data_fingerprint
src/stock_analyzer/alpha_v2/validation/freeze_precheck.py           # assert_execution_price_raw（exit 4）
src/stock_analyzer/alpha_v2/validation/frozen_model.py              # feature_price_mode / execution_price_mode 封存进 artifact_hash
src/stock_analyzer/alpha_v2/validation/live_data_health_inputs.py   # expected_active_lookback_days 在健康输入里的用法
scripts/alpha_v2_shadow_model_freeze.py                             # 冻结入口，exit 4 语义
scripts/alpha_v2_shadow_mature.py                                   # 成熟入口，exit 4 / 11
docs/alpha_v2/P0_Dual_Price_Series_Contract.md                       # 契约正文（注意工作树版本含 §5.3 提案，HEAD 版本不含）
```

> 勘误：`validation/dual_price_series.py` **不存在**；`dual_price_series.py` 在
> `alpha_v2/` 根下。项目 `AGENTS.md` 原先写错，已更正。

## 10. Evidence

- 代码：上节全部路径（HEAD 版本）
- 测试：`tests/test_alpha_v2_dual_price_series.py`（含 DP-17 断言零容忍语义）、
  `tests/test_alpha_v2_m3_freeze.py`（freeze 侧覆盖）、
  结构测试 `test_production_entrypoints_have_no_escape_hatch`
- 文档：`docs/alpha_v2/P0_Dual_Price_Series_Contract.md`（HEAD）、
  `docs/alpha_v2/RAW_Execution_Delta_Production_Wiring.md`
- commit：`be83f41` (2026-09-21) fix(alpha-v2): enforce dual price series contract
  (feature qfq / execution raw)、`5d8ba2b` (docs) P0 契约与 RAW delta 设计、
  `deb08c5` feat: record feature-side price mode evidence at bootstrap、
  `b2caa4c` feat: add production RAW execution delta pipeline、
  `378abc9` (2026-09-22) fix: require dual-delta readiness for live epoch
- 注：`dual_price_freeze.py` 没有同名专属测试文件，覆盖分散在上述两个测试文件里。

## 11. As-of

- HEAD：`42caaca`（分支 `feat/alpha-v2-raw-execution-delta-r1`）
- 最后对照代码核实：2026-09-23
- **§4 与 §7/§8.1 描述的是已提交实现；§5.3、§8.2 的修复、§8.3 的方案均处于
  工作树未提交状态，本次整理未运行任何测试去验证它们。**
