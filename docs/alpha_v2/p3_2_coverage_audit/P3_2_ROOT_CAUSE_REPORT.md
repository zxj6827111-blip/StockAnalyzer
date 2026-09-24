# P3.2 历史覆盖审计与 P3.1 Pre-Merge Review

> 状态：**审计完成（只读）**。未执行 Model Freeze / Epoch / Preflight / Promotion，未修改任何数据。
> 编译时间：2026-09-23 23:40 (+08:00)
> 审计范围：freeze source window `2024-11-14 .. 2026-08-31`（决策窗 `2025-06-02 .. 2026-08-31`，warmup 200 自然日）
> 机器可读证据：`P3_2_HISTORICAL_COVERAGE_AUDIT.json` / `P3_2_DAILY_COVERAGE.csv`（463 行逐日）/ `P3_2_ANOMALOUS_DATES.csv`

---

## 0. 结论摘要（先看这一段）

1. **2025-11-17 的 724 个缺失 bar 消失在最上游的 vendor 原始包**：`/data/vendor_history/全A日K/2025.zip` 内相应 symbol 的 CSV 里**没有 2025-11-17 这一行**。
   证据是**逐日全等**：2025 年**每一个**交易日（243 天），ZIP 的 `(symbol,date)` 集合与 qfq delta、raw delta 的集合**完全一致（0 差异）**。下载 / 解压 / ingest / dedup / upsert / 日期解析 / delta merge **全部无嫌疑**——它们忠实复制了 ZIP。
2. **这些 bar 在市场上是存在的**：独立来源 Tushare `daily(trade_date=20251117)` 当天返回 5,437 只，**724 只缺口票全部在其中**（`in_db_not_ts = 0`、`in_ts_not_db = 724`）。2025-11-18 同理（271/271）。
   → 定性：**UPSTREAM_SOURCE_GAP**（vendor 交付包缺行），**不是** INGESTION / DELTA_MERGE / PANEL_BUILD 缺陷。
3. **2025-11-18 与 11-17 同因**（271 只、max_run 23），但**magnitude 完全不同**；**2025-10-09 / 10-10 / 10-13 不是同型异常**——那三天 vendor 与 Tushare **逐只完全一致（0 差异）**，它们只是"过滤数偏高的合法日期"（见 §4，原因是北交所代码段迁移）。
4. 整个 source window 里**只有 2 天**属于"vendor 缺行"的大缺口（2025-11-17 / 11-18）；另有 4 个小缺口日（2025-11-11 / 11-12 / 11-13 / 2025-12-24，各 17–29 只，Tushare 侧存在）。
5. **结构性证据（新增，可替代比例阈值）**：把"缺失票号"按数字排序后取**最长连续段长度**，2008–2026 十年本地面板 2,429 个交易日 + 生产窗口 435 个交易日的合法上界都是 **3**；两个缺陷日的取值是 **23 / 57**。分离度 7 倍以上，且合法侧零误报。
6. **`be2e4ef` 的 contract fix 可以保留**（逐键三层裁决、过滤发生在 label/特征/风格之前、`assert_decisions_aligned` 未被放松）；但其中**单日比例闸 10% → 50% 的那次放宽没有依据**，因为 12.25% 本身就是被审计的异常值。
7. **工作区（未提交）已有一版外部并行改写**（P3.1.1：日截面健康门）。它修掉了 11-17，但**修不掉 11-18**（该日 breadth = 0.9517，0.90/0.95 都不触发），也**修不掉 2026-07 的 feature 侧单边缺口**。这两条是本报告提出的最小补丁（§7）。
8. 当前 P3 唯一真实 blocker：**vendor 交付包的历史缺行尚未修复/未被制度化为 fail closed**。数据没修、守卫没定稿，就不能进 Model Freeze。

---

## 1. 数据链路与"第一消失点"判据

链路（按 `RAW_Execution_Delta_Production_Wiring.md` §1）：

```text
vendor 原始包            全A日K/YYYY.zip（每票一个 CSV：YYYY/CODE.EX.csv）
   ↓ （本地只读校验，未改动）
daily_index.json         /app/artifacts/vendor_overlay/daily_index.json（5,818 票 / 27 个年包）
   ↓ import_vendor_zip_to_delta.py
RAW delta (execution)    vendor_delta_raw/market_delta_raw.duckdb   2,680,396 行 / 5,818 票
QFQ delta (feature)      vendor_delta/market_delta.duckdb           2,400,854 行 / 5,818 票
   ↓ load_daily_panel(窗口 + warmup)
freeze 面板              feature 2,259,889 行 / execution 2,372,392 行，当日历 306 天
   ↓ pit_universe → decisions → 可用性裁决 → label/特征/风格
训练帧
```

