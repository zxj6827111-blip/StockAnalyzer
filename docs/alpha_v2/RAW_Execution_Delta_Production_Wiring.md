# RAW Execution Delta 生产接线（P1）

> 状态：**工程层已实施**。RAW 基线**未建**、NAS **未切换**、epoch **未开**。
>
> 一句话：本变更让"feature/qfq delta + execution/raw delta"在同一次夜间事务里锁步推进，
> 并且只有两份都完整、同日、口径正确时 nightly readiness 才允许发布。

---

## 0. 为什么需要第二份 delta

项目价格契约（`stock_analyzer.backtest.price_contract`，P0 已落地为代码守卫）写得很清楚：

```text
Feature Series may be QFQ
Execution Series must be RAW
```

生产 NAS 此前只有一份 delta：`/app/artifacts/vendor_delta/market_delta.duckdb`，
`price_series_mode=qfq`。Alpha V2 的执行侧（label / 成交价 / 净收益 / 超额 / MAE/MFE）
需要**同一批 ZIP 数据产出的 raw 序列**。两条路线都不行：

- 拿 qfq 当成交价 → 除权日的 -50% 跳变被写成真实亏损，训练目标本身被污染；
- 从 qfq **反推** raw → 逆变换依赖因子完整性，缺因子的 symbol 会被"补"出一个假价格。

所以是第二份**物理独立**的库，而不是第二张视图：

```text
feature/qfq  : /app/artifacts/vendor_delta/market_delta.duckdb
execution/raw: /app/artifacts/vendor_delta_raw/market_delta_raw.duckdb
```

两者是同一次 `docker run` 里的两个角色，**不共享数据库事务**——"原子性"落在
readiness 的发布条件上：任一份失败，整晚不放行（§5）。

---

## 1. 架构

```text
Tushare / Vendor ZIP update      scripts/update_vendor_daily_from_tushare.py
        ↓                        （--batch，一次统一调用）
daily_index rebuild              _update_last_date_index
        ↓
Feature QFQ delta incremental    import_vendor_zip_to_delta.py --incremental --price-series-mode qfq
        ↓                        role=feature   → vendor_delta/market_delta.duckdb
Execution RAW delta incremental  import_vendor_zip_to_delta.py --incremental --price-series-mode raw
        ↓                        role=execution → vendor_delta_raw/market_delta_raw.duckdb
validate QFQ + RAW               ops/nightly_readiness.write_nightly_readiness（自己开库核对）
        ↓
nightly readiness publish        artifacts/runtime/nightly_data_ready.json（schema v3）
```

三个角色分工：

| 组件 | 职责 | 不做什么 |
| --- | --- | --- |
| `scripts/update_vendor_daily_from_tushare.py` | 编排两个 delta 角色、把口径写死在编排层、发布/拒绝 readiness | 不自己判定库的口径与覆盖 |
| `scripts/alpha_v2_raw_delta_coverage.py` | 建基线时的覆盖认证（只读校验 + marker 生成） | 不写数据、不参与日常增量 |
| `src/stock_analyzer/ops/raw_delta_baseline.py` | marker 身份模型 + 符号集合摘要 + 库事实实测 | 不做任何写入决策 |
| `src/stock_analyzer/ops/nightly_readiness.py` | release 级判定：开两份库逐项核对 | 不信 updater 的自述字段 |

### 角色与价格口径是**编排**决定的，不是配置决定的

```text
DELTA_ROLE_PRICE_SERIES_MODE = {feature: qfq, execution: raw}
```

两个角色走同一条代码路径，唯一差别就是 `--price-series-mode` 参数。这一点必须显式：
如果依赖 `config/default.yaml` 的默认值，则"某天改个配置，raw 目标被喂成 qfq"不会有
任何一步报错——写进去的行仍自称 qfq，而库名还叫 raw。

---

## 2. 基线生命周期（bootstrap）

### 2.1 建基线（每份库**一次**）

```bash
# 1) 全量导入（深度按 §3 的覆盖判据算，不要拍整数）
python scripts/import_vendor_zip_to_delta.py \
  --data-root /data \
  --index-path /app/artifacts/vendor_overlay/daily_index.json \
  --delta-db-path /app/artifacts/vendor_delta_raw/market_delta_raw.duckdb \
  --price-series-mode raw \
  --limit-days <按覆盖判据算出的深度>

# 2) 覆盖认证 + 写 bootstrap marker（注意：--write-marker 是唯一落 marker 的入口）
python scripts/alpha_v2_raw_delta_coverage.py \
  --raw-db     /app/artifacts/vendor_delta_raw/market_delta_raw.duckdb \
  --feature-db /app/artifacts/vendor_delta/market_delta.duckdb \
  --index-path /app/artifacts/vendor_overlay/daily_index.json \
  --source-window-start 2024-11-14 --source-window-end 2026-08-31 \
  --write-marker
```

