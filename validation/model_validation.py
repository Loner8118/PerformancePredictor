from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

from mathematical_engine.little_law import LittleLawAnalyzer, LittleLawValidationError
from mathematical_engine.queueing import QueueingAnalyzer, QueueingValidationError


class ModelValidationError(ValueError):
    """Bad input to this module (not raised for a single level's model
    failing to fit - see per-level "error" fields instead)."""


# ==========================================================================
# What this module does, and why it's NOT built like prediction_validation.py
# ==========================================================================
#
# prediction_validation.py exists because USL makes a genuinely forward-
# looking claim - "at 800 users, throughput will be X" - about a load
# level that, at fit time, has never been tested. That needed the whole
# freeze/hash/two-phase apparatus specifically to prove the claim was
# written down BEFORE the 800-user test happened.
#
# Little's Law and the queueing model don't make that kind of claim.
# compare_concurrency() and compare_system_time() both check "given
# THIS level's own arrival rate and service rate, does the model's
# prediction match what was ALSO observed at this SAME level" - both
# halves of the comparison come from the same single load-test run,
# with no forward-looking gap to defend. Little's Law and queueing.py's
# analyze() already run this comparison internally whenever
# observed_concurrency/observed_response_time are supplied - see
# little_law.py's concurrency_comparison and queueing.py's
# system_time_comparison. This module's entire job is orchestration
# over that existing machinery across every tested load level (the
# original 20-500 fitting range AND, once available, the 600+
# validation range from prediction_validation.py) and turning it into
# the aggregate error table a paper's Results section actually wants -
# no new comparison math is added here.
#
# A level missing what one model needs (e.g. no service_rate estimate
# for the queueing model) still gets checked by the OTHER model rather
# than failing the whole level - see validate_level()'s per-model error
# handling.


def _extract_little_law_fields(level: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "arrival_rate": level["arrival_rate"],
        "average_response_time": level["response_time"],
    }
    if level.get("observed_concurrency") is not None:
        data["observed_concurrency"] = level["observed_concurrency"]
    if level.get("service_time") is not None:
        data["service_time"] = level["service_time"]
    if level.get("observation_window") is not None:
        data["observation_window"] = level["observation_window"]
    return data


_QUEUEING_PASSTHROUGH_FIELDS = (
    "num_servers", "service_time_cv", "service_time_stdev",
    "service_time_p50", "service_time_p95", "service_time_unit",
)


