# Week5 扫描漏斗整改方案与验收清单

- 日期：2026-08-13
- 分支：`feat/week5-scan-funnel-fix`
- 基线：`a8c2d96`（远程 main，含 PR #12 delta-first + PR #14 增量快照管道）
- 依据：整改检查清单（A/B/C/D 共 14 项）+ 全仓库代码/测试核实
- 状态图例：✅ 已实施+测试｜⚠️ 部分/待补｜❌ 未实施｜⏳ 待实跑验证｜➖ 本轮明确不纳入

---

# 第一部分：整改实施计划（PLAN）

## 一、范围决策（已与用户确认）

| 决策点 | 结论 |
|---|---|
| B 组 Phase 2 并行（检查项 8/9/10） | ➖ 本轮不做，等 NAS 基准通过后再调 `max_workers=4` |
| 检查项 6 的 `post_scan_enrichment` | ✅ 纳入本轮（净新增设计） |
| 检查项 11 的 "fail" 落点 | 仅 flag 分级（`background_data_complete` 核心字段齐全才 True），**不引入消费端剔除** |
| 检查项 11 的 moneyflow 字段 | ➖ 忽略（独立 API，非 background 完整性字段） |
| 提交策略 | 两个 commit：①固化现有 Phase 1 主干 ②A7/A6 剩余/C11/补测试 |

## 二、现状盘点（核实结论）

- **基线关系**：`a8c2d96` = 远程 main 最新（PR #14 合并）；本地 HEAD（aced193）是基线直接父提交，`git log a8c2d96..HEAD` 与 `git diff a8c2d96 HEAD` 均为空。
- **现有整改全部在工作区未提交**：6 个修改文件（config.py / pipeline.py / runtime/service.py / runtime/services/week5_service.py / tests/test_config.py / tests/test_service_portfolio.py）+ 1 个未跟踪新测试文件 `tests/test_week5_scan_funnel_policy.py`（873 行、15 用例）。
- **已完成并通过测试**：检查项 1-5、6 的一半（最慢 5 只 + first-board/anomaly 计时）、13 的一半（config.py + test_config.py）。当前 `test_week5_scan_funnel_policy.py` + `test_config.py` + `test_service_portfolio.py` = 74 passed，`test_service_week5.py` = 71 passed。
- **未做**：检查项 7（light/deep 迭代优化）、6 剩余（阶段细分计时 + completed_count + post_scan_enrichment）、11（background 分级）、13 剩余（default.yaml + quality_gate）、14 补充测试。

## 三、Commit 1：固化现有 Phase 1 主干

> 目的：先把已实现且测试通过的部分提交，避免"工作区未提交"易丢失状态；历史可回滚、可 review。

1. **config/default.yaml**（L310-320 区域，`week5` 段）：补 `scan_progress_path: "artifacts/runtime/week5_scan_progress.json"`（与 `config.py:382-384`、`tests/test_config.py:114-117` 对齐）。
2. **quality_gate.py** `_RUFF_TARGETS`（L55-85，tests 为文件级列举）：加 `"tests/test_week5_scan_funnel_policy.py"`（`_MYPY_BLOCKING_TARGETS` 只查 src，无需动）。
3. **提交**：`git add` 6 个修改文件 + 未跟踪新测试文件。
   - 提交信息：`feat(week5): scan funnel Phase 1——forced 走漏斗/deep fail-closed/三类策略/进度文件/最慢5只`

## 四、Commit 2：A7 + A6 剩余（含 post_scan_enrichment）+ C11 + 补测试

### 检查项 7：light/deep 迭代优化（可行性已核实，零语义风险）

1. **light 改 records**（`runtime/service.py:6080`）：
   - `for _, row in frame.iterrows():` → `for row in frame.to_dict("records"):`
   - 配套 `runtime/service.py:5897` 兜底改为 dict 直达：`data = row if isinstance(row, dict) else pd.Series(row)`（否则每行仍重建 Series，提速打折）。
   - `_prefilter_week5_from_snapshot_row`（L5886-5937）全部字段访问已是 `data.get()`，**函数体零改动**，评分公式/舍入/排序键不变。
2. **deep 位置映射**（`runtime/service.py:6175-6185`）：
   - 循环前预构建 `position_by_index = {idx: i for i, idx in enumerate(rows.index)}`，循环内 `position = position_by_index[index]` 替换 `list(rows.index).index(index)`。
   - 已核实 `predictor.predict_rows`（`models/predictor.py:56-85`）行序严格保序，enumerate/位置映射与 `list.index()` 完全等价，去掉 O(n²)。