第 2 步必须 PASS 才落 marker。**覆盖不足 → 扩大 `--limit-days` 并重建**，不要打补丁式
追加（半截历史 + 新历史拼出来的序列没有任何一层能证明它完整）。

### 2.2 marker 是什么

`artifacts/vendor_delta_raw/raw_delta_bootstrap.json`（与库同目录，两者必须一起搬）：

```json
{
  "schema": "alpha_v2_raw_delta_bootstrap.v1",
  "price_series_mode": "raw",
  "db_path": ".../market_delta_raw.duckdb",
  "db_content_identity": {"rows": ..., "symbols_total": ...,
                          "actual_min_date": ..., "actual_max_date": ...,
                          "symbol_set_hash": "..."},
  "required_source_window": {"start": "2024-11-14", "end": "2026-08-31"},
  "actual_min_date": "...", "actual_max_date": "...",
  "symbols_expected": ..., "symbols_covered": ..., "rows": ...,
  "price_mode_check": {"expected": "raw", "observed": "raw", "certified": true,
                       "decision_rule": "panel_rows_declare_raw", "evidence": {...}},
  "coverage_status": "PASS",
  "source_index_path": "...", "source_index_hash": "...", "source_index_latest_date": "...",
  "build_commit": "..."
}
```

**身份用内容事实，不用整库 SHA256**：raw 库每晚都在长，对数百 MB 的 DuckDB 每晚重算一次
全文件摘要纯属开销。marker 里的取证快照（行数 / 日期区间 / 符号摘要）如实记录建基线当刻的
状态，**不参与每日校验**——它们本来就该随增量变化。每日校验只看不变量（§4.1）。

---

## 3. 覆盖判据（source window）

生产候选模型的口径：

```text
decision window : 2025-06-02 .. 2026-08-31
warmup          : 200 natural days
source window   : 2024-11-14 .. 2026-08-31
```

`--limit-days` 是**每 symbol 的行数**，不是自然日；"400 比 Week5 的 240 大"不构成任何
覆盖证明。正式判据全部是实测事实：

| # | 检查 | 判据 |
| --- | --- | --- |
| 8.1 | DB | 存在 / 可读 / `daily_bars` 在 / `(symbol,date)` 无重复 |
| 8.2 | 价格口径 | 行内声明恰好是 `raw`（非 `qfq`/`mixed`/`unknown`），并用 Alpha V2 的 `certify_price_mode()` 独立佐证 |
| 8.3 | 窗口 | `actual_min_date <= source_window_start` 且 `actual_max_date >= source_window_end` |
| 8.4 | 符号 | required = **feature 库在同一窗口内的符号集合**；缺一个即 BLOCKED |
| 8.5 | 行 | `(symbol,date)` 逐对比较：feature 有而 raw 没有 = 数据缺口；两边都没有 = 停牌 |

### 8.5 为什么用"逐对比较"而不是交易日历

不引入日历、也不假设"每天都该有 bar"。raw 与 qfq 来自同一批 ZIP，**行集合理应一致**
（口径只改价格数值，不改哪些行存在），于是：

```text
feature 有、raw 没有    → 数据缺口（真问题，BLOCKED，列出前 N 个样例）
两边都没有              → 停牌 / 未上市 / 无交易（正常，不报）
raw 有、feature 没有    → 正常：qfq 侧因子缺失的 symbol 会被跳过，raw 不需要因子
```

这条规则顺带解释了为什么 v3 readiness 用**包含链**而不是三方全等（§4.2）。

---

## 4. 增量生命周期与身份门

### 4.1 生产增量前的身份门（fail closed）

`--sync-vendor-delta-raw` 每次运行都会先验基线身份，六项任一不成立就整晚 fail closed：

| 项 | 判据 | 原因码 |
| --- | --- | --- |
| 库存在 | 是文件 | `raw_delta_baseline_missing` |
| marker 存在且可解析 | JSON 对象 | `raw_delta_baseline_marker_unreadable` |
| schema | `alpha_v2_raw_delta_bootstrap.v1` | `raw_delta_baseline_marker_schema_mismatch` |
| 口径 | marker 与 `price_mode_check` 都声明 raw | `raw_delta_price_mode_invalid` |
| 覆盖结论 | `coverage_status == PASS` | `raw_delta_coverage_blocked` |
| 库仍是那份库 | `daily_bars` 在、**行数与符号数不少于**建基线记录值、当前行内口径仍是 raw | `raw_delta_db_identity_mismatch` / `raw_delta_price_mode_invalid` |

