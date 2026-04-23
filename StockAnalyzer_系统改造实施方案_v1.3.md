# StockAnalyzer 系统改造实施方案 v1.3

更新时间：2026-03-11

---

## 一、方案背景

本方案基于以下三类输入综合形成：

1. 对当前仓库核心代码的逐模块核对
2. `stock_analyzer_optimization_analysis.md` 的关键结论复核
3. `beyond_models_analysis.md` 与 `model_enhancement_roadmap.md` 的有效建议筛选
4. 对 `implementation_plan.md` 与后续交叉评审意见的吸收与修订
5. 对 `v1.1` 版本落地可执行性的进一步实操审阅补充
6. 对 `v1.2` 版本验收细项与阶段门禁机制的最终收口优化

本方案只纳入**已经被源码验证、且对当前系统阶段真正有价值**的改造项；不把“理论上更先进但当前不该优先做”的内容混入近期实施范围。

---

## 二、改造目标

本轮改造聚焦四个目标：

1. **恢复模型链路真实性**
   - 避免“伪双模型”“静默降级”“假回滚”这类表面可用、实则失真的链路

2. **把已有数据真正用起来**
   - 重点提高背景数据、分时摘要、主力痕迹数据的实际利用率

3. **建立可执行的反馈闭环**
   - Evolution 的诊断结果要能回流到 Runtime / Pipeline，而不是停留在报告层

4. **提升组合与执行质量**
   - 从“能给买点”提升到“能更稳地赚到钱”

---

## 三、改造原则

### 3.1 先修真问题，再上新模型

优先修复以下根因问题：

- 模型真实后端不可见
- 原生模型不可持久化
- predictor 失败后静默退回启发式
- 训练/验证切分过于宽松
- rollback 使用占位数据
- Evolution 的诊断无法反向影响策略参数

在这些问题没有修好之前，不优先引入 TabNet、TFT、FinRL 一类高复杂度组件。

### 3.2 运行主链路优先于研究工具链

优先保证：

- 线上推断可信
- 夜间演化可信
- 评分与风控可信

其次再做：

- 因子研究
- 模型解释
- 新模型扩展

### 3.3 所有新增能力先走影子模式

对以下新增能力一律先采用 shadow / sidecar 方式：

- 在线增量学习
- 第三模型
- 新新闻情感模型
- 组合优化器

避免直接替换当前主链路。

### 3.4 每个阶段必须可验证

每个阶段都必须定义：

- 涉及文件
- 交付物
- 验收标准
- 回滚方式

---

## 四、当前确认的关键问题

以下问题已经通过代码核对确认成立，应作为本轮改造主线。

### 4.1 P0：模型运行真实性不足

- `LightGBMAdapter` 和 `XGBoostAdapter` 在缺依赖时都会退化为 fallback logistic
- 当前 artifact 中实际保存的也是 fallback logistic
- `meta` 只是简单均值
- 原生模型当前不支持序列化
- predictor 加载失败时，系统会静默退回手写启发式概率

### 4.2 P0：训练与评估链路不够严格

- 训练/验证只做简单时序切分
- 没有 embargo / purging
- 校准与评估使用同一验证集
- 标签对“同 bar 同时触及 TP/SL”的处理过于保守
- 评估指标过少

### 4.3 P0：Evolution 闭环不完整

- rollback 仍使用占位数据
- online update 主要是“是否允许更新”的策略判断，不是真正训练闭环
- M9 失败后，Batch2 几乎全量跳过

### 4.4 P1：已有数据利用不足

- `board` / `completion` 占评分权重，但仍是恒定中性值
- 背景数据已有 `holder_count`、`block_trade_net`、`financing_balance`、`northbound_net`、`dragon_tiger_flag`，但进入主模型的方式偏弱
- prefilter 偏趋势/活跃度，缺少主力痕迹维度
- 分时数据已有日内摘要，但还没有形成更强的行为型因子

### 4.5 P1：组合与执行层偏粗

- 仓位主要由 ATR 决定
- 组合层缺相关性约束
- 缺分批止盈与更细的退出机制

---

## 五、总体实施路线

本轮改造分四个阶段推进：

1. **阶段 A：稳定主链路**
2. **阶段 B：增强数据与评分**
3. **阶段 C：打通反馈闭环与组合层**
4. **阶段 D：引入研究与增强型工具**

