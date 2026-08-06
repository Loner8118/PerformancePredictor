from __future__ import annotations

from typing import Any, Dict


__all__ = ["generate_recommendations"]


# ---------------------------------------------------------
# Thresholds
# ---------------------------------------------------------

CPU_WARNING = 75
CPU_CRITICAL = 85

MEMORY_WARNING = 75
MEMORY_CRITICAL = 90

QUEUE_WARNING = 0.75
QUEUE_CRITICAL = 0.90

CAPACITY_WARNING = 75
CAPACITY_CRITICAL = 90


# ---------------------------------------------------------
# Main API
# ---------------------------------------------------------


def generate_recommendations(
    capacity_results: Dict[str, Any],
    scalability_results: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Generate system recommendations from capacity analysis.
    """
    _ = scalability_results

    results = capacity_results.get(
        "results",
        {}
    )

    resource_pressure = results.get(
        "resource_pressure",
        {}
    )
    print(resource_pressure)

    calculations = capacity_results.get(
        "calculations",
        {}
    )


    current_users = (
        capacity_results
        .get("inputs", {})
        .get("runtime", {})
        .get("current_users", 0)
    )


    cpu = (
        capacity_results
        .get("inputs", {})
        .get("runtime", {})
        .get("cpu_usage", 0)
    )


    memory = (
        capacity_results
        .get("inputs", {})
        .get("runtime", {})
        .get("memory_usage", 0)
    )


    queue_utilization = (
        capacity_results
        .get("inputs", {})
        .get("queueing", {})
        .get("utilization", 0)
    )


    capacity_used = results.get(
        "capacity_used_percent",
        0
    )


    bottleneck = detect_bottleneck(
        cpu,
        memory,
        queue_utilization
    )


    health = analyze_health(
        cpu=cpu,
        memory=memory,
        queue=queue_utilization,
        capacity=capacity_used,
        response_time=(
            capacity_results
            .get("inputs", {})
            .get("runtime", {})
            .get("response_time", 0)
    ),
    error_rate=(
        capacity_results
        .get("inputs", {})
        .get("runtime", {})
        .get("error_rate", 0)
    ),
    scalability_efficiency=(
        results.get(
            "scalability_efficiency",
            1.0
        )
    )
)


    scaling = recommend_scaling(
        cpu,
        memory,
        queue_utilization,
        capacity_used,
        results
    )


    summary = generate_summary(
        health,
        bottleneck,
        current_users,
        results.get(
            "safe_users",
            0
        )
    )


    return {

        "system_health": health,

        "bottleneck": bottleneck,

        "scaling_recommendation": scaling,

        "capacity_summary": {

            "current_users": current_users,

            "safe_users": results.get(
                "safe_users",
                0
            ),

            "remaining_capacity": results.get(
                "growth_potential_users",
                0
            ),

            "capacity_used_percent": capacity_used,
        },


        "summary": summary,

    }



# ---------------------------------------------------------
# Health Analysis
# ---------------------------------------------------------


def analyze_health(
    cpu: float,
    memory: float,
    queue: float,
    capacity: float,
    response_time: float = 0,
    error_rate: float = 0,
    scalability_efficiency: float = 1.0,
) -> str:
    """
    Determine overall system health using
    runtime + mathematical model indicators.
    """

    critical_conditions = [
        cpu >= CPU_CRITICAL,
        memory >= MEMORY_CRITICAL,
        queue >= QUEUE_CRITICAL,
        capacity >= CAPACITY_CRITICAL,
        response_time >= 2000,
        error_rate >= 5,
        scalability_efficiency < 0.3,
    ]

    if any(critical_conditions):
        return "Critical"


    warning_conditions = [
        cpu >= CPU_WARNING,
        memory >= MEMORY_WARNING,
        queue >= QUEUE_WARNING,
        capacity >= CAPACITY_WARNING,
        response_time >= 1000,
        error_rate >= 1,
        scalability_efficiency < 0.6,
    ]


    if any(warning_conditions):
        return "Warning"


    return "Healthy"



# ---------------------------------------------------------
# Bottleneck Detection
# ---------------------------------------------------------


def detect_bottleneck(
    cpu: float,
    memory: float,
    queue: float,
) -> Dict[str, Any]:
    """
    Identify main system bottleneck.
    """

    bottlenecks = {

        "CPU": cpu,

        "Memory": memory,

        "Queue": queue * 100,

    }


    main = max(
        bottlenecks,
        key=bottlenecks.get
    )


    severity_value = bottlenecks[main]


    if severity_value >= 85:
        severity = "Critical"

    elif severity_value >= 70:
        severity = "High"

    else:
        severity = "Moderate"


    return {

        "type": main,

        "severity": severity,

    }



# ---------------------------------------------------------
# Scaling Recommendation
# ---------------------------------------------------------


def recommend_scaling(
    cpu: float,
    memory: float,
    queue: float,
    capacity: float,
    results: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Decide scaling action.
    """


    if cpu >= CPU_CRITICAL:

        return {

            "action":
            "Vertical Scaling",

            "reason":
            "CPU saturation detected",

            "recommendation":
            "Increase CPU allocation",

        }


    if memory >= MEMORY_CRITICAL:

        return {

            "action":
            "Vertical Scaling",

            "reason":
            "Memory pressure detected",

            "recommendation":
            "Increase RAM allocation",

        }



    if queue >= QUEUE_CRITICAL:

        return {

            "action":
            "Horizontal Scaling",

            "reason":
            "Queue congestion detected",

            "recommendation":
            "Add more application instances",

        }



    if capacity >= CAPACITY_WARNING:

        return {

            "action":
            "Prepare Scaling",

            "reason":
            "Capacity utilization increasing",

            "recommendation":
            "Monitor workload and scale before saturation",

        }



    return {

        "action":
        "No Scaling Required",

        "reason":
        "System operating within safe limits",

        "recommendation":
        "Continue monitoring",

    }



# ---------------------------------------------------------
# Summary Generator
# ---------------------------------------------------------


def generate_summary(
    health: str,
    bottleneck: Dict[str, Any],
    current_users: int,
    safe_users: float,
) -> str:
    """
    Human-readable explanation.
    """


    return (
        f"System status: {health}. "
        f"Primary bottleneck: "
        f"{bottleneck['type']}. "
        f"Current users: {current_users}. "
        f"Safe capacity: {safe_users} users."
    )