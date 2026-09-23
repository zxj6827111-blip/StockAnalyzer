# Alpha V2 双价格序列契约（P0）

> 状态：**已实施（工程层）**；NAS 数据侧动作（建 RAW delta / 开 epoch）仍待用户授权。
> 日期：2026-09-21。分支：`hotfix/alpha-v2-dual-price-series`（独立 PR，不 merge）。

## 1. 问题：一份 qfq 序列同时当了特征与成交价

项目价格契约（`src/stock_analyzer/backtest/price_contract.py`）写得很清楚：

```text
Feature Series may be QFQ
Execution Series must be RAW
```

但 Alpha V2 的冻结与成熟链路此前只有一个 `--market-db`：

- `scripts/alpha_v2_shadow_model_freeze.py`：训练面板既喂 `FeatureEngineer`，又喂
  `build_label_v2`（净收益 / 超额 / MAE / MFE / 方向目标 / 全部基准序列）；
- `scripts/alpha_v2_shadow_mature.py`：同一个面板既算 outcome 又算风格维度；
- 生产 NAS 的正式库是 `/app/artifacts/vendor_delta/market_delta.duckdb`，
  `price_series_mode=qfq`。

后果不是"口径不够精确"，而是**训练目标本身失真**：复权序列在每次除权日产生非交易性
跳变，把"研究口径的调整"写成了"真实亏损"；同时涨跌停判定、可成交性（`ExecutionMatcher`）
拿到的也是复权价。更糟的是 `shadow_model_freeze.py` 在
`price_mode_certified=false` 时**只打 warning**——于是这条错误路径可以一路跑到冻结完成。

## 2. 契约

| 角色 | 用途 | 允许口径 | 硬门 |
| --- | --- | --- | --- |
| feature | `FeatureEngineer` 特征矩阵、风格维度（同板块 kNN 分组） | **qfq**（设计内）或 raw | 口径必须**可证**（声明或探针），且等于冻结模型声明的 feature mode |
| execution | `build_label_v2` / 成交价 / 净收益 / 超额 / MAE/MFE / 方向目标 / 全部基准序列 | **只有 raw** | `price_mode == raw` 且 `price_mode_certified == true`，否则 FAIL CLOSED |

三条纪律：

1. **守卫先于重活**：execution 口径不合法时，在构造完整特征矩阵之前失败——不允许跑
   几十分钟才报错；
2. **绝不猜测**：execution 面板缺一条 `(symbol, date)` 就是 target 不可用，不允许退回
   qfq、不允许从 qfq 反推 raw——**但"不可用"分两种，处置不同**（见 §2.1）；
3. **两条身份各自成块**：`feature_data_identity` / `execution_data_identity` 分开记录、
   分开对账、分开封存。

## 2.1 有效决策集：候选 ≠ 可交易（P3.1 `be2e4ef`；P3.1.1 加日截面健康门）

PIT 合格池（`expected_active_lookback_days=5`，**5 个自然日**，见
`asof_universe.build_pit_stats`）按设计含"最近还活跃、当天拿不到 execution bar"的票
（含退市/长停期间仍在 history 窗口内的票）。这些 `(symbol, decision_date)` 的 target
不成立（T+1 入场要求当日有决策 bar），必须在构造 label/特征之前出局。三段集合因此显式分开：

```text
decision universe      PIT eligible ∩ feature 日历         = decision_rows_before
execution available     上述 ∩ execution 当日有 bar          = decision_rows_after / intersection
training frame          上述 ∩ feature frame ∩ 有 label 的行   = len(frame)
```

裁决分三层，**日级健康门先于逐键裁决**：

