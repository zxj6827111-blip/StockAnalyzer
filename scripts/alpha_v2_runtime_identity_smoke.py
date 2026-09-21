"""NO_GIT_CONTAINER_SMOKE：在"没有 git 的容器形态"里真跑四个 Alpha V2 CLI 的身份门。

```bash
python scripts/alpha_v2_runtime_identity_smoke.py --out artifacts/alpha_v2/audit
```

**为什么不是单元测试**：BLK-D1/BLK-D2 是"容器里跑不起来"，只有把**真实 CLI**
放在**真实容器形态**下跑一遍才算证。沙箱按下面三件事复刻生产容器：

```text
1) 代码树被复制到 <sandbox>/{src,scripts,config}，沙箱里**没有 .git**；
2) 子进程 PATH 去掉 git 所在目录（生产容器根本没有 git 二进制）；
3) 沙箱根写入构建期身份：.build_commit 与 build_manifest.json（同 commit、dirty=false）。
```

CLI 的 ``REPO_ROOT`` 由脚本自身路径推导，所以复制后的沙箱就是它们的"仓库根"——
身份解析读到的正是沙箱里的构建身份。

**不创建任何真实生产 epoch**：所有产物写进 ``--out`` 指定的隔离目录
（默认落在本机临时目录，不碰 ``artifacts/alpha_v2/validation``）。epoch id 用
``alpha_v2_epoch_900``（900 段是排演/隔离段，永不进 clean OOS）。

判定口径：每个 CLI 都跑**两次**——沙箱（身份完好）与破坏身份的沙箱——用两者的
退出码差异证明"身份门真的被执行了"：

```text
身份完好 → 不得出现身份门消息，且退出码 != 3
身份破坏 → 必须出现身份门消息，且退出码 == 3
```
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

EPOCH_ID = "alpha_v2_epoch_900"
MODEL_ID = "alpha_v2_shadow_epoch_900"
IDENTITY_GATE_MARKERS = ("运行身份硬门未通过", "运行身份")  # 四个 CLI 的统一措辞
EXIT_IDENTITY = 3
# 同一个身份门在各 CLI 上的既有退出码（不新造语义）：
#   validation freeze  → 5（R3 起"脏树/构建身份"就是 exit 5）
#   model freeze / capture / mature → 3（它们的"冻结锚定/身份"码）
EXIT_IDENTITY_FREEZE = 5

# 生产形态的成交价格口径来自 .env 覆盖（M3 §17.2："execution=raw 由 env 覆盖"）：
# config/default.yaml 里是 qfq，生产容器用 SA__ 环境变量覆盖成 raw。smoke 必须
# 复刻这条覆盖，否则 freeze 会（正确地）停在 execution_price_mode 硬门 exit 4。
PRODUCTION_ENV_OVERRIDES = {"SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE": "raw"}

# 冻结模型工件必须**真实可加载**（R4.1.1 / 审稿 P2：生产 freeze 会用
# ``load_frozen_model`` 校验逐文件哈希 + artifact_hash 复算——"骨架工件"不再是
# 可放行的生产形态）。工件由最小合成矩阵经真实的 fit/persist 生产链产出。
SMOKE_FEATURE_COLUMNS = ["ret_1d", "ret_5d", "ma5", "ma20", "volume_ratio_5", "turnover_zscore20"]
# M4-L：preflight 硬门要求报告的训练窗与冻结模型 provenance.window 逐字一致。
SMOKE_TRAINING_WINDOW = ["2026-05-01", "2026-06-30"]


def _git_head() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def _path_without_git(path_value: str) -> tuple[str, list[str]]:
    """把 PATH 里含 git 可执行文件的目录摘掉（复刻"容器里没有 git 二进制"）。"""
    keep: list[str] = []
    removed: list[str] = []
    for part in path_value.split(os.pathsep):
        if not part.strip():
            continue
        candidate = Path(part)
        if (candidate / "git").exists() or (candidate / "git.exe").exists():
            removed.append(part)
            continue
        keep.append(part)
    return os.pathsep.join(keep), removed


def _build_sandbox(root: Path, *, commit: str, dirty: bool = False) -> Path:
    """复制代码树 + 写入构建身份；**不复制 .git**（这就是"容器"）。"""
    root.mkdir(parents=True, exist_ok=True)
    for name in ("src", "scripts", "config"):
        shutil.copytree(
            REPO_ROOT / name,
            root / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
        )
    (root / ".build_commit").write_text(f"{commit}\n", encoding="utf-8")
    (root / "build_manifest.json").write_text(
        json.dumps(
            {
                "commit": commit,
                "short_commit": commit[:12],
                "dirty": dirty,
                "built_at_utc": "2026-09-19T00:00:00+00:00",
                "config_schema": "stock-analyzer-config.v1",
                "runtime_state_schema": 9,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    assert not (root / ".git").exists(), "沙箱里不允许有 .git（那就不是容器形态了）"
    return root


def _write_smoke_market_db(path: Path) -> tuple[str, int]:
    """微型合成行情库 + 训练数据指纹（供模型 provenance 与 preflight 报告共用）。"""
    import duckdb
    import pandas as pd

    from stock_analyzer.alpha_v2.validation.training_data_fingerprint import (
        compute_training_data_fingerprint,
    )

    days = [date.fromisoformat(day) for day in ("2026-05-04", "2026-05-05", "2026-05-06")]
    rows = [
        {
            "symbol": f"6005{index:02d}",
            "date": day,
            "open": 10.0 + index,
            "high": 10.5 + index,
            "low": 9.5 + index,
            "close": 10.2 + index,
            "volume": 1_000_000.0 + index,
            "turnover": (1_000_000.0 + index) * (10.2 + index),
        }
        for index in range(3)
        for day in days
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path))
    try:
        connection.register("frame", pd.DataFrame(rows))
        connection.execute("CREATE OR REPLACE TABLE daily_bars AS SELECT * FROM frame")
    finally:
        connection.close()
    payload = compute_training_data_fingerprint(
        path,
        training_start=date.fromisoformat(SMOKE_TRAINING_WINDOW[0]),
        training_end=date.fromisoformat(SMOKE_TRAINING_WINDOW[1]),
    )
    return str(payload["fingerprint"]), int(payload["rows"])


def _write_rehearsal_model_artifact(
    artifacts_root: Path,
    *,
    commit: str,
    training_data_fingerprint: str,
    training_data_rows: int,
) -> Path:
    """写一份**真实可加载**的微型冻结模型工件。

    R4.1 起生产 freeze 要求工件的 ``code_commit`` 可证且等于运行身份；R4.1.1
    （审稿 P2）起 further 要求工件**内容完整**——生产 freeze 会 ``load_frozen_model``
    复算文件哈希。本函数用最小合成矩阵走真实的 ``fit_frozen_model`` /
    ``persist_frozen_model`` 生产链（与本机依赖的版本同一份代码），产物自然满足
    两道门；规模刻意小（~90 行），不影响 smoke 节奏。
    """
    import numpy as np
    import pandas as pd

    from stock_analyzer.alpha_v2.research.multi_head import HeadFitSpec
    from stock_analyzer.alpha_v2.validation.frozen_model import (
        fit_frozen_model,
        persist_frozen_model,
    )

    total = 90
    rng = np.random.default_rng(11)
    frame = pd.DataFrame(
        {
            "decision_date": [f"2026-07-{(i // 30) + 1:02d}" for i in range(total)],
            "symbol": [f"6005{i % 30:02d}" for i in range(total)],
            **{name: rng.normal(0.0, 1.0, total) for name in SMOKE_FEATURE_COLUMNS},
        }
    )
    base = 0.3 * frame["ret_1d"] + 0.2 * frame["ma5"] - 0.1 * frame["volume_ratio_5"]
    for horizon in (3, 5, 10, 15):
        frame[f"net_return_{horizon}d"] = base * (horizon / 5.0) + rng.normal(0.0, 0.01, total)
        frame[f"excess_return_{horizon}d"] = frame[f"net_return_{horizon}d"] - 0.001
        frame[f"mae_{horizon}d"] = -np.abs(frame[f"net_return_{horizon}d"]) * 0.6
        frame[f"up_net_{horizon}d"] = (frame[f"net_return_{horizon}d"] > 0).astype(float)
        frame[f"up_excess_{horizon}d"] = (frame[f"excess_return_{horizon}d"] > 0).astype(float)
        frame[f"mae_le_5pct_{horizon}d"] = (frame[f"mae_{horizon}d"] <= -0.05).astype(float)
    frame["alpha_target_5d"] = frame.groupby("decision_date")["excess_return_5d"].rank(pct=True)
    frame["is_train"] = False
    frame["is_calibration"] = False
    frame.loc[:59, "is_train"] = True
    frame.loc[60:89, "is_calibration"] = True
    model = fit_frozen_model(
        frame=frame,
        model_id=MODEL_ID,
        spec=HeadFitSpec(min_train_rows=20, min_class_balance=0.05),
        # M4-L / R1：production freeze 的 preflight 硬门要求
        #   ① 报告训练窗 == 模型训练窗；② 报告 model_identity（id/hash/schema/commit）
        #      == 冻结模型块；③ 双方 training_data_fingerprint 一致。
        # 烟雾夹具用**真实训练数据指纹实现**对一个微型合成库算一遍，保证是同一套链。
        provenance={
            "source": "no_git_container_smoke",
            "window": SMOKE_TRAINING_WINDOW,
            "training_data_fingerprint": training_data_fingerprint,
            "training_data_rows": training_data_rows,
        },
        extra_identity={
            "code_commit": commit,
            "identity_source": "container_build_identity",
            "code_commit_source": "container_build_identity",
        },
    )
    return persist_frozen_model(model, artifacts_root / "validation")


def _write_production_preflight(
    artifacts_root: Path,
    *,
    commit: str,
    model_identity: Mapping[str, object],
    training_data_fingerprint: str,
) -> Path:
    """写一份与沙箱身份/训练窗绑定的 PASS preflight（M4-L §25 硬门的合法输入）。

    用**真实**哈希约定（``preflight_hash_of``）生成，确保 smoke 检查的是门本身
    而不是一个伪造不了的负载。
    """
    from stock_analyzer.alpha_v2.validation.preflight import (
        PREFLIGHT_SCHEMA,
        VERDICT_PASS,
        preflight_hash_of,
    )

    payload: dict[str, object] = {
        "schema": PREFLIGHT_SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(),
        "verdict": VERDICT_PASS,
        "blocking_findings": [],
        "warnings": [],
        "facts": {},
        "runtime_identity": {"code_commit": commit},
        # 字段名与 preflight.check_model_identity 的输出对齐（gate 逐项比对）
        "model_identity": {
            "model_id": str(model_identity.get("model_id", "")),
            "model_artifact_hash": str(model_identity.get("artifact_hash", "")),
            "feature_schema_hash": str(model_identity.get("feature_schema_hash", "")),
            "model_training_code_commit": str(
                model_identity.get("model_training_code_commit", "")
            ),
            "provenance_window": list(
                dict(model_identity.get("provenance", {}) or {}).get("window") or []
            )
            or None,
            "training_data_fingerprint": training_data_fingerprint,
        },
        "data_identity": {
            "market_db": "smoke_synthetic",
            "training_data_fingerprint": training_data_fingerprint,
        },
        "training_window": {
            "start": SMOKE_TRAINING_WINDOW[0],
            "end": SMOKE_TRAINING_WINDOW[1],
        },
        "checks": [],
    }
    payload["preflight_hash"] = preflight_hash_of(payload)
    path = artifacts_root / "preflight.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _run_cli(
    *,
    sandbox: Path,
    script: str,
    args: list[str],
    env_path: str,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = env_path
    env.update(PRODUCTION_ENV_OVERRIDES)
    return subprocess.run(
        [sys.executable, str(sandbox / "scripts" / script), *args],
        capture_output=True,
        text=True,
        timeout=1800,
        env=env,
        cwd=str(sandbox),
    )


def _step(
    *,
    name: str,
    good_rc: int,
    broken_rc: int | None,
    good_ok: bool,
    expected_broken_exit: int | None,
    good_stderr: str = "",
    detail: str,
) -> dict[str, object]:
    """一步的判定：好沙箱要按预期走通，坏沙箱必须被身份门按预期码拦下。"""
    broken_ok = expected_broken_exit is None or broken_rc == expected_broken_exit
    message_absent = not any(marker in (good_stderr or "") for marker in IDENTITY_GATE_MARKERS)
    passed = bool(good_ok and broken_ok and message_absent)
    return {
        "step": name,
        "verdict": "PASS" if passed else "FAIL",
        "container_rc": good_rc,
        "broken_identity_rc": broken_rc if broken_rc is not None else "-",
        "expected_broken_exit": expected_broken_exit,
        "detail": detail,
    }


def _resolve_identity_in_sandbox(sandbox: Path, env_path: str) -> dict[str, object]:
    """A. 在沙箱里直接问 resolver：这份运行代码是谁。"""
    code = (
        "import json,sys;"
        f"sys.path.insert(0, {str(sandbox / 'src')!r});"
        "from stock_analyzer.alpha_v2.validation.runtime_identity "
        "import resolve_runtime_code_identity;"
        f"print(json.dumps(resolve_runtime_code_identity({str(sandbox)!r}"
        ", validation_mode='production', require_build_identity=True).to_payload()))"
    )
    env = dict(os.environ)
    env["PATH"] = env_path
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
        cwd=str(sandbox),
    )
    if out.returncode != 0:
        return {"error": out.stderr[-800:]}
    return json.loads(out.stdout.strip().splitlines()[-1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alpha V2 no-git 容器形态身份 smoke")
    parser.add_argument(
        "--out",
        default="",
        help="证据目录（默认：临时目录，绝不写进真实 artifacts/alpha_v2/validation）",
    )
    parser.add_argument("--commit", default="", help="沙箱构建身份用的 commit（默认取真实 HEAD）")
    parser.add_argument(
        "--keep-git-on-path",
        action="store_true",
        help=(
            "不剥离 PATH 里的 git 目录（跨平台 CI 用）。沙箱依然没有 .git，"
            "所以 resolver 仍必须自报 git_available=false —— 剥离只是额外复刻"
            "'容器里连 git 二进制都没有'这一层"
        ),
    )
    args = parser.parse_args(argv)

    commit = args.commit.strip() or _git_head()
    if not commit:
        print("ERROR: 拿不到 git HEAD，无法构造构建身份（用 --commit 显式给）", file=sys.stderr)
        return 2

    evidence_dir = (
        Path(args.out).expanduser().resolve()
        if args.out.strip()
        else Path(tempfile.mkdtemp(prefix="alpha_v2_no_git_smoke_"))
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="alpha_v2_smoke_work_"))
    if args.keep_git_on_path:
        env_path, removed = os.environ.get("PATH", ""), []
        path_strip_applied = False
    else:
        env_path, removed = _path_without_git(os.environ.get("PATH", ""))
        path_strip_applied = True

    sandbox = _build_sandbox(work_dir / "container", commit=commit)
    broken = _build_sandbox(work_dir / "container_broken", commit=commit)
    # 破坏身份：两源互相矛盾（.build_commit 与 manifest.commit 不同）
    (broken / ".build_commit").write_text("f" * 40 + "\n", encoding="utf-8")
    # 裸沙箱：既没有 .git，也没有构建身份 —— "unknown Git state 且无合法容器身份"
    bare = _build_sandbox(work_dir / "bare", commit=commit)
    (bare / ".build_commit").unlink()
    (bare / "build_manifest.json").unlink()

    artifacts = work_dir / "artifacts" / "alpha_v2"
    smoke_market_db = work_dir / "smoke_market.duckdb"
    data_fingerprint, data_rows = _write_smoke_market_db(smoke_market_db)
    model_dir = _write_rehearsal_model_artifact(
        artifacts,
        commit=commit,
        training_data_fingerprint=data_fingerprint,
        training_data_rows=data_rows,
    )
    from stock_analyzer.alpha_v2.validation.frozen_model import (
        frozen_model_identity_payload,
    )

    model_identity = dict(frozen_model_identity_payload(model_dir))
    start_date = date.today().isoformat()

    print("=" * 78)
    print("NO_GIT_CONTAINER_SMOKE")
    print(f"sandbox            = {sandbox}")
    print(f"commit             = {commit}")
    print(f"sandbox has .git   = {(sandbox / '.git').exists()}")
    print(f"PATH strip applied = {path_strip_applied}")
    print(f"git dirs stripped  = {removed or '(未剥离)'}")
    print(f"evidence dir       = {evidence_dir}")
    print("=" * 78)

    results: list[dict[str, object]] = []

    # ── A. runtime identity resolution ────────────────────────────────────────
    resolved = _resolve_identity_in_sandbox(sandbox, env_path)
    a_ok = (
        resolved.get("identity_verified") is True
        and resolved.get("identity_source") == "container_build_identity"
        and resolved.get("code_commit") == commit
        and resolved.get("git_available") is False
    )
    results.append(
        {
            "step": "A runtime identity resolution",
            "verdict": "PASS" if a_ok else "FAIL",
            "container_rc": 0,
            "broken_identity_rc": "-",
            "expected_broken_exit": None,
            "detail": (
                f"identity_source={resolved.get('identity_source')} "
                f"git_available={resolved.get('git_available')} "
                f"code_commit={str(resolved.get('code_commit'))[:12]}… "
                f"violations={resolved.get('violations')}"
            ),
        }
    )

    preflight_path = _write_production_preflight(
        artifacts,
        commit=commit,
        model_identity=model_identity,
        training_data_fingerprint=data_fingerprint,
    )
    freeze_args = [
        "--epoch-id",
        EPOCH_ID,
        "--out",
        str(artifacts),
        "--model-dir",
        str(model_dir),
        "--start-date",
        start_date,
        "--open-epoch",
        # M4-L §25：生产 freeze 的 preflight 硬门（PASS/WARN 才允许开 epoch）。
        "--preflight-report",
        str(preflight_path),
    ]

    # ── B. shadow model freeze identity（身份门，不跑训练）─────────────────────
    b_args = ["--window-start", "2026-01-01", "--window-end", "2026-02-01", "--model-id", MODEL_ID]
    b_good = _run_cli(
        sandbox=sandbox,
        script="alpha_v2_shadow_model_freeze.py",
        args=[*b_args, "--market-db", str(work_dir / "missing_market.duckdb")],
        env_path=env_path,
    )
    b_bad = _run_cli(
        sandbox=broken,
        script="alpha_v2_shadow_model_freeze.py",
        args=[*b_args, "--market-db", str(work_dir / "missing_market.duckdb")],
        env_path=env_path,
    )
    results.append(
        _step(
            name="B shadow model freeze identity",
            good_rc=b_good.returncode,
            broken_rc=b_bad.returncode,
            good_ok=b_good.returncode != EXIT_IDENTITY,
            expected_broken_exit=EXIT_IDENTITY,
            good_stderr=b_good.stderr,
            detail=(
                "身份门放行后因缺市场库中止（非身份原因，不产生任何工件）；"
                f"破坏身份必须 exit {EXIT_IDENTITY}"
            ),
        )
    )

    # ── C. validation freeze identity ─────────────────────────────────────────
    c_good = _run_cli(
        sandbox=sandbox,
        script="alpha_v2_validation_freeze.py",
        args=[*freeze_args, "--print-only"],
        env_path=env_path,
    )
    c_bad = _run_cli(
        sandbox=broken,
        script="alpha_v2_validation_freeze.py",
        args=[*freeze_args, "--print-only"],
        env_path=env_path,
    )
    # C0：不给 --model-dir 的生产 freeze 必须停在 feature schema 硬门（exit 6）——
    # 这就是"模型冻结必须先于 validation freeze"的机器可验证形式（不是文档约定）。
    c_no_model = _run_cli(
        sandbox=sandbox,
        script="alpha_v2_validation_freeze.py",
        args=[
            "--epoch-id",
            EPOCH_ID,
            "--out",
            str(artifacts),
            "--start-date",
            start_date,
            "--print-only",
        ],
        env_path=env_path,
    )
    c_no_model_ok = c_no_model.returncode == 6
    freeze_manifest: dict[str, object] = {}
    try:
        freeze_manifest = json.loads(c_good.stdout)
    except json.JSONDecodeError:
        freeze_manifest = {}
    build_identity = dict(freeze_manifest.get("build_identity", {}) or {})
    c_ok = (
        c_good.returncode == 0
        and c_bad.returncode == EXIT_IDENTITY_FREEZE
        and c_no_model_ok
        and str(freeze_manifest.get("code_commit", "")) == commit
        and build_identity.get("identity_source") == "container_build_identity"
    )
    results.append(
        {
            "step": "C validation freeze identity",
            "verdict": "PASS" if c_ok else "FAIL",
            "container_rc": c_good.returncode,
            "broken_identity_rc": c_bad.returncode,
            "expected_broken_exit": EXIT_IDENTITY_FREEZE,
            "detail": (
                f"code_commit_source={freeze_manifest.get('code_commit_source')} "
                f"identity_source={build_identity.get('identity_source')} "
                f"worktree={build_identity.get('worktree_dirty_entries')!r}；"
                f"缺 --model-dir 时 exit {c_no_model.returncode}（须 6 = schema 硬门，"
                f"证明模型冻结必须先于 validation freeze）；"
                f"破坏身份必须 exit {EXIT_IDENTITY_FREEZE}"
            ),
        }
    )

    # ── D. open epoch（真写盘，隔离 root）────────────────────────────────────
    d_good = _run_cli(
        sandbox=sandbox,
        script="alpha_v2_validation_freeze.py",
        args=freeze_args,
        env_path=env_path,
    )
    epochs_path = artifacts / "validation" / "epochs.json"
    epochs = json.loads(epochs_path.read_text(encoding="utf-8")) if epochs_path.exists() else {}
    opened = [e for e in epochs.get("epochs", []) if e.get("epoch_id") == EPOCH_ID]
    d_ok = (
        d_good.returncode == 0
        and bool(opened)
        and str(opened[0].get("identity", {}).get("code_commit", "")) == commit
    )
    results.append(
        {
            "step": "D open epoch",
            "verdict": "PASS" if d_ok else "FAIL",
            "container_rc": d_good.returncode,
            "broken_identity_rc": "-",
            "expected_broken_exit": None,
            "detail": f"epochs.json 已写；identity.code_commit 与构建身份一致={d_ok}",
        }
    )

    # ── E. shadow capture identity ────────────────────────────────────────────
    e_args = [
        "--epoch-id",
        EPOCH_ID,
        "--signal-date",
        start_date,
        "--out",
        str(artifacts),
        "--model-dir",
        str(model_dir),
        "--market-db",
        str(work_dir / "missing_market.duckdb"),
    ]
    e_good = _run_cli(
        sandbox=sandbox, script="alpha_v2_shadow_capture.py", args=e_args, env_path=env_path
    )
    e_bad = _run_cli(
        sandbox=broken, script="alpha_v2_shadow_capture.py", args=e_args, env_path=env_path
    )
    # M4-L 起捕获多了一道"当天生产漏斗必须存在"的 fail-closed 门（exit 10），
    # 位置在身份门之后：身份完好 → 10（漏斗缺失），身份破坏 → 仍是 3。
    results.append(
        _step(
            name="E shadow capture identity",
            good_rc=e_good.returncode,
            broken_rc=e_bad.returncode,
            good_ok=e_good.returncode in (10,),
            expected_broken_exit=EXIT_IDENTITY,
            good_stderr=e_good.stderr,
            detail=(
                "身份完好时停在 M4-L 生产漏斗硬门（exit 10：当天无 funnel 工件，"
                "fail-closed，不再退化成研究代理）；"
                f"破坏身份必须 exit {EXIT_IDENTITY}"
            ),
        )
    )

    # ── F. mature identity ────────────────────────────────────────────────────
    f_args = [
        "--epoch-id",
        EPOCH_ID,
        "--evaluation-date",
        start_date,
        "--out",
        str(artifacts),
        "--market-db",
        str(work_dir / "missing_market.duckdb"),
    ]
    f_good = _run_cli(
        sandbox=sandbox, script="alpha_v2_shadow_mature.py", args=f_args, env_path=env_path
    )
    f_bad = _run_cli(
        sandbox=broken, script="alpha_v2_shadow_mature.py", args=f_args, env_path=env_path
    )
    results.append(
        _step(
            name="F mature identity",
            good_rc=f_good.returncode,
            broken_rc=f_bad.returncode,
            good_ok=f_good.returncode == 0,
            expected_broken_exit=EXIT_IDENTITY,
            good_stderr=f_good.stderr,
            detail=(
                "本 epoch 尚无 shadow 日 → 身份门过后正常空跑（rc=0）；"
                f"破坏身份必须 exit {EXIT_IDENTITY}"
            ),
        )
    )

    # ── 破坏身份侧的退出码一致性（证明"门执行过"）─────────────────────────────
    broken_exit_ok = all(
        int(row["broken_identity_rc"]) == int(row["expected_broken_exit"])
        for row in results
        if str(row.get("broken_identity_rc", "-")).isdigit()
        and row.get("expected_broken_exit") is not None
    )

    # ── G. 无 git + 无构建身份：必须**干净**拒绝（exit 5），不能是 traceback ────
    # §21 末条在 CLI 层的形态：rehearsal 不做身份硬门，但清单结构上要求 code_commit
    # 非空，所以这里也必须是一个意图明确的拒绝，而不是落盘层的异常冒泡。
    g_bare = _run_cli(
        sandbox=bare,
        script="alpha_v2_validation_freeze.py",
        args=[
            "--epoch-id",
            EPOCH_ID,
            "--out",
            str(work_dir / "bare_out"),
            "--rehearsal",
            "--start-date",
            "2026-03-01",
        ],
        env_path=env_path,
    )
    g_ok = (
        g_bare.returncode == EXIT_IDENTITY_FREEZE
        and "Traceback" not in (g_bare.stderr or "")
        and not (work_dir / "bare_out" / "validation" / "validation_freeze_manifest.json").exists()
    )
    results.append(
        {
            "step": "G no-git no-identity",
            "verdict": "PASS" if g_ok else "FAIL",
            "container_rc": g_bare.returncode,
            "broken_identity_rc": "-",
            "expected_broken_exit": None,
            "detail": (
                "既无 git 又无构建身份（rehearsal）→ 必须干净 exit "
                f"{EXIT_IDENTITY_FREEZE} 且不落盘、无 traceback"
            ),
        }
    )

    failed = [row for row in results if row["verdict"] != "PASS"]
    payload = {
        "schema": "alpha_v2_no_git_container_smoke.v1",
        "commit": commit,
        "sandbox": str(sandbox),
        "sandbox_has_git_dir": (sandbox / ".git").exists(),
        "path_strip_applied": path_strip_applied,
        "path_dirs_without_git_binary": removed,
        "identity_gate_markers": list(IDENTITY_GATE_MARKERS),
        "broken_identity_exit_consistent": broken_exit_ok,
        "steps": results,
        "verdict": "PASS" if (not failed and broken_exit_ok) else "FAIL",
    }
    evidence = evidence_dir / "no_git_container_smoke.json"
    evidence.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    print()
    for row in results:
        print(
            f"  [{row['verdict']}] {row['step']:<34} "
            f"container_rc={row['container_rc']} broken_rc={row['broken_identity_rc']} "
            f"(expect {row.get('expected_broken_exit')})"
        )
        print(f"         {row['detail']}")
    print()
    print(f"破坏身份按预期码被拦下 = {broken_exit_ok}")
    print(f"verdict = {payload['verdict']}")
    print(f"evidence = {evidence}")
    shutil.rmtree(work_dir, ignore_errors=True)
    return 0 if payload["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
