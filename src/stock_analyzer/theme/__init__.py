"""M12 宏观事件主题层（Macro Theme Layer）。

在"个股负面风险规避"新闻线之外新增正向主题线：宏观事件（地缘政治、
气候、政策、供应链）→ 商品/产业链传导 → 板块 → 个股，以「评分加分 +
候选池注入」双通道接入选股漏斗。渐进启用 theme_mode: off/shadow/boost，
shadow 只记账不改结果（Phase 1 MVP），价格确认通过后才允许 boost。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
