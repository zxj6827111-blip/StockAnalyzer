# ADR-004 资格层证据的完整性：词汇表闭合、归因顺序、以及"没有判别力的硬门"不许发通过票

Status: Draft

As-of: 2026-10-08 @ HEAD `ce19b76`

本 ADR 管的是**资格层留档的可信度**，不是选股规则本身。它回答三个问题：

1. 一条淘汰原因要满足什么条件才能写进 `hard_eligibility`；
2. 一只票同一天踩中多条硬门时，"第一条原因"由谁决定；
3. 一条硬门当天的输入列**没有判别力**（被填成常数）时，留档该怎么写。

三条都由实测缺陷倒逼出来，不是设计偏好。缺陷本体见
`docs/trend_tail_selection_quality_report.md` §3b.2 / §3g / §3g.1 / §3g.2 / §3h / §3i，
根因清单见 `NOTE-002` 的 D15。

---

## 1. Status 为什么是 Draft

读侧三条决定都已落地并有测试与生产产物背书；**写侧还有一处没决定**，
线上侧也还剩三个未登记的原因名：

```text
已定并已验：占位常量的带版本解释规则（§3）、归因顺序是契约事实（§4）、
            无判别力的门不落这两层（§5）——三层都有专属测试，
            且 2026-08 干净窗口的 19 份真实留档全部通过 verify_trace()。
未定：      ingest 该不该停止写兜底常量、改写成 NULL（§6.1，跨模块数据语义变更）。
已闭合：    线上硬门的 20 个原因名已整体登记进 `_RULE_KIND`（`LIVE_HARD_GATE_NAMES`，全为 HARD）。
```

§6.1 落地之后本 ADR 升 Accepted（§6.2 的原因名已于 2026-10-08 整体登记）。

---

## 2. 决策一览

| # | 决策 | 反面（明确不做） |
| --- | --- | --- |
| 1 | 能写进 `hard_eligibility` 留档的原因名**必须**在 `_RULE_KIND` 里登记为 `HARD`；生产者一旦吐出未登记或非 HARD 的名字就直接 `SystemExit` | 不做"未知名字当硬门"的宽容：`classify_rule()` 对未知名字按 `predictive` 处理，而 §2 的消融实验正是按 `classify_rule()` 分组的——一条没登记的硬门会**从消融视野里消失**，比少用一条规则严重得多 |
| 2 | 多门命中时"第一条原因"的顺序是契约常量 `HARD_GATE_ATTRIBUTION_ORDER`，并被写进每条留档的 notes | 不依赖 dict 插入顺序：那样逐原因计数会随代码行序悄悄改数 |
| 3 | 一条硬门当天的输入列没有判别力（众数占比 > 0.5，或列全空）⇒ 这天的 `universe`/`hard_eligibility` **不落档**；列里被填进数据供应商兜底常量的那些**行**单独记 `unproven_float_market_cap` 出局 | 不把"门跑完了没淘汰任何票"读成"这只票过了这条门"；也不为了让留档有数字而把阈值改成绝对值（口径就变了） |
| 4 | 老记录**不重写**，按 `FLOAT_CAP_INTERPRETATION_VERSION` 这条带版本的解释规则读；解释版本本身进重放报告的 `thresholds` | 不就地 `UPDATE` 历史 `float_market_cap`——那会让既有留档与产物对不上。真值现在有独立来源了（研究库 `float_market_cap_ref`，2026-10-08 补采，见质量报告 §3j），但这条决定不变：真值作为另一张表由消费方显式加入并声明用了哪版口径，占位行留在原处当证据 |

---

## 3. 占位常量：为什么 1.2e10 是"没测过"的指纹

`_DEFAULT_FLOAT_MARKET_CAP = 12_000_000_000.0` 在三个 provider 里是同一个字面值：

```text
src/stock_analyzer/data/tushare_provider.py:23
src/stock_analyzer/data/akshare_provider.py:20
src/stock_analyzer/data/efinance_provider.py:18
```

tushare 那条写入路径（`tushare_provider.py:1677-1684`）在 `daily_basic` 给得出
`circ_mv` 时按 `× 10000`（万元→元）换算，给不出时 `fillna(_DEFAULT_FLOAT_MARKET_CAP)`；
而该调用外面包着 `except Exception: basic = pd.DataFrame()`（`:708-719`）——
**接口失败被静默吞掉**，退化成"这一列没有 circ_mv"，于是整列写兜底常量。

后果不是"数值不准"而是**门失效**：硬门阈值按同一列取横截面分位，列内 99.7% 都是这个
常量时分位数就等于常量本身，`value < threshold` 对任何行恒假 ⇒ 这条门那天对全市场
一个都不淘汰，却照样"运行完成"。库里实测（逐月等于该常量的行数占比）：
2026-04 99.7%、2026-05 99.98%、**2026-06 全月只剩这一个取值**、2026-03/07 各约五成；
2022-05 起每月 15~46 行、2025-09~2026-02 升到每月 2,511~5,608 行。