建议严格按顺序执行，不建议跳步。

---

## 六、阶段 A：稳定主链路

预计周期：1-2 周

目标：让“训练—存储—加载—推断—治理”链路先可信。

### A0. 建立改造前基线快照

**目标**

- 在改代码前固定一份可复现的“现状快照”，作为后续所有改造收益的对照基线

**涉及文件**

- `src/stock_analyzer/backtest/walk_forward.py`
- `src/stock_analyzer/runtime/service.py`
- `src/stock_analyzer/main.py`
- `config/default.yaml`

**实施内容**

- 先检查 `lightgbm` / `xgboost` 的安装状态、版本号与当前 artifact backend 状态
- 使用现有 walk-forward 链路，对当前主模型方案跑一次完整基线回测
- 记录至少以下指标：
  - 总体准确率 / AUC / Brier
  - 胜率 / 收益率 / 最大回撤 / 资金曲线摘要
  - 当前 artifact 的真实 backend 状态
  - predictor 是否存在 fallback / heuristic 退化
  - 背景类因子的非零覆盖率 / 非空覆盖率，用于评估 Phase B 扩因子的真实收益空间
- 若运行环境缺少原生依赖，则将本次结果标记为 `fallback_baseline`
- 若补齐原生依赖后 backend 发生变化，则追加一份 `native_baseline` 对照报告
- 产出 `artifacts/acceptance/baseline_report.json`
- 后续阶段 A/B/C 的关键实验结果均与该基线报告做并排对照

**验收标准**

- 存在可复现的 baseline 报告与生成命令
- 能明确回答“当前系统到底用了什么模型后端”
- 能回答“背景因子当前覆盖率是否足以支撑 Phase B 收益预期”
- 后续每一阶段的改造收益都有统一对照基线

### A1. 显式化模型后端与降级状态

**目标**

- 明确区分 `native`、`fallback`、`heuristic`
- 任何降级都可见、可告警、可追踪

**涉及文件**

- `src/stock_analyzer/models/adapters.py`
- `src/stock_analyzer/models/artifact.py`
- `src/stock_analyzer/models/predictor.py`
- `src/stock_analyzer/pipeline.py`
- `src/stock_analyzer/runtime/service.py`
- `src/stock_analyzer/config.py`

**实施内容**

- 在 artifact 中记录模型真实后端、训练时间、训练样本量、校准方式
- 在 pipeline report / runtime status 中暴露当前 predictor 模式
- 当双模型都为 fallback 时，标记 `degraded_model_mode=true`
- 当 predictor 加载失败时，不再默默走启发式；默认进入受控降级模式，并写入 reasons / audit
- 把 `lightgbm` / `xgboost` 纳入显式依赖检查

**验收标准**

- API / runtime report 中可直接看到当前模型后端
- 任何 fallback / heuristic 运行都能在报告中定位
- 主链路不再出现“看起来正常，实际是启发式”的无声退化

### A2. 支持原生模型持久化

**目标**

- 让 LightGBM / XGBoost 真正可训练、可保存、可重载

**实施内容**

- 为 LightGBM / XGBoost 增加原生模型保存与加载路径
- 推荐采用 sidecar 文件方案，而不是强行把大对象塞进 JSON
- artifact 记录 sidecar 文件路径、hash、backend 类型
- sidecar 写入采用“临时文件写入 → 校验 → `os.replace` 原子替换”的提交方式
- 读取端优先读取最新已提交版本；若最新版本校验失败，则回退到上一版已知可用 sidecar
- 避免 Evolution 直接覆盖 Runtime 正在读取的文件；优先采用“版本化 sidecar + 当前指针文件/manifest”模式
- 保留 fallback payload 兼容路径

**验收标准**

- 若环境安装了原生依赖，训练后能重新加载同一模型继续推断
- 重启服务后 predictor backend 不丢失
- Evolution 写入与 Runtime 读取并发时，不出现损坏 artifact / 半写入文件
- Windows 环境下模型替换不依赖普通 rename，统一使用 `os.replace`

### A3. 重构训练切分与校准流程

**目标**

- 让评估结果更接近真实 OOS 表现

**涉及文件**

