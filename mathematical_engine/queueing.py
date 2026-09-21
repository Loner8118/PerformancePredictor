from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


# ==========================================================================
# M/M/1 -> M/G/1 upgrade
# ==========================================================================
#
# The original model assumed exponentially-distributed service time (the
# "M" in M/M/1) - i.e. that the coefficient of variation of service time,
# Cs = stdev(S)/mean(S), is always exactly 1. Real request service times
# are usually MORE variable than that (occasional slow DB queries, GC
# pauses, cold caches), which means plain M/M/1 systematically
# UNDERSTATES queueing delay for real workloads.
#
# This module now implements M/G/1 via the Pollaczek-Khinchine (P-K)
# formula, which only needs one extra number - Cs² (squared coefficient
# of variation of service time) - and is otherwise identical to M/M/1:
#
#     Wq = (rho / (1 - rho)) * ((1 + Cs^2) / 2) * S
#
# When Cs² = 1 (exponential service), the (1+Cs²)/2 factor is exactly 1
# and this reduces algebraically to the original M/M/1 formulas - so
# every existing caller that doesn't supply variability data gets
# byte-identical results to before. Arrivals are still assumed Poisson
# (the "M" in M/G/1); relaxing that too (G/G/1) is future work and would
# need inter-arrival-time variability data this pipeline doesn't collect
# yet.
#
# For num_servers > 1, the exact multi-server generalization of P-K has
# no simple closed form, so the same (1+Cs²)/2 correction is applied to
# the M/M/c (Erlang C) queue length as a standard approximation
# (Sakasegawa), holding arrivals Poisson (Ca²=1). This is a widely used
# approximation, not an exact result - documented in the output via
# "queueing_model".
#
# Cs² is estimated from whatever variability data the caller provides,
# in this priority order (see _estimate_service_time_variability()):
#   1. explicit "service_time_cv"                    (most direct)
#   2. explicit "service_time_stdev" + service_time   (mean, stdev)
#   3. "service_time_p50" + "service_time_p95"        (log-normal fit)
#   4. nothing provided -> Cs² = 1.0 ("assumed_exponential", i.e. M/M/1)


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

VALID_CV_SOURCES = {
    "provided_cv",
    "provided_stdev",
    "estimated_lognormal_p50_p95",
    "assumed_exponential",
}

#: Shared by service_time parsing AND the optional variability fields
#: (service_time_stdev / service_time_p50 / service_time_p95), which are
#: interpreted in the same unit as "service_time_unit" regardless of
#: whether the caller supplied "service_rate" or "service_time" for the
#: mean.
_TIME_UNIT_TO_SECONDS = {
    "s": 1.0,
    "sec": 1.0,
    "seconds": 1.0,
    "ms": 1.0 / 1_000.0,
    "milliseconds": 1.0 / 1_000.0,
    "us": 1.0 / 1_000_000.0,
    "microseconds": 1.0 / 1_000_000.0,
}

#: Cs^2 is clamped to this range so a bad/noisy percentile estimate can't
#: blow up queue-length predictions to something absurd. 0 = deterministic
#: service time (M/D/1); 25 -> Cs = 5, already a very heavy-tailed service
#: time distribution - anything estimated beyond that is more likely noise
#: in the input data than a real signal.
_CS_SQUARED_MIN = 0.0
_CS_SQUARED_MAX = 25.0

#: z-score for the 95th percentile of a standard normal distribution, used
#: to fit a log-normal distribution's sigma from the P50/P95 service-time
#: percentiles: ln(p95) - ln(p50) = sigma * Z_0.95.
_Z_SCORE_P95 = 1.645