所以规则是：

- 这一列等于该字面值 ⇒ 该行市值**从未被测量**，不参与市值比较，单独记
  `unproven_float_market_cap`（已登记为 HARD）出局；
- 分位阈值只在测过的值上推导；测过的集合为空 ⇒ 该天整条门无从判定，
  显式记 `float_cap_gate_evaluable=False`；
- 误差方向是刻意选边的：真有某只票市值恰好等于 12,000,000,000.0 元时它也会被当未知，
  代价是少一个可判定样本；反过来则是把没测过的东西当成有效信息。
- `NaN` 不算占位（`unproven_float_market_cap_mask` 对 NaN 返回 False）——
  那是本来就缺，由读取该列的门的既有语义处理。这里只抓"被填过"的那种，
  因为它**看起来像数据**。

---

## 4. 归因顺序为什么必须是契约事实

`StageTrace` 的计数恒等式 `inputs == advanced + Σ rejected` 不许一只票进两个桶，
所以多门命中时只能记一条原因。它此前取决于 `daily_gates` 返回 dict 的插入顺序。

实测后果（94 个重放决策日）：`min_float_market_cap` 一共命中 **8,527** 次，
但排在前面的 `min_avg_turnover_20` 把绝大多数分走了，逐原因归因表里只剩 **132** 次。
两种读法都对，不写出顺序就没人知道差值是构造出来的——而 132 这个数字一度被读成
"这条门几乎不干活"，实际是它整段污染窗口根本没有判别力（见 §3）。

`HARD_GATE_ATTRIBUTION_ORDER` 现在是唯一的判定顺序，生产者在归因前按它排序，
并把顺序本身写进每条留档的 notes。把两条门的书写顺序对调，归因结果不许变
（`test_attribution_follows_contract_order_not_dict_insertion`）。

---

## 5. "没有判别力的门"这天不落档

`degenerate_gate_inputs()`（重放侧）与 `column_concentration_<col>`（就绪审计侧）
是同一件事的两个高度：前者管"这天的这两层能不能落档"，后者管"这份库能不能当训练输入"。

不落档是**有意的信息损失**，写在 `build_universe_stage_traces` 的返回值里：
宁可这两层缺席，也不落一条声称"门判过了"的留档。代价要一起记住——
2026-04~06 因此完全没有前两层留档，那段时间的"合格池"在读侧不存在。

真实产物证据：2026-08-03~08-29 的 19 个决策日 `days_with_non_evaluable_gate_inputs=[]`、
19/19 通过 `verify_trace()`，考虑 98,184 / 过完所有硬门 66,509（67.7%）、
市值门阈值 2,020,343,250；同一脚本在污染窗口给出 310,142 输入中
216,862（69.9%）"市值从未测过"、60 天里 40 天整日无从判定、晋级只剩 212。

---

## 6. 还没决定的两件事

### 6.1 写侧：ingest 应不该继续写兜底常量

方向明确（失败应当可见、缺值应当是 NULL），**没做的原因不是懒**：
`src/` 下有 26 个文件出现 `float_market_cap`（生产夜扫、特征快照、PIT 数据集、
Alpha V2 面板都在读它），把兜底常量换成 NULL 是一次跨模块数据语义变更，
按 AGENTS.md §12 要单独立项。真要推，需要先回答：读 NULL 时这 26 个消费方各自
是拒答（fail-closed）还是照常跑？现在还没有那份影响清单。

`daily_bars` 也没有能区分"测过 / 被填的"字段，所以补采之外还需要一个来源标记列——
这也是同一份变更的范围。

### 6.2 线上侧还有三个契约不认识的原因名

**2026-10-08 更新（已闭合，用户授权把这两层接进生产夜扫）**：`_RULE_KIND` 现在整体登记
线上选择器能吐出的 20 个原因名（`LIVE_HARD_GATE_NAMES`，全部 `HARD`），判定顺序写死成
`LIVE_HARD_GATE_ATTRIBUTION_ORDER`，每条门读哪些列写进 `LIVE_GATE_INPUT_COLUMNS`。
线上名与研究侧重放名是**同一批门的两套词汇**（线上 `low_float_market_cap` ↔ 契约
`min_float_market_cap`），这一点写在契约注释里而不是靠改名抹平：改名会动生产报告的既有字段，
而两套名字都登记进分类表后，消融按 `classify_rule()` 分组就不可能再把硬门当成可拆的预测规则。
`known_suspended` 的语义冲突不在这次闭合范围里：线上 `suspended` 读的是显式声明字段，
与研究侧那个"由缺 bar 推断"的名字不是一回事（质量报告 §3d）。


`future_listed`、`insufficient_history_window_bars`、`known_suspended` 实测都被
`classify_rule()` 判为 `predictive`，却写进 `hard_eligibility`。给
`build_universe_stage_traces` 加"全闭合词汇表"守卫会**直接打死这两层**，
所以本 ADR 只把 §2 里那条窄守卫（生产者侧 `_universe_fact` 对非 HARD 名字 `SystemExit`）
落地，剩下三个的定性要契约作者点头：