- `src/stock_analyzer/models/trainer.py`
- `src/stock_analyzer/models/calibration.py`
- `src/stock_analyzer/backtest/walk_forward.py`
- `src/stock_analyzer/config.py`

**实施内容**

- 将当前 `train / val` 改为 `train / calibration / test`
- 在切分中加入 `embargo = horizon_days + settlement_lag_days`
- 指标计算只在 test 集进行
- 增加 `auc`、`precision_at_k`、`recall_at_k`、`mean_prob_spread`
- 保留 `brier` 作为校准指标
- walk-forward 报告补充更统一的模型评估摘要

**验收标准**

- 训练报告中能同时看到 calibration 样本量与 test 样本量
- 不再出现“校准后又在同一批数据上评估”的流程

### A3.1 修复标签构造偏差

**目标**

- 降低高波动标的因“同 bar 触发 TP/SL”而被系统性低估的风险

**涉及文件**

- `src/stock_analyzer/labels/soup.py`
- `src/stock_analyzer/models/trainer.py`
- `src/stock_analyzer/backtest/walk_forward.py`

**实施内容**

- 为 `build_soup_labels` 增加可配置的同 bar 冲突处理策略，建议首版支持：
  - `conservative_zero`：保留现状，作为回退基线
  - `bar_shape_heuristic`：基于 K 线形态做启发式判定
  - `soft_label`：输出区间概率标签或中性标签
- 默认不直接用启发式覆盖现状，而是先采用：
  - 训练侧 shadow 对比
  - walk-forward 对比
  - 仅当评估确认收益更优，再切主策略
- 若未来能稳定获得更细粒度分时路径，再升级为真实先后顺序判定

**验收标准**

- 标签构造策略可配置、可回退
- 能产出“旧标签 vs 新标签”的分布差异与训练结果对比
- 未经验证前，不直接强切主标签语义

### A4. 修复 rollback 的占位逻辑

**目标**

- rollback 使用真实的 M11 shadow 数据和真实上下文

**涉及文件**

- `src/stock_analyzer/evolution/orchestrator.py`
- `src/stock_analyzer/evolution/governance/rollback.py`
- `src/stock_analyzer/evolution/modules/m11_shadow_loader.py`
- `src/stock_analyzer/evolution/modules/m11_shadow_portfolio.py`

**实施内容**

- 用 M11 实际 `champion/challenger` 日收益差构造 `diff_returns`
- 用真实观测天数、交易数、连续告警天数构造 `RollbackContext`
- 把占位逻辑彻底移除

**验收标准**

- rollback assessment 输入来源可追溯到 M11
- 在影子组合显著恶化时，rollback 状态可真实变化

### A5. 调整 M9 全量跳过策略

**目标**

- 让数据异常时的演化链路更具弹性

**实施内容**

- 将模块拆分为：
  - 强依赖当日数据模块
  - 可用历史/缓存运行模块
  - 仅治理类模块
- M9 失败时允许部分模块在 degraded 模式下运行

**验收标准**

- M9 异常时不是“一刀切停摆”
- 报告中能区分 `skipped_by_m9` 和 `degraded_run`

---

## 七、阶段 B：增强数据与评分

预计周期：2-3 周

目标：把已有数据更深地注入筛选、特征、评分。

### B1. 让 `board` / `completion` 变成真实组件

**涉及文件**

- `src/stock_analyzer/pipeline.py`
- `src/stock_analyzer/signal/scoring.py`
- `src/stock_analyzer/week6/engines.py`
- `src/stock_analyzer/runtime/service.py`

**实施内容**

- `board`：
  - 接入 Week6 主力评分 / M4 资金流方向 / 板块强弱信息
  - 输出 `0-1` 范围真实得分
- `completion`：
  - 基于 `background_data_complete`、财务完整性、分时摘要可用性、关键字段缺失率生成质量分
- 若数据不足，允许对单组件降权，而不是固定给 `0.5`

**验收标准**

- pipeline 输出中 `board` / `completion` 不再长期恒为 `0.5`
- 两个组件变化能解释 signal score 的变化

### B2. 扩展背景数据因子

**涉及文件**

- `src/stock_analyzer/feature/engineer.py`
- `src/stock_analyzer/data/background_adapter.py`
- `src/stock_analyzer/week6/engines.py`

**实施策略**

- 将背景因子拆成两层：
  - **Tier 1：现有字段深度利用**
  - **Tier 2：增量拉取明细字段**

