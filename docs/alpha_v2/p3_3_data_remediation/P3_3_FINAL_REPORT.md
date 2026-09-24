# P3.3 —— 数据整改与 freeze 守卫加固：最终报告

> As-of 2026-09-24。分支 `feat/alpha-v2-p3-3-data-remediation`（基线 `origin/main` = `75a8898`）。
> 机器可读证据：`P3_3_REPAIR_MANIFEST.json` / `P3_3_FEATURE_GAP_ROOT_CAUSE.json` /
> `P3_3_FREEZE_GUARD_VALIDATION.json`。
> 状态见 §4：**`P3_FREEZE_PREPARATION = BLOCKED`**。

---

## 0. 本轮实际做到的层级

```text
代码完成  ✅（守卫 + 修复层 + 测试，静态检查与定向套件全绿）
测试完成  ✅（定向 54 例 + test_alpha_v2_*.py 714 passed / 1 skipped）
数据修复  ⛔ 计划已生成、可复现、可回滚，但**未写入任何库**（用户决定：本地副本验证，之后再请示）
Freeze Ready  ⛔ 未达成
已部署生产  ⛔ 未 push、未 PR、未部署
```

**未产出** `P3_3_DAILY_COVERAGE.csv`：它是"修复后重跑历史审计"的产物，而修复未落库、
§17 全窗口 dry-run 与 §18 内存实测按用户决定推后。补一份假的等于伪造验收，
所以这里如实缺件。

---

## 1. 逐条回答任务书 §22

| # | 问题 | 回答 |
| --- | --- | --- |
| 1 | 2025 六个 vendor gap 是否全部修复 | **未修复**。6 个日期全部定位、缺口全部可补：计划 **1,081 行**（22/30/18/724/271/16），`skipped_already_present = 0`。未写库。 |
| 2 | 修复用什么 source | A 类（vendor 缺行）= Tushare `daily` + `daily_basic`；B 类（2026-07 feature 侧）**不需要外部来源**，用已存在的 RAW × vendor 复权因子派生。 |
| 3 | 是否改了原始 ZIP | **没有**。`2025.zip` 未被写入、未被移动，它继续作为"上游确实少交付"的原始证据。 |
| 4 | repair provenance 是否可重建 | 可。每条补写行在 `daily_bar_repairs` 登记 `(batch_id, symbol, trade_date, mode)` + `repair_source / source_file / source_query_time / source_row_hash / stored_row_hash / vendor_original_missing / repair_reason / verified_by / schema_version / created_at`；`verify_repairs()` 逐条回读比对哈希，`revert_repairs()` 按批次**只删登记过的主键**。 |
| 5 | 2026-07 feature 静默丢行根因 | `vendor_zip_overlay.py:1042-1065`：qfq 模式下因子加载失败 ⇒ 该票**整只被跳过**（raw 模式走无害分支），且增量导入只追加最新日期 ⇒ 缺的日期永不回填。`import_vendor_zip_to_delta.py:549` 只 `logger.warning` 并把 `ok` 留在 true。仓库还把这条写成"设计内"（`nightly_readiness.py:62`、`RAW_Execution_Delta_Production_Wiring.md:270`）。 |
| 6 | execution-present / feature-missing 是否彻底消除 | **代码侧已封堵，数据侧仍有 295 键未修**（27 票 × 11 session，含 000001/600000）。两道 fail-closed 门已上：逐键 `FEATURE_BAR_MISSING_FOR_EXECUTABLE_DECISION`、帧构造处 `FEATURE_ROW_MISSING` + `silent_drop == 0`。当前生产数据上 freeze 会**如实拒绝**，这是预期收紧而不是回归。 |
| 7 | 50% guard 是否删除 | 撤销为 **0.10**，并实测记录一条层级事实：`filtered_ratio > 10%` 蕴含 `breadth < 0.90`，所以广度门开着时这条闸**永远不会是第一个报的**；它只剩"小夹具关掉广度门"时的兜底价值（DP-24 因此显式关掉广度门来单测它）。没有直接删掉，是因为删了就没人记得它为什么存在。 |
| 8 | 最终 freeze guards | 见 `P3_3_FREEZE_GUARD_VALIDATION.json`：A1/A2/A3（跨面板双向 + merge 网）、B（0.10 量级报警）、C（连号段 ≥8 fail / ≥4 audit，**只看 interior 桶**）、D1/D2（日截面广度）、形状分桶、守恒账。`Guard D3`（修复后历史上的动态 breadth 标定）**未实现**，是缺口不是完成项。 |
| 9 | 北交所迁移是否误报 | **不误报**，但不是靠"再调一个阈值"：形状分桶把"此后不再出现 bar"（退市 / 换号）从"中间空洞"里摘出去，结构闸只看后者。DP-23 用真实形状（40 只连号旧码换号、当天总截面不变）钉住这条，并**显式断言反事实**：这批票号的连号段是 40 ≥ 8，若形状判据失效就会被误杀。 |
| 10 | P3 full-window dry-run 是否真正 PASS | **未跑**（用户决定推后）。不得声称。 |
| 11 | 最终 full training row count | **未重测**。P3.2 口径的候选 `1,650,654` 与过滤 `6,602` 仍是修复前数字。 |
| 12 | 合理的 freeze 内存建议 | 维持 **48 GB = 最小可行候选、64 GB = 优先**，且**不得**再用 13.4 GiB 那个数：它按 anon Δ 拟合，而实测 `--memory 6g` 下 128,751 行已经 OOMKilled 137（cgroup peak > 6 GB）。按 cgroup peak 两点外推 ≈ 21.6 KB/行 → 164 万行 ≈ **38 GB**，与 anon 口径差 2.8 倍。本轮未做 §18 的 100k/250k/500k 三点实测（推后），所以这只是**区间**不是结论。 |
| 13 | 是否可以 push / PR | **代码可以**（4 个提交、边界清楚、测试与静态检查齐）。但**不建议**在合入后立即对生产跑 freeze：新守卫会在 295 个键上 fail closed。 |
| 14 | 是否具备 Model Freeze 条件 | **不具备**。缺：数据修复落库、§17 全窗口 dry-run、§18 实测、Guard D3 标定。 |