| 观测 | 裁决 | 原因码 |
| --- | --- | --- |
| 【日级】决策日 execution 截面相对自身基线塌陷（<90%） | **fail closed** | `EXECUTION_SESSION_BREADTH_COLLAPSE` |
| 【日级】execution 同日截面明显低于 feature（<90%） | **fail closed** | `EXECUTION_SESSION_BREADTH_BELOW_FEATURE` |
| 【逐键】在 execution 面板里 | 保留 | — |
| 【逐键】票在整个 execution 面板都不存在 | **fail closed** | `SYMBOL_NOT_IN_EXECUTION_PANEL` |
| 【逐键】该日期在 execution 面板里不是交易日 | **fail closed** | `DECISION_DATE_NOT_A_SESSION_IN_EXECUTION_PANEL` |
| 【逐键】feature 面板当天**有** bar 而 execution 没有 | **fail closed** | `FEATURE_PANEL_HAS_BAR_ON_DECISION_DATE` |
| 【过滤】以上全不成立（两侧同票同日都无 bar） | **过滤 + 入账** | `NO_EXECUTION_BAR_ON_DECISION_DATE` |

⚠️ **最后一行不等于"已证明停牌"**。本仓库不存在可用于 Alpha V2 freeze 的、独立且
PIT-safe 的停牌真值源（`daily_trade_status` 实测 154 行 / 2 只票 / `sum(suspended)=0`
且 alpha_v2 从不读；`security_status` 0 行 0 生产方；`daily_bars.suspended` 全库恒
False）。更关键的是**两份面板共享同一条上游链路**：同一个缺陷会同时命中两侧，
于是"两边都没 bar"这个观测对"不可交易"与"对称断供"**给不出不同答案**——逐键判据在
这一类上原理性失效。日截面健康门就是为这一类补的：它完全不看 decision 集合，只看
"这一天面板自己还剩多少根 bar"。

三条逐键判据不是"停牌"，而是两份面板对同一份事实给出了不同答案——静默过滤会把真实的
断供/截断/换库伪装成"少了几行训练样本"。**单侧**丢失由"跨面板分歧"逐键拦截（与量级无关）。

### 2.2 三层判据的证据强度（P3.1.1 实测）

| 层 | 判据 | 是否依赖 decision 集合 | 跨窗口实测 |
| --- | --- | --- | --- |
| 日级广度 | 当日 bar 数 / 面板自身前 ≤20 session 中位数 < **0.90** | **否** | 十年 2,489 个 session：judged 2,488 / unjudgeable 1，只命中 **2 个**，且两者都是真实尾部截断（2026-04-02/03）；对 2016 年 8,778 条合法"当日无 bar"**零误报**；NAS 上报的 2025-11-17 形态（5,438→4,713=0.8667）**会被抓到** |
| 日级两侧 | execution 同日 bar 数 / feature 同日 < **0.90** | **否** | 方向单边（raw 多于 qfq 是设计内） |
| 逐键集合 | 整票缺席 / 该日非 session / 跨面板分歧 | 是 | 逐键、与量级无关 |
| 比例兜底 | 全窗 2%、单日 50%、50 行下限 | 是 | **provisional，已知不普适**（见下） |

日级门**主动弃权**的两种情形（都如实记进 `session_health.unjudgeable_*`，绝不记成"通过"）：
面板**第一个 session**（无前序可比——十年截面从 2,817 长到 5,198，用全局中位数兜底会把首日
误判成 59% 塌陷，从而误杀"窗口起点=面板首日"的真实 freeze；实测抓到并已修）；
基线截面 **< 100 行**（小尺度上比例无意义，生产每日 4,700–5,500 只不受影响）。

两条比例闸**不是停牌定义**，只是量级报警，且已知会误杀：

| 闸 | 默认 | 实测反例 / 依据 | 性质 |
| --- | --- | --- | --- |
| 全窗过滤占比 | 2% | 生产窗口 0.400%（6602/1650654）**但** 2016 年窗口 **2.0773%**（8778/422566）→ 该值不普适，已证实会 fail closed | provisional |
| 单日过滤占比 | 50% | 由 10% 放宽而来，理由是把 12.25%（2025-11-17）当成"最大合法值"；实测十年最大**形态合法**单日过滤仅 **3.834%**（2016-04-22），p99 3.566% → 10% 从未误杀 | provisional（偏松） |
| 行数下限 | 50 行 | 小窗口/小夹具不被比例门误杀 | 与比例共用 |

