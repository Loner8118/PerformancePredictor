from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


class LittleLawValidationError(ValueError):
    """Bad input data."""


class LittleLawCalculationError(RuntimeError):
    """A Little's Law calculation couldn't be performed (e.g. div by zero)."""


# --- Unit conversion ---

_TIME_UNIT_TO_SECONDS: Dict[str, float] = {
    "s": 1.0,
    "sec": 1.0,
    "seconds": 1.0,
    "ms": 1.0 / 1_000.0,
    "milliseconds": 1.0 / 1_000.0,
    "us": 1.0 / 1_000_000.0,
    "microseconds": 1.0 / 1_000_000.0,
}


def _convert_to_seconds(value: float, unit: str) -> float:
    if not isinstance(unit, str):
        raise LittleLawValidationError("Time unit must be a string.")
    normalized_unit = unit.strip().lower()
    if normalized_unit not in _TIME_UNIT_TO_SECONDS:
        supported = ", ".join(sorted(_TIME_UNIT_TO_SECONDS.keys()))
        raise LittleLawValidationError(f"Unsupported time unit '{unit}'. Supported units: {supported}.")
    return value * _TIME_UNIT_TO_SECONDS[normalized_unit]


# --- Validated workload container ---

@dataclass(frozen=True)
class Workload:
    """
    Validated, unit-normalized workload observation.

    arrival_rate:        requests/sec (lambda), > 0
    response_time:       seconds (W), > 0
    service_time:        seconds, optional - only set if the caller supplied it
    observed_concurrency: optional - externally measured concurrency (e.g.
        Locust "users"). NOT used in the L = lambda * W calculation, it's
        only carried through for the consistency comparison later. Locust
        virtual users and Little's Law L are not the same thing and
        shouldn't be conflated.
    observation_window: seconds, optional - how long the measurement window
        was (e.g. a 15s Locust run). Not used in the L = lambda * W
        calculation either - it only feeds the sample-confidence check,
        since lambda/W measured over a short window carry more sampling
        noise even though the formula itself is exact for the true
        long-run averages.
    metadata: passthrough, doesn't affect calculations
    """
    arrival_rate: float
    response_time: float
    service_time: Optional[float]
    observed_concurrency: Optional[float]
    observation_window: Optional[float]
    metadata: Dict[str, Any]

    @staticmethod
    def from_raw(data: Dict[str, Any]) -> "Workload":
        if not isinstance(data, dict):
            raise LittleLawValidationError("Input must be a dictionary.")

        if "arrival_rate" not in data:
            raise LittleLawValidationError("Missing required field: 'arrival_rate'.")
        if "average_response_time" not in data:
            raise LittleLawValidationError("Missing required field: 'average_response_time'.")

        arrival_rate_raw = data["arrival_rate"]
        response_time_raw = data["average_response_time"]

        for name, value in (("arrival_rate", arrival_rate_raw), ("average_response_time", response_time_raw)):
            # bool is a subclass of int in Python, reject it explicitly so
            # True/False don't silently pass as 1/0
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise LittleLawValidationError(
                    f"'{name}' must be a numeric value (int or float), got {type(value).__name__}."
                )

        arrival_rate = float(arrival_rate_raw)
        response_time = float(response_time_raw)

        if not math.isfinite(arrival_rate):
            raise LittleLawValidationError("'arrival_rate' must be finite.")

        if not math.isfinite(response_time):
            raise LittleLawValidationError("'average_response_time' must be finite.")

        if arrival_rate <= 0:
            raise LittleLawValidationError("'arrival_rate' must be greater than zero.")
        if response_time <= 0:
            raise LittleLawValidationError("'average_response_time' must be greater than zero.")

        response_time_unit = data.get("response_time_unit", "s")
        response_time = _convert_to_seconds(response_time, response_time_unit)

        service_time = None
        if "service_time" in data and data["service_time"] is not None:
            service_time_raw = data["service_time"]
            if isinstance(service_time_raw, bool) or not isinstance(service_time_raw, (int, float)):
                raise LittleLawValidationError(
                    f"'service_time' must be a numeric value (int or float), got {type(service_time_raw).__name__}."
                )
            if service_time_raw <= 0:
                raise LittleLawValidationError("'service_time' must be greater than zero if provided.")

            service_time_unit = data.get("service_time_unit", response_time_unit)
            service_time = _convert_to_seconds(float(service_time_raw), service_time_unit)

        observed_concurrency = None
        if "observed_concurrency" in data and data["observed_concurrency"] is not None:
            oc_raw = data["observed_concurrency"]
            if isinstance(oc_raw, bool) or not isinstance(oc_raw, (int, float)):
                raise LittleLawValidationError(
                    f"'observed_concurrency' must be a numeric value (int or float), got {type(oc_raw).__name__}."
                )
            if oc_raw < 0:
                raise LittleLawValidationError("'observed_concurrency' must not be negative.")
            observed_concurrency = float(oc_raw)

        observation_window = None
        if "observation_window" in data and data["observation_window"] is not None:
            ow_raw = data["observation_window"]
            if isinstance(ow_raw, bool) or not isinstance(ow_raw, (int, float)):
                raise LittleLawValidationError(
                    f"'observation_window' must be a numeric value (int or float), got {type(ow_raw).__name__}."
                )
            if ow_raw <= 0:
                raise LittleLawValidationError("'observation_window' must be greater than zero if provided.")

            observation_window_unit = data.get("observation_window_unit", response_time_unit)
            observation_window = _convert_to_seconds(float(ow_raw), observation_window_unit)

        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise LittleLawValidationError("'metadata' must be a dictionary if provided.")

        return Workload(
            arrival_rate=arrival_rate,
            response_time=response_time,
            service_time=service_time,
            observed_concurrency=observed_concurrency,
            observation_window=observation_window,
            metadata=metadata,
        )