---

## 2. 与任务书前提不符的地方（不掩盖）

1. **§2.3 的"2025-10-09/10/13 是代码迁移造成的高 filtered ratio"成立，但它们不是缺口日**：
   P3.2 已实测那三天 vendor 与 Tushare 逐只全等（`hole_both ≈ 0`）。本轮再次确认
   `execution_has_bar_and_feature_missing = 0`（全窗口 465 天），即那三天连单边不对称都没有。
2. **六个 gap 日期的缺行数与 P3.2 不完全相同**：本轮口径是"该日 Tushare 有、delta 没有"
   （22/30/18/724/271/16 = 1,081），P3.2 用的是"前后交易日都有、当天没有"的 sandwich 口径
   （23/29/17/724/271/17）。**这是定义差异不是数据矛盾**；修复计划取前者，
   因为它正好等于"允许补写的键集"，且不依赖邻居日是否也受损（11-12 的邻居 11-11 本身是缺陷日，
   sandwich 在这种日上会低估）。
3. **§2.4 说 07-31 恢复**：实测 07-31 仍有 **25** 个键缺（不是 0）。
   形状是"07-17 起缺、07-31 大部分回来、25 条留在库里"。
4. **112,503 个 feature-missing 键不等于缺陷**：绝大多数是 QFQ 侧覆盖起点
   （2024-11-15 单日 5,055 只，2024-12-17 才与 RAW 对齐）。守卫按**决策键**判，
   不按面板全量行判 —— 若改成后者，任何窗口起点落在 warmup 段的真实 freeze 都会被误杀。
   这条是本轮实测出来的、写下来免得下一个人重推。
