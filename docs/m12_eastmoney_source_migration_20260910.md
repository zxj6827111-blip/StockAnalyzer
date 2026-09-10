# 东财数据源受限的影响范围与替代方案（2026-09-10）

## 0. 结论摘要

1. **"东财被封"需要收窄**：被切断的只有 `push2*.eastmoney.com` 行情主机族
   （push2 / 17.push2 / 82.push2 / push2his 全部 `RemoteDisconnected`，
   0.0s 即断，属连接层封锁）；`datacenter.eastmoney.com`、
   `datacenter-web.eastmoney.com`、`quote.eastmoney.com` **均可达**。因此财务、
   龙虎榜、北向等走 datacenter 的东财接口并未失效。
2. **radar 连败的真因是两个代码缺陷，不是东财封锁**——东财只是触发器：
   - `_normalize_snapshot_time` 把不带时区的**北京时间**按 UTC 解析，
     快照时间戳整体超前 8 小时，age 恒为 −28800s，被 fail-closed 判 `stale`；
   - `_fetch_batch_frame` 把 akshare 整个循环包在一个 `try` 里，
     `stock_zh_a_spot_em` 抛异常会跳到 `except`，**排在后面的备源从未被执行**，
     "多源兜底"形同虚设。
3. **tushare 可用且是最优替代**，但**不能替代实时行情**：`realtime_quote`
   返回 `40101`（无权限），tushare 本身是 T-1 日频。
4. 板块成分改由 tushare 同花顺/申万名录解析，NAS 实测 **22 个板块解析出 20 个**；
   余下 2 个（国产芯片、中芯概念）是同花顺**无等价名**，不是接口问题。

## 1. 影响面盘点（按数据源而非按模块）

| 依赖 | 走哪个东财主机 | 现状 | 处理 |
|---|---|---|---|
| `efinance` 全市场快照 | push2 | **间歇不可达**（同日 13:31 失败、13:36 成功） | 保留为主源，失败即快速退避 |
| `akshare.stock_zh_a_spot_em` | push2 | 不可达（~5s 后 ConnectionError） | 修复备源链路后不再是单点 |
| `akshare.stock_board_*_em` | push2 | 不可达（4.2~16.5s） | M12 改走 tushare（见 §3） |
| `akshare.stock_yjbb_em` 等财务 | datacenter-web | 可达 | 无需处理 |
| `akshare.stock_lhb_*_em` 龙虎榜 | datacenter-web | 可达（且已由 tushare `top_list`/`top_inst` 批量覆盖） | 无需处理 |
| `akshare.stock_info_a_code_name` | query.sse.com.cn | 不可达（非东财，上交所侧） | 影响 `financial_adapter` 名称映射，待评估 |

## 2. radar 快照链修复

### 2.1 时区缺陷（radar `stale` 的唯一来源）

efinance 的 `更新时间` 形如 `"2026-09-10 14:32:00"`（北京时间、无偏移）。
旧实现 `pd.to_datetime(text, utc=True)` 把它贴上 `+00:00`，而 `now` 是
`14:31:45+08:00`，于是 `age = now − oldest ≈ −28800s`，超过
`_SNAPSHOT_FUTURE_TOLERANCE_SEC = 60s` → `_snapshot_age_sec` 返回 `None` →
报告 `status=stale`、`realtime_age_sec=null`。

修复：无偏移的完整日期时间按**市场时区**（`app.timezone`，默认 Asia/Shanghai）
解释；带偏移的按自身偏移解析后换算到市场时区。仅时间（`HH:MM[:SS]`）分支原本
就用抓取时刻的日期与时区补齐，口径一致。新增回归测试
`test_snapshot_naive_full_datetime_uses_market_timezone`。

### 2.2 备源链被异常中断（radar `unavailable` 的主要来源）

修复前 akshare 段的错误只有一个笼统的 `akshare:ConnectionError`，无法区分是
哪一支挂掉；且 `stock_zh_a_spot_em` 一旦抛错，同一 `try` 内的
`stock_zh_a_spot_tx` / `stock_zh_a_spot` 都不会被执行。

修复：逐源独立 `try/except`，错误标签精确到接口名（如
`stock_zh_a_spot_em:ConnectionError`），并把源序列扩展为

```
efinance(主) → akshare: stock_zh_a_spot_em(东财) → stock_zh_a_spot_tx(腾讯) → stock_zh_a_spot(新浪)
```

腾讯 `stock_zh_a_spot_tx` 是独立于东财/新浪的第三方源，NAS 实测 **6.2s / 5561 行**
（新浪同口径 16.6~29.2s），使东财被断时仍有数秒级通路。其列名为拼音缩写
（`zxj/zdf/zd/volume/turnover/hsl`），新增 `_prepare_tx_spot_frame` 做列映射与
量纲对齐（成交量 手→股、成交额 万元→元），并由 `最新价 − 涨跌额` 推导昨收
（否则涨跌幅与涨停距离会归零）。回归测试
`test_backup_source_failure_does_not_abort_fallback_chain`。

> 未改动 `market_snapshot_timeout_sec`（30s）：修复备源链后正常路径
> ≈ efinance 快速失败 + 腾讯 6s，30s 足够；仅当 efinance 成功但耗时接近 30s
> 时偏紧，列为观察项。

## 3. M12 板块成分源迁移

东财不可达期间 22 个板块**全部** unresolved，主题→个股映射恒为空。改为：

```
tushare 同花顺(ths_index + ths_member) → tushare 申万(index_classify + index_member)
  → akshare 东财(恢复后自动生效) → 上日缓存(stale) → unresolved
```