def _estimate_service_time_variability(
    data: Dict[str, Any],
    service_time_seconds: float,
    unit_to_seconds: Dict[str, float],
    unit_seconds_factor: float,
) -> Tuple[float, str]:
    """
    Estimate Cs^2 (squared coefficient of variation of service time) from
    whatever variability data the caller supplied, in priority order.
    Falls back to Cs^2 = 1.0 (exponential assumption, i.e. plain M/M/1
    behavior) when nothing is provided - this is what keeps every
    existing caller's output unchanged unless they opt in to the more
    accurate M/G/1 model by supplying real variability data.

    All of "service_time_stdev", "service_time_p50", "service_time_p95"
    are interpreted in the same unit as "service_time_unit" (default
    seconds) - the same unit the mean service_time was given in.

    Returns (cs_squared, source) where source is one of VALID_CV_SOURCES.
    """
    # 1. Direct coefficient of variation.
    if data.get("service_time_cv") is not None:
        cv_raw = data["service_time_cv"]
        if isinstance(cv_raw, bool) or not isinstance(cv_raw, (int, float)):
            raise QueueingValidationError(
                f"'service_time_cv' must be a numeric value, got {type(cv_raw).__name__}."
            )
        cv = float(cv_raw)
        if not math.isfinite(cv) or cv < 0:
            raise QueueingValidationError("'service_time_cv' must be a finite number >= 0.")
        return _clamp_cs_squared(cv * cv), "provided_cv"

    # 2. Standard deviation of service time (same unit as service_time).
    if data.get("service_time_stdev") is not None:
        stdev_raw = data["service_time_stdev"]
        if isinstance(stdev_raw, bool) or not isinstance(stdev_raw, (int, float)):
            raise QueueingValidationError(
                f"'service_time_stdev' must be a numeric value, got {type(stdev_raw).__name__}."
            )
        stdev = float(stdev_raw)
        if not math.isfinite(stdev) or stdev < 0:
            raise QueueingValidationError("'service_time_stdev' must be a finite number >= 0.")
        stdev_seconds = stdev * unit_seconds_factor
        if service_time_seconds <= 0:
            raise QueueingCalculationError("service_time must be greater than zero to derive Cs from stdev.")
        cv = stdev_seconds / service_time_seconds
        return _clamp_cs_squared(cv * cv), "provided_stdev"

    # 3. Log-normal fit from P50/P95 service-time percentiles - a
    # standard way to estimate spread for right-skewed latency data
    # without needing raw per-request samples (which this pipeline's
    # Locust CSV export doesn't retain, only aggregated percentiles).
    p50_raw = data.get("service_time_p50")
    p95_raw = data.get("service_time_p95")
    if p50_raw is not None and p95_raw is not None:
        for label, val in (("service_time_p50", p50_raw), ("service_time_p95", p95_raw)):
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise QueueingValidationError(f"'{label}' must be a numeric value, got {type(val).__name__}.")
        p50 = float(p50_raw) * unit_seconds_factor
        p95 = float(p95_raw) * unit_seconds_factor
        if p50 > 0 and p95 > p50 and math.isfinite(p50) and math.isfinite(p95):
            sigma = math.log(p95 / p50) / _Z_SCORE_P95
            cs_squared = math.exp(sigma * sigma) - 1.0
            return _clamp_cs_squared(cs_squared), "estimated_lognormal_p50_p95"
        # p95 <= p50 or non-positive input is inconsistent (percentiles
        # must be non-decreasing) - fall through to the default rather
        # than raise, since this is optional enrichment data, not a
        # required field.

    # 4. No variability data supplied - assume exponential service time,
    # which is exactly the original M/M/1 behavior.
    return 1.0, "assumed_exponential"


def _clamp_cs_squared(cs_squared: float) -> float:
    return max(_CS_SQUARED_MIN, min(_CS_SQUARED_MAX, cs_squared))