**判据不是"像不像数据缺失"，而是集合关系能否复现**：

| 检查 | 命令/方法 | 结果 |
| --- | --- | --- |
| ZIP ↔ delta 逐日全等（2025 全年 243 天） | 单遍读取 `2025.zip` 全部 5,718 个 symbol CSV，按日建集合，与两个 delta 库逐日比对 | **0 差异（每一天）** |
| ZIP 内 724 票是否缺该行 | 直读 `2025/300476.SZ.csv` 等 | 该行**不存在**（前后行存在） |
| 独立来源交叉验证 | Tushare `daily(trade_date=20251117/20251118/...)` | 缺口票**在 Tushare 存在** |
| PIT 候选集复现 | 复刻 `asof_universe`（109 自然日窗 / ≥60 bar），与 P3 官方逐日序列对账 | **306 天 0 不匹配** |
| 候选集总数对账 | 本审计逐日表求和 vs P3 官方 | `1,645,144 + 5,510(2026-08-31) = 1,650,654` ✅；过滤 `6,596 + 6 = 6,602` ✅ |

> 结论：**first_missing_stage = vendor_source（原始包）**。上游 ZIP 少了行，下游每一步都只是忠实搬运。

---

## 2. 逐日覆盖统计（source window 2024-11-14 .. 2026-08-31）

`P3_2_DAILY_COVERAGE.csv` 为逐日全表（463 行，列包含 `pit_candidate_count` / `qfq_symbols` / `raw_symbols` /
`execution_symbol_count` / `feature_symbol_count` / `prev|next_symbol_count` / `hole_both` / `missing_ratio` /
`qfq_only` / `raw_only` / `filtered_no_execution_bar` / `gap_10sess` / `gap_max_run`）。
下面给出任务要求的全部统计量。

### 2.1 单日共享缺失（`hole_both` = 前后交易日都有、当天没有，两侧同时缺）

| 指标 | 值 |
| --- | --- |
| P50 | 0.0000% |
| P90 | 0.0547% |
| P95 | 0.0908% |
| P99 | 0.6608% |
| **max** | **14.0229%（2025-11-17）** |

### 2.2 决策层过滤占比（`filtered_ratio` = 当日 PIT 候选里"当日无 execution bar"的比例）

| 指标 | 全部 435 天 | 剔除 11-17 / 11-18 |
| --- | --- | --- |
| P50 | 0.1485% | — |
| P90 | 0.2978% | — |
| P95 | 0.5085% | — |
| P99 | 4.6615% | 4.6421% |
| max | **12.2512%**（2025-11-17） | **4.7346%** |

source window 合计：候选 1,917,875 / 过滤 7,219 = **0.3764%**。

### 2.3 Top 20 异常日期（按 `hole_both`）

| # | 日期 | hole_both | 占比 | 过滤数 | 最长连续段 | 定性 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 2025-11-17 | 724 | 14.02% | 634 | **57** | UPSTREAM_SOURCE_GAP |
| 2 | 2025-11-18 | 271 | 5.75% | 281 | **23** | UPSTREAM_SOURCE_GAP |
| 3 | 2025-04-29 | 41 | 0.77% | 44 | 1 | NORMAL_SUSPENSION |
| 4 | 2026-04-29 | 41 | 0.75% | 48 | 1 | NORMAL_SUSPENSION |
| 5 | 2026-04-30 | 36 | 0.66% | 51 | 1 | NORMAL_SUSPENSION |
| 6 | 2025-04-30 | 34 | 0.64% | 39 | 2 | NORMAL_SUSPENSION |
| 7 | 2025-11-12 | 29 | 0.54% | 34 | 3 | UPSTREAM_SOURCE_GAP（小） |
| 8 | 2025-11-11 | 23 | 0.43% | 26 | 3 | UPSTREAM_SOURCE_GAP（小） |
| 9 | 2025-11-13 | 17 | 0.31% | 24 | 2 | UPSTREAM_SOURCE_GAP（小） |
| 10 | 2025-12-24 | 17 | 0.31% | 13 | 2 | UPSTREAM_SOURCE_GAP（小） |
| 11 | 2025-04-28 | 14 | 0.26% | 18 | 1 | NORMAL_SUSPENSION |
| 12 | 2026-04-28 | 14 | 0.26% | 22 | 1 | NORMAL_SUSPENSION |
| 13 | 2026-04-27 | 8 | 0.15% | 15 | 1 | NORMAL_SUSPENSION |
| 14–20 | 2025-04-22/24/25、2025-05-19、2026-04-20/24、2026-06-15 | 5–6 | 0.09–0.11% | 11–15 | 1 | NORMAL_SUSPENSION |

