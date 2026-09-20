"""配置身份指纹（Alpha V2 P0-00）。

用途：回答"这份配置到底是哪一份"。落点有两处：蓝图 §4.1 ``DecisionIdentity``
的 ``config_hash``，以及 §11.1 每日监控 Identity 块。

算法与 ``pipeline._stable_config_hash`` / ``learning.backfill._stable_config_hash``
逐字节一致（canonical JSON + sha256），差别只在输入：本模块先用**名称级脱敏**
再哈希。两条理由：

1. 需求要求 "config hash 应基于稳定的非敏感配置表示"：把 webhook/token 一起
   哈希会让指纹随密钥轮换而变，本地与 NAS 之间只有密钥不同的同一套行为配置
   会得到不同指纹，基线身份记录因此失去可比性；
2. 哈希是单向摘要，本身不泄漏密钥，但一个"取决于密钥的稳定标识"没有意义。

脱敏只按字段名判定（词段含 token/secret/password/webhook/key 等，或子串含
``*_id`` 类账号标识），不做值匹配：新增凭据字段只要遵守既有命名约定即自动
被覆盖。算法等价性由 ``tests/test_alpha_v2_baseline.py`` 对既有实现做对照验证。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from stock_analyzer.config import StockAnalyzerConfig

# 脱敏后的占位符。用常量而非空串：空串会与"字段本来就没配"混淆，
# 也让 hash 对不同长度的密钥保持稳定。
REDACTION_MARKER = "<redacted>"

# 词段级敏感名（按 "_" 切分后精确匹配）：llm_max_tokens 这类计数不受影响。
_SENSITIVE_NAME_SEGMENTS = frozenset(
    {
        "token",
        "secret",
        "password",
        "passwd",
        "webhook",
        "key",
        "credential",
        "bearer",
    }
)

# 子串级敏感名：账号/会话标识。用**后缀**匹配而非包含匹配，否则
# ``feishu_app_receive_id_type``（行为字段，取值 open_id/chat_id）会被误脱敏。
_SENSITIVE_NAME_SUFFIXES = (
    "chat_id",
    "chat_ids",
    "receive_id",
    "receive_ids",
    "template_id",
    "template_ids",
    "department_id",
    "department_ids",
    "app_id",
    "app_ids",
)


def is_sensitive_config_name(name: str) -> bool:
    """字段名是否属于"凭据/账号标识"（脱敏与敏感项检查共用同一判据）。"""
    lowered = str(name).strip().lower()
    if not lowered:
        return False
    if any(segment in _SENSITIVE_NAME_SEGMENTS for segment in lowered.split("_")):
        return True
    return lowered.endswith(_SENSITIVE_NAME_SUFFIXES)


def stable_payload_hash(payload: Mapping[str, Any]) -> str:
    """canonical JSON + sha256（与既有 ``_stable_config_hash`` 同一配方）。"""
    serialized = json.dumps(
        dict(payload),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def redacted_config_payload(config: StockAnalyzerConfig) -> dict[str, Any]:
    """完整配置的脱敏副本（凭据/账号标识字段值替换为占位符）。"""
    payload = config.model_dump(mode="json")
    redacted = _redact_node(payload)
    if not isinstance(redacted, dict):  # pragma: no cover - 配置根必为映射
        raise TypeError("redacted config payload must be a mapping")
    return redacted


def redacted_config_hash(config: StockAnalyzerConfig) -> str:
    """脱敏配置指纹：密钥轮换不变，行为配置变化必变。"""
    return stable_payload_hash(redacted_config_payload(config))


def _redact_node(node: Any) -> Any:
    if isinstance(node, Mapping):
        return {str(key): _redact_value(key, value) for key, value in node.items()}
    if isinstance(node, list):
        return [_redact_node(item) for item in node]
    return node


def _redact_value(key: object, value: Any) -> Any:
    """敏感名 + 非数值/非布尔值才替换：``enforce_receive_id`` 这类开关保留真值。"""
    if is_sensitive_config_name(str(key)) and not isinstance(value, (bool, int, float)):
        return REDACTION_MARKER
    return _redact_node(value)
