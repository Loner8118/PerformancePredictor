from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence


class SLOValidationError(ValueError):
    """Bad input (not a real load-level record, or invalid thresholds)."""


# ==========================================================================
# What this module does
# ==========================================================================
#
# Everything else in the mathematical engine speaks in sigma, kappa, rho,
# service demand - correct, but not what most people asking "is this
# system ready for production" actually want to hear. SLO analysis
# reframes the same underlying data into the one question everyone
# already understands: does the app stay within an agreed response-time
# and error-rate budget, and up to how many users does that hold?
#
#     P95 < 500ms, error rate < 1%
#     100 users -> PASS
#     200 users -> PASS
#     300 users -> PASS
#     400 users -> PASS
#     500 users -> FAIL
#     SLO capacity = 400 users
#
# One deliberate design choice worth calling out: SLO capacity requires
# CONTINUITY, not just "the highest level that happens to individually
# pass". If 100/200/400 pass but 300 fails, the honest answer is "SLO
# capacity = 200", not 400 - a level failing and then a later level
# passing usually means measurement noise or a non-monotonic test order,
# not that 400 is actually safe while 300 in between it and the baseline
# isn't. Reporting 400 anyway would be exactly the kind of silently-
# contradictory result (a "safe" number sitting past a documented
# failure) this whole pipeline has otherwise gone out of its way to
# avoid - see capacity.py's safe_users/breaking_point fix for the same
# principle applied elsewhere.
#
# This module is deliberately data-source-agnostic: each level record is
# just {"users", "response_time", "error_rate_percent"}, tagged with an
# optional "source" ("observed" or "predicted"). That lets the exact
# same evaluate_slo()/determine_slo_capacity() logic serve two different
# callers:
#   - Real, ALREADY-TESTED load levels (response_time/error_rate straight
#     from csv_parser.py's p95/failure_rate for each tested user count) -
#     this gives a CONFIRMED SLO capacity, backed by actual measurements.
#   - scalability.py's PREDICTED levels (see from_scalability_predictions()
#     below) - this extends the same evaluation into untested territory,
#     giving a PROJECTED SLO capacity. Predicted levels use
#     predicted_response_time as a stand-in for P95, which is an average-
#     response-time-scaled approximation, not a true predicted P95 -
#     flagged explicitly in the adapter's docstring and in this module's
#     notes when predicted levels are present.
# The two are reported separately (slo_capacity_observed vs
# slo_capacity_overall) rather than merged into one number, so a report
# never implies a purely-extrapolated figure is as solid as a measured one.


DEFAULT_MAX_RESPONSE_TIME_SECONDS = 0.5
DEFAULT_MAX_ERROR_RATE_PERCENT = 1.0

_VALID_SOURCES = ("observed", "predicted")


@dataclass(frozen=True)
class SLOThresholds:
    max_response_time_seconds: float = DEFAULT_MAX_RESPONSE_TIME_SECONDS
    max_error_rate_percent: float = DEFAULT_MAX_ERROR_RATE_PERCENT

    def __post_init__(self) -> None:
        if not isinstance(self.max_response_time_seconds, (int, float)) or isinstance(self.max_response_time_seconds, bool):
            raise SLOValidationError("max_response_time_seconds must be numeric.")
        if not math.isfinite(self.max_response_time_seconds) or self.max_response_time_seconds <= 0:
            raise SLOValidationError("max_response_time_seconds must be a positive finite number.")
        if not isinstance(self.max_error_rate_percent, (int, float)) or isinstance(self.max_error_rate_percent, bool):
            raise SLOValidationError("max_error_rate_percent must be numeric.")
        if not math.isfinite(self.max_error_rate_percent) or not (0.0 <= self.max_error_rate_percent <= 100.0):
            raise SLOValidationError("max_error_rate_percent must be between 0 and 100.")


# --- Per-level evaluation ---