### 2.4 两类"不是缺口但会被误读成缺口"的形态

| 形态 | 区间 | 现象 | 真实原因 | 判定 |
| --- | --- | --- | --- | --- |
| **北交所代码段迁移** | 2025-09-30 → 2025-10-09 起 | 旧代码 430/83/87xxx（242 只）最后一根 bar 停在 **2025-09-30**；新代码 920xxx（**243 只**）首根 bar 出现在 **2025-10-09**。此后 ~3 周内旧代码仍是 PIT 候选（109 天窗内仍有 ≥60 bar）→ 每天 246–256 条被过滤（**4.55%–4.74%**，全窗口最大合法性过滤群体） | **市场事实**：北交所代码段迁移（1:1 换号） | 非缺口；但**必须**纳入 universe 连续性策略（`security_identity_mapping` 表**实测 0 行**，即当前**未映射**） |
| **feature 侧单边缺口** | 2026-07-17 .. 2026-07-30（11 个交易日） | **27 只票**（含 `000001` 平安银行、`600000` 浦发银行）在 qfq 面板**完全没有 bar**，而 raw 面板正常；形状是"07-16 有 → 07-17..07-30 无 → 07-31 恢复"。**端到端实测**（`daily_feature_frame` 对全量 PIT 候选的丢行数）：**07-16 = 5 行（0.091%，对照）→ 07-20 = 31 行（0.565%）→ 07-27 = 37 行（0.673%）→ 08-03 = 6 行（0.109%，对照）**，即缺陷期每天多丢 25–31 行，11 个 session 合计 ≈ 300 行 | feature 侧 panel build（qfq 因子/状态链路），受影响票集合与 `daily_trade_status`（32 票 / 2026-05-06..07-31，`suspended` 全 false）**高度重合** | **PANEL_BUILD_DEFECT（feature 侧）**，当前**无任何守卫覆盖**（见 §7.3） |

### 2.5 训练窗口起点的 feature 覆盖现状

qfq（feature）库逐日 symbol 数在 **2024-11-01 只有 278 只**，随后爬升（2024-12-03=445、12-16=1,741），**2024-12-17 起与 raw 完全一致**；raw 侧同期一直是 5,325–5,347。
即：**声明 source_window 起点为 2024-11-14，但 feature 侧真正可用的历史从 2024-12-17 开始**（差 33 自然日）。
这是 warmup 段的覆盖事实（首个决策日 2025-06-03 的 109 天窗落在 2025-02-13 之后，故不影响 PIT 资格），但它意味着**早期决策日的长窗特征是在缺段上算出来的**，且现有证据里没有任何字段描述这件事。

---

## 3. 逐层追查（任务 §5）

| 环节 | 检查方式 | 结论 |
| --- | --- | --- |
| **vendor 原始包** | 直读 `2025.zip` 内 5,718 个 CSV | ❌ **此处首次缺行**（724 只票的 2025-11-17 行不存在） |
| download / archive | 包内 11,438 个 entry 全部可解、逐票 CSV 可解析；archive 内部时间戳统一 2026-05-04 15:42 | ✅ 完整（无截断、无损坏） |
| extract | ZIP 与 delta 逐日集合 0 差异 | ✅ 无丢行 |
| RAW ingest | 同上（raw 与 qfq 逐日集合一致，且都等于 ZIP） | ✅ 无丢行 |
| delta merge / dedup / upsert | 逐日集合全等 + `(symbol,date)` 无重复 | ✅ 无缺陷 |
| date parsing / calendar | 缺口日的 `date` 键完全对齐（无时区/格式偏移：缺口是"整行不存在"而非"日期漂移"） | ✅ |
| QFQ build | 缺口在 raw 侧同样存在 → 不是因子链路造成的 | ✅ 无嫌疑 |
| feature / execution 面板 | 两面板当日 symbol 数**完全相同**（4,713 / 4,713；5,169 / 5,169） | ✅ 无单边丢失 |