> **2025-11-17 的正确定性**：raw 与 qfq **两侧同时**少 724 个 symbol 的当日 bar
> （逐票形态"前一根 11-14、后一根 11-18"，抽样 200/200）。这不是"部分覆盖缺口可以放行"，
> 而是**上游链路的数据缺陷**——本地 warehouse 同一天的截面毫无异常
> （5,155 票，相邻 5,156 / 5,157，11 月均值 5,126）。现在它由日级广度门拦下
> （−13.3% 远低于 0.90），而不是被当成阈值依据把闸放宽。
> 此类日期在放行前必须先修链路；`filtered_dates_top` 仍提供可审计的按日分布。

审计字段进 `dual_price_evidence.decision_alignment`：`decision_rows_before` /
`filtered_missing_execution_rows` / `decision_rows_after` / `intersection` /
`filtered_ratio` / `filter_reason` / `filter_reason_semantics` / `filtered_examples` /
`filtered_dates_top` / `max_daily_filtered_ratio` / **`session_health`**（judged /
unjudgeable / worst_breadth / panel 比值 / limits / enforced）/ `status`。
`decision_accounting` 另显式记 `filtered_unavailable_rows` + 原因码 + 语义标注。

`assert_decisions_aligned` 保留为**零容忍**版本（任何一条不齐即抛）。⚠️ 截至 `f2596ce`
它**只有测试调用方**，所称"供研究/回放路径使用"尚无对应入口。
训练帧走 `filter_decisions_by_execution_availability`。

> 实测（生产窗口 2025-06-02..2026-08-31 / warmup 200，**P3.1 口径、日级门之前**）：
> `1,650,654` 条候选 / `6,602` 条被过滤（`0.400%`）/ `1,644,052` 条进入训练帧，
> `status=PASS`。⚠️ **该结论在 P3.1.1 之后不再成立**：同一窗口含 2025-11-17，
> 日级广度门会 fail closed。上表数字是历史测量，不是当前预期。

## 3. 守卫落点

| 环节 | 实现 | 失败形态 |
| --- | --- | --- |
| 冻结训练 | `require_certified_execution_series` + `filter_decisions_by_execution_availability`（`build_dual_price_training_frame` 前两步；后者内部**日级广度门先于逐键裁决**） | 口径违例 / 日截面异常 / 逐键结构缺陷 → `PriceSeriesContractError`，CLI exit 4（**显式捕获**，不走未捕获 traceback）；当日无 execution bar → 过滤并记审计（**不代表已证明停牌**） |
| `build_label_v2` | 默认 `enforce_execution_price_series=True`；非 raw / 未认证直接抛错 | `PriceSeriesContractError` |
| outcome 成熟 | `outcome_maturation.mature_epoch_outcomes` 门 0（在任何计算/写入之前） | `PriceSeriesContractError`，CLI exit 4，**0 行 outcome** |
| KPI 证据 | `validation_kpis`：逐日 `_day_outcome_price_series_ok` + 逐行 `_certified_raw_mask` | 该日不 clean；主样本为空；`price_series` 块如实计数 |
| Production Preflight | `check_feature_price_series` / `check_execution_price_series` / `check_execution_data_fingerprint` | `BLOCKED`（exit 1） |

`build_label_v2` 为**研究回放**保留了一个显式出口：
`enforce_execution_price_series=False` **且** `research_replay_reason` 非空（理由进入
diagnostics）。生产入口（freeze / mature）**不含这个开关**，并由结构测试钉住
（`test_production_entrypoints_have_no_escape_hatch`）。

## 4. 两条数据身份与工件哈希 v3

冻结模型 provenance 新增并**封存**（受 `artifact_hash` 保护）：