# --- Validated workload container ---

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
    service_time_cv_squared: float
    service_time_cv_source: str
    observed_response_time: Optional[float]

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

            normalized_unit = QueueWorkload._normalize_time_unit(data.get("service_time_unit", "s"))
            service_time *= _TIME_UNIT_TO_SECONDS[normalized_unit]
            service_rate = 1.0 / service_time
            service_rate_source = "derived_from_service_time"

        # "service_time_unit" also governs how the optional variability
        # fields below (service_time_stdev / _p50 / _p95) are interpreted -
        # parsed here unconditionally since it applies on both the
        # service_rate and service_time input paths.
        unit_seconds_factor = _TIME_UNIT_TO_SECONDS[
            QueueWorkload._normalize_time_unit(data.get("service_time_unit", "s"))
        ]
        service_time_seconds = 1.0 / service_rate
        service_time_cv_squared, service_time_cv_source = _estimate_service_time_variability(
            data, service_time_seconds, _TIME_UNIT_TO_SECONDS, unit_seconds_factor,
        )

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

        observed_response_time = None
        if "observed_response_time" in data and data["observed_response_time"] is not None:
            ort_raw = data["observed_response_time"]
            if isinstance(ort_raw, bool) or not isinstance(ort_raw, (int, float)):
                raise QueueingValidationError(
                    f"'observed_response_time' must be a numeric value (int or float), "
                    f"got {type(ort_raw).__name__}."
                )
            observed_response_time = float(ort_raw)
            if not math.isfinite(observed_response_time):
                raise QueueingValidationError("'observed_response_time' must be a finite number.")
            if observed_response_time < 0:
                raise QueueingValidationError("'observed_response_time' must not be negative.")

        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise QueueingValidationError("'metadata' must be a dictionary if provided.")

        return QueueWorkload(
            arrival_rate=arrival_rate,
            service_rate=service_rate,
            service_rate_source=service_rate_source,
            num_servers=num_servers,
            metadata=metadata,
            service_time_cv_squared=service_time_cv_squared,
            service_time_cv_source=service_time_cv_source,
            observed_response_time=observed_response_time,
        )

    @staticmethod
    def _normalize_time_unit(service_time_unit: Any) -> str:
        if not isinstance(service_time_unit, str):
            raise QueueingValidationError("'service_time_unit' must be a string.")
        normalized_unit = service_time_unit.strip().lower()
        if normalized_unit not in _TIME_UNIT_TO_SECONDS:
            raise QueueingValidationError(f"Unsupported service_time_unit '{service_time_unit}'.")
        return normalized_unit


# --- Core M/M/1 equations (pure functions, no state) ---

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


def calculate_queue_length_mg1(rho: float, cs_squared: float) -> float:
    """
    M/G/1 (Pollaczek-Khinchine), expressed as a queue length via Little's
    law applied to Wq = (rho/(1-rho)) * ((1+Cs^2)/2) * S:

        Lq = lambda * Wq = (rho^2 / (1 - rho)) * ((1 + Cs^2) / 2)

    Reduces exactly to the M/M/1 formula calculate_queue_length(rho) when
    cs_squared == 1.0 (exponential service time), since (1+1)/2 == 1.
    Returns inf when rho >= 1 (unstable, same as M/M/1).
    """
    if rho < 0:
        raise QueueingCalculationError("rho must not be negative.")
    if cs_squared < 0:
        raise QueueingCalculationError("cs_squared must not be negative.")
    if rho >= 1:
        return math.inf
    return ((rho ** 2) / (1.0 - rho)) * ((1.0 + cs_squared) / 2.0)


def calculate_requests_in_system_mg1(queue_length: float, rho: float) -> float:
    """M/G/1: L = Lq + rho (average number actively in service = rho for a
    single server, same as M/M/1 - only the queueing portion changes)."""
    if rho < 0:
        raise QueueingCalculationError("rho must not be negative.")
    if math.isinf(queue_length):
        return math.inf
    return queue_length + rho


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


# --- M/M/c equations (multi-server queueing, via the Erlang C formula) ---
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


def calculate_queue_length_ggc_approx(offered_load: float, num_servers: int, cs_squared: float) -> float:
    """
    G/G/c approximation (Sakasegawa), generalizing M/M/c to non-exponential
    service time the same way calculate_queue_length_mg1() generalizes
    M/M/1: multiply the M/M/c (Erlang C) queue length by (Ca^2+Cs^2)/2.
    Arrivals are still assumed Poisson here (Ca^2 = 1), so this is really
    "M/G/c" - true G/G/c would also need the arrival process's own
    coefficient of variation, which this pipeline doesn't estimate.

    Reduces exactly to calculate_queue_length_mmc() when cs_squared == 1.0.
    This is a standard, widely-used approximation, not an exact result -
    exact multi-server queueing with general service time has no closed
    form.
    """
    if cs_squared < 0:
        raise QueueingCalculationError("cs_squared must not be negative.")
    mmc_queue_length = calculate_queue_length_mmc(offered_load, num_servers)
    if math.isinf(mmc_queue_length):
        return math.inf
    return mmc_queue_length * ((1.0 + cs_squared) / 2.0)


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


