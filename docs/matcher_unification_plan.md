# 统一撮合引擎改造方案

- 状态：评审稿 v1
- 日期：2026-08-10
- 分支：fix/audit-p0-r1
- 决策者摘要见文末第 7 节

## 0. 背景与验收依据

PRD 验收标准明确要求撮合行为一致：

- 验收 #4（implementation_plan.md:715）"无未来函数，回测与线上一致"。
- 验收 #32（implementation_plan.md:743）"动态滑点公式在回测和实时均生效"。
- 验收 #22（implementation_plan.md:733）"回测撮合器执行 T+1 和不可成交规则"。

经代码核实：回测端撮合规则完整（T+1、停牌、涨跌停、动态滑点、价格 tick、手数、最低成交额、止盈止损出场、无未来函数），而线上模拟盘与独立研究脚本各有一套简化实现，且不一致性仅靠事后周报对账度量，不纠正撮合行为。详见第 1 节差距清单。

## 1. 现状差距清单

### 1.1 三套撮合实现定位

| # | 实现 | 位置 | 使用方 |
|---|------|------|--------|
| A | 回测撮合 `ExecutionMatcher` | `src/stock_analyzer/backtest/matcher.py`（390 行） | 仅回测端：`walk_forward.py:12,86,199-265`、`backtest/__init__.py:3`、`service.py:8533-8542`（按需回测入口） |
| B | 线上模拟盘撮合（内联逻辑） | `src/stock_analyzer/runtime/service.py` | `_apply_live_auto_portfolio_signals`（约 13063-13550+） |
| C | 研究脚本撮合（硬编码常量） | `scripts/bt_*.py`（4 个脚本） | 独立回测实验 |

### 1.2 逐规则对比

| 规则 | 回测(A) | 线上模拟盘(B) | 脚本(C) | 备注 |
|------|:---:|:---:|:---:|------|
| T+1 卖出限制 | ✅ `matcher.py:69-74` | ❌ 无（买入当日可卖出，`service.py:13365` 直接 close） | ❌ 无 | 持仓有 `opened_at`（`portfolio/book.py:38`），线上实现可行 |
| 停牌不可成交 | ✅ `matcher.py:55,67`（`bar.suspended`） | ❌ 无 | ❌ 无 | 线上行情 payload 无 suspended 字段（`week5_service.py:2687-2827`） |
| 涨停不可买入 | ✅ `matcher.py:57` + `data/limit_rule.py:20-77` 涨跌幅计算 | ❌ 无 | ❌ 无 | 线上有 `prev_close`，可回退计算涨跌停价 |
| 跌停不可卖出 | ✅ `matcher.py:75` | ❌ 无 | ❌ 无 | 同上 |
| 动态滑点公式 | ✅ `matcher.py:79-94` `(atr14/close)*0.35 + (volume_ratio-1)*0.001` | ❌ 无（`_select_simulated_trade_price` 取五档价/最新价，零滑点） | ⚠️ 固定常量 `SLIPPAGE=0.0015`（`bt_ma_gates_portfolio.py:67` 等 4 处） | 线上无 atr14/volume_ratio 现成输入，见 5.3 |
| 滑点超限降级跳过 | ✅ `matcher.py:96-97` + `walk_forward.py:218` | ❌ 无 | ❌ 无 | 无 |
| 价格 tick（exchange_tick 买 ceil/卖 floor） | ✅ `matcher.py:313-321` | ❌ 无（成交价直接取档位价） | ❌ 无 | 无 |
| 手数规则（100 股整手） | ✅ `matcher.py:306-311` | ⚠️ 等价但另写：`_simulation_lot_size`（`service.py:12976-12980`）恒返回 100，且忽略 config 其他取值 | ❌ 无 | 无 |
| 最低成交额（min_notional=5000） | ✅ `matcher.py:131-139` | ❌ 无 | ❌ 无 | 无 |
| 费用（佣金/过户费/印花税） | ✅ `matcher.py:323-344`（含日期化印花税 `resolve_stamp_tax_rate`） | ⚠️ 共享费率参数但另写实现：`service.py:13006-13026`，无日期化印花税 | ⚠️ 硬编码 `COMMISSION=0.0003`、`STAMP=0.0005`（4 个脚本，与 config 默认值巧合一致） | `idle_queue_weekend_trade_service.py:159-161` 亦引用费率 |
| 止盈止损出场（含 deferred/强制平仓） | ✅ `matcher.py:149-304` | ⚠️ 另一套 C3 持仓管理：`_build_c3_position_management_items`（`service.py:12432`） | ⚠️ 简化 TAKE_PROFIT/STOP_LOSS/MAX_HOLD 硬编码 | 线上为"目标仓位管理"而非逐日扫描，属设计差异，不强制统一（见 5.5） |
| 无未来函数 | ✅ `time_semantics.py:10-98` + `walk_forward.py:109,144` + `test_walk_forward.py:45` | 不适用（实时） | 不适用 | 无 |
| 撮合参数来源 | ✅ `BacktestMatcherConfig`（`config.py:841-862`） | ⚠️ 仅费率与手数读 config（`service.py:12977,13014`） | ❌ 全部硬编码 | 无 |