```json
{
  "validation_mode": "production",
  "feature_price_mode": "qfq",
  "execution_price_mode": "raw",
  "db_role_binding": "dual_source",
  "feature_data_identity": {
    "role": "feature", "db": "...", "price_series_mode": "qfq",
    "price_series_certified": false, "certification_source": "...",
    "certification_evidence": {...},
    "fingerprint_version": "v2", "source_window": ["...", "..."],
    "warmup_days": 200, "fingerprint": "<sha256>",
    "columns": ["..."], "rows": 2405272
  },
  "execution_data_identity": {
    "role": "execution", "db": "...", "price_series_mode": "raw",
    "price_series_certified": true, "...": "..."
  }
}
```

工件哈希版本升到 **v3**：

```text
v1 = R4.1 算法（训练 provenance 不在受保护身份内）
v2 = + config_hash + 规范化 training_provenance（R1.1）
v3 = + 双价格源身份 + validation_mode（本 P0）
```

版本轴的意义：v1/v2 仍按各自语义复算、仍可加载（历史 / rehearsal 兼容），但
**生产只接受 v3**——v2 工件的价格口径只是"单库自述"，无法证明训练目标来自 raw。
`validation_mode != production` 的工件（rehearsal）在生产同样 BLOCKED。

## 5. 生产接线

配置（`config/default.yaml` + `AlphaV2Config`）：

```yaml
alpha_v2:
  feature_market_db: ""      # 空 → 回退 market_warehouse.db_path（NAS 的 qfq delta）
  execution_market_db: ""    # 空 → execution 侧一律 fail closed
```

NAS 上按既有约定用环境变量覆盖（不改受跟踪的 yaml）：

```bash
SA__ALPHA_V2__FEATURE_MARKET_DB=/app/artifacts/vendor_delta/market_delta.duckdb
SA__ALPHA_V2__EXECUTION_MARKET_DB=/app/artifacts/vendor_delta_raw/market_delta_raw.duckdb
```

每日循环（`LiveShadowCycleService`）的两个角色分开：

| 步骤 | feature DB | execution DB |
| --- | --- | --- |
| capture（特征 + 模型推理） | `--market-db` = `feature_market_db_path()`（qfq） | — |
| mature（outcome / 基准） | `--feature-market-db`（仅风格维度，按 epoch 内 symbol 过滤加载） | `--execution-market-db`（raw，每天重新 certify） |

`--market-db` 在 freeze / mature 上成为**已废弃参数**：只在 `--rehearsal` 下被接受
（两角色绑同一份库，provenance 如实标 `db_role_binding=legacy_single_db`）；生产形态
给出它会直接 exit 4。

## 5.1 Live 运行期不变量（Final R1）

冻结时的口径只证明"训练那一刻是对的"；**运行期必须每天重新证明**：

```text
TRAIN          feature mode = qfq        execution mode = raw + certified
LIVE CAPTURE   feature mode == 冻结模型声明的 feature mode（每天复核）
LIVE MATURE    style feature mode == 冻结 feature mode；execution = raw + certified
KPI            matured outcome == raw + certified
```

任何一项不成立 → 该日 **NO CLEAN EVIDENCE**。

| 环节 | 期望值来源（唯一权威） | 实测判别 | 不成立时 |
| --- | --- | --- | --- |
| capture（`alpha_v2_shadow_capture.py`） | 工件 `model.manifest.provenance.feature_data_identity.price_series_mode` | 当天面板 `certify_price_mode()` | production/test：exit 11，**在 `pit_universe` / 特征 / 预测 / 写盘之前**；rehearsal：警告 + 日清单标 `contract_ok=false` |
| scheduler 前置（`_ensure_feature_price_series`） | freeze 清单 `model.provenance.feature_data_identity.price_series_mode` | `derive_feature_price_series_input()`（轻量 probe 40d × ≤300 只） | production/test：`alpha_v2_waiting:feature_price_mode_mismatch`（窗口末尾 → 记 missing day）；**不再**反复 `step_failed:capture` |
| mature（`alpha_v2_shadow_mature.py`） | freeze 清单同一字段 | feature 面板 `certify_price_mode()` | production/test：exit 11 且 0 行 outcome；**禁止**退回 execution 面板算 style |
| mature 函数层（`mature_epoch_outcomes`） | `validation_mode` 形参（默认 production） | `style_panel is None` | production/test：抛 `OutcomeMaturationError`；rehearsal：`style_features_source=execution_panel_fallback_rehearsal` |
| KPI（`validation_kpis`） | 冻结清单 + 行级自述 | 逐行 `price_mode/price_mode_certified` | 该日不 clean、主口径样本为空 |

