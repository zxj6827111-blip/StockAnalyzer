# trend 尾盘链路：§4 验收证据（As-of 2026-10-08 @ `e7023c1` 之后的工作树）

范围只覆盖选股质量改进计划第一轮落地到 trend 的这条链路：夜扫观察池 → 次日
14:30–14:50 尾盘确认 → 最多 3 只最终推荐 → 成交与 5 交易日退出。monster 与旧
`runtime/service.py::_final_signal_selector` 不在本文件的主张范围内。

结论先说：**工程验收已通过（下表逐场景有测试钉住）；选股质量验收仍是
`blocked`，本文件没有、也不会给出任何命中率数字。**

---

## 1. 工程验收：计划列出的 13 个场景逐条对应到真实测试

| 场景 | 钉住它的测试 |
| --- | --- |
| 时区跨日 | `test_confirmation_datetimes_are_local_to_the_trading_day`、`test_cross_day_bars_do_not_leak_into_the_tail_window` |
| 节假日 / 非交易日 | `test_holiday_and_non_trading_day_are_not_tail_days` |
| 尾盘窗口边界 | `test_tail_window_slots_start_inclusive_end_exclusive`、`test_live_path_never_evaluates_a_slot_that_has_not_arrived_yet`、`test_stale_live_quote_blocks_confirmation_instead_of_using_old_bar` |
| 确认后成交（下一根，不吃确认槽本身） | `test_fill_uses_the_next_completed_bar_after_confirmation_not_the_slot_bar`、`test_confirmation_never_reads_an_unfinished_bar` |
| 涨 / 跌停 | `test_limit_up_locked_fill_is_no_fill`、`test_limit_up_locked_entry_is_recorded_as_no_fill`（链路级）、`test_unsellable_exit_day_defers_and_maturity_is_the_actual_exit`、`test_missing_price_or_limit_data_fails_closed_without_guessing` |
| 停牌 | `test_suspended_fill_bar_is_no_fill_but_not_a_loss`、`test_halt_code_in_trade_status_blocks_the_buy`、`test_halted_exit_day_defers_instead_of_assuming_a_sell`、`test_missing_trade_status_is_not_read_as_suspension`、`test_missing_bar_is_not_reported_as_suspension` |
| T+1 | `test_no_tp_or_sl_can_trigger_on_the_entry_day_because_of_t_plus_1`、`test_entry_on_or_before_decision_day_is_rejected` |
| 跳空止损 | `test_gap_down_stop_exit_uses_the_open_not_the_stop_level` |
| 同根双触发 | `test_double_trigger_same_bar_resolves_to_stop_loss_first` |
| 未成熟 / 顺延 / 数据末尾 | `test_unsellable_exit_day_defers_and_maturity_is_the_actual_exit`、`test_defer_window_exhaustion_does_not_fake_a_successful_exit`、`test_series_end_before_plan_exit_is_uncertain_not_realized_profit`、`test_immature_exit_stays_uncertain_without_a_label`、`test_undeclared_trade_status_is_uncertain_fail_closed`、`test_uncalculable_corporate_action_is_uncertain` |
| 最低佣金 / 申报数量 / 成本日期版本 | `test_minimum_commission_applies_on_a_10k_notional_order`、`test_reference_notional_below_one_lot_is_no_fill`、`test_cost_schedule_is_date_versioned_and_reports_provenance`、`test_net_return_charges_both_sides_of_cost`、`test_engine_and_label_share_one_cost_authority` |
| 特征 / 数据缺失 | `test_missing_feature_snapshot_is_a_visible_caveat`、`test_missing_minute_bars_are_reported_not_replaced_by_open_price`、`test_malformed_probability_is_dropped_not_coerced_to_zero`、`test_missing_limit_prices_stay_visible_instead_of_being_filled`、`test_index_gap_is_insufficient_not_zero_filled` |
| 模型身份异常 | `test_model_identity_violations_produce_zero_recommendations`、`test_missing_model_identity_fails_closed`、`test_serving_model_from_another_label_policy_is_not_usable`、`test_missing_serving_manifest_is_visible_not_defaulted` |
| 标签口径未绑定（registry 查不到 / 另一套 TP·SL / registry 没接线） | `test_registered_label_policy_is_positively_verified`、`test_declared_label_policy_absent_from_registry_is_named`、`test_unwired_registry_is_reported_as_unavailable`、`test_label_policy_with_other_tp_sl_is_drift_not_a_match`、`test_verify_names_the_cause_instead_of_returning_a_bare_none`、`test_registration_cli_uses_real_exit_codes` |

