from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


# --- Exceptions ---

class ScalabilityValidationError(ValueError):
    """Raised when input data fails validation rules."""


class ScalabilityCalculationError(RuntimeError):
    """Raised when a scalability calculation cannot be performed."""


# ==========================================================================
# What this module does
# ==========================================================================
#
# Projects USL's fitted curve forward onto load levels that haven't been
# tested yet, and estimates what every other runtime metric (CPU,
# memory, response time, error rate, disk/network I/O) would look like
# at each of those levels too - not just throughput.
#
# This is the "predicted" leg of the observed / predicted / validated
# three-way split the pipeline as a whole reports. Deliberately NOT
# duplicated here:
#   - "Observed": the raw measurements at load levels that were ACTUALLY
#     tested (e.g. 20-500 users). That data, and the in-sample fit
#     quality over that same range, already lives in usl.py's own
#     analyze() output (observed_vs_predicted, fit_quality). Repeating
#     it here would just be the same numbers under a different key.
#   - "Predicted": what this module computes - USL's curve evaluated at
#     UNTESTED levels (e.g. 600-2000), plus every other metric scaled
#     off of it. This is genuinely this module's job.
#   - "Validated": once a predicted level is LATER actually tested (the
#     project's own "prediction validation experiment" methodology -
#     fit on 20-500, predict 600-1000, then actually run 600-1000 and
#     compare), validate_prediction() below checks the prediction
#     against that real measurement. This is the single most important
#     number in the whole pipeline's research story, and was previously
#     entirely missing from this file despite prediction being its
#     entire purpose - see validate_prediction()'s docstring.
#
# Two independent physical ceilings, cross-checked against every
# prediction, sourced from different modules that each solve a
# different piece of classical queueing/operational theory:
#   - bottleneck_results (bottleneck.py): X <= min(1/D_max, N/(D+Z)) -
#     a hard ceiling on throughput given TODAY's per-request resource
#     demands. A prediction that exceeds this is impossible right now,
#     full stop, regardless of what the USL curve says.
#   - amdahl_results (amdahl.py): the throughput achievable if the
#     CURRENT bottleneck were optimized away entirely, holding every
#     other resource's demand fixed. Deliberately not conflated with the
#     bound above - a prediction can exceed today's asymptotic bound
#     while still sitting under the post-optimization Amdahl ceiling, so
#     both are checked and reported independently (see
#     _extract_amdahl_ceiling()'s docstring).
# Both are optional: omit either (or both) and predict_scalability()
# behaves exactly as it did before that module existed - they're
# supplementary cross-checks, not required inputs.


# --- Configurable constants ---

RESPONSE_TIME_GRADUAL_EXPONENT: float = 0.7
RESPONSE_TIME_MAX_MULTIPLIER: float = 100.0

ERROR_RATE_GRADUAL_SENSITIVITY: float = 0.05
ERROR_RATE_POST_SATURATION_SENSITIVITY: float = 5.0

CLASSIFICATION_STABLE_MAX_PERCENT: float = 70.0
CLASSIFICATION_NEAR_CAPACITY_MAX_PERCENT: float = 90.0
CLASSIFICATION_OVERLOADED_MAX_PERCENT: float = 110.0

OUTPUT_DECIMAL_PLACES: int = 2


# --- Validation helpers ---

def _require_numeric_field(
    section: Dict[str, Any],
    field_name: str,
    section_name: str,
    min_value: Optional[float] = None,
    allow_none: bool = False,
) -> Optional[float]:
    """
    Extract and validate one numeric field.
    """

    if field_name not in section or section[field_name] is None:
        if allow_none:
            return None

        raise ScalabilityValidationError(
            f"Missing required field '{field_name}' in '{section_name}'."
        )

    value = section[field_name]

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScalabilityValidationError(
            f"'{section_name}.{field_name}' must be numeric, "
            f"got {type(value).__name__}."
        )

    value = float(value)

    if not math.isfinite(value):
        raise ScalabilityValidationError(
            f"'{section_name}.{field_name}' must be finite."
        )

    if min_value is not None and value < min_value:
        raise ScalabilityValidationError(
            f"'{section_name}.{field_name}' must be >= {min_value}, "
            f"got {value}."
        )

    return value


def _safe_round(value: Optional[float], nd: int) -> Optional[float]:
    """
    round() raises OverflowError on +/-inf, and math.inf is a legitimate
    value here (an unbounded asymptotic bound when the bottleneck demand
    is zero). Non-finite values pass through unchanged - the rest of the
    pipeline (app.py's sanitize_for_json) already converts them to null
    before JSON serialization, consistent with how queueing.py returns
    raw math.inf for an unstable queue.
    """
    if value is None:
        return None
    if not math.isfinite(value):
        return value
    return round(value, nd)