5. **Tushare 的 `daily` 不提供 `up_limit` / `down_limit`**，但两个 delta 库里这两列
   **本来就 100% NULL**（涨跌停由 `limit_rule.build_price_limits` 派生），
   所以不构成保真度损失。`is_st / is_delisting_risk / suspended` 全库恒 False、
   `name` 恒空、`pre_close` 列在 delta 里根本不存在 —— 补写行按同一取值才是忠实的。

---

## 3. 语义验证结果（任务书 §6 要求的那四个量）

对照日 2025-11-14 / 11-19 / 12-23 / 12-25，每日 5,438 只逐只 JOIN，
`db_only = 0`、`ts_only = 0`、`trade_date_agrees = 1.0`：

```text
column_mapping / unit_mapping / exact_match_ratio / tolerance
open, high, low, close  = tushare.open/high/low/close            因子 1.0    exact 1.000000   max_rel 0.0
volume                  = tushare.vol         × 100               因子 100.0  dominant_share 1.0  tol(1e-6) 1.0
turnover                = tushare.amount      × 1000              因子 1000.0 dominant_share 1.0  tol(1e-6) 1.0
float_market_cap        = tushare.daily_basic.circ_mv × 10000     因子 10000.0 dominant_share 1.0（10,887 行对照）
```

单位换算常数**每个都只有一个取值**（无长尾、无并列值），因此
`DATA_REPAIR = COMPATIBLE`，不是 `BLOCKED_BY_SOURCE_INCOMPATIBILITY`。
B 类另有一条独立不变量：`qfq == raw × factor` 在 891/891 条对照行上逐值成立
（最大相对差 5.4e-11，纯浮点噪声）。

⚠️ 一个必须一起读的副作用：`compute_training_data_fingerprint` 按
`PANEL_BAR_COLUMNS` 逐行哈希，**新增行本身就会改变指纹**。这是正确行为
（训练输入真的变了），但意味着修复前封存的任何工件身份都会失配 —— 修完必须重冻，
且不得回头去改指纹来"对上"。

---

## 4. 状态判定（任务书 §21）

```text
P3_1_CONTRACT_FIX        = PASS
P3_2_DATA_COVERAGE       = BLOCKED        （缺口全部可补、计划已生成，但未落库）
P3_3_FREEZE_GUARDS       = PARTIAL        （A/B/C/形状/守恒已实现并验；Guard D3 未实现）
P3_FREEZE_PREPARATION    = BLOCKED
```

blocker 清单（按依赖顺序）：

1. **repair 未应用**：1,081 RAW + 295 QFQ 行停在 manifest 里。需要一次授权的生产写操作。
2. **上游静默跳过未修**：`vendor_zip_overlay.py:1042-1065` 的 qfq-only skip 仍然
   `warning + ok=true`。不修它，下一次因子抖动还会造出新的 295 键。
3. **§17 全窗口 dry-run 未跑**（推后）：`feature_missing_structural / panel_asymmetry /
   unknown_drop / accounting_status` 四项在真实窗口上的取值没有证据。
4. **§18 内存实测未做**（推后）：38–48 GB 只是两点外推的区间。
5. Guard D3 需在**修复后**的历史上重标 breadth 分位数。
6. `security_identity_mapping` 仍 0 行，换号目前靠形状识别；有官方映射后应改为查表。

---

## 5. 只读边界声明

本轮 NAS 侧全部为只读：`duckdb(read_only=True)` / `ATTACH ... (READ_ONLY)`，
引擎限 `memory_limit=700MB` + `threads=2`（api 容器 4 GiB 上限内，另有
`temp_directory=/tmp` 允许溢写）。Tushare 共 14 次调用（4 对照 + 6 缺口 + 2 `daily_basic` 对照 +
2 次容错重试），凭据只从容器环境变量读、未写入任何文件或日志。
未 `docker cp` 进 `/app/src`（修复模块经 `/tmp` 以 importlib 加载 —— 2026-09-08
那次容器污染就是这么发生的）。未重启容器、未改 marker / artifact / 原始包。