**相同输入下线上与历史路径必须给出一致的筛选与交易判定**（对应发布清单 R15）分两层钉住：

- 入场判定层：`test_live_and_history_paths_agree_on_identical_input`。
- 链路层：`test_live_and_history_chain_paths_agree_on_identical_bars` —— 同一批分钟 bar
  与同一组概率，比较 `final_symbols`、漏斗计数、拒绝原因、成交数量/金额/成交时刻；
  显式**不**比 `trace_digest`，因为两条路径的数据时间本来就不同（线上=时钟 14:45，
  历史=最后一根已完成 bar 14:44），这一点由
  `test_history_mode_stamps_the_explicit_trade_date` 钉住。

**§3.2 的另一条门：历史不可复现的信息不得进训练**（新闻 / 主题 / completion /
板块配额 / 探索样本）。这一条此前**只有声明没有牙**：`ADVISORY_SOURCES` 在 `src/`
里没有任何消费方，唯一碰它的测试是同义反复（`source in ADVISORY_SOURCES` 恒真）。
现在由 `feature/trend_candidate_contract.assert_training_features()` 落地，逐条钉住：

- `test_non_reproducible_information_is_refused_by_training` —— 带 news/theme/completion/
  sector_quota/exploration/analyst 词根的列一律按"不可复现的信息源"拒绝；
- `test_columns_outside_the_four_groups_are_refused_too` —— 不认识的列也拒（"未登记"），
  空清单与重复列各拒一次；
- `test_the_four_reproducible_groups_are_training_eligible` —— 四组整体与逐组都放行，
  门不许把自己的输入挡掉；
- `test_advisory_bonus_rules_are_declared_and_never_hard_gates` —— 五个建议性规则名
  都必须在 `_RULE_KIND` 里且分类为 `predictive`，不得进 `HARD`；
- 消费侧：`test_non_reproducible_features_are_refused_before_any_sample_work`（训练入口
  排在查样本之前，`rows=[]` 仍报特征问题）、`test_walk_forward_refuses_features_it_cannot_reproduce`、
  `test_cli_refuses_illegal_features_before_reading_samples`（退出码 5，且不落报告）。

放行清单只有 `REPRODUCIBLE_FEATURE_COLUMNS` = 四组已有行情列；线上打分侧本来就只能
拿到 `build_trend_feature_frame()` 产出的这些列，所以训练与线上用同一份白名单。

**§3.1 的记录语义：本轮另外补上的三处真实缺陷**（来自对本分支的逐条审计，不是重构）：

1. **身份读的是清单里不存在的顶层字段**。`build_serving_manifest` 产出的
   `model_serving_manifest.v1` 把 `label_policy_id` 放在 `serving`、`model_id` 放在
   `registry`、权威哈希放在 `authority`，顶层只有 `schema`/`generated_at`/`source`；
   而 `_resolve_model_identity()` 原来按顶层读 —— 对着真实清单会**永远读空**，
   "绑定实际加载的模型"只在测试用的扁平 fixture 里成立。现在由 `_manifest_field()`
   按真实分层读，顶层仅作回退。钉住：`test_identity_binds_from_the_real_v1_manifest_sections`。