**A/B 判定（任务 §5 的两问）**：

* A. "vendor 原始文件是否存在这些 bar？" → **不存在**。进一步查 download/ingest/merge/dedup/date/calendar 全部排除（逐日全等）。
* B. "vendor 原始源本身就没有？" → **是**。按任务 §5.B 规定：**不改 Alpha、不改训练窗口，只记录 `UPSTREAM_SOURCE_GAP`，等待人工决定数据治理方案**（本报告不擅自修数据）。

缺口形态补充（支持"批量/分片丢失"假设）：724 只分成 **210 段**连续票号，最长 **57** 连号（样本 `300476,300477,300478,…`）；11-18 的 271 只**全部是沪市主板**（`600469–600486` 等连号段），最长 23 连号。而正常停牌日是按"零散票"出现，最长连续段只有 1–3。

---

## 4. 对任务前提的更正（重要）

任务书提到"同型异常还至少存在于 2025-11-18、2025-10-09、2025-10-10、2025-10-13"。**实测结果与前提不符，分别说明**：

| 日期 | 实测 | 判定 |
| --- | --- | --- |
| 2025-11-18 | 271 只共享缺失、max_run 23、Tushare 全部存在 | ✅ 与 11-17 **同因**（vendor 缺行） |
| 2025-10-09 | qfq=raw=**Tushare=5,419**，逐只 0 差异；`hole_both`=1 | ❌ **不是覆盖缺口**。过滤 256 条（4.73%）全部是**北交所换号**留下的旧代码 |
| 2025-10-10 | 同上（5,422 / 0 差异 / hole=0，过滤 254） | ❌ 同上 |
| 2025-10-13 | 同上（5,424 / 0 差异 / hole=0，过滤 252） | ❌ 同上 |

这个更正直接改变阈值结论：**合法的北交所迁移日（4.7%/天）与真实的 vendor 缺行日（11-18 = 5.43%/天）在量级上是相邻的**。
因此在同一根比例尺上**不存在**能把两者分开的阈值——把单日闸从 10% 提到 50% 不仅没有依据，而且方向相反（它让真实缺陷更容易通过）。**必须换判据**（结构/来源），而不能调数值。

---

## 5. `be2e4ef`（P3.1）Pre-Merge Review

审查对象：`be2e4ef`（`git show be2e4ef`，6 文件 +1027/-32）；工作区当前另有未提交的外部改写（见 §6）。

| # | 审查项 | 结论 | 依据 |
| --- | --- | --- | --- |
| 1 | execution availability filter 本身是否正确 | **正确**。三层判据是集合关系（保留 / 过滤 / 结构缺陷 fail closed），不依赖比例直觉 | `dual_price_series.py::filter_decisions_by_execution_availability` |
| 2 | 是否在 label / 特征 / 风格构造**之前** | **是**。`training_decisions` 才喂给 `build_label_v2`、`compute_style_features`、`daily_feature_frame` | `dual_price_freeze.py` §2→§3→§4 顺序；`decision_accounting` 四段账 |
| 3 | 是否引入 survivorship / lookahead | **否**。PIT 候选只用到 ≤ as_of 的 bar；被过滤的票是"当日没有可成交观测"，不是"事后知道它停牌" | `pit_universe` 语义 + DP-11 |
| 4 | 是否改变 PIT universe | **否**。`pit_universe` / `expected_active_lookback_days` / `min_history_days` 一行未动 | diff 无相关改动 |
| 5 | 是否改变 Alpha selection | **否**（本仓库该链路无 Alpha 选择；freeze 只做训练帧） | — |
| 6 | 是否改变 label / target | **否**。`OutcomeSpec` 未动；label 逐 (symbol,date) 独立计算（entry=next_session_open, price_basis=raw）；`compute_style_features` 逐票独立，无横截面分母 | `research/outcomes.py` / `research/benchmarks.py` |
| 7 | 是否改变 training window | **否**。窗口/ warmup 来自 CLI 参数，未动 | diff |
| 8 | 是否有"为了让测试通过"而放宽的 guard | **有一处**：单日比例闸 10% → 50%（见 #9）。其余：`assert_decisions_aligned` 保持零容忍，`PriceSeriesContractError` 退出码修正（原为未捕获 traceback→exit 1，现为文档化的 exit 4）是**修复**不是放宽 | `git show be2e4ef -- src/.../dual_price_series.py`；`scripts/alpha_v2_shadow_model_freeze.py` try/except |
| 9 | 10%→50% 具体在哪里 | 常量 `DEFAULT_MAX_DAILY_FILTERED_RATIO = 0.50`（`dual_price_series.py`），判据是 `per_date_filtered[worst] > ceil(0.50 × 该日候选数)` 时 raise | 同文件 `filter_decisions_by_execution_availability` 单日闸段 |
| 10 | contract fix 与 coverage policy 能否解耦 | **可以，且应该**。见 §7 的最小方案 | — |

