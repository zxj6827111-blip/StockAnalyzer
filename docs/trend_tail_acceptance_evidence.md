# trend 尾盘链路：§4 验收证据（As-of 2026-10-08 @ `335fcfd` 之后的工作树）

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
| `pytest tests/test_trend_strategy_contract.py tests/test_trend_tail_shadow_runtime.py tests/test_trend_tail_page_and_feedback.py tests/test_minute_bar_store.py tests/test_funnel_trace.py tests/test_tail_net_profit_label.py tests/test_trend_data_readiness.py tests/test_tail_net_profit_trainer.py tests/test_trend_candidate_contract.py tests/test_tail_walk_forward.py tests/test_tail_exit_funnel.py -q` | **237 passed**（条数：55/22/26/15/17/19/13/20/16/20/14） |
| `pytest tests -k "trend or tail"` | **238 passed, 4029 deselected** |
| `pytest tests -k "week5 or live_runtime or automation"` | **319 passed, 3948 deselected**（身份绑定改动没有破坏既有自动化链） |
| `ruff check` 本轮触及的 5 个源文件 + 6 个测试文件 + 2 个 scripts CLI | All checks passed |
| `ruff check src tests`（仓库全量） | 48 errors —— 全部落在分支未触碰的文件，属既有基线 |
| `mypy --python-version 3.12` 本分支源文件 | **4** errors，逐条用 `git show HEAD:<file>` 对照确认**都是既有噪声**：`research/funnel_trace.py:355`（HEAD 上是同一处，行号从 339 移到 355）、`research/tail_mature_feedback.py:313`、`models/tail_net_profit_trainer.py:293/296`（LightGBM 注入点，运行时已由 `native booster trainer is unavailable` 硬门拦住）。本轮新增代码没有带来新的类型错误。 |
| `python scripts/run_quality_gate.py --stage clean-scope --fail-on-error` | `blocking_failures = ["mypy_blocking"]`，**不是本分支引入**：报错是 `.venv` 里 numpy stub 的 `Type statement is only supported in Python 3.12 and greater`，而 `pyproject.toml` 钉 `python_version = "3.11"`，mypy 在检查任何项目文件之前就终止（"errors prevented further checking"）。同一命令加 `--python-version 3.12` 后，那 4 个目标文件（均非本分支文件）只剩 1 个既有 `var-annotated`。 |

| `env -u PYTHONPATH python scripts/audit_trend_data_readiness.py --help` | 正常输出用法。本轮修掉了一个真实缺陷：该 CLI 缺 `src/` 路径自举，照本文件 §3 的命令去做解锁的人第一跳就是 `ModuleNotFoundError`；测试原来靠注入 `PYTHONPATH` 掩盖了它，现已改为不注入。 |

未执行、因此不主张：`--stage full`、freeze、Production Preflight、任何生产部署。

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

# 2. 测量覆盖度；readiness 仍是 blocked 就不要往下走
python scripts/audit_trend_data_readiness.py \
    --db <market.duckdb> --minute-db artifacts/research/tail_minute_bars.duckdb

# 3. 覆盖度达标后跑滚动验证：≥4 折、匹配基线对照、真实退出码
python scripts/validate_tail_selection_quality.py \
    --samples artifacts/research/tail_samples.jsonl \
    --features excess_ret_20,ma20_slope,avg_turnover_20,atr14_pct --model-id <id> \
    --training-commit <sha> --runtime-commit <sha> \
    --feature-compute-version <n> --label-policy-id <label_policy_v4_...>
# 退出码：0=质量门通过 / 3=样本不足 blocked / 4=跑完但门不过 / 5=训练或身份失败
```

第 3 步的编排已经存在（`research/tail_walk_forward.py`：折边界、注入 split 的
embargo 核对、observed/replayed 分开计数、身份不通过就整轮不成立；20 条测试见
`tests/test_tail_walk_forward.py`），缺的只是第 1、2 步落出来的真实数据。
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
测试完成  ✔ 工程验收场景（237 + 319 passed，见 §2）
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
| 4 | 研究侧的**参考数据没有写入通路**，分钟库里的涨跌停/交易状态列恒为 NULL | `security_status` 表由 `data/market_warehouse.py:502` 建、`:1137 upsert_security_status()` 写，但这个方法在 `src/` 与 `scripts/` 里**除定义外零调用**（现状已由既有证据记录：`scripts/alpha_v2_m4h_evidence_map.py:232` "security_status 表 0 行"）；`scripts/sync_tail_minute_bars.py` 全文没有出现过 `up_limit/down_limit/trade_status`，只把 vendor 帧交给 `upsert_frame`，所以分钟表这三列只能是 NULL | 计划 §3.1 要求"补齐并校验…在独立研究库补齐后验证"。涨跌停与停牌因此只能靠生产仓库的 `trade_status` 联结，历史重建没有独立可验的副本；§3.3 的"涨停锁死不记成交"在纯分钟库路径上无法自证 |
| 5 | 影子链路**当日重跑没有幂等键** | `week5_automation_service.py` 的夜间扫描有 `_idempotent_night_scan`，而调用尾盘影子那条没有；`write_trace` 固定落 `funnel_trace_<date>.json` | 覆盖是确定性的（同样输入同样结果），但"今天跑过几次"不可见；60 交易日影子统计开始前应补一个运行序号或运行哈希 |

第 3 条决定了另一件必须说清的事：**影子链路目前是"必然 0 只"的状态**，直到有一份
带 commit 的在服清单存在。这是设计上的 fail-closed，不是 bug，但也意味着 §4 的
"≥60 个完整交易日 + ≥100 笔成熟模拟成交"在模型真正训出来之前不可能开始累积。