def _extract_bottleneck_bound_inputs(bottleneck_results: Any) -> Optional[Dict[str, float]]:
    """
    Pull just what's needed to project bottleneck.py's asymptotic
    throughput bound at arbitrary future user counts, out of the direct
    output of bottleneck.analyze_bottleneck() / analyze_bottleneck_safe().

    Returns None if bottleneck_results is missing, an "unavailable"
    result, or malformed - predict_scalability() then skips the bound
    cross-check entirely and behaves exactly as it did before
    bottleneck.py existed. This mirrors capacity.py's
    _extract_bottleneck_data() for the same reason: bottleneck data is
    optional supplementary evidence here, not a required input.
    """
    if not isinstance(bottleneck_results, dict):
        return None
    if bottleneck_results.get("bottleneck_analysis") == "unavailable":
        return None

    total_demand = bottleneck_results.get("total_service_demand")
    bottleneck_demand = bottleneck_results.get("bottleneck", {}).get("service_demand")
    think_time = bottleneck_results.get("think_time", 0.0)

    if isinstance(total_demand, bool) or not isinstance(total_demand, (int, float)):
        return None
    if isinstance(bottleneck_demand, bool) or not isinstance(bottleneck_demand, (int, float)):
        return None
    if not math.isfinite(total_demand) or not math.isfinite(bottleneck_demand):
        return None
    if total_demand < 0 or bottleneck_demand < 0:
        return None

    if isinstance(think_time, bool) or not isinstance(think_time, (int, float)) or not math.isfinite(think_time) or think_time < 0:
        think_time = 0.0

    return {
        "total_demand": float(total_demand),
        "bottleneck_demand": float(bottleneck_demand),
        "think_time": float(think_time),
    }


def _extract_amdahl_ceiling(amdahl_results: Any) -> Optional[float]:
    """
    Pull the bottleneck resource's Amdahl's Law optimization ceiling
    (implied_max_throughput) out of amdahl.analyze_amdahl()'s output, if
    available.

    This is a DIFFERENT number from the asymptotic bound above, not a
    replacement for it: the asymptotic bound (1/D_max) is a hard ceiling
    on CURRENT throughput given today's per-request demands; this ceiling
    is a hypothetical best case AFTER the current bottleneck is optimized
    to be infinitely fast, holding every other resource's demand fixed.
    A prediction can legitimately exceed the asymptotic bound (impossible
    today) while still sitting under the Amdahl ceiling (achievable if
    the bottleneck gets fixed) - the two are checked independently below
    rather than assuming one always dominates the other.

    Returns None if amdahl_results is missing, unavailable, or the
    ceiling itself is unbounded (only one resource monitored - see
    amdahl.py's own caveat about that case).
    """
    if not isinstance(amdahl_results, dict) or not amdahl_results.get("available"):
        return None

    bottleneck_analysis = amdahl_results.get("bottleneck_analysis")
    if not isinstance(bottleneck_analysis, dict):
        return None

    ceiling = bottleneck_analysis.get("implied_max_throughput")
    if isinstance(ceiling, bool) or not isinstance(ceiling, (int, float)):
        return None
    if not math.isfinite(ceiling) or ceiling <= 0:
        return None

    return float(ceiling)