2. **记录失败必须可见**（计划原话）。读不到清单原来只留一个空 dict、运行 commit
   读不到只留 `""`、特征计算版本读不到只留 `0`，事后无法区分"没配路径 / 读盘炸了 /
   内容为空"。现在这些以 `model_identity.recording_failures` 落进留档，并由
   `page_view` 折成 `identity_recording_failed:*` 的 caveat 显示。钉住：
   `test_missing_serving_manifest_is_visible_not_defaulted`、
   `test_raising_manifest_reader_is_named_not_swallowed`、
   `test_v1_manifest_without_a_code_commit_blocks_and_names_the_cause`、
   `test_identity_binding_failure_shows_up_as_a_page_caveat`。
   顺带把留档每层的 `feature_compute_version` 改成取自**同一个已验证身份**，
   不再二次读模块常量（两处读值可以不一致，身份只有一份）。
3. **留档时刻没有时区**。`write_trace()` 原来写裸 `datetime.now()`，宿主机偏移一变
   NAS 与本地的留档就对不上，而这些留档是影子验证唯一的证据来源。现在写
   契约时区（Asia/Shanghai）的带偏移时刻并附 `written_at_timezone`。钉住：
   `test_written_at_is_the_contract_timezone_not_the_host_clock`。

4. **§3.1"补齐并校验…在独立研究库补齐后验证"的另一半**：研究库原先只有分钟表，
   日历 / RAW 日线 / 精确涨跌停 / 停复牌 / 证券历史状态只能读生产仓库。现在
   `research/tail_reference_store.py` + `scripts/sync_tail_reference_data.py` 把这五类
   **只读**复制进同一个研究库。钉住：
   `test_only_raw_daily_bars_are_landed_and_deduplicated`、
   `test_adjusted_basis_is_refused_rather_than_rescaled`、
   `test_missing_reference_rows_stay_missing`（缺就是不填 0、不猜 normal）、
   `test_approximated_limit_prices_are_not_usable_by_default`、
   `test_calendar_flags_rows_the_authority_disputes`（日历用
   `data/trading_calendar.is_open_trading_date` 交叉核对，不让行情自我证明）、
   `test_missing_warehouse_tables_are_reported_as_gaps`、`test_row_level_provenance_beats_the_caller_default`、
   以及 CLI 三条退出码测试。

5. **§3.1 的"验证"另一半 + §4 的"线上与历史一致"落到结构上**：参考库建好后没人读它
   就等于没有 —— 标签与历史重建此前不碰 `execution_inputs()`，所以"涨停锁死"在纯研究库
   路径上无法自证。现在 `research/tail_rebuild.py` + `scripts/rebuild_tail_labels.py`
   把日级精确涨跌停/停复牌喂进 `bars_for(day_limits=...)`、把 raw 日线序列喂进出场，
   判定出口仍然只有 `build_tail_net_profit_label` 一个。同时修掉一个 fail-open：
   `bars_for()` 原先给状态列为空的分钟 bar 补 `trade_status="normal"`，这会**吞掉**
   `suspend_d` 传进来的权威状态（停牌股被当成可买）。钉住：
   `test_day_limits_reach_the_minute_bars_so_limit_up_lock_is_provable`、
   `test_suspend_flag_from_the_reference_store_blocks_the_fill`、
   `test_trade_status_from_the_source_is_not_masked_by_a_default`、
   `test_missing_minute_bars_are_insufficient_and_never_an_open_price_backtest`、
   `test_reference_gaps_are_named_not_folded_into_the_fill_rate`、
   `test_a_session_hole_inside_the_window_blocks_instead_of_shifting_day_five`、
   `test_missing_status_declaration_yields_no_realized_label_but_is_not_a_gap`、
   `test_live_and_rebuild_paths_agree_on_identical_bars`（同一批 bar，带线上时钟跑一次、
   走重建跑一次，成交时点/价格/标签逐项相同）、`test_summary_keeps_gaps_fill_rate_and_labels_apart`、
   `test_live_service_and_contract_share_one_confirmation_object`（确认谓词从服务私有函数
   提升为契约的 `hard_gate_confirmation`，两边引用同一个对象）。

