from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from mathematical_engine.capacity import CPU_SAFE_LIMIT, MEMORY_SAFE_LIMIT
from mathematical_engine.slo import analyze_slo, SLOThresholds


class AblationError(ValueError):
    """Bad input to this module."""


# ==========================================================================
# What this module does
# ==========================================================================
#
# Answers RQ5 with numbers, not a claim: "does combining runtime
# monitoring with analytical models actually produce better capacity/
# bottleneck answers than runtime metrics alone?" Four configurations,
# increasingly sophisticated, are all asked the same two questions -
# "how many users is safe?" and "which resource is the bottleneck?" -
# against the SAME real ground-truth data (the actually-measured 600+
# validation levels from prediction_validation.py's Phase B), and scored
# against it:
#
#   A. Runtime only        - linear extrapolation of whichever raw
#                             resource % is currently highest, no
#                             modeling at all. What a basic monitoring
#                             dashboard's "you're at 70% CPU" reasoning
#                             produces.
#   B. + Little's Law/Queueing - adds queueing-theoretic capacity
#                             reasoning (extrapolating utilization rho),
#                             still no USL fit, still no service-demand
#                             bottleneck attribution.
#   C. + USL/Bottleneck/Amdahl - the full existing pipeline as built:
#                             capacity.py's USL-fitted safe_users,
#                             bottleneck.py's Utilization-Law-based
#                             resource attribution.
#   D. + Forced Flow        - Config C, but bottleneck.py's resources
#                             also include component-level (database/
#                             cache/API) demand via forced_flow.py. Only
#                             differs from C when a component genuinely
#                             is the bottleneck; otherwise degrades to
#                             being identical to C - itself a meaningful
#                             ablation result (component instrumentation
#                             not available, or not the constraint), not
#                             a failure to differentiate.
#
# Configs A and B are deliberately real, if simple, reasoning - not
# straw men. They reuse the same safety-limit constants
# (CPU_SAFE_LIMIT/MEMORY_SAFE_LIMIT) capacity.py itself uses, so the
# comparison is "different sophistication of reasoning toward the same
# target", not "different arbitrary thresholds".
#
# Honesty note on ground truth: there is no true oracle for "which
# resource is really the bottleneck" outside a purpose-built synthetic
# benchmark app with a deliberately injected, known bottleneck. This
# module's ground-truth bottleneck is a heuristic proxy - whichever raw
# resource metric was highest at the highest actually-tested load level
# - not a certainty. It is a reasonable, defensible proxy, and it is
# labeled as one throughout rather than asserted as ground truth with
# full confidence.


_RAW_RESOURCE_FIELDS = ("cpu_usage", "memory_usage", "disk_io", "network_io")
_RESOURCE_NAME_NORMALIZATION = {
    "cpu_usage": "cpu", "memory_usage": "memory", "disk_io": "disk", "network_io": "network",
}
_QUEUE_SAFE_UTILIZATION = 0.85  # matches queueing.py's own StabilityThresholds.stable_max default


def _naive_bottleneck(runtime: Dict[str, Any], component_metrics: Optional[Dict[str, float]] = None) -> Optional[str]:
    """
    The naive 'just look at the biggest percentage' bottleneck guess -
    deliberately the approach the rest of this pipeline's Utilization-
    Law/Forced-Flow service-demand analysis (bottleneck.py) was built to
    improve on, kept here specifically so Configs A/B have something
    real to be compared against.

    component_metrics (optional) lets a raw pressure reading for a
    component like a database - e.g. db_monitor.py's
    connection_pool_utilization_avg_pct - compete on equal footing with
    cpu/memory/disk/network. This matters for more than fairness to
    Configs A/B: without it, the ground-truth heuristic below could
    never say "database" at all, which would make it structurally
    impossible for Config D to ever be credited as correct even when a
    database genuinely is the real bottleneck - only able to fall back
    to whichever OS-level resource happened to be elevated as a
    downstream symptom. Seeing the same raw number isn't the same as
    Config C/D's demand-based REASONING about it, though - a basic
    dashboard showing "DB pool: 95%" still can't tell you whether that's
    actually the binding constraint versus an even-higher but less
    meaningful CPU reading; that weighing is exactly what Utilization
    Law / Forced Flow are for.
    """
    candidates = {
        name: runtime[name] for name in _RAW_RESOURCE_FIELDS
        if isinstance(runtime.get(name), (int, float)) and not isinstance(runtime.get(name), bool)
    }
    if component_metrics:
        candidates.update({
            name: value for name, value in component_metrics.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        })
    if not candidates:
        return None
    return max(candidates, key=candidates.get)