# --- Core Little's Law math (pure functions, no state) ---

def calculate_requests(arrival_rate: float, response_time: float) -> float:
    """L = lambda * W"""
    if arrival_rate <= 0 or response_time <= 0:
        raise LittleLawCalculationError(
            "arrival_rate and response_time must both be positive to calculate requests in system."
        )
    return arrival_rate * response_time


def calculate_arrival_rate(requests_in_system: float, response_time: float) -> float:
    """lambda = L / W"""
    if response_time <= 0:
        raise LittleLawCalculationError("response_time must be greater than zero to calculate arrival_rate.")
    if requests_in_system < 0:
        raise LittleLawCalculationError("requests_in_system must not be negative.")
    return requests_in_system / response_time


def calculate_response_time(requests_in_system: float, arrival_rate: float) -> float:
    """W = L / lambda"""
    if arrival_rate <= 0:
        raise LittleLawCalculationError("arrival_rate must be greater than zero to calculate response_time.")
    if requests_in_system < 0:
        raise LittleLawCalculationError("requests_in_system must not be negative.")
    return requests_in_system / arrival_rate


def calculate_waiting_time(response_time: float, service_time: float) -> float:
    """
    waiting_time = response_time - service_time

    Only meaningful when service_time was actually measured/known - this
    is not part of core Little's Law, it's a derived breakdown of W into
    time spent waiting vs. time spent being served.
    """
    if response_time <= 0:
        raise LittleLawCalculationError("response_time must be greater than zero to calculate waiting_time.")
    if service_time <= 0:
        raise LittleLawCalculationError("service_time must be greater than zero to calculate waiting_time.")
    # A tiny floating-point overshoot (service_time fractionally larger than
    # response_time due to rounding during unit conversion) shouldn't hard-fail
    # this check - only a real, meaningful violation should.
    if service_time > response_time * (1 + 1e-9):
        raise LittleLawCalculationError("service_time cannot be greater than response_time.")
    return max(0.0, response_time - service_time)


# --- Sample-confidence diagnostic (short observation windows) ---

def estimate_renewal_cycles(observation_window: float, response_time: float) -> float:
    """
    Rough estimate of how many complete "request lifetimes" fit inside the
    observation window - i.e. roughly how many independent samples the
    measured arrival rate and response time are actually based on.

    A 15-second load-test window with a 2.5s average response time only
    observed on the order of 6 such cycles. L = lambda * W is still exact
    for the *true* long-run averages, but lambda and W measured over a
    short window carry real sampling noise, and this is a simple way to
    flag when that noise is likely to matter.
    """
    if observation_window <= 0:
        raise LittleLawCalculationError("observation_window must be greater than zero.")
    if response_time <= 0:
        raise LittleLawCalculationError("response_time must be greater than zero.")
    return observation_window / response_time


