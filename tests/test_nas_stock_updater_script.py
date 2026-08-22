"""受管 NAS updater（scripts/nas_stock_updater.sh）的契约测试。

背景：2026-08-20 生产 cron 因外部脚本与镜像参数错位失败（rc=2），index 与
delta 停在 8/19 而 ZIP 已到 8/20。该脚本迁入仓库后由部署脚本原子安装，
这些测试锁住它的关键运维能力与编排约束，防止回归。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _script() -> str:
    return (REPO_ROOT / "scripts" / "nas_stock_updater.sh").read_text(
        encoding="utf-8",
    )


def test_managed_updater_is_single_transaction() -> None:
    """一次一体化调用完成 ZIP+index+delta+readiness，不再拆三次独立调用。"""
    script = _script()

    assert "--require-readiness" in script
    assert "--sync-vendor-delta /app/artifacts/vendor_delta/market_delta.duckdb" in script
    assert "--index-path /app/artifacts/vendor_overlay/daily_index.json" in script
    assert "--batch" in script
    # 外层独立的 index/delta 步骤必须删除：重复执行会破坏事务边界。
    assert "run_index" not in script
    assert "run_delta_sync" not in script
    assert "build_vendor_zip_daily_index.py" not in script
    assert "import_vendor_zip_to_delta.py" not in script


def test_managed_updater_drops_deprecated_intraday_summary_flags() -> None:
    """废弃的分钟摘要参数不得再传给 updater（2026-08-20 事故的直接诱因）。"""
    script = _script()

    assert "--intraday-summary-output" not in script
    assert "--intraday-summary-keep-days" not in script
    assert "--intraday-summary-required-latest-date" not in script


def test_managed_updater_keeps_operational_capabilities() -> None:
    """从生产脚本迁移时必须保留的运维能力：锁/续跑/分类重试/日志。"""
    script = _script()

    # 单实例锁：手动运行与 cron 冲突保护。
    assert 'exec 9>"$BASE/logs/updater.lock"' in script
    assert "flock -n 9" in script
    # checkpoint 续跑。
    assert "--checkpoint /data/update_checkpoint.json" in script
    # "empty 属预期" 分类判定 + 首次失败后 30 分钟重试。
    assert 'bad = [f for f in fails if "empty" not in f]' in script
    assert "sleep 1800" in script
    # 日志与 JSON summary 落盘路径保持不变。
    assert "$BASE/logs/updater.log" in script
    assert "$BASE/logs/updater_last.json" in script
    # Tushare token 经 env-file 传入，不落脚本。
    assert '--env-file "$ENVFILE"' in script


def test_managed_updater_verifies_readiness_after_success() -> None:
    """成功退出前必须复核 summary：ok=true 且 readiness.written=true。"""
    script = _script()

    assert 'd.get("ok")' in script
    assert 'readiness.get("written")' in script
    assert "readiness verification FAILED" in script


def test_managed_updater_supports_manual_date_override() -> None:
    """一次性补跑历史日需要日期覆盖接口；缺省行为仍是当天。"""
    script = _script()

    assert 'END="${UPDATER_END_DATE:-$(date +%Y-%m-%d)}"' in script


def test_managed_updater_keeps_runtime_volume_mounts() -> None:
    """vendor 目录可写（ZIP 更新）、artifacts 卷承载 index/delta/readiness。"""
    script = _script()

    assert "/vol1/1000/股票历史数据:/data:rw" in script
    assert "stock_analyzer_runtime_artifacts:/app/artifacts" in script
    # 分钟摘要挂载已无用途：updater 不再读写 480 日摘要。
    assert "intraday_summary" not in script


@pytest.mark.skipif(os.name == "nt", reason="Bash syntax is verified on Linux CI")
def test_managed_updater_has_valid_bash_syntax() -> None:
    completed = subprocess.run(
        ["bash", "-n", str(REPO_ROOT / "scripts" / "nas_stock_updater.sh")],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
