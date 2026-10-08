# trend 尾盘净盈利概率链路：发布与回滚清单 / 影子验证方案

As-of: 2026-10-08 @ HEAD `e70d84d`（分支 `feat/stock-selection-quality-overhaul`）

配套决策见 `.agents/notes/ADR-003-trend-tail-strategy-contract.md`；
根因与阻塞见 `.agents/notes/NOTE-002-selection-quality-root-causes.md`。

本清单**不是**"照做就能上线"的操作手册，而是把"现在还不能上线"的判据写清楚：
第 0 节未通过，后面的节不必看。

---

## 0. 前置硬门（当前状态：**未通过**）

```text
[ ] P0-1 带时刻的分钟行情落库并可回补（当前 intraday_summary_1m/5m 只有日级聚合列）
[ ] P0-2 index_daily 常驻增量进调度（scripts/sync_index_daily.py 至今无调度入口，
       两库停在 2026-08-14）
[x] P0-3 scripts/audit_trend_data_readiness.py 退出码从 5(blocked) 降到 0/3
        —— **2026-10-08 实测已达**：`--db artifacts/research/market_copy.duckdb
        --minute-db/--reference-db artifacts/research/tail_minute_bars.duckdb`
        退出码 **3**、`readiness=insufficient`、`blocking_gaps=[]`。
        唯一 `insufficient` 项是 `column_concentration_float_market_cap`
        （2,611 个交易日里 80 天超过众数占比上限、众数正是 12,000,000,000）——
        仓库那一列按 §3.1 不就地改写，独立真值在研究库 `float_market_cap_ref`。
        同一份报告里 `tail_window_minute_bars` 已 ok：研究库 1 分钟 bar
        **120,762,449 行**、尾盘窗口 bar **10,522,869 行**（全市场，带 bar_time）。
[ ] P0-4 至少 4 折、每折标签真实成熟的 walk-forward 结果（models/tail_net_profit_trainer
       .evaluate_selection_quality 判定 passed）
```

依据：改进计划 §5"历史分钟行情不足时，尾盘策略验证明确记为阻塞，继续采集所需数据，
不能用开盘回测代替"。**开盘价回测的结果不得写进任何发布判断。**

---

## 1. 发布前检查（逐条留证据，不允许"应该没问题"）

