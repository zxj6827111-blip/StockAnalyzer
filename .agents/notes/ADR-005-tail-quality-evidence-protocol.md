# ADR-005 尾盘选股质量证据协议与晋升边界

Status: Draft

As-of: 2026-10-12 @ HEAD `5e4fe71fd8962b1bd57271979abb85caf1d52dab`（已核对主 pipeline 的 14:45 候选级运行上下文捕获实现、迟到拒收和定向验证；该提交待 PR / NAS 验证；271 字段仍无完整字段级 PIT 证据；策略仍无合格候选）

## 1. 决策范围

本 ADR 固定尾盘策略的历史质量验证必须如何构造交易日、估计不确定性、报告输入身份，
以及哪些结果不得作为发布证据。它不宣称当前策略有效，也不替代未来影子观察。

## 2. 已落地的判定规则

1. 滚动验证必须使用显式开放交易日历，并区分交易所开放日与已知决策覆盖日。日历中的
   `is_open` 用于验证决策日和入场日；可选 `is_decision_session` 用于限制训练/测试切分
   及最低决策日计数。若未提供该列，向后兼容地把每个开放日视为决策覆盖日。终端入场日
   可以是开放日但不是决策日。不能用有样本的日期代替已登记的决策日期轴；样本日期
   （含入场日）不在开放日历中、日期无效或缺少 `is_open` 时，验证阻塞。
2. 折数来自实际 walk-forward split。每个结果需携带真实 `fold_id` 与交易日 `session_index`；
   不得按日期数量推测折数。
3. 处理臂与基线的净盈利率点估计及区间使用相同统计量：各自成熟盈利成交数除以成熟成交数，
   配对移动块重抽后重算汇总比例。主块长 10 个连续交易日，敏感性块长 5 / 20，20,000 次、
   PCG64、seed `20261010`。采样块不得跨折或跨交易日缺口。
4. 样本不足与质量失败分开报告。质量判定前至少需 120 个配对测试日、每个真实测试折至少
   30 日、每臂至少 300 笔成熟成交；条件不满足不得记为质量通过。
5. 样本 JSONL 与交易日历 CSV 的路径、SHA-256、行数 / 开放日数进入验证报告，以便复现输入。
   样本拼接键 `(symbol, decision_date)` 必须唯一；验证输入 `(symbol, decision_date,
   entry_date, capture_mode)` 必须唯一，且一次运行只能包含一种 `capture_mode`，防止
   重复文件或 observed / replay 混合抬高计数。
6. 当前评估器只覆盖一个处理臂与一个同池旧排序基线，报告固定
   `scope=development_pairwise_only`、`release_eligible=false`。开发结果不具备晋升资格。
7. 独立确认的统计计算不能单独授予发布资格。命令行必须同时通过全链路字段 PIT 审计，确认
   每个 `(trade_date, arm_id)` 都是 `observed_snapshot`，所需字段覆盖、输入摘要、来源文件与
   带时区的可用/采集/计算时钟完整且不晚于决策截止。`replayed_recompute`、字段缺失、未知填补、
   错误哈希或无法对齐的会话键均 fail-closed。

## 3. 实现状态与仍阻止质量确认的部分

当前工作树已经提供阶段一、二和阶段三所需的核心实现，但 ADR 仍为 Draft；代码和单测不等于
收益验收。以下事实必须继续分开：

- **已实现（2026 开发样本重建完成、参考缺口为零）**：RAW 1 分钟退出、实际退出时刻
  maturity、公司行动现金 / 股数处理与 adj_factor 缺口门，见 ADR-006。排序后的 v9 子库重建了
  28,200 个请求、0 个参考缺口、19,982 条成熟可训练；12 个停牌未成交、8 个公司行动覆盖不确定、
  17 个停牌/分钟冲突仍不计输赢。前导零丢失的 v2 子库已作废，不能用于收益结论。
- **账本实现完成，未用于质量晋升**：`tail_account_ledger.py` 以 30 万现金、每笔 1 万、每日最多
  3 只，重放无补选成交、持仓、RAW 日标记、费用、分红应收和现金净值。当前阶段二没有合格
  候选，尚无四臂共同输入可用于独立确认账户检验。
- **已实现（未运行质量通过）**：`tail_confirmatory.py` 固定一个候选与三个对照、八项检验，使用
  20,000 次 PCG64 配对移动块抽样、Bonferroni 单侧 99.375% 下界和 5 / 20 日敏感性检查；另有开发
  数据上的 80% 功效样本量估算。它要求冻结时间、工件摘要、独立窗口开封声明与全臂同日账本。