补充：

- **feature 侧不要求 `certified=true`**：qfq 的 `certified` 本来就是 false，误用 execution
  标准会把正确的 qfq 判成失败；feature 只要求"模式可证 + 等于冻结声明"。
- 严格集合是 `{production, test}`（`LIVE_STRICT_VALIDATION_MODES`）。比"只挡 production"更严
  一格是有意的：`test` 在本仓库是 **clean-OOS 合格模式**（只为确定性时钟存在），放它降级
  会产生"clean 证据 + 变了语义的 style 基准"。只有 `rehearsal` 允许带标注降级。
- capture 的当日清单新增 `feature_price_series` 证据块（`expected_mode` / `observed_mode` /
  `certification_source` / `certification_evidence` / `source_db` / `contract_ok` /
  `enforced` / `reason`），因此任何一个 L20/L60/L120/L250 日都能直接回答
  "当天模型看到的 feature price mode 是什么"。

> 已知遗留（不在本轮范围）：``signal_close_raw`` 仍取 feature 面板的当日 close，
> 而它在 KPI 里被用作执行质量诊断（`entry_gap_mean`）。它是**诊断字段、不是证据**，
> 且 capture 一旦改用 raw 库就跨越了"capture 只依赖 feature 库"的边界，故本轮不动；
> 建议后续以独立变更改为读 execution 侧（或在字段名上显式标注 feature 口径）。

## 6. RAW delta 数据源设计（本 PR 不改 NAS 数据）

目标：新增**独立** RAW delta，绝不覆盖现有 qfq：

```text
/app/artifacts/vendor_delta/market_delta.duckdb          ← 现状，保持 qfq，不动
/app/artifacts/vendor_delta_raw/market_delta_raw.duckdb  ← 新增，raw
```

现有工具已经支持所需能力（无需新写取数逻辑）：

- `scripts/import_vendor_zip_to_delta.py --price-series-mode raw`：从 vendor ZIP 直接
  产出 **raw** 序列（`VendorZipOverlayProvider(price_series_mode="raw")`，不查复权因子）；
- `--incremental`：只写比基线更新的日期（增量同步）；
- `scripts/shadow_rebuild_price_series.py --target-mode raw`：以复权因子归档对 qfq 序列
  做**逆变换**得到 raw（一次性基线备选路径；缺因子的 symbol 跳过而不是贴假标签）。

### 6.1 基线导入（**coverage-driven**，不接受固定深度推荐）

```bash
# 容器内（只读 ZIP + 写新 delta 路径）
python scripts/import_vendor_zip_to_delta.py \
  --data-root /data \
  --index-path /app/artifacts/vendor_overlay/daily_index.json \
  --delta-db-path /app/artifacts/vendor_delta_raw/market_delta_raw.duckdb \
  --price-series-mode raw \
  --limit-days <按 §6.1.1 算出来的深度>
```

> ⚠️ **`--limit-days` 是 per-symbol 的"行数"，不是自然日**，因此"400 看起来比 Week5 的
> 240 大"**不构成** Alpha V2 生产冻结的覆盖证明。**400 只是 Week5 常规 lookback 的示例值，
> 不是生产冻结的充分条件**；不要为了方便拍一个 500/600。

#### 6.1.1 覆盖判据（正式上线的唯一标准）

正式 candidate model 的口径：

```text
decision window : 2025-06-02 .. 2026-08-31
warmup          : 200 natural days
source window   : 2024-11-14 .. 2026-08-31        ← RAW baseline 必须覆盖到这里
```

