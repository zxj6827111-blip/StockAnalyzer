"""模型输出语义契约（C2）。

模型输出只有三种**互不兼容**的语义，任何消费方都必须先确认自己拿到的是哪一种，
不得把其中一种当作另一种使用：

``event_probability``
    「未来某事件发生」的概率。soup（TP/SL 路径标签）属此类：标签是
    「先触 TP 还是先触 SL」，因此可与 0/1 事件标签直接算 Brier / logloss /
    accuracy，`0.5` 是"事件发生与否"的边界。

``rank_quantile``
    同日**横截面分位归属**的概率（return_rank v3：top 30% → 1、bottom 30% → 0、
    **中间 40% 在训练中被剔除**）。它**不是**全市场上涨概率：
      * `0.5` 是"上尾 vs 下尾"的边界，不是"涨 vs 跌"；
      * 中间段样本在训练里没有标签定义，其分数是插值，不具校准含义；
      * 合法用法是**同日横截面内比较排序**；绝对值不具跨日/跨市场可比性。
    因此：**不得**用 0/1 事件标签（含"收盘价高于开盘价"这类代理标签）对它算
    Brier / logloss / accuracy，也**不得**把 rank 写回原 `probabilities` 字段。

``ranking_score``
    未校准的排序分，只能比大小，不能当概率、不能过概率阈值。

消费方规则（盘查见 ``CONSUMER_RULES``）：事件标签类指标（Brier/logloss/accuracy）
要求 ``event_probability``；阈值/共识类判定（cross review）在 ``rank_quantile``
下含义变为"尾部归属边界"，需显式声明而不是默认按概率解释。
"""

from __future__ import annotations

OUTPUT_SEMANTICS_EVENT_PROBABILITY = "event_probability"
OUTPUT_SEMANTICS_RANK_QUANTILE = "rank_quantile"
OUTPUT_SEMANTICS_RANKING_SCORE = "ranking_score"

# 已登记 label basis → 输出语义。v1/v2 soup 系列是 TP/SL 路径事件；v3 是
# 横截面分位。新增 basis 必须在此显式登记（未知 basis 直接拒绝，不允许
# "沉默地沿用事件概率语义"）。
_BASIS_TO_SEMANTICS: dict[str, str] = {
    "soup": OUTPUT_SEMANTICS_EVENT_PROBABILITY,
    "soup_5d_tp5_before_sl5": OUTPUT_SEMANTICS_EVENT_PROBABILITY,
    "return_rank": OUTPUT_SEMANTICS_RANK_QUANTILE,
    "rank": OUTPUT_SEMANTICS_RANK_QUANTILE,
    "ranking_score": OUTPUT_SEMANTICS_RANKING_SCORE,
}

MIDDLE_DROPPED_NOTE = (
    "return_rank 训练剔除了同日中间 40% 样本，故其输出概率针对的是**两端样本"
    "之间的正类**（上尾 vs 下尾），不自动等于全市场上涨概率；中间段样本的分数"
    "是插值，不得据此判定涨跌。"
)

# 消费方盘查结论（file 相对仓库根）：每项写明"允许怎么用 / 禁止怎么用"。
CONSUMER_RULES: dict[str, dict[str, str]] = {
    "src/stock_analyzer/models/predictor.py": {
        "role": "生产者",
        "allowed": "按工件 label_policy_id 声明语义；predict_rows 返回分数，不在此改名成概率语义",
        "forbidden": "对 rank_quantile 工件把 0.5 当涨跌边界",
    },
    "src/stock_analyzer/pipeline.py": {
        "role": "概率健康观测 + 融合入分",
        "allowed": "记录语义供审计；把模型分量当作 0-1 有界质量分量参与加权排序",
        "forbidden": "把加权总分反解为上涨概率；对 rank_quantile 分量做事件阈值判定",
    },
    "src/stock_analyzer/signal/cross_review.py": {
        "role": "共识/一致性阈值判定",
        "allowed": "阈值仍可用于**分数**一致性检查（同语义比较）",
        "forbidden": "把阈值解释为「上涨概率阈值」；跨语义比较（事件 vs 分位）",
    },
    "src/stock_analyzer/signal/scoring.py": {
        "role": "加权汇总为 0-100 分",
        "allowed": "bounded 分量加权排序",
        "forbidden": "把 total_score/100 当概率；用概率阈值解释等级",
    },
    "src/stock_analyzer/evolution/modules/shadow_online_model_v2.py": {
        "role": "Brier / logloss / accuracy 事件指标",
        "allowed": "仅在标签与模型同为 event_probability 语义时计算",
        "forbidden": "用「收盘价高于开盘价」代理标签去评 rank_quantile 分数（默认拒绝）",
    },
}


def output_semantics_for_basis(basis: object) -> str | None:
    """label basis → 输出语义。

    - 已登记 basis：返回对应语义；
    - 空值：返回 ``None``（调用方自行决定历史兼容行为）；
    - **未登记的非空 basis：抛 ValueError（fail-closed）**——新增标签口径必须
      显式登记语义，避免被下游默默当成事件概率。
    """

    if basis is None:
        return None
    text = str(basis).strip().lower()
    if not text:
        return None
    mapped = _BASIS_TO_SEMANTICS.get(text)
    if mapped is not None:
        return mapped
    if text.startswith("label_policy_v3") or text.startswith("v3"):
        return OUTPUT_SEMANTICS_RANK_QUANTILE
    if text == "label_return_rank":
        return OUTPUT_SEMANTICS_RANK_QUANTILE
    raise ValueError(
        f"unregistered label basis for output semantics: {basis!r}; "
        "register it in output_semantics._BASIS_TO_SEMANTICS before consuming"
    )


def semantics_supports_event_label_metrics(semantics: str | None) -> bool:
    """事件标签类指标（Brier/logloss/accuracy）是否可用于该语义。

    ``None``（未声明）保持历史行为 True，由调用方另计"未声明"计数；
    一旦声明为非事件语义即返回 False（不得用事件标签评它）。
    """

    if semantics is None:
        return True
    return semantics == OUTPUT_SEMANTICS_EVENT_PROBABILITY


def describe_output_semantics(basis: object) -> dict[str, object]:
    """可审计的语义说明（进程内报告/健康字段用）。"""

    semantics = output_semantics_for_basis(basis)
    return {
        "label_basis": None if basis is None else str(basis),
        "output_semantics": semantics,
        "event_label_metrics_allowed": semantics_supports_event_label_metrics(semantics),
        "middle_dropped_note": MIDDLE_DROPPED_NOTE
        if semantics == OUTPUT_SEMANTICS_RANK_QUANTILE
        else "",
    }