6. **就绪门现在校验研究库副本**（§3.1 "补齐后验证"里"验证"那一步）。生产仓库那一组
   检查只回答"源头有没有数据"；副本那一组回答"复制过来的东西能不能真的拿来判成交与
   出场"。缺必要来源、混进非 raw 口径、副本内日历与行情互相矛盾、一条显式
   `trade_status` 都没有 → **blocked**；精确涨跌停或状态声明覆盖不足、
   `ref_security_status` 空 → **insufficient**；不传 `--reference-db` 时留一条
   `reference_copy_validated = insufficient`，免得把"没校验"读成"校验通过"。
   探测全程只读，不建表。钉住：
   `test_unvalidated_reference_copy_is_named_not_assumed`、
   `test_validated_reference_copy_lifts_readiness_to_ready`、
   `test_missing_reference_sources_block_instead_of_reporting_zero_coverage`、
   `test_no_declared_trade_status_blocks_label_production`、
   `test_approximated_limit_prices_are_not_counted_as_exact`、
   `test_calendar_conflict_inside_the_copy_blocks_the_rebuild`、
   `test_legacy_adjusted_rows_in_the_copy_are_refused`、
   `test_empty_security_status_is_insufficient_not_blocked`、
   `test_cli_reads_the_reference_copy_only_when_told_to`。

---

## 1a. 数据侧新查出的一个真实缺口（不是代码问题）

生产仓库 `daily_trade_status` 表**没有 `trade_status` 列**
（`data/market_warehouse.py:355-365` 只有 `suspended / suspend_type`），而
`read_warehouse_reference_frames()` 同时把这张表当作涨跌停与停复牌两个来源。
后果：从仓库同步出来的研究库里 `trade_status` 恒为 NULL → 出场逐日判定落到
`unknown_trade_status` → **真实重建样本 0 条可训练**。这不是策略亏损，是来源没补齐；
补齐方式（给该表加显式状态列，或在同步层声明"`suspended` 布尔即状态"）属于数据层决策，
本轮不顺手改语义，只把它计进 `undeclared_status_days` 与报告的
`trade_status_source_gap`。

---

## 1b. §2 的 9 层漏斗：谁在写、还差谁

`FUNNEL_LAYERS` 声明 9 层，本表说明**当前谁产出**（`grep` 可核对，不靠记忆）：

| 层 | 生产者 | 状态 |
| --- | --- | --- |
| `universe` / `hard_eligibility` / `quality_300` / `light_100` / `deep_50` | `alpha_v2/validation/production_funnel.py`（夜扫 source evidence + counts + 防篡改哈希） | 由**既有 Alpha V2 证据链**负责，按计划与本契约保持独立；不并成一条 trace |
| `night_watch_pool` / `tail_confirmation` / `final_recommendation` | `runtime/services/trend_tail_shadow_service.py` | 已产：逐层输入/晋级/拒绝原因/特征/身份/数据时间，最终推荐另存特征快照 |
| `execution_exit` | `research/tail_mature_feedback.attach_exit_outcomes()` + `execution_exit_stage()`，由 `scripts/record_tail_exit_funnel.py` 落档 | 已产：退出**成熟后**写第二份留档（`funnel_trace_<date>_execution_exit.json`），不覆盖入场那份；未成交/不确定/未成熟/无标签记录都留在拒绝原因里 |

代价说清楚：因为跨两条链，"一次查询看完整个漏斗"目前做不到，只能靠同一个
`trade_date` + `contract_digest` 手工对齐。

---

## 2. 本轮真实执行过的命令与结果

