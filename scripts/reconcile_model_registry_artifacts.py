"""Model Registry 与磁盘工件对账（S05 / 原 P0-02，只读报告）。

用途：把 registry 里的历史坏记录**分类标注**（不伪造修复），并给后续治理动作
（重训/重登记/归档）提供事实基础。报告写入 ``artifacts/alpha_v2/audit/``。

只读保证：本脚本不写 registry、不改工件、不移动归档文件；只读学习库
（``learning_protocol.duckdb`` 的 model_registry 表）与磁盘工件。

用法::

    python scripts/reconcile_model_registry_artifacts.py \\
        --protocol-db artifacts/training/learning_protocol.duckdb \\
        --archive-root artifacts/model_archive \\
        --alias-path artifacts/model_v1.json \\
        --output-json artifacts/alpha_v2/audit/model_registry_reconciliation.json

退出码：0 = 已生成报告；1 = 学习库不可读（如实失败，不产出空报告）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_analyzer.models.registry_reconciliation import (  # noqa: E402
    build_reconciliation_report,
)


def _load_records(protocol_db: Path) -> list[dict[str, object]]:
    import duckdb  # noqa: WPS433

    con = duckdb.connect(str(protocol_db), read_only=True)
    try:
        rows = con.execute(
            "SELECT model_id, artifact_uri, artifact_content_hash, lifecycle_state "
            "FROM model_registry"
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "model_id": str(row[0]),
            "artifact_uri": str(row[1]),
            "artifact_content_hash": str(row[2]),
            "lifecycle_state": str(row[3]),
        }
        for row in rows
    ]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Model Registry 工件对账（只读）")
    parser.add_argument(
        "--protocol-db",
        default=str(ROOT / "artifacts" / "training" / "learning_protocol.duckdb"),
    )
    parser.add_argument("--archive-root", default=str(ROOT / "artifacts" / "model_archive"))
    parser.add_argument("--alias-path", default=str(ROOT / "artifacts" / "model_v1.json"))
    parser.add_argument(
        "--output-json",
        default=str(
            ROOT / "artifacts" / "alpha_v2" / "audit" / "model_registry_reconciliation.json"
        ),
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="只打印报告，不写盘",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    protocol_db = Path(args.protocol_db).expanduser()
    if not protocol_db.exists():
        print(f"[reconcile] 学习库不存在: {protocol_db}", file=sys.stderr)
        return 1
    try:
        records = _load_records(protocol_db)
    except Exception as exc:  # noqa: BLE001 - CLI 边界统一转退出码
        print(f"[reconcile] 读取 registry 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    report = build_reconciliation_report(
        records,
        alias_paths=[args.alias_path],
        archive_root=args.archive_root,
    )
    report["generated_at"] = datetime.now().astimezone().isoformat()
    report["protocol_db"] = str(protocol_db)
    report["archive_root"] = str(Path(args.archive_root).expanduser())
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.print_only:
        print(text)
        return 0
    output = Path(args.output_json).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    print(f"[reconcile] 对账报告已写入: {output}")
    print(f"[reconcile] 分类计数: {report['kind_counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