| 编号 | 检查 | 判据 / 命令 |
| --- | --- | --- |
| R1 | 契约摘要与工件一致 | `rank_final_recommendations` 的 `ModelIdentity.validate()` 返回空串；`contract_digest` 等于 `DEFAULT_TREND_CONTRACT.digest()` |
| R2 | 训练 commit == 运行 commit | 走 `alpha_v2/validation/runtime_identity.resolve_runtime_code_identity()`，禁止 CLI 自己 `git rev-parse`（ADR-001 §7.2） |
| R3 | 模型身份完整 | `artifact_content_hash` / `feature_compute_version` / `label_policy_id` 均非空；缺失即 0 只推荐，不补名额。留档 `model_identity.recording_failures` 必须逐项点名（`training_commit_absent_from_serving_manifest` 等），空清单不算过 |
| R3b | 专属清单存在且工件没被换过 | `tail_model_serving_manifest.json` 存在时，`verify_tail_serving_manifest()` 重新哈希工件并逐字段比对；不过则 `blocking_reason = tail_serving_manifest_unverified` ⇒ 0 只。清单缺失时退回旧在服清单并留名 `tail_serving_manifest_absent` —— **退回可以，静默换模型不行** |
| R3c | 概率来源可指认 | `model_identity.probability_source ∈ {caller_supplied, challenger_artifact, none}`；`none` 时该轮必须 0 只。`challenger_artifact` 时留档分数必须与独立加载同一工件的 `load_tail_model_predictor()` 逐位一致 |
| R3d | 缺特征不填零 | 某只缺工件要求的特征 ⇒ `probability_scoring_failed:<symbol>:feature_missing:<名字>` 可见且不参与排序；不允许用 0 顶替 |
| R4 | 新 label_policy 已登记且未覆盖旧记录 | `python scripts/register_tail_label_policy.py --registry-db <learning_protocol.duckdb> --verify-only` 退出码 **0**（3=未注册或对不上，5=registry 不可用）；旧 `soup_*` / `return_rank` 记录原样存在。影子侧的等价判据是 `model_identity.label_policy_verified == true` |
| R5 | 输出语义已登记 | `output_semantics_for_basis("net_profit_5d_tail") == "event_probability"` |
| R6 | 配置无残留冲突 | `audit_strategy_contract_conflicts(config) == []`（runtime/asof/labels 三块都改指契约后才会空） |
| R7 | 成交口径是 raw | 数据就绪报告 `raw_price_basis_declared == ok` |
| R8 | 成本按日期冻结 | `cost_profile(date).source == "cost_schedule"`，且 `overridden` 集合被写进留档 |
| R9 | 线上/历史判定一致 | 同一 `confirmation` 谓词 + 同一 bar 序列，`evaluate_tail_entry(quote_as_of=…)` 与 `evaluate_tail_entry(quote_as_of=None)` 结果相同（已有测试；发布前在真实数据上抽查一日） |
| R10 | 漏斗留档完整 | 当日 `funnel_trace_*.json` 每层计数自洽（**可判**：`scripts/audit_selection_funnel.py` 读侧跑 `verify_trace()`，计数不闭合 / 契约摘要不是当前契约 / 内容与存储摘要对不上 → 退 5，不拿不可信证据下结论）、`model_identity.identity_recorded=true`、最终推荐每行 `caveats` 不含 `feature_snapshot_missing` |
| R11 | 质量验收门 | `evaluate_selection_quality(...).passed == True`（提升 ≥5pp、分块 CI 下界 >0、平均净收益 >0、尾部 p05 不明显恶化、折数 ≥4） |
| R12 | 影子观察达标 | `scripts/audit_shadow_evidence.py --trace-dir artifacts/runtime/trend_tail_shadow`（`research/shadow_evidence.py`）算出 `shadow_readiness(observed_trade_days≥60, matured_simulated_fills≥100).ready_for_release_review == True`；被阻断的轮次、`_night` 半段留档、自检不过或时间不可证的文件都不计入分子分母，且逐份点名。observed 与 replayed 必须分别跑一次命令，不得合并。影子轮次报告里的 `trend_tail_shadow.shadow_readiness` 给出同一组数字（observed 口径），不必等人去命令行查 |
| R13 | 页面不猜口径 | `page_view(report)` 在 `strategy / entry_window / min_net_profit_probability / contract_digest / reference_notional` 任一缺失时 raise；"尾盘确认"页与旧"推荐汇总"页各自读各自的接口，前端构建（`npm run build`）通过 |
| R14 | 反馈闭环不越权 | `summarize_mature_feedback(...)` 的 `state` 只取 `challenger_suggested / keep_observing / shadow_only`，`promotion` 固定为人工票据字符串；净盈利率分母只含已实现样本，observed 与 replayed 不互借样本量 |
| R15 | 线上/历史同判定 | 同一批分钟 bar + 同一组概率下，`timestamp` 模式与 `timestamp=None + trade_date` 模式的入选集合、漏斗计数、拒绝原因、成交（数量/金额/成交时刻）逐项相等；历史重算的 `trade_date`/`data_as_of` 落在请求的那天，**不是今天**；两者都不给则 `TrendContractError` |
| R16 | 训练载荷新增字段不误伤已冻结工件 | **2026-10-08 已核实为不阻塞**：训练器摘要把 `feature_completeness` 算进去（`tail_net_profit_trainer._digest(payload)`），但下游只把它当**不透明字符串留档**——`tail_model_artifact.py:119` 原样抄成 `training_artifact_digest`，`tail_serving_manifest.py:141-143` 亦然，而工件自检 `_stable_digest(body)` 覆盖的是工件自身 body（:291-294），不重算训练侧那一份。所以新增载荷字段只会让**新训出来的**工件摘要变化，不会让已冻结工件被判成 `artifact_digest_mismatch`。`tests/test_tail_model_artifact.py` + `tests/test_tail_serving_manifest.py`(19) 通过 |

---

## 2. 发布动作（顺序不可颠倒）

1. **只影子，不接管真推荐。** 新路径产物写到独立目录
   （建议 `artifacts/runtime/trend_tail_shadow/`），旧 trend 输出保持原样。
2. 命令顺序（本地/研究区执行，NAS 不跑训练；退出码都是真实退出码）：

   ```bash
   # ① 标签口径落库（写 registry，按 hash 幂等）
   python scripts/register_tail_label_policy.py --registry-db data/learning_protocol.duckdb   # 0
   # ② 样本 → 工件（→ 可选清单）：3=训练按要求停止，5=身份不可证
   python scripts/train_tail_net_profit_model.py --samples <jsonl> --features <a,b> \
       --model-id <id> --out artifacts/research/models/<artifact>.json \
       --manifest-out artifacts/research/tail_model_serving_manifest.json \
       --registry-db data/learning_protocol.duckdb
   # ③ 只读复核：0=绑定成立，3=口径未绑定
   python scripts/register_tail_label_policy.py --registry-db data/learning_protocol.duckdb --verify-only
   ```

   清单状态只会是 `challenger`：晋升是人工动作，这份文件不自带那个权力。