def _validate_inputs(
    usl_results: Dict[str, Any],
    capacity_results: Dict[str, Any],
    runtime_metrics: Dict[str, Any],
    prediction_targets: List[float],
    bottleneck_results: Optional[Dict[str, Any]] = None,
    amdahl_results: Optional[Dict[str, Any]] = None,
    actual_throughput_by_users: Optional[Dict[float, float]] = None,
) -> Dict[str, Any]:
    """
    Validate and normalize all inputs required by predict_scalability().
    """

    if not isinstance(usl_results, dict):
        raise ScalabilityValidationError(
            "usl_results must be a dictionary."
        )

    if not isinstance(capacity_results, dict):
        raise ScalabilityValidationError(
            "capacity_results must be a dictionary."
        )

    if not isinstance(runtime_metrics, dict):
        raise ScalabilityValidationError(
            "runtime_metrics must be a dictionary."
        )

    if bottleneck_results is not None and not isinstance(bottleneck_results, dict):
        raise ScalabilityValidationError(
            "bottleneck_results must be a dictionary if provided."
        )

    if amdahl_results is not None and not isinstance(amdahl_results, dict):
        raise ScalabilityValidationError(
            "amdahl_results must be a dictionary if provided."
        )

    if actual_throughput_by_users is not None and not isinstance(actual_throughput_by_users, dict):
        raise ScalabilityValidationError(
            "actual_throughput_by_users must be a dictionary if provided."
        )

    if (
        not isinstance(prediction_targets, (list, tuple))
        or len(prediction_targets) == 0
    ):
        raise ScalabilityValidationError(
            "prediction_targets must be a non-empty list of user counts."
        )

    # ----------------------------------------------------------------------
    # USL fields
    # ----------------------------------------------------------------------

    sigma = _require_numeric_field(
        usl_results,
        "sigma",
        "usl_results",
        min_value=0.0,
    )

    kappa = _require_numeric_field(
        usl_results,
        "kappa",
        "usl_results",
        min_value=0.0,
    )

    baseline_throughput = _require_numeric_field(
        usl_results,
        "baseline_throughput",
        "usl_results",
        min_value=0.0001,
    )

    peak_throughput = _require_numeric_field(
        usl_results,
        "peak_throughput",
        "usl_results",
        min_value=0.0,
        allow_none=True,
    )

    optimal_users = _require_numeric_field(
        usl_results,
        "optimal_users",
        "usl_results",
        min_value=0.0,
        allow_none=True,
    )

    saturation_point = _require_numeric_field(
        usl_results,
        "saturation_point",
        "usl_results",
        min_value=0.0,
        allow_none=True,
    )

    # ----------------------------------------------------------------------
    # Capacity fields
    # ----------------------------------------------------------------------

    capacity_results_section = capacity_results.get("results")

    if not isinstance(capacity_results_section, dict):
        raise ScalabilityValidationError(
            "capacity_results must contain a 'results' section."
        )

    safe_users = _require_numeric_field(
        capacity_results_section,
        "safe_users",
        "capacity_results.results",
        min_value=0.0,
    )

    # ----------------------------------------------------------------------
    # Runtime fields
    # ----------------------------------------------------------------------

    current_users = _require_numeric_field(
        runtime_metrics,
        "current_users",
        "runtime_metrics",
        min_value=0.0001,
    )

    current_throughput = _require_numeric_field(
        runtime_metrics,
        "throughput",
        "runtime_metrics",
        min_value=0.0001,
    )

    current_response_time = _require_numeric_field(
        runtime_metrics,
        "response_time",
        "runtime_metrics",
        min_value=0.0001,
    )

    current_cpu = _require_numeric_field(
        runtime_metrics,
        "cpu_usage",
        "runtime_metrics",
        min_value=0.0,
    )

    if current_cpu > 100.0:
        raise ScalabilityValidationError(
            "'runtime_metrics.cpu_usage' must be <= 100."
        )

    current_memory = _require_numeric_field(
        runtime_metrics,
        "memory_usage",
        "runtime_metrics",
        min_value=0.0,
    )

    if current_memory > 100.0:
        raise ScalabilityValidationError(
            "'runtime_metrics.memory_usage' must be <= 100."
        )

    current_error_rate = _require_numeric_field(
        runtime_metrics,
        "error_rate",
        "runtime_metrics",
        min_value=0.0,
    )

    if current_error_rate > 100.0:
        raise ScalabilityValidationError(
            "'runtime_metrics.error_rate' must be <= 100."
        )

    # These fields are used later by predict_disk_io() and
    # predict_network_io(), so validate them here as well.
    current_disk_io = _require_numeric_field(
        runtime_metrics,
        "disk_io",
        "runtime_metrics",
        min_value=0.0,
    )

    current_network_io = _require_numeric_field(
        runtime_metrics,
        "network_io",
        "runtime_metrics",
        min_value=0.0,
    )

    # ----------------------------------------------------------------------
    # Prediction targets
    # ----------------------------------------------------------------------

    targets: List[float] = []

    for index, target in enumerate(prediction_targets):
        if isinstance(target, bool) or not isinstance(target, (int, float)):
            raise ScalabilityValidationError(
                f"prediction_targets[{index}] must be numeric, "
                f"got {type(target).__name__}."
            )

        target_value = float(target)

        if not math.isfinite(target_value):
            raise ScalabilityValidationError(
                f"prediction_targets[{index}] must be finite."
            )

        if target_value <= 0:
            raise ScalabilityValidationError(
                f"prediction_targets[{index}] must be positive, "
                f"got {target_value}."
            )

        targets.append(target_value)

    # ----------------------------------------------------------------------
    # Bottleneck bound inputs (optional)
    # ----------------------------------------------------------------------

    bottleneck_bound_inputs = _extract_bottleneck_bound_inputs(bottleneck_results)
    amdahl_ceiling = _extract_amdahl_ceiling(amdahl_results)

    # ----------------------------------------------------------------------
    # Actual (later-measured) throughput, keyed by target user count -
    # for validate_prediction() below. Keys are normalized to float so a
    # caller doesn't need to worry about matching int vs. float exactly
    # against the same prediction_targets values.
    # ----------------------------------------------------------------------

    normalized_actuals: Dict[float, float] = {}
    if actual_throughput_by_users:
        for key, value in actual_throughput_by_users.items():
            if isinstance(key, bool) or not isinstance(key, (int, float)):
                raise ScalabilityValidationError(
                    f"actual_throughput_by_users keys must be numeric user counts, got {key!r}."
                )
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ScalabilityValidationError(
                    f"actual_throughput_by_users[{key!r}] must be numeric, got {type(value).__name__}."
                )
            if not math.isfinite(value) or value < 0:
                raise ScalabilityValidationError(
                    f"actual_throughput_by_users[{key!r}] must be a non-negative finite number."
                )
            normalized_actuals[float(key)] = float(value)

    return {
        "sigma": sigma,
        "kappa": kappa,
        "baseline_throughput": baseline_throughput,
        "peak_throughput": peak_throughput,
        "optimal_users": optimal_users,
        "saturation_point": saturation_point,
        "safe_users": safe_users,
        "current_users": current_users,
        "current_throughput": current_throughput,
        "current_response_time": current_response_time,
        "current_cpu": current_cpu,
        "current_memory": current_memory,
        "current_error_rate": current_error_rate,
        "current_disk_io": current_disk_io,
        "current_network_io": current_network_io,
        "targets": targets,
        "bottleneck_bound_inputs": bottleneck_bound_inputs,
        "amdahl_ceiling": amdahl_ceiling,
        "actual_throughput_by_users": normalized_actuals,
    }


# --- USL throughput prediction ---

def predict_throughput(
    users: float,
    sigma: float,
    kappa: float,
    baseline_throughput: float,
) -> float:
    """
    Predict throughput using already-fitted USL parameters.

    X(N) = X(1) * N /
           (1 + sigma * (N - 1) + kappa * N * (N - 1))
    """

    if users < 1:
        raise ScalabilityCalculationError(
            "users must be >= 1 to predict throughput."
        )

    denominator = (
        1.0
        + sigma * (users - 1.0)
        + kappa * users * (users - 1.0)
    )

    if denominator <= 0:
        raise ScalabilityCalculationError(
            "USL denominator is non-positive."
        )

    return baseline_throughput * users / denominator


# --- Asymptotic-bound cross-check (bottleneck.py) ---