**对 `be2e4ef` 的净评价**：contract fix（三层裁决 + 顺序正确 + 严禁回退 qfq）**予以保留**；唯一必须撤销的是"把 12.25% 当作合法最大值、据此把单日闸放宽到 50%"这一步推理——12.25% 正是本报告要审计的异常本身。`DP-16`（build(all) 与 build(kept) 逐值相同）是有效的不变性测试，但它只在夹具上跑过，**没有在生产窗口上验证过**；本次审计在真实数据上的等价检查见 §5.1。

### 5.1 生产数据上的等价性检查（本审计新增）

* 被过滤键（634 / 281 / 246…）的 `filtered_with_feature_bar` 在 **435/435 天全部为 0** → 它们两侧都没有当日 bar，故不可能改变任何"peer/分母"（`compute_style_features` 逐票独立，`build_benchmark_suite` 只作用于已有 label 的 outcome 行）。
* PIT 候选集复刻与官方逐日序列 **306 天 0 不匹配**，过滤总量 `6,596+6 = 6,602` 精确对账 → 过滤逻辑可复现。
* 未在生产全窗口上重跑 `build_dual_price_training_frame` 两次做逐值比较（成本：label 构建 ≈ 0.0146 s/行，全窗口 ≈ 6.6 h，超出 NAS 只读诊断预算）。**这一条如实标注为"未验证"**，建议在具备 ≥48 GB 主机时随真正 freeze 一起验证。

---

## 6. 工作区状态：外部并行改写（未提交）

审计期间（22:00–22:39）**有另一会话在并发修改工作区**，我的审查基于固定快照：

```text
HEAD = f2596ce9d46ffea3f77e4d528559a445067d322a （be2e4ef 之上新增"docs(agents): bootstrap …"一个提交）
未提交（M）：
  src/stock_analyzer/alpha_v2/dual_price_series.py              sha256(LF) 5ca8b264…  mtime 22:33
  src/stock_analyzer/alpha_v2/validation/dual_price_freeze.py   sha256(LF) 364f1747…  mtime 22:00
  scripts/alpha_v2_shadow_model_freeze.py                       sha256(LF) 5e20abd9…  mtime 22:18
  tests/test_alpha_v2_dual_price_series.py                      sha256(LF) 3d485698…  mtime 22:36
  .agents/notes/ADR-002-dual-price-freeze-contract.md / README.md / docs/alpha_v2/P0_Dual_P…
未跟踪：docs/system_issues_for_review_20260917.md（用户文件，非本审计产物）
```

该改写（可称 P3.1.1）做了三件事：① 新增**日截面健康门**（当日 bar 数 / 前 20 个 session 中位数 < 0.90 → fail closed；execution 当日截面 < feature 同日 × 0.90 → fail closed）；② 把两条比例闸重新标注为 **provisional anomaly guard**，并在注释里写明"12.25% 不是合法值、50% 那次放宽是错的"；③ 明确 **FILTER ≠ 已证明停牌**，把 `filter_reason_semantics` 写进审计。

**我独立验证了它的关键论据**（本地十年面板 `artifacts/warehouse/market.duckdb`，2,489 个交易日）：