### 1.3 关键缺陷（已核实）

1. **验收 #4/#32 是纸面满足**：`acceptance_service.py:82-98` 只校验 `enforce_t_plus_1/reject_limit_up_buy/reject_limit_down_sell/stop_loss_next_tradable` 四个 config 开关为 True，并不校验线上模拟盘是否真正执行这些规则。验收可以全绿而线上无 T+1、无涨跌停拦截。
2. **配置项未被消费**：`stop_loss_next_tradable`（`config.py:846`）与 `suspended_defer`（`config.py:845`）仅在 acceptance_service 被检查，`matcher.py` 与 runtime 均未读取——开关存在但行为不存在。
3. **动态滑点在实时端完全不生效**：`_select_simulated_trade_price`（`service.py:13028-13061`）返回档位价后直接成交，无滑点、无 tick、无拒单逻辑。
4. **线上 sell 路径无任何可成交性校验**：卖出（`service.py:13227-13324`、`13343-13397`）与买出（`service.py:13458-13476`）均不检查停牌/涨跌停/T+1。
5. **scripts 硬编码常量与 config 默认值存在"巧合一致"风险**：`SLIPPAGE=0.0015`/`COMMISSION=0.0003`/`STAMP=0.0005` 与 `config.py:849-855` 默认值相同，但修改 config 时脚本不会跟随。
6. **一致性验证仅为事后度量**：week7 模拟盘/券商周报对账（`reconcile_service.py:110-119`、`week7_sim_broker_service.py:18`）比较的是结果数字，不纠正撮合行为，且不覆盖上述规则差异。

## 2. 目标架构

### 2.1 候选方案

| 方案 | 描述 | 优点 | 缺点 |
|------|------|------|------|
| A. 共享撮合引擎（推荐） | 将 `ExecutionMatcher` 的**规则原子**（can_buy/can_sell/dynamic_slippage/apply_slippage/plan_order/estimate_cost/tick/rounding）下沉为独立包 `src/stock_analyzer/execution/`，回测与线上均调用同一引擎实例（同一 `BacktestMatcherConfig`+`LimitRuleConfig`）；`simulate_exit` 保持回测专有但复用 can_sell/tick；scripts 改为 import 引擎或至少收敛常量来源 | 单实现、单参数源，天然满足验收 #4/#32；规则改动一处生效；已有 `tests/test_backtest_matcher.py` 直接覆盖新引擎 | 需新增"行情快照→bar 语义"适配层；线上动态滑点缺 atr14 输入（见 5.3）；改造面涉及 runtime 核心成交路径，需 shadow 对比期 |
| B. 仅参数配置化统一 | 两端代码不动，只保证共用 `BacktestMatcherConfig`/`LimitRuleConfig` 参数 + 增加一致性测试闸门 | 改动最小、风险最低 | 逻辑仍是双份实现，必然漂移；验收 #4/#32 依然"形式满足"；治标不治本 |
| C. 整体统一回测语义 | 把 `simulate_exit` 等时序扫描也搬进线上，线上持仓按"逐日模拟"驱动 | 回测/线上极端一致 | 线上是盘中实时信号驱动，与逐日扫描语义冲突；C3 目标仓位管理与 TP/SL 扫描是两套产品逻辑，强行统一破坏线上功能，工作量/风险不可接受 |

