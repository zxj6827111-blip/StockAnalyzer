"""主题知识库（theme taxonomy）YAML 加载与校验。

种子知识库是主题线质量的上限：``config/theme_taxonomy.yaml`` 定义
主题族（theme_id / 事件关键词 / 传导方向 / 价格确认对象 / 板块列表），
shadow 期按 ledger 有效性数据迭代。pydantic 校验保证结构合法，任何
未知字段直接报错（extra="forbid"），避免 YAML 手误被静默吞掉。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from stock_analyzer._pydantic_compat import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ThemeDefinition(_StrictModel):
    """单个主题族定义。

    ``event_type`` 为主题族事件分类（geopolitics/climate/policy/supply_chain），
    ``direction`` 为事件对商品的预期传导方向（+1 看多 / -1 看空），
    ``commodities`` 为价格确认对象（经 :mod:`price_confirmation` 内置注册表
    解析为期货主力连续合约），``boards`` 为 akshare 概念/行业板块名列表，
    ``symbols`` 为可选静态成分股覆盖（板块接口不可用时的兜底，通常为空）。
    """

    theme_id: str
    event_type: str
    event_keywords: list[str]
    direction: int = 1
    commodities: list[str] = Field(default_factory=list)
    boards: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    description: str = ""


class ThemeConfirmation(_StrictModel):
    """价格确认阈值（与 config.MacroThemeConfig.confirmation 镜像）。"""

    price_move_1d_min: float = 0.015
    price_move_3d_min: float = 0.03
    lookback_days: int = 3


class ThemeTaxonomy(_StrictModel):
    """整个主题知识库：主题族列表 + 全局确认阈值。"""

    themes: list[ThemeDefinition]
    confirmation: ThemeConfirmation = Field(default_factory=ThemeConfirmation)

    def by_id(self, theme_id: str) -> ThemeDefinition | None:
        for theme in self.themes:
            if theme.theme_id == theme_id:
                return theme
        return None

    def validate_references(self) -> list[str]:
        """校验主题内部引用一致性，返回问题描述列表（空=合法）。

        检查：theme_id 唯一；关键词非空；direction 只能是 ±1；commodities
        非空（价格确认对象是命门，缺失意味着该族永远无法激活）。
        """
        problems: list[str] = []
        seen: set[str] = set()
        for theme in self.themes:
            if theme.theme_id in seen:
                problems.append(f"duplicate theme_id: {theme.theme_id}")
            seen.add(theme.theme_id)
            if not theme.theme_id.strip():
                problems.append("empty theme_id")
            if not theme.event_keywords:
                problems.append(f"{theme.theme_id}: empty event_keywords")
            if theme.direction not in (-1, 1):
                problems.append(f"{theme.theme_id}: direction must be +1/-1")
            if not theme.commodities:
                problems.append(
                    f"{theme.theme_id}: empty commodities (price confirmation impossible)"
                )
            if not theme.boards and not theme.symbols:
                problems.append(f"{theme.theme_id}: empty boards and symbols (no stock mapping)")
        return problems


def load_taxonomy(path: str | Path) -> ThemeTaxonomy:
    """加载并校验主题知识库 YAML。

    未知字段抛 ``ValidationError``（pydantic extra=forbid），主题族引用
    问题由 :meth:`ThemeTaxonomy.validate_references` 在调用方显式检查。
    """
    taxonomy_path = Path(path)
    with taxonomy_path.open("r", encoding="utf-8") as fp:
        raw_data: Any = yaml.safe_load(fp) or {}
    if not isinstance(raw_data, dict):
        raise ValueError(f"theme taxonomy must be a mapping: {taxonomy_path}")
    taxonomy = ThemeTaxonomy.model_validate(raw_data)
    problems = taxonomy.validate_references()
    if problems:
        raise ValueError(
            f"theme taxonomy validation failed for {taxonomy_path}:\n- " + "\n- ".join(problems)
        )
    return taxonomy