# --- Classification thresholds ---

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

    def __post_init__(self) -> None:
        values = (self.idle_max, self.light_max, self.moderate_max, self.busy_max, self.congested_max)
        if any(v <= 0 for v in values):
            raise QueueingValidationError("Queue status thresholds must be positive.")
        if not all(values[i] < values[i + 1] for i in range(len(values) - 1)):
            raise QueueingValidationError("Queue status thresholds must be strictly increasing.")


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

    def __post_init__(self) -> None:
        if self.stable_max <= 0 or self.near_saturation_max <= 0:
            raise QueueingValidationError("Stability thresholds must be positive.")
        if not self.stable_max < self.near_saturation_max:
            raise QueueingValidationError("stable_max must be less than near_saturation_max.")


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

    def __post_init__(self) -> None:
        if self.low_max <= 0 or self.medium_max <= 0 or self.high_max <= 0:
            raise QueueingValidationError("Congestion risk thresholds must be positive.")
        if not self.low_max < self.medium_max < self.high_max:
            raise QueueingValidationError("Congestion risk thresholds must be strictly increasing.")
        if self.queue_length_critical <= 0:
            raise QueueingValidationError("queue_length_critical must be positive.")


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


# --- Signals (indicators only - no recommendations generated here) ---

@dataclass
class SignalThresholds:
    utilization_high_min: float = 0.8
    high_wait_time_min_seconds: float = 0.05
    long_queue_min_requests: float = 5.0

    def __post_init__(self) -> None:
        if self.utilization_high_min <= 0 or self.high_wait_time_min_seconds <= 0 or self.long_queue_min_requests <= 0:
            raise QueueingValidationError("Signal thresholds must be positive.")


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


# --- Observed-vs-modeled comparison (queueing prediction vs. actual response time) ---

def compare_system_time(
    calculated_system_time: float,
    observed_response_time: Optional[float],
    tolerance: float = 0.30,
) -> Optional[Dict[str, Any]]:
    """
    Compare the queueing model's predicted system_time (W) against an
    actually-observed response time (e.g. Locust's average response time
    at this load level).

    Unlike little_law.py's compare_concurrency() - which compares against
    a virtual-user count that's EXPECTED to diverge from L for legitimate
    reasons (think time, etc.) - system_time and an actually-measured
    average response time are modeling the exact same real-world
    quantity. Persistent divergence here means the model doesn't fit this
    workload well (bursty/non-Poisson arrivals, service_rate estimated
    rather than directly measured, external dependencies not captured),
    not that a mismatch is expected by design - so this is a genuine
    model-quality signal, not just a sanity check. Note this comparison
    remains meaningful under M/G/1 too: supplying real variability data
    (service_time_cv etc.) should, if anything, make this comparison
    tighter than plain M/M/1 did, since M/G/1 corrects M/M/1's systematic
    understatement of queueing delay for realistically variable service
    times.
    """
    if observed_response_time is None:
        return None
    if not math.isfinite(observed_response_time) or observed_response_time < 0:
        raise QueueingValidationError("observed_response_time must be a non-negative finite number.")
    if not 0 < tolerance:
        raise QueueingValidationError("tolerance must be greater than zero.")

    if math.isinf(calculated_system_time):
        return {
            "calculated_system_time": calculated_system_time,
            "observed_response_time": observed_response_time,
            "absolute_difference": None,
            "relative_difference": None,
            "tolerance": tolerance,
            "consistent": False,
            "note": (
                "The model predicts an unstable/saturated queue (infinite system time), but a "
                "finite response time was actually observed - the real system likely has "
                "request timeouts, load shedding, or connection limits this simplified "
                "queueing model doesn't capture, rather than truly unbounded queueing."
            ),
        }

    absolute_difference = calculated_system_time - observed_response_time
    relative_difference = (
        absolute_difference / observed_response_time if observed_response_time != 0 else None
    )
    consistent = abs(relative_difference) <= tolerance if relative_difference is not None else None

    return {
        "calculated_system_time": calculated_system_time,
        "observed_response_time": observed_response_time,
        "absolute_difference": absolute_difference,
        "relative_difference": relative_difference,
        "tolerance": tolerance,
        "consistent": consistent,
        "note": (
            "The queueing model's predicted response time diverges from what was actually "
            "observed by more than the tolerance. This can mean arrivals/service times don't "
            "fit the Poisson assumption well at this load level, or that service_rate was "
            "estimated rather than directly measured - treat this model's predictions as "
            "directional, not exact, for this workload."
        ) if consistent is False else None,
    }


