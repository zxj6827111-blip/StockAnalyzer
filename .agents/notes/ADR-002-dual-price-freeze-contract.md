# ADR-002 双价格冻结契约（QFQ / RAW）

Status: Draft

As-of: 2026-09-23 @ HEAD `f2596ce`（详见 §11）

## 1. Status

**Draft，且是有意保持 Draft。**

原因不是证据不足，而是本契约当前处于**两段不同成熟度**的拼接状态，而仓库还没有对第二段
做出终局裁决：

```text
第一段：价格角色契约（feature 可 qfq / execution 必须 raw+certified）
        → 证据充分、已提交、有专属测试、有事故支撑。这部分本身可视为 Accepted。

第二段：决策集裁决口径（(symbol, decision_date) 在 execution 面板没有当日 bar 时
        该 fail closed 还是该过滤）
        → 已经走过两次落地：be2e4ef（P3.1，过滤式裁决）与本轮 P3.1.1
          （加日截面健康门 + 把 FILTER 语义降级为"未证明停牌"）。
          第二段**方向已定、判据仍有待定项**：两条比例闸的阈值重设、
          以及 NAS 侧 vendor_delta 2025-11-17 覆盖缺口是否已被修，都还没结论。
```

第二段终局（阈值按多窗口分布重设 + NAS 窗口实测通过）后，本 ADR 升为 Accepted。

**本轮已核实的一条事实（写死，避免下一个人重新找一遍）：**

> 本仓库**不存在**可直接用于 Alpha V2 freeze 的、独立且 PIT-safe 的停牌真值源。
> 因此"feature 与 execution 两侧同时缺 bar"**不构成**停牌证明——两份面板共享同一条
> 上游链路，同一个缺陷会同时命中两侧，该观测对"不可交易"与"对称断供"给不出不同答案。

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
只是候选名单），而 `expected_active_lookback_days=5` 的语义是"**最近 5 个自然日内**
（≈3 个交易日）有 bar 即算活跃"——`asof_universe.build_pit_stats:158` 用的是
`pd.Timedelta(days=5)`，不是交易日；`history_window_days`（`:259-265`）也显式按 1.6 倍
换算成自然日。因此**"当天拿不到 bar 但 5 个自然日内还在交易"的票按设计就在候选池里**——
它不是脏数据，是 PIT 语义的直接产物。这条是 §5 争议的根源。

⚠️ 但**反过来不成立**：候选池里有"当天无 bar"的票，推不出"当天无 bar 的票就是停牌"。
`pit_universe` 已经算出了本仓库唯一一层 PIT-safe 的相对分类
（`expected_active_symbols` vs `known_suspended_symbols`，后者 = eligible 但 lookback 内
0 根 bar），它的定义就写着"停牌/停更，无法区分"——即仓库自己承认这个观测**二义**。
freeze 入口目前只取两者之和 `eligible_symbols`，把这层分类丢掉了（见 §5.5）。

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

## 5. 第二段：决策集裁决口径（方向已定，判据待定稿）

### 5.1 历史实现（零容忍，`be83f41`..`42caaca`，已被 `be2e4ef` 取代）

`assert_decisions_aligned()`：缺一条 `(symbol, date)` 即抛
`PriceSeriesContractError` → 文档称 CLI exit 4（实际未捕获、退 1，见 §8.2）。
当时生产调用点**只有一个**（`validation/dual_price_freeze.py` 第 2 步）。
文档表述是"缺失即 target 不可用，绝不允许退回 qfq 或从 qfq 反推 raw"。
该函数**仍在**（语义未放松，DP-17 钉住），但见 §5.4-Q1：它现在只有测试调用方。

### 5.2 零容忍与 §3 设计意图的冲突（仍成立，是裁决动因）

按 §3，PIT 候选池**按设计**含"当天拿不到 bar"的票。零容忍口径下生产窗口无法冻结
（实测 6,602/1,650,654 被拦）——这是**已提交实现与设计意图之间的真实张力**，
不是数据问题。

### 5.3 P3.1：已由 `be2e4ef` 提交，尚未完成契约验收

