"""Score fusion with champion-bound cache keys and policy rules."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class FusionResult:
    """Fused score output."""

    fused_score: float
    cache_key: str
    from_cache: bool
    base_score: float = 0.0
    applied_rules: tuple[str, ...] = ()
    bonus_raw: float = 0.0
    bonus_capped: float = 0.0
    veto_triggered: bool = False
    veto_module: str = ""
    veto_confidence: float = 0.0


class ScoreFusionEngine:
    """Fuse module scores into one score with champion-bound cache keys."""

    def __init__(
        self,
        default_weights: Mapping[str, float] | None = None,
        *,
        enable_bonus_cap: bool = True,
        bonus_modules: tuple[str, ...] = ("M3", "M7"),
        bonus_neutral_score: float = 50.0,
        bonus_cap: float = 15.0,
        enable_veto: bool = True,
        veto_modules: tuple[str, ...] = ("M1", "M6"),
        veto_score_threshold: float = 55.0,
        veto_score_cap: float = 65.0,
        veto_confidence_gate: float = 0.75,
    ) -> None:
        self._default_weights = {
            key: float(value) for key, value in (default_weights or {}).items()
        }
        self._enable_bonus_cap = bool(enable_bonus_cap)
        self._bonus_modules = tuple(str(item) for item in bonus_modules)
        self._bonus_neutral_score = float(bonus_neutral_score)
        self._bonus_cap = max(0.0, float(bonus_cap))
        self._enable_veto = bool(enable_veto)
        self._veto_modules = tuple(str(item) for item in veto_modules)
        self._veto_score_threshold = float(veto_score_threshold)
        self._veto_score_cap = float(veto_score_cap)
        self._veto_confidence_gate = float(veto_confidence_gate)
        self._cache: dict[str, FusionResult] = {}

    def fuse(
        self,
        module_scores: Mapping[str, float],
        active_champion_id: str,
        version: str = "v1",
        veto_confidence: float | None = None,
    ) -> FusionResult:
        """Fuse input scores and cache by version + champion id.

        Args:
            module_scores: Per-module numeric score mapping.
            active_champion_id: Current active champion identifier.
            version: Fusion version tag.
            veto_confidence: Confidence context for one-vote veto activation.

        Returns:
            A :class:`FusionResult`.

        Raises:
            ValueError: If inputs are invalid.
        """
        if not module_scores:
            raise ValueError("module_scores must not be empty")
        if not active_champion_id.strip():
            raise ValueError("active_champion_id must not be empty")

        normalized_scores = {str(key): float(value) for key, value in module_scores.items()}
        cache_key = self.build_cache_key(
            module_scores=normalized_scores,
            active_champion_id=active_champion_id,
            version=version,
            veto_confidence=veto_confidence,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return replace(cached, from_cache=True)

        total_weight = 0.0
        weighted_sum = 0.0
        for module, score in normalized_scores.items():
            weight = self._default_weights.get(module, 1.0)
            if weight <= 0:
                continue
            total_weight += weight
            weighted_sum += score * weight
        if total_weight <= 0:
            raise ValueError("all effective weights are non-positive")

        base_score = weighted_sum / total_weight
        fused_score = base_score
        applied_rules: list[str] = []

        bonus_raw = 0.0
        bonus_capped = 0.0
        if self._enable_bonus_cap and self._bonus_cap >= 0.0:
            for module in self._bonus_modules:
                bonus_score = normalized_scores.get(module)
                if bonus_score is None:
                    continue
                weight = self._default_weights.get(module, 1.0)
                if weight <= 0:
                    continue
                uplift = max(0.0, bonus_score - self._bonus_neutral_score)
                bonus_raw += uplift * (weight / total_weight)
            bonus_capped = min(bonus_raw, self._bonus_cap)
            if bonus_raw > bonus_capped:
                fused_score = fused_score - bonus_raw + bonus_capped
                applied_rules.append("bonus_cap")
            else:
                bonus_capped = bonus_raw

        veto_conf = float(veto_confidence) if veto_confidence is not None else 0.0
        veto_triggered = False
        veto_module = ""
        if self._enable_veto and veto_conf >= self._veto_confidence_gate:
            for module in self._veto_modules:
                veto_score = normalized_scores.get(module)
                if veto_score is None:
                    continue
                if float(veto_score) <= self._veto_score_threshold:
                    veto_triggered = True
                    veto_module = module
                    if fused_score > self._veto_score_cap:
                        fused_score = self._veto_score_cap
                        applied_rules.append(f"veto:{module}")
                    break

        fused_score = max(0.0, min(100.0, float(fused_score)))
        result = FusionResult(
            fused_score=fused_score,
            cache_key=cache_key,
            from_cache=False,
            base_score=float(base_score),
            applied_rules=tuple(applied_rules),
            bonus_raw=float(bonus_raw),
            bonus_capped=float(bonus_capped),
            veto_triggered=veto_triggered,
            veto_module=veto_module,
            veto_confidence=veto_conf,
        )
        self._cache[cache_key] = result
        return result

    def build_cache_key(
        self,
        module_scores: Mapping[str, float],
        active_champion_id: str,
        version: str = "v1",
        veto_confidence: float | None = None,
    ) -> str:
        """Build a deterministic cache key bound to champion id."""
        confidence = float(veto_confidence) if veto_confidence is not None else None
        payload = {
            "active_champion_id": active_champion_id,
            "module_scores": {key: float(module_scores[key]) for key in sorted(module_scores)},
            "policy": {
                "enable_bonus_cap": self._enable_bonus_cap,
                "bonus_modules": list(self._bonus_modules),
                "bonus_neutral_score": self._bonus_neutral_score,
                "bonus_cap": self._bonus_cap,
                "enable_veto": self._enable_veto,
                "veto_modules": list(self._veto_modules),
                "veto_score_threshold": self._veto_score_threshold,
                "veto_score_cap": self._veto_score_cap,
                "veto_confidence_gate": self._veto_confidence_gate,
                "veto_confidence": confidence,
            },
            "version": version,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        return f"{version}:{active_champion_id}:{digest}"

    def invalidate_champion(self, active_champion_id: str) -> int:
        """Invalidate cache entries for one champion id.

        Args:
            active_champion_id: Champion id to invalidate.

        Returns:
            Number of removed cache keys.
        """
        prefix = f":{active_champion_id}:"
        to_delete = [key for key in self._cache if prefix in key]
        for key in to_delete:
            del self._cache[key]
        return len(to_delete)

    def clear(self) -> None:
        """Clear all cache entries."""
        self._cache.clear()
