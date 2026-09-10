from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional


class QueueingValidationError(ValueError):
    """Bad input data for the queueing model."""


class QueueingCalculationError(RuntimeError):
    """A queueing calculation received a mathematically invalid argument
    (distinct from the expected, gracefully-handled rho >= 1 instability
    case, which never raises)."""

VALID_SERVICE_RATE_SOURCES = {
    "measured",
    "derived_from_service_time",
    "estimated_from_lowest_load_response_time",
}


# --------------------------------------------------------------------------
# Validated workload container
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class QueueWorkload:
    """
    Validated workload ready for queueing calculations.

    arrival_rate: requests/sec (lambda), > 0
    service_rate: requests/sec (mu), > 0 - the rate of ONE server/worker
    service_rate_source:
        "measured" if service_rate was directly measured,
        "derived_from_service_time" if service_rate was computed as
        1/service_time, or
        "estimated_from_lowest_load_response_time" if service capacity
        was estimated from the lowest-load Locust response time.

        The latter is an approximation because Locust response time
        represents end-to-end request latency, not pure server processing
        time.
    num_servers: number of parallel servers/workers sharing the queue.
        Defaults to 1 (M/M/1 - a single busy queue). If your deployment
        runs multiple gunicorn workers / container replicas behind one
        arrival stream, set this to the real worker count instead - a
        single-server model systematically overestimates queueing delay
        and understates stable capacity compared to several parallel
        servers handling the same aggregate load (M/M/c).
    """
    arrival_rate: float
    service_rate: float
    service_rate_source: str
    num_servers: int
    metadata: Dict[str, Any]

    @staticmethod
    def from_raw(data: Dict[str, Any]) -> "QueueWorkload":
        """
        Validation rules:
            1. "arrival_rate" must be present, numeric, finite, > 0.
            2. Exactly one service-capacity input must be present:
               either "service_rate" directly, or "service_time" (from
               which service_rate = 1 / service_time is derived).
               Response time and throughput are NOT accepted as
               stand-ins for service_rate - W = waiting_time +
               service_time, so using response time as service time
               would understate mu and make rho meaningless.
            3. "num_servers" is optional, defaults to 1, must be a
               positive whole number if provided.
            4. "metadata" is passed through unchanged.

        Deliberately NOT validated: arrival_rate >= num_servers * service_rate.
        That's an unstable-but-real system state, not invalid input - it's
        handled gracefully downstream during calculation/classification.
        """
        if not isinstance(data, dict):
            raise QueueingValidationError("Input must be a dictionary.")

        if "arrival_rate" not in data:
            raise QueueingValidationError("Missing required field: 'arrival_rate'.")

        arrival_rate_raw = data["arrival_rate"]
        if isinstance(arrival_rate_raw, bool) or not isinstance(arrival_rate_raw, (int, float)):
            raise QueueingValidationError(
                f"'arrival_rate' must be a numeric value (int or float), got {type(arrival_rate_raw).__name__}."
            )
        arrival_rate = float(arrival_rate_raw)
        if not math.isfinite(arrival_rate):
            raise QueueingValidationError("'arrival_rate' must be a finite number.")
        if arrival_rate <= 0:
            raise QueueingValidationError("'arrival_rate' must be greater than zero.")

        has_service_rate = (
            "service_rate" in data
            and data["service_rate"] is not None
        )

        has_service_time = (
            "service_time" in data
            and data["service_time"] is not None
        )

        if has_service_rate and has_service_time:
            raise QueueingValidationError(
                "Provide either 'service_rate' or 'service_time', not both."
            )

        if not has_service_rate and not has_service_time:
            raise QueueingValidationError(
                "Queueing analysis requires either 'service_rate' or 'service_time'. "
                "Throughput and response time are not valid substitutes - use "
                "analyze_queue_safe()/unavailable_queueing_result() if a real "
                "service-time measurement isn't available yet."
            )

        if has_service_rate:
            service_rate_raw = data["service_rate"]
        
            if isinstance(service_rate_raw, bool) or not isinstance(
                service_rate_raw, (int, float)
            ):
                raise QueueingValidationError(
                    f"'service_rate' must be a numeric value (int or float), "
                    f"got {type(service_rate_raw).__name__}."
                )
        
            service_rate = float(service_rate_raw)
        
            if not math.isfinite(service_rate):
                raise QueueingValidationError(
                    "'service_rate' must be a finite number."
                )
        
            if service_rate <= 0:
                raise QueueingValidationError(
                    "'service_rate' must be greater than zero."
                )
        
            # ---------------------------------------------------------
            # Service-rate provenance
            # ---------------------------------------------------------
        
            service_rate_source = data.get(
                "service_rate_source",
                "measured"
            )
        
            if service_rate_source not in VALID_SERVICE_RATE_SOURCES:
                raise QueueingValidationError(
                    f"Invalid service_rate_source: "
                    f"{service_rate_source!r}. "
                    f"Expected one of: "
                    f"{sorted(VALID_SERVICE_RATE_SOURCES)}"
                )
        else:
            service_time_raw = data["service_time"]
            if isinstance(service_time_raw, bool) or not isinstance(service_time_raw, (int, float)):
                raise QueueingValidationError(
                    f"'service_time' must be a numeric value (int or float), got {type(service_time_raw).__name__}."
                )
            service_time = float(service_time_raw)
            if not math.isfinite(service_time):
                raise QueueingValidationError("'service_time' must be a finite number.")
            if service_time <= 0:
                raise QueueingValidationError("'service_time' must be greater than zero.")
            service_time_unit = data.get("service_time_unit", "s")
            if not isinstance(service_time_unit, str):
                raise QueueingValidationError("'service_time_unit' must be a string.")

            _TIME_UNIT_TO_SECONDS = {
                "s": 1.0,
                "sec": 1.0,
                "seconds": 1.0,
                "ms": 1.0 / 1_000.0,
                "milliseconds": 1.0 / 1_000.0,
                "us": 1.0 / 1_000_000.0,
                "microseconds": 1.0 / 1_000_000.0,
            }

            normalized_unit = service_time_unit.strip().lower()
            if normalized_unit not in _TIME_UNIT_TO_SECONDS:
                raise QueueingValidationError(
                    f"Unsupported service_time_unit '{service_time_unit}'."
                )

            service_time *= _TIME_UNIT_TO_SECONDS[normalized_unit]
            service_rate = 1.0 / service_time
            service_rate_source = "derived_from_service_time"

        num_servers_raw = data.get("num_servers", 1)
        if isinstance(num_servers_raw, bool) or not isinstance(num_servers_raw, (int, float)):
            raise QueueingValidationError(
                f"'num_servers' must be a numeric value (int or float), got {type(num_servers_raw).__name__}."
            )
        if not math.isfinite(num_servers_raw):
            raise QueueingValidationError("'num_servers' must be finite.")
        if num_servers_raw < 1:
            raise QueueingValidationError("'num_servers' must be at least 1.")
        if float(num_servers_raw) != int(num_servers_raw):
            raise QueueingValidationError("'num_servers' must be a whole number.")
        num_servers = int(num_servers_raw)

        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise QueueingValidationError("'metadata' must be a dictionary if provided.")

        return QueueWorkload(
            arrival_rate=arrival_rate,
            service_rate=service_rate,
            service_rate_source=service_rate_source,
            num_servers=num_servers,
            metadata=metadata,
        )