def _validate_level(level: Any, index: int) -> Dict[str, Any]:
    if not isinstance(level, dict):
        raise SLOValidationError(f"levels[{index}] must be a dict.")

    users = level.get("users")
    if isinstance(users, bool) or not isinstance(users, (int, float)):
        raise SLOValidationError(f"levels[{index}]['users'] must be numeric.")
    if not math.isfinite(users) or users <= 0:
        raise SLOValidationError(f"levels[{index}]['users'] must be a positive finite number.")

    response_time = level.get("response_time")
    if isinstance(response_time, bool) or not isinstance(response_time, (int, float)):
        raise SLOValidationError(f"levels[{index}]['response_time'] must be numeric.")
    if not math.isfinite(response_time) or response_time < 0:
        raise SLOValidationError(f"levels[{index}]['response_time'] must be a non-negative finite number.")

    error_rate_percent = level.get("error_rate_percent")
    if isinstance(error_rate_percent, bool) or not isinstance(error_rate_percent, (int, float)):
        raise SLOValidationError(f"levels[{index}]['error_rate_percent'] must be numeric.")
    if not math.isfinite(error_rate_percent) or not (0.0 <= error_rate_percent <= 100.0):
        raise SLOValidationError(f"levels[{index}]['error_rate_percent'] must be between 0 and 100.")

    source = level.get("source", "observed")
    if source not in _VALID_SOURCES:
        raise SLOValidationError(f"levels[{index}]['source'] must be one of {_VALID_SOURCES}, got {source!r}.")

    return {
        "users": float(users),
        "response_time": float(response_time),
        "error_rate_percent": float(error_rate_percent),
        "source": source,
    }


def evaluate_slo(
    users: float,
    response_time: float,
    error_rate_percent: float,
    thresholds: SLOThresholds,
    source: str = "observed",
) -> Dict[str, Any]:
    """
    Evaluate a single load level against the SLO. Reports each criterion
    separately (response_time_ok, error_rate_ok) rather than only the
    combined pass/fail, so a FAIL clearly shows WHICH budget was
    exceeded - "response time was fine but errors spiked" and "both blew
    the budget" call for different fixes, and collapsing that into one
    boolean would hide the distinction.
    """
    response_time_ok = response_time <= thresholds.max_response_time_seconds
    error_rate_ok = error_rate_percent <= thresholds.max_error_rate_percent

    return {
        "users": users,
        "response_time": response_time,
        "response_time_ok": response_time_ok,
        "error_rate_percent": error_rate_percent,
        "error_rate_ok": error_rate_ok,
        "passed": response_time_ok and error_rate_ok,
        "source": source,
    }


# --- SLO capacity determination ---

