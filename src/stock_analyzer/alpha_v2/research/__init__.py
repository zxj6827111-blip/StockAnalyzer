"""Alpha V2 研究包（M2：S11–S23）。

**为什么单独建包**（蓝图 §15 建议放 ``src/stock_analyzer/research/``）：

1. ``alpha_v2`` 是 V2 的显式边界（S00 起），Legacy/evolution 研究模块与 V2
   研究混在同一目录会让"哪些代码属于 V2"不可辨认，也让 S00 的架构守卫失去意义；
2. ``research/`` 下已有 16 个 legacy/evolution sidecar；若在其下再放
   ``walk_forward.py``，会与 ``backtest/walk_forward.py`` /
   ``backtest/walk_forward_xsec.py`` 同名混淆；
3. 放在 ``alpha_v2/**`` 内可复用 ``alpha_v2.artifacts`` 的目录/原子写语义。

**本包纪律**（M2 全程）：

- 只读研究：本包不写生产决策、不改 Legacy 阈值、不 promote 模型；
- 一切 outcome 必须来自**可执行入场 + raw 成交价 + 成本后净收益**；
- 无法证明的数据一律标 ``not_available`` / ``unverified``，不编造。
"""

from __future__ import annotations

__all__: list[str] = []