| 命令 | 结果 |
| --- | --- |
| `pytest tests/test_trend_strategy_contract.py tests/test_trend_tail_shadow_runtime.py tests/test_trend_tail_page_and_feedback.py tests/test_minute_bar_store.py tests/test_funnel_trace.py tests/test_tail_net_profit_label.py tests/test_trend_data_readiness.py tests/test_tail_net_profit_trainer.py tests/test_trend_candidate_contract.py tests/test_tail_walk_forward.py tests/test_tail_exit_funnel.py tests/test_tail_reference_store.py -q` | **251 passed**（条数：55/22/26/15/17/19/13/20/16/20/14/14） |
| `pytest tests -k "trend or tail"` | **252 passed, 4029 deselected** |
| `pytest tests -k "week5 or live_runtime or automation"` | **319 passed, 3948 deselected**（身份绑定改动没有破坏既有自动化链） |
| `ruff check` 本轮触及的 6 个源文件 + 7 个测试文件 + 3 个 scripts CLI | All checks passed |
| `ruff check src tests`（仓库全量） | 48 errors —— 全部落在分支未触碰的文件，属既有基线 |
| `mypy --python-version 3.12` 本分支源文件 | **4** errors，逐条用 `git show HEAD:<file>` 对照确认**都是既有噪声**：`research/funnel_trace.py:355`（HEAD 上是同一处，行号从 339 移到 355）、`research/tail_mature_feedback.py:313`、`models/tail_net_profit_trainer.py:293/296`（LightGBM 注入点，运行时已由 `native booster trainer is unavailable` 硬门拦住）。新增的 `research/tail_reference_store.py` 本轮实测 **0 error**。 |
| `python scripts/run_quality_gate.py --stage clean-scope --fail-on-error` | `blocking_failures = ["mypy_blocking"]`，**不是本分支引入**：报错是 `.venv` 里 numpy stub 的 `Type statement is only supported in Python 3.12 and greater`，而 `pyproject.toml` 钉 `python_version = "3.11"`，mypy 在检查任何项目文件之前就终止（"errors prevented further checking"）。同一命令加 `--python-version 3.12` 后，那 4 个目标文件（均非本分支文件）只剩 1 个既有 `var-annotated`。 |

| `env -u PYTHONPATH python scripts/audit_trend_data_readiness.py --help` | 正常输出用法。本轮修掉了一个真实缺陷：该 CLI 缺 `src/` 路径自举，照本文件 §3 的命令去做解锁的人第一跳就是 `ModuleNotFoundError`；测试原来靠注入 `PYTHONPATH` 掩盖了它，现已改为不注入。 |

未执行、因此不主张：`--stage full`、freeze、Production Preflight、任何生产部署。

### 2b. 消费侧接线那一轮真实执行的命令（As-of 同一工作树）

| 命令 | 结果 |
| --- | --- |
| `pytest tests/test_tail_rebuild.py --collect-only` | **18 tests collected**（新文件） |
| `pytest <13 个 trend/tail/minute/funnel 测试文件> --tb=no` | **269 passed in 30.58s** |
| `pytest tests/ -k "trend or tail" --tb=no` | **270 passed, 4029 deselected in 37.38s** |
| `pytest tests/ -k "trend or tail or minute or funnel"` | 1 个 error：`test_alpha_v2_m4l_e2e_rehearsal` 依赖 lightgbm，本机 `.venv` 缺 `libomp.dylib` → 属既有环境基线，不是本分支引入 |
| `ruff check` 本轮触及的 4 个源文件 + 1 个 CLI + 1 个测试文件 | All checks passed |
| `ruff check src tests` / `ruff check scripts` | 48 / 27 errors —— 与分支起点同数，本轮没有新增 |
| `mypy --python-version 3.12` 本轮 4 个源文件 | 这 4 个文件**自身 0 error**（其余报错来自被跟进导入的历史文件） |
| `python -c "…_cost_side(config/default.yaml, 2026-03-09)"` | 冻结成本表可用：900 股 @10.05 买费 ¥5.09（最低佣金生效）、@10.60 卖费 ¥9.87（含印花税），trend 滑点 **0.0015**。这一步是必要的：`resolve_tail_slippage_ratio()` 的 `matcher` 参数要的是配置对象，先前误传 `ExecutionMatcher` 壳会**静默取到 0 滑点**，已改并留注释 |

### 2c. 就绪门校验研究库副本那一轮