**Tier 1：优先落地（建议 18-22 个因子）**

- 股东行为：
  - `holder_count_chg_5/20/60`
  - `holder_count_zscore_60`
  - `holder_count_decrease_streak`
- 北向资金：
  - `northbound_net_5/10/20/60`
  - `northbound_net_zscore_60`
  - `northbound_momentum_5v20`
- 融资融券：
  - `financing_balance_chg_5/20/60`
  - `financing_balance_zscore_60`
  - `financing_balance_trend_5v20`
- 大宗交易：
  - `block_trade_net_5/20`
  - `block_trade_frequency_20`
  - `block_trade_direction_10`
- 基本面质量：
  - `roe_trend_60`
  - `debt_ratio_stability_60`
  - `background_completion_score`

**计算成本约束**

- `holder_count_decrease_streak` 这类窗口/状态型因子，不建议在大 universe 的实时 `transform()` 中逐股票全量重算
- prefilter 阶段优先使用简化版代理特征，例如“近 60 日递减次数 / 是否连续减少 3 次以上”
- 完整 streak 计算放在精筛阶段、离线预计算结果或缓存层，避免 5000 只股票批量扫描时明显拖慢特征工程

**Tier 2：条件增强（建议再加 8-12 个因子）**

- 龙虎榜细项：
  - `dragon_tiger_buy_amount`
  - `dragon_tiger_sell_amount`
  - `dragon_tiger_net_amount`
  - `dragon_tiger_inst_buy`
  - `dragon_tiger_inst_sell`
  - `dragon_tiger_inst_net`
- 若 AKShare / 其他源可稳定获取，再逐步纳入：
  - `northbound_position_ratio`
  - `northbound_position_ratio_chg`

**说明**

- 不再仅满足于 `dragon_tiger_flag` 的 0/1 标记
- 第一批以“已有字段做深”为主，第二批再接详细买卖金额数据
- 所有 Tier 2 字段必须 zero-safe / missing-safe，不得影响现有运行

**验收标准**

- 背景类特征显著增加，且能进入训练 artifact 的 feature columns

### B3. 重做 prefilter：从单层强势筛选改为两段漏斗

**涉及文件**

- `src/stock_analyzer/runtime/service.py`
- `src/stock_analyzer/config.py`

**目标结构**

- 第一层：`5000 -> 500`
  - 快速排除明显不合格标的
  - 保留趋势、流动性、基础主力痕迹
- 第二层：`500 -> 50`
  - 用更完整的信号、背景数据和风险复核做 shortlist

**实施内容**

- 保留现有 prefilter 的趋势骨架
- 新增主力维度分项
- 第一层 `5000 -> 500` 仍先做 prefilter，Pipeline 针对这 500 只候选运行
- 第二层 `500 -> 50` 明确放在 Pipeline 信号生成之后执行，对候选信号做额外综合排序
- 增加单独的 shortlist scoring，而不是把 Pipeline 分数直接等价为最终入池顺序

**第一层 baseline 评分公式（可配置）**

- 趋势维度：`40%`
- 主力潜伏维度：`25%`
- 量价结构维度：`15%`
- 基本可交易性/流动性：`10%`
- 波动与风险惩罚：`10%`

建议实现方式：

- 先把各维度都归一到 `0-1`
- 再做加权求和，而不是继续累加“硬编码分值”

建议维度拆分：

- 趋势维度：
  - `close vs ma20/60/120`
  - `ret20/60`
- 主力潜伏维度：
  - `holder_count_chg_60`
  - `northbound_net_20/60`
  - `dragon_tiger_net_amount` 或 `dragon_tiger_flag`
- 量价结构维度：
  - `recent volume expansion`
  - `ATR contraction`
  - `heat_ratio`
- 流动性：
  - `avg_turnover_20`
  - `float_market_cap`
- 风险惩罚：
  - `volatility20`
  - `is_st / delisting risk`

**第二层 shortlist 评分公式（建议新增）**

- 模型信号：`35%`
- 主力确认：`25%`
- 背景数据质量与财务约束：`15%`
- 分时行为特征：`15%`
- 执行与风险复核：`10%`

建议输入：