### 检查项 6 剩余：阶段细分计时 + completed_count

1. **pipeline.py** `_stage_ms_accum`（L123-131、L173-176）与 `_last_pipeline_stage_ms`（L198-200）新增 5 key，埋点位置（已定位）：
   - `intraday_ms`：包住 L984 `_fetch_intraday_summaries`
   - `market_context_ms`：包住 L988 `_maybe_build_market_index`
   - `cross_review_ms`：包住 L760-767 `evaluate_cross_review`
   - `score_risk_ms`：包住 L768-840（打分 + risk_controller.evaluate）
   - `learning_persist_ms`：包住 L975-984 `_persist_learning_snapshot`
   - **兼容约束**：intraday/market_context 同时继续累计进 `feature_engine_ms`（旧字段语义不破坏，细分是并列增量）。
2. **completed_count**：`_last_pipeline_stage_ms` 加 `completed_count: len(signals)`。
3. **post_scan_enrichment**（净新增，设计如下）：
   - 现状：first-board/anomaly 循环（`week5_service.py:2024-2054`）对每只 symbol 二次 `live_provider.fetch_daily_bars()`；`post_scan_enrichment` 字段全仓库不存在。
   - 设计（最小、向后兼容）：
     1. `PipelineSignal`（`types.py:49-58`）新增可选字段 `post_scan_enrichment: str = ""`（序列化 JSON 字符串，默认空不破坏既有消费方）。
     2. 在 `_finalize_symbol_signal`（pipeline.py，`inputs` 已含 bars）把 analysis_bars 尾部序列化为紧凑 JSON：`[{date, open, high, low, close, turnover}, ...]`，长度 ≥ `first_board_scan_lookback_days`（约 40）以覆盖 streak/turnover/gap 计算。
     3. 用 pipeline 级开关 `self._capture_post_scan_enrichment` 门控，仅在 week5 monster 扫描前置 True，避免污染 portfolio/learning 等常规路径。
     4. `week5_service.py` first-board/anomaly：优先从 `signal_map[symbol]["post_scan_enrichment"]` 反序列化 bars；字段为空/缺列时回退 `live_provider.fetch_daily_bars`（向后兼容）。
   - 约束：实现时确认 `_signal_fetch_lookback_days ≥ first_board_scan_lookback_days`，否则加长拉取或截断保护。

### 检查项 11：background 分级（仅 flag 分级，moneyflow 忽略）

- **核心字段**（以代码库现有 6 字段为准）：core = {block_trade_net, financing_balance, margin_financing_balance, northbound_net, dragon_tiger_flag}；optional = {holder_count}。
- **唯一改动点** `background_adapter.py:94`：`background_data_complete = bool(source_tokens)` → **核心字段全部来源成功才 True**；`background_missing_fields` 区分核心/可选（可选缺失不翻转 complete）。
- `tushare_provider.py:1356` / `vendor_zip_overlay.py:808` 已硬编码 False（缺核心，正确），仅核对 missing_fields 清单口径一致即可。
- **消费端维持现状**（selector L1300-1303、service L5783 的 0.35 降级 + 原因码），**不引入剔除**；可选增加原因码 `background_data_core_missing` 与现有 `background_data_partial` 区分，不改打分/门限。
- `snapshot.py:1676-1678` 透传不变（忠实反映新 flag）。

### 检查项 14：补测试

- light records 迭代 vs iterrows 逐字段等价断言。
- pipeline_stage_ms 新 5 key + completed_count 断言（扩展 `test_week5_scan_final_pipeline_timing_report`）。
- post_scan_enrichment：pipeline 开关开启时字段非空且可反序列化、first-board/anomaly 走 enrich 路径不二次 fetch、字段空时回退 fetch。
- C11 分级：核心齐全→True / 缺任一核心→False+分级 missing / 仅缺 holder_count→True 且 missing 清单标注可选。
- 新测试文件（如新增）入 `_RUFF_TARGETS`。

---

# 第二部分：验收计划

## 五、14 项状态总览