# --------------------------------------------------------------------------
# Core M/M/1 equations (pure functions, no state)
# --------------------------------------------------------------------------

def calculate_utilization(arrival_rate: float, service_rate: float, num_servers: float = 1) -> float:
    """
    rho = lambda / (c * mu) - per-server utilization, where c is the
    number of parallel servers. Defaults to c=1 (identical to the
    original M/M/1-only behavior). May legitimately be >= 1 (unstable
    system).
    """
    if service_rate <= 0:
        raise QueueingCalculationError("service_rate must be greater than zero to calculate utilization.")
    if arrival_rate < 0:
        raise QueueingCalculationError("arrival_rate must not be negative.")
    if num_servers < 1:
        raise QueueingCalculationError("num_servers must be at least 1.")
    return arrival_rate / (num_servers * service_rate)


def calculate_queue_length(rho: float) -> float:
    """M/M/1: Lq = rho^2 / (1 - rho). Returns inf when rho >= 1."""
    if rho < 0:
        raise QueueingCalculationError("rho must not be negative.")
    if rho >= 1:
        return math.inf
    return (rho ** 2) / (1.0 - rho)


def calculate_requests_in_system(rho: float) -> float:
    """M/M/1: L = rho / (1 - rho). Returns inf when rho >= 1."""
    if rho < 0:
        raise QueueingCalculationError("rho must not be negative.")
    if rho >= 1:
        return math.inf
    return rho / (1.0 - rho)


