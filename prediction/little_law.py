from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------

class LittleLawValidationError(ValueError):
    """Raised when input data fails validation rules for Little's Law."""


class LittleLawCalculationError(RuntimeError):
    """Raised when a Little's Law calculation cannot be performed
    (e.g. division by zero in a rearranged equation)."""


# --------------------------------------------------------------------------
# Unit conversion helpers
# --------------------------------------------------------------------------

# Supported response-time input units and their conversion factor to seconds.
# Kept as a module-level constant (not scattered through the code) so new
# units can be added in one place.
_RESPONSE_TIME_UNIT_TO_SECONDS: Dict[str, float] = {
    "s": 1.0,
    "sec": 1.0,
    "seconds": 1.0,
    "ms": 1.0 / 1_000.0,
    "milliseconds": 1.0 / 1_000.0,
    "us": 1.0 / 1_000_000.0,
    "microseconds": 1.0 / 1_000_000.0,
}


def _convert_response_time_to_seconds(value: float, unit: str) -> float:
    """
    Convert a response-time value expressed in `unit` into seconds.

    Args:
        value: Numeric response time.
        unit: One of the keys in _RESPONSE_TIME_UNIT_TO_SECONDS
              (case-insensitive).

    Returns:
        Response time expressed in seconds.

    Raises:
        LittleLawValidationError: If the unit is not recognized.
    """
    normalized_unit = unit.strip().lower()
    if normalized_unit not in _RESPONSE_TIME_UNIT_TO_SECONDS:
        supported = ", ".join(sorted(_RESPONSE_TIME_UNIT_TO_SECONDS.keys()))
        raise LittleLawValidationError(
            f"Unsupported response_time_unit '{unit}'. Supported units: {supported}."
        )
    return value * _RESPONSE_TIME_UNIT_TO_SECONDS[normalized_unit]


