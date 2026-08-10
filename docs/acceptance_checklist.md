# PRD v7 验收标准核对清单（32 条）

- 日期：2026-08-10
- 分支：fix/audit-p0-r1（基于 main 8b90ccc + 24 commits）
- 依据：implementation_plan.md §21（第 710-743 行）+ 全仓库代码/测试核实
- 状态图例：✅ 已实现+测试｜⚠️ 部分/待补｜❌ 疑似缺失｜⏳ 需实跑验证

## 总览

| 状态 | 条数 | 条目 |
|---|---|---|
| ✅ 已实现+测试 | 20 | #1 #2 #4 #5 #8 #10 #12 #13 #14 #16 #21 #22 #23 #24 #25 #27 #28 #29 #30 #11 |
| ⚠️ 部分/待补 | 6 | #3 #15 #18 #26 #31 #32 |
| ❌ 疑似缺失 | 2 | #9 #19（#20 与 #9 同源，见下） |
| ⏳ 需实跑 | 3 | #6 #7（+ #3/#26/#17 的达标属性） |

## 明细

| # | 验收标准 | 状态 | 实现证据 | 测试证据 | 剩余动作 |
|---|---|---|---|---|---|
| 1 | 系统侧全自动运行，唯一人工动作：15:30 对账录入 | ✅ | service.py `_register_default_jobs`（15886-16170）注册 13+ 类任务；close_reconcile 于 config.py:823 15:30 | test_service_scheduler.py:220-496、test_service_closed_loop_flow.py:256-331 | 无 |
| 2 | 数据源故障可降级+自动停新仓 | ✅ | resilient_provider.py（连续失败→degraded）→ pipeline → risk/controls.py:150-156 → soup hold | test_resilient_provider/test_risk_controls/test_pipeline（本轮补恢复链路） | 无 |
| 3 | 端到端≤60s、首板≤5min、异常≤30s | ⚠️ | sla_report（service.py:12229，target 60s/alert 30s）；backpressure 60s（config.py:446） | test_service_dashboard.py:142、test_evolution_latency_slo.py | **首板≤5min 与代码 monster_scan_sla 15min 不一致**：需澄清 PRD 口径或调配置；实际耗时须实跑 |
| 4 | 无未来函数，回测与线上一致 | ✅ | time_semantics.py 四时间戳不变量（回测端）；本轮撮合引擎统一：滑点/tick/手数/费用两端同一实现 | test_backtest_live_consistency.py 12 用例（双模式）；test_walk_forward.py:45 | P2 灰度项（线上 T+1/涨跌停）已被契约测试锁定，开启时逐项灰度 |
| 5 | 双模型信号可追溯 | ✅ | service.py:9137 `_build_signal_payload_with_recommendation_ids`；predictor 输出 lgbm/xgb/meta | test_main_dashboard_actions.py:271、test_service_portfolio.py:2235 | 无 |
| 6 | 连续 5 交易日稳定运行 | ⏳ | scripts/run_scheduler_soak.py（可配 days） | 无 pytest 断言 | **NAS 实跑 5 个交易日** |
| 7 | 压力测试：极端行情回撤<25% | ⏳ | stress/scenarios.py:36-53（max_drawdown_limit_pct=25） | test_stress_scenarios.py:27-40（合成行情 6 场景过） | **真实极端行情压测实跑** |
| 8 | 资金曲线保护线 95% 有效触发 | ✅ | config.py:325 protect_line=0.95；risk/controls.py:56-57 freeze | test_service_portfolio.py:2229 | 无 |
| 9 | 喝汤参数不可盘中修改 | ❌ | **未找到**：无 freeze_windows 配置、无冻结机制（计划 §8.7 的 09:15-15:00 冻结+OTP 未落地）；现状"无运行时改参 API"仅为隐式满足 | 无对应测试 | **需决策**：补冻结机制实现，或与 #20 合并降级为"文档化约束" |
| 10 | 回撤熔断可触发（单日2.5%/单周4%） | ✅ | config.py:332-333；risk/controls.py:86-96 should_pause/should_reduce | test_risk_controls.py:53、test_stress_scenarios.py | 无 |
| 11 | 执行质量可追踪 | ✅ | sample_store execution outcome（realized_slippage_bp/fill_ratio）、execution_risk_labels、week7 对账 | test_learning_sample_store.py、test_service_week7_sim_broker.py | 无 |
| 12 | 指令通道正常（签名+幂等） | ✅ | SignedCommandProcessor（HMAC 验签、时间戳窗口、command_id 去重） | test_security.py（security 终审确认） | 无 |
| 13 | 智能推送过滤生效 | ✅ | notify/filter.py NotificationFilter（安静窗/冷却去重/阈值） | test_notification_filter.py 9 用例 | 无 |
| 14 | Dashboard 面板正常 | ✅ | api/dashboard.py + 前端页面齐全 | test_main_dashboard.py、test_service_dashboard.py:92 | 无 |
| 15 | 竞价速报正常输出 | ⚠️ | auction_report 任务 09:26（config.py:822、service.py:15905/16564） | **无直接断言用例**（测试均绕过） | 补 1 个直接用例 + 实跑验证推送 |
| 16 | 跨市场因子正常 | ✅ | week6/engines.py:170 GlobalMarketFactorEngine | test_service_week6.py:238、test_main_week6.py:143 | 无 |
| 17 | 异常告警 30 秒触达 | ⚠️ | 通知链路齐备（飞书/企微/pushplus + 本轮新增钉钉/短信冗余） | 通道级测试全过 | **30s 触达属性需实跑**（依赖网络与配置） |
| 18 | 模拟盘 vs 实盘周报自动生成 | ⚠️ | week7_sim_broker_service.py 功能完整（评分/导出/通知） | test_service_week7_sim_broker.py 3 用例 | **未注册调度器**（无自动触发）→ 补月末/周度调度注册（小改动） |
| 19 | 黑名单生效 | ❌ | **全仓库未找到个股黑名单过滤**（仅授权关键词/数据质量停摆日，非黑名单） | 无 | **需决策**：补实现（策略/标的黑名单）或明确由其他机制（妖股隔离/质量闸门）覆盖 |
| 20 | 参数冻结窗口生效 | ⚠️ | 与 #9 同源：未找到冻结窗口实现 | 无 | 与 #9 合并决策 |
| 21 | 全链路审计 trace_id 可回放 | ✅ | service.py:19330 `_record_audit_event`、trace_replay、api/audit.py | test_service_audit.py:80-103 | 无 |
| 22 | 回测撮合器执行 T+1 和不可成交规则 | ✅ | execution/engine.py can_buy/can_sell（t_plus_1_block、limit_up/down_reject、suspended） | test_backtest_matcher.py 11 用例、test_execution_engine.py | 无 |
| 23 | 标签与策略一致 | ✅ | labels/soup.py build_soup_labels、label_policy_registry | test_labels.py 6 用例 | 无 |
| 24 | 多策略分头阈值生效 | ✅ | service.py:14879 `_run_multi_strategy_pipeline`（allocation_weights 加权+分策略阈值） | test_service_multi_strategy.py:73/94 | 无 |
| 25 | 妖股隔离风控生效 | ✅ | service.py:18360 `_monster_isolation_gate`（总仓25%/单票8%/情绪<45） | test_service_week5.py:1947-2025 | 无 |
| 26 | 首板 1 分钟常态化达标 | ⚠️ | config.py:429 first_board_interval_min=1；区间任务注册 | test_service_scheduler.py:602-624（调度常态化） | **1 分钟性能达标无 SLA 断言，须实跑** |
| 27 | 流动性门槛过滤生效 | ✅ | pipeline.py:1193 `_liquidity_check`（三门槛）→ soup liquidity_filter | 本轮补齐 5 用例（三违规维度+边界） | 无 |
| 28 | 因子 IC 衰减报告月度输出 | ✅ | 本轮新增 research/ic_decay_report.py + 月末调度 + CLI | test_research_ic_decay_report.py 18 用例 | 无 |
| 29 | 策略自毁开关（连续3月跑输触发） | ✅ | service.py 自毁机制 + week7 kill_switch | test_service_week7_kill_switch.py | 无 |
| 30 | 云冷备：Mac 失联 15 分钟推送持仓快照 | ✅ | config.py:563-564（ping 10min/alert 15min）；run_cloud_backup_check | test_service_week7_cloud_backup.py 4 用例 | 无 |
| 31 | 长假前 3 天自动降仓 50% | ⚠️ | week6/engines.py:145 CalendarFactorEngine（pre_holiday_reduce_days=3→0.5） | test_service_week6_execution.py:269（0.5 缩放路径） | 实现为"周五前3交易日"近似，非交易日历；日期触发分支无直接测试（补 1 用例） |
| 32 | 动态滑点公式回测和实时均生效 | ⚠️→✅ | 本轮共享引擎：execution/engine.py，线上滑点已接线（apply_dynamic_slippage_live 开关） | test_backtest_live_consistency.py 双模式断言 | 开关默认关（shadow），**灰度开启后实跑确认** |