def calculate_waiting_time(queue_length: float, arrival_rate: float) -> float:
    """Wq = Lq / lambda. Same formula for M/M/1 and M/M/c."""
    if arrival_rate <= 0:
        raise QueueingCalculationError("arrival_rate must be greater than zero to calculate waiting_time.")
    if queue_length < 0:
        raise QueueingCalculationError("queue_length must not be negative.")
    if math.isinf(queue_length):
        return math.inf
    return queue_length / arrival_rate


def calculate_system_time(requests_in_system: float, arrival_rate: float) -> float:
    """W = L / lambda. Same formula for M/M/1 and M/M/c."""
    if arrival_rate <= 0:
        raise QueueingCalculationError("arrival_rate must be greater than zero to calculate system_time.")
    if requests_in_system < 0:
        raise QueueingCalculationError("requests_in_system must not be negative.")
    if math.isinf(requests_in_system):
        return math.inf
    return requests_in_system / arrival_rate


def calculate_service_time(service_rate: float) -> float:
    """Service Time = 1 / mu (per server - independent of server count)."""
    if service_rate <= 0:
        raise QueueingCalculationError("service_rate must be greater than zero to calculate service_time.")
    return 1.0 / service_rate


def calculate_idle_probability(rho: float) -> float:
    """M/M/1: P0 = 1 - rho, clamped to 0 for rho >= 1 (overloaded server is never idle)."""
    if rho < 0:
        raise QueueingCalculationError("rho must not be negative.")
    return max(0.0, 1.0 - rho)


# --------------------------------------------------------------------------
# M/M/c equations (multi-server queueing, via the Erlang C formula)
# --------------------------------------------------------------------------
#
# A real Flask/gunicorn deployment usually runs several worker
# processes/replicas sharing one incoming request stream, not a single
# server. Modelling that as M/M/1 systematically overstates queueing
# delay: c parallel servers each handling a share of the load clear
# queued work much faster than one server handling all of it. M/M/c
# captures that, and reduces to exactly the M/M/1 formulas above when
# num_servers = 1 (verified: with c=1 the Erlang C terms below simplify
# algebraically to P0 = 1-rho and Lq = rho^2/(1-rho)).

def _mmc_terms(offered_load: float, num_servers: int) -> Dict[str, float]:
    """
    Shared iterative computation for M/M/c: builds

        sum_{n=0}^{c-1} a^n/n!    and    (a^c/c!) / (1 - rho)

    using running ratios rather than raw factorials/powers, so it stays
    numerically stable even for larger server counts. offered_load (a) is
    lambda/mu in Erlangs - NOT the same as per-server utilization
    rho = a / num_servers.

    Returns {"rho", "p0", "erlang_c"} - erlang_c is the Erlang C
    probability that an arriving request finds all servers busy and has
    to wait.
    """
    if num_servers < 1:
        raise QueueingCalculationError("num_servers must be at least 1.")
    if offered_load < 0:
        raise QueueingCalculationError("offered_load must not be negative.")

    rho = offered_load / num_servers

    if rho >= 1:
        # Unstable/critically loaded: every arrival effectively waits,
        # system is never empty. Matches the M/M/1 inf-handling pattern.
        return {"rho": rho, "p0": 0.0, "erlang_c": 1.0}

    term = 1.0       # a^0 / 0!
    summation = 1.0
    for n in range(1, num_servers):
        term *= offered_load / n
        summation += term
    term *= offered_load / num_servers  # now a^c / c!

    erlang_term = term / (1.0 - rho)
    p0 = 1.0 / (summation + erlang_term)
    erlang_c = erlang_term * p0

    return {"rho": rho, "p0": p0, "erlang_c": erlang_c}


def calculate_erlang_c_probability(offered_load: float, num_servers: int) -> float:
    """Probability an arriving request has to wait (all c servers busy)."""
    return _mmc_terms(offered_load, num_servers)["erlang_c"]


def calculate_queue_length_mmc(offered_load: float, num_servers: int) -> float:
    """M/M/c: Lq = C(c,a) * rho / (1 - rho). Returns inf when rho >= 1."""
    terms = _mmc_terms(offered_load, num_servers)
    if terms["rho"] >= 1:
        return math.inf
    return terms["erlang_c"] * terms["rho"] / (1.0 - terms["rho"])


def calculate_requests_in_system_mmc(queue_length: float, offered_load: float) -> float:
    """M/M/c: L = Lq + a (a = lambda/mu = average number of busy servers)."""
    if math.isinf(queue_length):
        return math.inf
    if offered_load < 0:
        raise QueueingCalculationError("offered_load must not be negative.")
    return queue_length + offered_load