### 2.2 推荐：方案 A（共享引擎 + 分层接入）

```
stock_analyzer/execution/
  engine.py      # ExecutionEngine：can_buy/can_sell/plan_order/estimate_cost/
                 #   dynamic_slippage_ratio/apply_slippage/apply_price_tick
  bar_adapter.py # 行情归一化：回测 DataFrame 行 与 线上 market_payload → 统一 bar 视图
                 #   （suspended/up_limit/down_limit/pre_close/close 等，缺省走 build_price_limits 回退）
  config.py      # 校验 + schema_version（可选，见 5.6）
backtest/matcher.py   # 变为薄壳：ExecutionEngine 组合 + simulate_exit（回测时序出场）
runtime/service.py    # 模拟盘成交路径改调 Engine（P1/P2 落地）
scripts/bt_*.py       # 可选：import 引擎；最低要求：常量收敛到 config 默认值（P0）
```

关键设计决策：

1. **决策原子与出场扫描分离**：`simulate_exit`（`matcher.py:149-304`）是回测专用的未来 bars 时序扫描，不属于线上语义；线上侧只复用 can_sell/apply_slippage/estimate_cost。这避免了方案 C 的风险。
2. **适配层保证 bar 语义同构**：`can_buy/can_sell` 依赖 `bar.suspended/up_limit/down_limit`（`matcher.py:346-369`）。线上 market_payload 无这些字段（`week5_service.py:2687-2827`），适配层负责用 `prev_close`+`build_price_limits`（`data/limit_rule.py:20-77`）回退计算涨跌停价，suspended 缺省视为 False 并打日志标记"无停牌数据"。
3. **参数单一来源**：所有费率/滑点/手数/tick/限额只存在于 `BacktestMatcherConfig`+`LimitRuleConfig`（`config.py:841-862`、`data/limit_rule.py`），runtime 与回测不再自写任何"等价"计算。
4. **拒绝原因审计**：线上拒绝时复用 `MatchDecision.reason` 并计入现有 `execution_attempts` 分类（`service.py:13090-13111` 的 `pre_trade_blocked/risk_gate_blocked` 结构），成交/拒单原因带撮合版本（见 5.6）。

## 3. 迁移步骤

### P0 — 统一费用与滑点参数（低风险，先行）

- **改动范围**：
  - `service.py:13006-13026` `_estimate_simulated_trade_fee` 改为调用共享 `estimate_cost`（含日期化印花税），删除重复实现。
  - `service.py:12976-12980` `_simulation_lot_size` 删除，统一走 `plan_order` 手数逻辑。
  - `scripts/bt_*.py`：4 个脚本的 `SLIPPAGE/COMMISSION/STAMP` 常量改为 `from stock_analyzer.config import BacktestMatcherConfig` 默认值（或配置注入），消除巧合一致。
  - 新增 config 校验：engine 构造时校验参数合法性（commission>0、min_commission>=5 等，复用 `acceptance_service.py:100-106` 语义）。
- **风险**：低。费用算法与现有一致（费率相同），差异仅在日期化印花税（历史费率表生效时会微调）。
- **回滚**：保留 `_estimate_simulated_trade_fee` 旧实现 1 个版本，配置开关 `runtime.use_shared_cost_model: bool = False` 默认关闭，验证期打开。

### P1 — 引入共享撮合服务（中风险）

- **改动范围**：
  - 新增 `src/stock_analyzer/execution/`（engine + bar_adapter + config 校验），`tests/test_backtest_matcher.py` 迁移为对新引擎的直接测试，`matcher.py` 改为薄壳。
  - `walk_forward.py:199-265` 与 `service.py:8533-8542` 改为消费新引擎（行为不变）。
  - 线上成交价路径改为：`_select_simulated_trade_price`（保留，作为"行情选价"）→ `apply_slippage`（动态滑点；atr14/volume_ratio 缺省时退化为 `slippage_by_strategy` 静态 base，见 5.3）→ `apply_price_tick` → `plan_order` 校验（手数/最低成交额）。
  - 配置新增 `runtime.apply_dynamic_slippage: bool`（默认 False 的 shadow 开关，见 P2）。
