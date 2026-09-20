"""生成 Alpha V2 基线身份清单（蓝图 §5 P0-00）。

用途：在开始 Alpha V2 任何改造前/后，冻结一份"Legacy 行为面 + V2 开关状态 +
代码/配置身份"记录，落盘到 ``artifacts/alpha_v2/audit/baseline_manifest.json``。

只在显式执行本脚本时写盘：Legacy 运行路径（夜扫 / 调度 / API）不会调用它，
因此 ``alpha_v2.enabled=false`` 时不产生副作用。

用法::

    python scripts/build_alpha_v2_baseline_manifest.py
    python scripts/build_alpha_v2_baseline_manifest.py --config config/default.yaml \\
        --output artifacts/alpha_v2/audit/baseline_manifest.json

退出码：0 = 已写出；1 = 失败（配置加载或写盘错误）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_analyzer.alpha_v2 import build_baseline_manifest, write_baseline_manifest  # noqa: E402
from stock_analyzer.config import get_config, load_config  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成 Alpha V2 基线清单")
    parser.add_argument(
        "--config",
        default="",
        help="配置文件路径；缺省走 get_config()（含 *.local.yaml 与 SA__ 环境覆盖）",
    )
    parser.add_argument(
        "--output",
        default="",
        help="输出路径；缺省 <alpha_v2.artifact_root>/audit/baseline_manifest.json",
    )
    parser.add_argument(
        "--project-root",
        default=str(ROOT),
        help="相对 artifact_root 的锚定目录；缺省为仓库根",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="只打印清单 JSON，不写盘、不创建目录",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        config = load_config(args.config) if args.config else get_config()
    except Exception as exc:  # noqa: BLE001 - CLI 边界统一转成退出码
        print(f"[alpha_v2] 配置加载失败: {exc}", file=sys.stderr)
        return 1

    if args.print_only:
        manifest = build_baseline_manifest(config, project_root=args.project_root)
        print(json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
        return 0

    try:
        target = write_baseline_manifest(
            config,
            project_root=args.project_root,
            output_path=args.output or None,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[alpha_v2] 基线清单写盘失败: {exc}", file=sys.stderr)
        return 1

    print(f"[alpha_v2] 基线清单已写入: {target}")
    print(
        "[alpha_v2] enabled="
        f"{config.alpha_v2.enabled} shadow_only={config.alpha_v2.shadow_only} "
        f"enforce_final_selection={config.alpha_v2.enforce_final_selection}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
