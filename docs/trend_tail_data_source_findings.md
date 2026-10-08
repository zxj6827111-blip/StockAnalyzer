# 尾盘链路数据来源实测（NAS 只读巡检 → 本地研究库）

As-of: 2026-10-08 @ HEAD `5e2a032`（分支 `feat/stock-selection-quality-overhaul`）

本篇只记**实测读数**与它们的含义，不替代 `docs/selection_quality_plan_status.md` 的逐项台账。
所有远端操作均为只读：`scripts/nas_exec.py` 跑探针 + SFTP 复制到本机
`artifacts/research/`（gitignored）。没有重启容器、没有改生产仓库、没有写 NAS 任何文件。

---

## 1. 生产仓库的真实形状（`/app/artifacts/warehouse/market.duckdb`，677 MB）

表：`daily_bars` `daily_trade_status` `security_status` `security_identity_mapping`
`index_daily` `intraday_summary_1m` `intraday_summary_5m` `financial_snapshots`
`moneyflow` `hk_hold` `margin_detail` `top_list_events` `top_inst_events` `block_trade_events`。

| 实测项 | 读数 | 对计划的意义 |
| --- | --- | --- |
| `daily_bars` 行数 / 符号 / 日期跨度 | 10,535,661 行、5,571 个符号、2016-01-04 → 2026-09-30 | 日线足够长，历史重放有底 |
| `daily_bars.up_limit/down_limit`（最近 30 个自然日） | **0 / 121,844 填充** | 精确涨跌停在日线表里**没有落地** |
| `daily_bars.price_series_mode`（同窗口） | 121,844 行全为 NULL | **无法声明这批价格是 RAW 还是 QFQ** —— §3.1「不用 QFQ 模拟成交」在这里无法证明 |
| `daily_bars.suspended` / `is_st` | 0 NULL；`suspended` 真值 1 行、`is_st` 真值 2,572 行 | 停牌/ST 是**显式声明列**，缺的只是覆盖面 |
| `daily_trade_status` | 68 行、2 个符号、48 个交易日，列为 `up_limit/down_limit/suspended/suspend_type/...`，**没有 `trade_status` 列** | 停复牌声明存在但覆盖面≈0；此前"无 trade_status 列"的说法需要按此精确化 |
| `security_status` | **0 行** | 证券历史状态/退市覆盖为空 ⇒ §2 股票池的 survivorship **不能声明 complete**（`delisting_coverage_verified=False` 是对的） |
| `intraday_summary_1m/5m` | 566,537 / 266,167 行，列是 `session_return`、`last30_return`、`minute_count` 等**日级聚合**，无时刻列 | 这两张表不能用来定位 14:30–14:50 |
| 生产 `funnel_trace_*.json` | **0 份** | 读侧 `verify_trace()` 与 `digest()` 加 `blocking` 字段后，没有旧留档会被误判；也说明影子链路从未在生产落盘 |
| 最新夜扫报告 `2026-09-30/nr-20260930-01.json` | 无 `universe_snapshot` / `universe_quality_selection` / `night_funnel_trace` 键 | 部署镜像早于本分支；接线效果只能在本地按报告契约验证，不能声称已在生产生效 |

## 2. 关键发现：带时刻的分钟行情**一直存在**，只是没落进仓库

容器挂载里有两个源：

```text
/vol1/1000/股票数据/output/minute_raw        → 容器 /data/qq_minute_raw
/vol1/1000/股票历史数据                        → 容器 /data/vendor_history
```

- `minute_raw/1m_YYYYMMDD/SH#600000.csv`：表头 `date,time,open,high,low,close,volume`，
  09:30–15:00 共 241 行，**含完整 14:30–14:50 窗口**；2026-09-30 当日 4,540 个文件
  （SH 1,681 / SZ 2,859）；目录跨度 2025-11-14 → 2026-10-07，共 215 个（部分节假日目录为空）。
- `/vol1/1000/股票历史数据/沪深分钟数据/Stock_1min_2000-now/*.zip`：按年/按月归档，
  内部 `2026-01/20260116_1min/sh688136.csv`，表头
  `datetime,code,name,open,close,high,low,volume,amount,pct_chg,amplitude`，
  `datetime` 形如 `2026-01-16 09:30:00`。年包 2024/2025，月包 2026-01…2026-07。
  **目录名正好匹配 `archive_paths()` 的 `Stock*_{interval}-now` 约定**，
  `scripts/sync_tail_minute_bars.py` 无需改代码即可消费。

bar 时刻语义是**实测判定**而不是猜的：09:30 是开盘集合竞价 bar，
09:31 的 open（34.64）等于 09:30 的 close，09:32 的 open 又等于 09:31 的 close，
14:59 量为 0、15:00 是收盘集合竞价 ⇒ 时刻标注的是**区间完成时刻**，
即 `--bar-time-semantics bar_end`；量单位是手（amount/volume≈100×price）⇒ `volume_multiplier=100`；
价格是未复权 ⇒ `--price-basis raw`。