## 剩余动作清单

### A. 代码侧可立即做（约 1-2 人天）
1. #15 竞价速报：补 1 个直接断言用例（构造速报内容断言输出）
2. #18 周报：注册周度调度任务（自动生成）
3. #31 长假降仓：补 pre_holiday 日期触发分支测试
4. #9/#19/#20 决策：补冻结机制与黑名单实现（各 0.5-1 人天），或明确降级为文档化约束并修改验收口径

### B. 需实跑验证（NAS 环境，与代码并行）
- #6 连续 5 交易日稳定运行（soak 脚本在位）
- #7 极端行情压测回撤 <25%（stress 套件在位）
- #3 性能预算实际耗时（含首板 5min 口径澄清）
- #26 首板 1 分钟实际性能
- #17 告警 30s 触达实测
- #32 滑点 shadow 灰度开启

## 审计修正记录
1. 原审计判"#28 已实现"错误 → 实际完全缺失，本轮已补实现+测试（现 ✅）
2. 原审计判"#9/#19/#20 已实现"未经核实 → 本次核对发现疑似缺失（参数冻结、黑名单），需决策
3. 原审计判"#32 部分"→ 本轮撮合引擎统一后核心一致（滑点/tick/手数/费用），T+1/涨跌停线上接入留 P2 灰度并契约锁定
4. #18 周报"自动生成"存在调度缺口（功能完整但无自动触发），原审计未发现