def classify_sample_confidence(renewal_cycles: Optional[float]) -> str:
    """Coarse confidence band for how much sampling noise to expect."""
    if renewal_cycles is None:
        return "Unknown"
    if renewal_cycles < 10:
        return "Low"
    if renewal_cycles < 30:
        return "Moderate"
    return "High"


def _generate_measurement_confidence(
    observation_window: Optional[float],
    response_time: float,
) -> Dict[str, Any]:
    if observation_window is None:
        return {
            "observation_window": None,
            "estimated_renewal_cycles": None,
            "status": "Unknown",
            "note": (
                "No observation window was supplied, so sample-confidence cannot be "
                "assessed - this doesn't affect the L = lambda * W calculation itself, "
                "only how much sampling noise the underlying lambda/W measurements "
                "likely carry."
            ),
        }

    cycles = estimate_renewal_cycles(observation_window, response_time)
    status = classify_sample_confidence(cycles)

    note = None
    if status == "Low":
        note = (
            f"Only about {cycles:.1f} complete request cycles were observed in the "
            f"{observation_window:.1f}s measurement window. The measured arrival rate "
            f"and response time - and therefore L - carry meaningfully higher sampling "
            f"noise than a longer run would; treat this snapshot as indicative rather "
            f"than precise."
        )
    elif status == "Moderate":
        note = (
            f"About {cycles:.1f} complete request cycles were observed in the "
            f"{observation_window:.1f}s measurement window - a reasonable sample, but "
            f"a longer run would reduce noise in the measured arrival rate and "
            f"response time further."
        )

    return {
        "observation_window": observation_window,
        "estimated_renewal_cycles": cycles,
        "status": status,
        "note": note,
    }


# --- Consistency comparison (Locust concurrency vs. calculated L) ---

def compare_concurrency(
    calculated_l: float,
    observed_concurrency: Optional[float],
    tolerance: float = 0.30,
) -> Optional[Dict[str, Any]]:
    """
    Compare Little's Law's calculated L against an externally observed
    concurrency figure (e.g. Locust's configured user count).

    This is deliberately not "L should equal observed users" - a Locust
    "user" is a virtual client that may spend time thinking/waiting
    outside of what the server measures, so some divergence is normal.
    It's a sanity check, not an equality assertion - tolerance defaults
    to 30% (looser than capacity.py's own 20% arrival-rate-vs-throughput
    check, since virtual users vs. L are expected to diverge more even in
    a perfectly healthy system).
    """
    if observed_concurrency is None:
        return None
    if not 0 < tolerance:
        raise LittleLawValidationError("tolerance must be greater than zero.")

    absolute_difference = calculated_l - observed_concurrency
    relative_difference = (
        absolute_difference / observed_concurrency if observed_concurrency != 0 else None
    )

    consistent = abs(relative_difference) <= tolerance if relative_difference is not None else None

    return {
        "calculated_l": calculated_l,
        "observed_concurrency": observed_concurrency,
        "absolute_difference": absolute_difference,
        "relative_difference": relative_difference,
        "tolerance": tolerance,
        "consistent": consistent,
        "note": (
            "Calculated concurrent requests (L) diverges from the observed/configured "
            "concurrency by more than the tolerance. Some divergence is expected "
            "(virtual users spend time thinking/waiting outside what the server "
            "measures) but this is worth a second look if the gap is larger than "
            "expected for this workload."
        ) if consistent is False else None,
    }


# --- Load classification ---