- **已实测（2026 阶段二无合格候选）**：最多 12 个 LR / 现有参数 LightGBM 特征臂、训练窗相关性去重、
  开发阶段 Holm 校正，以及“先过开发门、再按四折平均样本外净收益、特征数、LR 优先级”候选选择
  均已登记。2026 v9 开发窗口覆盖 94 个决策日；十个价格特征臂全部在首个校准窗口因 raw AUC 低于
  0.5 而 fail-closed，两个 `moneyflow` / `top_list` 臂因 PIT 可用时点未证明而 blocked。没有一个
  数据实验臂到达模型训练与工件 round-trip，因此 LightGBM 已安装不等于本轮已完成真实训练/序列化
  验收。报告 `tail_stage2_ablation_raw_v9.json` 选择结果为 `no_qualified_candidate`。
- **已实测（2026 ST 修正与公司行动官方证据后的 v5 阶段二仍 blocked）**：195,077 请求合并 21 条
  官方公司行动复算标签后，样本有 179,629 条成熟可训练记录、0 个参考缺口。固定 12 臂矩阵中 10 个
  价格臂因为只覆盖 58 个决策日（四折最低需要 71 日）而阻塞，2 个 `moneyflow` / `top_list` 臂因
  PIT 可用时点未证明而阻塞；`no_qualified_candidate`、`release_eligible=false`。报告位于
  `artifacts/research/tail_nas_collection_20261011_v1/official_action_fix_v1/stage2_ablation_corrected_st_v5.json`。
- **已实现（未来确认入口）**：`validate_tail_independent_confirmation.py` 读取带哈希的开发日历和四臂
  逐日输入，先计算开发功效样本数，再执行四臂八项校正检验；缺工件摘要、窗口开封声明或日历会
  阻断。命令行 attestation 是人的声明，不是密码学证明。
- **已实测（2025 修复后的 RAW v3 开发窗口仍无候选）**：全量 72,600 请求有 0 个参考缺口、62,949
  条成熟可训练；12 臂中 9 个价格臂因校准窗 raw AUC < 0.5 停止，两个资金流 / 龙虎榜臂因 PIT
  可用时点未证明而阻塞。唯一完成计算的 `drop_market_relative__logistic_baseline` 有 230 笔成熟成交，
  同池容量基线 525 笔；命中率差 +5.81pp，但 20,000 次配对移动块 bootstrap 的 95% 区间为
  [−7.65pp, +22.00pp]，跨 0，且处理臂低于 300 笔门槛，故质量状态为 `sample_insufficient`。
  `candidate_selection=no_qualified_candidate`。报告为
  `artifacts/research/raw_exit_zero_volume_fix_2025_v1/stage2_ablation_raw_2025_v3.json`。
- **已实测（2025 资金流 / 龙虎榜来源覆盖已盘点，PIT 仍未证明）**：本地文件日期集合与 2025 开放日历
  的 243 个日期完全相等；
  `moneyflow` 有 1,248,109 行且 `(trade_date, ts_code)` 唯一，`top_list` 有 18,033 行、1,616 个重复股票日键
  （1,681 条多余行，其中 1,609 个键含多个上榜理由），必须按唯一股票日去重。采集报告声称无失败、空日或可能截断日期，但不含逐日采集时间、
  逐响应哈希或历史版本；Tushare 官方 `moneyflow` / `top_list` 文档也没有为这些文件给出可核实的
  历史发布截止或 as-of 版本。因此两臂继续 fail-closed；资金流缺行不得填零，单纯使用前一交易日滞后
  不能补出历史版本。可复跑审计为 `scripts/audit_tail_event_feature_provenance.py`，报告为
  `artifacts/research/tail_event_pit_provenance_audit_20261011_v3.json`。
- **已实测（2025 未校准 raw-score 方向探索，不构成候选或确认）**：在完全相同的每日可评分池内，
  对冻结 12 臂各比较 raw score 高分 Top3 与低分 Top3；10 个价格特征臂的低分减高分净盈利率点差
  全为负（−1.68 至 −15.46 个百分点），平均净收益点差也全为负（−0.374 至 −1.796 个百分点）。
  24 个臂 × 指标构成一个单侧 Holm 校正族，20 个可计算检验的校正后 p 值均为 1.0；两个
  资金流 / 龙虎榜臂仍因 PIT 未证明而
  blocked。输入为已经查看过的 `replayed_recompute` 开发数据；raw score 不是校准概率，不能绕过
  raw AUC 门、生成候选或进入独立确认。不可覆盖报告为
  `artifacts/research/tail_score_direction_audit_20261012_v5.json`。