- 名录按日落盘（`_catalogues/YYYY-MM-DD.json`），一天只打 3 次 tushare；
  名录全空时不写盘，避免把失败固化一天。
- 成分帧必须显式取 `con_code`：`ths_member` 有 `ts_code`/`con_code` 两列、
  `index_member` 有 `index_code`/`con_code` 两列，泛匹配 `"code"` 会先命中
  **指数自身代码**，把指数代码当成分股返回。
- `index_member` 的历史成分按 `is_new == "Y"` 过滤（申万接口含已调出记录）。
- 别名顺序：**声明的别名优先，规范名兜底**。原因是同花顺存在同名不同粒度实体
  ——`煤化工` 既是行业（884281.TI，8 只）也是概念（`煤化工概念`，112 只），
  若规范名优先会把目标概念板块缩成 8 只（NAS 实测）。
- 不做模糊匹配：`国产芯片` 若模糊匹配到同花顺 `芯片概念`（910 只，约占全市场
  16%），等于把整条电子链注入主题，语义失真。

### 3.1 命名映射表（NAS 真实接口逐个验证，括号内为实测成分数）

| 主题 | 规范名（东财） | 解析来源 | 命中名 | 成分数 |
|---|---|---|---|---|
| geo_oil | 石油行业 | ths 行业 | 油气开采及服务 | 19 |
| geo_oil | 油气设服 | ths 行业 | 油服工程 | 11 |
| geo_oil | 页岩气 | ths 概念 | 页岩气 | 52 |
| geo_oil | 航运概念 | ths 概念 | 航运概念 | 86 |
| geo_oil | 煤化工 | ths 概念 | 煤化工概念 | 112 |
| enso_agri | 农业种植 | ths 概念 | 农业种植 | 93 |
| enso_agri | 农牧饲渔 | 申万 L1 | 农林牧渔 | 104 |
| enso_agri | 天然橡胶 | ths 行业 | 橡胶制品 | 22 |
| enso_agri | 水产养殖 | ths 行业 | 水产养殖 | **3（偏窄）** |
| enso_agri | 种业 | ths 行业 | 种子生产 | 10 |
| heat_power | 电力行业 | ths 行业 | 电力 | 110 |
| heat_power | 火电 / 水电 / 虚拟电厂 | ths | 同名 | 31 / 11 / 157 |
| policy_infra | 工程建设 | ths 行业 | 建筑与工程Ⅲ(A股) | 167 |
| policy_infra | 水泥建材 | ths 概念 | 水泥概念 | 46 |
| policy_infra | 基建工程 | ths 行业 | 基础建设 | 27 |
| policy_infra | 一带一路 | ths 概念 | 一带一路 | 791 |
| supply_chip | 半导体 | ths 行业 | 半导体 | 185 |
| supply_chip | 光刻机 | ths 概念 | 光刻机 | 51 |
| supply_chip | 国产芯片 / 中芯概念 | — | **无等价名** | 0 |

## 4. tushare 能力矩阵（NAS 实测，token 56 字符）

可直接使用：`ths_index`(1111 条名录) / `ths_member` / `index_classify`(511) /
`index_member` / `index_member_all` / `stk_holdernumber`(5500) /
`bak_basic`(5569/日) / `daily_basic`(5550) / `moneyflow_ths`(5209) /
`stk_factor_pro`(5550，含复权与衍生指标) / `limit_list_d`(82) /
`cyq_perf`(5550，筹码分布) / `hm_list`(117，游资名录) / `daily` / `adj_factor` /
`trade_cal` / `stock_basic` / `fina_indicator` / `income` / `top_list` / `top_inst` /
`moneyflow` / `block_trade` / `margin_detail`。

**不可用**：`realtime_quote` → `code=40101 请指定正确的接口名`（无权限）。
结论：tushare 不能承担实时全市场快照，实时链路仍需 efinance/腾讯/新浪。

## 5. 验证证据

- 单测：`tests/test_theme_layer.py`（含 5 个新增 board_resolver 用例，覆盖
  con_code 取值、is_new 过滤、别名优先、名录未命中回落、名录按日缓存）、
  `tests/test_theme_service.py`、`tests/test_week5_automation.py`（含 2 个新增
  快照回归用例）全绿；`test_pipeline.py` / `test_config.py` /
  `test_week5_snapshot_integration.py` / `test_security.py` 全绿。
- 静态检查：`ruff check` 改动文件无新增（仅剩基线既有的 2 处 F811）；
  `mypy` 同一调用集 **186 errors / 32 files → 186 errors / 32 files，零新增**。
- 端到端：NAS 容器内用真实 token 复刻解析序列，22 个板块 **resolved=20/22**，
  唯一两个未命中即上表的"无等价名"项。

## 6. 遗留与待拍板

1. **国产芯片 / 中芯概念** 需要命名决策：接受 `芯片概念`(910 只)、手工给
   `symbols` 精选、或维持 unresolved（当前选择）。
2. **水产养殖** 同花顺仅 3 只（东财原板块约 20 只），偏窄，需换名或改静态清单。
3. `market_snapshot_timeout_sec=30` 对 efinance 成功路径偏紧（实测抓取 21~32s），
   是否需要提到 45~60s 待定。
4. `stock_info_a_code_name`（query.sse.com.cn）不可达，影响
   `financial_adapter` 的名称映射（ST/退市识别）；可改用 tushare `stock_basic`，
   本次未动。
5. `financial_trust_level` 在 night_scan 空池场景下退化为 `missing`（是空池的
   **结果**而非独立故障），与本次改动无关。
