# 学习链整改 C2：输出语义与下游契约（2026-09-14）

> 材料性质：**契约定义 + 消费方盘查记录**；代码改动见同批提交。
> 权威口径：`src/stock_analyzer/models/output_semantics.py`（唯一登记点）。

## §1 三种输出语义（不得混用）

| 语义 | 含义 | 可做 | 禁止 |
| --- | --- | --- | --- |
| `event_probability` | 「未来某事件发生」的概率（soup 的 TP/SL 路径标签属此类） | 与 0/1 事件标签直接算 Brier / logloss / accuracy；`0.5` = 事件发生与否 | 当作"涨跌"以外的事件解释 |
| `rank_quantile` | 同日**横截面分位归属**概率（return_rank v3：top 30% → 1、bottom 30% → 0） | 同日横截面内排序比较；尾部归属判定 | 当全市场上涨概率；用 0/1 事件标签（含价格代理）算 Brier/logloss；把 rank 写回原概率字段 |
| `ranking_score` | 未校准排序分 | 比大小 | 过概率阈值、当概率解释 |

**中间段剔除的含义（关键）**：return_rank 训练剔除了同日中间 40% 样本，因此它的
输出概率针对的是**两端样本之间的正类**（上尾 vs 下尾），**不自动等于全市场上涨
概率**；中间段样本在训练里没有标签定义，其分数是插值，不得据此判定涨跌。
`0.5` 是"上尾 vs 下尾"的边界，不是"涨 vs 跌"。

## §2 消费方盘查（原 §1.5 清单，逐项落地）

| 位置 | 角色 | 现状与处置 |
| --- | --- | --- |
| `models/predictor.py` | 生产者 | 新增 `label_policy_id` 字段与 `output_semantics` / `output_semantics_report()`：语义由工件 label 契约派生并在 docstring 声明；**未登记契约在推理路径 fail-soft**（返回 None + 错误串留痕），**审计路径 fail-closed**（`describe_output_semantics` 抛错） |
| `pipeline.py`（概率健康观测，原 1526 行附近） | 观测 | 健康快照新增 `output_semantics` 字段（含 label_policy_id / 允许事件指标 / 未登记原因），纯增量留痕，不改判定 |
| `pipeline.py`（cross review 调用，原 1557 行附近） | 共识判定 | 传入 `output_semantics=`，判定结果以 reason 形式留痕；阈值含义随语义可审计 |
| `signal/cross_review.py` | 阈值/一致性 | docstring 明确：`p_*_min` 是对**分数**的阈值——事件语义读作"事件概率下限"，分位语义读作"尾部归属边界"；同调用内三个分数必须同语义 |
| `signal/scoring.py` | 加权汇总 | docstring 明确：`total_score` 是**排序装置**，`total_score/100` 不是概率，等级阈值不得解释为概率阈值 |
| `evolution/modules/shadow_online_model_v2.py` | Brier/logloss/accuracy | 新增语义门：声明非事件语义（或未登记口径）时**拒绝价格代理标签**（`close >= open`），排除并计入 `excluded_label_semantics_mismatch`；计数在成熟度过滤**之前**从原始记录统计，避免"整批被排除"被误读为"没有成熟样本" |

## §3 顺序遵从（方案要求）

方案要求「先修 A2 的实现缺陷 → 再做 raw/rank 与低参数校准的对照 → 最后才决定去留」：
- A2（校准器边界）已在批次 A 完成（PR #48）；
- 本项只做**语义契约与消费方改造**，未做"去校准"决策，也未改动任何阈值数值——
  阈值去留属 C3 的配对比较议题。

## §4 验收（可复现）

- `tests/test_output_semantics_contract.py` 13 项：basis→语义三态映射（已知/空/未登记
  抛错）、中间段说明只在 `rank_quantile` 出现、生产者声明、cross review 留痕、
  shadow 拒绝代理标签与计数留痕（含端到端 reasons 断言）。
- 未登记 basis 的行为双向验证：审计路径 `pytest.raises(ValueError)`，推理路径
  `output_semantics is None` + 错误串非空。
- 既有 shadow 套件保持通过（价格代理在**事件语义**下行为不变）。
