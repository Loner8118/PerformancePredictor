from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------

class QueueingValidationError(ValueError):
    """Raised when input data fails validation rules for the M/M/1 model."""


class QueueingCalculationError(RuntimeError):
    """Raised when a queueing calculation cannot be performed because a
    helper function received a mathematically invalid argument (distinct
    from the *expected* and gracefully-handled rho >= 1 instability
    case, which never raises)."""


# --------------------------------------------------------------------------
# Validated workload container
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class QueueWorkload:
    """
    A validated workload observation ready for M/M/1 queueing
    calculations.

    Attributes:
        arrival_rate: Requests per second (lambda), guaranteed > 0.
        service_rate: Requests per second (mu), guaranteed > 0.
        metadata: Arbitrary passthrough metadata (does not affect
            calculations).
    """
    arrival_rate: float
    service_rate: float
    metadata: Dict[str, Any]

    @staticmethod
    def from_raw(data: Dict[str, Any]) -> "QueueWorkload":
        """
        Validate a raw input dictionary into a QueueWorkload.

        Validation rules enforced:
            1. "arrival_rate" and "service_rate" must be present.
            2. Both values must be numeric (int or float, not bool).
            3. Both values must be strictly greater than zero.
            4. Any "metadata" key is passed through unchanged and never
               participates in calculations.

        Note deliberately NOT validated here: arrival_rate >= service_rate.
        That condition describes an unstable-but-real system state, not
        an invalid input, and is handled gracefully downstream during
        calculation and classification rather than rejected here.

        Args:
            data: Raw input dictionary, e.g.:
                {
                    "arrival_rate": 450,
                    "service_rate": 520,
                    "metadata": {...}   # optional
                }

        Returns:
            A validated QueueWorkload instance.

        Raises:
            QueueingValidationError: If any validation rule is violated.
        """
        if not isinstance(data, dict):
            raise QueueingValidationError("Input must be a dictionary.")

        if "arrival_rate" not in data:
            raise QueueingValidationError("Missing required field: 'arrival_rate'.")
        if "service_rate" not in data:
            raise QueueingValidationError("Missing required field: 'service_rate'.")

        arrival_rate_raw = data["arrival_rate"]
        service_rate_raw = data["service_rate"]

        # Numeric-only check. Explicitly reject bool, since bool is a
        # subclass of int in Python and would otherwise silently pass.
        for name, value in (("arrival_rate", arrival_rate_raw), ("service_rate", service_rate_raw)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise QueueingValidationError(
                    f"'{name}' must be a numeric value (int or float), got {type(value).__name__}."
                )

        arrival_rate = float(arrival_rate_raw)
        service_rate = float(service_rate_raw)

        if arrival_rate <= 0:
            raise QueueingValidationError("'arrival_rate' must be greater than zero.")
        if service_rate <= 0:
            raise QueueingValidationError("'service_rate' must be greater than zero.")

        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise QueueingValidationError("'metadata' must be a dictionary if provided.")

        return QueueWorkload(arrival_rate=arrival_rate, service_rate=service_rate, metadata=metadata)


# --------------------------------------------------------------------------
# Core M/M/1 equations (pure functions, no state)
# --------------------------------------------------------------------------

def calculate_utilization(arrival_rate: float, service_rate: float) -> float:
    """
    Server utilization: rho = lambda / mu

    The fraction of time the server is busy processing requests. This is
    the central parameter of the M/M/1 model; every other quantity below
    is derived from rho.

    Args:
        arrival_rate: lambda, requests/second.
        service_rate: mu, requests/second.

    Returns:
        rho, the utilization ratio (unitless). Note that rho may be >= 1,
        which represents an unstable system and is a valid return value
        here -- it is the caller's responsibility to branch on this when
        computing downstream queue-length/time metrics.

    Raises:
        QueueingCalculationError: If service_rate is not positive
            (division by zero) or arrival_rate is negative.
    """
    if service_rate <= 0:
        raise QueueingCalculationError("service_rate must be greater than zero to calculate utilization.")
    if arrival_rate < 0:
        raise QueueingCalculationError("arrival_rate must not be negative.")
    return arrival_rate / service_rate


def calculate_queue_length(rho: float) -> float:
    """
    Average number of requests waiting in queue (not being served):

        Lq = rho^2 / (1 - rho)

    Args:
        rho: Utilization ratio.

    Returns:
        Lq. If rho >= 1, the queue grows without bound at steady state,
        so float('inf') is returned instead of dividing by a
        non-positive denominator.

    Raises:
        QueueingCalculationError: If rho is negative.
    """
    if rho < 0:
        raise QueueingCalculationError("rho must not be negative.")
    if rho >= 1:
        return math.inf
    return (rho ** 2) / (1.0 - rho)


def calculate_requests_in_system(rho: float) -> float:
    """
    Average number of requests in the system (waiting + being served):

        L = rho / (1 - rho)

    Args:
        rho: Utilization ratio.

    Returns:
        L. If rho >= 1, returns float('inf') for the same reason as
        calculate_queue_length().

    Raises:
        QueueingCalculationError: If rho is negative.
    """
    if rho < 0:
        raise QueueingCalculationError("rho must not be negative.")
    if rho >= 1:
        return math.inf
    return rho / (1.0 - rho)


def calculate_waiting_time(queue_length: float, arrival_rate: float) -> float:
    """
    Average time a request spends waiting in queue before service begins:

        Wq = Lq / lambda

    Args:
        queue_length: Lq, average requests waiting in queue.
        arrival_rate: lambda, requests/second.

    Returns:
        Wq, in seconds. Propagates float('inf') if queue_length is
        infinite (unstable system).

    Raises:
        QueueingCalculationError: If arrival_rate is not positive
            (division by zero) or queue_length is negative.
    """
    if arrival_rate <= 0:
        raise QueueingCalculationError("arrival_rate must be greater than zero to calculate waiting_time.")
    if queue_length < 0:
        raise QueueingCalculationError("queue_length must not be negative.")
    if math.isinf(queue_length):
        return math.inf
    return queue_length / arrival_rate


def calculate_system_time(requests_in_system: float, arrival_rate: float) -> float:
    """
    Average total time a request spends in the system (waiting + being
    served):

        W = L / lambda

    Args:
        requests_in_system: L, average requests in the system.
        arrival_rate: lambda, requests/second.

    Returns:
        W, in seconds. Propagates float('inf') if requests_in_system is
        infinite (unstable system).

    Raises:
        QueueingCalculationError: If arrival_rate is not positive
            (division by zero) or requests_in_system is negative.
    """
    if arrival_rate <= 0:
        raise QueueingCalculationError("arrival_rate must be greater than zero to calculate system_time.")
    if requests_in_system < 0:
        raise QueueingCalculationError("requests_in_system must not be negative.")
    if math.isinf(requests_in_system):
        return math.inf
    return requests_in_system / arrival_rate


def calculate_service_time(service_rate: float) -> float:
    """
    Average time required to service a single request:

        Service Time = 1 / mu

    Args:
        service_rate: mu, requests/second.

    Returns:
        Average service time, in seconds.

    Raises:
        QueueingCalculationError: If service_rate is not positive
            (division by zero).
    """
    if service_rate <= 0:
        raise QueueingCalculationError("service_rate must be greater than zero to calculate service_time.")
    return 1.0 / service_rate


def calculate_idle_probability(rho: float) -> float:
    """
    Probability that the server is idle (no requests being served):

        P0 = 1 - rho

    Args:
        rho: Utilization ratio.

    Returns:
        P0, clamped to a minimum of 0.0. When rho >= 1 the raw formula
        would produce a non-positive (physically meaningless) value; a
        server that is overloaded is never idle, so 0.0 is returned
        instead.

    Raises:
        QueueingCalculationError: If rho is negative.
    """
    if rho < 0:
        raise QueueingCalculationError("rho must not be negative.")
    return max(0.0, 1.0 - rho)


# --------------------------------------------------------------------------
# Classification thresholds
# --------------------------------------------------------------------------

@dataclass
class QueueStatusThresholds:
    """
    Configurable upper bounds (exclusive) for each queue-status band,
    expressed in terms of utilization (rho).

    Classification walks the bands in order and returns the first one
    whose upper bound exceeds rho:

        rho < idle_max         -> "Idle"
        rho < light_max        -> "Light"
        rho < moderate_max     -> "Moderate"
        rho < busy_max         -> "Busy"
        rho < congested_max    -> "Congested"
        rho >= congested_max   -> "Critical"

    Defaults are illustrative starting points and are centralized here
    (rather than scattered as magic numbers) so they can be tuned per
    deployment.
    """
    idle_max: float = 0.1
    light_max: float = 0.3
    moderate_max: float = 0.5
    busy_max: float = 0.9
    congested_max: float = 1.0


@dataclass
class StabilityThresholds:
    """
    Configurable utilization (rho) boundaries for stability
    classification.

        rho < stable_max              -> "Stable"
        rho < near_saturation_max     -> "Near Saturation"
        rho >= near_saturation_max    -> "Unstable"

    Rationale: an M/M/1 queue is only mathematically stable in the
    strict sense when rho < 1; the "Near Saturation" band (default
    0.8 <= rho < 1.0) exists because queue length and waiting time grow
    non-linearly (roughly as 1/(1-rho)) as rho approaches 1, so a system
    can be technically stable yet practically unusable well before
    rho actually reaches 1. rho >= near_saturation_max (default 1.0) is
    always "Unstable", matching the point at which the closed-form
    equations diverge.
    """
    stable_max: float = 0.8
    near_saturation_max: float = 1.0


@dataclass
class CongestionRiskThresholds:
    """
    Configurable thresholds for congestion-risk classification, based
    primarily on utilization (rho) with a secondary escalation trigger
    based on absolute queue length.

        rho < low_max                          -> "Low"
        rho < medium_max                       -> "Medium"
        rho < high_max                         -> "High"
        rho >= high_max                        -> "Critical"

    Additionally, regardless of rho, if the computed queue_length meets
    or exceeds `queue_length_critical` (or is infinite), the risk is
    escalated to "Critical". This secondary check exists because a
    system very close to rho = 1 (e.g. 0.98) can already have a large,
    rapidly growing queue even though it has not technically crossed the
    instability boundary yet.
    """
    low_max: float = 0.5
    medium_max: float = 0.8
    high_max: float = 1.0
    queue_length_critical: float = 20.0


def classify_queue_status(
    rho: float,
    thresholds: Optional[QueueStatusThresholds] = None,
) -> str:
    """
    Classify server load into a qualitative queue-status band based on
    utilization (rho). See QueueStatusThresholds for the band definitions.

    Args:
        rho: Utilization ratio.
        thresholds: Optional custom thresholds.

    Returns:
        One of: "Idle", "Light", "Moderate", "Busy", "Congested", "Critical".
    """
    if thresholds is None:
        thresholds = QueueStatusThresholds()

    if rho < thresholds.idle_max:
        return "Idle"
    if rho < thresholds.light_max:
        return "Light"
    if rho < thresholds.moderate_max:
        return "Moderate"
    if rho < thresholds.busy_max:
        return "Busy"
    if rho < thresholds.congested_max:
        return "Congested"
    return "Critical"


def classify_stability(
    rho: float,
    thresholds: Optional[StabilityThresholds] = None,
) -> str:
    """
    Classify queue stability based on utilization (rho). See
    StabilityThresholds for the band definitions and rationale.

    Args:
        rho: Utilization ratio.
        thresholds: Optional custom thresholds.

    Returns:
        One of: "Stable", "Near Saturation", "Unstable".
    """
    if thresholds is None:
        thresholds = StabilityThresholds()

    if rho < thresholds.stable_max:
        return "Stable"
    if rho < thresholds.near_saturation_max:
        return "Near Saturation"
    return "Unstable"


def classify_congestion_risk(
    rho: float,
    queue_length: float,
    thresholds: Optional[CongestionRiskThresholds] = None,
) -> str:
    """
    Classify congestion risk based on utilization (rho), with a
    secondary escalation based on absolute queue length. See
    CongestionRiskThresholds for rationale.

    Args:
        rho: Utilization ratio.
        queue_length: Lq, average requests waiting in queue (may be inf).
        thresholds: Optional custom thresholds.

    Returns:
        One of: "Low", "Medium", "High", "Critical".
    """
    if thresholds is None:
        thresholds = CongestionRiskThresholds()

    if rho >= thresholds.high_max or math.isinf(queue_length) or queue_length >= thresholds.queue_length_critical:
        return "Critical"
    if rho >= thresholds.medium_max:
        return "High"
    if rho >= thresholds.low_max:
        return "Medium"
    return "Low"


# --------------------------------------------------------------------------
# Signals (indicators only -- no recommendations are generated here)
# --------------------------------------------------------------------------

@dataclass
class SignalThresholds:
    """
    Configurable thresholds used purely to derive boolean/indicator
    signals for downstream modules. Kept separate from the
    classification thresholds above so signal sensitivity can be tuned
    independently of the human-readable classification bands.
    """
    utilization_high_min: float = 0.8
    high_wait_time_min_seconds: float = 0.05
    long_queue_min_requests: float = 5.0


def _generate_signals(
    rho: float,
    queue_length: float,
    waiting_time: float,
    stability: str,
    congestion_risk: str,
    thresholds: SignalThresholds,
) -> Dict[str, bool]:
    """
    Build a dictionary of indicator signals for downstream modules
    (Capacity Planning, Recommendation Engine).

    This function does NOT make recommendations. It only exposes boolean
    signals that later modules can interpret.

    Args:
        rho: Utilization ratio.
        queue_length: Lq (may be inf).
        waiting_time: Wq, in seconds (may be inf).
        stability: Result of classify_stability().
        congestion_risk: Result of classify_congestion_risk().
        thresholds: Signal sensitivity thresholds.

    Returns:
        Dictionary of signal name -> boolean value.
    """
    return {
        "utilization_high": rho >= thresholds.utilization_high_min,
        "near_saturation": stability == "Near Saturation",
        "unstable_system": stability == "Unstable",
        "high_wait_time": math.isinf(waiting_time) or waiting_time >= thresholds.high_wait_time_min_seconds,
        "long_queue": math.isinf(queue_length) or queue_length >= thresholds.long_queue_min_requests,
        "congestion_detected": congestion_risk in ("High", "Critical"),
    }


# --------------------------------------------------------------------------
# Analyzer (orchestrates validation -> calculation -> classification)
# --------------------------------------------------------------------------

class QueueingAnalyzer:
    """
    Orchestrates M/M/1 queueing analysis for a single workload snapshot.

    This class separates concerns internally:
        - Validation:      QueueWorkload.from_raw()
        - Calculation:     calculate_utilization() and related equations
        - Classification:  classify_queue_status() / classify_stability()
                            / classify_congestion_risk()
        - Signal exposure: _generate_signals()

    It is intended to be reusable by other mathematical modules (e.g. a
    Capacity Planning module could instantiate this with custom
    thresholds tuned to its own operating environment).
    """

    def __init__(
        self,
        queue_status_thresholds: Optional[QueueStatusThresholds] = None,
        stability_thresholds: Optional[StabilityThresholds] = None,
        congestion_risk_thresholds: Optional[CongestionRiskThresholds] = None,
        signal_thresholds: Optional[SignalThresholds] = None,
    ) -> None:
        """
        Args:
            queue_status_thresholds: Optional custom queue-status bands.
            stability_thresholds: Optional custom stability bands.
            congestion_risk_thresholds: Optional custom congestion-risk bands.
            signal_thresholds: Optional custom signal sensitivity thresholds.
        """
        self.queue_status_thresholds = queue_status_thresholds or QueueStatusThresholds()
        self.stability_thresholds = stability_thresholds or StabilityThresholds()
        self.congestion_risk_thresholds = congestion_risk_thresholds or CongestionRiskThresholds()
        self.signal_thresholds = signal_thresholds or SignalThresholds()

    def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Run a full M/M/1 queueing analysis on a single workload.

        Args:
            data: Raw workload input, e.g.:
                {
                    "arrival_rate": 450,
                    "service_rate": 520,
                    "metadata": {...}   # optional, passthrough only
                }

        Returns:
            Structured dictionary:
                {
                    "arrival_rate": ...,
                    "service_rate": ...,
                    "utilization": ...,
                    "queue_length": ...,
                    "requests_in_system": ...,
                    "waiting_time": ...,
                    "system_time": ...,
                    "service_time": ...,
                    "idle_probability": ...,
                    "queue_status": ...,
                    "stability": ...,
                    "congestion_risk": ...,
                    "signals": {...},
                    "metadata": {...}
                }

            Note: queue_length, requests_in_system, waiting_time, and
            system_time will be float('inf') when arrival_rate >=
            service_rate (unstable system), per the module's graceful
            instability handling described in the module docstring.

        Raises:
            QueueingValidationError: If input validation fails (missing
                fields, non-numeric values, zero/negative values).
        """
        workload = QueueWorkload.from_raw(data)

        # Core M/M/1 equations, computed in dependency order.
        rho = calculate_utilization(workload.arrival_rate, workload.service_rate)
        queue_length = calculate_queue_length(rho)
        requests_in_system = calculate_requests_in_system(rho)
        waiting_time = calculate_waiting_time(queue_length, workload.arrival_rate)
        system_time = calculate_system_time(requests_in_system, workload.arrival_rate)
        service_time = calculate_service_time(workload.service_rate)
        idle_probability = calculate_idle_probability(rho)

        queue_status = classify_queue_status(rho, self.queue_status_thresholds)
        stability = classify_stability(rho, self.stability_thresholds)
        congestion_risk = classify_congestion_risk(rho, queue_length, self.congestion_risk_thresholds)

        signals = _generate_signals(
            rho=rho,
            queue_length=queue_length,
            waiting_time=waiting_time,
            stability=stability,
            congestion_risk=congestion_risk,
            thresholds=self.signal_thresholds,
        )

        return {
            "arrival_rate": workload.arrival_rate,
            "service_rate": workload.service_rate,
            "utilization": rho,
            "queue_length": queue_length,
            "requests_in_system": requests_in_system,
            "waiting_time": waiting_time,
            "system_time": system_time,
            "service_time": service_time,
            "idle_probability": idle_probability,
            "queue_status": queue_status,
            "stability": stability,
            "congestion_risk": congestion_risk,
            "signals": signals,
            "metadata": workload.metadata,
        }


# --------------------------------------------------------------------------
# Convenience functional wrapper (for simple/one-shot usage)
# --------------------------------------------------------------------------

def analyze_queue(
    data: Dict[str, Any],
    queue_status_thresholds: Optional[QueueStatusThresholds] = None,
    stability_thresholds: Optional[StabilityThresholds] = None,
    congestion_risk_thresholds: Optional[CongestionRiskThresholds] = None,
    signal_thresholds: Optional[SignalThresholds] = None,
) -> Dict[str, Any]:
    """
    One-shot convenience function: validate and analyze a single workload
    snapshot using the M/M/1 queueing model.

    This is the primary entry point intended for use by the Flask layer
    (e.g. a `/api/queueing/analyze` endpoint would call this directly),
    and by other mathematical modules in the pipeline that need to
    convert an arrival rate / service rate pair (measured or
    USL-predicted) into queue behavior metrics.

    Args:
        data: Dict with "arrival_rate" and "service_rate" keys (plus
            optional "metadata").
        queue_status_thresholds: Optional custom queue-status bands.
        stability_thresholds: Optional custom stability bands.
        congestion_risk_thresholds: Optional custom congestion-risk bands.
        signal_thresholds: Optional custom signal sensitivity thresholds.

    Returns:
        Structured M/M/1 queueing analysis dictionary.
    """
    analyzer = QueueingAnalyzer(
        queue_status_thresholds=queue_status_thresholds,
        stability_thresholds=stability_thresholds,
        congestion_risk_thresholds=congestion_risk_thresholds,
        signal_thresholds=signal_thresholds,
    )
    return analyzer.analyze(data)