def _extract_queueing_fields(level: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if level.get("service_rate") is None and level.get("service_time") is None:
        return None  # queueing.py cannot fit at all without one of these

    data: Dict[str, Any] = {"arrival_rate": level["arrival_rate"]}
    if level.get("service_rate") is not None:
        data["service_rate"] = level["service_rate"]
    else:
        data["service_time"] = level["service_time"]
    data["observed_response_time"] = level["response_time"]
    for field in _QUEUEING_PASSTHROUGH_FIELDS:
        if level.get(field) is not None:
            data[field] = level[field]
    return data


def validate_level(
    level: Dict[str, Any],
    concurrency_tolerance: float = 0.30,
    response_time_tolerance: float = 0.30,
) -> Dict[str, Any]:
    """
    Run both Little's Law and queueing consistency checks for ONE load
    level.

    Args:
        level: {
            "users": label for this level (e.g. Locust's configured
                concurrency) - display only, not fed into either model,
            "arrival_rate": throughput (req/s) - required by both models,
            "response_time": observed average response time (s) -
                required by both, used as Little's Law's W and as the
                queueing model's observed_response_time,
            "observed_concurrency": e.g. Locust's virtual-user count -
                optional, enables Little's Law's comparison,
            "service_rate" or "service_time": required for the queueing
                model to run at all - a level with neither still gets a
                Little's Law result, just no queueing result,
            "num_servers", "service_time_cv"/"service_time_stdev"/
                "service_time_p50"+"service_time_p95", "service_time_unit",
                "observation_window": all optional, passed through
                verbatim to the relevant analyzer.
        }

    Returns:
        {"users", "little_law": {"result", "error"}, "queueing": {"result", "error"}}
        - "result" is the full analyze() output (None if "error" is set).
        Errors here mean this one level's data couldn't be validated
        (e.g. missing a required field), not that the model disagreed
        with reality - a genuine disagreement is a normal, valid result
        with consistent=False in its comparison, not an error.

    Raises:
        ModelValidationError: level is missing arrival_rate/response_time,
            the two fields required by both models unconditionally.
    """
    if not isinstance(level, dict):
        raise ModelValidationError("level must be a dict.")
    if level.get("arrival_rate") is None or level.get("response_time") is None:
        raise ModelValidationError("level must include both 'arrival_rate' and 'response_time'.")

    little_law_result = None
    little_law_error = None
    try:
        little_law_result = LittleLawAnalyzer(concurrency_tolerance=concurrency_tolerance).analyze(
            _extract_little_law_fields(level)
        )
    except LittleLawValidationError as e:
        little_law_error = str(e)

    queueing_result = None
    queueing_error = None
    queueing_data = _extract_queueing_fields(level)
    if queueing_data is None:
        queueing_error = (
            "No 'service_rate' or 'service_time' supplied - the queueing model needs an "
            "estimate of service rate to run at all, unlike Little's Law."
        )
    else:
        try:
            queueing_result = QueueingAnalyzer(response_time_tolerance=response_time_tolerance).analyze(
                queueing_data
            )
        except QueueingValidationError as e:
            queueing_error = str(e)

    return {
        "users": level.get("users"),
        "little_law": {"result": little_law_result, "error": little_law_error},
        "queueing": {"result": queueing_result, "error": queueing_error},
    }


def _summarize_comparisons(comparisons: Sequence[Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    """
    Shared aggregator for both compare_concurrency() and
    compare_system_time() results - both return the same shape
    (absolute_difference/relative_difference/consistent), which is what
    makes one aggregator correct for either without duplicating it.
    """
    present = [c for c in comparisons if c is not None]
    usable = [c for c in present if c.get("relative_difference") is not None]

    if not present:
        return {"compared_count": 0, "consistent_count": 0, "mae": None, "rmse": None, "mape_percent": None}

    errors = [c["absolute_difference"] for c in usable]
    percentage_errors = [abs(c["relative_difference"]) * 100.0 for c in usable]
    consistent_count = sum(1 for c in present if c.get("consistent"))

    mae = (sum(abs(e) for e in errors) / len(errors)) if errors else None
    rmse = math.sqrt(sum(e * e for e in errors) / len(errors)) if errors else None
    mape = (sum(percentage_errors) / len(percentage_errors)) if percentage_errors else None

    return {
        "compared_count": len(present),
        "consistent_count": consistent_count,
        "mae": round(mae, 4) if mae is not None else None,
        "rmse": round(rmse, 4) if rmse is not None else None,
        "mape_percent": round(mape, 2) if mape is not None else None,
    }


def validate_levels(
    levels: Sequence[Dict[str, Any]],
    concurrency_tolerance: float = 0.30,
    response_time_tolerance: float = 0.30,
) -> Dict[str, Any]:
    """
    Run validate_level() across a whole series of tested load levels
    (typically the fitting range and, once available, the validation
    range from prediction_validation.py combined) and produce the
    aggregate error tables.

    Returns:
        {
          "levels": [per-level table rows - see _build_table_row()],
          "little_law_summary": {compared_count, consistent_count, mae, rmse, mape_percent},
          "queueing_summary": {compared_count, consistent_count, mae, rmse, mape_percent},
        }

    Raises:
        ModelValidationError: levels is empty, or any individual level
            is missing arrival_rate/response_time (see validate_level()).
    """
    if not levels:
        raise ModelValidationError("levels must not be empty.")

    per_level = [
        validate_level(level, concurrency_tolerance=concurrency_tolerance, response_time_tolerance=response_time_tolerance)
        for level in levels
    ]

    table = [_build_table_row(entry) for entry in per_level]

    concurrency_comparisons = [
        entry["little_law"]["result"].get("concurrency_comparison")
        for entry in per_level
        if entry["little_law"]["result"] is not None
    ]
    system_time_comparisons = [
        entry["queueing"]["result"].get("system_time_comparison")
        for entry in per_level
        if entry["queueing"]["result"] is not None
    ]

    return {
        "levels": table,
        "little_law_summary": _summarize_comparisons(concurrency_comparisons),
        "queueing_summary": _summarize_comparisons(system_time_comparisons),
    }


def _build_table_row(entry: Dict[str, Any]) -> Dict[str, Any]:
    """
    Flattens one validate_level() entry into the "Users | Predicted L |
    Observed L | Error | Predicted W | Observed W | Error" shape a
    paper's results table actually wants, pulling the relevant numbers
    out of each analyzer's full (much larger) result dict.
    """
    ll = entry["little_law"]["result"]
    ll_comparison = ll.get("concurrency_comparison") if ll else None
    q = entry["queueing"]["result"]
    q_comparison = q.get("system_time_comparison") if q else None

    return {
        "users": entry["users"],
        "little_law_error": entry["little_law"]["error"],
        "predicted_l": ll.get("requests_in_system") if ll else None,
        "observed_concurrency": ll_comparison.get("observed_concurrency") if ll_comparison else None,
        "l_percentage_error": (
            round(abs(ll_comparison["relative_difference"]) * 100, 2)
            if ll_comparison and ll_comparison.get("relative_difference") is not None else None
        ),
        "l_consistent": ll_comparison.get("consistent") if ll_comparison else None,
        "queueing_error": entry["queueing"]["error"],
        "predicted_system_time": q.get("system_time") if q else None,
        "observed_response_time": q_comparison.get("observed_response_time") if q_comparison else None,
        "w_percentage_error": (
            round(abs(q_comparison["relative_difference"]) * 100, 2)
            if q_comparison and q_comparison.get("relative_difference") is not None else None
        ),
        "w_consistent": q_comparison.get("consistent") if q_comparison else None,
    }