- **风险**：中。滑点+tick 会直接改变线上成交价与成交金额，影响可用现金与持仓成本。
- **回滚**：`runtime.apply_dynamic_slippage=False` 即完全恢复现状成交价逻辑；引擎代码与 runtime 调用点解耦（通过开关分流），可在不迁移第二行代码的情况下回退。

### P2 — 线上接入 T+1/涨跌停/停牌拦截（高风险，最后）

- **改动范围**：
  - 买入路径（`service.py:13399-13476` 一带）：`can_buy(bar)` 前置校验，拒单写入 `buy_new_rejected` 并带 reason（`limit_up_reject` 等）。
  - 卖出路径（`service.py:13227-13324` exit_plan、`13343-13397` sell 信号）：`can_sell(bar, last_buy_date=持仓 opened_at, current_date=now)` 前置校验；T+1 拦截时记 `t_plus_1_block`，当日挂起（沿用现有 pending 语义或直接拒单，二选一，需产品确认，见 5.7）。
  - 涨跌停判定：通过 bar_adapter 用 `prev_close` 回退计算；ST/创业板/科创板 涨跌幅区分由 `LimitRuleConfig`（`data/limit_rule.py:80-99`）处理。
- **风险**：高。会系统性减少线上成交（涨跌停拦截、T+1 延后），模拟盘业绩曲线变化，week7 周报对账口径需要同步说明；必须先 shadow 对比（见第 4 节）。
- **回滚**：`runtime.enforce_t_plus_1_live`、`runtime.reject_limit_live` 两个开关默认 False；灰度顺序：先 T+1 后涨跌停；任一指标劣化即关闭对应开关。

## 4. 测试策略

### 4.1 两端一致性差分测试（核心新增）

同一输入数据集（构造 N 个行情快照 + 持仓状态 + 信号），分别跑**回测引擎调用路径**与**线上成交路径**（`_apply_live_auto_portfolio_signals` 的 dry_run 模式已存在，`service.py:13070` 附近），断言输出逐单一致：

- 字段级断言：成交/拒绝决策、价格（含 tick）、数量（含手数）、费用、reason 分类必须逐单相等。
- 覆盖矩阵：普通买入/涨停买入/跌停卖出/停牌买入/停牌卖出/T+1 当日卖出/T+1 次日卖出/最低成交额边界/价格 tick 边界/滑点超限降级/手续费下限 5 元。
- 新文件：`tests/test_matcher_consistency_runtime.py`。
- 准入：P0/P1/P2 每个阶段合并前跑通，作为 CI 闸门；同时替换 `acceptance_service.py:82-98` 的"开关为 True 即通过"为**行为级校验**（用 fixture 行情跑一次线上路径断言拦截发生）。

### 4.2 单元测试补强

- 新引擎直接测试（从 `tests/test_backtest_matcher.py` 迁入，保持 15 个既有断言覆盖 T+1/动态滑点/deferred/历史费率，`test_backtest_matcher.py:16,35,75` 等）。
- `bar_adapter`：线上 payload → bar 视图的字段映射与回退计算（prev_close→up_limit/down_limit 边界，ST 5%、创业板 20%）。
- 线上路径：`service.py` 注入伪造 Engine 的桩测试，验证拦截 reason 正确写入 `execution_attempts`。
- 无未来函数：维持 `test_walk_forward.py:45`，并新增引擎级测试：同一 bar 若带未来字段（available_time 晚于决策时间）必须被拒。

### 4.3 Shadow 对比期（线上行为验证）

- P2 上线后保留 1-2 周 shadow：同一信号流并行跑"旧路径"与"新路径"，日终对比成交数、拒单数、持仓成本、当日盈亏差异；差异归因到具体规则，逐条关闭或修复。

## 5. 风险与开放问题