def _normalize_resource_name(name: Optional[str]) -> Optional[str]:
    if name is None:
        return None
    base = str(name).split("/")[0].strip().lower()
    return _RESOURCE_NAME_NORMALIZATION.get(base, base)


def _bottleneck_matches(predicted: Optional[str], actual: Optional[str]) -> Optional[bool]:
    if predicted is None or actual is None:
        return None
    return _normalize_resource_name(predicted) == _normalize_resource_name(actual)


def _percent_error(predicted: Optional[float], actual: Optional[float]) -> Optional[float]:
    if predicted is None or actual is None or actual == 0:
        return None
    return round(abs(predicted - actual) / actual * 100.0, 2)


# --- Per-configuration estimators ---

def _config_a_runtime_only(runtime: Dict[str, Any], component_metrics: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    current_users = runtime.get("current_users")
    cpu = runtime.get("cpu_usage")
    memory = runtime.get("memory_usage")

    safe_users = None
    limiting_values = [v for v in (cpu, memory) if isinstance(v, (int, float)) and v > 0]
    if current_users and limiting_values:
        limiting = max(limiting_values)
        safe_limit = min(CPU_SAFE_LIMIT, MEMORY_SAFE_LIMIT)
        safe_users = current_users * (safe_limit / limiting)

    return {
        "config": "A_runtime_only",
        "description": "Raw runtime metrics only - linear extrapolation of the highest observed resource %, no modeling.",
        "safe_users": round(safe_users, 2) if safe_users is not None else None,
        "bottleneck": _naive_bottleneck(runtime, component_metrics),
    }


def _config_b_plus_queueing(
    runtime: Dict[str, Any],
    queueing_results: Optional[Dict[str, Any]],
    component_metrics: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    current_users = runtime.get("current_users")
    rho = queueing_results.get("utilization") if isinstance(queueing_results, dict) else None

    safe_users = None
    if current_users and isinstance(rho, (int, float)) and rho > 0:
        safe_users = current_users * (_QUEUE_SAFE_UTILIZATION / rho)

    bottleneck = _naive_bottleneck(runtime, component_metrics)
    stability = queueing_results.get("stability") if isinstance(queueing_results, dict) else None
    if bottleneck and stability and str(stability).lower() != "stable":
        bottleneck = f"{bottleneck}/queue"

    return {
        "config": "B_plus_queueing",
        "description": "Config A plus Little's Law / queueing-theoretic capacity reasoning (utilization-based extrapolation).",
        "safe_users": round(safe_users, 2) if safe_users is not None else None,
        "bottleneck": bottleneck,
    }


def _config_c_full_analytical(
    capacity_results: Dict[str, Any],
    bottleneck_results: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    safe_users = capacity_results.get("results", {}).get("safe_users")
    bottleneck = (
        bottleneck_results.get("bottleneck", {}).get("resource")
        if isinstance(bottleneck_results, dict) else None
    )
    return {
        "config": "C_full_analytical",
        "description": "Full pipeline: USL-fitted capacity reference + queueing + Utilization Law bottleneck + Amdahl cross-check.",
        "safe_users": safe_users,
        "bottleneck": bottleneck,
    }


def _config_d_plus_forced_flow(
    capacity_results: Dict[str, Any],
    bottleneck_results_with_components: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    safe_users = capacity_results.get("results", {}).get("safe_users")
    bottleneck = (
        bottleneck_results_with_components.get("bottleneck", {}).get("resource")
        if isinstance(bottleneck_results_with_components, dict) else None
    )
    return {
        "config": "D_plus_forced_flow",
        "description": "Config C, with bottleneck.py's resources also including component-level (DB/cache/API) demand via Forced Flow.",
        "safe_users": safe_users,
        "bottleneck": bottleneck,
    }


# --- Ground truth from real Phase B data ---

def _ground_truth(
    validation_levels: Sequence[Dict[str, Any]],
    slo_thresholds: Optional[SLOThresholds],
) -> Dict[str, Any]:
    """
    Derives the best-available (not oracle-certain - see module overview)
    ground truth from real, actually-measured higher-load data:
      - actual_safe_capacity: SLO capacity computed directly from
        observed response-time/error-rate at each real tested level.
      - actual_bottleneck: the naive-highest-metric heuristic applied to
        the highest actually-tested level - including that level's
        component_metrics if supplied (e.g. db_monitor.py's connection-
        pool pressure), so ground truth can say "database" when that's
        genuinely what the data shows, not just whichever OS-level
        resource happened to be elevated as a downstream symptom.
    """
    slo_input = [
        {
            "users": lvl["users"],
            "response_time": lvl["response_time"],
            "error_rate_percent": lvl["error_rate_percent"],
        }
        for lvl in validation_levels
    ]
    slo_result = analyze_slo(slo_input, thresholds=slo_thresholds)

    highest_level = max(validation_levels, key=lambda lvl: lvl["users"])
    actual_bottleneck = _naive_bottleneck(highest_level, highest_level.get("component_metrics"))

    return {
        "actual_safe_capacity": slo_result["slo_capacity_overall"],
        "actual_bottleneck": actual_bottleneck,
        "slo_result": slo_result,
    }


def run_ablation(
    runtime_metrics: Dict[str, Any],
    capacity_results: Dict[str, Any],
    validation_levels: Sequence[Dict[str, Any]],
    queueing_results: Optional[Dict[str, Any]] = None,
    bottleneck_results: Optional[Dict[str, Any]] = None,
    bottleneck_results_with_components: Optional[Dict[str, Any]] = None,
    component_metrics: Optional[Dict[str, float]] = None,
    slo_thresholds: Optional[SLOThresholds] = None,
) -> Dict[str, Any]:
    """
    Args:
        runtime_metrics: the fitting-run's runtime snapshot (same shape
            capacity.py/scalability.py consume) - current_users,
            cpu_usage, memory_usage, disk_io, network_io required for
            Configs A/B to produce an estimate.
        capacity_results: capacity.analyze_capacity()'s output from the
            fitting run - Configs C/D read safe_users from this directly.
        validation_levels: REAL, actually-measured load levels from
            prediction_validation.py's Phase B - each
            {"users", "response_time", "error_rate_percent"} at minimum,
            optionally "component_metrics": {name: 0-100 pressure
            reading, e.g. from db_monitor.py} for the highest level, so
            ground truth can identify a component as the bottleneck.
            This is the ground truth every config is scored against; it
            must come from real load tests, not predictions.
        queueing_results: optional - queueing.analyze_queue()'s output
            from the fitting run, for Config B.
        bottleneck_results: optional - bottleneck.analyze_bottleneck()'s
            output WITHOUT component_demands, for Config C.
        bottleneck_results_with_components: optional - the same, but
            WITH component_demands merged in (see
            forced_flow.to_bottleneck_resources()), for Config D. Falls
            back to bottleneck_results if not supplied, meaning Config D
            degrades to being identical to Config C - a valid outcome
            when no component instrumentation is available.
        component_metrics: optional - the fitting-run's own component
            pressure readings (same shape as each validation level's
            "component_metrics"), let Configs A/B "see" the same raw
            component numbers Config C/D's demand-based reasoning has
            access to, so the comparison isolates REASONING quality
            rather than being won or lost purely on visibility.
        slo_thresholds: optional SLOThresholds for the ground-truth SLO
            capacity calculation.

    Returns:
        {"ground_truth": {...}, "configurations": [4 dicts, each with
         config/description/safe_users/bottleneck/prediction_error_percent/
         bottleneck_correct]}.

    Raises:
        AblationError: validation_levels is empty, or runtime_metrics/
            capacity_results aren't dicts.
    """
    if not isinstance(runtime_metrics, dict):
        raise AblationError("runtime_metrics must be a dict.")
    if not isinstance(capacity_results, dict):
        raise AblationError("capacity_results must be a dict.")
    if not validation_levels:
        raise AblationError(
            "validation_levels must not be empty - ablation scores each configuration against "
            "real measured data, not predictions; it needs at least one actually-tested level."
        )

    ground_truth = _ground_truth(validation_levels, slo_thresholds)
    actual_capacity = ground_truth["actual_safe_capacity"]
    actual_bottleneck = ground_truth["actual_bottleneck"]

    configurations = [
        _config_a_runtime_only(runtime_metrics, component_metrics),
        _config_b_plus_queueing(runtime_metrics, queueing_results, component_metrics),
        _config_c_full_analytical(capacity_results, bottleneck_results),
        _config_d_plus_forced_flow(capacity_results, bottleneck_results_with_components or bottleneck_results),
    ]

    for config in configurations:
        config["prediction_error_percent"] = _percent_error(config["safe_users"], actual_capacity)
        config["bottleneck_correct"] = _bottleneck_matches(config["bottleneck"], actual_bottleneck)

    return {"ground_truth": ground_truth, "configurations": configurations}
