"""在服工件的身份对账：把「进程实际加载的文件」与「注册表登记的 champion」对起来。

**为什么需要它**（2026-09-16 实证）：`registry` 有 `artifact_content_hash` 列、`bundle.py`
有 `compute_artifact_identity_hash`，但**加载路径从不参与**——`AnalyzerPipeline` 是按
`config.training.artifact_path` 直接读文件的。后果：

- 生产实际在跑哪个工件，只能靠文件 mtime 旁证（我排查 raw A/B 自检失败时就吃了这个亏，
  只能猜"是不是工件被换过"）；
- 注册表里唯一指向该文件的行是 `revoked` + `blocked_reason=quarantine:empty_content_hash`，
  而在服文件的哈希谁都没算过——**"登记 ≠ 发布 ≠ 热载"没有可验证的连接**。

本模块只做**判定**，不做门禁：返回状态与两侧哈希，由调用方决定报警还是拒绝。
把判定与执行分开，是为了让"先观测、再决定要不要强制"这条路径可走。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

# 状态取值（稳定契约，供接口与测试引用）
IDENTITY_MATCH = "match"
IDENTITY_MISMATCH = "mismatch"
IDENTITY_NO_CHAMPION = "no_champion"
IDENTITY_CHAMPION_HASH_MISSING = "champion_hash_missing"
IDENTITY_LOADED_HASH_MISSING = "loaded_hash_missing"
IDENTITY_REGISTRY_UNAVAILABLE = "registry_unavailable"
# 注册表被**本进程持有的写锁**挡住 —— 属"这次读不到"，不是"身份对不上"。
# 2026-09-16 盘中实测：巡检 10:50 报 registry_unavailable 被当 defect，其实只是 api 容器
# 正在写这个库。探测器假警报一次就会被当噪音，必须与真错误分开。
IDENTITY_REGISTRY_BUSY = "registry_busy"
# 在服工件与某条**已登记**记录的内容哈希一致，但那条不是 champion（或压根没有
# champion）：身份**可验证**了，只是"批准"缺位。二者必须分开——2026-09-16 定案。
IDENTITY_MATCH_REGISTERED = "match_registered"

IDENTITY_STATUSES = (
    IDENTITY_MATCH,
    IDENTITY_MISMATCH,
    IDENTITY_NO_CHAMPION,
    IDENTITY_CHAMPION_HASH_MISSING,
    IDENTITY_LOADED_HASH_MISSING,
    IDENTITY_REGISTRY_UNAVAILABLE,
    IDENTITY_REGISTRY_BUSY,
    IDENTITY_MATCH_REGISTERED,
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _first_hash_match(
    records: Sequence[Mapping[str, Any]], loaded_hash: str
) -> Mapping[str, Any] | None:
    """按内容哈希在登记记录里找第一条匹配（大小写不敏感；空哈希不参与匹配）。"""
    target = _text(loaded_hash).lower()
    if not target:
        return None
    for record in records:
        if not isinstance(record, Mapping):
            continue
        if _text(record.get("artifact_content_hash")).lower() == target:
            return record
    return None


def content_hash_matches_stamp(*, claimed: object, actual: object) -> bool | None:
    """发布时**盖章**的哈希 vs 加载时按**实际文件**算出的哈希。

    ``None`` 表示无法判定（任一侧为空）——**不得**把"无法判定"读成 True：没有哈希
    不等于哈希一致，这正是 2026-09-05 那条 champion 被 ``quarantine:empty_content_hash``
    拦下的原因。返回 False 才是硬信号：磁盘上的工件与发布时不是同一个。
    """
    claimed_text = _text(claimed).lower()
    actual_text = _text(actual).lower()
    if not claimed_text or not actual_text:
        return None
    return claimed_text == actual_text


def describe_artifact_identity(
    *,
    loaded_uri: object,
    loaded_hash: object,
    champion: Mapping[str, Any] | None = None,
    registered: Sequence[Mapping[str, Any]] = (),
    registry_error: str = "",
    registry_busy: bool = False,
) -> dict[str, object]:
    """判定在服工件与注册表 champion 的身份关系。

    判定顺序（先排除"无法判定"，再判等）：
    1. ``registry_busy`` → ``registry_busy``（本进程的写锁挡着，属"这次读不到"）；
    2. ``registry_error`` 非空 → ``registry_unavailable``（读不到注册表 ≠ 身份不一致）；
    3. 在服工件没算出哈希 → ``loaded_hash_missing``（无法比对，绝不能当成 match）；
    4. ``champion`` 为 None → ``no_champion``；
    5. champion 的 ``artifact_content_hash`` 为空 → ``champion_hash_missing``
       （这正是 2026-09-05 quarantine 把唯一那条 champion 记录拦下的原因）；
    6. 两侧哈希相等 → ``match``，否则 ``mismatch``。

    注意哈希比较**大小写不敏感、去空白**：registry 侧历史上写入过带大写/空白的值，
    格式差异不该被读成"身份不符"（那会制造假警报），但也绝不把空串当相等。
    """
    loaded_hash_text = _text(loaded_hash)
    loaded_uri_text = _text(loaded_uri)
    payload: dict[str, object] = {
        "status": IDENTITY_REGISTRY_UNAVAILABLE,
        "loaded_uri": loaded_uri_text,
        "loaded_content_hash": loaded_hash_text,
        "champion_model_id": "",
        "champion_content_hash": "",
        "detail": "",
    }
    if registry_busy:
        payload["status"] = IDENTITY_REGISTRY_BUSY
        payload["detail"] = "注册表被本进程的写锁占用，这次读不到（属暂时读不到，不是身份不符）" + (
            f": {registry_error}" if registry_error else ""
        )
        return payload
    if registry_error:
        payload["detail"] = f"注册表不可读: {registry_error}"
        return payload
    if not loaded_hash_text:
        payload["status"] = IDENTITY_LOADED_HASH_MISSING
        payload["detail"] = "在服工件未计算内容哈希，身份无法验证"
        return payload
    if champion is None:
        # 身份"可验证"与"已批准"分开：没有 champion 时，仍可能与某条 challenger/trained
        # 记录同哈希——那说明"这就是登记过的那份内容"，只是没人批准它。
        same = _first_hash_match(registered, loaded_hash_text)
        if same is not None:
            payload["status"] = IDENTITY_MATCH_REGISTERED
            payload["champion_model_id"] = _text(same.get("model_id"))
            payload["detail"] = (
                f"在服工件 == 登记记录 {_text(same.get('model_id'))}"
                f"（{_text(same.get('lifecycle_state')) or 'unknown'}），但注册表无 champion"
            )
            return payload
        payload["status"] = IDENTITY_NO_CHAMPION
        payload["detail"] = "注册表没有 champion，在服工件无登记身份"
        return payload

    champion_id = _text(champion.get("model_id"))
    champion_hash = _text(champion.get("artifact_content_hash"))
    payload["champion_model_id"] = champion_id
    payload["champion_content_hash"] = champion_hash
    if not champion_hash:
        payload["status"] = IDENTITY_CHAMPION_HASH_MISSING
        payload["detail"] = f"champion {champion_id or '(unknown)'} 没有内容哈希，身份无法验证"
        return payload
    if champion_hash.lower() == loaded_hash_text.lower():
        payload["status"] = IDENTITY_MATCH
        payload["detail"] = f"在服工件 == champion {champion_id}"
    else:
        payload["status"] = IDENTITY_MISMATCH
        payload["detail"] = (
            f"在服工件与 champion {champion_id} 的内容哈希不同（"
            f"loaded={loaded_hash_text[:12]}… champion={champion_hash[:12]}…）"
        )
    return payload