@dataclass
class LoadClassificationThresholds:
    """
    Upper bounds (exclusive) for each load band, in terms of L. Centralized
    here instead of scattered as magic numbers so they can be tuned per
    deployment - a high-throughput API might treat hundreds of concurrent
    requests as "Moderate", a lightweight internal tool might call that
    "Critical".
    """
    very_light_max: float = 10.0
    light_max: float = 50.0
    moderate_max: float = 150.0
    high_max: float = 400.0
    very_high_max: float = 800.0

    def __post_init__(self) -> None:
        values = (
            self.very_light_max,
            self.light_max,
            self.moderate_max,
            self.high_max,
            self.very_high_max,
        )

        if any(value <= 0 for value in values):
            raise LittleLawValidationError("Load classification thresholds must be positive.")

        if not all(values[i] < values[i + 1] for i in range(len(values) - 1)):
            raise LittleLawValidationError(
                "Load classification thresholds must be strictly increasing."
            )


def classify_load(
    requests_in_system: float,
    thresholds: Optional[LoadClassificationThresholds] = None,
) -> str:
    if thresholds is None:
        thresholds = LoadClassificationThresholds()

    if requests_in_system < thresholds.very_light_max:
        return "Very Light"
    if requests_in_system < thresholds.light_max:
        return "Light"
    if requests_in_system < thresholds.moderate_max:
        return "Moderate"
    if requests_in_system < thresholds.high_max:
        return "High"
    if requests_in_system < thresholds.very_high_max:
        return "Very High"
    return "Critical"

# --- Signals (indicators only - no recommendations generated here) ---