def calculate_idle_probability_mmc(offered_load: float, num_servers: int) -> float:
    """M/M/c: P0, probability the entire system (all c servers) is empty."""
    return _mmc_terms(offered_load, num_servers)["p0"]


# --------------------------------------------------------------------------
# Classification thresholds
# --------------------------------------------------------------------------

@dataclass
class QueueStatusThresholds:
    """
    Upper bounds (exclusive) for each queue-status band, in terms of rho:
        rho < idle_max       -> "Idle"
        rho < light_max      -> "Light"
        rho < moderate_max   -> "Moderate"
        rho < busy_max       -> "Busy"
        rho < congested_max  -> "Congested"
        rho >= congested_max -> "Critical"

    rho here is always per-server utilization, so these bands apply the
    same way whether num_servers is 1 or 10 - "how busy is each worker",
    not "how busy is the fleet as a whole".
    """
    idle_max: float = 0.1
    light_max: float = 0.3
    moderate_max: float = 0.5
    busy_max: float = 0.9
    congested_max: float = 1.0


@dataclass
class StabilityThresholds:
    """
    rho < stable_max           -> "Stable"
    rho < near_saturation_max  -> "Near Saturation"
    rho >= near_saturation_max -> "Unstable"

    The "Near Saturation" band exists because Lq/Wq grow roughly as
    1/(1-rho), so a system can be technically stable (rho < 1) yet
    practically unusable well before rho actually hits 1.
    """
    stable_max: float = 0.8
    near_saturation_max: float = 1.0


@dataclass
class CongestionRiskThresholds:
    """
    rho < low_max     -> "Low"
    rho < medium_max  -> "Medium"
    rho < high_max    -> "High"
    rho >= high_max   -> "Critical"

    Independently of rho, queue_length >= queue_length_critical (or
    infinite) also escalates to "Critical" - a system at rho=0.98 can
    already have a large, fast-growing queue before crossing rho=1.
    """
    low_max: float = 0.5
    medium_max: float = 0.8
    high_max: float = 1.0
    queue_length_critical: float = 20.0


def classify_queue_status(rho: float, thresholds: Optional[QueueStatusThresholds] = None) -> str:
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


def classify_stability(rho: float, thresholds: Optional[StabilityThresholds] = None) -> str:
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
# Signals (indicators only - no recommendations generated here)
# --------------------------------------------------------------------------

@dataclass
class SignalThresholds:
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
    return {
        "utilization_high": rho >= thresholds.utilization_high_min,
        "near_saturation": stability == "Near Saturation",
        "unstable_system": stability == "Unstable",
        "high_wait_time": math.isinf(waiting_time) or waiting_time >= thresholds.high_wait_time_min_seconds,
        "long_queue": math.isinf(queue_length) or queue_length >= thresholds.long_queue_min_requests,
        "congestion_detected": congestion_risk in ("High", "Critical"),
    }


# --------------------------------------------------------------------------
# Unavailable-result helper
# --------------------------------------------------------------------------