| 命令 | 结果 |
| --- | --- |
| `pytest tests/test_trend_data_readiness.py --collect-only` | **22 tests collected**（原 13 条 + 副本校验 9 条） |
| `pytest tests/test_trend_data_readiness.py tests/test_minute_bar_store.py` | **37 passed in 56.64s**（22 + 15；没有一条既有断言被放宽） |
| `pytest tests/ -k "trend or tail"` | **279 passed, 4029 deselected in 67.45s**，exit 0 |
| `ruff check` 本轮触及的 1 个源文件 + 1 个 CLI + 1 个测试文件 | All checks passed；全量 `src tests` 48 / `scripts` 27，与基线同数 |
| `mypy --python-version 3.12 research/trend_data_readiness.py` | 该文件自身 **0 error** |
| `python scripts/run_quality_gate.py --stage clean-scope --fail-on-error` | 退出码 2：`ruff_clean_scope` **returncode 0**，唯一阻塞项仍是 `mypy_blocking` —— `.venv` 里 numpy stub 的 `Type statement is only supported in Python 3.12 and greater`，而 `pyproject.toml` 钉 `python_version = "3.11"`，mypy 在检查任何项目文件前就终止。加 `--python-version 3.12` 复跑那 4 个目标文件（**都不是本分支触碰过的文件**）只剩 1 个既有 `acceptance_service.py:1078 Need type annotation for "equity"`。结论：本轮改动没有给门新增失败，门本身为既有环境错配而红 |

一处**既有行为被收紧**，写清楚：以前不传 `--reference-db` 时，只要生产仓库 + 分钟库达标就报
`ready`；现在会多一条 `reference_copy_validated = insufficient`（退出码 3）。理由是
"没校验"不能占"校验通过"的位置。受影响的既有测试 `test_bar_timestamped_minute_table_lifts_the_block`
已改为同时提供已校验副本，而不是把新检查降级。审计器在本仓库**没有任何 runtime/调度调用方**
（只有这个 CLI 与测试），所以这次收紧不影响生产路径。

---

## 3. 选股质量验收：当前状态 = blocked（不是"通过"，也不是"失败"）

计划要求：按交易日滚动训练/校准/测试、标签在后续阶段开始前真实成熟、**≥4 个测试折**、
净盈利率较匹配基线提升 ≥5pp 且分块 bootstrap CI 下界 > 0、平均净收益为正、尾部不明显
恶化，然后 **≥60 个完整交易日 + ≥100 笔成熟模拟成交** 的未来影子验证。

阻塞原因是数据可得性，不是代码：

1. 落库的分钟行情只有日级聚合（`intraday_summary_1m/5m` 没有 bar 时刻列），
   14:30–14:50 的逐 5 分钟确认与"确认后下一根成交"**无法从历史库重建**。
   时刻信息在 vendor 分钟包里是存在的，是聚合步骤把它丢掉了 —— 所以这是持久化选择，
   不是数据不可得。已补：`research/minute_bar_store.py` + `scripts/sync_tail_minute_bars.py`
   + `audit_trend_data_readiness.py --minute-db`。
2. 本机上没有任何真实 vendor 分钟包，因此**历史覆盖度至今未被测量过**。
3. `p_net_profit_5d_tail` 目前**没有生产者**：影子链路逐日写下的阻塞原因是
   `no_tail_probability_available` 与 `minute_bars_unavailable`，这是事实而非待填的空格。

解锁顺序（每条命令都真实存在）：