- `pipeline meta / cross_review / score`
- `main_force_score / board_score / completion_score`
- `background_completion_score`
- `intraday tail_strength / above_vwap_ratio / close_position`
- `liquidity_pass / risk_gate / financial_filter`

**执行顺序澄清**

- 实际链路应为：`prefilter -> Pipeline 生成信号 -> shortlist scoring -> watchlist / signal_pool 同步`
- 第二层不是替代 Pipeline，也不是在 Pipeline 之前做“预打分”
- 这样可以避免“模型分数依赖 shortlist、shortlist 又依赖模型分数”的表述性循环依赖

**验收标准**

- week5 report 中能看到两段筛选结果
- shortlist 的入池逻辑具备可解释性

### B4. 明确分时数据流，并双轨增强分时因子

**涉及文件**

- `src/stock_analyzer/data/intraday_summary.py`
- `src/stock_analyzer/feature/engineer.py`

**当前约束**

- Runtime / Pipeline 当前消费的是“日级分时摘要”
- `FeatureEngineer` 现有输入不是原始 1 分钟面板，而是 provider 返回的 summary frame

**因此明确采用双轨方案**

#### B4.1 生产主链路：扩展 `data/intraday_summary.py`

- 在分钟数据同步或摘要生成阶段，直接从原始 1 分钟 / 5 分钟 K 线计算更丰富的日级摘要
- 这些摘要进入 DuckDB / package / provider
- `FeatureEngineer` 继续只消费日级 summary，不改变 runtime 主接口

**首批建议新增摘要**

- `tail30_volume_share`
- `morning30_volume_share`
- `above_vwap_ratio`
- `price_efficiency`
- `am_pm_reversal_strength`
- `tail_volatility_ratio`
- `close_vwap_stability`
- `intraday_pullback_ratio`

#### B4.2 研究与离线路径：新增 `src/stock_analyzer/feature/intraday_factors.py`

- 将“从原始分钟面板提取日内因子”的计算逻辑封装成独立模块
- 该模块可被：
  - `data/intraday_summary.py` 复用
  - 离线研究直接调用
  - 未来 raw-minute 特征实验复用

**这样做的原因**

- 既解决“谁来计算分时摘要”的数据流问题
- 又保留未来直接使用分钟原始面板的扩展空间
- 不强行让 runtime 当场读取和计算原始 1 分钟数据

**验收标准**

- 分时数据流清晰：原始分钟数据 -> 摘要/因子模块 -> 日级特征 -> artifact
- 新分时特征进入 artifact
- 不明显拖慢日常 pipeline 时延

---

## 八、阶段 C：打通反馈闭环与组合层

预计周期：2-4 周

目标：让 Evolution 不只是“诊断”，而能影响运行决策。

### C1. 把 M2 / M4 / M6 / M10 回流到 Runtime

**涉及文件**

- `src/stock_analyzer/evolution/orchestrator.py`
- `src/stock_analyzer/runtime/service.py`
- `src/stock_analyzer/pipeline.py`

**回流规则建议**

- `M2` 市场状态：
  - 调整 strategy allocation
  - 调整阈值与仓位倍率
- `M4` 资金流方向：
  - 影响 `board` 分数
  - 影响进攻/防守模式
- `M6` 卖压：
  - 降低单票目标仓位上限
  - 提升 watch / hold 转换概率
- `M10` 健康状态：
  - 决定是否信任当日模型分数
  - degraded 时触发保守模式

**验收标准**

- Runtime 报告中能明确看到 evolution controls 的来源与影响

### C2. 建立 M1 负面案例库

**目标**

- 把 M1 从“计数器”变成“可用知识库”

**实施内容**

- 记录典型亏损样本的结构化原因：
  - 高位追涨
  - 流动性不足
  - 高卖压
  - 模型分歧
  - 数据不完整
- 在后续信号阶段增加负样本相似度惩罚

**验收标准**

- M1 输出不再只有 bucket count
- 能在 signal reasons 中看到负例约束痕迹

### C3. 升级组合与执行层

**涉及文件**

- `src/stock_analyzer/strategy/soup.py`
- `src/stock_analyzer/portfolio/book.py`
- `src/stock_analyzer/runtime/service.py`

**实施内容**

#### C3.1 先落地确定性约束

