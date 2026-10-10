"""把 trend 净盈利标签契约注册进 ``LabelPolicyRegistry``，或只核对已注册的那条。

```bash
# 注册（写 registry 表；按 hash 幂等，重复跑不会积累重复行）
python scripts/register_tail_label_policy.py --registry-db data/learning_protocol.duckdb

# 只读核对：清单/留档里那个 label_policy_id 到底存不存在、字段是否等于当前契约
python scripts/register_tail_label_policy.py --registry-db <db> --verify-only
```

为什么要单独一个命令：``labels/tail_net_profit.py`` 能构造契约记录，但**没有任何调用方
把它落库**，于是影子留档里的 ``label_policy_id`` 只是一个字符串，谁都核不了。
注册是显式动作而不是运行时副作用 —— registry 表在生产学习库里，让一次只读的尾盘
影子运行顺手写它，等于让读路径决定生产状态。

``--verify-only`` 读到的三种失败要区分开：没这张表/查不到这个 id（来源缺失）、
查到了但字段不一样（口径漂移，比查不到更危险：它会用另一套 TP/SL 或持有期解释这批
样本）、以及 registry 根本读不了。

退出码是**真实退出码**：

- ``0`` 已注册且逐字段等于当前契约
- ``3`` 读得到但没注册/对不上 → 标签口径未绑定，选股质量验收不得引用这批样本
- ``5`` registry 或契约不可用（库文件读不出、契约摘要不一致）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.contracts.trend_strategy import DEFAULT_TREND_CONTRACT  # noqa: E402
from stock_analyzer.labels.tail_net_profit import (  # noqa: E402
    register_tail_label_policy,
    tail_label_policy_record,
    verify_tail_label_policy,
)
from stock_analyzer.learning.label_policy_registry import (  # noqa: E402
    LabelPolicyRegistry,
)

RC_OK = 0
RC_UNBOUND = 3
RC_ERROR = 5


def _payload(*, registry_db: str, contract: Any, expected: Any, record: Any,
             failures: tuple[str, ...]) -> dict[str, Any]:
    stored = record if record is not None else expected
    return {
        "registry_db": str(registry_db),
        "contract_version": contract.contract_version,
        "contract_digest": contract.digest(),
        # 报的是**当前契约推导出的** id；stored_* 是 registry 里真查到的那份，
        # 两者不一样时 failures 里一定写着 drifts_from_tail_contract。
        "label_policy_id": expected.label_policy_id,
        "label_name": expected.label_name,
        "stored_label_name": stored.label_name,
        "stored_label_policy_hash": stored.label_policy_hash,
        "stored_maturity_rule": stored.maturity_rule,
        "stored_price_basis": stored.price_basis,
        "registered": record is not None,
        "failures": list(failures),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-db", required=True,
                        help="learning_protocol.duckdb（LabelPolicyRegistry 落库处）")
    parser.add_argument("--verify-only", action="store_true",
                        help="只读核对，不写 registry 表")
    parser.add_argument("--out", default="")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    contract = DEFAULT_TREND_CONTRACT
    expected = tail_label_policy_record(contract)
    registry = LabelPolicyRegistry(args.registry_db)
    try:
        if not args.verify_only:
            registered = register_tail_label_policy(registry, contract)
            if registered.label_policy_hash != expected.label_policy_hash:
                # 同 id 不同内容会被 registry 自己拒掉；这里防的是"幂等返回了别的东西"。
                print(f"registry 返回了另一份契约：{registered.label_policy_id}", file=sys.stderr)
                return RC_ERROR
        record, failures = verify_tail_label_policy(
            registry, label_policy_id=expected.label_policy_id, contract=contract
        )
    except (OSError, ValueError) as exc:
        print(f"registry 不可用: {type(exc).__name__}: {exc}", file=sys.stderr)
        return RC_ERROR

    payload = _payload(registry_db=args.registry_db, contract=contract, expected=expected,
                       record=record, failures=failures)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    if not args.quiet:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    if failures:
        print(f"标签口径未绑定: {failures}", file=sys.stderr)
        return RC_UNBOUND
    return RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