| 检查项 | 内容 | 预期状态 |
|---|---|---|
| 1 | forced_full_deep 改走漏斗 | ✅（已实施，待提交） |
| 2 | deep 未运行/空结果 fail-closed | ✅（已实施，待提交） |
| 3 | 三类策略区分 | ✅（已实施，待提交） |
| 4 | 结构化计数 | ✅（已实施，待提交） |
| 5 | scan_progress_path 进度文件 | ✅（已实施，待提交） |
| 6 | pipeline 阶段计时 | ⚠️→✅（细分计时+completed_count+post_scan_enrichment 本轮补） |
| 7 | light/deep 迭代优化 | ❌→✅（本轮补） |
| 8 | final_pipeline_transform_max_workers | ➖ 不纳入 |
| 9 | pipeline 三步骤 ProcessPool 拆分 | ➖ 不纳入 |
| 10 | OMP/OPENBLAS/MKL/NUMEXPR=1 | ➖ 不纳入 |
| 11 | background_data_complete 分级 | ❌→✅（本轮补，仅 flag 分级） |
| 12 | staleness 配置化 | ✅（基线已配置化，非本轮工作） |
| 13 | 配置同步链路 | ⚠️→✅（default.yaml + quality_gate 本轮补） |
| 14 | 测试覆盖 | ⚠️→✅（A7/C11/post_scan 测试本轮补） |

## 六、逐项验收标准

### Commit 1：固化现有 Phase 1 主干

| # | 验收标准 | 状态 | 证据 | 验收命令 |
|---|---|---|---|---|
| 1 | forced 分支 `prefilter_enabled=True` 且 `funnel_policy="snapshot_funnel"`，profile 名与触发原因保留 | ✅ | `week5_service.py:139-150` | `pytest tests/test_week5_scan_funnel_policy.py::test_week5_offhours_forced_profile_runs_snapshot_funnel` |
| 2 | deep `deep_stage_ran=false` 或 deep 空时清空 raw 候选（仅 pinned 追加），不回退 quality 300/monster_cap 120 | ✅ | `week5_service.py:1591-1616` | `test_week5_scan_deep_empty_fail_closed_pinned_only` / `_no_pinned_empty_report` |
| 3 | 三类策略：snapshot_funnel / intentional_full_deep / direct_non_universe，Friday/weekend 保留 `prefilter_enabled=False` | ✅ | `week5_service.py:121-138, 1478-1482` | `test_week5_scan_friday_intentional_full_deep_keeps_raw_candidates` / `test_week5_scan_manual_symbols_direct_non_universe` |
| 4 | funnel.policy/deep_stage_ran/deep_symbols_empty/deep_empty_reason/deep_selected_count/pinned_added_count/pipeline_input_count + selection_source 四值 | ✅ | `week5_service.py:1737-1761` | `test_week5_scan_normal_snapshot_funnel_input_within_deep_target` |
| 5 | 进度文件字段全集 + 原子写 + completed/failed 终态 + final pipeline 单股心跳 | ✅ | `week5_service.py:3316-3403` | `test_week5_scan_progress_file_phases_heartbeat_and_completion` / `_failed_terminal_state` |
| 13 | `config/default.yaml` 补 `scan_progress_path`；quality_gate `_RUFF_TARGETS` 加新测试文件 | ✅ | `config/default.yaml`（L310-320 区域）、`quality_gate.py:55-85` | `pytest tests/test_config.py::test_load_default_config_values` + `python -m ruff check tests/test_week5_scan_funnel_policy.py` |

**Commit 1 提交前整体验证**：

```bash
python -m pytest tests/test_week5_scan_funnel_policy.py tests/test_config.py tests/test_service_portfolio.py tests/test_service_week5.py
```

### Commit 2：A7 + A6 剩余（含 post_scan_enrichment）+ C11 + 补测试

