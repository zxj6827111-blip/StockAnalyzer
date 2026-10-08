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