| 改写方的断言 | 我的独立复算 | 结论 |
| --- | --- | --- |
| 健康面板 breadth p01 = 0.9923，十年内 <0.99 极罕见 | p01 = **0.9953**、p05 = 0.9997、min = 0.0083（截断尾） | ✅ 成立（数值差在合理范围） |
| 十年内 <0.90 的只有库尾截断日 | 只有 2026-04-02（0.0095）/ 04-03（0.0083）；另 2026-04-01 = 0.9211（部分截断） | ✅ 成立；**0.95 同样零误报**（并多抓 04-01） |
| 2016 年窗口按同一 PIT 语义是 8778/422566 = 2.0773% | **精确复现：422,566 / 8,778 = 2.0773%**，最差日 2016-04-22 = 3.83% | ✅ 成立（2% 默认闸已被证伪普适性） |
| 十年最大"形态合法"单日过滤占比 3.834% | 十年逐日最大 **4.69%**（2018-02-08，p99 = 2.86%）；生产窗口合法最大 **4.73%** | ⚠️ 略大于其值（窗口不同），但**远小于 50%** |

**该改写仍存在的两个缺口（本报告的最小补丁对象）**：
1. **11-18 逃逸**：日截面门只看"当天截面是否塌"。11-18 的当天 bars = 5,169，前 20 个 session 中位数 = 5,431.5 → **ratio = 0.9517**，0.90 与 0.95 都不触发；execution/feature 同日比 = 1.000（两侧同缺）；单日比例闸 5.43% < 50% 也不触发。→ **271 个真实缺失 bar 会静默进入"合法过滤"**（只留在 `filtered_dates_top` 里）。**同一个 vendor 缺陷，大洞被拦、小洞被放行**——这正说明判据必须结构性。
2. **feature 侧单边缺口无守卫**（§2.4）：`execution 有 bar、feature 无 bar` 的方向没有检查。`daily_feature_frame` 对这类键**不返回行**（实测 4 点：07-16 = 5 行 / 07-20 = 31 行 / 07-27 = 37 行 / 08-03 = 6 行；缺陷期每天多丢 25–31 行），而 `build_dual_price_training_frame` 的 `features.merge(primary, how="inner")` 会把这些决策**静默丢出训练帧**；审计里的 `decision_accounting` 又用"未成熟/未成交"解释 `training_frame_rows < outcome_rows`，**无法区分**"正常无 label"与"feature 侧缺行"。（任务书 §6 明确要求这一形态 hard fail。）

---

## 7. 覆盖守卫重设计（先给统计，不拍脑袋）

### 7.1 阈值必须换维度：量级不可分，结构可分

```text
合法最大过滤群体：北交所迁移日 4.55%–4.74%（真实市场事实）
真实缺陷日：      2025-11-18 = 5.43%（vendor 缺行 271 只）
                 —— 两者相邻，任何单一比例阈值都会同时放掉真缺陷或误杀合法日
```

### 7.2 结构性判据的实测标定（本报告新增，全部可复算）

`max_run` = 当日"共享缺失/被过滤"票号集合中**最长连续数字段**长度。

| 数据集 | 会话数 | 合法 max_run | 缺陷 max_run | 说明 |
| --- | --- | --- | --- | --- |
| 生产 delta 窗口（2025-06-02..2026-08-31 口径，实际覆盖 2024-11-14 起 435 天） | 435 | **3**（2 天：2025-11-11/12） | **23 / 57** | 3/4 天分布：run=3 有 2 天，run=4..22 为 0 |
| 本地十年面板（2016-01-04..2026-04-03） | 2,429 | **3**（16 天，最大占比 4.69%） | **580**（仅库尾截断 2 天） | run 直方图 {0:54, 1:2110, 2:247, 3:16, 580:2} |

**结论**：`run ≥ 8` 作为 fail-closed 阈值，在 2,864 个 session 上**零误报**，且对两个已知缺陷日留有 3–7 倍余量；
`run ∈ {4..7}` 在实测记录里没有实例，建议**记录并要求独立来源复核**（audit-only），不直接 fail。
（反例说明：北交所迁移日的 246–256 条过滤，max_run 只有 **2**，不会被误杀。）

### 7.3 建议的最小改动（保留 contract fix，异常继续 fail closed）