最后一条是**单调不变量**，没有阈值：逐日增量只会让计数增长，所以正常推进碰不到它；
而"库被清空 / 被换成另一份 / 被指向 qfq 库"三种事故都会立刻撞上。

顺序有意如此：先证"还是同一份库"，再证"这份库是 raw"。反过来会让"库被清空"报成
口径未知，把真正的故障类型藏在一个次要现象后面。

**为什么必须在导入之前**：`import_vendor_zip_to_delta.py --incremental` 对目标库里还没有
基线的 symbol 会走 `full_import_symbols` 并用 `--limit-days` 补导。空路径上的第一次生产
运行因此会"成功"造出一份只有默认浅深度的 raw 基线——它看起来是 raw、行数也像样，却覆盖
不到 source window。这种"半基线"比缺库更难发现，因为没有任何一步报错。这条门就是封堵点，
**绝不用增量偷偷初始化**。

### 4.2 两个角色的推进条件

```text
两个角色只要「索引进度可信」就跑，不以「本次 ZIP 有没有新行」为条件。
```

理由是重试语义（§5.2）：第一晚 raw 失败、第二晚 ZIP 已经追平，"没有新行"并不等于
"没有事要做"——落后的那个角色必须能在同一路径上收敛。无事可做时 importer 自己是廉价的
空转（逐符号日期比较后直接跳过）。

### 4.3 RAW 特有的纪律：不做因子漂移重写

qfq 侧有一条历史重写通道：除权会重标定**整段历史**，所以 `_factor_value_on_anchor`
发现锚点因子 ≠ 1.0 时会用 `overwrite_existing=True` 重写该 symbol 的全部历史行。

**raw 没有因子可漂移。** `--price-series-mode raw` 时这条通道整体关闭，并如实上报：

```json
{"price_series_mode": "raw",
 "factor_drift_detection": "disabled_non_qfq_mode",
 "drift_refreshed_symbol_count": 0}
```

代码里还有一条兜底不变量：非 qfq 角色若产出任何 drift symbol（说明门被绕过），
直接抛 `DataSourceError`，而不是继续走 `overwrite_existing` 把历史行覆盖成半截数据。
raw 只按真实新增 bar 增量推进。

---

## 5. Readiness schema 与失败/重试语义

### 5.1 schema v3

```json
{
  "schema_version": 3,
  "target_trade_date": "2026-08-19",
  "daily": {"ok": true, "latest_trade_date": "2026-08-19"},
  "index": {"ok": true, "symbol_set_hash": "..."},
  "delta": {"ok": true, "role": "feature", "price_series_mode": "qfq",
            "latest_trade_date": "2026-08-19", "symbol_set_hash": "..."},
  "execution_delta": {"ok": true, "role": "execution", "price_series_mode": "raw",
                      "latest_trade_date": "2026-08-19", "symbol_set_hash": "..."},
  "symbol_membership": {
    "symbols_expected": 5541, "symbols_feature": 5541, "symbols_execution": 5541,
    "symbol_set_hash_expected": "...", "symbol_set_hash_feature": "...",
    "symbol_set_hash_execution": "...",
    "missing_feature": [], "missing_execution": [], "feature_not_in_execution": [],
    "membership_locked": true
  },
  "raw_delta_baseline": {"ok": true, "schema": "...", "coverage_status": "PASS",
                         "required_source_window": {...}},
  "delta_db_path": "...", "execution_delta_db_path": "...", "index_path": "..."
}
```

**只增不改**：`delta` 继续表示 feature/qfq；执行侧一律是新键 `execution_delta`。
不带 `--sync-vendor-delta-raw` 时仍写 v2，历史文件与既有消费者不受影响。

### 5.2 成员锁步是**包含链**，不是三方全等

```text
index_expected ⊆ feature ⊆ execution
```

- `index_expected` 是"当天应该有的票"（沿用既有 hollow 规则：`entries` 为空的新股占位不计）；
- feature 缺一只 → 那天少一个决策样本 → 拦；
- execution 缺 feature 有的 → label 算不出来 → 拦；
- **execution 多出来的不算错**：raw 侧不需要复权因子，所以 qfq 侧因因子缺失被跳过的
  symbol 在 raw 侧照样有行。要求三方全等会把这条**正常**路径判成故障，每晚误杀。

