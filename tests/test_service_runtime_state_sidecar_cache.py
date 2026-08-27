"""runtime_state history sidecar 的读缓存 / 跳写 / 孤儿 tmp 清理行为测试。

背景：``runtime_state.json`` 每次被任一进程改写，其它进程下一个请求就会触发
全量重载，而重载会逐个读取并解析 17 个 history sidecar。线上实测这些 sidecar
合计 60MB / 1.3 万条记录，纯解析约 1.2s（纯 CPU），是接口延迟毛刺的主因。

这里覆盖三条优化的行为契约：
1. 文件字节未变时复用已解析结果，不重复解析；
2. 内容未变时跳过重写，避免无意义地推进 mtime（mtime 一变就会触发其它进程重载）；
3. 中断残留的 ``*.tmp`` 会被清理，但不会误删正在写入的新临时文件。
"""

from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from stock_analyzer.runtime.services import runtime_state_service as state_module
from stock_analyzer.runtime.services.runtime_state_service import RuntimeStateService

_IDENTITY_KEYS = ("timestamp",)


class _StubService:
    """最小服务替身：只提供 sidecar 读写所需的路径与合并函数。"""

    def __init__(self, state_path: Path) -> None:
        self._runtime_state_path = str(state_path)

    def _merge_runtime_state_history(
        self,
        existing_raw: object,
        current_raw: object,
        *,
        limit: int,
        identity_keys: tuple[str, ...],
    ) -> list[dict[str, object]]:
        merged: dict[tuple[str, ...], dict[str, object]] = {}
        for source in (existing_raw, current_raw):
            if not isinstance(source, list):
                continue
            for item in source:
                if not isinstance(item, dict):
                    continue
                key = tuple(str(item.get(name, "")) for name in identity_keys)
                merged[key] = deepcopy(item)
        values = list(merged.values())
        return values[-max(1, limit) :]


def _build(tmp_path: Path) -> tuple[RuntimeStateService, Path]:
    stub = _StubService(tmp_path / "runtime_state.json")
    sidecar_service = RuntimeStateService(stub)
    sidecar_dir = tmp_path / "runtime_state_history"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    return sidecar_service, sidecar_dir


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _rows(count: int, *, tag: str = "r") -> list[dict[str, object]]:
    return [{"timestamp": f"2026-08-27T10:00:{index:02d}", "tag": tag} for index in range(count)]


def test_sidecar_read_reuses_parsed_rows_when_file_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar_service, sidecar_dir = _build(tmp_path)
    _write_jsonl(sidecar_dir / "demo_history.jsonl", _rows(3))

    calls = {"count": 0}
    original_loads = json.loads

    def _counting_loads(*args: Any, **kwargs: Any) -> Any:
        calls["count"] += 1
        return original_loads(*args, **kwargs)

    monkeypatch.setattr(state_module.json, "loads", _counting_loads)

    first = sidecar_service._load_runtime_state_history_sidecar("demo_history", limit=10)
    after_first = calls["count"]
    assert len(first) == 3
    assert after_first == 3, "首次加载应逐行解析"

    second = sidecar_service._load_runtime_state_history_sidecar("demo_history", limit=10)
    assert second == first
    assert calls["count"] == after_first, "文件未变时不应重复解析"


def test_sidecar_read_respects_limit_from_cache(tmp_path: Path) -> None:
    sidecar_service, sidecar_dir = _build(tmp_path)
    _write_jsonl(sidecar_dir / "demo_history.jsonl", _rows(5))

    # 先用大 limit 填充缓存，再用小 limit 读取，必须仍然只返回尾部 N 条
    sidecar_service._load_runtime_state_history_sidecar("demo_history", limit=10)
    limited = sidecar_service._load_runtime_state_history_sidecar("demo_history", limit=2)
    assert [row["timestamp"] for row in limited] == [
        "2026-08-27T10:00:03",
        "2026-08-27T10:00:04",
    ]


def test_sidecar_read_reparses_after_file_changes(tmp_path: Path) -> None:
    sidecar_service, sidecar_dir = _build(tmp_path)
    path = sidecar_dir / "demo_history.jsonl"
    _write_jsonl(path, _rows(3, tag="old"))
    assert sidecar_service._load_runtime_state_history_sidecar("demo_history", limit=10)[0][
        "tag"
    ] == "old"

    _write_jsonl(path, _rows(4, tag="new"))
    reloaded = sidecar_service._load_runtime_state_history_sidecar("demo_history", limit=10)
    assert len(reloaded) == 4
    assert reloaded[0]["tag"] == "new"


def test_sidecar_read_returns_empty_when_missing(tmp_path: Path) -> None:
    sidecar_service, _ = _build(tmp_path)
    assert sidecar_service._load_runtime_state_history_sidecar("absent_history", limit=10) == []