> 状态：已提交（2026-09-23 13:20），**不是**工作区提案。涉及
> `src/stock_analyzer/alpha_v2/dual_price_series.py`、
> `src/stock_analyzer/alpha_v2/validation/dual_price_freeze.py`、
> `scripts/alpha_v2_shadow_model_freeze.py`、
> `tests/test_alpha_v2_dual_price_series.py`、`docs/alpha_v2/P0_Dual_Price_Series_Contract.md`、
> `docs/alpha_v2/PROGRESS.md` §23。

把"缺 bar"分成两类，判据是**可复现的集合关系**而非比例直觉：

| `(symbol, decision_date)` 的观测 | 裁决 | 原因码 |
| --- | --- | --- |
| 在 execution 面板里 | 保留 | — |
| 票在、日在，仅当天无 bar | 过滤并写审计 | `NO_EXECUTION_BAR_ON_DECISION_DATE` |
| 票在整个 execution 面板都不存在 | fail closed | `SYMBOL_NOT_IN_EXECUTION_PANEL` |
| 该日期在 execution 面板里不是交易日 | fail closed | `DECISION_DATE_NOT_A_SESSION_IN_EXECUTION_PANEL` |
| feature 面板当天**有** bar 而 execution 没有 | fail closed | `FEATURE_PANEL_HAS_BAR_ON_DECISION_DATE` |

后三类的理由值得单独记住：**它们都不是"停牌"，而是两份面板对同一份事实给出了不同答案**。
静默过滤会把真实的断供 / 截断 / 换库伪装成"少了几行训练样本"。
**当前代码里的两条比例闸是：总体 2% / 单日 50%，各带 50 行下限**
（`DEFAULT_MAX_FILTERED_RATIO=0.02` / `DEFAULT_MAX_DAILY_FILTERED_RATIO=0.50` /
`DEFAULT_MAX_FILTERED_ROWS_FLOOR=50`）。单日闸原为 10%，`be2e4ef` 放宽到 50%——
§7 已把那次放宽记成走偏的决策。

生产窗口实测（NAS 只读一次性容器，module sha256 `83de9367…`）：
6,602 / 1,650,654 = 0.399963%，`status=PASS`，`filtered_dates=306`（**每个交易日都有过滤**），
最大单日 12.2512% @ 2025-11-17。⚠️ 这组数字是 **P3.1 口径**的；P3.1.1 加日级门后
同一窗口会在 2025-11-17 fail closed（见 §5.5）。

### 5.4 定稿前必须回答的问题——本轮逐条回答

1. **`assert_decisions_aligned` 的"研究/回放路径"定位**：❌ **仍不成立**。全仓检索确认
   它只有测试调用方（`tests/test_alpha_v2_dual_price_series.py` DP-17）。
   代码与 P0 文档已就地标注这个缺口。**处置仍未做**：补调用方或删定位描述。
2. **2% / 10% 阈值是否普适**：❌ **已用十年真实面板证伪 2%**。同一 PIT 语义下
   2016 年窗口 = **8,778/422,566 = 2.0773%**，超过 2% 闸会被 fail closed，而逐票取证
   证明那批是真实停牌（gap 2..180 session，100% 之后复牌）。详见 §5.6 分布表。
3. **`cross_check_panel=feature` 双向失效**：✅ **成立，且比原判断更强**——不是"若 feature
   也缺才会失效"，而是**原理性**的：决策集本身由 feature 面板生成
   （`scripts/alpha_v2_shadow_model_freeze.py:268-271`），所以任何同时命中两侧的缺陷
   必然两侧一致。P3.1.1 的日级广度门就是为这一类补的（它不看 decision 集合）。
4. **被过滤行是否改变质量池/基准分母**：✅ DP-16 钉住"逐值相同"，本轮又把
   `filtered_unavailable_rows` + 原因码 + 语义标注显式写进 `decision_accounting`。
5. **方向上属于放松 fail-closed**：⚠️ `be2e4ef` 确实是放松。P3.1.1 的处置是
   **一边收紧判据（新增日级门，把对称截断重新变回 fail closed）、一边把比例闸明确
   降级为非定义性**，不是在原方向上继续放宽。

### 5.5 P3.1.1：日截面健康门 + FILTER 语义降级（本轮，工作区未提交）