```bash
# 1. 本地把带时刻的分钟行情落到独立研究库（不碰生产仓、不上 NAS 调度）
python scripts/sync_tail_minute_bars.py --root <vendor 分钟包目录> \
    --out artifacts/research/tail_minute_bars.duckdb \
    --start 2024-01-01 --end 2026-09-30 \
    --price-basis raw --bar-time-semantics bar_start

# 2. 把日级参考数据复制进同一个研究库（日历 / RAW 日线 / 精确涨跌停 / 停复牌 / 证券状态）
python scripts/sync_tail_reference_data.py \
    --warehouse <market.duckdb> \
    --out artifacts/research/tail_minute_bars.duckdb \
    --start 2025-01-02 --end 2026-09-30 \
    --symbols-file artifacts/research/tail_symbols.txt
# 退出码：0=五类来源都补齐 / 3=有来源缺失（如实记为不足，继续采集）/ 5=仓库不可读
# 生产仓库只以 read_only 打开；缺表会报 <source>_table_missing，不会补 0 冒充已补齐

# 3. 测量覆盖度；readiness 仍是 blocked 就不要往下走
#    --reference-db 校验研究库里的日级参考副本（默认就是分钟库那个文件）；
#    不传就只会得到 reference_copy_validated = insufficient（没校验 ≠ 校验通过）
python scripts/audit_trend_data_readiness.py \
    --db <market.duckdb> --minute-db artifacts/research/tail_minute_bars.duckdb \
    --reference-db artifacts/research/tail_minute_bars.duckdb

# 3b. 从研究库重建 replayed 样本（标签侧；特征侧由调用方按同一个 as-of 契约拼接）
python scripts/rebuild_tail_labels.py \
    --db artifacts/research/tail_minute_bars.duckdb \
    --requests artifacts/research/tail_requests.jsonl \
    --labels artifacts/research/tail_replayed_labels.jsonl \
    --report artifacts/research/tail_rebuild_report.json
# 退出码：0=全部判得动 / 3=有请求参考数据不足（记为阻塞，单列计数，不折算成未成交）
#        / 5=请求或研究库不可用。费用默认取 config/default.yaml 的冻结成本表；
# 只有显式 --zero-cost 才算无费用调试标签，报告会写 cost_model=zero_cost_debug
# 若报告里 missing_reference_inputs 含 entry_minute_bars → 分钟数据仍不足，
# 按 §5 要求记为阻塞继续采集，**不得改用开盘回测**

# 4. 覆盖度达标后跑滚动验证：≥4 折、匹配基线对照、真实退出码
python scripts/validate_tail_selection_quality.py \
    --samples artifacts/research/tail_samples.jsonl \
    --features excess_ret_20,ma20_slope,avg_turnover_20,atr14_pct --model-id <id> \
    --training-commit <sha> --runtime-commit <sha> \
    --feature-compute-version <n> --label-policy-id <label_policy_v4_...>
# 退出码：0=质量门通过 / 3=样本不足 blocked / 4=跑完但门不过 / 5=训练或身份失败
```

第 4 步的编排已经存在（`research/tail_walk_forward.py`：折边界、注入 split 的
embargo 核对、observed/replayed 分开计数、身份不通过就整轮不成立；20 条测试见
`tests/test_tail_walk_forward.py`），缺的只是第 1~3 步在真实仓库上落出来的数据。
**这套编排至今只在合成样本上跑通过，没有在真实历史数据上跑过 —— 因此本文件
不为任何真实命中率背书。**

第 2 步未达标时的处理方式是计划里定死的：**尾盘策略验证明确记为阻塞、继续采集，
不得用开盘价回测顶替**（`contracts/trend_strategy.py` 对
`execution_price_basis != "raw"` 与开盘入场语义直接 raise）。

---

## 4. 本文件明确不主张的事

- **0.60 是初始选股规则，不是已证明 60% 命中率。** 没有任何 observed 样本支撑该数字。
- **特征白名单只证明"训练输入在夜扫当时可复现"，不证明这四组在时间外真的有效。**
  `assert_training_features()` 挡的是 look-ahead，逐组消融的结论仍要等真实标签。
- **bronze 样本占比没有被认定为当前生产模型的根因**；根因清单里它是假设，
  见 `.agents/notes/NOTE-002-selection-quality-root-causes.md`。
- 旧完整链路在同一天**没有可比成交样本**，因此"较旧链路提升 X pp"当前无法计算，
  也不得伪造。
- `runtime/service.py::_final_signal_selector`、`backtest/holding_curve.py` /
  `AsofBacktestConfig` 仍是旧开盘口径，**未被尾盘链路背书**，也未接线。
- 自动学习只产出 challenger；正式模型更新必须走验证 + 人工发布
  （`tail_mature_feedback.py` 的 `promotion` 字段是写死的人工票据串）。

---

## 5. 当前完成度层级

