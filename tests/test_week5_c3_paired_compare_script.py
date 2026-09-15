"""`scripts/week5_c3_paired_compare.py` 的配对口径。

C3 §3 的配对表当初是一次性脚本算的、没进仓库，所以那些数字不可复现。本脚本把口径
固化下来，这个文件锁住三条最容易悄悄漂移的性质：

1. 配对是**同日取差**，不是两条序列各自求 CI 再比区间重叠；
2. 未配对的日要计数（`unpaired_days`），不能静默当成 0；
3. checkpoint 缺目录/为空必须报错退出，不能静默返回空序列让下游算出 NaN 结论。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "week5_c3_paired_compare", REPO_ROOT / "scripts" / "week5_c3_paired_compare.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_checkpoint(out_dir: Path, variant: str, folds: list[dict[str, object]]) -> None:
    ckpt = out_dir / f"checkpoints_{variant}"
    ckpt.mkdir(parents=True, exist_ok=True)
    for index, fold in enumerate(folds):
        payload = {"fold_id": index, "status": "completed", **fold}
        (ckpt / f"fold_{index:02d}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


def test_paired_delta_uses_same_day_differences(tmp_path: Path) -> None:
    module = _load_module()
    # 三个同日 + 一个只在 current 里 → 配对 3 天，未配对 1 天
    current = {"2026-06-01": 0.10, "2026-06-02": 0.12, "2026-06-03": 0.14, "2026-06-04": 0.20}
    baseline = {"2026-06-01": 0.05, "2026-06-02": 0.06, "2026-06-03": 0.09}
    result = module.paired_delta(current, baseline, block_days=1)
    assert result["valid_days"] == 3
    assert result["unpaired_days"] == 1
    assert result["delta_ic_mean"] == pytest.approx((0.05 + 0.06 + 0.05) / 3)
    # 差值恒正 → CI 下界必须为正（若写成"两个 CI 比重叠"这里就测不出来）
    assert result["delta_ic_ci95"][0] > 0.0
    assert result["months_total"] == 1


def test_load_daily_ic_merges_folds_and_skips_unfinished(tmp_path: Path) -> None:
    module = _load_module()
    _write_checkpoint(
        tmp_path,
        "raw_blend",
        [
            {"daily_ic": [["2026-06-01", 0.1], ["2026-06-02", 0.2]]},
            {"status": "failed", "daily_ic": [["2026-06-03", 9.9]]},
            {"daily_ic": [["2026-06-03", 0.3]]},
        ],
    )
    series = module.load_daily_ic(tmp_path, "raw_blend")
    assert series == {"2026-06-01": 0.1, "2026-06-02": 0.2, "2026-06-03": 0.3}


def test_load_daily_ic_fails_closed_on_missing_checkpoint(tmp_path: Path) -> None:
    module = _load_module()
    with pytest.raises(SystemExit):
        module.load_daily_ic(tmp_path, "blend")


def test_cli_requires_a_pair(tmp_path: Path) -> None:
    module = _load_module()
    with pytest.raises(SystemExit):
        module.main(["--out-dir", str(tmp_path)])