> 涉及 `dual_price_series.py`（`assess_decision_session_health` /
> `_session_bar_counts` / 两个新原因码 / 4 个新默认常量）、
> `validation/dual_price_freeze.py`（`decision_accounting` 扩字段）、
> `scripts/alpha_v2_shadow_model_freeze.py`（打印 session health + 去掉停牌断言）、
> `tests/test_alpha_v2_dual_price_series.py`（DP-15b 改写、DP-15c/15e/DP-19 新增、
> DP-16 扩断言）、`docs/alpha_v2/P0_Dual_Price_Series_Contract.md` §2.1/§2.2。

**为什么必须有这一层**：§5.4-Q3 的结论是逐键判据对"两侧对称缺失"原理性失明，
而仓库又没有独立停牌真值源（§1）。剩下的选择只有两个：要么承认检不出来，
要么找一个**不依赖 decision 集合**的证据。日截面广度属于后者——
"这一天面板自己还剩多少根 bar"既不需要停牌日历，也不需要第三份数据源。

裁决顺序（**日级门先于逐键 FILTER**）：

| 层 | 判据 | 裁决 | 原因码 |
| --- | --- | --- | --- |
| 日级 | 当日 bar 数 / 面板自身前 ≤20 session 中位数 < **0.90** | fail closed | `EXECUTION_SESSION_BREADTH_COLLAPSE` |
| 日级 | execution 同日 bar 数 / feature 同日 < **0.90**（方向单边） | fail closed | `EXECUTION_SESSION_BREADTH_BELOW_FEATURE` |
| 逐键 / 比例 | 同 §5.3 | — | 同 §5.3 |

**两类情形不判**，且都如实记进 `session_health.unjudgeable_dates` /
`unjudgeable_examples`（"不判"绝不能伪装成"通过"）：

1. **面板的第一个 session** —— 没有前序 session 就没有"塌陷"可言。
   ⚠️ 不得用"其余 session 中位数"兜底：截面本身随年代增长，兜底会误杀首日
   （实测复现，见 §5.6 表下教训）。
2. **基线截面 `< 100` 行** —— 几只票的夹具里停一只就掉 17%，比例在这个尺度上无意义。
   生产窗口每日 4,700–5,500 只，不受此豁免影响。

**FILTER 语义已降级**：审计里新增
`filter_reason_semantics = "execution_observation_unavailable_NOT_PROVEN_SUSPENDED"`，
所有把两侧同缺称作"合法停牌"的代码注释/文档/CLI 输出改成"当日无 execution bar"。
原因码字符串**未改**（保持工件身份与下游取值稳定）。

**能发现什么 / 仍然发现不了什么**：见 §6.1。

**本轮明确没做、但定稿前必须处理的**：

1. 阈值重设：2% 总闸已知会误杀（2016 = 2.0773%）。候选做法是按 §5.6 分布改成
   "分窗口自适应"或降级为纯 advisory —— **需用户拍板**，本轮只标注不定值。
2. 让 FILTER 消费 `pit_universe` 已经算好的 `expected_active` / `known_suspended`
   分桶（§3 末尾）。实测干净窗口 active 单日最大 43 条 vs 缺陷日 634–724 条，
   分离度约 15 倍，比"过滤总量占比"更有意义，且零新数据依赖。
3. NAS 侧 vendor_delta 2025-11-17 的 724 票单日洞：**至今未修**。
   本轮之后它会被日级门 fail closed，即 P3 freeze 在该窗口仍过不了 —— 这是
   预期的收紧，但必须先有人去修链路。
4. `alpha_v2_research_run.py` / `alpha_v2_m4h_run.py` 两个入口的口径一致性（§9 已列出）。
5. sandwich 判据（过滤键必须有前向 bar）：实测在 8,778 条合法停牌上**零误报**，
   但这份库无退市票，上线前需配退市豁免 —— 因此本轮**未实现**，只记录证据。

### 5.6 跨窗口实测分布（本地真实十年面板，只读）

数据源：`artifacts/warehouse/market.duckdb`（9,881,442 行 / 5,198 票 / 2,489 交易日 /
2016-01-04..2026-04-03，与 M4H 盘点逐项一致）。按仓库自身 PIT 语义重放，**不是夹具**。