def _install_single_spec(
    sidecar_service: RuntimeStateService,
    sidecar_dir: Path,
    records: list[dict[str, object]],
    *,
    limit: int = 10,
) -> None:
    def _specs() -> list[tuple[str, list[dict[str, object]], int, Path, tuple[str, ...]]]:
        return [
            (
                "demo_history",
                records,
                limit,
                sidecar_dir / "demo_history.jsonl",
                _IDENTITY_KEYS,
            )
        ]

    sidecar_service._runtime_state_sidecar_specs = _specs  # type: ignore[method-assign]


def test_persist_skips_rewrite_when_content_unchanged(tmp_path: Path) -> None:
    sidecar_service, sidecar_dir = _build(tmp_path)
    records = _rows(3)
    _install_single_spec(sidecar_service, sidecar_dir, records)
    path = sidecar_dir / "demo_history.jsonl"

    sidecar_service._persist_runtime_state_history_sidecars({})
    assert path.exists()
    first_stat = path.stat()

    # 记录内容完全一致：不应重写，mtime 必须保持不变
    sidecar_service._persist_runtime_state_history_sidecars({})
    second_stat = path.stat()
    assert second_stat.st_mtime_ns == first_stat.st_mtime_ns
    assert second_stat.st_size == first_stat.st_size
    assert list(sidecar_dir.glob("*.tmp")) == [], "跳过重写时不应留下临时文件"


def test_persist_rewrites_when_records_change(tmp_path: Path) -> None:
    sidecar_service, sidecar_dir = _build(tmp_path)
    records = _rows(2)
    _install_single_spec(sidecar_service, sidecar_dir, records)
    path = sidecar_dir / "demo_history.jsonl"

    sidecar_service._persist_runtime_state_history_sidecars({})
    before = path.read_text(encoding="utf-8")

    records.append({"timestamp": "2026-08-27T11:00:00", "tag": "added"})
    sidecar_service._persist_runtime_state_history_sidecars({})
    after = path.read_text(encoding="utf-8")

    assert after != before
    assert "2026-08-27T11:00:00" in after
    assert len(after.strip().splitlines()) == 3


def test_persist_updates_read_cache_so_reload_skips_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar_service, sidecar_dir = _build(tmp_path)
    _install_single_spec(sidecar_service, sidecar_dir, _rows(3))
    sidecar_service._persist_runtime_state_history_sidecars({})

    calls = {"count": 0}
    original_loads = json.loads

    def _counting_loads(*args: Any, **kwargs: Any) -> Any:
        calls["count"] += 1
        return original_loads(*args, **kwargs)

    monkeypatch.setattr(state_module.json, "loads", _counting_loads)

    rows = sidecar_service._load_runtime_state_history_sidecar("demo_history", limit=10)
    assert len(rows) == 3
    assert calls["count"] == 0, "刚写出的内容应已在读缓存中，重载无需再解析"


def test_persist_sweeps_stale_tmp_files_but_keeps_fresh_ones(tmp_path: Path) -> None:
    sidecar_service, sidecar_dir = _build(tmp_path)
    _install_single_spec(sidecar_service, sidecar_dir, _rows(1))

    stale = sidecar_dir / "demo_history.jsonl.deadbeef.tmp"
    stale.write_text("stale\n", encoding="utf-8")
    stale_epoch = time.time() - 7200.0
    os.utime(stale, (stale_epoch, stale_epoch))

    fresh = sidecar_dir / "demo_history.jsonl.cafebabe.tmp"
    fresh.write_text("fresh\n", encoding="utf-8")

    sidecar_service._persist_runtime_state_history_sidecars({})

    assert not stale.exists(), "超过 1 小时的孤儿临时文件应被清理"
    assert fresh.exists(), "新临时文件可能属于其它进程正在写入，不能删"


def test_tmp_sweep_is_throttled(tmp_path: Path) -> None:
    sidecar_service, sidecar_dir = _build(tmp_path)
    _install_single_spec(sidecar_service, sidecar_dir, _rows(1))

    sidecar_service._persist_runtime_state_history_sidecars({})

    # 第一次落盘已执行清理；紧接着造一个旧 tmp，节流窗口内不应被扫到
    stale = sidecar_dir / "demo_history.jsonl.feedface.tmp"
    stale.write_text("stale\n", encoding="utf-8")
    stale_epoch = time.time() - 7200.0
    os.utime(stale, (stale_epoch, stale_epoch))

    sidecar_service._persist_runtime_state_history_sidecars({})
    assert stale.exists(), "清理应按 10 分钟节流，避免每次落盘都扫描目录"
