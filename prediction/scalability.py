from __future__ import annotations

import math
from typing import Any, Dict, List, Optional
from typing_extensions import runtime


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------

class ScalabilityValidationError(ValueError):
    """Raised when input data fails validation rules for scalability
    prediction."""


class ScalabilityCalculationError(RuntimeError):
    """Raised when a scalability calculation cannot be performed because
    a helper function received a mathematically invalid argument."""


# --------------------------------------------------------------------------
# Configurable constants
# --------------------------------------------------------------------------
# Centralized here (rather than scattered as magic numbers through the
# code) so behavior can be tuned without touching calculation logic.

# -- Response time model -----------------------------------------------
# Below the saturation point, response time is assumed to grow gradually
# and sub-linearly with user count: RT(N) = RT_current * (N / N_current)^exponent.
RESPONSE_TIME_GRADUAL_EXPONENT: float = 0.7

# Beyond the saturation point, response time is assumed to grow
# exponentially with the fractional distance past saturation:
# RT(N) = RT_at_saturation * exp(rate * (N - saturation_point) / saturation_point)
RESPONSE_TIME_EXPONENTIAL_RATE: float = 1.5

# -- Error rate model -----------------------------------------------------
# Below saturation, error rate grows only slightly, proportional to the
# fractional growth in users, scaled by this (small) sensitivity factor.
ERROR_RATE_GRADUAL_SENSITIVITY: float = 0.05

# Beyond saturation, error rate increases roughly linearly with the
# fractional distance past saturation, scaled by this sensitivity factor
# (expressed in percentage points).
ERROR_RATE_POST_SATURATION_SENSITIVITY: float = 15.0

# -- Saturation risk classification --------------------------------------
# See determine_saturation_risk() for how these are applied.
# (No additional constants needed -- risk bands are defined directly by
# safe_users, saturation_point, and optimal_users, all of which are
# already-computed inputs rather than new magic numbers.)

# -- Prediction classification (capacity utilization bands, in percent) --
CLASSIFICATION_STABLE_MAX_PERCENT: float = 70.0
CLASSIFICATION_NEAR_CAPACITY_MAX_PERCENT: float = 90.0
CLASSIFICATION_OVERLOADED_MAX_PERCENT: float = 110.0
# >= CLASSIFICATION_OVERLOADED_MAX_PERCENT -> "Collapsed"

