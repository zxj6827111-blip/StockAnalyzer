from __future__ import annotations

from pathlib import Path


def test_key_user_facing_files_have_no_mojibake_markers() -> None:
    root = Path(__file__).resolve().parents[1]
    targets = [
        root / "src" / "stock_analyzer" / "command" / "wecom_interaction.py",
        root / "src" / "stock_analyzer" / "runtime" / "service.py",
        root / "src" / "stock_analyzer" / "main.py",
        root / "src" / "stock_analyzer" / "cli.py",
        root / "README.md",
        root / "V3.2_闲时任务可执行规格表.md",
        root / "offhours_evolution_plan_v1.6.md",
    ]
    # Typical mojibake fragments seen when UTF-8 text is decoded with a wrong encoding.
    markers = [
        "Ã",
        "â€",
        "â€™",
        "\ufffd",
        "鍚",
        "鎸",
        "锛",
        "鏈€",
        "鏆",
        "鎭",
        "纭",
        "甯",
        "鍛",
        "璁",
        "绔炰环",
    ]
    for path in targets:
        text = path.read_text(encoding="utf-8")
        for marker in markers:
            assert marker not in text, f"mojibake marker {marker!r} found in {path}"