- **仍缺实测证据 / 外部时间门槛**：旧完整选股链路是否能共同 PIT 重放；PIT 证券状态与全部特征的
  逐字段来源、可用时点证明也未完成。独立确认数据必须在候选冻结后收集且未开封，已查看的 2025 / 2026
  数据不能改名成独立集；当前正式候选尚未产生，因此确认窗口不能启动。影子至少 60 个完整交易日、
  100 笔成熟模拟成交和账户净值必须由未来真实观察逐日累积，不能通过本地代码或历史重标加速。

- **PIT 审计实现已完成，历史证据仍缺**：`tail_field_pit.py` 从资格硬门、趋势特征、事件特征、旧版
  `FeatureEngineer` 输出及原始快照生成当前 271 个去重字段清单。审计器绑定确认输入 SHA、唯一
  `(trade_date, arm_id)`、观察模式、字段行数、未知填补计数、来源 SHA/版本和时钟顺序。独立确认
  CLI 现要求 full-chain PIT 审计；统计函数自身始终 `release_eligible=false`，只有 CLI 的 PIT、
  未开封、功效及统计门全部通过后才可能置为 true。当前清单报告中 271 个字段全部 blocked，
  因而这项代码修复并未产生候选或真实确认结果。

- **前向分钟观察采集已接线，尚待 NAS 首次运行验证**：`Week5AutomationService` 在尾盘确认窗口
  请求 Sina 1 分钟线，记录每只票的来源、响应接收时钟与最新 bar；决策分钟、跨日、未来和超出策略
  陈旧度门的数据均被排除。14:45 快照只留存真实观察池的决策输入，其它窗口的行情只供影子评估成交。
  供应商 K 线标签的区间端点语义仍未核实，故有意排除 14:45 标签本身。快照明确标记
  `full_chain_pit_evidence=false`；它不补历史字段时钟，不改变候选、策略或发布门。需要在 NAS 部署后
  检查第一份不可覆盖日快照。

- **运行时批量行情快照来源时钟补充（待合并部署验证）**：`Week5MarketSnapshotService.capture()` 对本进程
  完整收到的非空 provider 批量响应记录 `source_retrieved_at`；计划快照 `timestamp` 仍单独保留。该时钟
  是应用接收时钟，不是供应商发布时间、逐字段时钟或每只证券独立响应时钟；若接收晚于决策截止，仍不能
  作为该截止时点的可用证据。`ResilientProvider.status()` 暴露配置的 primary mode 及 primary/backup
  实现类；`snapshot.py` 的来源签名在无 status 时回退到配置的 primary mode，使构建时身份与后续新鲜度检查
  一致，避免 wrapper 快照每次被误判为过期。这是配置链身份，不证明某次请求实际由哪个 provider 返回，
  fallback 实际来源仍需单次响应级记录。以上只补来源元数据，不能替代 271 字段逐字段来源、版本、可用时钟
  和采集时钟审计；全链路门继续 fail-closed。

- **14:45 主 pipeline 运行上下文前向捕获（代码已实现，待 PR / NAS 首次运行验证）**：只有生产调度的
  `decision_input` 槽会开启捕获。主 pipeline 为每个候选信号留存 legacy `FeatureEngineer` 最新特征行、
  特征版本和列摘要，并记录应用拿到全部 pipeline 输入的时钟与特征计算时钟；不可覆盖快照另存本轮
  pipeline 报告、候选对应的批量行情行，以及明确标成“上下文”的 `features_light` 候选行。`scheduled_at`
  与 pipeline 完成后的 `decision_at` 分开，实时行情陈旧度按实际决策时钟计算。若 pipeline 完成时已离开
  14:45 分钟，采集报告会标为未留档，不把晚到数据伪装成该槽输入。此捕获仍限候选池；provider 仅记录配置链，
  不证明单次响应的实际 fallback 来源；仍缺逐字段供应商发布时间、来源文件 SHA 和字段级可用时钟，故不是
  271 字段 PIT 证据，审计与晋升门继续 fail-closed。

- **验证环境兼容已闭合**：本机原 Python 3.12 环境的 NumPy stub 与项目 mypy 目标 3.11 不兼容；
  使用已有 Python 3.11 解释器和隔离 uv 环境运行 `clean-scope --fail-on-error`，报告 `ok=true`、
  blocking/nonblocking failures 均为空。环境路径、命令与本轮报告记在
  `docs/selection_quality_plan_status.md` §16。