def _generate_signals(
    requests_in_system: float,
    arrival_rate: float,
    response_time: float,
    classification: str,
    previous: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Indicator signals for downstream modules (queueing.py, capacity.py,
    recommendation.py). No recommendations here - just facts they can
    interpret.

    Trend signals need a previous snapshot; without one they come back
    as None (unknown) rather than guessed.
    """
    signals: Dict[str, Any] = {
        "high_occupancy": classification in ("High", "Very High", "Critical"),
        "critical_occupancy": classification == "Critical",
        "occupancy_trend": None,
        "response_time_trend": None,
        "arrival_rate_trend": None,
    }

    if previous is not None:
        prev_l = previous.get("requests_in_system")
        prev_lambda = previous.get("arrival_rate")
        prev_w = previous.get("response_time")

        if isinstance(prev_l, (int, float)):
            if requests_in_system > prev_l:
                signals["occupancy_trend"] = "increasing"
            elif requests_in_system < prev_l:
                signals["occupancy_trend"] = "decreasing"
            else:
                signals["occupancy_trend"] = "stable"

        if isinstance(prev_w, (int, float)):
            if response_time > prev_w:
                signals["response_time_trend"] = "increasing"
            elif response_time < prev_w:
                signals["response_time_trend"] = "decreasing"
            else:
                signals["response_time_trend"] = "stable"

        if isinstance(prev_lambda, (int, float)):
            if arrival_rate > prev_lambda:
                signals["arrival_rate_trend"] = "increasing"
            elif arrival_rate < prev_lambda:
                signals["arrival_rate_trend"] = "decreasing"
            else:
                signals["arrival_rate_trend"] = "stable"

    return signals


def _generate_summary(requests_in_system: float) -> str:
    rounded = round(requests_in_system)
    return f"Average of {rounded} requests are expected to be inside the system simultaneously."


# --- Analyzer ---

class LittleLawAnalyzer:
    """
    Runs Little's Law analysis for one workload snapshot, or a full
    series of them (e.g. one entry per Locust load level).

    Internally: Workload.from_raw() -> calculate_requests() ->
    classify_load() -> _generate_signals(), plus the optional waiting-time
    breakdown, the sample-confidence diagnostic, and the observed-vs-
    calculated concurrency consistency check.
    """

    def __init__(
        self,
        thresholds: Optional[LoadClassificationThresholds] = None,
        concurrency_tolerance: float = 0.30,
    ) -> None:
        if thresholds is not None and not isinstance(thresholds, LoadClassificationThresholds):
            raise LittleLawValidationError(
                f"thresholds must be a LoadClassificationThresholds instance, got {type(thresholds).__name__}."
            )
        if (
            not isinstance(concurrency_tolerance, (int, float))
            or isinstance(concurrency_tolerance, bool)
            or not math.isfinite(concurrency_tolerance)
            or concurrency_tolerance <= 0
        ):
            raise LittleLawValidationError("concurrency_tolerance must be a positive finite number.")

        self.thresholds = thresholds or LoadClassificationThresholds()
        self.concurrency_tolerance = float(concurrency_tolerance)

    def analyze(self, data: Dict[str, Any], previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Args:
            data: {"arrival_rate": ..., "average_response_time": ...,
                   "response_time_unit": ... (optional),
                   "service_time": ... (optional),
                   "observed_concurrency": ... (optional),
                   "observation_window": ... (optional),
                   "observation_window_unit": ... (optional, defaults to response_time_unit),
                   "metadata": {...} (optional)}
            previous: previous analyze() result, used only for trend signals.
        """
        workload = Workload.from_raw(data)

        requests_in_system = calculate_requests(workload.arrival_rate, workload.response_time)

        # concurrent_requests / system_occupancy are the same number as L
        # in this steady-state model, just surfaced under the terminology
        # capacity.py / recommendation.py expect.
        concurrent_requests = requests_in_system
        system_occupancy = requests_in_system

        waiting_time = None
        if workload.service_time is not None:
            waiting_time = calculate_waiting_time(workload.response_time, workload.service_time)

        classification = classify_load(requests_in_system, self.thresholds)
        summary = _generate_summary(requests_in_system)
        signals = _generate_signals(
            requests_in_system=requests_in_system,
            arrival_rate=workload.arrival_rate,
            response_time=workload.response_time,
            classification=classification,
            previous=previous,
        )
        measurement_confidence = _generate_measurement_confidence(
            observation_window=workload.observation_window,
            response_time=workload.response_time,
        )

        result = {
            "arrival_rate": workload.arrival_rate,
            "response_time": workload.response_time,
            "time_in_system": workload.response_time,  # required by capacity.py
            "service_time": workload.service_time,
            "waiting_time": waiting_time,
            "requests_in_system": requests_in_system,
            "concurrent_requests": concurrent_requests,
            "system_occupancy": system_occupancy,
            "load_classification": classification,
            "concurrency_comparison": compare_concurrency(
                requests_in_system,
                workload.observed_concurrency,
                tolerance=self.concurrency_tolerance,
            ),
            "measurement_confidence": measurement_confidence,
            "analysis_summary": summary,
            "signals": signals,
            "units": {
                "arrival_rate": "requests/sec",
                "response_time": "seconds",
                "service_time": "seconds",
                "waiting_time": "seconds",
                "requests_in_system": "requests",
                "concurrent_requests": "requests",
                "system_occupancy": "requests",
                "observation_window": "seconds",
            },
            "metadata": workload.metadata,
        }
        return result

    def analyze_series(self, levels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Run analyze() across a series of load levels in order (e.g. the
        20/50/100/200/300/500-user Locust runs), chaining each result as
        the "previous" snapshot for the next one so trend signals come
        out automatically. Caller is responsible for passing levels in
        the order they should be compared (normally ascending load).
        """
        results: List[Dict[str, Any]] = []
        previous: Optional[Dict[str, Any]] = None

        for level_data in levels:
            result = self.analyze(level_data, previous=previous)
            results.append(result)
            previous = result

        return results


# --- Convenience functional wrappers ---

def analyze_workload(
    data: Dict[str, Any],
    previous: Optional[Dict[str, Any]] = None,
    thresholds: Optional[LoadClassificationThresholds] = None,
    concurrency_tolerance: float = 0.30,
) -> Dict[str, Any]:
    """One-shot: validate + analyze a single workload snapshot."""
    analyzer = LittleLawAnalyzer(thresholds=thresholds, concurrency_tolerance=concurrency_tolerance)
    return analyzer.analyze(data, previous=previous)


def analyze_workload_levels(
    levels: List[Dict[str, Any]],
    thresholds: Optional[LoadClassificationThresholds] = None,
    concurrency_tolerance: float = 0.30,
) -> List[Dict[str, Any]]:
    """
    One-shot: validate + analyze a full series of load levels (e.g. every
    Locust run at 20, 50, 100, 200, 300, 500 users), in the order given.
    """
    analyzer = LittleLawAnalyzer(thresholds=thresholds, concurrency_tolerance=concurrency_tolerance)
    return analyzer.analyze_series(levels)