`source_window` = 决策窗起点向前 `warmup_days` **自然日**（不是交易日、不是行数）：

```text
source_window_start = 2025-06-02 - 200d = 2024-11-14
```

上线流程（**以覆盖证明为准，不以某个整数为准**）：

```text
1) 按 source_window 计算所需历史深度：
   required_days = (decision_window_end - source_window_start).days
2) 用 feature 侧同一窗口的 symbol 集合作为 required symbols
   （即训练/打标签真正会读到的那些票）
3) RAW baseline 导入后实际验证：
   - required symbols 覆盖率（分母 = required symbols，分子 = 在 source_window
     内有 bar 的 symbol；任何缺失列出 symbol 明细）
   - raw 的 min(date) <= source_window_start（且逐 symbol 检查，不只看全库 min）
   - source_window 内 symbols × trading days 的行覆盖是否完整
4) 覆盖不足 → 扩大 --limit-days 并**重建**（不是打补丁式追加）
5) 记录并归档一条 `source_window coverage PASS` 证据（symbol 级明细 + 窗口 + 行数），
   它才是"baseline 可用"的凭据
```

可用现成读取口径做核对（与训练链同一套）：`compute_training_data_fingerprint(...,
training_start=2025-06-02, training_end=2026-08-31, warmup_days=200)` 返回
`source_window` / `rows` / `columns`，预检会把这三项与冻结模型封存值逐项对账
（§5）——所以 baseline 的深度**先被这条对账间接证明，再由 §6.1.1 的符号级明细直接证明**。

### 6.2 每日增量同步

现状：`update_vendor_daily_from_tushare.py --sync-vendor-delta <qfq path>` 在夜间链路里
调用 `import_vendor_zip_to_delta --incremental`，**未透传** `--price-series-mode`，因此
只能产出配置默认（qfq）。RAW delta 的日常增量有两条可选路径：

```text
A) 给 updater 增加第二个目标（推荐）
   --sync-vendor-delta           /app/artifacts/vendor_delta/market_delta.duckdb
   --sync-vendor-delta-raw       /app/artifacts/vendor_delta_raw/market_delta_raw.duckdb
   （并在内部给 RAW 目标透传 --price-series-mode raw）
   → 同一个事务里 qfq 与 raw 同步推进，天然锁步

B) 独立 cron 步骤
   每天在 nightly chain 之后跑一次
   import_vendor_zip_to_delta.py --incremental --price-series-mode raw \
       --delta-db-path /app/artifacts/vendor_delta_raw/market_delta_raw.duckdb
   → 简单，但与主链可能产生滞后（正是 preflight 的 stale 门要抓的情形）
```

两条路径都必须**保持 raw 与 qfq 的最新交易日同步**：preflight 的
`execution_market_db_stale`（`EXECUTION_MAX_LAG_DAYS = 3`）会直接 BLOCKED，这正是
"raw 链断供"要暴露的信号。

### 6.3 新鲜度与只读消费

- 生产消费方**只读**打开 raw delta（`duckdb.connect(read_only=True)`，由
  `load_daily_panel` / `compute_training_data_fingerprint` 保证）；
- 新鲜度由 preflight 的三条判据把关：
  `execution_latest >= feature_latest - 3d`、`execution_latest >= training_end`、
  指纹逐项与模型封存值一致；
- 写入只能由 updater / import 脚本执行，Alpha V2 侧没有任何写 raw delta 的代码路径。

### 6.4 上线顺序（待授权后执行）

```text
1. 建基线：§6.1（NAS 上用一次性 docker run，产物在新路径）
2. 接通日更：§6.2 任一方案 + 观察一个交易日
3. 冻结模型：--feature-market-db qfq --execution-market-db raw
4. preflight：--market-db qfq --execution-market-db raw --model-dir <新工件>
   → 必须 PASS/WARN 才允许 freeze / 开 epoch
5. validation freeze：--preflight-report <上一步工件>（生产模式必填）
```

## 7. 非目标（本 PR 不做）