包含链同时干掉了旧口径的漏洞：旧判据只比 `symbols_on_target_date` 的**计数**，
`{A,B}` 与 `{A,C}` 计数相同、成员不同，会被静默放行。链式判据下这种情形必然留下
非空的缺失列表。审计块里同时记录三方的计数、符号集合摘要与前 N 个缺失样例
（大集合整体不写进报告）。

### 5.3 每条门拦住什么

| 场景 | 结果 | 谁拦的 |
| --- | --- | --- |
| execution 库实际 qfq | 不发布 | readiness（`price_series_mode` 分布 ≠ `{raw}`） |
| execution 库混口径 | 不发布 | readiness（同上；报错里给全表口径分布） |
| feature 库实际 raw | 不发布 | readiness（分布 ≠ `{qfq}`） |
| 两份 delta 最新交易日不一致 | 不发布 | readiness（各自 vs `target_trade_date`） |
| 两份 delta 计数相同、成员不同 | 不发布 | readiness（成员锁步） |
| 任一角色导入失败 | 不发布 | updater（`full_run_ok` 含两个角色） |
| raw 基线缺失 / marker 缺失 | 非零退出，不发布 | updater（导入之前） |
| v3 文件缺 `execution_delta` 块或块不 ok | gate 不 ready | `check_nightly_readiness`（不许按 v2 降级放行） |

口径门读的是**全表**分布，不按目标日过滤：一份库里混进一行另一种口径，意味着按这份序列
算出来的特征/label 在跨越那一行时不连续。门要回答的是"这份库能不能当那个角色的序列"，
而不是"今天新增的行对不对"——写入口（`market_warehouse` 的逐 symbol 口径门禁）负责不
让新的污染进来，readiness 负责不让已有的污染被 release。失败信息里带全表口径分布
（例如 `qfq=2400000, raw=12`），所以第一次撞门就能直接定位到那 12 行。

### 5.4 失败与重试

```text
不要求两份 DuckDB 共享事务。允许 QFQ 已更新 / RAW 更新失败，
但此时：NO readiness。第二次重试幂等收敛。
绝不允许：QFQ success + RAW failure → readiness success。
```

- 任何非 dry-run 运行**开头**就 `invalidate_nightly_readiness()`（fail-closed），
  所以一次失败的更新不会留下可被 21:45 selector 消费的旧 ready 文件；
- 两个角色都会被尝试（不短路），重试是一次完整重跑；
- 增量导入按 `(symbol,date)` 幂等：重试不会写出重复行（`coverage validator` 也把
  `duplicate (symbol,date) != 0` 列为 BLOCKED）。

---

## 6. NAS 切换顺序（**PR merge ≠ NAS cutover**）

本 PR 只交付工程实现。**合入 main 不等于上线**：`nas_stock_updater.sh` 已经改成 dual
delta，但在 NAS 上直接跑会整晚 fail closed（`raw_delta_baseline_missing`）——这是设计
行为，不是缺陷。

顺序不能反：

```text
1) 拉新代码 → 重建镜像（新代码必须在镜像里）
2) 在新镜像里建 RAW 基线（§2.1 两步），产物落在 artifacts 卷：
     /app/artifacts/vendor_delta_raw/market_delta_raw.duckdb
     /app/artifacts/vendor_delta_raw/raw_delta_bootstrap.json
3) coverage validator PASS（+ marker 落盘）
4) python scripts/alpha_v2_raw_delta_coverage.py --verify-marker --raw-db <raw 库>  再次确认
5) 安装/切换到受管的 nas_stock_updater.sh（scripts/nas_deploy_update.sh 原子安装）
6) 观察一个交易日：更新日志出现 [verify] dual delta ok，readiness 文件是 schema v3
```

**不允许**用第二个 cron 或独立 shell 脚本单独推 raw。那会重新产生"QFQ ready / RAW 未
ready / selector 已 release"的竞态——正是本变更要消除的东西。

### 6.1 配置

生产 NAS 绝对路径不写进 Python 默认值，走既有环境变量约定：

```text
SA__ALPHA_V2__FEATURE_MARKET_DB=/app/artifacts/vendor_delta/market_delta.duckdb
SA__ALPHA_V2__EXECUTION_MARKET_DB=/app/artifacts/vendor_delta_raw/market_delta_raw.duckdb
SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE=raw
```

marker 路径**故意没有**环境变量覆盖：默认与 raw 库同目录，需要改时走 CLI
（`--write-marker --marker-path` / updater 的 `--raw-baseline-marker`）。marker 是
"这份库是经认证的基线"的唯一凭据，多一个来源就多一处"我以为指向的是那个文件"的含糊。

---

## 7. 回滚

按代价从低到高：