# --------------------------------------------------------------------------
# Validated workload container
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Workload:
    """
    A validated, unit-normalized workload observation ready for Little's
    Law calculations.

    Attributes:
        arrival_rate: Requests per second (lambda), guaranteed > 0.
        response_time: Average time in system, in seconds (W), guaranteed > 0.
        metadata: Arbitrary passthrough metadata (does not affect
            calculations).
    """
    arrival_rate: float
    response_time: float
    metadata: Dict[str, Any]

    @staticmethod
    def from_raw(data: Dict[str, Any]) -> "Workload":
        """
        Validate and normalize a raw input dictionary into a Workload.

        Validation rules enforced:
            1. "arrival_rate" and "average_response_time" must be present.
            2. Both values must be numeric (int or float).
            3. Both values must be strictly greater than zero.
            4. Response time is converted to seconds if an optional
               "response_time_unit" key is supplied (defaults to seconds
               if omitted).
            5. Any "metadata" key is passed through unchanged and never
               participates in calculations.

        Args:
            data: Raw input dictionary, e.g.:
                {
                    "arrival_rate": 450.0,
                    "average_response_time": 0.35,
                    "response_time_unit": "s",      # optional
                    "metadata": {...}                # optional
                }

        Returns:
            A validated Workload instance.

        Raises:
            LittleLawValidationError: If any validation rule is violated.
        """
        if not isinstance(data, dict):
            raise LittleLawValidationError("Input must be a dictionary.")

        if "arrival_rate" not in data:
            raise LittleLawValidationError("Missing required field: 'arrival_rate'.")
        if "average_response_time" not in data:
            raise LittleLawValidationError("Missing required field: 'average_response_time'.")

        arrival_rate_raw = data["arrival_rate"]
        response_time_raw = data["average_response_time"]

        # Numeric-only check. Explicitly reject bool, since bool is a
        # subclass of int in Python and would otherwise silently pass.
        for name, value in (("arrival_rate", arrival_rate_raw), ("average_response_time", response_time_raw)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise LittleLawValidationError(
                    f"'{name}' must be a numeric value (int or float), got {type(value).__name__}."
                )

        arrival_rate = float(arrival_rate_raw)
        response_time = float(response_time_raw)

        if arrival_rate <= 0:
            raise LittleLawValidationError("'arrival_rate' must be greater than zero.")
        if response_time <= 0:
            raise LittleLawValidationError("'average_response_time' must be greater than zero.")

        # Optional unit conversion for response time.
        response_time_unit = data.get("response_time_unit", "s")
        response_time = _convert_response_time_to_seconds(response_time, response_time_unit)

        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise LittleLawValidationError("'metadata' must be a dictionary if provided.")

        return Workload(arrival_rate=arrival_rate, response_time=response_time, metadata=metadata)


# --------------------------------------------------------------------------
# Core Little's Law math (pure functions, no state)
# --------------------------------------------------------------------------

def calculate_requests(arrival_rate: float, response_time: float) -> float:
    """
    Core Little's Law equation: L = lambda * W

    Computes the average number of requests inside the system given an
    arrival rate and average response time.

    Args:
        arrival_rate: Average arrival rate, lambda (requests/second).
        response_time: Average time in system, W (seconds).

    Returns:
        L, the average number of requests inside the system.

    Raises:
        LittleLawCalculationError: If either input is not positive.
    """
    if arrival_rate <= 0 or response_time <= 0:
        raise LittleLawCalculationError(
            "arrival_rate and response_time must both be positive to calculate requests in system."
        )
    return arrival_rate * response_time


def calculate_arrival_rate(requests_in_system: float, response_time: float) -> float:
    """
    Rearranged Little's Law: lambda = L / W

    Solves for arrival rate given a known number of requests in the
    system and average response time. Useful for validation or for
    future extensions that work backward from observed occupancy.

    Args:
        requests_in_system: L, average number of requests in the system.
        response_time: W, average response time (seconds).

    Returns:
        lambda, the implied arrival rate (requests/second).

    Raises:
        LittleLawCalculationError: If response_time is not positive
            (division by zero) or requests_in_system is negative.
    """
    if response_time <= 0:
        raise LittleLawCalculationError("response_time must be greater than zero to calculate arrival_rate.")
    if requests_in_system < 0:
        raise LittleLawCalculationError("requests_in_system must not be negative.")
    return requests_in_system / response_time


def calculate_response_time(requests_in_system: float, arrival_rate: float) -> float:
    """
    Rearranged Little's Law: W = L / lambda

    Solves for average response time given a known number of requests in
    the system and arrival rate. Useful for validation or for future
    extensions that work backward from observed occupancy.

    Args:
        requests_in_system: L, average number of requests in the system.
        arrival_rate: lambda, average arrival rate (requests/second).

    Returns:
        W, the implied average response time (seconds).

    Raises:
        LittleLawCalculationError: If arrival_rate is not positive
            (division by zero) or requests_in_system is negative.
    """
    if arrival_rate <= 0:
        raise LittleLawCalculationError("arrival_rate must be greater than zero to calculate response_time.")
    if requests_in_system < 0:
        raise LittleLawCalculationError("requests_in_system must not be negative.")
    return requests_in_system / arrival_rate


# --------------------------------------------------------------------------
# Load classification
# --------------------------------------------------------------------------

@dataclass
class LoadClassificationThresholds:
    """
    Configurable upper bounds (exclusive) for each load classification
    band, expressed in terms of L (average requests in system).

    Classification is determined by finding the first band whose upper
    bound exceeds the computed L:

        L < very_light_max        -> "Very Light"
        L < light_max              -> "Light"
        L < moderate_max           -> "Moderate"
        L < high_max                -> "High"
        L < very_high_max          -> "Very High"
        L >= very_high_max         -> "Critical"

    These defaults are illustrative starting points for a typical web
    application and are intentionally centralized here (rather than
    scattered as magic numbers through the code) so they can be tuned
    per deployment -- e.g. a high-throughput API might expect hundreds
    of concurrent requests to still be "Moderate", while a lightweight
    internal tool might consider that "Critical".
    """
    very_light_max: float = 10.0
    light_max: float = 50.0
    moderate_max: float = 150.0
    high_max: float = 400.0
    very_high_max: float = 800.0


def classify_load(
    requests_in_system: float,
    thresholds: Optional[LoadClassificationThresholds] = None,
) -> str:
    """
    Classify system occupancy (L) into a qualitative load band.

    Args:
        requests_in_system: L, average number of requests in the system.
        thresholds: Optional custom thresholds. Defaults to
            LoadClassificationThresholds() if not provided.

    Returns:
        One of: "Very Light", "Light", "Moderate", "High", "Very High",
        "Critical".
    """
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


# --------------------------------------------------------------------------
# Signals (indicators only -- no recommendations are generated here)
# --------------------------------------------------------------------------

def _generate_signals(
    requests_in_system: float,
    arrival_rate: float,
    response_time: float,
    classification: str,
    thresholds: LoadClassificationThresholds,
    previous: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build a dictionary of indicator signals for downstream modules
    (Queueing Theory, Capacity Planning, Recommendation Engine).

    This function does NOT make recommendations. It only exposes boolean
    or descriptive signals that later modules can interpret.

    Trend-based signals ("occupancy increasing", "response time
    increasing", "arrival rate increasing") require a previous snapshot
    for comparison. If `previous` is not supplied, those signals are
    reported as None (unknown) rather than guessed.

    Args:
        requests_in_system: Current L value.
        arrival_rate: Current lambda value.
        response_time: Current W value.
        classification: Current load classification string.
        thresholds: The thresholds used for classification, so
            "high"/"critical" signals stay consistent with classify_load().
        previous: Optional dict describing the previous analysis result
            (expected to contain "requests_in_system", "arrival_rate",
            "response_time" keys), used to derive trend signals.

    Returns:
        Dictionary of signal name -> value (bool, str, or None).
    """
    signals: Dict[str, Any] = {
        # "High occupancy" should align with the classification bands
        # themselves: it fires once L reaches the point where
        # classify_load() would label the workload "High" or worse
        # (i.e. at or beyond the moderate_max boundary), not only at the
        # much higher very_high_max boundary.
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


# --------------------------------------------------------------------------
# Summary text
# --------------------------------------------------------------------------

def _generate_summary(requests_in_system: float) -> str:
    """
    Produce a short, human-readable summary sentence describing the
    computed occupancy. Intended for direct inclusion in reports.

    Args:
        requests_in_system: L, average number of requests in the system.

    Returns:
        A one-sentence natural-language summary.
    """
    rounded = round(requests_in_system)
    return f"Average of {rounded} requests are expected to be inside the system simultaneously."


# --------------------------------------------------------------------------
# Analyzer (orchestrates validation -> calculation -> classification)
# --------------------------------------------------------------------------

class LittleLawAnalyzer:
    """
    Orchestrates Little's Law analysis for a single workload snapshot.

    This class separates concerns internally:
        - Validation:      Workload.from_raw()
        - Calculation:     calculate_requests() / helper equations
        - Classification:  classify_load()
        - Signal exposure: _generate_signals()

    It is intended to be reusable by other mathematical modules (e.g. a
    Queueing Theory module could instantiate this with a custom
    LoadClassificationThresholds tuned to its own context).
    """

    def __init__(self, thresholds: Optional[LoadClassificationThresholds] = None) -> None:
        """
        Args:
            thresholds: Optional custom load classification thresholds.
                Defaults to LoadClassificationThresholds() if omitted.
        """
        self.thresholds = thresholds or LoadClassificationThresholds()

    def analyze(
        self,
        data: Dict[str, Any],
        previous: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Run a full Little's Law analysis on a single workload.

        Args:
            data: Raw workload input, e.g.:
                {
                    "arrival_rate": 450.0,
                    "average_response_time": 0.35,
                    "metadata": {...}   # optional, passthrough only
                }
            previous: Optional previous analysis result (as returned by
                this method) used solely to derive trend signals.

        Returns:
            Structured dictionary:
                {
                    "arrival_rate": ...,
                    "response_time": ...,
                    "requests_in_system": ...,
                    "concurrent_requests": ...,
                    "system_occupancy": ...,
                    "load_classification": ...,
                    "analysis_summary": ...,
                    "signals": {...},
                    "metadata": {...}
                }

        Raises:
            LittleLawValidationError: If input validation fails.
            LittleLawCalculationError: If a calculation cannot be performed.
        """
        workload = Workload.from_raw(data)

        # Core Little's Law computation: L = lambda * W
        requests_in_system = calculate_requests(workload.arrival_rate, workload.response_time)

        # Concurrent requests and system occupancy are mathematically
        # identical to L in this steady-state model, but are surfaced as
        # separate named outputs because downstream modules (Capacity
        # Planning, Recommendation Engine) use this specific terminology
        # and may evolve independently in future versions.
        concurrent_requests = requests_in_system
        system_occupancy = requests_in_system

        classification = classify_load(requests_in_system, self.thresholds)
        summary = _generate_summary(requests_in_system)
        signals = _generate_signals(
            requests_in_system=requests_in_system,
            arrival_rate=workload.arrival_rate,
            response_time=workload.response_time,
            classification=classification,
            thresholds=self.thresholds,
            previous=previous,
        )

        return {
            "arrival_rate": workload.arrival_rate,
            "response_time": workload.response_time,
            "time_in_system": workload.response_time,  # Required by capacity.py
            "requests_in_system": requests_in_system,
            "concurrent_requests": concurrent_requests,
            "system_occupancy": system_occupancy,
            "load_classification": classification,
            "analysis_summary": summary,
            "signals": signals,
            "metadata": workload.metadata,
        }


# --------------------------------------------------------------------------
# Convenience functional wrapper (for simple/one-shot usage)
# --------------------------------------------------------------------------

def analyze_workload(
    data: Dict[str, Any],
    previous: Optional[Dict[str, Any]] = None,
    thresholds: Optional[LoadClassificationThresholds] = None,
) -> Dict[str, Any]:
    """
    One-shot convenience function: validate, analyze, and classify a
    single workload snapshot using Little's Law.

    This is the primary entry point intended for use by the Flask layer
    (e.g. a `/api/little-law/analyze` endpoint would call this directly),
    and by other mathematical modules in the pipeline that need to
    convert an arrival rate / response time pair (measured or
    USL-predicted) into occupancy metrics.

    Args:
        data: Dict with "arrival_rate" and "average_response_time" keys
            (plus optional "response_time_unit" and "metadata").
        previous: Optional previous analysis result, used only to derive
            trend signals.
        thresholds: Optional custom load classification thresholds.

    Returns:
        Structured Little's Law analysis dictionary.
    """
    analyzer = LittleLawAnalyzer(thresholds=thresholds)
    return analyzer.analyze(data, previous=previous)