1. **保留** `be2e4ef` 的逐键三层裁决与"过滤先于 label/特征/风格"，原样不动。
2. **保留**日截面健康门；建议把 `min_session_breadth_ratio` 由 0.90 **收紧到 0.95**（十年 p05 = 0.9997，零误报，并能多抓部分截断日 2026-04-01 = 0.9211）。该项为独立判据，不与比例闸混用。
3. **新增 run-length 结构门**（对每个决策日的"被过滤集合"计算 max_run）：`≥ 8` → fail closed（`EXECUTION_SHARED_MISSING_CONTIGUOUS_RUN`）；`≥ 4` → 记审计并要求独立来源核对。此项同时覆盖 11-17 与 **11-18**。
4. **新增单边跨面板门**：`execution 有 bar、feature 无 bar` 的键数 > 0 → fail closed（或在审计中显式列出），因为这类键会被 `how="inner"` 静默丢出训练帧；并在 `decision_accounting` 中把 `feature_missing_rows` 单列，避免与"无 label"混淆。
5. **撤销 50%**：单日比例闸回到**实测**口径（十年合法最大 3.83%、生产合法最大 4.73% → 10% 仍然安全），或在前三条结构门落地后**删掉**它——保留会给人"50% 已被批准"的错觉。
6. **重设窗口级闸**：`2%` 已被十年 2016 窗口 2.0773% 证伪；建议按多窗口分布重设（本文只提供分布，不擅自定值）。
7. **单列一类"身份迁移"**：北交所换号不该走覆盖口径（它是 universe 连续性问题）。要么补 `security_identity_mapping`（实测 0 行）把 430/83/87xxx → 920xxx 映射起来，要么在 note 中显式声明"旧代码票在换号后按退市处理"，二选一，不能默认。

### 7.4 日截面门的固有盲区（必须写明，避免"加了门就安全"的错觉）

日截面门用**前 20 个 session 的中位数**做基线，因此：

* **缓慢恶化会被基线吸收**：若某缺陷连续 >20 个 session 每天只削掉 5%–15% 的 bar，基线会跟着下移，比例回到 ≈1.0 → 门永不触发（温水煮青蛙）。本窗口 2024-11-13..2024-12-16 feature 侧爬升正是这种形态（最高缺失 94%，但因为是"渐进起点"而非"单日塌陷"，日截面门原理上不适用，只能靠"声明窗口 ↔ 实际覆盖"的一致性检查）。
* **不改变当天 bar 数的缺陷**（缺的是连号段而非整片截面）天然无感——11-18 就是这种：只少 5%，但少的是 23 连号段。这正是 §7.3 第 3 条存在的理由。
* 三类门（**日截面** / **结构连号段** / **单边跨面板**）各管一种形态，缺一不可；比例闸只能当量级报警，不能当"合法"的定义。

---

## 8. 交付状态（任务 §12 三选一）

```text
P3_1_CONTRACT_FIX        = PASS      （be2e4ef 的三层裁决与顺序正确；唯一需撤销的是 10%→50% 的放宽推理）
P3_2_DATA_COVERAGE       = BLOCKED   （vendor 2025 包缺行未修；且守卫未定稿 → 现状仍可能放行 11-18）
P3_FREEZE_PREPARATION    = BLOCKED_BY_DATA_COVERAGE
```

**不入下一阶段的三个硬理由**：① 4 个 vendor 缺行日（含 2 个大缺口）真实存在且未修；② 现有/在途守卫都不能拦住 11-18；③ 内存规模未证明（见下）。

**48 GB 主机问题（任务 §13.10）**：结论是 **48 GB 主机在容量上"刚够但没有余量"，且不能直接用 P3 里那条线性拟合做容量规划**。理由（P3 证据里的两点实测，换成"会被 OOM-kill 的口径"即 cgroup peak）：

| 决策行数 | cgroup peak | anon 增量（P3 拟合用的量） | 备注 |
| --- | --- | --- | --- |
| 42,770 | 3,364.9 MB | 989.4 MB | 完成 |
| 128,751（全量 7.8%） | **≥5,221.9 MB**（在 `--memory 6g` 下 **OOMKilled 137**） | 1,673.0 MB | 未完成，说明真实峰值 > 6 GB |