**这推翻了"尾盘策略验证被数据卡住"的原表述**：分钟带时刻的历史源在 NAS 上存在且可本地化，
真正的阻塞点转移到了参考数据（见 §4）。

## 3. 缺的那一环：按历史日期生成重建请求的生产者

`rebuild_tail_labels.py --requests` 与 `validate_tail_selection_quality.py --samples`
都存在且有测试，但**仓库里没有任何东西按历史日期生成过它们** —— 所以 §4 的
"≥4 折 / +5pp / 60 天影子"此前是没有输入的空门槛。本次补上
`scripts/replay_tail_candidate_pool.py`（提交 `5e2a032`）。

本地实测（用仓库副本 + 流动性前 900 个符号）：

- 2025-01-06 → 2026-06-30、池 300：356 个决策日、**106,800 条请求**。
- 2026-01-05 → 2026-05-28、池 60：94 个决策日、**5,640 条请求、208 个不同符号**。
- 硬门出局符号日：`min_avg_turnover_20` 95,186、`min_float_market_cap` 31,082、
  `is_st` 1,138、`overextension_risk` 713、`board_eligibility` 574、`suspended` 0。
- 过程中被实测揪出并修掉的两个自身缺陷：
  `stale_market_data` 原先拿"全局最后一根 bar"比对当日，误杀 317,702 个有效符号日
  （改成"距上一根日线断档 > 30 个自然日"）；`daily_bars` 混着指数代码
  （899050 = 北证50）被选进池，改为按板块前缀走 `board_eligibility` 显式出局。

## 4. 现在真正阻塞 §4 的是参考数据，不是分钟行情

`sync_tail_reference_data.py --warehouse 本地副本` 落库结果（退出码 3 = 只补到部分）：

| 来源 | 落库 |
| --- | --- |
| `ref_trade_calendar` | 116 行（116 个开市日）✅ |
| `ref_limit_prices` / `ref_suspend_status` | 各 59 行、**2 个符号**（就是 `daily_trade_status` 的全部覆盖面） |
| `ref_daily_bars_raw` | **0 行** —— `daily_bars` 存在但 `price_series_mode` 全 NULL，价格口径无法证明是 RAW，同步器按 ADR-002 拒绝搬运 |
| `ref_security_status` | **0 行** —— 源表本身为空 |

报告原文：`来源不足: ['daily_bars_table_missing', 'security_status_table_missing']`，
`approximated_limit_price_rows = 0`（没有用近似值顶替），
`suspend_rows_without_trade_status = 59`。

结论与下一步（按可行性排序）：

1. **RAW 日线有现成替代源**：`/vol1/1000/股票历史数据/全A日K/*.zip`（未复权，
   复权因子单列在 `复权因子/`）与 `minute_raw` 的分钟 RAW 同口径。
   需要给 `sync_tail_reference_data.py` 增加"vendor 日线 ZIP"这一来源，
   让 `ref_daily_bars_raw` 有可证明口径的行 —— 这是解锁 §4 的第一优先代码项。
2. **精确涨跌停**只有 2 个符号：来源应是 tushare `stk_limit`（doc_id=183）批量补采，
   注意 `scheduler-*` 容器解析不了 `api.waditu.com`，补采入口不能放在那两个容器里。
3. **退市/证券状态**：`security_status` 空表，§2 的 survivorship 在补到数据前
   只能声明 `incomplete_or_unknown`。
4. 分钟入库吞吐：900 符号 × 1.5 年的整包读取会退化成逐成员解析（RSS 6 GB、13 MB/分钟），
   已按"只取请求实际用到的 208 个符号 + 5 个月窗口"收敛。

## 5. 状态（AGENTS.md §9.1 口径）

- 工程验收：**通过**（13 个场景测试钉住，未变）。
- 数据与契约修复：§3.1 的**校验器已实测可用**，本次把"缺什么"从推测变成读数。
- 选股质量验收（≥4 折 / +5pp / CI 下界>0）：**仍 blocked**，但阻塞点已改名为
  `ref_daily_bars_raw` + 涨跌停覆盖 + 证券状态三项参考数据；分钟行情不再是阻塞项。
- 影子验证（60 天 / 100 笔）：**blocked（0 天 / 0 笔）**，且必须用未来真实观察，
  历史分钟数据再多也不能顶替（计划原文）。
- 未部署、未改生产；本地 `artifacts/research/` 里的副本与中间产物不入库。

---

## 6. 本篇写完之后的落地进展（同一轮内，提交 `2a69d8a`）