| # | 验收标准 | 状态 | 证据位置 | 验收命令 |
|---|---|---|---|---|
| 7a | light 用 `frame.to_dict("records")` 顺序稳定迭代，`_prefilter_week5_from_snapshot_row` 评分/舍入/排序键不变 | ⏳ | `service.py:6080, 5897` | 逐字段等价测试（新增） |
| 7b | deep 预构建位置映射替换 `list(rows.index).index(index)`，输出与旧实现逐字段一致 | ⏳ | `service.py:6175-6185` | 逐字段等价测试（新增） |
| 6a | pipeline_stage_ms 新增 5 key：intraday/market_context/cross_review/score_risk/learning_persist，且 intraday/market_context 继续累计进 feature_engine_ms | ⏳ | `pipeline.py:123-131, 198-200` | `test_week5_scan_final_pipeline_timing_report`（扩展） |
| 6b | `_last_pipeline_stage_ms` 加 `completed_count`（== len(signals)） | ⏳ | `pipeline.py:198-200` | 同上 |
| 6c | `post_scan_enrichment`：PipelineSignal 新增可选字段（默认空串），pipeline 开关门控；first-board/anomaly 优先读该字段，空时回退 fetch_daily_bars | ⏳ | `types.py:49-58`、`pipeline.py` `_finalize_symbol_signal`、`week5_service.py:2024-2054` | 新增测试：开关开启字段非空可反序列化 / 走 enrich 不二次 fetch / 空时回退 |
| 11 | `background_data_complete` 核心字段齐全才 True；可选字段（holder_count）缺失不翻转；missing 清单区分核心/可选；消费端维持 0.35 降级不剔除 | ⏳ | `background_adapter.py:94`、`tushare_provider.py:1356`、`vendor_zip_overlay.py:808` | 新增分级测试：核心齐全 True / 缺任一核心 False+分级 missing / 仅缺可选 True |

**Commit 2 提交前整体验证**：

```bash
python -m pytest tests/test_week5_scan_funnel_policy.py tests/test_service_week5.py tests/test_config.py tests/test_service_portfolio.py
python -m ruff check src/stock_analyzer/pipeline.py src/stock_analyzer/runtime/service.py src/stock_analyzer/runtime/services/week5_service.py src/stock_analyzer/data/background_adapter.py src/stock_analyzer/types.py
```

## 七、预期效果分层

### 代码/测试可验证（本轮直接达成）

1. **正确性固化**：light/deep 评分公式、舍入、排序键零改动，测试锁死"改性能不改行为"。
2. **可观测性补齐**：报告新增 5 子阶段耗时 + `completed_count` + 最慢 5 只明细 → 下次 NAS 慢可直接下钻到子阶段，支撑 Phase 2 并行拆步决策。
3. **first-board/anomaly 去二次 I/O**：复用 pipeline 已拉的 bars，减少 N 次 `fetch_daily_bars`，数据一致性更好。
4. **background 语义精确化**：可选字段缺失不再误判"数据不完整"；候选集不变（消费端未引入剔除）。
5. **门禁闭合 + 历史可追溯**：default.yaml/quality_gate 补齐，两个 commit 可回滚、可 review。

### 需 NAS 实证（不提前下结论）

| 项 | 预期方向 | 不确定性 |
|---|---|---|
| light records 迭代 | 去掉 pandas 每行装箱开销 | 300 行量级绝对时间体感有限，取决于 universe 行数 |
| first-board 去二次拉取 | 减少 N 次 provider 调用 | 幅度取决于 provider 延迟与 N |
| deep position map | O(n²)→O(n) | deep 候选通常几百只，收益可能很小 |

> 关键认知：检查项 7 在 300 行量级下主要价值是**正确性固化和代码整洁**，不是可量化的性能数字；真正大头性能在 Phase 2 并行，本轮未纳入。

## 八、边界与未做项（明确）

1. **B 组 Phase 2 并行（8/9/10）未做**：final pipeline 仍串行，`max_workers` 默认 1。
2. **C11 的 fail 语义未做**：消费端仍是"降级 0.35 + 原因码"，不剔除候选。
3. **moneyflow 字段未接入**：独立 API，与 background 完整性无关。
4. **NAS 20/50 smoke + 300 SLO 校准仍未做**：快照改造遗留验证缺口，本轮整改后仍需 NAS 跑一轮才谈"生产就绪"。

## 九、验收完成判定

- [ ] Commit 1 提交且工作区无 Phase 1 主干残留（`git status` 干净）
- [ ] Commit 2 提交，全部新增测试通过（预期 ≥145 passed）
- [ ] ruff 对涉及文件无新增告警
- [ ] `git diff a8c2d96..HEAD --stat` 覆盖 14 项中本轮纳入的全部文件
- [ ] 检查项 6/7/11/13/14 的 ⏳ 全部转为 ✅（见第六节 Commit 2 表）
- [ ] （可选，另行安排）NAS 20/50 smoke + 300 SLO 校准，作为 Phase 2 并行前置依据
