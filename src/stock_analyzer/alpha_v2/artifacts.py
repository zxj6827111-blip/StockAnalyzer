"""Alpha V2 artifact 目录语义（蓝图 §15）。

``artifacts/alpha_v2/`` 下的五个子目录各有唯一消费者，P0-00 只落地**目录语义
与 root 解析**，不实现后续 writer：

- ``audit``     基线清单 / 任务验证记录（P0-00 立即使用）
- ``decisions`` 每日决策日志（P0-10）
- ``outcomes``  outcome 成熟记录（P0-10）
- ``manifests`` 每次运行的身份清单（P0-10 / P2）
- ``reports``   影子对照报告（P2）

目录创建一律由显式调用 :meth:`AlphaV2ArtifactLayout.ensure` 触发（CLI 或测试），
绝不在 Legacy 运行路径里隐式发生；``mkdir(exist_ok=True)`` 保证幂等，重复调用
不改变任何既有文件。写入本身用"临时文件 + ``os.replace``"的原子写法，与仓库其它
artifact 写入（``nightly_report_service._write_json_atomic``）保持同一约定。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from stock_analyzer.config import AlphaV2Config

# 蓝图 §15 规定的最小目录集合；顺序即创建顺序（父目录先建）。
ALPHA_V2_SUBDIRECTORIES: tuple[str, ...] = (
    "audit",
    "decisions",
    "outcomes",
    "manifests",
    "reports",
)


def default_project_root() -> Path:
    """仓库根目录（与 config.py / acceptance_artifacts.py 同一约定）。"""
    return Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class AlphaV2ArtifactLayout:
    """Alpha V2 artifact 根目录及其子目录的解析结果。"""

    configured_root: str
    root: Path
    audit: Path
    decisions: Path
    outcomes: Path
    manifests: Path
    reports: Path

    @classmethod
    def from_config(
        cls,
        config: AlphaV2Config | None = None,
        *,
        project_root: str | Path | None = None,
    ) -> AlphaV2ArtifactLayout:
        """按配置解析布局；相对路径锚定到 ``project_root``（默认仓库根）。"""
        alpha_v2 = config if config is not None else AlphaV2Config()
        configured = str(alpha_v2.artifact_root).strip()
        root = Path(configured)
        if not root.is_absolute():
            base = Path(project_root) if project_root is not None else default_project_root()
            root = base / root
        return cls(
            configured_root=configured,
            root=root,
            audit=root / "audit",
            decisions=root / "decisions",
            outcomes=root / "outcomes",
            manifests=root / "manifests",
            reports=root / "reports",
        )

    def subdirectory_paths(self) -> dict[str, Path]:
        return {name: self.root / name for name in ALPHA_V2_SUBDIRECTORIES}

    def ensure(self) -> AlphaV2ArtifactLayout:
        """幂等创建 root 与全部子目录，返回自身便于链式调用。"""
        for path in self.subdirectory_paths().values():
            path.mkdir(parents=True, exist_ok=True)
        return self

    def as_payload(self) -> dict[str, object]:
        """供 manifest / 审计记录落盘的可序列化描述。"""
        return {
            "configured_root": self.configured_root,
            "resolved_root": str(self.root),
            "subdirectories": {
                name: str(path) for name, path in self.subdirectory_paths().items()
            },
        }


def write_json_atomic(path: Path, payload: Mapping[str, object]) -> Path:
    """原子写入 JSON（临时文件 + fsync + os.replace；失败时清理临时文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        # 审计目录不允许留下半截临时文件（后续任务按文件枚举审计产物）。
        temp.unlink(missing_ok=True)
        raise
    return path