def calculate_asymptotic_bound_at(
    users: float,
    total_demand: float,
    bottleneck_demand: float,
    think_time: float,
) -> Dict[str, Any]:
    """
    X <= min(1/D_max, N/(D+Z)), evaluated at an arbitrary future user
    count. Same math as bottleneck.calculate_asymptotic_bound() -
    reimplemented locally (rather than importing bottleneck.py) so
    scalability.py stays independently importable/testable, consistent
    with capacity.py also only ever consuming bottleneck.py's plain
    output dict rather than its class. See bottleneck.py's
    calculate_asymptotic_bound() docstring for the full derivation.

    Assumes per-request service demand (D_i) stays constant as N grows -
    a simplifying assumption. Contention effects that make demand grow
    with concurrency are exactly what USL's sigma/kappa already model;
    this bound is a complementary, harder physical constraint on top of
    that curve, not a replacement for it.
    """
    if total_demand < 0 or bottleneck_demand < 0 or think_time < 0:
        raise ScalabilityCalculationError(
            "total_demand, bottleneck_demand, and think_time must be non-negative."
        )
    if users <= 0:
        raise ScalabilityCalculationError("users must be greater than zero.")

    bottleneck_bound = (1.0 / bottleneck_demand) if bottleneck_demand > 0 else math.inf
    denom = total_demand + think_time
    concurrency_bound = (users / denom) if denom > 0 else math.inf
    asymptotic_bound = min(bottleneck_bound, concurrency_bound)
    binding_constraint = (
        "bottleneck_resource" if bottleneck_bound <= concurrency_bound else "concurrency"
    )

    return {
        "bottleneck_throughput_bound": bottleneck_bound,
        "concurrency_throughput_bound": concurrency_bound,
        "asymptotic_throughput_bound": asymptotic_bound,
        "binding_constraint": binding_constraint,
    }


# --- Prediction validation (predicted vs. actually-later-measured) ---

def validate_prediction(
    predicted_throughput: float,
    actual_throughput: Optional[float],
    tolerance: float = 0.30,
) -> Optional[Dict[str, Any]]:
    """
    Compare a scalability prediction against what was ACTUALLY measured,
    once that load level was really tested. This is the "validated" leg
    of observed/predicted/validated (see module overview above), and the
    central experimental result the project's own methodology is built
    around: fit USL on tested levels, predict untested ones, then
    actually run those levels and check the prediction against reality.

    Matches the same pattern every other mathematical_engine module
    already has for exactly this reason - little_law.compare_concurrency,
    queueing.compare_system_time, forced_flow.compare_component_throughput
    - scalability.py, whose entire purpose is prediction, was previously
    the one place in the pipeline this was missing.

    Returns None if actual_throughput wasn't supplied - most prediction
    targets won't have been tested yet, and that's the normal case, not
    an error.
    """
    if actual_throughput is None:
        return None
    if isinstance(actual_throughput, bool) or not isinstance(actual_throughput, (int, float)):
        raise ScalabilityValidationError(
            f"actual_throughput must be numeric, got {type(actual_throughput).__name__}."
        )
    if not math.isfinite(actual_throughput) or actual_throughput < 0:
        raise ScalabilityValidationError("actual_throughput must be a non-negative finite number.")
    if not 0 < tolerance:
        raise ScalabilityValidationError("tolerance must be greater than zero.")

    absolute_error = predicted_throughput - actual_throughput
    relative_error = (absolute_error / actual_throughput) if actual_throughput != 0 else None
    percentage_error = abs(relative_error) * 100.0 if relative_error is not None else None
    consistent = abs(relative_error) <= tolerance if relative_error is not None else None

    return {
        "predicted_throughput": round(predicted_throughput, OUTPUT_DECIMAL_PLACES),
        "actual_throughput": actual_throughput,
        "absolute_error": round(absolute_error, OUTPUT_DECIMAL_PLACES),
        "relative_error": round(relative_error, 4) if relative_error is not None else None,
        "percentage_error": round(percentage_error, OUTPUT_DECIMAL_PLACES) if percentage_error is not None else None,
        "tolerance": tolerance,
        "consistent": consistent,
        "note": (
            f"Predicted throughput diverges from the actually-measured value by more than "
            f"{tolerance:.0%} - USL's fit may not generalize well to this load level. This is "
            f"exactly the kind of result worth reporting honestly rather than hiding: it shows "
            f"where the model's assumptions stop holding, which is itself a real finding."
        ) if consistent is False else None,
    }