1. **只想停用 raw 角色**（回退到单 delta）：把 `scripts/nas_stock_updater.sh` 里的
   `--sync-vendor-delta-raw` 一行去掉并重新安装。readiness 回到 v2，Week5 / 夜扫 /
   Alpha V2 capture 全部不受影响（它们读的是 `nightly_data_ready` 是否 ready）。
   raw 库与 marker 留在卷里，不影响任何消费者。
2. **回到旧镜像**：`scripts/nas_deploy_update.sh --branch <旧 ref>`（或部署脚本提供的
   回滚点）。artifacts 卷里的 raw 库/marker 是新增文件，不参与旧代码路径。
3. **彻底清掉 raw 基线**：删除 `vendor_delta_raw/` 整个目录。下次要用必须从 §2.1 重建
   ——这也是为什么"半基线"在设计上不可达。

任何回滚都**不改变** feature/qfq delta 的路径与语义，也不改 Week5 选股 / 生产漏斗 /
cross review / 任何阈值。

---

## 8. 运维检查清单

```bash
# 基线身份（离线可跑，不写任何东西）
python scripts/alpha_v2_raw_delta_coverage.py --verify-marker \
  --raw-db /app/artifacts/vendor_delta_raw/market_delta_raw.duckdb

# 覆盖复核（不落 marker）
python scripts/alpha_v2_raw_delta_coverage.py \
  --raw-db <raw> --feature-db <feature> --index-path <index> \
  --source-window-start 2024-11-14 --source-window-end 2026-08-31 \
  --json-out /tmp/raw_coverage.json

# 看今晚两个角色到底推了什么
python -c "import json;d=json.load(open('/vol1/docker/tools/logs/updater_last.json'));\
print(d['ok'], d['target_trade_date'], d['dual_delta_enabled']);\
print(d['feature_delta_sync']); print(d['execution_delta_sync'])"

# 看 readiness 文件本身
# /app/artifacts/runtime/nightly_data_ready.json  → schema_version 必须是 3
```

日志判读：

```text
[verify] dual delta ok: execution=... mode=raw      → 两个角色都推进了
[verify] NOT releasable: dual delta enabled but execution role updated=False ...
                                                    → raw 角色没推进（看 execution_delta_sync.reason）
execution_delta_sync.reason = raw_delta_baseline_missing          → 基线没建/没挂上
execution_delta_sync.reason = raw_delta_baseline_marker_unreadable → marker 丢了（库在）
execution_delta_sync.reason = raw_delta_db_identity_mismatch       → 库被清空/替换
execution_delta_sync.reason = raw_delta_price_mode_invalid         → 指向了 qfq 库
```

---

## 9. 不变量（本变更不得触碰）

```text
Legacy final output changed            = NO
Week5 selection changed                = NO   （300/100/50、夜扫、漏斗、晚报都不动）
Production Funnel changed              = NO
Night scan timing changed              = NO

Existing QFQ path unchanged            = /app/artifacts/vendor_delta/market_delta.duckdb
RAW path independent                   = /app/artifacts/vendor_delta_raw/market_delta_raw.duckdb

Alpha final selection enforcement      = FALSE
Production Promotion                   = LOCKED
```

Week5 / 夜扫只问"`nightly_data_ready` 是否 ready"，不读 block 细节；v3 只是把"数据事实"
从"feature 就绪"扩成"feature 就绪 **且** execution 就绪"。

### 已知遗留（不在本阶段）

- `shadow capture.signal_close_raw` 仍取自 feature 面板。它目前只进入 `entry_gap_mean`
  等诊断，不参与 Clean OOS 资格 / 净收益 / 超额 / Rank IC / MAE/MFE / L20-L250；
  本阶段不扩大 capture 依赖，记入 backlog：**diagnostic field naming / raw execution
  enrichment debt**。
- `import_vendor_zip_to_delta.py` 仍会把"无基线的 symbol"按 `--limit-days` 全量补导
  （新股正常路径）。基线级的保护由 marker 的单调不变量承担，而不是逐符号计数。
- `ops/runtime_invariants.check_readiness`（运维不变量面板）目前只看
  `daily`/`index`/`delta` 三个槽位。v3 文件**能正常通过**（不破坏既有语义），但面板
  还不会把 `execution_delta` 的结论一并展示。补这一格属运维可观测性改进，不在本阶段
  的允许改动范围内，留作后续小改动。
- `check_nightly_readiness` 接受 v2 与 v3 两个版本。**要判定"今晚该不该有执行侧"必须
  看 `schema_version`**：v3 缺 `execution_delta` 会被拒，v2 不受影响。这是有意设计
  （§14 向后兼容），代价是"双 delta 已启用"这件事不在 readiness 文件之外单独声明。
