"""Alpha V2 施工包（蓝图 §5 P0-00 起步）。

当前只包含 P0-00 要求的两件基础设施：

- :mod:`stock_analyzer.alpha_v2.artifacts`：``artifacts/alpha_v2`` 目录语义；
- :mod:`stock_analyzer.alpha_v2.baseline`：基线身份清单（baseline manifest）。

设计约束（P0-00 零行为变化）：本包不被 Legacy 运行路径 import，所有写入都由
显式调用（脚本 / 测试 / 后续 V2 服务）触发，因此 ``alpha_v2.enabled=false`` 时
不产生任何副作用是结构性成立，而不是靠运行期开关判断。
"""

from __future__ import annotations

from stock_analyzer.alpha_v2.artifacts import (
    ALPHA_V2_SUBDIRECTORIES,
    AlphaV2ArtifactLayout,
)
from stock_analyzer.alpha_v2.baseline import (
    BASELINE_MANIFEST_SCHEMA,
    behavior_surface_snapshot,
    build_baseline_manifest,
    write_baseline_manifest,
)

__all__ = [
    "ALPHA_V2_SUBDIRECTORIES",
    "BASELINE_MANIFEST_SCHEMA",
    "AlphaV2ArtifactLayout",
    "behavior_surface_snapshot",
    "build_baseline_manifest",
    "write_baseline_manifest",
]
