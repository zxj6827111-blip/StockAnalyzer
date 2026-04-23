"""M8 six-gate memory bridge that retrieves similar patterns from M3."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
from stock_analyzer.evolution.m3_vector_profile import (
    build_default_m3_vector_profile,
    build_m3_vector_from_record,
)

_M8_GATE_NAMES = (
    "pcv",
    "deflated_sharpe_fdr",
    "llm_semantic",
    "noise_injection",
    "random_walk",
    "registry",
)


@dataclass(frozen=True, slots=True)
class M8GateCheck:
    """One M8 gate-check decision."""

    name: str
    passed: bool
    value: float
    threshold: float
    detail: str
    provenance: str = "computed"

@dataclass(frozen=True, slots=True)
class M8Suggestion:
    """One M8 suggestion item resolved from M3 retrieval."""

    symbol: str
    recommendation: str
    best_similarity: float
    indices: list[int]
    scores: list[float]
    total_vectors: int
    vector_profile_id: str = ""
    passed_gates: int = 0
    gate_total: int = 0
    failed_gates: list[str] = field(default_factory=list)
    missing_gate_inputs: list[str] = field(default_factory=list)
    derived_gate_inputs: list[str] = field(default_factory=list)
    registry_signature: str = ""
    gate_checks: list[M8GateCheck] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class M8SuggestionResult:
    """M8 batch suggestion result."""

    score: float
    top_k: int
    promoted: int
    review: int
    novel: int
    invalid: int
    suggestions: list[M8Suggestion]
    gate_pass_rate: float
    gate_failure_counts: dict[str, int]
    gate_provenance_counts: dict[str, dict[str, int]]
    gate_names: list[str]
    strict_gate_inputs: bool


SearchFn = Callable[[list[float], int], Mapping[str, object]]
QueryVectorBuilder = Callable[[Mapping[str, object]], list[float] | None]


def run_m8_memory_bridge(
    candidates: Sequence[Mapping[str, object]],
    search_fn: SearchFn,
    top_k: int = 5,
    promote_similarity: float = 0.80,
    review_similarity: float = 0.55,
    min_gate_passes_for_review: int = 4,
    pcv_min_score: float = 0.55,
    deflated_sharpe_min: float = 0.10,
    fdr_alpha: float = 0.10,
    llm_min_confidence: float = 0.55,
    noise_stability_min: float = 0.60,
    noise_trials: int = 3,
    noise_sigma: float = 0.01,
    random_walk_trials: int = 16,
    random_walk_max_pvalue: float = 0.35,
    registry_blocked_signatures: Sequence[str] = (),
    registry_dedupe_within_run: bool = True,
    allow_similarity_proxies: bool = True,
    strict_gate_inputs: bool = False,
    random_seed: int = 20260301,
    query_vector_builder: QueryVectorBuilder | None = None,
) -> M8SuggestionResult:
    """Run M8 memory retrieval with six-gate recommendation mapping.

    Args:
        candidates: Candidate records, each requiring symbol/open/high/low/close/volume.
        search_fn: Search callback that calls M3 and returns indices/scores.
        top_k: Nearest-neighbor count.
        promote_similarity: Threshold to mark candidate as ``promote``.
        review_similarity: Threshold to mark candidate as ``review``.
        min_gate_passes_for_review: Minimum gate passes to qualify for ``review``.
        pcv_min_score: Gate-1 minimum PCV proxy score.
        deflated_sharpe_min: Gate-2 minimum deflated Sharpe proxy.
        fdr_alpha: Gate-2 FDR alpha threshold.
        llm_min_confidence: Gate-3 minimum LLM confidence.
        noise_stability_min: Gate-4 minimum noise stability score.
        noise_trials: Gate-4 perturbation trial count.
        noise_sigma: Gate-4 perturbation sigma.
        random_walk_trials: Gate-5 random-walk trial count.
        random_walk_max_pvalue: Gate-5 max allowed random-walk p-value.
        registry_blocked_signatures: Gate-6 blocked signatures.
        registry_dedupe_within_run: Gate-6 in-run dedupe switch.
        allow_similarity_proxies: Whether missing Gate-1/2/3 inputs may use
            conservative similarity-derived proxies.
        strict_gate_inputs: Whether missing Gate-1/2/3 inputs should fail gates
            directly instead of using derived proxies.
        random_seed: Deterministic gate simulation seed.

    Returns:
        Structured M8 result with score and itemized suggestions.
    """
    if top_k <= 0:
        raise ValueError("top_k must be > 0")
    if promote_similarity < review_similarity:
        raise ValueError("promote_similarity must be >= review_similarity")
    if min_gate_passes_for_review <= 0:
        raise ValueError("min_gate_passes_for_review must be > 0")
    if min_gate_passes_for_review > len(_M8_GATE_NAMES):
        raise ValueError("min_gate_passes_for_review exceeds six-gate count")
    if not 0.0 <= pcv_min_score <= 1.0:
        raise ValueError("pcv_min_score must be in [0, 1]")
    if not 0.0 <= fdr_alpha <= 1.0:
        raise ValueError("fdr_alpha must be in [0, 1]")
    if not 0.0 <= llm_min_confidence <= 1.0:
        raise ValueError("llm_min_confidence must be in [0, 1]")
    if not 0.0 <= noise_stability_min <= 1.0:
        raise ValueError("noise_stability_min must be in [0, 1]")
    if noise_trials <= 0:
        raise ValueError("noise_trials must be > 0")
    if noise_sigma <= 0.0:
        raise ValueError("noise_sigma must be > 0")
    if random_walk_trials <= 0:
        raise ValueError("random_walk_trials must be > 0")
    if not 0.0 <= random_walk_max_pvalue <= 1.0:
        raise ValueError("random_walk_max_pvalue must be in [0, 1]")

    promoted = 0
    review = 0
    novel = 0
    invalid = 0
    suggestions: list[M8Suggestion] = []
    gate_failure_counts: dict[str, int] = {name: 0 for name in _M8_GATE_NAMES}
    gate_provenance_counts: dict[str, dict[str, int]] = {
        name: {"provided": 0, "derived": 0, "missing": 0, "computed": 0}
        for name in _M8_GATE_NAMES
    }
    seen_registry: set[str] = set()
    blocked_registry = {
        str(item).strip() for item in registry_blocked_signatures if str(item).strip()
    }

    for idx, candidate in enumerate(candidates):
        symbol = str(candidate.get("symbol", "UNKNOWN"))
        vector_builder = query_vector_builder or build_m8_query_vector
        vector = vector_builder(candidate)
        if vector is None:
            invalid += 1
            suggestions.append(
                M8Suggestion(
                    symbol=symbol,
                    recommendation="invalid",
                    best_similarity=0.0,
                    indices=[],
                    scores=[],
                    total_vectors=0,
                    passed_gates=0,
                    gate_total=0,
                    failed_gates=[],
                    missing_gate_inputs=[],
                    derived_gate_inputs=[],
                    registry_signature="",
                    gate_checks=[],
                )
            )
            continue

        payload = search_fn(vector, top_k)
        indices = _as_int_list(payload.get("indices"))
        scores = _as_float_list(payload.get("scores"))
        total_vectors = _as_int(payload.get("total_vectors"), default=0)
        vector_profile_id = str(payload.get("vector_profile_id", ""))
        best_similarity = scores[0] if scores else 0.0

        gate_checks, registry_signature = _evaluate_six_gates(
            candidate=candidate,
            idx=idx,
            vector=vector,
            best_similarity=best_similarity,
            search_fn=search_fn,
            top_k=top_k,
            promote_similarity=promote_similarity,
            review_similarity=review_similarity,
            pcv_min_score=pcv_min_score,
            deflated_sharpe_min=deflated_sharpe_min,
            fdr_alpha=fdr_alpha,
            llm_min_confidence=llm_min_confidence,
            noise_stability_min=noise_stability_min,
            noise_trials=noise_trials,
            noise_sigma=noise_sigma,
            random_walk_trials=random_walk_trials,
            random_walk_max_pvalue=random_walk_max_pvalue,
            blocked_registry=blocked_registry,
            seen_registry=seen_registry,
            registry_dedupe_within_run=registry_dedupe_within_run,
            allow_similarity_proxies=allow_similarity_proxies,
            strict_gate_inputs=strict_gate_inputs,
            random_seed=random_seed,
        )
        failed_gates = [item.name for item in gate_checks if not item.passed]
        missing_gate_inputs = [item.name for item in gate_checks if item.provenance == "missing"]
        derived_gate_inputs = [item.name for item in gate_checks if item.provenance == "derived"]
        for name in failed_gates:
            gate_failure_counts[name] = gate_failure_counts.get(name, 0) + 1
        for item in gate_checks:
            gate_key = item.name
            if gate_key not in gate_provenance_counts:
                gate_provenance_counts[gate_key] = {
                    "provided": 0,
                    "derived": 0,
                    "missing": 0,
                    "computed": 0,
                }
            counts = gate_provenance_counts[gate_key]
            provenance = item.provenance if item.provenance in counts else "computed"
            counts[provenance] += 1
        passed_gates = len(gate_checks) - len(failed_gates)

        if passed_gates == len(_M8_GATE_NAMES) and best_similarity >= promote_similarity:
            recommendation = "promote"
            promoted += 1
        elif (
            passed_gates >= min_gate_passes_for_review and best_similarity >= review_similarity
        ):
            recommendation = "review"
            review += 1
        else:
            recommendation = "novel"
            novel += 1

        suggestions.append(
            M8Suggestion(
                symbol=symbol,
                recommendation=recommendation,
                best_similarity=best_similarity,
                indices=indices,
                scores=scores,
                total_vectors=total_vectors,
                vector_profile_id=vector_profile_id,
                passed_gates=passed_gates,
                gate_total=len(_M8_GATE_NAMES),
                failed_gates=failed_gates,
                missing_gate_inputs=missing_gate_inputs,
                derived_gate_inputs=derived_gate_inputs,
                registry_signature=registry_signature,
                gate_checks=gate_checks,
            )
        )

    valid = max(1, len(candidates) - invalid)
    score = max(0.0, min(100.0, ((promoted + 0.5 * review) / valid) * 100.0))
    gate_total_checks = sum(item.gate_total for item in suggestions)
    gate_passes = sum(item.passed_gates for item in suggestions)
    gate_pass_rate = gate_passes / max(gate_total_checks, 1)
    return M8SuggestionResult(
        score=score,
        top_k=top_k,
        promoted=promoted,
        review=review,
        novel=novel,
        invalid=invalid,
        suggestions=suggestions,
        gate_pass_rate=gate_pass_rate,
        gate_failure_counts=gate_failure_counts,
        gate_provenance_counts=gate_provenance_counts,
        gate_names=list(_M8_GATE_NAMES),
        strict_gate_inputs=strict_gate_inputs,
    )


def _evaluate_six_gates(
    candidate: Mapping[str, object],
    idx: int,
    vector: list[float],
    best_similarity: float,
    search_fn: SearchFn,
    top_k: int,
    promote_similarity: float,
    review_similarity: float,
    pcv_min_score: float,
    deflated_sharpe_min: float,
    fdr_alpha: float,
    llm_min_confidence: float,
    noise_stability_min: float,
    noise_trials: int,
    noise_sigma: float,
    random_walk_trials: int,
    random_walk_max_pvalue: float,
    blocked_registry: set[str],
    seen_registry: set[str],
    registry_dedupe_within_run: bool,
    allow_similarity_proxies: bool,
    strict_gate_inputs: bool,
    random_seed: int,
) -> tuple[list[M8GateCheck], str]:
    gate_checks: list[M8GateCheck] = []

    # Gate-1: PCV gate.
    pcv_score = _as_probability(candidate.get("pcv_score"))
    pcv_provenance = "provided"
    if pcv_score is None:
        if strict_gate_inputs or not allow_similarity_proxies:
            pcv_score = 0.0
            pcv_provenance = "missing"
            pcv_detail = "missing_pcv_score"
        else:
            pcv_score = _clamp01(best_similarity * 0.85)
            pcv_provenance = "derived"
            pcv_detail = "derived_from_similarity_proxy"
    else:
        pcv_detail = "candidate_pcv_score"
    gate_checks.append(
        M8GateCheck(
            name="pcv",
            passed=pcv_provenance != "missing" and pcv_score >= pcv_min_score,
            value=pcv_score,
            threshold=pcv_min_score,
            detail=pcv_detail,
            provenance=pcv_provenance,
        )
    )

    # Gate-2: Deflated-Sharpe + FDR gate.
    deflated_sharpe = _as_float_or_none(candidate.get("deflated_sharpe"))
    fdr_p_value = _as_probability(candidate.get("fdr_p_value"))
    sharpe_fdr_provenance = "provided"
    missing_sharpe_fdr_inputs: list[str] = []
    if deflated_sharpe is None:
        missing_sharpe_fdr_inputs.append("deflated_sharpe")
    if fdr_p_value is None:
        missing_sharpe_fdr_inputs.append("fdr_p_value")

    if missing_sharpe_fdr_inputs:
        if strict_gate_inputs or not allow_similarity_proxies:
            sharpe_fdr_score = 0.0
            sharpe_fdr_provenance = "missing"
            sharpe_fdr_detail = "missing=" + ",".join(missing_sharpe_fdr_inputs)
            gate_checks.append(
                M8GateCheck(
                    name="deflated_sharpe_fdr",
                    passed=False,
                    value=sharpe_fdr_score,
                    threshold=1.0,
                    detail=sharpe_fdr_detail,
                    provenance=sharpe_fdr_provenance,
                )
            )
        else:
            sharpe_fdr_provenance = "derived"
            safe_deflated = (
                deflated_sharpe
                if deflated_sharpe is not None
                else max(0.0, (best_similarity - review_similarity) * 0.45)
            )
            safe_fdr = (
                fdr_p_value
                if fdr_p_value is not None
                else _clamp01(0.65 + (1.0 - best_similarity) * 0.35)
            )
            sharpe_ratio = safe_deflated / max(deflated_sharpe_min, 1e-9)
            fdr_ratio = (fdr_alpha + 1e-9) / max(safe_fdr, 1e-9)
            sharpe_fdr_score = min(sharpe_ratio, fdr_ratio)
            gate_checks.append(
                M8GateCheck(
                    name="deflated_sharpe_fdr",
                    passed=sharpe_fdr_score >= 1.0,
                    value=sharpe_fdr_score,
                    threshold=1.0,
                    detail=(
                        f"sharpe={safe_deflated:.4f},fdr_p={safe_fdr:.4f};"
                        f"missing={','.join(missing_sharpe_fdr_inputs)}"
                    ),
                    provenance=sharpe_fdr_provenance,
                )
            )
    else:
        assert deflated_sharpe is not None
        assert fdr_p_value is not None
        sharpe_ratio = deflated_sharpe / max(deflated_sharpe_min, 1e-9)
        fdr_ratio = (fdr_alpha + 1e-9) / max(fdr_p_value, 1e-9)
        sharpe_fdr_score = min(sharpe_ratio, fdr_ratio)
        gate_checks.append(
            M8GateCheck(
                name="deflated_sharpe_fdr",
                passed=sharpe_fdr_score >= 1.0,
                value=sharpe_fdr_score,
                threshold=1.0,
                detail=f"sharpe={deflated_sharpe:.4f},fdr_p={fdr_p_value:.4f}",
                provenance=sharpe_fdr_provenance,
            )
        )

    # Gate-3: LLM semantic gate.
    raw_llm_verdict_value = candidate.get("llm_verdict")
    llm_confidence = _as_probability(candidate.get("llm_confidence"))
    llm_provenance = "provided"
    if not isinstance(raw_llm_verdict_value, str) or not raw_llm_verdict_value.strip():
        if strict_gate_inputs or not allow_similarity_proxies:
            llm_provenance = "missing"
            raw_llm_verdict = "missing"
        else:
            llm_provenance = "derived"
            raw_llm_verdict = "review"
    else:
        raw_llm_verdict = raw_llm_verdict_value
    if llm_confidence is None:
        if strict_gate_inputs or not allow_similarity_proxies:
            llm_provenance = "missing"
            llm_confidence = 0.0
        else:
            llm_provenance = "derived"
            llm_confidence = _clamp01(max(0.35, min(0.75, best_similarity * 0.8)))
    normalized_verdict = str(raw_llm_verdict).strip().lower()
    llm_verdict_score = _llm_verdict_score(normalized_verdict)
    llm_gate_value = llm_verdict_score * llm_confidence
    gate_checks.append(
        M8GateCheck(
            name="llm_semantic",
            passed=(
                llm_provenance != "missing"
                and llm_verdict_score >= 0.5
                and llm_gate_value >= llm_min_confidence
            ),
            value=llm_gate_value,
            threshold=llm_min_confidence,
            detail=f"verdict={normalized_verdict},confidence={llm_confidence:.4f}",
            provenance=llm_provenance,
        )
    )

    # Gate-4: Noise-injection robustness gate.
    noise_rng = np.random.default_rng(random_seed + idx * 193 + 1)
    noisy_scores: list[float] = []
    for _ in range(noise_trials):
        noise = noise_rng.normal(0.0, noise_sigma, size=len(vector))
        noisy_vector = [
            max(0.0, float(vector[pos]) * (1.0 + float(noise[pos])))
            for pos in range(len(vector))
        ]
        noisy_payload = search_fn(noisy_vector, top_k)
        noisy_values = _as_float_list(noisy_payload.get("scores"))
        noisy_scores.append(noisy_values[0] if noisy_values else 0.0)
    mean_delta = float(
        np.mean(np.asarray([abs(item - best_similarity) for item in noisy_scores], dtype=float))
    )
    stability = _clamp01(1.0 - mean_delta / max(0.2, best_similarity, 1e-6))
    gate_checks.append(
        M8GateCheck(
            name="noise_injection",
            passed=stability >= noise_stability_min,
            value=stability,
            threshold=noise_stability_min,
            detail=f"mean_delta={mean_delta:.4f}",
            provenance="computed",
        )
    )

    # Gate-5: Random-walk false-positive gate.
    walk_rng = np.random.default_rng(random_seed + idx * 193 + 2)
    random_baseline = walk_rng.uniform(0.0, 1.0, size=random_walk_trials)
    random_hits = int(np.sum(random_baseline >= _clamp01(best_similarity)))
    random_walk_pvalue = (random_hits + 1) / (random_walk_trials + 1)
    gate_checks.append(
        M8GateCheck(
            name="random_walk",
            passed=random_walk_pvalue <= random_walk_max_pvalue,
            value=random_walk_pvalue,
            threshold=random_walk_max_pvalue,
            detail=f"hits={random_hits}/{random_walk_trials}",
            provenance="computed",
        )
    )

    # Gate-6: Registry and dedupe gate.
    explicit_signature = candidate.get("factor_signature")
    if isinstance(explicit_signature, str) and explicit_signature.strip():
        signature = explicit_signature.strip()
    else:
        symbol = str(candidate.get("symbol", "UNKNOWN")).strip().upper()
        rounded = [f"{_clamp01(best_similarity):.4f}"] + [f"{item:.3f}" for item in vector[:3]]
        signature = f"{symbol}:{'|'.join(rounded)}"
    blocked = signature in blocked_registry
    duplicated = registry_dedupe_within_run and signature in seen_registry
    registry_passed = not blocked and not duplicated
    if registry_passed and registry_dedupe_within_run:
        seen_registry.add(signature)
    gate_checks.append(
        M8GateCheck(
            name="registry",
            passed=registry_passed,
            value=1.0 if registry_passed else 0.0,
            threshold=0.5,
            detail=("blocked" if blocked else "duplicated" if duplicated else "ok"),
            provenance="computed",
        )
    )

    # Slightly dampen derived gate values when similarity is very weak.
    if best_similarity < review_similarity * 0.5:
        adjusted: list[M8GateCheck] = []
        for item in gate_checks:
            if (
                item.name in {"pcv", "deflated_sharpe_fdr", "noise_injection"}
                and item.provenance in {"derived", "computed"}
            ):
                value = item.value * 0.8
                adjusted.append(
                    M8GateCheck(
                        name=item.name,
                        passed=value >= item.threshold and item.passed,
                        value=value,
                        threshold=item.threshold,
                        detail=f"{item.detail};weak_similarity_adjusted",
                        provenance=item.provenance,
                    )
                )
            else:
                adjusted.append(item)
        gate_checks = adjusted

    return gate_checks, signature


def build_m8_query_vector(candidate: Mapping[str, object]) -> list[float] | None:
    """Build one M3-compatible query vector from candidate features.

    Args:
        candidate: Candidate mapping with OHLCV-like fields.

    Returns:
        Query vector aligned to the active default M3 profile.
        Returns ``None`` if close is invalid.
    """
    return build_m3_vector_from_record(
        candidate,
        vector_profile=build_default_m3_vector_profile(),
        regime_state="range",
    )


def _as_float(value: object, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_int(value: object, default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _as_int_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        if isinstance(item, int):
            result.append(item)
        elif isinstance(item, float):
            result.append(int(item))
    return result


def _as_float_list(value: object) -> list[float]:
    if not isinstance(value, list):
        return []
    result: list[float] = []
    for item in value:
        if isinstance(item, (int, float)):
            result.append(float(item))
    return result


def _as_float_or_none(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_probability(value: object) -> float | None:
    parsed = _as_float_or_none(value)
    if parsed is None:
        return None
    return _clamp01(parsed)


def _llm_verdict_score(verdict: str) -> float:
    lowered = verdict.strip().lower()
    if lowered in {"approve", "pass", "support", "positive"}:
        return 1.0
    if lowered in {"review", "neutral", "uncertain"}:
        return 0.5
    if lowered in {"reject", "block", "negative", "noise"}:
        return 0.0
    return 0.5


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))