§4 第 1 条已不再只是"下一步"：`sync_tail_reference_data.py` 现在接受
`--vendor-daily-root`，从 `全A日K/{2025,2026}.zip` 读**可证明口径**的原始价日线。
成员发现复用 `build_vendor_zip_daily_index`（含同名包去重），数量倍率沿用
`VendorZipOverlayProvider` 的声明而不是另抄常数（slots dataclass 的类属性是
`member_descriptor`，默认值只能从 `dataclasses.fields()` 取 —— 代码里写明了这点，
否则下一个人又会照着 `Provider.daily_volume_multiplier` 直接 float() 而炸在运行期）。

本机实测（208 个请求符号，2026-01-01..2026-06-30）：

| 项 | 结果 |
| --- | --- |
| `ref_daily_bars_raw` | **24,086 行 / 208 个符号 / 2026-01-05 → 2026-06-30** |
| `source` / `price_basis` | `vendor_zip_daily_raw` / `raw` |
| `daily_bars` 缺口 | 由 `daily_bars_table_missing` 变为 **sufficient** |
| 剩余缺口 | 仅 `security_status_table_missing`（源表本身为空） |
| 未声明的证券状态 | `is_st` / `is_delisting_risk` 留 NULL —— 没声明不等于"不是 ST" |
| 回归测试 | `test_vendor_daily_raw_source_declares_units_and_leaves_undeclared_flags_unknown`（手→股 ×100、千元→元 ×1000、万元→元 ×10000 三条换算各自有断言） |

单位换算与真实数据对得上是这次接线的关键判据：605081 在 2026-01-05 的
`volume 30906.45 手 → 3.09M 股`、`amount 32908.135 千元 → 3.29 亿元`，
按均价 10.6 元反算正好吻合；`circ_mv` 万元 ×10000 → 154 亿流通市值合理。
所以倍率不是猜的，是和源数据一起被验证过的。

---

## 7. §4 的第一批真实读数（2026-01-05 → 2026-05-28 决策日，重建样本）

链路第一次端到端跑通：`replay_tail_candidate_pool → sync_tail_reference_data（含 stk_limit 补采）
→ sync_tail_minute_bars → rebuild_tail_labels → assemble_tail_samples →
validate_tail_selection_quality`。缺的第三个生产者（把特征快照和标签拼成验证器样本的
joiner）这次补上了：`scripts/assemble_tail_samples.py`。

数据面：分钟库 **6,464,825 行 / 208 符号 / 129 个交易日**，尾盘窗口
26,825/26,825 个符号日**完整**；参考库 `ref_daily_bars_raw` 28,861 行、
`ref_limit_prices` 28,912 行（tushare `stk_limit` 补采 139/139 天 0 失败）、
可交易声明 28,861 行（只从"当天确有成交的 RAW 日线"正向声明，缺 bar 的日子一律不写）。

| 读数 | 值 |
| --- | --- |
| 重建请求 / 标签记录 | 5,640 / 5,613（27 个符号日有日线断档、3 条缺入场日日线） |
| **成交率** | **48.9%**（2,742 filled / 5,613） |
| 未成交原因 | `below_lot_size` 2,801、`limit_up_locked` 70 —— 1 万元参考额在高价股上买不满一手 |
| 可训练（已成熟且判得动） | 2,742 |
| **净盈利率（无条件，池内）** | **37.78%**（1,036 / 2,742，已扣冻结成本表的费用与滑点） |
| LR 基线校准窗口 AUC | **0.4990** → 训练器按 §3.3 直接停机，不产出塌缩概率模型 |
| 四组信息里最强的单特征 | `avg_turnover_20` AUC 0.5416、`amount_to_float_cap` 0.5359；`float_market_cap` 0.4548（反向） |
| 各组最强 \|AUC−0.5\| | market_relative 0.0366 / trend_position 0.0353 / volume_liquidity 0.0452 / volatility_overheat 0.0251 |

（AUC 自检：完美分数 1.0、反向 0.0 —— 第一版手写秩统计把秩按排序位置而非原始顺序赋值，
导致所有特征 AUC 相同，已改 `pandas.rank` 重算。）

这三条读数合起来是 §4 的第一份**实证**结论，而不是一句"数据不够"：

1. 池内无条件净盈利率 37.8%，离准入阈值 0.60 差 22 个百分点；
2. 四组可复现行情信息的排序信号都在 AUC≈0.45–0.54 之间，最强的一条只有 0.542；
3. 因此"仅靠重排同一批候选"要拿到 ≥5pp 提升、把净盈率推到 0.60 附近，
   在这段真实数据上不成立 —— 质量门拒绝给数字（rc=5）是正确行为，不是流水线坏了。

工程侧的结论与之分离：**工程验收仍是通过的**（13 个场景测试 + 本轮新增的生产者与
fail-closed 行为都有测试钉住）；**选股质量验收现在是"已测量、未达标"**，
不再是"无数据、不可测"。影子验证仍必须是未来真实观察（0 天 / 0 笔）。