1. **行为变化对模拟盘业绩的影响（高）**：涨跌停拦截减少成交、T+1 延后卖出、滑点压低收益——三者都让模拟盘"更接近实盘"但短期数字会变差，week7 对账周报需同步口径说明，否则容易误判为策略退化。缓解：shadow 对比 + 分开关灰度 + 周报新增"撮合版本/规则影响"章节。
2. **与券商对账的关系**：`reconcile`（`reconcile_service.py:110`）是对账后的持仓/资金快照对齐，属事后度量；统一撮合后模拟盘差异来源从"撮合不一致"变为"真实成交 vs 模拟成交"，对账逻辑不需改，但差异归因报告需要区分两类来源。
3. **线上动态滑点输入缺失**：动态公式需要 atr14 与 volume_ratio（`matcher.py:92`），线上 market_payload 无此字段。两个选择：a) 线上用 1m intraday 帧（`week5_service.py:2754-2803` 已加载）计算 atr14 近似值；b) 线上退化为 `slippage_by_strategy` 静态 base。选择 b 则验收 #32"实时生效"仍不完整，需决策者明确接受或追加工作量。
4. **行情快照缺 suspended 字段**：线上停牌判定依赖数据源是否提供停牌标记；缺省时无法拦截停牌买入/卖出，需在 bar_adapter 明确"无停牌数据"的降级策略（建议：宁可放行并审计标记，不做猜测拦截）。
5. **止盈止损出场是否统一**：线上 C3 持仓管理（`service.py:12432`）与回测 `simulate_exit` 语义不同（目标仓位 vs 逐日 TP/SL 扫描），本方案 P0-P2 **不**统一此项，仅共享 can_sell/tick 原子。是否需要更深度统一，建议作为后续独立立项评估。
6. **撮合规则版本化**：建议 `BacktestMatcherConfig` 增加 `schema_version: str = "v1"`，并在成交/拒单审计中记录撮合版本（可扩展 `service.py:13176-13193` execution 记录的 price_source/note 字段）；历史回测重跑必须钉住版本，避免规则演进导致旧结果漂移。
7. **T+1 拦截后的行为语义（待产品确认）**：当日买入被要求卖出时，是"当日拒单"还是"挂起次日执行"？回测语义是 deferred（`matcher.py:187-211`），线上现有结构没有挂起队列；建议线上先"当日拒单+审计记录"，与实盘"卖出委托会被券商拒绝"一致。

## 6. 工作量估计

| 阶段 | 内容 | 后端人天 | 测试人天 | 合计 |
|------|------|:---:|:---:|:---:|
| P0 | 共享费率/滑点参数、scripts 常量收敛、config 校验 | 1 | 1 | 2 |
| P1 | execution 引擎抽取、matcher 薄壳化、线上滑点/tick/手数接入、开关 | 3 | 3 | 6 |
| P2 | 线上 T+1/涨跌停/停牌拦截、审计 reason、shadow 对比 | 3 | 3 | 6 |
| 收尾 | acceptance 行为级校验、周报口径更新、文档 | 1 | 1 | 2 |
| 合计 | | 8 | 8 | **16 人天** |

注：不含 5.5 出场逻辑深度统一与 5.3 线上 atr14 实时计算（若决策者选择方案 b，可省后者；若选择实时 atr14，P1 +1 人天）。

## 7. 决策者摘要

- **推荐方案**：方案 A——把 `ExecutionMatcher` 的规则原子提取为共享撮合引擎 `stock_analyzer/execution/`，回测与线上模拟盘共用同一引擎与同一份 `BacktestMatcherConfig`；`simulate_exit` 时序出场保持回测专有（线上用 C3 目标仓位管理），scripts 脚本至少收敛常量来源。一句话：**一份规则、两端执行、差分测试把关**。
- **关键风险**：(1) 线上接入 T+1/涨跌停/滑点会系统性改变模拟盘成交与业绩，必须 shadow 并行 + 分开关灰度；(2) 线上行情缺 atr14/停牌字段，动态滑点实时生效与停牌拦截存在数据前提问题，需明确降级策略；(3) 当前验收 #4/#32 是"开关为 True"的形式满足，本方案才是行为满足，改动前需先认可现状与实盘目标差距。
- **建议优先级**：P0（2 人天）立即做——零行为变化、消除三套参数分叉；P1（6 人天）次之，完成引擎统一并 shadow 验证滑点/tick 影响；P2（6 人天）在 P1 稳定后再上，先 T+1 后涨跌停，逐项灰度。总预算 16 人天。