- 加入行业/主题集中度约束
- 加入简化相关性约束
- 维持 ATR 基础仓位，同时引入信号强度倍率
- 引入分批止盈：
  - 第一档减仓
  - 第二档再减仓
  - 剩余仓位走 trailing stop
- 保持现有 max_hold_days 逻辑，逐步细化退出原因

#### C3.2 组合优化工具明确选型：`PyPortfolioOpt` 的 `HRP`

- 推荐工具：`PyPortfolioOpt`
- 推荐算法：`HRP`（Hierarchical Risk Parity）
- 使用方式：
  - 先 shadow
  - 候选池至少满足 `N>=5`
  - 近 `60` 日收益率可用
  - 只输出建议权重，不立即接管主仓位

**为什么选 HRP**

- 不需要稳定估计预期收益
- 能处理相关性聚类
- 比直接上均值方差更稳

**最终决策**

- 近期主线：手写确定性约束 + 相关性上限
- 中期增强：HRP shadow optimizer
- 通过 shadow 对比后，再决定是否提升为正式仓位建议器

**验收标准**

- 新持仓决策不再只由 ATR 单因子决定
- 组合层能主动限制集中风险
- HRP 建议权重可独立输出并与现有方案做对照

### C4. 在线学习先走 shadow 模式

**目标**

- 为 `river` 铺路，但不直接接管主模型

**实施内容**

- 在 Evolution 中建立 `shadow_online_model`
- 使用成熟样本做增量学习
- 在线模型只输出 shadow 概率与表现比较
- 暂不直接参与主决策

**验收标准**

- 有独立的 shadow online report
- 不影响现有主模型稳定性

---

## 九、阶段 D：研究与增强型工具

预计周期：3-6 周

目标：在主链路稳定后，逐步引入研究与增强工具。

### D1. Alphalens 因子体检

**建议等级：高**

用途：

- 找出无效因子
- 看因子衰减周期
- 指导特征裁剪与新增

建议先作为离线研究任务，不直接侵入 runtime。

### D2. SHAP 可解释性

**建议等级：中高**

用途：

- 输出信号解释
- 监控模型逻辑漂移

建议在原生树模型稳定可用之后落地。

### D3. CatBoost 作为第三模型

**建议等级：中高**

用途：

- 增强异构性
- 处理类别特征更友好

建议先 shadow，再决定是否升级 cross review 为三模型。

### D4. FinBERT / 更真实的新闻情感模型

**建议等级：中**

用途：

- 提升新闻模块语义质量

前提：

- 先明确新闻模块在系统中的权重与实际贡献
- 避免在主模型链路还不稳定时先砸 NLP 复杂度

### D5. Qlib 作为研究侧桥接

**建议等级：中**

用途：

- 借用 Alpha158/360 因子
- 借用研究和执行模拟能力

实施方式：

- 独立 `qlib_bridge.py`
- 仅做离线研究 sidecar，不直接重构现有数据层

### D6. 暂缓项

以下内容暂不建议进入近期实施主线：

- TabNet / FT-Transformer
- TFT
- FinRL
- 重型端到端深度时序模型

原因：

- 当前主问题不在“模型不够新”
- 基础链路与反馈闭环尚未打牢

---

## 十、建议排期

### 第 1 周

- A0 改造前基线快照
- A1 模型后端可见化
- A2 原生模型持久化方案确定
- A3 训练切分方案重构设计
- A3.1 标签偏差修复方案 shadow 设计

### 第 2 周

- A2 落地
- A3 落地
- A3.1 标签偏差修复对比实验
- A4 rollback 接真实 M11
- A5 M9 降级运行

### 第 3-4 周

- B1 `board/completion` 实值化
- B2 背景因子扩展
- B3 两段式 prefilter
- B4 分时数据流改造 + 摘要增强

### 第 5-6 周

- C1 Evolution 回流 Runtime
- C2 M1 案例库
- C3 组合层与退出层升级
- C4 shadow online learner

### 第 7 周以后

- D1 Alphalens
- D2 SHAP
- D3 CatBoost shadow
- D4 FinBERT / D5 Qlib bridge

### 10.1 阶段门禁与检查点

- `A0` 完成后，固化基线报告；后续所有阶段收益判断都以该报告为统一基准
- 阶段 A 完成后，必须再跑一次 walk-forward checkpoint，并与 `A0` 对比：
  - 若主链路可信度指标未达标，则不得进入阶段 B