| 窗口 | 候选 | 过滤 | 总占比 | 单日占比 P50/P90/P95/P99/max | 单日条数 P50/P90/P99/max | 2% 闸会否误杀 |
| --- | --- | --- | --- | --- | --- | --- |
| 2016 | 422,566 | 8,778 | **2.0773%** | 1.85 / 2.73 / 2.93 / 3.57 / **3.83%** | 43/62/78/85 | **会**（8778 > 8452） |
| 2019 | 825,770 | 1,916 | 0.2320% | 0.20/0.36/0.48/0.72/0.83% | 7/12/24/28 | 否 |
| 2022 | 1,091,509 | 1,348 | 0.1235% | 0.11/0.19/0.22/0.32/0.68% | 5/8/14/30 | 否 |
| 2024 | 1,217,379 | 981 | 0.0806% | 0.06/0.20/0.26/0.30/0.52% | 3/10/15/26 | 否 |
| 2025 | 1,242,158 | 2,212 | 0.1781% | 0.18/0.25/0.27/0.35/0.84% | 9/13/18/43 | 否 |
| 2025-06-02..2026-04-03（含截断尾） | 1,053,429 | 12,441 | 1.1810% | 0.17/0.25/0.29/**7.79**/**99.19%** | 9/13/403/**5130** | 否 |
| NAS 生产窗口（`be2e4ef` 报，本轮**未复现**） | 1,650,654 | 6,602 | 0.399963% | — | — | — |

日截面广度分布（**新闸所依据的那一个统计量**）：

```text
count / median(前 ≤20 session)：p01=0.9923  p05=0.9996  median=1.0028
2,489 个 session 中：judged 2,488 / unjudgeable 1（面板首日）
低于 0.90 的：2 个 —— 2026-04-02 / 04-03（都是真实尾部截断）
低于 0.99 但高于 0.90 的：19 个（无一例是缺陷，也无一例被 0.90 误判）
```

→ 0.90 在十年真实数据上**零误报**，且对已知缺陷形态（NAS 上报 −13.3%）有 4.3 个百分点
余量。这是本契约里**唯一有跨窗口实测支撑**的阈值；2% 与 50% 都没有。

**门本身是用真实数据验出来的，不是只跑夹具**（脚本
`%TEMP%\sa_guard_verify.py`，只读）：

| 验证 | 结果 |
| --- | --- |
| `_session_bar_counts(panel)` vs 裸 SQL `GROUP BY` | 窗口内 59 个 session 逐日相等（唯一"多出"的是 warmup 带进的 2025-12-31，属预期） |
| 真实十年全序列喂进 `assess_decision_session_health` | 只报 2026-04-02 / 04-03；2016 年 8,778 条合法"当日无 bar"**零误报** |
| NAS 上报的 2025-11-17 形态（5,438 → 4,713 = 0.8667） | **被抓到**（`EXECUTION_SESSION_BREADTH_COLLAPSE`） |

> ⚠️ 这轮验证**当场抓出一个会误杀真实 freeze 的实现缺陷**：首版实现给面板**第一个
> session** 用"其余 session 中位数"兜底，而十年截面本身在增长
> （2016-01-04 只有 2,364 只 vs 全期中位 3,982）→ 首日被判成 59.4% 塌陷。
> 后果是"窗口起点正好落在面板首日"的每一次真实 freeze 都会被误拒。
> 已修为**首日一律不判**并如实记 `unjudgeable_examples=["…(no_preceding_session)"]`，
> 由 DP-20 钉住。教训：**比例型完整性判据在无前序基线的边界上必须显式弃权**，
> 兜底基线等于换一个统计量，而那个统计量这里不成立。


## 6. Invariants（不论 §5 怎么定都不能改）

1. **绝不从 qfq 反推 raw**，也绝不在 execution 侧退回 qfq。这条与 §5 的裁决选择无关。
2. **"不可用"不得静默消失**：任何被排除的 `(symbol, date)` 必须留下可复现的账
   （前后行数 + 原因码 + 样例 + 按日分布）。既不能一声不响地丢，也不能一声不响地留。
3. **不得为了让 freeze 通过而**：偷换 universe、修改 Alpha 定义、把真实数据缺失改判成
   停牌、把 fail-closed 改成 fail-open。
4. **绝对收益只来自 raw execution**；feature 侧不贡献任何收益口径。
5. **跨面板分歧不是停牌**：两份面板对"这只票这天有没有交易"必须给同一个答案。
6. 主判据必须是**集合关系**；量级/比例只能作为兜底层，不得成为唯一判据。
   P3.1.1 把这条具体化：两条比例闸在代码与文档里都标为
   **provisional anomaly guard**，`limits.ratio_gates_are_suspension_definition = False`。
7. 生产入口不得新增价格口径逃逸开关；研究出口的 `research_replay_reason` 必须非空。
8. 修复执行可用性/对齐问题时，**不得顺手修改 Alpha 信号逻辑**。
9. **不得把"两侧同时缺 bar"表述成停牌证明。** 允许说的形式只有两种：
   "当日无 execution bar"（观测）与"未证明不可交易成因"（承认未知）。
   唯一豁免是 `enforce_session_health_guard=False` 这条**只为小夹具**存在的开关；
   生产入口不传，且它必须在审计里以 `session_health.enforced=false` 现形。
10. **不得把某一次观测到的异常值抬高出比例，当作长期契约阈值。**
    阈值必须落在"同一统计量在多个窗口上的健康分布"之上，并写出实测来源（§5.6）。
    §7 里 10%→50% 那次放宽就是这条的反例。
11. **日级判据必须先于逐键 FILTER 裁决**（`filter_decisions_by_execution_availability`
    内部顺序）。反过来做会让"整天截面塌陷"被拆成几千条逐票过滤，从契约上消失。

### 6.1 日截面健康门的能力边界（必须一起读，否则会被当成万能门）

**能发现**：面板级截断 / 换库 / 上游少写一整片 symbol（自身广度）；execution 侧系统性
少于 feature 侧（两侧广度）。十年真实面板上零误报（§5.6）。

**仍然发现不了——两类，都必须如实承认**：

1. **双面板同步发生的稀疏缺失，且日级截面仍正常**。例：某缺陷只打掉 60 只票 / 5,200
   （≈1.2%）当日的 bar，两侧一致。广度比 0.987 > 0.90 → 门放过；逐键两侧一致 → 分歧
   检查放过；比例 1.2% < 2% → 兜底闸也放过。**这一类在本轮之后依然无解**，
   唯一的真解是独立停牌真值源（§6.2）。
2. **两侧同步地"整段时间"缺失**（比如共同的窗口起点/终点被截）。日级看每一天都健康，
   逐键看两侧一致，没有任何一层会报。当前只由 `preflight.check_market_db` 的
   `tail_fragment`（只看最新一天、阈值 0.5）与 `--source-window` 覆盖检查间接兜住。

### 6.2 独立停牌真值源现状（本轮逐项核实，避免重复调查）

| 候选 | 位置 | 实测状态 | 可用性 |
| --- | --- | --- | --- |
| `daily_trade_status` | `data/market_warehouse.py:355-372` | 154 行 / **2 只票** / 77 日期 / `sum(suspended)=0`；写入端 `apply_trade_status_to_daily`（`:764` 的 `if ts in daily.index`）**结构上丢弃无 bar 日的标记**；`as_of` 被写成 trade_date、tushare `ann_date` 被丢弃 → 非 PIT | ❌ 粒度对，内容空，且未接 alpha_v2 |
| `security_status` | `market_warehouse.py:502-520` | 区间粒度 + 不重叠校验齐备；**0 行、0 生产方**（`upsert_/fetch_` 只有测试调用） | ❌ 空容器 |
| `daily_bars.suspended` | `:209`，alpha_v2 **确实读**（`panel.py:57,536`→`outcomes.py:409`） | 9,881,442 行里 true = **0**；8 个写入点全部硬编码 False（`vendor_zip_overlay.py:1249` 等） | ❌ 恒假 |
| 交易所日历 | 无持久化表；tushare `trade_cal`（`tushare_provider.py:832-875`）；`data/trading_calendar.py:47-55` 只判工作日 | alpha_v2 的 `calendar` = `DISTINCT date FROM daily_bars`（`panel.py:486`）→ **自证循环** | ❌ |
| symbol master | `delisted_symbols.py` + `artifacts/universe/delisted.json`（`stock_basic(list_status='D')`） | 只有 `delist_date`，**`list_date` 全仓零命中**；per-symbol 静态；解析失败返回 `{}`（fail-open）；未接 alpha_v2 | ❌ |
| `known_suspended_symbols` | `data/asof_universe.py:214-221` | PIT-safe、已计算、逐决策日；但定义即"eligible 且 lookback 内 0 bar = 停牌/停更无法区分" | ⚠️ **不是真值源，但本轮唯一被丢弃的相对证据**（见 §5.5 后续动作） |

## 7. Rejected / Avoided Approaches

| 做法 | 状态 | 证据 |
| --- | --- | --- |
| 单库同时承担 feature 与 execution 角色 | 已否决（`be83f41`），仅保留 `--rehearsal` 下自曝式兼容 | P0 契约文档 §1；生产正式库 `price_series_mode=qfq` 导致训练目标失真 |
| `price_mode_certified=false` 只 warning 继续跑 | 已否决 | `be83f41` commit message；P0 文档明确记为原缺陷 |
| "缺 bar"一律 fail closed（`be83f41`..`42caaca` 的实现） | 与 PIT 设计冲突，见 §5.2；已被 `be2e4ef` 取代 | `pit_universe` 的 `expected_active_lookback_days=5` |
| "缺 bar"一律当停牌过滤 | 已否决方向 | 会把断供/截断/换库伪装成样本减少（§5.3 三类缺陷码即为此保留） |
| **"两侧同时无 bar" 当作停牌证明** | **已否决（P3.1.1）** | 决策集由 feature 面板生成 → 对称缺陷必然两侧一致，原理上检不出来；且全仓无独立停牌真值源（§6.2 逐项实测） |
| **单日闸 10% → 50%（`be2e4ef`）** | **判定为走偏，P3.1.1 用日级门替代其意图** | 放宽理由是"实测最大合法单日 12.25%"，但那 12.25% 是上游覆盖缺口（本地 warehouse 同日截面 5,155 票、相邻日 5,156/5,157，毫无异常）。十年实测最大**形态合法**单日过滤 = 3.834%，10% 从未误杀 |
| **用某一窗口观测值 × 倍数反推契约阈值** | 已否决（Invariant 10） | 2% 闸被 2016 年窗口 2.0773% 证伪（§5.6），而 2016 那批逐票取证 100% 之后复牌 |
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

2. **文档承诺的退出码没有真的兑现**（2026-09-23 判定，**已由 `be2e4ef` 修复**）
   表现：`scripts/alpha_v2_shadow_model_freeze.py` 对 panel/cert 的调用都捕获了
   `PriceSeriesContractError` 并 exit 4，唯独 `build_dual_price_training_frame` 那处**没有**
   捕获 → 契约违例以未捕获 traceback 形式变成解释器 exit 1。
   影响面：文档、上游判据、验收脚本里的 exit 码全部与实态脱节。
   为什么没挡住：文档与代码分头演进，退出码只在"成功路径"和"更早的那道门"上被验证过。
   现在：显式 `except PriceSeriesContractError` → `return exc.exit_code`（4），并由
   DP-18 钉住"exit 4 且无 traceback + 打印 alignment 报告"。P3.1.1 的日级门走同一个
   catch，因此新缺陷码也是真 exit 4。

3. **PIT 候选集与可交易集被混为一谈**（方向已定，判据待定稿）
   表现：§5.2 的冲突；零容忍口径下生产窗口无法冻结。
   根因：`pit_universe` 的活跃性回看窗口（**5 个自然日**，见 §3）与"当天必须有 bar"不是
   同一件事。
   为什么之前没挡住：候选集从未被要求与 execution 面板逐键对账，直到 freeze 真的跑生产窗口。
   现状：`be2e4ef`（过滤式）+ P3.1.1（日级门 + 语义降级）。待定稿项见 §5.5 与下方 4。

4. **用异常观测值反过来放宽检测闸**（2026-09-23，P3.1.1 纠正）
   表现：唯一一次真正报出异常的闸（单日 10%）被以"它报了 12.25%"为由放宽到 50%，
   于是那个缺陷从此在任何 freeze 里不可见，并被写进文档当作"实测最大合法值"。
   根因：**没有把"被闸报出"与"闸误报"区分开**——两者都表现为"闸拦住了生产窗口"，
   而解除阻塞的路径比修上游快得多。
   为什么危险：它长得像"用数据说话"（commit message 原话："阈值来自实测，不是拍脑袋"），
   实际是把缺陷样本量成了基线。
   现在：新增 Invariant 10；日级广度门（0.90）接管该缺陷检测职责，且在十年面板上零误报。

## 9. Implementation Locations

```text
src/stock_analyzer/backtest/price_contract.py                       # "Feature may be QFQ / Execution must be RAW" 的原始契约
src/stock_analyzer/alpha_v2/dual_price_series.py                    # 角色常量、认证要求、身份对账键、日截面健康门 + 可用性裁决（1112 行）
src/stock_analyzer/alpha_v2/validation/dual_price_freeze.py         # build_dual_price_training_frame 四步编排（249 行）
src/stock_analyzer/alpha_v2/research/panel.py                       # DailyPanel / pit_universe / certify_price_mode / PriceModeCertification
src/stock_analyzer/alpha_v2/research/outcomes.py                    # build_label_v2、T+1 可成交性、no_fill 语义
src/stock_analyzer/alpha_v2/validation/outcome_maturation.py        # mature 门 0（计算/写入之前）
src/stock_analyzer/alpha_v2/validation/validation_kpis.py           # 逐日/逐行价格口径证据计数
src/stock_analyzer/alpha_v2/validation/preflight.py                 # check_feature_price_series / check_execution_price_series / check_execution_data_fingerprint
src/stock_analyzer/alpha_v2/validation/freeze_precheck.py           # assert_execution_price_raw（exit 4）
src/stock_analyzer/alpha_v2/validation/frozen_model.py              # feature_price_mode / execution_price_mode 封存进 artifact_hash
src/stock_analyzer/alpha_v2/validation/live_data_health_inputs.py   # expected_active_lookback_days 在健康输入里的用法
src/stock_analyzer/data/asof_universe.py                            # build_pit_stats:132-176（:158 是 5 **自然日**）/ resolve_asof_universe:214-221（expected_active vs known_suspended）
src/stock_analyzer/data/market_warehouse.py                         # daily_trade_status:355-372 / security_status:502-520 / apply_trade_status_to_daily:754-783（:764 的 `if ts in daily.index` 会丢弃无 bar 日的停牌标记）
src/stock_analyzer/data/tushare_provider.py                         # suspend_d→suspended:1789-1874（:1822 三态正确、:1825/:1853 as_of=trade_date 非 PIT、丢弃 ann_date）
src/stock_analyzer/data/vendor_zip_overlay.py                       # :1249 `frame["suspended"] = False`（生产 vendor ZIP→delta 路径，恒假来源）
src/stock_analyzer/ops/intraday_freshness.py                        # :230-272 resolve_daily_trade_state（"前后都有 bar 才算 not_trading"的 sandwich 启发式；alpha_v2 未消费）
scripts/alpha_v2_raw_delta_coverage.py                              # :26-33 §8.5 另一处"两边都没有→不报"裁决，必须与本契约同步
scripts/alpha_v2_research_run.py:224 / scripts/alpha_v2_m4h_run.py:610  # 把 eligible_symbols 直喂 build_label_v2，**无**可用性过滤（口径不一致，待处置）
scripts/backfill_trade_status.py                                    # daily_trade_status 事后全量回填（非 PIT）
scripts/alpha_v2_shadow_model_freeze.py                             # 冻结入口，exit 4 语义；打印 alignment + session health
scripts/alpha_v2_shadow_mature.py                                   # 成熟入口，exit 4 / 11
docs/alpha_v2/P0_Dual_Price_Series_Contract.md                       # 契约正文（§2.1 三层裁决 / §2.2 证据强度与实测阈值）
```

> 勘误：`validation/dual_price_series.py` **不存在**；`dual_price_series.py` 在
> `alpha_v2/` 根下。项目 `AGENTS.md` 原先写错，已更正。

`filter_decisions_by_execution_availability` 内部顺序（P3.1.1 新增，读代码时容易漏）：

```text
dual_price_series.py
  _session_bar_counts                    # 面板逐 session bar 数（日级广度的原料）
  assess_decision_session_health         # 两条日级判据 → (payload, defects)
  filter_decisions_by_execution_availability
      └─ 先：session 缺陷 → PriceSeriesContractError（CLI exit 4）
      └─ 后：逐键集合关系 → 保留 / 缺陷 / 过滤
      └─ 末：两条 provisional 比例闸
```

## 10. Evidence

- 代码：上节全部路径（HEAD `f2596ce` + 本轮 P3.1.1 工作区改动）
- 测试：`tests/test_alpha_v2_dual_price_series.py` **39 例全绿**
  （DP-11..DP-20：前提 / 保留 / 过滤 / 三类逐键缺陷 / 日级广度塌陷 / unjudgeable 如实记账 /
  两侧广度比 / 比例闸 / 不变性 + 显式入账 / 零容忍版未放松 / CLI exit 4 /
  **DP-19 两侧对称截断，并反证"关掉日级门即复现旧漏洞"** /
  **DP-20 面板首日不得判塌陷**）
- 真实数据验证（非夹具）：`assess_decision_session_health` 直接吃真实十年逐日截面
  → 只报 2026-04-02/03、2016 年零误杀；NAS 上报的 2025-11-17 形态被抓到。
  `_session_bar_counts` 与裸 SQL `GROUP BY` 逐日一致。**这一轮验出一个会误杀真实
  freeze 的首-session 兜底缺陷，已修**（§5.6）。
- ⚠️ 勘误：`tests/test_alpha_v2_m3_freeze.py` **不含**任何 decision/alignment/eligible/
  pit_universe 断言（本轮 grep 确认）。本契约的覆盖只在
  `test_alpha_v2_dual_price_series.py` 一个文件里，不是"分散在两个文件"。
- 静态检查：`ruff check`（4 个改动文件）= 基线 2 条既有告警，0 新增；
  `mypy --follow-imports=silent` 对 `dual_price_series.py` = **10 = 基线 10**，0 新增
- 实测证据：§5.6 跨窗口分布取自本地真实库 `artifacts/warehouse/market.duckdb`
  （只读重放仓库自身 PIT 语义，非夹具）
- 文档：`docs/alpha_v2/P0_Dual_Price_Series_Contract.md`、
  `docs/alpha_v2/RAW_Execution_Delta_Production_Wiring.md`、
  `docs/alpha_v2/M4H_Historical_Data_Inventory.md:54,80` 与
  `M4H_Historical_Locked_OOS_Report.md:597`（仓库此前已把"无停牌日历"记成数据缺陷）
- commit：`be83f41` (2026-09-21) enforce dual price series contract、
  `5d8ba2b` (docs) P0 契约与 RAW delta 设计、`deb08c5` feature-side price mode evidence、
  `b2caa4c` production RAW execution delta pipeline、
  `378abc9` (2026-09-22) require dual-delta readiness for live epoch、
  **`be2e4ef` (2026-09-23) filter unavailable execution bars during dual price freeze**、
  `f2596ce` (2026-09-23) docs(agents) bootstrap decision knowledge

## 11. As-of

- HEAD：`f2596ce`（分支 `feat/alpha-v2-raw-execution-delta-r1`；其父 `be2e4ef` 即 P3.1）
- 最后对照代码核实：2026-09-23（P3.1.1 本轮）
- **§4 / §5.1 / §5.2 / §5.3 / §7 / §8.1 / §8.2 描述已提交实现；
  §5.5 / §5.6 / §6.1 / §6.2 / §8.3 / §8.4 中标注 P3.1.1 的部分处于工作区未提交状态。**
- 本轮实际运行：`pytest tests/test_alpha_v2_dual_price_series.py -q` → **39 passed**；
  ruff / mypy 与基线逐项对齐；两次十年真实面板只读统计与门验证。
  **未**运行 freeze / training / Production Preflight，**未**连 NAS，**未** commit / push。