3. 注册为 challenger：沿用 `runtime/services/learning_governance_service.py` 的
   两阶段票据（proposal → 人工 approve → release ticket → execute → confirm），
   **不新增晋升入口**，也不允许训练流程自己切别名（ADR-001 §7.2）。
3. 生产 compose 一律走 `scripts/nas_compose_files.sh` 给出的文件组合；手工拼 `-f`
   会触发 `SA_NAS_PRODUCTION_GUARD`（AGENTS.md §2，两次同型事故）。内存限制只在
   `docker-compose.memlimit.yml` 里，漏掉 = 没有上限。
4. 发布前确认：当前 commit / image、正在跑的 scheduler（`api`、`scheduler-heavy`、
   `scheduler-critical`、`redis`）、备份点、回滚命令已实测一次。
5. 切换后先观察一个完整尾盘周期（14:30–14:50 + 次日成熟），期间不做第二次变更。

## 3. 回滚

| 触发条件 | 动作 |
| --- | --- |
| 尾盘留档缺特征快照 / 模型身份异常但仍在出推荐 | 立即停新路径 job，旧路径不受影响（旧模型与旧路径保留未删） |
| 契约摘要与工件不符（`strategy_contract_digest_mismatch` / `artifact_contract_mismatch`） | 阻断推荐输出（代码已 fail-closed），回滚 alias 到上一 champion |
| `tail_serving_manifest_unverified`（工件哈希对不上 / 状态不是 challenger / registry 查不到该口径） | 把那份清单文件改名或移走（路径可用 `SA_TAIL_SERVING_MANIFEST_PATH`、配置或注入改指，但**设成空值不会关闭**，会落回默认路径）。文件不在 → 链路自动退回"0 只 + 点名原因"；旧路径与旧模型不受影响 |
| `challenger_artifact_not_bound` / `probability_source = none` | 属预期的影子状态（还没在服模型），**不是**故障；不得为了出推荐而塞任何兜底分数 |
| 连续多日 `blocking_reason` 非空 | 检查 `trend_data_readiness`，必要时回到"只观察不推荐" |
| 净盈利率显著低于影子期估计 | 走 `learning_governance_service.rollback()`，保留证据目录不删 |

回滚不得清理 volume、不得改历史 marker、不得覆盖 artifact（AGENTS.md §2.1）。

## 4. 影子验证方案（未来真值，不接受历史数字替代）

- **观察对象**：夜间观察池 → 次日 14:30–14:50 尾盘确认 → 最终推荐 0–3 只 →
  1 万元参考金额成交 → TP+8%/SL−5%、持有 5 日（入场日为第 1 日）。
- **必须分开记录**：`observed_snapshot`（系统当时真实打分）与
  `replayed_recompute`（事后重算）。本项目已实测两者前向结果差一个量级，
  任何合并统计都视为无效。
- **上报指标**：净盈利率（分母只含已实现样本）、平均/中位净收益、成交率、
  确认率、尾部亏损 p05、推荐覆盖率（有推荐的天数占比）、资金占用、
  以及按拒绝原因的分布（`below_threshold` / `cap_exceeded` / `risk_blocked` /
  `not_filled` / `uncertain`）。
- **对照**：同日同风格基线、同一合格池的旧排序、旧完整链路。旧链路若没有成交样本，
  如实报 `baseline_has_no_fill_samples`，**不得伪造命中率**。
- **成熟度**：逐 horizon 报 n；9 月下旬信号的 5/10 日持有期在数据不足时不得计入。
- **上线判据**：见 §1 R11 + R12。证据不足就保持影子状态。
- **当前实测状态**：R11/R12 **未执行**（历史分钟覆盖度未知），逐场景证据见
  `docs/trend_tail_acceptance_evidence.md`。

## 5. 阈值 0.60 的说明

0.60 是**初始选股规则**，用来控制"推荐多少只"，**不代表已证明实际命中率是 60%**。
项目自身锁定 OOS 的绝对命中率区间是 43–45%（`docs/alpha_v2/M4H_Historical_Locked_OOS_Report.md:206,591`）。
若影子期发现 0.60 长期筛不出股票（常见 0 只），那是要重新讨论准入规则的信号，
不是把阈值调低的理由。