def _summarize_validation(predictions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Aggregate MAE/RMSE/MAPE across every prediction that had a matching
    actual_throughput supplied - the same error metrics usl.py's own
    prediction_reliability uses, applied here to the full pipeline's
    end-to-end predictions rather than USL's in-sample fit alone.
    """
    validations = [p["validation"] for p in predictions if p.get("validation") is not None]

    if not validations:
        return {
            "validated_count": 0,
            "consistent_count": 0,
            "mae": None,
            "rmse": None,
            "mape_percent": None,
        }

    errors = [v["absolute_error"] for v in validations]
    percentage_errors = [v["percentage_error"] for v in validations if v["percentage_error"] is not None]
    consistent_count = sum(1 for v in validations if v["consistent"])

    mae = sum(abs(e) for e in errors) / len(errors)
    rmse = math.sqrt(sum(e * e for e in errors) / len(errors))
    mape = (sum(percentage_errors) / len(percentage_errors)) if percentage_errors else None

    return {
        "validated_count": len(validations),
        "consistent_count": consistent_count,
        "mae": round(mae, OUTPUT_DECIMAL_PLACES),
        "rmse": round(rmse, OUTPUT_DECIMAL_PLACES),
        "mape_percent": round(mape, OUTPUT_DECIMAL_PLACES) if mape is not None else None,
    }


# --- Metric prediction helpers ---

def calculate_efficiency(
    predicted_throughput: float,
    baseline_throughput: float,
    users: float,
) -> float:
    """
    Calculate predicted scalability efficiency as a ratio from 0 to 1.
    """

    if users <= 0 or baseline_throughput <= 0:
        raise ScalabilityCalculationError(
            "users and baseline_throughput must be positive."
        )

    ideal_throughput = baseline_throughput * users
    efficiency = predicted_throughput / ideal_throughput

    return max(0.0, min(1.0, efficiency))


def calculate_capacity_utilization(
    users: float,
    safe_users: float,
) -> float:
    """
    Calculate capacity utilization as a percentage.

    A safe capacity of zero means the system has no safe
    extrapolation capacity, typically because the queue is unstable.
    """

    if users <= 0:
        raise ScalabilityCalculationError(
            "users must be greater than zero."
        )

    if safe_users <= 0:
        return float("inf")

    return (users / safe_users) * 100.0


def predict_cpu(
    current_cpu: float,
    predicted_throughput: float,
    current_throughput: float,
) -> float:
    """
    Estimate CPU usage by scaling current CPU with throughput.
    """

    if current_throughput <= 0:
        raise ScalabilityCalculationError(
            "current_throughput must be greater than zero."
        )

    predicted = (
        current_cpu
        * (predicted_throughput / current_throughput)
    )

    return max(0.0, min(100.0, predicted))


def predict_memory(
    current_memory: float,
    predicted_users: float,
    current_users: float,
) -> float:
    """
    Estimate memory usage by scaling current memory with users.
    """

    if current_users <= 0:
        raise ScalabilityCalculationError(
            "current_users must be greater than zero."
        )

    predicted = (
        current_memory
        * (predicted_users / current_users)
    )

    return max(0.0, min(100.0, predicted))


def predict_disk_io(
    current_disk_io: float,
    current_throughput: float,
    predicted_throughput: float,
) -> float:
    """
    Estimate disk I/O by scaling current disk I/O with throughput.
    """

    if current_throughput <= 0:
        return current_disk_io

    predicted = (
        current_disk_io
        * predicted_throughput
        / current_throughput
    )

    return max(0.0, round(predicted, 2))


def predict_network_io(
    current_network_io: float,
    current_throughput: float,
    predicted_throughput: float,
) -> float:
    """
    Estimate network I/O by scaling current network I/O with throughput.
    """

    if current_throughput <= 0:
        return current_network_io

    predicted = (
        current_network_io
        * predicted_throughput
        / current_throughput
    )

    return max(0.0, round(predicted, 2))


def predict_response_time(
    current_response_time: float,
    users: float,
    current_users: float,
    saturation_point: Optional[float],
) -> float:
    """
    Estimate response time using gradual growth with a bounded
    post-saturation increase.

    USL saturation is treated as a warning point, not as a point
    where response time becomes mathematically infinite.
    """

    if current_users <= 0:
        raise ScalabilityCalculationError(
            "current_users must be greater than zero."
        )

    growth_ratio = users / current_users

    if (
        saturation_point is None
        or saturation_point <= 0
        or users <= saturation_point
    ):
        predicted = (
            current_response_time
            * growth_ratio ** RESPONSE_TIME_GRADUAL_EXPONENT
        )
    else:
        response_time_at_saturation = (
            current_response_time
            * (saturation_point / current_users)
            ** RESPONSE_TIME_GRADUAL_EXPONENT
        )

        excess_ratio = (
            users - saturation_point
        ) / saturation_point

        # Quadratic growth after saturation.
        # This represents increasing contention without
        # producing mathematically explosive values.
        post_saturation_multiplier = (
            1.0
            + excess_ratio
            + excess_ratio ** 2
        )

        predicted = (
            response_time_at_saturation
            * post_saturation_multiplier
        )

    maximum_response_time = (
        current_response_time
        * RESPONSE_TIME_MAX_MULTIPLIER
    )

    return min(
        max(0.0, predicted),
        maximum_response_time,
    )


def predict_error_rate(
    current_error_rate: float,
    users: float,
    current_users: float,
    saturation_point: Optional[float],
) -> float:
    """
    Estimate error rate as a percentage.

    Error rate increases gradually with load. After saturation,
    additional pressure increases the estimated error rate, but
    the prediction remains bounded between 0% and 100%.
    """

    if current_users <= 0:
        raise ScalabilityCalculationError(
            "current_users must be greater than zero."
        )

    growth_ratio = users / current_users

    if (
        saturation_point is None
        or saturation_point <= 0
        or users <= saturation_point
    ):
        predicted = current_error_rate * (
            1.0
            + ERROR_RATE_GRADUAL_SENSITIVITY
            * max(0.0, growth_ratio - 1.0)
        )
    else:
        error_rate_at_saturation = current_error_rate * (
            1.0
            + ERROR_RATE_GRADUAL_SENSITIVITY
            * max(
                0.0,
                saturation_point / current_users - 1.0,
            )
        )

        excess_ratio = (
            users - saturation_point
        ) / saturation_point

        predicted = (
            error_rate_at_saturation
            + ERROR_RATE_POST_SATURATION_SENSITIVITY
            * excess_ratio
        )

    return max(0.0, min(100.0, predicted))

# --- Classification helpers ---

def determine_saturation_risk(
    users: float,
    safe_users: float,
    saturation_point: Optional[float],
    optimal_users: Optional[float],
) -> str:
    """
    Classify theoretical saturation risk.

    A safe capacity of zero indicates that the system has no
    safe extrapolation capacity.
    """

    if safe_users <= 0:
        return "Critical"

    if users < safe_users:
        return "Low"

    if optimal_users is not None and users >= optimal_users:
        return "Critical"

    if (
        saturation_point is not None
        and saturation_point > 0
        and users < saturation_point
    ):
        return "Medium"

    return "High"


def classify_prediction(capacity_utilization: float) -> str:
    """
    Classify prediction based on safe capacity utilization.

    < 70%       -> Stable
    70% - <90%  -> Near Capacity
    90% - <=110% -> Overloaded
    > 110%      -> Collapsed
    """

    if capacity_utilization < CLASSIFICATION_STABLE_MAX_PERCENT:
        return "Stable"

    if (
        capacity_utilization
        < CLASSIFICATION_NEAR_CAPACITY_MAX_PERCENT
    ):
        return "Near Capacity"

    if (
        capacity_utilization
        <= CLASSIFICATION_OVERLOADED_MAX_PERCENT
    ):
        return "Overloaded"

    return "Collapsed"


# --- Summary ---

def generate_summary(
    predictions: List[Dict[str, Any]],
    safe_users: float,
    bottleneck_bound_available: bool = False,
    amdahl_ceiling_available: bool = False,
) -> Dict[str, Any]:
    """
    Generate summary values from computed predictions.
    """

    if not predictions:
        return {
            "maximum_predicted_users": None,
            "safe_prediction_limit": round(
                safe_users,
                OUTPUT_DECIMAL_PLACES,
            ),
            "first_overloaded_point": None,
            "first_collapsed_point": None,
            "first_bound_exceeded_point": None,
            "first_amdahl_ceiling_exceeded_point": None,
            "recommended_max_users": round(
                safe_users,
                OUTPUT_DECIMAL_PLACES,
            ),
            "highest_cpu": None,
            "highest_memory": None,
            "highest_response_time": None,
            "highest_disk_io": None,
            "highest_network_io": None,
            "bottleneck_bound_available": bottleneck_bound_available,
            "amdahl_ceiling_available": amdahl_ceiling_available,
            "validation_summary": _summarize_validation([]),
        }

    maximum_predicted_users = max(
        prediction["users"]
        for prediction in predictions
    )

    first_overloaded_point = next(
        (
            prediction["users"]
            for prediction in predictions
            if prediction["classification"] == "Overloaded"
        ),
        None,
    )

    first_collapsed_point = next(
        (
            prediction["users"]
            for prediction in predictions
            if prediction["classification"] == "Collapsed"
        ),
        None,
    )

    first_bound_exceeded_point = next(
        (
            prediction["users"]
            for prediction in predictions
            if prediction.get("exceeds_asymptotic_bound")
        ),
        None,
    )

    first_amdahl_ceiling_exceeded_point = next(
        (
            prediction["users"]
            for prediction in predictions
            if prediction.get("exceeds_amdahl_ceiling")
        ),
        None,
    )

    acceptable_levels = [
        prediction["users"]
        for prediction in predictions
        if prediction["classification"]
        in ("Stable", "Near Capacity")
    ]

    if safe_users <= 0:
        recommended_max_users = 0.0
    else:
        recommended_max_users = (
            max(acceptable_levels)
            if acceptable_levels
            else safe_users
        )

    highest_cpu = max(
        prediction["predicted_cpu"]
        for prediction in predictions
    )

    highest_memory = max(
        prediction["predicted_memory"]
        for prediction in predictions
    )

    highest_response_time = max(
        prediction["predicted_response_time"]
        for prediction in predictions
    )

    highest_disk_io = max(
        prediction["predicted_disk_io"]
        for prediction in predictions
    )

    highest_network_io = max(
        prediction["predicted_network_io"]
        for prediction in predictions
    )

    return {
        "maximum_predicted_users": round(
            maximum_predicted_users,
            OUTPUT_DECIMAL_PLACES,
        ),
        "safe_prediction_limit": round(
            safe_users,
            OUTPUT_DECIMAL_PLACES,
        ),
        "first_overloaded_point": (
            round(
                first_overloaded_point,
                OUTPUT_DECIMAL_PLACES,
            )
            if first_overloaded_point is not None
            else None
        ),
        "first_collapsed_point": (
            round(
                first_collapsed_point,
                OUTPUT_DECIMAL_PLACES,
            )
            if first_collapsed_point is not None
            else None
        ),
        "first_bound_exceeded_point": (
            round(
                first_bound_exceeded_point,
                OUTPUT_DECIMAL_PLACES,
            )
            if first_bound_exceeded_point is not None
            else None
        ),
        "first_amdahl_ceiling_exceeded_point": (
            round(
                first_amdahl_ceiling_exceeded_point,
                OUTPUT_DECIMAL_PLACES,
            )
            if first_amdahl_ceiling_exceeded_point is not None
            else None
        ),
        "recommended_max_users": round(
            recommended_max_users,
            OUTPUT_DECIMAL_PLACES,
        ),
        "highest_cpu": round(
            highest_cpu,
            OUTPUT_DECIMAL_PLACES,
        ),
        "highest_memory": round(
            highest_memory,
            OUTPUT_DECIMAL_PLACES,
        ),
        "highest_response_time": round(
            highest_response_time,
            OUTPUT_DECIMAL_PLACES,
        ),
        "highest_disk_io": round(
            highest_disk_io,
            OUTPUT_DECIMAL_PLACES,
        ),
        "highest_network_io": round(
            highest_network_io,
            OUTPUT_DECIMAL_PLACES,
        ),
        "bottleneck_bound_available": bottleneck_bound_available,
        "amdahl_ceiling_available": amdahl_ceiling_available,
        "validation_summary": _summarize_validation(predictions),
    }


# --- Public entry point ---

def predict_scalability(
    usl_results: Dict[str, Any],
    capacity_results: Dict[str, Any],
    runtime_metrics: Dict[str, Any],
    prediction_targets: List[float],
    bottleneck_results: Optional[Dict[str, Any]] = None,
    amdahl_results: Optional[Dict[str, Any]] = None,
    actual_throughput_by_users: Optional[Dict[float, float]] = None,
) -> Dict[str, Any]:
    """
    Predict application behavior at future user levels.

    prediction_targets should contain future levels such as:

        [600, 700, 800, 1000, 2000, 5000]

    Do not replace prediction_targets with the already-observed levels:

        [20, 50, 100, 200, 300, 500]

    bottleneck_results is optional - pass the direct output of
    bottleneck.analyze_bottleneck() / analyze_bottleneck_safe() to cross-
    check every prediction against the independently-derived asymptotic
    throughput ceiling (X <= min(1/D_max, N/(D+Z))). When a predicted
    throughput exceeds that ceiling, the prediction is flagged
    (exceeds_asymptotic_bound=True) and its classification/saturation_risk
    are escalated to Collapsed/Critical, since a USL curve that has
    diverged from a hard physical constraint is no longer a trustworthy
    extrapolation at that point - regardless of what capacity_utilization
    alone would suggest. When omitted, behavior is identical to before
    bottleneck.py existed.

    amdahl_results is optional - pass the direct output of
    amdahl.analyze_amdahl() to also flag predictions that exceed the
    OPTIMIZED-bottleneck ceiling (implied_max_throughput): the throughput
    achievable if the current bottleneck resource were fixed entirely,
    holding every other resource's demand constant. This is a DIFFERENT,
    higher ceiling than the asymptotic bound - a prediction can exceed
    the asymptotic bound (impossible today) while still sitting under the
    Amdahl ceiling (achievable once the bottleneck is fixed), so both are
    checked and reported independently rather than one superseding the
    other. Exceeding the Amdahl ceiling additionally means a single-
    resource fix won't be enough to reach that load level. When omitted,
    behavior is identical to before amdahl.py existed.

    actual_throughput_by_users is optional - {user_count: actual
    throughput measured once that level was really tested, ...}. This is
    the "validated" leg of observed/predicted/validated (see module
    overview): a prediction target that was extrapolated before the
    experiment, then actually run afterward, gets its predicted value
    checked against reality via validate_prediction() - MAE/RMSE/MAPE
    across every validated target are aggregated into
    summary["validation_summary"]. A target with no matching actual value
    here simply has "validation": None - most targets won't have been
    tested yet, and that's the normal, expected case.
    """

    validated = _validate_inputs(
        usl_results=usl_results,
        capacity_results=capacity_results,
        runtime_metrics=runtime_metrics,
        prediction_targets=prediction_targets,
        bottleneck_results=bottleneck_results,
        amdahl_results=amdahl_results,
        actual_throughput_by_users=actual_throughput_by_users,
    )

    sigma = validated["sigma"]
    kappa = validated["kappa"]
    baseline_throughput = validated["baseline_throughput"]

    optimal_users = validated["optimal_users"]
    saturation_point = validated["saturation_point"]
    safe_users = validated["safe_users"]

    current_users = validated["current_users"]
    current_throughput = validated["current_throughput"]
    current_response_time = validated["current_response_time"]
    current_cpu = validated["current_cpu"]
    current_memory = validated["current_memory"]
    current_error_rate = validated["current_error_rate"]

    # Use the validated values instead of accessing runtime_metrics
    # directly during the prediction loop.
    current_disk_io = validated["current_disk_io"]
    current_network_io = validated["current_network_io"]

    bottleneck_bound_inputs = validated["bottleneck_bound_inputs"]
    amdahl_ceiling = validated["amdahl_ceiling"]
    actual_throughput_by_users = validated["actual_throughput_by_users"]

    predictions: List[Dict[str, Any]] = []

    for users in sorted(validated["targets"]):

        predicted_throughput = predict_throughput(
            users=users,
            sigma=sigma,
            kappa=kappa,
            baseline_throughput=baseline_throughput,
        )

        predicted_cpu = predict_cpu(
            current_cpu=current_cpu,
            predicted_throughput=predicted_throughput,
            current_throughput=current_throughput,
        )

        predicted_memory = predict_memory(
            current_memory=current_memory,
            predicted_users=users,
            current_users=current_users,
        )

        predicted_response_time = predict_response_time(
            current_response_time=current_response_time,
            users=users,
            current_users=current_users,
            saturation_point=saturation_point,
        )

        predicted_error_rate = predict_error_rate(
            current_error_rate=current_error_rate,
            users=users,
            current_users=current_users,
            saturation_point=saturation_point,
        )

        predicted_disk_io = predict_disk_io(
            current_disk_io=current_disk_io,
            current_throughput=current_throughput,
            predicted_throughput=predicted_throughput,
        )

        predicted_network_io = predict_network_io(
            current_network_io=current_network_io,
            current_throughput=current_throughput,
            predicted_throughput=predicted_throughput,
        )

        scalability_efficiency = calculate_efficiency(
            predicted_throughput=predicted_throughput,
            baseline_throughput=baseline_throughput,
            users=users,
        )

        capacity_utilization = calculate_capacity_utilization(
            users=users,
            safe_users=safe_users,
        )

        # ------------------------------------------------------------
        # Asymptotic bound cross-check (bottleneck.py), if available
        # ------------------------------------------------------------
        bound_info = None
        bounded_predicted_throughput = predicted_throughput
        exceeds_asymptotic_bound = False

        if bottleneck_bound_inputs is not None:
            bound_info = calculate_asymptotic_bound_at(
                users=users,
                total_demand=bottleneck_bound_inputs["total_demand"],
                bottleneck_demand=bottleneck_bound_inputs["bottleneck_demand"],
                think_time=bottleneck_bound_inputs["think_time"],
            )
            asymptotic_bound = bound_info["asymptotic_throughput_bound"]

            exceeds_asymptotic_bound = bool(
                math.isfinite(asymptotic_bound)
                and predicted_throughput > asymptotic_bound * (1 + 1e-6)
            )

            bounded_predicted_throughput = (
                min(predicted_throughput, asymptotic_bound)
                if math.isfinite(asymptotic_bound)
                else predicted_throughput
            )

        # ------------------------------------------------------------
        # Amdahl optimization-ceiling cross-check (amdahl.py), if
        # available. Independent of the asymptotic bound above - see
        # _extract_amdahl_ceiling()'s docstring for why these two
        # numbers are deliberately not conflated.
        # ------------------------------------------------------------
        exceeds_amdahl_ceiling = False
        if amdahl_ceiling is not None:
            exceeds_amdahl_ceiling = predicted_throughput > amdahl_ceiling * (1 + 1e-6)

        saturation_risk = determine_saturation_risk(
            users=users,
            safe_users=safe_users,
            saturation_point=saturation_point,
            optimal_users=optimal_users,
        )

        classification = classify_prediction(
            capacity_utilization
        )

        # A predicted throughput that exceeds a hard physical ceiling
        # means the USL curve has diverged from what's achievable at
        # this N - that's a stronger, more direct signal than the
        # capacity_utilization-based classification above, so it wins.
        # Exceeding the Amdahl ceiling is at least as strong a signal
        # (it means even an optimized bottleneck wouldn't get there), so
        # it escalates the same way.
        if exceeds_asymptotic_bound or exceeds_amdahl_ceiling:
            classification = "Collapsed"
            saturation_risk = "Critical"

        validation = validate_prediction(
            predicted_throughput=predicted_throughput,
            actual_throughput=actual_throughput_by_users.get(users),
        )

        predictions.append({
            "users": round(
                users,
                OUTPUT_DECIMAL_PLACES,
            ),
            "predicted_throughput": round(
                predicted_throughput,
                OUTPUT_DECIMAL_PLACES,
            ),
            "bounded_predicted_throughput": _safe_round(
                bounded_predicted_throughput,
                OUTPUT_DECIMAL_PLACES,
            ) if bound_info is not None else None,
            "asymptotic_throughput_bound": _safe_round(
                bound_info["asymptotic_throughput_bound"],
                OUTPUT_DECIMAL_PLACES,
            ) if bound_info is not None else None,
            "bound_binding_constraint": (
                bound_info["binding_constraint"] if bound_info is not None else None
            ),
            "exceeds_asymptotic_bound": exceeds_asymptotic_bound,
            "amdahl_max_throughput_ceiling": (
                _safe_round(amdahl_ceiling, OUTPUT_DECIMAL_PLACES) if amdahl_ceiling is not None else None
            ),
            "exceeds_amdahl_ceiling": exceeds_amdahl_ceiling,
            "predicted_cpu": round(
                predicted_cpu,
                OUTPUT_DECIMAL_PLACES,
            ),
            "predicted_memory": round(
                predicted_memory,
                OUTPUT_DECIMAL_PLACES,
            ),
            "predicted_response_time": round(
                predicted_response_time,
                OUTPUT_DECIMAL_PLACES,
            ),
            "predicted_error_rate": round(
                predicted_error_rate,
                OUTPUT_DECIMAL_PLACES,
            ),
            "predicted_disk_io": round(
                predicted_disk_io,
                OUTPUT_DECIMAL_PLACES,
            ),
            "predicted_network_io": round(
                predicted_network_io,
                OUTPUT_DECIMAL_PLACES,
            ),
            "scalability_efficiency": round(
                scalability_efficiency,
                OUTPUT_DECIMAL_PLACES,
            ),
            "capacity_utilization": round(
                capacity_utilization,
                OUTPUT_DECIMAL_PLACES,
            ) if math.isfinite(capacity_utilization) else capacity_utilization,
            "saturation_risk": saturation_risk,
            "classification": classification,
            "validation": validation,
        })

    summary = generate_summary(
        predictions=predictions,
        safe_users=safe_users,
        bottleneck_bound_available=bottleneck_bound_inputs is not None,
        amdahl_ceiling_available=amdahl_ceiling is not None,
    )

    return {
        "predictions": predictions,
        "summary": summary,
    }