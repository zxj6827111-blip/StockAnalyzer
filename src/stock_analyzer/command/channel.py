"""Instruction channel with signature verification and idempotency."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from stock_analyzer.config import CommandChannelConfig
from stock_analyzer.infra.cache import CacheStore


@dataclass(slots=True)
class RuntimeState:
    current_equity: float = 1.0
    watchlist: list[str] = field(default_factory=list)
    pause_new_buy: bool = False
    reconcile_required: bool = False


@dataclass(slots=True)
class CommandEnvelope:
    command_id: str
    timestamp: int
    action: str
    payload: dict[str, Any]
    signature: str


@dataclass(slots=True)
class CommandExecutionResult:
    accepted: bool
    code: str
    message: str
    state: RuntimeState

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["state"] = asdict(self.state)
        return payload


class SignedCommandProcessor:
    """Validate signed commands and apply deterministic state transitions."""

    def __init__(
        self, config: CommandChannelConfig, cache: CacheStore, state: RuntimeState
    ) -> None:
        self._config = config
        self._cache = cache
        self._state = state

    @staticmethod
    def build_signature(
        secret_key: str,
        command_id: str,
        timestamp: int,
        action: str,
        payload: dict[str, Any],
    ) -> str:
        payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        base = f"{command_id}|{timestamp}|{action}|{payload_json}".encode()
        return hmac.new(secret_key.encode("utf-8"), base, digestmod=hashlib.sha256).hexdigest()

    def execute(self, command: CommandEnvelope) -> CommandExecutionResult:
        if not self._config.enabled:
            return CommandExecutionResult(
                accepted=False,
                code="disabled",
                message="command channel disabled",
                state=self._state,
            )

        if not self._verify_signature(command):
            return CommandExecutionResult(
                accepted=False,
                code="bad_signature",
                message="signature mismatch",
                state=self._state,
            )

        now_ts = int(time.time())
        if abs(now_ts - command.timestamp) > self._config.max_clock_skew_sec:
            return CommandExecutionResult(
                accepted=False,
                code="timestamp_skew",
                message="command timestamp out of range",
                state=self._state,
            )

        dedup_key = f"cmd:{command.command_id}"
        if self._cache.exists(dedup_key):
            return CommandExecutionResult(
                accepted=False,
                code="duplicate",
                message="duplicate command_id",
                state=self._state,
            )

        apply_result = self._apply(command.action.upper(), command.payload)
        if apply_result.accepted:
            self._cache.set(dedup_key, "1", ttl_sec=self._config.dedup_ttl_sec)
        return apply_result

    def state_snapshot(self) -> RuntimeState:
        return self._state

    def _verify_signature(self, command: CommandEnvelope) -> bool:
        expected = self.build_signature(
            secret_key=self._config.secret_key,
            command_id=command.command_id,
            timestamp=command.timestamp,
            action=command.action,
            payload=command.payload,
        )
        return hmac.compare_digest(expected, command.signature)

    def _apply(self, action: str, payload: dict[str, Any]) -> CommandExecutionResult:
        if action == "PAUSE_NEW_BUY":
            self._state.pause_new_buy = True
            return CommandExecutionResult(
                accepted=True,
                code="ok",
                message="new buy paused",
                state=self._state,
            )

        if action == "RESUME_NEW_BUY":
            self._state.pause_new_buy = False
            return CommandExecutionResult(
                accepted=True,
                code="ok",
                message="new buy resumed",
                state=self._state,
            )

        if action == "SET_EQUITY":
            value = float(payload.get("current_equity", 0.0))
            if value <= 0:
                return CommandExecutionResult(
                    accepted=False,
                    code="bad_payload",
                    message="current_equity must be > 0",
                    state=self._state,
                )
            self._state.current_equity = value
            return CommandExecutionResult(
                accepted=True,
                code="ok",
                message="equity updated",
                state=self._state,
            )

        if action == "ADD_SYMBOL":
            symbol = str(payload.get("symbol", "")).strip()
            if not symbol:
                return CommandExecutionResult(
                    accepted=False,
                    code="bad_payload",
                    message="symbol is required",
                    state=self._state,
                )
            if symbol not in self._state.watchlist:
                self._state.watchlist.append(symbol)
            return CommandExecutionResult(
                accepted=True,
                code="ok",
                message="symbol added",
                state=self._state,
            )

        if action == "REMOVE_SYMBOL":
            symbol = str(payload.get("symbol", "")).strip()
            if not symbol:
                return CommandExecutionResult(
                    accepted=False,
                    code="bad_payload",
                    message="symbol is required",
                    state=self._state,
                )
            self._state.watchlist = [item for item in self._state.watchlist if item != symbol]
            return CommandExecutionResult(
                accepted=True,
                code="ok",
                message="symbol removed",
                state=self._state,
            )

        if action == "SET_WATCHLIST":
            symbols = payload.get("symbols")
            if not isinstance(symbols, list):
                return CommandExecutionResult(
                    accepted=False,
                    code="bad_payload",
                    message="symbols must be list[str]",
                    state=self._state,
                )
            normalized = [str(item).strip() for item in symbols if str(item).strip()]
            if not normalized:
                return CommandExecutionResult(
                    accepted=False,
                    code="bad_payload",
                    message="watchlist cannot be empty",
                    state=self._state,
                )
            self._state.watchlist = normalized
            return CommandExecutionResult(
                accepted=True,
                code="ok",
                message="watchlist replaced",
                state=self._state,
            )

        if action == "CLOSE_POSITION":
            symbol = str(payload.get("symbol", "")).strip()
            if not symbol:
                return CommandExecutionResult(
                    accepted=False,
                    code="bad_payload",
                    message="symbol is required",
                    state=self._state,
                )
            return CommandExecutionResult(
                accepted=True,
                code="ok",
                message="close position accepted",
                state=self._state,
            )

        if action == "CLOSE_ALL_POSITIONS":
            return CommandExecutionResult(
                accepted=True,
                code="ok",
                message="close all positions accepted",
                state=self._state,
            )

        if action == "SET_POSITION":
            symbol = str(payload.get("symbol", "")).strip()
            target_position = float(payload.get("target_position", -1.0))
            if not symbol:
                return CommandExecutionResult(
                    accepted=False,
                    code="bad_payload",
                    message="symbol is required",
                    state=self._state,
                )
            if target_position <= 0.0:
                return CommandExecutionResult(
                    accepted=False,
                    code="bad_payload",
                    message="target_position must be > 0",
                    state=self._state,
                )
            return CommandExecutionResult(
                accepted=True,
                code="ok",
                message="set position accepted",
                state=self._state,
            )

        if action == "SET_BROKER_POSITIONS":
            positions = payload.get("positions")
            if not isinstance(positions, list):
                return CommandExecutionResult(
                    accepted=False,
                    code="bad_payload",
                    message="positions must be list[object]",
                    state=self._state,
                )
            for item in positions:
                if not isinstance(item, dict):
                    return CommandExecutionResult(
                        accepted=False,
                        code="bad_payload",
                        message="positions item must be object",
                        state=self._state,
                    )
                symbol = str(item.get("symbol", "")).strip()
                target_position = float(item.get("target_position", -1.0))
                if not symbol:
                    return CommandExecutionResult(
                        accepted=False,
                        code="bad_payload",
                        message="positions item symbol is required",
                        state=self._state,
                    )
                if target_position < 0.0:
                    return CommandExecutionResult(
                        accepted=False,
                        code="bad_payload",
                        message="positions item target_position must be >= 0",
                        state=self._state,
                    )
            return CommandExecutionResult(
                accepted=True,
                code="ok",
                message="broker positions accepted",
                state=self._state,
            )

        if action == "RUN_RECONCILE":
            return CommandExecutionResult(
                accepted=True,
                code="ok",
                message="reconcile requested",
                state=self._state,
            )

        if action == "ACK_RECONCILE":
            self._state.reconcile_required = False
            return CommandExecutionResult(
                accepted=True,
                code="ok",
                message="reconcile acknowledged",
                state=self._state,
            )

        if action == "SET_RECOMMENDATION_STATUS":
            symbol = str(payload.get("symbol", "")).strip()
            status = str(payload.get("status", "")).strip().lower()
            if not symbol:
                return CommandExecutionResult(
                    accepted=False,
                    code="bad_payload",
                    message="symbol is required",
                    state=self._state,
                )
            if status not in {"recommended", "bought", "watching", "dropped"}:
                return CommandExecutionResult(
                    accepted=False,
                    code="bad_payload",
                    message="status must be one of recommended/bought/watching/dropped",
                    state=self._state,
                )
            return CommandExecutionResult(
                accepted=True,
                code="ok",
                message="recommendation status accepted",
                state=self._state,
            )

        return CommandExecutionResult(
            accepted=False,
            code="unknown_action",
            message=f"unsupported action:{action}",
            state=self._state,
        )
