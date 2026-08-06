from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from prediction import usl, little_law, queueing, capacity, recommendation
from prediction import scalability

logger = logging.getLogger(__name__)

__all__ = ["analyze_system"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION: str = "1.0"
ENGINE_NAME: str = "Mathematical Prediction Engine"

REQUIRED_TOP_LEVEL_KEYS: Tuple[str, ...] = ("load_test", "runtime", "queue")
REQUIRED_LOAD_TEST_KEYS: Tuple[str, ...] = ("user_levels", "throughput")
REQUIRED_RUNTIME_KEYS: Tuple[str, ...] = (
    "current_users",
    "throughput",
    "response_time",
    "cpu_usage",
    "memory_usage",
    "error_rate",
)
REQUIRED_QUEUE_KEYS: Tuple[str, ...] = ("arrival_rate", "service_rate")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_system(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Run the complete mathematical prediction pipeline."""
    total_start = time.perf_counter()
    timing: Dict[str, float] = {
        "usl_time_ms": 0.0,
        "little_law_time_ms": 0.0,
        "queue_time_ms": 0.0,
        "capacity_time_ms": 0.0,
        "scalability_time_ms": 0.0,
        "recommendation_time_ms": 0.0,
    }

    try:
        _validate_metrics(metrics)
    except (TypeError, ValueError, KeyError) as exc:
        logger.exception("Metrics validation failed")
        return _build_failure_response(
            failed_module="validation",
            error=str(exc),
            total_start=total_start,
            timing=timing,
        )

    # ------------------------------------------------------------------
    # Step 1 – Universal Scalability Law
    # ------------------------------------------------------------------
    try:
        usl_input = _prepare_usl_input(metrics)
        t0 = time.perf_counter()
        usl_results = usl.run_usl_analysis(usl_input)

        print("\n========== DEBUG USL RESULT ==========")
        print(usl_results)
        print("======================================")

        timing["usl_time_ms"] = (time.perf_counter() - t0) * 1000.0
        logger.debug("USL analysis completed in %.2f ms", timing["usl_time_ms"])
    except Exception as exc:  # noqa: BLE001
        logger.exception("USL module failed")
        return _build_failure_response(
            failed_module="usl",
            error=str(exc),
            total_start=total_start,
            timing=timing,
        )

    # ------------------------------------------------------------------
    # Step 2 – Little's Law
    # ------------------------------------------------------------------
    try:
        little_input = _prepare_little_law_input(metrics)
        t0 = time.perf_counter()
        little_law_results = little_law.analyze_workload(little_input)
        timing["little_law_time_ms"] = (time.perf_counter() - t0) * 1000.0
        logger.debug(
            "Little's Law analysis completed in %.2f ms",
            timing["little_law_time_ms"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Little's Law module failed")
        return _build_failure_response(
            failed_module="little_law",
            error=str(exc),
            total_start=total_start,
            timing=timing,
        )

    # ------------------------------------------------------------------
    # Step 3 – Queueing Theory
    # ------------------------------------------------------------------
    try:
        queue_input = _prepare_queue_input(metrics)
        t0 = time.perf_counter()
        queue_results = queueing.analyze_queue(queue_input)
        timing["queue_time_ms"] = (time.perf_counter() - t0) * 1000.0
        logger.debug(
            "Queueing analysis completed in %.2f ms", timing["queue_time_ms"]
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Queueing module failed")
        return _build_failure_response(
            failed_module="queueing",
            error=str(exc),
            total_start=total_start,
            timing=timing,
        )

    # ------------------------------------------------------------------
    # Step 4 – Capacity Planning
    # ------------------------------------------------------------------
    try:
        capacity_input = _prepare_capacity_input(
            usl_results=usl_results,
            little_law_results=little_law_results,
            queue_results=queue_results,
            runtime_metrics=metrics["runtime"],
        )
        t0 = time.perf_counter()

        print("\n========== DEBUG CAPACITY INPUT ==========")
        print(capacity_input)
        print("==========================================")

        capacity_results = capacity.analyze_capacity(capacity_input)
        timing["capacity_time_ms"] = (time.perf_counter() - t0) * 1000.0
        logger.debug(
            "Capacity analysis completed in %.2f ms",
            timing["capacity_time_ms"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Capacity module failed")
        return _build_failure_response(
            failed_module="capacity",
            error=str(exc),
            total_start=total_start,
            timing=timing,
        )
    

    # ------------------------------------------------------------------
    # Step 5 – Scalability Prediction
    # ------------------------------------------------------------------
    try:

        prediction_targets = [
            1000,
            1500,
            2000,
            2500,
            3000,
            3500,
            4000,
            4500,
            5000,
        ]

        t0 = time.perf_counter()

        scalability_results = scalability.predict_scalability(
            usl_results=usl_results,
            capacity_results=capacity_results,
            runtime_metrics=metrics["runtime"],
            prediction_targets=prediction_targets,
        )

        timing["scalability_time_ms"] = (
            time.perf_counter() - t0
        ) * 1000.0

        logger.debug(
            "Scalability prediction completed in %.2f ms",
            timing["scalability_time_ms"],
        )

    except Exception as exc:

        logger.exception(
            "Scalability module failed"
        )

        return _build_failure_response(
            failed_module="scalability",
            error=str(exc),
            total_start=total_start,
            timing=timing,
        )

    # step 6 – Recommendation Generation
    try:
    
            t0 = time.perf_counter()
    
            recommendation_results = (
                recommendation.generate_recommendations(
                    capacity_results=capacity_results,
                    scalability_results=scalability_results,
                )
            )
    
            timing["recommendation_time_ms"] = (
                time.perf_counter() - t0
            ) * 1000.0
    
    
            logger.debug(
                "Recommendation analysis completed in %.2f ms",
                timing["recommendation_time_ms"]
            )
    
    
    except Exception as exc:  # noqa: BLE001
    
        logger.exception(
            "Recommendation module failed"
        )
    
        return _build_failure_response(
            failed_module="recommendation",
            error=str(exc),
            total_start=total_start,
            timing=timing,
        )

    # ------------------------------------------------------------------
    # Step 7 – Assemble success response
    # ------------------------------------------------------------------
    total_ms = (time.perf_counter() - total_start) * 1000.0
    metadata = _collect_metadata(
        analysis_time_ms=total_ms,

        usl_time_ms=timing["usl_time_ms"],

        little_law_time_ms=
            timing["little_law_time_ms"],

        queue_time_ms=
            timing["queue_time_ms"],

        capacity_time_ms=
            timing["capacity_time_ms"],

        scalability_time_ms=timing["scalability_time_ms"],

        recommendation_time_ms=
            timing["recommendation_time_ms"],
    )

    logger.info(
        "Pipeline completed successfully in %.2f ms", total_ms
    )

    return {
        "status": "success",

        "usl": usl_results,

        "little_law": little_law_results,

        "queueing": queue_results,

        "capacity": capacity_results,

        "scalability": scalability_results,

        "recommendation": recommendation_results,

        "metadata": metadata,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _validate_metrics(metrics: Dict[str, Any]) -> None:
    """Validate the overall structure of the metrics payload."""
    if not isinstance(metrics, dict):
        raise TypeError("metrics must be a dictionary")

    for key in REQUIRED_TOP_LEVEL_KEYS:
        if key not in metrics:
            raise KeyError(f"Missing required top-level key: '{key}'")

    load_test = metrics["load_test"]
    if not isinstance(load_test, dict):
        raise ValueError("'load_test' must be a dictionary")
    for key in REQUIRED_LOAD_TEST_KEYS:
        if key not in load_test:
            raise KeyError(f"Missing required load_test key: '{key}'")

    user_levels = load_test["user_levels"]
    throughput = load_test["throughput"]
    if not isinstance(user_levels, (list, tuple)):
        raise ValueError("'load_test.user_levels' must be a list or tuple")
    if not isinstance(throughput, (list, tuple)):
        raise ValueError("'load_test.throughput' must be a list or tuple")
    if len(user_levels) != len(throughput):
        raise ValueError(
            "user_levels and throughput must have equal length."
        )
    if len(user_levels) < 3:
        raise ValueError(
            "USL requires at least 3 observations."
        )

    runtime = metrics["runtime"]
    if not isinstance(runtime, dict):
        raise ValueError("'runtime' must be a dictionary")
    for key in REQUIRED_RUNTIME_KEYS:
        if key not in runtime:
            raise KeyError(f"Missing required runtime key: '{key}'")

    queue = metrics["queue"]
    if not isinstance(queue, dict):
        raise ValueError("'queue' must be a dictionary")
    for key in REQUIRED_QUEUE_KEYS:
        if key not in queue:
            raise KeyError(f"Missing required queue key: '{key}'")


def _prepare_usl_input(metrics: Dict[str, Any]) -> Dict[str, Any]:
    load_test = metrics["load_test"]
    return {
        "user_levels": load_test["user_levels"],
        "throughput": load_test["throughput"],
    }


def _prepare_little_law_input(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Transform raw metrics into the dictionary expected by Little's Law."""
    runtime = metrics["runtime"]
    return {
        "current_users": runtime["current_users"],
        "throughput": runtime["throughput"],
        "response_time": runtime["response_time"],
        "arrival_rate": runtime.get("arrival_rate", runtime["throughput"]),
        "average_response_time": runtime.get("average_response_time", runtime["response_time"]),
    }


def _prepare_queue_input(metrics: Dict[str, Any]) -> Dict[str, Any]:
    queue = metrics["queue"]
    return {
        "arrival_rate": queue["arrival_rate"],
        "service_rate": queue["service_rate"],
    }


def _prepare_capacity_input(
    usl_results: Dict[str, Any],
    little_law_results: Dict[str, Any],
    queue_results: Dict[str, Any],
    runtime_metrics: Dict[str, Any],
) -> Dict[str, Any]:

    return {

        "usl": {
            "peak_throughput": usl_results["peak_throughput"],
            "optimal_users": usl_results["optimal_users"],
            "saturation_point": usl_results["saturation_point"],
            "scalability_efficiency": usl_results["scalability_efficiency"],
        },


        "little_law": {
            "requests_in_system": little_law_results[
                "requests_in_system"
            ],

            "time_in_system": little_law_results["time_in_system"],
        },


        "queueing": {
            "utilization": queue_results["utilization"],
            "queue_length": queue_results["queue_length"],
            "waiting_time": queue_results["waiting_time"],
            "stability": queue_results["stability"],
        },


        "runtime": {

            "current_users": runtime_metrics["current_users"],

            "throughput": runtime_metrics["throughput"],

            "response_time": runtime_metrics["response_time"],

            "cpu_usage": runtime_metrics["cpu_usage"],

            "memory_usage": runtime_metrics["memory_usage"],

            "disk_io": runtime_metrics.get(
                "disk_io",
                0.0
            ),

            "network_io": runtime_metrics.get(
                "network_io",
                0.0
            ),

            "error_rate": runtime_metrics["error_rate"],
        }
    }


def _collect_metadata(
    analysis_time_ms,
    usl_time_ms,
    little_law_time_ms,
    queue_time_ms,
    capacity_time_ms,
    scalability_time_ms,
    recommendation_time_ms,
) -> Dict[str, Any]:

    return {
        "analysis_time_ms": round(analysis_time_ms, 2),
        "usl_time_ms": round(usl_time_ms, 2),
        "little_law_time_ms": round(little_law_time_ms, 2),
        "queue_time_ms": round(queue_time_ms, 2),
        "capacity_time_ms": round(capacity_time_ms, 2),
        "scalability_time_ms": round(scalability_time_ms, 2),
        "recommendation_time_ms": round(
            recommendation_time_ms,
            2
        ),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": VERSION,
        "engine": ENGINE_NAME,
    }


def _build_failure_response(
    failed_module: str,
    error: str,
    total_start: float,
    timing: Dict[str, float],
) -> Dict[str, Any]:
    total_ms = (time.perf_counter() - total_start) * 1000.0
    metadata = {
        "analysis_time_ms": round(total_ms, 2),
        "usl_time_ms": round(timing.get("usl_time_ms", 0.0), 2),
        "little_law_time_ms": round(timing.get("little_law_time_ms", 0.0), 2),
        "queue_time_ms": round(timing.get("queue_time_ms", 0.0), 2),
        "capacity_time_ms": round(timing.get("capacity_time_ms", 0.0), 2),
        "scalability_time_ms": round(timing.get("scalability_time_ms", 0.0), 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": VERSION,
        "engine": ENGINE_NAME,
    }
    return {
        "status": "failed",
        "failed_module": failed_module,
        "error": error,
        "metadata": metadata,
    }