# -- Rounding ---------------------------------------------------------------
OUTPUT_DECIMAL_PLACES: int = 2


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _require_numeric_field(
    section: Dict[str, Any],
    field_name: str,
    section_name: str,
    min_value: Optional[float] = None,
    allow_none: bool = False,
) -> Optional[float]:
    """
    Extract and validate a single numeric field from a section dictionary.

    Args:
        section: The sub-dictionary being validated (e.g. usl_results).
        field_name: The key to extract.
        section_name: Human-readable section name, for error messages.
        min_value: Optional inclusive lower bound.
        allow_none: Whether the field may be missing or None (used for
            USL fields like optimal_users, which can legitimately be
            None when USL found no finite optimum).

    Returns:
        The validated float value, or None if allow_none and the field
        is absent/None.

    Raises:
        ScalabilityValidationError: On any validation failure.
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
            f"'{section_name}.{field_name}' must be numeric, got {type(value).__name__}."
        )

    value = float(value)
    if math.isnan(value) or math.isinf(value):
        raise ScalabilityValidationError(f"'{section_name}.{field_name}' must be finite.")
    if min_value is not None and value < min_value:
        raise ScalabilityValidationError(
            f"'{section_name}.{field_name}' must be >= {min_value}, got {value}."
        )
    return value


def _validate_inputs(
    usl_results: Dict[str, Any],
    capacity_results: Dict[str, Any],
    runtime_metrics: Dict[str, Any],
    prediction_targets: List[float],
) -> Dict[str, Any]:
    """
    Validate all four inputs to predict_scalability() and return a
    normalized dictionary of the fields this module actually needs.

    Validation rules:
        * usl_results must contain numeric sigma (>= 0), kappa (>= 0),
          and baseline_throughput (> 0) -- these are REUSED as-is, never
          refit. peak_throughput, optimal_users, and saturation_point are
          also read; optimal_users and saturation_point may be None
          (USL reports None for optimal_users when kappa <= 0).
        * capacity_results must contain numeric safe_users (> 0).
        * runtime_metrics must contain numeric current_users (> 0),
          throughput (> 0), response_time (> 0), cpu_usage (0-100),
          memory_usage (0-100), and error_rate (>= 0).
        * prediction_targets must be a non-empty list of positive
          numeric user counts.

    Args:
        usl_results: Output of usl.run_usl_analysis() (or equivalent dict).
        capacity_results: Output of capacity.analyze_capacity() (or
            equivalent dict).
        runtime_metrics: Latest observed runtime metrics.
        prediction_targets: Future user levels to predict for.

    Returns:
        Dict with normalized/validated fields:
            sigma, kappa, baseline_throughput, peak_throughput,
            optimal_users, saturation_point, safe_users, current_users,
            current_throughput, current_response_time, current_cpu,
            current_memory, current_error_rate, targets

    Raises:
        ScalabilityValidationError: On any validation failure.
    """
    if not isinstance(usl_results, dict):
        raise ScalabilityValidationError("usl_results must be a dictionary.")
    if not isinstance(capacity_results, dict):
        raise ScalabilityValidationError("capacity_results must be a dictionary.")
    if not isinstance(runtime_metrics, dict):
        raise ScalabilityValidationError("runtime_metrics must be a dictionary.")
    if not isinstance(prediction_targets, (list, tuple)) or len(prediction_targets) == 0:
        raise ScalabilityValidationError("prediction_targets must be a non-empty list of user counts.")

    sigma = _require_numeric_field(usl_results, "sigma", "usl_results", min_value=0.0)
    kappa = _require_numeric_field(usl_results, "kappa", "usl_results", min_value=0.0)
    baseline_throughput = _require_numeric_field(usl_results, "baseline_throughput", "usl_results", min_value=0.0001)
    peak_throughput = _require_numeric_field(usl_results, "peak_throughput", "usl_results", min_value=0.0, allow_none=True)
    optimal_users = _require_numeric_field(usl_results, "optimal_users", "usl_results", min_value=0.0, allow_none=True)
    saturation_point = _require_numeric_field(usl_results, "saturation_point", "usl_results", min_value=0.0, allow_none=True)

    results = capacity_results.get("results")

    if not isinstance(results, dict):
        raise ScalabilityValidationError(
            "capacity_results must contain a 'results' section."
        )

    safe_users = _require_numeric_field(
        results,
        "safe_users",
        "capacity_results.results",
        min_value=0.0001,
    )

    current_users = _require_numeric_field(runtime_metrics, "current_users", "runtime_metrics", min_value=0.0001)
    current_throughput = _require_numeric_field(runtime_metrics, "throughput", "runtime_metrics", min_value=0.0001)
    current_response_time = _require_numeric_field(runtime_metrics, "response_time", "runtime_metrics", min_value=0.0001)
    current_cpu = _require_numeric_field(runtime_metrics, "cpu_usage", "runtime_metrics", min_value=0.0)
    if current_cpu > 100.0:
        raise ScalabilityValidationError(f"'runtime_metrics.cpu_usage' must be <= 100, got {current_cpu}.")
    current_memory = _require_numeric_field(runtime_metrics, "memory_usage", "runtime_metrics", min_value=0.0)
    if current_memory > 100.0:
        raise ScalabilityValidationError(f"'runtime_metrics.memory_usage' must be <= 100, got {current_memory}.")
    current_error_rate = _require_numeric_field(runtime_metrics, "error_rate", "runtime_metrics", min_value=0.0)

    targets: List[float] = []
    for i, target in enumerate(prediction_targets):
        if isinstance(target, bool) or not isinstance(target, (int, float)):
            raise ScalabilityValidationError(
                f"prediction_targets[{i}] must be numeric, got {type(target).__name__}."
            )
        target_value = float(target)
        if target_value <= 0:
            raise ScalabilityValidationError(
                f"prediction_targets[{i}] must be a positive user count, got {target_value}."
            )
        targets.append(target_value)

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
        "targets": targets,
    }


# --------------------------------------------------------------------------
# Prediction helpers
# --------------------------------------------------------------------------

def predict_throughput(users: float, sigma: float, kappa: float, baseline_throughput: float) -> float:
    """
    Predict throughput at a future user level using the Universal
    Scalability Law equation, reusing the already-fitted sigma, kappa,
    and baseline_throughput from usl.py. This module never refits the
    model -- it only evaluates the existing fitted curve at new points.

        X(N) = baseline_throughput * N / (1 + sigma*(N-1) + kappa*N*(N-1))

    Args:
        users: N, the future user level to predict for.
        sigma: Fitted contention coefficient (from usl_results).
        kappa: Fitted coherency coefficient (from usl_results).
        baseline_throughput: X(1), fitted baseline throughput.

    Returns:
        Predicted throughput at `users`.

    Raises:
        ScalabilityCalculationError: If users < 1.
    """
    if users < 1:
        raise ScalabilityCalculationError("users must be >= 1 to predict throughput.")
    denominator = 1.0 + sigma * (users - 1.0) + kappa * users * (users - 1.0)
    if denominator <= 0:
        # Not expected for sigma, kappa >= 0 and users >= 1, but guarded
        # defensively against pathological fitted parameters.
        raise ScalabilityCalculationError("USL denominator is non-positive; check sigma/kappa values.")
    return baseline_throughput * users / denominator


def calculate_efficiency(predicted_throughput: float, baseline_throughput: float, users: float) -> float:
    """
    Predicted scalability efficiency: how close the predicted throughput
    is to the theoretical "perfectly linear" ideal throughput.

        Ideal Throughput = baseline_throughput * users
        Efficiency = Predicted Throughput / Ideal Throughput

    Clamped to [0, 1] since efficiency above 1 (super-linear scaling)
    is not physically meaningful under the USL model and below 0 is
    impossible for non-negative throughput.

    Args:
        predicted_throughput: Result of predict_throughput().
        baseline_throughput: X(1), fitted baseline throughput.
        users: N, the user level being evaluated.

    Returns:
        Efficiency ratio in [0, 1].

    Raises:
        ScalabilityCalculationError: If users <= 0 or baseline_throughput <= 0.
    """
    if users <= 0 or baseline_throughput <= 0:
        raise ScalabilityCalculationError("users and baseline_throughput must be positive to calculate efficiency.")
    ideal_throughput = baseline_throughput * users
    efficiency = predicted_throughput / ideal_throughput
    return max(0.0, min(1.0, efficiency))


def calculate_capacity_utilization(users: float, safe_users: float) -> float:
    """
    Predicted capacity utilization (%): how far the predicted user level
    sits relative to the safe operating capacity determined by
    capacity.py.

        Capacity Utilization = (users / safe_users) * 100

    Args:
        users: N, the user level being evaluated.
        safe_users: Safe operating capacity (from capacity_results).

    Returns:
        Capacity utilization percentage. Not clamped -- values well
        above 100% are expected and meaningful (they indicate the
        predicted load exceeds the safe ceiling).

    Raises:
        ScalabilityCalculationError: If safe_users <= 0.
    """
    if safe_users <= 0:
        raise ScalabilityCalculationError("safe_users must be greater than zero to calculate capacity_utilization.")
    return (users / safe_users) * 100.0


def predict_cpu(current_cpu: float, predicted_throughput: float, current_throughput: float) -> float:
    """
    Predict CPU utilization (%) by scaling the currently measured CPU
    usage by the ratio of predicted to current throughput:

        predicted_cpu = current_cpu * (predicted_throughput / current_throughput)

    Clamped to [0, 100].

    Args:
        current_cpu: Currently observed CPU usage (%).
        predicted_throughput: Result of predict_throughput().
        current_throughput: Currently observed throughput.

    Returns:
        Predicted CPU usage, clamped to [0, 100].

    Raises:
        ScalabilityCalculationError: If current_throughput <= 0.
    """
    if current_throughput <= 0:
        raise ScalabilityCalculationError("current_throughput must be greater than zero to predict CPU usage.")
    predicted = current_cpu * (predicted_throughput / current_throughput)
    return max(0.0, min(100.0, predicted))


def predict_memory(current_memory: float, predicted_users: float, current_users: float) -> float:
    """
    Predict memory utilization (%) by scaling the currently measured
    memory usage by the ratio of predicted to current user count:

        predicted_memory = current_memory * (predicted_users / current_users)

    Clamped to [0, 100]. Memory is modeled as scaling with concurrent
    users rather than throughput, since per-session/per-connection state
    (caches, sessions, buffers) typically grows with concurrency rather
    than request rate alone.

    Args:
        current_memory: Currently observed memory usage (%).
        predicted_users: N, the future user level being evaluated.
        current_users: Currently observed concurrent user count.

    Returns:
        Predicted memory usage, clamped to [0, 100].

    Raises:
        ScalabilityCalculationError: If current_users <= 0.
    """
    if current_users <= 0:
        raise ScalabilityCalculationError("current_users must be greater than zero to predict memory usage.")
    predicted = current_memory * (predicted_users / current_users)
    return max(0.0, min(100.0, predicted))

def predict_disk_io(
    current_disk_io: float,
    current_throughput: float,
    predicted_throughput: float,
) -> float:
    """
    Predict disk I/O using throughput scaling.

    Assumption:
    More requests generally produce more disk activity.
    """

    if current_throughput <= 0:
        return current_disk_io

    predicted = (
        current_disk_io
        * predicted_throughput
        / current_throughput
    )

    return round(predicted, 2)

def predict_network_io(
    current_network_io: float,
    current_throughput: float,
    predicted_throughput: float,
) -> float:
    """
    Predict network I/O using throughput scaling.

    Assumption:
    Network traffic increases with request throughput.
    """

    if current_throughput <= 0:
        return current_network_io

    predicted = (
        current_network_io
        * predicted_throughput
        / current_throughput
    )

    return round(predicted, 2)

def predict_response_time(
    current_response_time: float,
    users: float,
    current_users: float,
    saturation_point: Optional[float],
) -> float:
    """
    Predict response time at a future user level.

    Below (or at) the saturation point, response time is modeled as
    growing gradually and sub-linearly with user count:

        RT(N) = current_response_time * (N / current_users) ^ RESPONSE_TIME_GRADUAL_EXPONENT

    Beyond the saturation point, response time is modeled as growing
    exponentially with the fractional distance past saturation, anchored
    to continue smoothly from the gradual curve's value AT the
    saturation point:

        RT_at_saturation = current_response_time * (saturation_point / current_users) ^ RESPONSE_TIME_GRADUAL_EXPONENT
        RT(N) = RT_at_saturation * exp(RESPONSE_TIME_EXPONENTIAL_RATE * (N - saturation_point) / saturation_point)

    If saturation_point is unavailable (USL found no finite optimum),
    the gradual model is used for all user levels, since there is no
    known threshold beyond which to switch to exponential growth.

    This is intentionally a simple, documented approximation, as
    permitted by the requirements ("a simple implementation is
    acceptable") -- it is not a queueing-theoretic derivation (that
    belongs to queueing.py).

    Args:
        current_response_time: Currently observed response time.
        users: N, the future user level being evaluated.
        current_users: Currently observed concurrent user count.
        saturation_point: USL saturation point, or None.

    Returns:
        Predicted response time (same unit as current_response_time).

    Raises:
        ScalabilityCalculationError: If current_users <= 0.
    """
    if current_users <= 0:
        raise ScalabilityCalculationError("current_users must be greater than zero to predict response_time.")

    gradual_rt = current_response_time * (users / current_users) ** RESPONSE_TIME_GRADUAL_EXPONENT

    if saturation_point is None or users <= saturation_point:
        return gradual_rt

    rt_at_saturation = current_response_time * (saturation_point / current_users) ** RESPONSE_TIME_GRADUAL_EXPONENT
    fractional_excess = (users - saturation_point) / saturation_point
    return rt_at_saturation * math.exp(RESPONSE_TIME_EXPONENTIAL_RATE * fractional_excess)


def predict_error_rate(
    current_error_rate: float,
    users: float,
    current_users: float,
    saturation_point: Optional[float],
) -> float:
    """
    Predict error rate (%) at a future user level.

    Below (or at) the saturation point, error rate stays close to the
    currently observed value, with only a small linear adjustment
    proportional to the fractional growth in users:

        predicted = current_error_rate * (1 + ERROR_RATE_GRADUAL_SENSITIVITY * (N/current_users - 1))

    Beyond the saturation point, error rate increases roughly linearly
    with the fractional distance past saturation:

        error_at_saturation = current_error_rate * (1 + ERROR_RATE_GRADUAL_SENSITIVITY * (saturation_point/current_users - 1))
        predicted = error_at_saturation + ERROR_RATE_POST_SATURATION_SENSITIVITY * (N - saturation_point) / saturation_point

    Clamped to [0, 100]. If saturation_point is unavailable, the gradual
    model is used for all user levels.

    Args:
        current_error_rate: Currently observed error rate (%).
        users: N, the future user level being evaluated.
        current_users: Currently observed concurrent user count.
        saturation_point: USL saturation point, or None.

    Returns:
        Predicted error rate, clamped to [0, 100].

    Raises:
        ScalabilityCalculationError: If current_users <= 0.
    """
    if current_users <= 0:
        raise ScalabilityCalculationError("current_users must be greater than zero to predict error_rate.")

    def _gradual(n: float) -> float:
        growth_ratio = (n / current_users) - 1.0
        return current_error_rate * (1.0 + ERROR_RATE_GRADUAL_SENSITIVITY * growth_ratio)

    if saturation_point is None or users <= saturation_point:
        predicted = _gradual(users)
    else:
        error_at_saturation = _gradual(saturation_point)
        fractional_excess = (users - saturation_point) / saturation_point
        predicted = error_at_saturation + ERROR_RATE_POST_SATURATION_SENSITIVITY * fractional_excess

    return max(0.0, min(100.0, predicted))


def determine_saturation_risk(
    users: float,
    safe_users: float,
    saturation_point: Optional[float],
    optimal_users: Optional[float],
) -> str:
    """
    Classify saturation risk for a future user level.

    Rules (evaluated in order):
        * users < safe_users                     -> "Low"
        * safe_users <= users < saturation_point  -> "Medium"
        * users >= optimal_users (the point       -> "Critical"
          beyond which USL predicts throughput
          itself starts declining -- the "beyond
          peak throughput region")
        * otherwise (users >= saturation_point    -> "High"
          but below optimal_users, or
          optimal_users is unknown)

    If saturation_point is unavailable, only the safe_users boundary is
    used to distinguish "Low" from a non-Low risk, and that non-Low risk
    is "High" unless optimal_users indicates "Critical" -- since without
    a known saturation point there is no meaningful "Medium" band to
    report.

    Args:
        users: N, the future user level being evaluated.
        safe_users: Safe operating capacity (from capacity_results).
        saturation_point: USL saturation point, or None.
        optimal_users: USL theoretical peak user count, or None.

    Returns:
        One of "Low", "Medium", "High", "Critical".
    """
    if users < safe_users:
        return "Low"

    if optimal_users is not None and users >= optimal_users:
        return "Critical"

    if saturation_point is not None and users < saturation_point:
        return "Medium"

    return "High"


def classify_prediction(capacity_utilization: float) -> str:
    """
    Classify a predicted load level based on capacity utilization (%).

        < 70%       -> "Stable"
        70% - <90%  -> "Near Capacity"
        90% - <=110% -> "Overloaded"
        > 110%      -> "Collapsed"

    Args:
        capacity_utilization: Result of calculate_capacity_utilization().

    Returns:
        One of "Stable", "Near Capacity", "Overloaded", "Collapsed".
    """
    if capacity_utilization < CLASSIFICATION_STABLE_MAX_PERCENT:
        return "Stable"
    if capacity_utilization < CLASSIFICATION_NEAR_CAPACITY_MAX_PERCENT:
        return "Near Capacity"
    if capacity_utilization <= CLASSIFICATION_OVERLOADED_MAX_PERCENT:
        return "Overloaded"
    return "Collapsed"


# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------

def generate_summary(predictions: List[Dict[str, Any]], safe_users: float) -> Dict[str, Any]:
    """
    Build the summary section from a list of already-computed per-level
    predictions (assumed sorted ascending by "users").

    Args:
        predictions: List of prediction dicts, each containing at least
            "users", "predicted_cpu", "predicted_memory",
            "predicted_response_time", and "classification".
        safe_users: Safe operating capacity (from capacity_results),
            used as the "safe_prediction_limit" and as the fallback for
            "recommended_max_users" if every predicted level is already
            overloaded or collapsed.

    Returns:
        Summary dictionary:
            {
                "maximum_predicted_users": ...,
                "safe_prediction_limit": ...,
                "first_overloaded_point": ...,
                "first_collapsed_point": ...,
                "recommended_max_users": ...,
                "highest_cpu": ...,
                "highest_memory": ...,
                "highest_response_time": ...
            }
    """
    maximum_predicted_users = max(p["users"] for p in predictions)

    first_overloaded_point = next(
        (p["users"] for p in predictions if p["classification"] == "Overloaded"), None
    )
    first_collapsed_point = next(
        (p["users"] for p in predictions if p["classification"] == "Collapsed"), None
    )

    acceptable_levels = [p["users"] for p in predictions if p["classification"] in ("Stable", "Near Capacity")]
    recommended_max_users = max(acceptable_levels) if acceptable_levels else safe_users

    highest_cpu = max(p["predicted_cpu"] for p in predictions)
    highest_memory = max(p["predicted_memory"] for p in predictions)
    highest_response_time = max(p["predicted_response_time"] for p in predictions)
    highest_disk_io = max(prediction["predicted_disk_io"]for prediction in predictions)
    highest_network_io = max(prediction["predicted_network_io"]for prediction in predictions)

    return {
        "maximum_predicted_users": round(maximum_predicted_users, OUTPUT_DECIMAL_PLACES),
        "safe_prediction_limit": round(safe_users, OUTPUT_DECIMAL_PLACES),
        "first_overloaded_point": (
            round(first_overloaded_point, OUTPUT_DECIMAL_PLACES) if first_overloaded_point is not None else None
        ),
        "first_collapsed_point": (
            round(first_collapsed_point, OUTPUT_DECIMAL_PLACES) if first_collapsed_point is not None else None
        ),
        "recommended_max_users": round(recommended_max_users, OUTPUT_DECIMAL_PLACES),
        "highest_cpu": round(highest_cpu, OUTPUT_DECIMAL_PLACES),
        "highest_memory": round(highest_memory, OUTPUT_DECIMAL_PLACES),
        "highest_response_time": round(highest_response_time, OUTPUT_DECIMAL_PLACES),
        "highest_disk_io": round(highest_disk_io, OUTPUT_DECIMAL_PLACES),
        "highest_network_io": round(highest_network_io, OUTPUT_DECIMAL_PLACES),
    }


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def predict_scalability(
    usl_results: Dict[str, Any],
    capacity_results: Dict[str, Any],
    runtime_metrics: Dict[str, Any],
    prediction_targets: List[float],
) -> Dict[str, Any]:
    """
    Predict application behavior at future, untested user loads by
    evaluating the already-fitted USL curve and scaling current runtime
    metrics accordingly. This is the sole public entry point of the
    module; all other functions are supporting helpers.

    Args:
        usl_results: Output of usl.run_usl_analysis() (or equivalent
            dict) containing at least "sigma", "kappa", and
            "baseline_throughput" (reused, never refit), plus
            "peak_throughput", "optimal_users", and "saturation_point".
        capacity_results: Output of capacity.analyze_capacity() (or
            equivalent dict) containing at least "safe_users".
        runtime_metrics: Latest observed runtime metrics, containing
            "current_users", "throughput", "response_time", "cpu_usage",
            "memory_usage", and "error_rate".
        prediction_targets: Non-empty list of future user levels to
            predict for (e.g. [600, 700, ..., 5000]).

    Returns:
        {
            "predictions": [
                {
                    "users": ...,
                    "predicted_throughput": ...,
                    "predicted_cpu": ...,
                    "predicted_memory": ...,
                    "predicted_response_time": ...,
                    "predicted_error_rate": ...,
                    "scalability_efficiency": ...,
                    "capacity_utilization": ...,
                    "saturation_risk": ...,
                    "classification": ...
                },
                ...
            ],
            "summary": {
                "maximum_predicted_users": ...,
                "safe_prediction_limit": ...,
                "first_overloaded_point": ...,
                "first_collapsed_point": ...,
                "recommended_max_users": ...,
                "highest_cpu": ...,
                "highest_memory": ...,
                "highest_response_time": ...
            }
        }

        Predictions are returned sorted ascending by "users" regardless
        of the input order of prediction_targets, so that "first
        overloaded/collapsed point" in the summary is well-defined as
        load increases. All numeric values are rounded to
        OUTPUT_DECIMAL_PLACES (2) decimal places.

    Raises:
        ScalabilityValidationError: If any input fails validation.
        ScalabilityCalculationError: If a calculation cannot be performed
            (e.g. pathological fitted parameters).
    """
    validated = _validate_inputs(usl_results, capacity_results, runtime_metrics, prediction_targets)

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

    predictions: List[Dict[str, Any]] = []
    for users in sorted(validated["targets"]):
        predicted_throughput = predict_throughput(users, sigma, kappa, baseline_throughput)
        predicted_cpu = predict_cpu(current_cpu, predicted_throughput, current_throughput)
        predicted_memory = predict_memory(current_memory, users, current_users)
        predicted_response_time = predict_response_time(current_response_time, users, current_users, saturation_point)
        predicted_error_rate = predict_error_rate(current_error_rate, users, current_users, saturation_point)
        predicted_disk_io = predict_disk_io(runtime_metrics["disk_io"],runtime_metrics["throughput"],predicted_throughput,)
        predicted_network_io = predict_network_io(runtime_metrics["network_io"],runtime_metrics["throughput"],predicted_throughput,)
        scalability_efficiency = calculate_efficiency(predicted_throughput, baseline_throughput, users)
        capacity_utilization = calculate_capacity_utilization(users, safe_users)
        saturation_risk = determine_saturation_risk(users, safe_users, saturation_point, optimal_users)
        classification = classify_prediction(capacity_utilization)

        predictions.append({
            "users": round(users, OUTPUT_DECIMAL_PLACES),
            "predicted_throughput": round(predicted_throughput, OUTPUT_DECIMAL_PLACES),
            "predicted_cpu": round(predicted_cpu, OUTPUT_DECIMAL_PLACES),
            "predicted_memory": round(predicted_memory, OUTPUT_DECIMAL_PLACES),
            "predicted_response_time": round(predicted_response_time, OUTPUT_DECIMAL_PLACES),
            "predicted_error_rate": round(predicted_error_rate, OUTPUT_DECIMAL_PLACES),
            "predicted_disk_io": round(predicted_disk_io, OUTPUT_DECIMAL_PLACES),
            "predicted_network_io": round(predicted_network_io, OUTPUT_DECIMAL_PLACES),
            "scalability_efficiency": round(scalability_efficiency, OUTPUT_DECIMAL_PLACES),
            "capacity_utilization": round(capacity_utilization, OUTPUT_DECIMAL_PLACES),
            "saturation_risk": saturation_risk,
            "classification": classification,
        })

    summary = generate_summary(predictions, safe_users)

    return {
        "predictions": predictions,
        "summary": summary,
    }