def unavailable_queueing_result(
    reason: str,
    arrival_rate: Optional[float] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Explicit "we don't have what we need" result, for when no real
    service_rate/service_time is available yet. Returning this instead
    of quietly treating throughput as service_rate keeps the pipeline
    from producing a queueing analysis that looks legitimate but isn't.
    """
    return {
        "queueing_analysis": "unavailable",
        "reason": reason,
        "arrival_rate": arrival_rate,
        "metadata": metadata or {},
    }


# --------------------------------------------------------------------------
# Analyzer
# --------------------------------------------------------------------------

class QueueingAnalyzer:
    """
    Queueing approximation for a single workload snapshot - not a literal
    claim that the system is a textbook Poisson-arrival/exponential-
    service queue. Real Flask/Docker deployments have connection pools,
    DB calls, etc. This is a standard approximation used to estimate
    utilization, queue growth, waiting time, and stability from observed
    characteristics.

    Uses M/M/1 (single server) by default. If num_servers > 1 is
    supplied in the input, switches to M/M/c (multi-server, via the
    Erlang C formula) instead - important if the deployment runs
    multiple worker processes or replicas behind one arrival stream,
    since M/M/1 would otherwise overstate queueing delay for that setup.
    """

    def __init__(
        self,
        queue_status_thresholds: Optional[QueueStatusThresholds] = None,
        stability_thresholds: Optional[StabilityThresholds] = None,
        congestion_risk_thresholds: Optional[CongestionRiskThresholds] = None,
        signal_thresholds: Optional[SignalThresholds] = None,
    ) -> None:
        self.queue_status_thresholds = queue_status_thresholds or QueueStatusThresholds()
        self.stability_thresholds = stability_thresholds or StabilityThresholds()
        self.congestion_risk_thresholds = congestion_risk_thresholds or CongestionRiskThresholds()
        self.signal_thresholds = signal_thresholds or SignalThresholds()

    def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Args:
            data: {"arrival_rate": ..., "service_rate": ...} or
                  {"arrival_rate": ..., "service_time": ...}, plus optional
                  "num_servers" (default 1) and "metadata". Exactly one of
                  service_rate/service_time is required - see
                  QueueWorkload.from_raw().

        Raises:
            QueueingValidationError: bad/missing input.
        """
        workload = QueueWorkload.from_raw(data)

        offered_load = workload.arrival_rate / workload.service_rate  # a = lambda/mu, in Erlangs
        rho = calculate_utilization(workload.arrival_rate, workload.service_rate, workload.num_servers)

        if workload.num_servers == 1:
            # Exact M/M/1 closed forms - unchanged from before, so every
            # existing single-server caller gets byte-identical numbers.
            queue_length = calculate_queue_length(rho)
            requests_in_system = calculate_requests_in_system(rho)
            idle_probability = calculate_idle_probability(rho)
        else:
            terms = _mmc_terms(offered_load, workload.num_servers)
            if terms["rho"] >= 1:
                queue_length = math.inf
                requests_in_system = math.inf
            else:
                queue_length = terms["erlang_c"] * terms["rho"] / (1.0 - terms["rho"])
                requests_in_system = queue_length + offered_load
            idle_probability = terms["p0"]

        waiting_time = calculate_waiting_time(queue_length, workload.arrival_rate)
        system_time = calculate_system_time(requests_in_system, workload.arrival_rate)
        service_time = calculate_service_time(workload.service_rate)

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
            "service_rate_source": workload.service_rate_source,
            "num_servers": workload.num_servers,
            "queueing_model": "M/M/1" if workload.num_servers == 1 else f"M/M/{workload.num_servers}",
            "offered_load": offered_load,
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
            "units": {
                "arrival_rate": "requests/sec",
                "service_rate": "requests/sec",
                "offered_load": "erlangs",
                "utilization": "ratio",
                "queue_length": "requests",
                "requests_in_system": "requests",
                "waiting_time": "seconds",
                "system_time": "seconds",
                "service_time": "seconds",
                "idle_probability": "ratio",
            },
            "metadata": workload.metadata,
        }


# --------------------------------------------------------------------------
# Convenience functional wrappers
# --------------------------------------------------------------------------

def analyze_queue(
    data: Dict[str, Any],
    queue_status_thresholds: Optional[QueueStatusThresholds] = None,
    stability_thresholds: Optional[StabilityThresholds] = None,
    congestion_risk_thresholds: Optional[CongestionRiskThresholds] = None,
    signal_thresholds: Optional[SignalThresholds] = None,
) -> Dict[str, Any]:
    """One-shot: validate + analyze. Raises if service_rate/service_time is missing."""
    analyzer = QueueingAnalyzer(
        queue_status_thresholds=queue_status_thresholds,
        stability_thresholds=stability_thresholds,
        congestion_risk_thresholds=congestion_risk_thresholds,
        signal_thresholds=signal_thresholds,
    )
    return analyzer.analyze(data)


def analyze_queue_safe(data: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
    """
    Same as analyze_queue(), except when neither service_rate nor
    service_time is present in `data`, it returns
    unavailable_queueing_result() instead of raising. Meant for pipeline
    code that wants to keep running (and report "unavailable") when
    real service-time instrumentation isn't wired up yet, rather than
    crash or silently fabricate a service_rate from throughput.
    """
    has_service_rate = isinstance(data, dict) and data.get("service_rate") is not None
    has_service_time = isinstance(data, dict) and data.get("service_time") is not None

    if not has_service_rate and not has_service_time:
        return unavailable_queueing_result(
            reason="No service_rate or service_time provided - queueing analysis needs a real "
                   "service-time measurement, not throughput or response time.",
            arrival_rate=data.get("arrival_rate") if isinstance(data, dict) else None,
            metadata=data.get("metadata") if isinstance(data, dict) else None,
        )

    return analyze_queue(data, **kwargs)