按 cgroup peak 的两点外推 ≈ **21.6 KB/行** → 全量 164 万行 ≈ **38 GB**；而 P3 文档里的 `0.00795 MB/行` 是 **anon 增量**，外推只有 13.7 GB——**两者差 2.8 倍，后者已被"6 g 下 12.9 万行仍 OOM"反例否决**，不能用于容量规划。
**结论：48 GB 主机具备最小可行性，但需要先做一次规模验证（例如分块构建 label 或先跑 25% 决策集测峰值），并通过 heavy 等效 config_hash 环境 + 可证代码身份 + 只读源库 + 独立产物目录。数据缺口修复与守卫定稿之前，freeze 仍不具备条件。**

---

## 9. 复算命令（可迁移）

```bash
# 1) ZIP ↔ delta 逐日集合比对（NAS，只读）
python /tmp/p32/q4_zip_vs_db.py 2025          # -> 输出每日 zip/qfq/raw 计数与差异数

# 2) 独立来源交叉验证（NAS，容器内，只读 + 少量 API 调用）
python /tmp/p32/q12_tushare.py                # Tushare daily(trade_date=...) vs delta

# 3) PIT 候选 + 过滤逐日表（NAS，容器内，只读；~35 s）
python /tmp/p32/q15_pit_table.py              # 复刻 109 天窗 / ≥60 bar，对账官方 P3 序列

# 4) 十年标定（本地 Windows，只读）
python scripts/local_runguard2.py             # run-length + breadth + 2016 窗口 2.0773%
```

（以上脚本位于审计工作目录，未纳入仓库；如需固化为可复跑证据，应作为独立 commit 提交到 `scripts/` 下并附测试。）

---

## 10. 未验证 / 边界（不得当作已知）

* 未在**生产全窗口**上重跑两次 `build_dual_price_training_frame` 做逐值比对（`DP-16` 的生产版等价性未验证，成本见 §5.1）。
* 未验证 2025-11-11/12/13、2025-12-24 这 4 个小缺口日的**产生机制**（只证明"delta 与 ZIP 一致、Tushare 有 bar"）；也未证明 12-24 的 17 只与 11 月缺口同源。
* 未定位 2026-07 feature 侧缺口的**直接原因**（只测到：27 只票 × 10 个 session 无 qfq bar、raw 有、受影响票集合与 `daily_trade_status` 的 32 票高度重合、`suspended` 全为 false）。需要查更新器/因子链路的日志才能定因。
* 未审计 2026 年 vendor 包的**包内一致性**（2026.zip 由本仓库更新器每晚维护，与 2025.zip 的静态包性质不同；本次只做了 delta↔Tushare 的日期抽样交叉验证）。
* 本报告未修改任何数据、未生成任何 model / epoch / production artifact；NAS 侧动作全部为只读（`read_only` 连接 + `:ro` 挂载）。

---

## 11. 本轮验证记录（实际跑过的命令与结果）

| 验证 | 命令 | 结果 |
| --- | --- | --- |
| 目标测试文件（当前工作区快照，含外部改写） | `python -m pytest tests/test_alpha_v2_dual_price_series.py -q` | **39 passed / 0 failed（exit 0）**，快照哈希见 §6 |
| PIT 候选集复刻对账 | NAS 只读容器内 `q15_pit_table.py` | 与官方 P3 逐日序列 **306 天 0 不匹配**；总数 `1,645,144+5,510 = 1,650,654`、过滤 `6,596+6 = 6,602` |
| ZIP ↔ delta 逐日全等 | NAS 只读 `q4_zip_vs_db.py 2025` | 243 天 **0 差异** |
| 独立来源交叉验证 | NAS 容器内 `q12_tushare.py`（少量 API 调用） | 11-17：Tushare 5,437 / DB 4,713，缺口 724 **全在 Tushare**；10-09/10-10/10-13/11-14：**0 差异** |
| 十年标定 | 本地 `local_runguard2.py`（只读） | run≤3 合法上界、breadth p01=0.9953、2016 窗口 2.0773% 精确复现 |
| feature 侧静默丢行 | NAS 容器内 `q21_featloss.py`（4 个决策日） | 07-16 = 5 行 / **07-20 = 31 行（0.565%）** / **07-27 = 37 行（0.673%）** / 08-03 = 6 行；对照日 5–6 行 vs 缺陷期 31–37 行 |

**push 状态**：本地分支 `feat/alpha-v2-raw-execution-delta-r1` 比 `origin/feat/alpha-v2-raw-execution-delta-r1` **领先 2 个 commit**（`be2e4ef`、`f2596ce`）——两者**都还没推送**，更没有 PR。