def determine_slo_capacity(evaluations: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """
    SLO capacity = the highest user level where the SLO holds
    CONTINUOUSLY from the lowest tested/predicted level up to that
    point - not merely the highest level that happens to individually
    pass. See module overview for why continuity matters: a level that
    fails followed by a later level that passes almost always means
    noise or an out-of-order test, not that the later level is actually
    safe.

    Returns {"slo_capacity": float | None, "first_failure_at": float | None}.
    slo_capacity is None if even the lowest level fails; first_failure_at
    is None if every level passes (capacity extends through the highest
    level tested/predicted, not necessarily beyond it).
    """
    ordered = sorted(evaluations, key=lambda e: e["users"])

    slo_capacity: Optional[float] = None
    first_failure_at: Optional[float] = None

    for evaluation in ordered:
        if evaluation["passed"]:
            slo_capacity = evaluation["users"]
        else:
            first_failure_at = evaluation["users"]
            break

    return {"slo_capacity": slo_capacity, "first_failure_at": first_failure_at}


# --- scalability.py bridge ---

def from_scalability_predictions(predictions: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Adapt scalability.predict_scalability()'s "predictions" list into
    this module's generic level-record shape, so that output can be
    passed straight into SLOAnalyzer.analyze() alongside (or instead of)
    real observed levels.

    Honesty note: predicted_response_time is scalability.py's average-
    response-time scaled off USL's throughput curve (see scalability.py's
    predict_response_time()), not a true predicted P95 - there's no
    percentile-level model anywhere in this pipeline for UNTESTED load
    levels, only for already-tested ones (csv_parser.py's real P95). SLO
    evaluation against a real P95 is meaningfully stricter than against
    an average, so a PREDICTED "PASS" here is a softer signal than an
    OBSERVED one - this is exactly why source="predicted" is tagged
    through, and why slo_capacity_observed and slo_capacity_overall are
    reported separately rather than merged.
    """
    return [
        {
            "users": p["users"],
            "response_time": p["predicted_response_time"],
            "error_rate_percent": p["predicted_error_rate"],
            "source": "predicted",
        }
        for p in predictions
    ]


# --- Analyzer ---

class SLOAnalyzer:
    """Evaluates a series of load levels (observed, predicted, or a mix)
    against a response-time/error-rate SLO, and determines the resulting
    SLO capacity."""

    def __init__(self, thresholds: Optional[SLOThresholds] = None) -> None:
        if thresholds is not None and not isinstance(thresholds, SLOThresholds):
            raise SLOValidationError(
                f"thresholds must be an SLOThresholds instance, got {type(thresholds).__name__}."
            )
        self.thresholds = thresholds or SLOThresholds()

    def analyze(self, levels: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Args:
            levels: [{"users": float, "response_time": float (seconds),
                      "error_rate_percent": float,
                      "source": "observed" | "predicted" (optional,
                      defaults "observed")}, ...]. Use
                      from_scalability_predictions() to build predicted
                      entries from scalability.predict_scalability()'s
                      output.

        Raises:
            SLOValidationError: empty/malformed levels.
        """
        if not isinstance(levels, (list, tuple)) or len(levels) == 0:
            raise SLOValidationError("levels must be a non-empty list of load-level records.")

        validated_levels = [_validate_level(level, i) for i, level in enumerate(levels)]

        evaluations = [
            evaluate_slo(
                users=lvl["users"],
                response_time=lvl["response_time"],
                error_rate_percent=lvl["error_rate_percent"],
                thresholds=self.thresholds,
                source=lvl["source"],
            )
            for lvl in validated_levels
        ]
        evaluations.sort(key=lambda e: e["users"])

        overall = determine_slo_capacity(evaluations)

        observed_evaluations = [e for e in evaluations if e["source"] == "observed"]
        observed = determine_slo_capacity(observed_evaluations) if observed_evaluations else None

        notes: List[str] = []
        has_predicted = any(e["source"] == "predicted" for e in evaluations)
        if has_predicted:
            notes.append(
                "Predicted levels evaluate an average-response-time-scaled figure against the "
                "SLO, not a true predicted P95 - treat a PASS on a predicted level as a softer "
                "signal than a PASS on an observed one."
            )
        if (
            observed is not None
            and observed["slo_capacity"] is not None
            and overall["slo_capacity"] is not None
            and overall["slo_capacity"] < observed["slo_capacity"]
        ):
            notes.append(
                f"Including predicted levels LOWERED the deduced SLO capacity ({overall['slo_capacity']:g} "
                f"vs {observed['slo_capacity']:g} users from observed data alone) - a predicted level "
                f"between two observed ones failed the SLO; worth checking whether that predicted "
                f"level is trustworthy (see usl.py's prediction_reliability) before treating this "
                f"as a real regression."
            )

        return {
            "thresholds": {
                "max_response_time_seconds": self.thresholds.max_response_time_seconds,
                "max_error_rate_percent": self.thresholds.max_error_rate_percent,
            },
            "evaluations": evaluations,
            "slo_capacity_observed": observed["slo_capacity"] if observed is not None else None,
            "first_failure_observed_at": observed["first_failure_at"] if observed is not None else None,
            "slo_capacity_overall": overall["slo_capacity"],
            "first_failure_overall_at": overall["first_failure_at"],
            "notes": notes,
        }


def analyze_slo(
    levels: Sequence[Dict[str, Any]],
    thresholds: Optional[SLOThresholds] = None,
) -> Dict[str, Any]:
    """One-shot: construct an SLOAnalyzer and run analyze()."""
    return SLOAnalyzer(thresholds=thresholds).analyze(levels)
