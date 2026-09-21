"""Alpha V2 M4-L：Production Data Preflight（NAS 上线前的只读数据体检）。

```bash
# 生产形态：训练窗与冻结模型工件绑定，PASS 才算数
python scripts/alpha_v2_production_preflight.py \
    --market-db artifacts/warehouse/market.duckdb \
    --model-dir artifacts/alpha_v2/validation/model/alpha_v2_shadow_epoch_001 \
    --training-start 2025-06-02 --training-end 2026-08-31

# 只算不落盘（人工排查）
python scripts/alpha_v2_production_preflight.py ... --print-only
```

职责边界（M4-L §26）：**发现 / 量化 / 阻断 / 记录**——

- 只读检查 market.duckdb 与配置，不写数据库、不改特征、不自动修 volume 单位；
- ``BLOCKED`` 必须非零退出（exit 1），且后续 ``alpha_v2_validation_freeze.py``
  的生产硬门会拒绝开 epoch（exit 7）；
- 审计工件默认落 ``artifacts/alpha_v2/audit/production_preflight_<ts>.json``
  （含 verdict / blocking_findings / warnings / facts / runtime_identity /
  data_identity / training_window / checks / generated_at）。

训练窗来源优先级：``--training-start/--training-end`` 显式给出；或从
``--model-dir`` 的 provenance.window 解析；两者都给必须一致（§23 防"查 A 训 B"）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from stock_analyzer.alpha_v2.validation.preflight import (  # noqa: E402
    VERDICT_BLOCKED,
    PreflightError,
    run_production_preflight,
    write_preflight_audit,
)
from stock_analyzer.config import load_config  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alpha V2 M4-L：Production Data Preflight")
    parser.add_argument("--market-db", default="artifacts/warehouse/market.duckdb")
    parser.add_argument("--config", default=str(REPO_ROOT / "config" / "default.yaml"))
    parser.add_argument("--training-start", default="")
    parser.add_argument("--training-end", default="")
    parser.add_argument(
        "--model-dir",
        default="",
        help="冻结影子模型工件目录（用于解析 provenance.window 与特征 schema）",
    )
    parser.add_argument("--feature-columns-file", default="")
    parser.add_argument(
        "--out-dir", default=str(REPO_ROOT / "artifacts" / "alpha_v2" / "audit")
    )
    parser.add_argument("--max-feature-probe-symbols", type=int, default=300)
    parser.add_argument(
        "--skip-feature-probe",
        action="store_true",
        help="跳过特征探针（离线/无数据环境）；结果最多为 WARN，不会伪装成 PASS",
    )
    parser.add_argument("--print-only", action="store_true")
    return parser.parse_args(argv)


def _window_from_model(model_dir: str) -> list[str] | None:
    from stock_analyzer.alpha_v2.validation.frozen_model import (
        frozen_model_identity_payload,
    )

    payload = frozen_model_identity_payload(model_dir)
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        return None
    window = provenance.get("window")
    if not isinstance(window, (list, tuple)) or len(window) != 2:
        return None
    return [str(window[0]), str(window[1])]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = load_config(Path(args.config))

    window: list[str] | None = None
    if args.training_start and args.training_end:
        window = [args.training_start, args.training_end]
    model_window: list[str] | None = None
    if args.model_dir:
        try:
            model_window = _window_from_model(args.model_dir)
        except Exception as exc:  # noqa: BLE001 - CLI 边界
            print(f"[preflight] 模型工件读取失败: {exc}", file=sys.stderr)
            return 2
        if model_window is None:
            print(
                "[preflight] 模型工件缺 provenance.window，无法把检查绑定到训练窗（§23）",
                file=sys.stderr,
            )
            return 2
        if window is not None and window != model_window:
            print(
                f"[preflight] 拒绝：显式训练窗 {window} 与模型 provenance {model_window} 不一致",
                file=sys.stderr,
            )
            return 2
        window = model_window
    if window is None:
        print(
            "[preflight] 必须给出 --training-start/--training-end 或 --model-dir（§23）",
            file=sys.stderr,
        )
        return 2
    try:
        training_start = date.fromisoformat(window[0])
        training_end = date.fromisoformat(window[1])
    except ValueError as exc:
        print(f"[preflight] 训练窗日期不可解析: {window}（{exc}）", file=sys.stderr)
        return 2

    feature_columns: list[str] = []
    if args.feature_columns_file:
        feature_columns = list(
            json.loads(Path(args.feature_columns_file).read_text(encoding="utf-8"))
        )
    elif args.model_dir:
        from stock_analyzer.alpha_v2.validation.frozen_model import (
            frozen_model_identity_payload,
        )

        feature_columns = list(
            frozen_model_identity_payload(args.model_dir).get("feature_columns", [])
            or []
        )

    try:
        payload = run_production_preflight(
            config=config,
            repo_root=REPO_ROOT,
            market_db=args.market_db,
            training_start=training_start,
            training_end=training_end,
            feature_columns=feature_columns,
            model_dir=args.model_dir or None,
            feature_probe_skipped_reason=(
                "--skip-feature-probe" if args.skip_feature_probe else ""
            ),
            max_feature_probe_symbols=int(args.max_feature_probe_symbols),
        )
    except PreflightError as exc:
        print(f"[preflight] 无法执行: {exc}", file=sys.stderr)
        return 2

    verdict = str(payload.get("verdict", ""))
    print(f"[preflight] verdict = {verdict}")
    for finding in payload.get("blocking_findings", []):
        print(f"[preflight] BLOCKING: {finding}", file=sys.stderr)
    for finding in payload.get("warnings", []):
        print(f"[preflight] WARNING: {finding}", file=sys.stderr)
    if args.print_only:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        path = write_preflight_audit(payload, audit_root=args.out_dir)
        print(f"[preflight] 审计工件: {path}")
    return 1 if verdict == VERDICT_BLOCKED else 0


if __name__ == "__main__":
    raise SystemExit(main())