未完成证据不得通过降低阈值、删减对照、填零、伪造账户流水、重复读取已见历史数据或部署生产来替代。

## 4. 实施位置

```text
src/stock_analyzer/models/tail_net_profit_trainer.py
  evaluate_selection_quality() / _paired_moving_block_bootstrap()
src/stock_analyzer/research/tail_walk_forward.py
  sufficiency_blockers() / run_walk_forward() / _outcome()
scripts/validate_tail_selection_quality.py
  --trading-calendar / load_trading_calendar() / unique sample keys / input_manifest
scripts/assemble_tail_samples.py
  request / label decision-key uniqueness
scripts/run_tail_model_ablation.py
  frozen 12-arm model/feature development comparison and candidate-selection rule
  `is_decision_session` 日历列、replayed-only underpowered exploration
scripts/audit_tail_event_feature_provenance.py
  historical moneyflow/top-list row coverage, duplicate symbol-days, hashes, and fail-closed PIT status
tests/test_audit_tail_event_feature_provenance.py
  source completeness, duplicate-key, and permanent PIT-block behavior
scripts/validate_tail_independent_confirmation.py
  future four-arm report, power estimate, input hashes, unopened-window and full-chain PIT gates
src/stock_analyzer/research/tail_account_ledger.py
  independent per-arm cash / position / daily NAV replay
src/stock_analyzer/research/tail_confirmatory.py
  development power estimate and frozen 8-test statistics; never grants release eligibility alone
src/stock_analyzer/research/tail_field_pit.py
  code-derived field inventory and input-bound PIT coverage/source/clock audit
scripts/audit_tail_field_pit.py
  field inventory and fail-closed PIT report command
src/stock_analyzer/research/tail_observed_inputs.py
  immutable 14:45 observed input snapshot; minute-bar source/collection metadata; never full-chain PIT evidence
src/stock_analyzer/runtime/services/week5_automation_service.py
  forward-only live 1-minute producer; 14:45 candidate-only pipeline/runtime context; same-day and decision-clock filtering; fail-closed late/missing capture
src/stock_analyzer/pipeline.py
  per-symbol legacy latest-feature capture with input/computation clocks and feature-column digest; source identity remains unattested
src/stock_analyzer/runtime/service.py
  scheduler-only activation of production PIT input capture; propagate capture flags into the pipeline
src/stock_analyzer/runtime/services/week5_market_snapshot_service.py
  批量行情快照计划时钟与应用响应接收时钟分离；注入数据不伪造 provider 接收时钟
src/stock_analyzer/data/resilient_provider.py
  配置 provider mode 与 primary/backup 实现类身份；不声明单次 fallback 的实际来源
src/stock_analyzer/feature/snapshot.py
  feature snapshot 来源签名使用配置 provider mode 回退值，保证构建与新鲜度校验一致
src/stock_analyzer/runtime/services/trend_tail_shadow_service.py
  observed source metadata persistence and contract-local quote clock
tests/test_week5_automation.py / tests/test_week5_automation_scheduler.py / tests/test_trend_tail_shadow_runtime.py / tests/test_tail_observed_inputs.py / tests/test_week5_scan_funnel_policy.py
  live capture filtering, scheduler activation, late-window rejection, immutable runtime-context persistence, feature-row shape, and clock ordering
src/stock_analyzer/research/tail_score_direction.py
  same-pool daily high/low raw-score Top-K selection and paired moving-block comparison
scripts/audit_tail_score_direction.py
  replayed-development-only 12-arm direction audit; global Holm family; release_eligible=false
tests/test_tail_score_direction.py / tests/test_audit_tail_score_direction_cli.py
  directional selection/statistics, input restrictions, immutable output, and Holm family scope
tests/test_tail_field_pit.py / tests/test_audit_tail_field_pit_cli.py
tests/test_tail_confirmatory_cli.py
  field evidence, tamper/path/clock rejection, and confirmation release-gate behavior
src/stock_analyzer/data/tushare_provider.py
src/stock_analyzer/data/akshare_provider.py
src/stock_analyzer/data/efinance_provider.py
src/stock_analyzer/data/background_adapter.py
scripts/build_tdx_offline_package.py
src/stock_analyzer/runtime/universe_candidate_selector.py
  同日市值读取、缺值保留
```

本次工作树有运行定向测试和本地研究数据重建；重建输出须与真实因子覆盖、输入 SHA 和单测一起复核。
未部署生产。任何 `release_eligible=false` 的开发报告不能转成上线许可。