- 不改 NAS 数据（不建 raw delta、不动现有 qfq delta、不执行任何部署/epoch 动作）；
- 不处理 NAS 冻结 OOM、不缩短训练窗口、不动 LightGBM 参数、不做模型调优；
- 不触碰 Production Promotion（仍 LOCKED）、不改 Legacy 选股 / Week5 / 生产漏斗 /
  Cross Review / 任何阈值；
- 不在本 PR 里改 `update_vendor_daily_from_tushare.py`（§6.2 方案 A 是**设计**，
  实施属数据侧变更，需单独授权）。

## 8. 验收映射

### 8.1 训练 / 执行 / 预检（P0 主体）

| 用例 | 证明 |
| --- | --- |
| DP-1 | QFQ feature + RAW execution → freeze 放行，两条证据分开 |
| DP-2 | QFQ execution → 在**特征矩阵构造之前**拒绝（特征是会爆炸的探针，未被调用） |
| DP-3 | 声称 raw 但未认证 → 拒绝；完全没有口径声明的库（探针 unknown）同样拒绝 |
| DP-4 | corporate-action 夹具：`ret_1d` == qfq 期望、`net_return_3d` == raw 期望，且两者差值 > 0.3 |
| DP-5 | mature 用 qfq → 函数层抛错 + CLI exit 4，**0 行 outcome 落盘** |
| DP-6 | mature 用 raw → 接受，行上 `price_mode=raw` / `price_mode_certified=true` |
| DP-7 | KPI：`price_mode_certified=false` 的 outcome 不进成熟证据（治理层 + 逐行 + 计数） |
| DP-8 | feature 指纹变化 → preflight BLOCKED |
| DP-9 | execution 指纹变化 → preflight BLOCKED |
| DP-10 | execution 库落后 feature 库（raw 链断供）→ preflight BLOCKED |

### 8.2 Live 运行期（Final R1）

| 用例 | 位置 | 证明 |
| --- | --- | --- |
| LIVE-F1 | `test_alpha_v2_dual_price_series.py` | 冻结 qfq + 当天 qfq → capture 放行，日清单 `contract_ok=true` |
| LIVE-F1b | 同上 | 冻结模型未声明 feature 口径 → capture exit 11（fail closed） |
| LIVE-F2 | 同上 | 冻结 qfq + 当天 raw → exit 11，且 `daily_feature_frame` / `predict_frozen_model_matrix` 探针**从未被调用**、无快照、无日清单 |
| LIVE-F3 | 同上 | feature 库口径不可证（无声明 + 样本不足）→ exit 11 |
| LIVE-F4 | 同上 | scheduler 的 capture argv 必须是 `alpha_v2.feature_market_db`（不是 `market_warehouse.db_path`） |
| LIVE-F5 | `test_alpha_v2_m4l_cycle_clean_day_e2e.py` | 口径漂移 → 当晚 `waiting:feature_price_mode_mismatch` 且不 capture；同晚修回 → 立即 capture + `clean_oos_days=1` |
| LIVE-F6 | 同上 | 漂移到 deadline → `recorded_missing:feature_price_mode_mismatch`、无 capture、`clean_oos_days=0` |
| LIVE-M1 | `test_alpha_v2_dual_price_series.py` | 冻结 qfq + mature feature 库 qfq + execution raw → 放行，行上 raw+certified |
| LIVE-M2 | 同上 | mature feature 库 raw → exit 11 且 0 行新 outcome |
| LIVE-M3 | 同上 | feature 库缺失 / 未配置 → exit 11（**禁止**退回 execution 面板） |
| LIVE-M4 | 同上 | 绕开 CLI 直接调用函数：production/test（含默认值）下 `style_panel=None` → 抛 `OutcomeMaturationError` |
| LIVE-M5 | 同上 | rehearsal + `style_panel=None` → 允许，摘要标 `execution_panel_fallback_rehearsal` |

结构守卫：`test_production_entrypoints_have_no_escape_hatch` 钉住 freeze/mature 两个生产
入口不得出现关闭 execution 守卫的开关。