# --- Unavailable-result helper ---

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


# --- Analyzer ---

class QueueingAnalyzer:
    """
    Queueing approximation for a single workload snapshot - not a literal
    claim that the system is a textbook Poisson-arrival/exponential-
    service queue. Real Flask/Docker deployments have connection pools,
    DB calls, etc. This is a standard approximation used to estimate
    utilization, queue growth, waiting time, and stability from observed
    characteristics.

    Uses M/G/1 (single server, Pollaczek-Khinchine) by default. If
    num_servers > 1 is supplied in the input, switches to the G/G/c
    approximation (multi-server, Erlang C generalized via Sakasegawa)
    instead - important if the deployment runs multiple worker processes
    or replicas behind one arrival stream, since a single-server model
    would otherwise overstate queueing delay for that setup.

    Service time is only assumed exponential (making this exactly M/M/1
    or M/M/c) when the caller doesn't supply any variability data - see
    QueueWorkload.from_raw()/_estimate_service_time_variability() for the
    optional "service_time_cv" / "service_time_stdev" /
    "service_time_p50"+"service_time_p95" inputs that opt into the more
    accurate general-service-time model. Check the returned
    "service_time_variability_source" field to see which case applied.
    """

    def __init__(
        self,
        queue_status_thresholds: Optional[QueueStatusThresholds] = None,
        stability_thresholds: Optional[StabilityThresholds] = None,
        congestion_risk_thresholds: Optional[CongestionRiskThresholds] = None,
        signal_thresholds: Optional[SignalThresholds] = None,
        response_time_tolerance: float = 0.30,
    ) -> None:
        for value, expected_type, name in (
            (queue_status_thresholds, QueueStatusThresholds, "queue_status_thresholds"),
            (stability_thresholds, StabilityThresholds, "stability_thresholds"),
            (congestion_risk_thresholds, CongestionRiskThresholds, "congestion_risk_thresholds"),
            (signal_thresholds, SignalThresholds, "signal_thresholds"),
        ):
            if value is not None and not isinstance(value, expected_type):
                raise QueueingValidationError(
                    f"{name} must be a {expected_type.__name__} instance, got {type(value).__name__}."
                )
        if (
            not isinstance(response_time_tolerance, (int, float))
            or isinstance(response_time_tolerance, bool)
            or not math.isfinite(response_time_tolerance)
            or response_time_tolerance <= 0
        ):
            raise QueueingValidationError("response_time_tolerance must be a positive finite number.")

        self.queue_status_thresholds = queue_status_thresholds or QueueStatusThresholds()
        self.stability_thresholds = stability_thresholds or StabilityThresholds()
        self.congestion_risk_thresholds = congestion_risk_thresholds or CongestionRiskThresholds()
        self.signal_thresholds = signal_thresholds or SignalThresholds()
        self.response_time_tolerance = float(response_time_tolerance)

    def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Args:
            data: {"arrival_rate": ..., "service_rate": ...} or
                  {"arrival_rate": ..., "service_time": ...}, plus optional
                  "num_servers" (default 1) and "metadata". Exactly one of
                  service_rate/service_time is required - see
                  QueueWorkload.from_raw().

                  Optionally also one of (in priority order)
                  "service_time_cv", "service_time_stdev", or
                  "service_time_p50"+"service_time_p95" to model service
                  time as general (M/G/1 or M/G/c) rather than exponential.
                  Omitting all of these keeps the original M/M/1 or M/M/c
                  behavior exactly.

                  Also optional: "observed_response_time" (e.g. Locust's
                  measured average response time at this load level), for
                  the model-quality comparison below.

        Raises:
            QueueingValidationError: bad/missing input.
        """
        workload = QueueWorkload.from_raw(data)

        offered_load = workload.arrival_rate / workload.service_rate  # a = lambda/mu, in Erlangs
        rho = calculate_utilization(workload.arrival_rate, workload.service_rate, workload.num_servers)
        cs_squared = workload.service_time_cv_squared

        if workload.num_servers == 1:
            # M/G/1 (Pollaczek-Khinchine). Reduces exactly to the
            # original M/M/1 numbers when cs_squared == 1.0 (i.e. no
            # variability data was supplied - exponential service time
            # is assumed by default), so every existing caller that
            # doesn't opt in to richer variability data is unaffected.
            queue_length = calculate_queue_length_mg1(rho, cs_squared)
            requests_in_system = calculate_requests_in_system_mg1(queue_length, rho)
            idle_probability = calculate_idle_probability(rho)
        else:
            terms = _mmc_terms(offered_load, workload.num_servers)
            if terms["rho"] >= 1:
                queue_length = math.inf
                requests_in_system = math.inf
            else:
                # G/G/c approximation (Sakasegawa) - see
                # calculate_queue_length_ggc_approx(). Reduces exactly to
                # the original M/M/c numbers when cs_squared == 1.0.
                queue_length = calculate_queue_length_ggc_approx(offered_load, workload.num_servers, cs_squared)
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

        is_exponential_assumption = workload.service_time_cv_source == "assumed_exponential"
        base_model = "M/M" if is_exponential_assumption else "M/G"
        queueing_model = f"{base_model}/1" if workload.num_servers == 1 else f"{base_model}/{workload.num_servers}"

        return {
            "arrival_rate": workload.arrival_rate,
            "service_rate": workload.service_rate,
            "service_rate_source": workload.service_rate_source,
            "num_servers": workload.num_servers,
            "queueing_model": queueing_model,
            "offered_load": offered_load,
            "utilization": rho,
            "queue_length": queue_length,
            "requests_in_system": requests_in_system,
            "waiting_time": waiting_time,
            "system_time": system_time,
            "service_time": service_time,
            "idle_probability": idle_probability,
            "service_time_cv": math.sqrt(cs_squared),
            "service_time_cv_squared": cs_squared,
            "service_time_variability_source": workload.service_time_cv_source,
            "queue_status": queue_status,
            "stability": stability,
            "congestion_risk": congestion_risk,
            "system_time_comparison": compare_system_time(
                system_time,
                workload.observed_response_time,
                tolerance=self.response_time_tolerance,
            ),
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
                "service_time_cv": "ratio (stdev/mean)",
                "service_time_cv_squared": "ratio",
            },
            "metadata": workload.metadata,
        }


# --- Convenience functional wrappers ---

def analyze_queue(
    data: Dict[str, Any],
    queue_status_thresholds: Optional[QueueStatusThresholds] = None,
    stability_thresholds: Optional[StabilityThresholds] = None,
    congestion_risk_thresholds: Optional[CongestionRiskThresholds] = None,
    signal_thresholds: Optional[SignalThresholds] = None,
    response_time_tolerance: float = 0.30,
) -> Dict[str, Any]:
    """One-shot: validate + analyze. Raises if service_rate/service_time is missing."""
    analyzer = QueueingAnalyzer(
        queue_status_thresholds=queue_status_thresholds,
        stability_thresholds=stability_thresholds,
        congestion_risk_thresholds=congestion_risk_thresholds,
        signal_thresholds=signal_thresholds,
        response_time_tolerance=response_time_tolerance,
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