- 前两个按语义应是 HARD（未来上市 / 窗口内历史不足都是数据完整性，不是预测）；
- `known_suspended` 的语义**本身待定**——它的定义是"eligible 但最近窗口内没有任何 bar"，
  而计划 §3.1 明令"不把缺 bar 当停牌"，所以把它登记成 HARD 停牌门会与该条冲突。
  现在保持名字与快照一致、由 notes 说明它不等于证明停牌。

---

## 7. Implementation Locations

词汇表与归因顺序（契约）：

```text
src/stock_analyzer/feature/trend_candidate_contract.py
  _RULE_KIND（含 "unproven_float_market_cap": HARD、"insufficient_history_at_asof": HARD）
  classify_rule()（:167，未知 ⇒ predictive）
  HARD_GATE_ATTRIBUTION_ORDER（:123）
  UNPROVEN_FLOAT_MARKET_CAP（:153）/ FLOAT_CAP_INTERPRETATION_VERSION
  unproven_float_market_cap_mask()（:157）
  assert_training_features()（:369）
```

生产者与消费点：

```text
scripts/replay_tail_candidate_pool.py
  GATE_INPUT_COLUMNS（:319）/ MAX_MODAL_VALUE_SHARE = 0.50
  RULE_INPUT_COLUMNS / gate_input_columns()（§2 要这一层记"读了哪些列"，
  清单由当天真的跑过的规则映射出来，未知规则贡献 0 列）
  degenerate_gate_inputs()（:324）/ _universe_fact()（:339，非 HARD 名字 ⇒ SystemExit）
  daily_gates()（:394，占位行不进市值比较 + unproven_float_market_cap 单列）
  阈值推导只吃测过的值；report.thresholds.float_cap_interpretation
  symbols_file_facts()（:283，第一层输入的路径/字节 sha256/数量/生产者）
scripts/measure_marketwide_gate_coverage.py
  day_funnel()（全市场同一套 HARD 规则 + 契约归因顺序 + float_cap_gate_evaluable）
src/stock_analyzer/research/night_scan_funnel_trace.py
  _universe_facts()（:83）/ build_universe_stage_traces()（:119，
  non_evaluable_gates 非空 ⇒ 返回空元组不落这两层）
src/stock_analyzer/research/trend_data_readiness.py
  MAX_MODAL_VALUE_SHARE（:58）/ CONCENTRATION_GUARDED_COLUMNS（:59）
  _audit_column_concentration()（:114）
src/stock_analyzer/data/tushare_provider.py
  :23 兜底常量 / :708-719 被吞掉的 daily_basic / :1677-1684 fillna 写入点
scripts/build_tail_universe_symbols.py（全市场清单 + provenance JSON 生产者）
```

测试（各自钉住一条不变量）：

```text
tests/test_trend_candidate_contract.py(17)
tests/test_replay_tail_intraday_features.py(10)
tests/test_measure_marketwide_gate_coverage.py(5)
tests/test_night_scan_funnel_trace.py(9)
tests/test_trend_data_readiness.py
tests/test_record_replay_funnel_trace.py(3)
```

证据产物与文档：

```text
docs/trend_tail_selection_quality_report.md §3b.2 / §3g / §3g.1 / §3g.2 / §3h / §3i
docs/selection_quality_plan_status.md（§3.1 读侧解释规则那一行 + 缺口清单）
artifacts/research/funnel_traces_marketwide_aug/（19 份，读侧解释规则生效后真实产出）
```

---

## 8. 改这里之前必须知道的

1. **不要为了多落几天档而放松 §5**。把 `MAX_MODAL_VALUE_SHARE` 调高或直接删掉
   `degenerate_gate_inputs` 的拒绝分支，会让污染窗口重新产出留档，而那份留档在说谎。
2. **不要在 `dropped()` 里给 NaN 兜底成"通过"**。`min_float_market_cap` 的比较对 NaN
   恒假，这就是本缺陷的机制本身。
3. **新增硬门原因名必须同时进 `_RULE_KIND` 和 `HARD_GATE_ATTRIBUTION_ORDER`**；
   只进前者会让归因落到"未声明顺序的名字"尾巴上（排序键会变，数会跟着变），
   只进后者会被 `test_attribution_order_is_a_declared_hard_gate_vocabulary` 判红。
4. **解释规则是版本化的**：改 `UNPROVEN_FLOAT_MARKET_CAP` 的判定或换阈值口径时
   要升 `FLOAT_CAP_INTERPRETATION_VERSION`，否则同一份库换解释得到的合格池对不上，
   事后无法复现当时是哪一版。
5. 本节所有"已通过"措辞都只描述**资格判定**，不代表 §4 的选股质量验收达标
   （四组特征时间外 AUC 仍 < 0.5，影子验证仍 0 天 / 0 笔）。