- 阶段 B 完成后，必须再跑一次 walk-forward checkpoint，并与 `A0`、阶段 A checkpoint 对比：
  - 若数据利用率 / 筛选质量没有改善，则不得进入阶段 C 的主链路接管项
- 阶段 C 完成后，必须再跑一次组合与执行 checkpoint：
  - 若组合层改造未优于基线，则保持 shadow，不直接提升为正式执行逻辑
- 阶段 D 默认保持研究/sidecar 属性，不占用 A/B/C 门禁资源

**建议产物**

- `artifacts/acceptance/baseline_report.json`
- `artifacts/acceptance/checkpoint_phase_a.json`
- `artifacts/acceptance/checkpoint_phase_b.json`
- `artifacts/acceptance/checkpoint_phase_c.json`

---

## 十一、验收指标

### 11.1 主链路可信度

- 原生模型 artifact 可加载率 = 100%
- predictor 静默退化次数 = 0
- rollback 占位输入次数 = 0

### 11.2 数据利用率

- 背景类有效特征数 `>= 30`（当前约 `12`）
- `board/completion` 非 `0.5` 占比 `>= 80%`
- 新增分时摘要特征进入 artifact 并参与训练的数量 `>= 8`

### 11.3 筛选质量

- 第二层 shortlist 排序的输入字段覆盖率 = 100%
- shortlist 中 `background_completion_score >= 0.7` 的占比 `>= 60%`
- 最终自动同步 watchlist 的候选项中，可追溯 shortlist score/reasons 的占比 = 100%

### 11.4 运行质量

- 每次 degraded 运行都带有独立的 `degraded_reason` 与时间戳字段
- `M9` 异常时，非 `M9` 主流程仍可产出可用报告，且核心输出字段保留率 `>= 80%`
- `buy/watch` 信号中，`reasons` 数量 `>= 2` 的占比 `>= 90%`

### 11.5 组合与执行

- 同一行业持仓数 `<= 2`（默认值，可配置）
- 分批止盈方案相对“一次性止盈”的平均持仓收益差 `> 0`
- 新组合建议在回测/影子组合中，不高于基线方案的最大回撤

---

## 十二、风险与回滚策略

### 12.1 依赖风险

- `lightgbm` / `xgboost` / `river` / `catboost` 等新增依赖可能导致部署复杂度上升

**策略**

- 主功能优先采用可选依赖
- 全部先 shadow / optional 再切主链

### 12.2 数据质量风险

- 背景数据与分时数据扩展后，空值与错位问题会增加

**策略**

- 所有新增因子必须配 completion / quality 标记
- 缺失时宁可降权，不要伪造强信号

### 12.3 复杂度失控风险

- 同时做模型、数据、组合、NLP 会导致改造面过宽

**策略**

- 严格按阶段推进
- 未完成阶段 A，不进入阶段 D 的重型工作

---

## 十三、立即执行清单

建议先从以下 12 项开工：

1. 确认 `lightgbm` / `xgboost` 安装状态与当前 artifact backend；若缺失先补依赖
2. 跑一次完整 walk-forward，生成 `artifacts/acceptance/baseline_report.json`
3. 为模型 artifact 增加真实 backend 元数据
4. 去掉 predictor 的静默启发式退化
5. 实现 LightGBM / XGBoost 原生模型持久化，并补齐 sidecar 并发安全
6. 将训练流程改成 train/calibration/test + embargo
7. 为标签冲突处理增加 shadow 策略并完成对比评估
8. 用真实 M11 结果替换 rollback 占位输入
9. 把 `board` / `completion` 做成真实评分组件
10. 扩展背景因子并纳入 prefilter
11. 明确分时数据流：摘要增强 + `intraday_factors.py`，并预留阶段 checkpoint
12. 把 M2 / M4 / M6 / M10 回流到 runtime 参数

---

## 十四、最终建议

本轮改造的核心，不是“继续加更多模型”，而是：

- 先让已有模型链路真实可信
- 先把已有数据真正吃透
- 先让 Evolution 的结论能反向影响 Runtime
- 最后再做研究工具和新模型扩展

如果执行顺序正确，这一轮改造完成后，系统会从“功能很多但部分链路失真”升级为“主链路可信、数据利用更深、反馈闭环开始成型”的可持续架构。