```text
代码完成  ✔（含影子链路、契约、留档、页面、反馈）
测试完成  ✔ 工程验收场景（251 + 319 passed，见 §2；接线那一轮再 +18 → 269/270 passed，见 §2b）
业务验证  ✘ 选股质量验收 blocked（§3：历史分钟数据覆盖度未知，且无真实标签）
Freeze Ready  ✘ 未执行
Production Ready ✘ 未执行；旧路径仍在服务真实推送
已部署生产    ✘ 本轮没有任何生产动作
```

---

## 6. 本轮审计查出的、仍然未实现的事项（不含 §3 的数据门槛项）

这些不是"待跑数据"，是**代码里根本还不存在的路径**。列出来是因为把"已接线"
说成"已完成"，正是本计划 §2 要消灭的那类自欺。

| # | 缺口 | 可核对的事实 | 影响 |
| --- | --- | --- | --- |
| 1 | v4 净盈利标签策略**没有任何 runtime/CLI 注册入口** | `labels/tail_net_profit.py:74 tail_label_policy_record()` 的 docstring 写着"供 ``LabelPolicyRegistry.register`` 落库"，但 `grep -rn tail_label_policy_record src/` 只命中定义文件本身，调用方只有测试 | 训练与留档里的 `label_policy_id` 只能由外部给定；"新增独立标签"还没进 registry |
| 2 | **没有按版本解释旧记录的读取器** | `scripts/record_tail_exit_funnel.py` 对 `contract_digest` 不一致直接退 5；`TailLabelRecord.from_dict` 缺任一字段即 raise；`funnel_trace.read_trace()` 是裸 JSON | 对**新**记录这是对的（不接受别的口径写证据），但计划要求的"旧记录保留原始值 + 带版本解释规则兼容"目前只有 `label_policy_v4_*` 这个名字，没有实现 |
| 3 | `model_serving_manifest.v1` 里**没有 code commit 字段** | `build_serving_manifest` 的 payload 顶层只有 `schema`/`generated_at`/`source`/`serving`/`registry`/`authority`/`research_fail_closed`，其中不含 commit | 对着**真实**清单，尾盘身份必然 `training_commit_unknown` ⇒ 0 只（fail-closed，行为正确但链路是黑的）。要打通得先扩清单 schema —— 那是 ADR-001 的信任边界变更，需单独决策，不在本轮顺手改 |
| 4 | ~~研究侧的参考数据没有写入通路~~ → **本轮已建**（`research/tail_reference_store.py` + `scripts/sync_tail_reference_data.py`，14 条测试）；~~消费侧没接线~~ → **本轮已接**（`research/tail_rebuild.py` + `scripts/rebuild_tail_labels.py`，18 条测试）：五类来源按主键幂等落进研究库，只认 raw 日线，缺表报 `<source>_table_missing`，比例推算的上下限默认不可用；重建把日级精确涨跌停/停复牌喂进分钟 bar，"涨停锁死""停牌"在纯研究库路径上已自证 | 仍缺三件事：①在**真实** `market.duckdb` 上跑一次 sync + rebuild 并留下报告（本机没有该文件，只能证明逻辑，不能证明覆盖）；②生产库的 `security_status` 本身就是 0 行（`upsert_security_status` 零调用方），复制过来的也是空 → 这一路仍会报缺；③`daily_trade_status` 表**没有 `trade_status` 列** → 真实重建全量落 `unknown_trade_status`、0 条可训练（见 §1a）。第 ③ 条是数据层来源缺口，补齐前要单独决策，本轮不顺手改状态语义 |
| 5 | 影子链路**当日重跑没有幂等键** | `week5_automation_service.py` 的夜间扫描有 `_idempotent_night_scan`，而调用尾盘影子那条没有；`write_trace` 固定落 `funnel_trace_<date>.json` | 覆盖是确定性的（同样输入同样结果），但"今天跑过几次"不可见；60 交易日影子统计开始前应补一个运行序号或运行哈希 |

第 3 条决定了另一件必须说清的事：**影子链路目前是"必然 0 只"的状态**，直到有一份
带 commit 的在服清单存在。这是设计上的 fail-closed，不是 bug，但也意味着 §4 的
"≥60 个完整交易日 + ≥100 笔成熟模拟成交"在模型真正训出来之前不可能开始累积。
