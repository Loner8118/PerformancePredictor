from __future__ import annotations

"""
recommendation.py
==================

Recommendation Engine — the final stage of the performance-prediction
pipeline:

    Locust -> Runtime Monitoring -> USL -> Little's Law -> Queueing Theory
           -> Capacity Planning -> Scalability Prediction -> Recommendation Engine

This module performs NO mathematical modelling of its own. It consumes the
already-computed outputs of the upstream modules (usl.py, little_law.py,
queueing.py, capacity.py, scalability.py) and converts them into
structured, evidence-based, explainable engineering recommendations.

Design rules (see project spec):
    * Never invent metrics or bottlenecks.
    * Never claim a specific root cause (e.g. "the database is the
      bottleneck") unless the available evidence actually supports it —
      use "investigate" / "may indicate" / "consider" language instead.
    * Treat an unstable queue (queue_stability == "unstable") as always
      worth a CRITICAL warning that any calculated safe-capacity number
      isn't reliable for planning yet - regardless of the exact
      safe_users value, since capacity.py derives safe_users from
      breaking_point (which only caps growth at current_users when
      unstable, it doesn't zero out) rather than the fragile literal
      safe_users == 0 check this used to rely on.
    * Avoid duplicate/near-duplicate recommendations — when several
      models flag the same underlying risk, merge them into one
      stronger recommendation with combined evidence.
    * Every recommendation carries a priority, category, problem
      statement, evidence, concrete actions, expected impact, and a
      confidence rating.
    * Reuse thresholds/constants already defined in capacity.py rather
      than re-inventing them.

Primary entry point: `generate_recommendations(...)`.
"""

from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Reuse existing thresholds from capacity.py instead of duplicating them.
# Falls back to the same defaults if capacity.py isn't importable in this
# environment (e.g. running recommendation.py in isolation / unit tests).
# ---------------------------------------------------------------------------
try:
    from capacity import (
        CPU_SAFE_LIMIT,
        MEMORY_SAFE_LIMIT,
        RESPONSE_TIME_THRESHOLD_MS,
        DEFAULT_P95_RESPONSE_TIME_THRESHOLD_MS,
        ERROR_RATE_THRESHOLDS,
        CAPACITY_MARGIN_THRESHOLDS,
        QUEUE_UTILIZATION_THRESHOLDS,
        QUEUE_LENGTH_CRITICAL_THRESHOLD,
    )
except ImportError:  # pragma: no cover - defensive fallback only
    CPU_SAFE_LIMIT = 85.0
    MEMORY_SAFE_LIMIT = 85.0
    RESPONSE_TIME_THRESHOLD_MS = 2000.0
    DEFAULT_P95_RESPONSE_TIME_THRESHOLD_MS = 1000.0
    ERROR_RATE_THRESHOLDS = {"Moderate": 1.0, "High": 5.0}
    CAPACITY_MARGIN_THRESHOLDS = {"Healthy": 40.0, "Near Capacity": 20.0, "At Capacity": 5.0}
    QUEUE_UTILIZATION_THRESHOLDS = {"Low": 0.5, "Moderate": 0.7, "High": 0.85}
    QUEUE_LENGTH_CRITICAL_THRESHOLD = 10.0


# --- Exceptions ---

class RecommendationEngineError(Exception):
    """Base exception for the recommendation engine."""


class RecommendationValidationError(RecommendationEngineError):
    """Raised when a required upstream module output is missing/malformed."""


# --- Priority / category ordering ---

class Priority:
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


_PRIORITY_ORDER = [Priority.CRITICAL, Priority.HIGH, Priority.MEDIUM, Priority.LOW, Priority.INFO]
_PRIORITY_RANK = {p: i for i, p in enumerate(_PRIORITY_ORDER)}

# Tie-break ordering within the same priority (spec section 21).
_CATEGORY_RANK = {
    "Queueing": 0,
    "Error Rate": 1,
    "Bottleneck Analysis": 2,
    "Optimization Ceiling": 3,
    "Capacity": 4,
    "Response Time": 5,
    "CPU": 6,
    "Memory": 7,
    "Disk I/O": 8,
    "Network I/O": 9,
    "USL Scalability": 10,
    "Little's Law": 11,
    "Scalability Prediction": 12,
    "Scaling Strategy": 13,
    "Database": 14,
}
_DEFAULT_CATEGORY_RANK = 99


# --- Small helpers ---

class _IdGenerator:
    """Sequential, per-prefix recommendation IDs, e.g. QUEUE-001, CPU-002."""

    def __init__(self) -> None:
        self._counters: Dict[str, int] = {}

    def next(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]:03d}"


def _rec(
    id_gen: _IdGenerator,
    prefix: str,
    priority: str,
    category: str,
    title: str,
    problem: str,
    evidence: Dict[str, Any],
    recommendation: str,
    actions: List[str],
    expected_impact: str,
    confidence: str,
) -> Dict[str, Any]:
    return {
        "id": id_gen.next(prefix),
        "priority": priority,
        "category": category,
        "title": title,
        "problem": problem,
        "evidence": evidence,
        "recommendation": recommendation,
        "actions": actions,
        "expected_impact": expected_impact,
        "confidence": confidence,
    }


def _round(value: Any, nd: int = 2) -> Any:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if value != value or value in (float("inf"), float("-inf")):
                return value
            return round(float(value), nd)
        except (TypeError, ValueError):
            return value
    return value


def _get(d: Optional[Dict[str, Any]], *path: str, default: Any = None) -> Any:
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


# --- A. Capacity recommendations ---

_CAPACITY_CLASSIFICATION_PRIORITY = {
    "Healthy": Priority.INFO,
    "Near Capacity": Priority.MEDIUM,
    "At Capacity": Priority.HIGH,
    "Overloaded": Priority.HIGH,
    "Critical": Priority.CRITICAL,
}

_CAPACITY_CLASSIFICATION_MESSAGE = {
    "Healthy": "Current workload remains within the calculated safe capacity.",
    "Near Capacity": "The system is operating close to its safe capacity. Additional traffic may reduce performance stability.",
    "At Capacity": "The system is operating at or near its safe capacity. Increasing workload without additional capacity may cause degradation.",
    "Overloaded": "Current workload exceeds the recommended safe operating range.",
    "Critical": "The system requires immediate capacity or workload intervention.",
}


def _capacity_recommendations(id_gen: _IdGenerator, results: Dict[str, Any]) -> List[Dict[str, Any]]:
    classification = results.get("capacity_classification")
    if classification is None:
        return []

    priority = _CAPACITY_CLASSIFICATION_PRIORITY.get(classification, Priority.LOW)
    message = _CAPACITY_CLASSIFICATION_MESSAGE.get(
        classification, f"Capacity classification: {classification}."
    )

    safe_users = results.get("safe_users")
    current_users = results.get("current_users")
    remaining_capacity = results.get("growth_potential_users")
    capacity_margin = results.get("capacity_margin")
    breaking_point = results.get("breaking_point")

    evidence = {
        "capacity_classification": classification,
        "safe_users": _round(safe_users),
        "current_users": _round(current_users),
        "remaining_capacity": _round(remaining_capacity),
        "capacity_margin_percent": _round(capacity_margin),
        "breaking_point_users": _round(breaking_point),
        "capacity_reference_reason": results.get("capacity_reference_reason"),
    }

    if classification == "Healthy":
        actions = [
            "Continue routine monitoring of capacity margin",
            "Re-run capacity analysis periodically as traffic patterns change",
        ]
        impact = "No corrective action needed; current headroom is sufficient for the observed workload."
        confidence = "High"
    else:
        actions = [
            "Increase infrastructure capacity (CPU, memory, or instance count) ahead of further growth",
            "Reduce workload before reaching the calculated safe capacity limit",
            "Establish autoscaling triggers below the safe-capacity threshold",
            "Avoid operating beyond the calculated safe user limit",
            "Monitor capacity margin closely as load approaches the breaking point",
        ]
        impact = "Restores comfortable headroom between current load and the safe operating limit, reducing risk of degradation under further growth."
        confidence = "High" if results.get("extrapolation_reliable") else "Medium"

    return [
        _rec(
            id_gen,
            "CAP",
            priority,
            "Capacity",
            title=f"Capacity status: {classification}",
            problem=(
                f"Capacity planning classifies current operating conditions as '{classification}', "
                f"with a capacity margin of {_round(capacity_margin)}% relative to the calculated "
                f"safe user limit of {_round(safe_users)}."
            ),
            evidence=evidence,
            recommendation=message,
            actions=actions,
            expected_impact=impact,
            confidence=confidence,
        )
    ]


def _zero_capacity_special_case(id_gen: _IdGenerator, results: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Spec section 16 / queue-instability special case.

    Originally keyed off safe_users being exactly 0.0, back when
    calculate_safe_capacity() in capacity.py zeroed out safe_users
    directly whenever the queue was unstable. capacity.py now derives
    safe_users from breaking_point instead (see capacity.py's
    calculate_safe_capacity docstring for why - it closes a safe_users-
    vs-breaking_point contradiction the old design allowed), and
    breaking_point only CAPS growth at current_users when the queue is
    unstable rather than zeroing out - so safe_users is no longer
    reliably exactly 0.0 in this situation; it depends on whatever the
    binding constraint (queue/CPU/memory/capacity_reference) happens to
    be. The unstable queue itself is always the real thing worth
    flagging as CRITICAL here, independent of what specific number
    safe_users lands on - so that's checked directly now instead of an
    indirect, coincidental numeric equality.
    """
    stability = str(results.get("queue_stability", "")).lower()
    if stability != "unstable":
        return []

    safe_users = results.get("safe_users")

    return [
        _rec(
            id_gen,
            "CAP-ZERO",
            Priority.CRITICAL,
            "Capacity",
            title="Queue is unstable - safe capacity can't be trusted for planning",
            problem=(
                "The queueing model shows requests arriving faster than the system can process "
                "them (utilization at or above 100%), so the queue keeps growing without bound "
                "instead of settling into a steady state. Any 'safe capacity' figure calculated "
                "from this data is not a reliable planning number until this is resolved."
            ),
            evidence={
                "safe_users": _round(safe_users),
                "queue_stability": results.get("queue_stability"),
                "queue_utilization": results.get("queue_utilization"),
                "current_users": _round(results.get("current_users")),
            },
            recommendation=(
                "Treat any capacity number from this run as unreliable until queue stability is "
                "restored. Resolve the underlying overload before using predictions to plan for "
                "higher user loads."
            ),
            actions=[
                "Increase service/processing capacity (more workers, more instances)",
                "Reduce incoming request rate until the queue returns to a stable state",
                "Re-run capacity and scalability analysis once utilization is below 100%",
                "Do not use current predictions to plan for higher user loads until resolved",
            ],
            expected_impact=(
                "Once queue stability is restored, the calculated safe-capacity figure becomes "
                "a trustworthy number for future-load planning."
            ),
            confidence="High",
        )
    ]


# --- B. CPU recommendations ---

def _cpu_recommendations(id_gen: _IdGenerator, results: Dict[str, Any]) -> List[Dict[str, Any]]:
    pressure = _get(results, "resource_pressure", "cpu")
    cpu_usage = results.get("current_cpu_usage")

    if pressure not in ("High", "Critical") or cpu_usage is None:
        return []

    priority = Priority.CRITICAL if pressure == "Critical" else Priority.HIGH

    return [
        _rec(
            id_gen,
            "CPU",
            priority,
            "CPU",
            title=(
                "CPU utilization at critical level" if pressure == "Critical"
                else "CPU utilization approaching safe limit"
            ),
            problem=(
                f"CPU usage is currently {_round(cpu_usage)}%, classified as '{pressure}' pressure "
                f"against a configured safe threshold of {CPU_SAFE_LIMIT}%."
            ),
            evidence={
                "cpu_usage_percent": _round(cpu_usage),
                "cpu_safe_threshold_percent": CPU_SAFE_LIMIT,
                "current_users": _round(results.get("current_users")),
                "estimated_cpu_capacity_multiplier_at_breaking_point": _round(
                    results.get("estimated_cpu_capacity_multiplier")
                ),
            },
            recommendation=(
                "CPU is approaching the configured safe threshold. Consider vertical scaling "
                "or CPU optimization before increasing concurrent users."
            ),
            actions=[
                "Increase CPU allocation for the application instance(s)",
                "Profile and optimize CPU-intensive code paths",
                "Reduce unnecessary computation on the hot request path",
                "Optimize serialization/deserialization of request and response payloads",
                "Improve caching to avoid repeated computation",
                "Move CPU-heavy tasks to background workers/queues",
                "Consider horizontal scaling if CPU pressure persists after optimization",
            ],
            expected_impact=(
                "Reduces CPU contention and lowers the risk of CPU-driven latency and "
                "error-rate increases as load grows."
            ),
            confidence="High",
        )
    ]


# --- C. Memory recommendations ---

def _memory_recommendations(id_gen: _IdGenerator, results: Dict[str, Any]) -> List[Dict[str, Any]]:
    pressure = _get(results, "resource_pressure", "memory")
    memory_usage = results.get("current_memory_usage")

    if pressure not in ("High", "Critical") or memory_usage is None:
        return []

    priority = Priority.CRITICAL if pressure == "Critical" else Priority.HIGH

    return [
        _rec(
            id_gen,
            "MEM",
            priority,
            "Memory",
            title=(
                "Memory utilization at critical level" if pressure == "Critical"
                else "Memory utilization approaching safe limit"
            ),
            problem=(
                f"Memory usage is currently {_round(memory_usage)}%, classified as '{pressure}' "
                f"pressure against a configured safe threshold of {MEMORY_SAFE_LIMIT}%."
            ),
            evidence={
                "memory_usage_percent": _round(memory_usage),
                "memory_safe_threshold_percent": MEMORY_SAFE_LIMIT,
                "current_users": _round(results.get("current_users")),
                "estimated_memory_capacity_multiplier_at_breaking_point": _round(
                    results.get("estimated_memory_capacity_multiplier")
                ),
            },
            recommendation=(
                "Memory usage is approaching the configured safe threshold. Consider vertical "
                "scaling or memory optimization before increasing concurrent users."
            ),
            actions=[
                "Increase memory allocation for the application instance(s)",
                "Investigate potential memory leaks under sustained load",
                "Reduce the amount of data held in memory per request",
                "Optimize or bound in-memory caching",
                "Stream large responses/files instead of buffering them fully",
                "Avoid loading entire datasets into memory; use pagination",
                "Consider horizontal scaling if memory pressure persists after optimization",
            ],
            expected_impact=(
                "Reduces risk of out-of-memory failures, GC pressure, and memory-driven "
                "latency degradation as load grows."
            ),
            confidence="High",
        )
    ]


# --- D. Queue recommendations ---

def _queue_recommendations(id_gen: _IdGenerator, results: Dict[str, Any]) -> List[Dict[str, Any]]:
    stability = str(results.get("queue_stability", "")).lower()
    utilization = results.get("queue_utilization")
    queue_length = results.get("queue_length")
    waiting_time = results.get("queue_waiting_time")

    if stability == "unstable":
        return [
            _rec(
                id_gen,
                "QUEUE",
                Priority.CRITICAL,
                "Queueing",
                title="Queue instability detected",
                problem=(
                    "The queueing model indicates the current workload exceeds sustainable "
                    "service capacity (utilization at or above 1.0), producing unbounded "
                    "queue growth."
                ),
                evidence={
                    "utilization": _round(utilization),
                    "queue_length": _round(queue_length) if queue_length != float("inf") else "infinite",
                    "waiting_time": _round(waiting_time) if waiting_time != float("inf") else "infinite",
                    "stability": results.get("queue_stability"),
                },
                recommendation=(
                    "Queueing model indicates an unstable system. The current workload exceeds "
                    "sustainable service capacity. Do not extrapolate current performance "
                    "linearly. Increase processing capacity or reduce arrival rate before "
                    "scaling workload further."
                ),
                actions=[
                    "Reduce incoming request rate (rate limiting, load shedding, backpressure)",
                    "Increase processing capacity (more workers, more application instances)",
                    "Optimize average request service time",
                    "Add horizontal capacity to distribute concurrent load",
                    "Investigate the specific bottleneck causing the service-time deficit",
                ],
                expected_impact=(
                    "Restores queue stability and prevents unbounded growth of waiting time "
                    "and queued requests."
                ),
                confidence="High",
            )
        ]

    if utilization is None:
        return []

    if utilization >= QUEUE_UTILIZATION_THRESHOLDS["High"]:
        priority, title, message = (
            Priority.HIGH,
            "Queue utilization very high",
            "Queue utilization is very high. The system is approaching or exceeding sustainable service capacity.",
        )
        actions = [
            "Increase processing capacity (workers/instances)",
            "Reduce request arrival rate where possible",
            "Optimize average request service time",
            "Introduce asynchronous processing for slow operations",
            "Investigate the endpoints contributing most to service time",
        ]
    elif utilization >= QUEUE_UTILIZATION_THRESHOLDS["Moderate"]:
        priority, title, message = (
            Priority.MEDIUM,
            "Queue utilization approaching sustainable limit",
            "Queue utilization is approaching the sustainable operating limit. Additional load "
            "may significantly increase waiting time.",
        )
        actions = [
            "Increase processing capacity ahead of further load growth",
            "Optimize average request service time",
            "Monitor queue length and waiting time closely",
        ]
    elif utilization >= QUEUE_UTILIZATION_THRESHOLDS["Low"]:
        priority, title, message = (
            Priority.LOW,
            "Queue utilization increasing",
            "Queue utilization is increasing. Monitor waiting time and queue length as workload grows.",
        )
        actions = [
            "Continue monitoring queue utilization, length, and waiting time",
            "Establish alerting thresholds ahead of the sustainable limit",
        ]
    else:
        return []

    if queue_length is not None and queue_length != float("inf") and queue_length >= QUEUE_LENGTH_CRITICAL_THRESHOLD:
        priority = Priority.HIGH if _PRIORITY_RANK[priority] > _PRIORITY_RANK[Priority.HIGH] else priority

    return [
        _rec(
            id_gen,
            "QUEUE",
            priority,
            "Queueing",
            title=title,
            problem=f"Queue utilization is currently {_round(utilization)}.",
            evidence={
                "utilization": _round(utilization),
                "queue_length": _round(queue_length),
                "waiting_time": _round(waiting_time),
            },
            recommendation=message,
            actions=actions,
            expected_impact="Keeps queue waiting time and length within acceptable bounds as load grows.",
            confidence="High",
        )
    ]


# --- E. USL recommendations (contention / coherency / efficiency / saturation) ---

_USL_SIGMA_HIGH_THRESHOLD = 0.05
_USL_KAPPA_HIGH_THRESHOLD = 0.001
_USL_EFFICIENCY_LOW_THRESHOLD = 0.5
_USL_EFFICIENCY_VERY_LOW_THRESHOLD = 0.3


def _usl_recommendations(
    id_gen: _IdGenerator,
    usl_results: Optional[Dict[str, Any]],
    capacity_results: Dict[str, Any],
) -> List[Dict[str, Any]]:
    recs: List[Dict[str, Any]] = []

    sigma = _get(usl_results, "parameters", "sigma")
    kappa = _get(usl_results, "parameters", "kappa")
    reliability_status = _get(usl_results, "prediction_reliability", "status")
    usable_for_extrapolation = _get(usl_results, "prediction_reliability", "usable_for_extrapolation")

    scalability_efficiency = capacity_results.get("scalability_efficiency")

    if sigma is not None and sigma >= _USL_SIGMA_HIGH_THRESHOLD:
        recs.append(
            _rec(
                id_gen,
                "USL-SIGMA",
                Priority.HIGH if sigma >= _USL_SIGMA_HIGH_THRESHOLD * 2 else Priority.MEDIUM,
                "USL Scalability",
                title="Shared resources are limiting how well the app scales",
                problem=(
                    "As more users are added, throughput isn't growing as fast as it should. "
                    "This pattern usually means something shared - a database connection pool, "
                    "a lock, or a single-threaded step - is creating a bottleneck that gets "
                    "worse the more traffic comes in."
                ),
                evidence={"sigma": _round(sigma, 4), "threshold_used": _USL_SIGMA_HIGH_THRESHOLD},
                recommendation=(
                    "Look for a shared resource that many requests compete for at the same time "
                    "- a database connection pool, a lock, or a step that can only run one at a "
                    "time - and see if it can be widened, cached, or removed."
                ),
                actions=[
                    "Investigate shared-resource contention (locks, connection pools, semaphores)",
                    "Profile the application under concurrent load to locate serialized sections",
                    "Consider increasing connection pool sizes or reducing lock scope",
                    "Investigate database connection contention and slow queries",
                ],
                expected_impact="Fixing this lets throughput keep growing as more users are added, instead of leveling off early.",
                confidence="High" if usable_for_extrapolation else "Medium",
            )
        )

    if kappa is not None and kappa >= _USL_KAPPA_HIGH_THRESHOLD:
        recs.append(
            _rec(
                id_gen,
                "USL-KAPPA",
                Priority.HIGH if kappa >= _USL_KAPPA_HIGH_THRESHOLD * 2 else Priority.MEDIUM,
                "USL Scalability",
                title="Performance may get worse, not just slower, at very high load",
                problem=(
                    "Our model predicts that beyond a certain number of users, adding more "
                    "traffic will actually make the app slower overall - not just stop it from "
                    "getting faster. This usually happens when different parts of the system "
                    "have to coordinate or stay in sync with each other, and that coordination "
                    "cost grows faster than the extra traffic being handled."
                ),
                evidence={"kappa": _round(kappa, 6), "threshold_used": _USL_KAPPA_HIGH_THRESHOLD},
                recommendation=(
                    "Look for places where different parts of the app have to coordinate or "
                    "stay in sync when many requests happen at once - shared caches, database "
                    "locks, or communication between services - since these often cause "
                    "performance to fall off a cliff instead of leveling off gracefully."
                ),
                actions=[
                    "Investigate shared-state synchronization and coordination points",
                    "Review inter-service communication patterns for excessive coordination",
                    "Investigate database locking and contention under concurrent load",
                    "Consider reducing coordination requirements (e.g. sharding, partitioning)",
                ],
                expected_impact="Delays or removes the point at which the app starts getting slower as load grows, instead of just leveling off.",
                confidence="High" if usable_for_extrapolation else "Medium",
            )
        )

    if scalability_efficiency is not None and scalability_efficiency < _USL_EFFICIENCY_LOW_THRESHOLD:
        priority = (
            Priority.HIGH if scalability_efficiency < _USL_EFFICIENCY_VERY_LOW_THRESHOLD else Priority.MEDIUM
        )
        recs.append(
            _rec(
                id_gen,
                "USL-EFF",
                priority,
                "USL Scalability",
                title="Adding more users isn't paying off the way it should",
                problem=(
                    f"Scalability efficiency is {_round(scalability_efficiency * 100, 1)}%, meaning "
                    f"each additional user is producing noticeably less extra throughput than "
                    f"a well-scaling system should deliver."
                ),
                evidence={"scalability_efficiency": _round(scalability_efficiency)},
                recommendation=(
                    "Efficiency is low - increasing the number of users is producing "
                    "substantially less throughput growth than expected."
                ),
                actions=[
                    "Investigate contention and coordination overhead (see the scalability findings above)",
                    "Inspect database query patterns and connection-pool sizing under load",
                    "Optimize shared resources and reduce synchronization overhead",
                    "Consider horizontal scaling to distribute load across more instances",
                ],
                expected_impact="Improves how much throughput is gained from each additional concurrent user.",
                confidence="Medium",
            )
        )

    if reliability_status == "low" or usable_for_extrapolation is False:
        recs.append(
            _rec(
                id_gen,
                "USL-REL",
                Priority.LOW,
                "USL Scalability",
                title="USL model fit has low prediction reliability",
                problem=(
                    "The USL model's fit quality is low, so extrapolated predictions beyond the "
                    "tested user range should be treated with caution."
                ),
                evidence={
                    "prediction_reliability_status": reliability_status,
                    "usable_for_extrapolation": usable_for_extrapolation,
                    "r2": _get(usl_results, "fit_quality", "r2"),
                },
                recommendation=(
                    "USL fit confidence is low. Predictions extrapolated beyond the tested user "
                    "range should be interpreted cautiously; gather additional load-test data "
                    "points to improve fit reliability."
                ),
                actions=[
                    "Run additional Locust load tests at more user levels, especially near expected peak load",
                    "Avoid making firm capacity commitments based on low-confidence extrapolation",
                ],
                expected_impact="Improves confidence in future capacity and scalability predictions.",
                confidence="High",
            )
        )

    return recs


# --- F. Little's Law recommendations ---

def _little_law_recommendations(
    id_gen: _IdGenerator,
    little_law_results: Optional[Dict[str, Any]],
    capacity_results: Dict[str, Any],
) -> List[Dict[str, Any]]:
    recs: List[Dict[str, Any]] = []

    classification = _get(little_law_results, "load_classification")
    requests_in_system = _get(little_law_results, "requests_in_system")
    response_time = _get(little_law_results, "response_time")
    signals = _get(little_law_results, "signals") or {}

    if classification in ("High", "Very High", "Critical") and requests_in_system is not None:
        priority = Priority.HIGH if classification in ("Very High", "Critical") else Priority.MEDIUM
        recs.append(
            _rec(
                id_gen,
                "LL-OCC",
                priority,
                "Little's Law",
                title=f"High concurrent request occupancy ({classification})",
                problem=(
                    f"Little's Law calculates {_round(requests_in_system, 1)} requests concurrently "
                    f"in the system on average, classified as '{classification}'."
                ),
                evidence={
                    "requests_in_system": _round(requests_in_system, 1),
                    "load_classification": classification,
                    "response_time_seconds": _round(response_time),
                },
                recommendation=(
                    "A large number of requests are simultaneously present in the system. "
                    "Investigate response-time and queueing behavior before increasing load further."
                ),
                actions=[
                    "Cross-check with the queueing analysis for utilization and wait-time trends",
                    "Profile slow endpoints contributing most to time-in-system",
                    "Consider increasing processing capacity ahead of further load growth",
                ],
                expected_impact="Reduces the number of requests held concurrently in the system, easing memory/connection pressure.",
                confidence="High",
            )
        )

    if response_time is not None and response_time > (RESPONSE_TIME_THRESHOLD_MS / 1000.0):
        recs.append(
            _rec(
                id_gen,
                "LL-RT",
                Priority.MEDIUM,
                "Little's Law",
                title="Response time is a major contributor to concurrency",
                problem=(
                    f"Average time in system is {_round(response_time, 3)}s. The longer each "
                    f"request takes, the more requests end up stacked up in the system at the "
                    f"same time, even without any change in how many arrive per second."
                ),
                evidence={"response_time_seconds": _round(response_time, 3)},
                recommendation=(
                    "Response time is contributing significantly to the number of concurrent "
                    "requests in the system. Optimize slow operations before increasing concurrency."
                ),
                actions=[
                    "Profile and optimize the slowest request-handling code paths",
                    "Reduce blocking I/O or synchronous external calls on the request path",
                ],
                expected_impact="Lowering response time reduces concurrent occupancy for the same arrival rate.",
                confidence="High",
            )
        )

    little_law_consistency = capacity_results.get("little_law_consistency") or {}
    if little_law_consistency.get("consistent") is False:
        recs.append(
            _rec(
                id_gen,
                "LL-CONSIST",
                Priority.LOW,
                "Little's Law",
                title="Little's Law consistency check failed",
                problem=(
                    f"The arrival rate derived from Little's Law differs from observed throughput "
                    f"by {little_law_consistency.get('difference_percent')}%, exceeding the "
                    f"20% consistency tolerance."
                ),
                evidence=little_law_consistency,
                recommendation=(
                    "Little's Law inputs show a significant difference between derived arrival "
                    "rate and observed throughput. Results should be interpreted cautiously "
                    "because the workload may not be in steady state."
                ),
                actions=[
                    "Verify measurements were taken over a stable (steady-state) observation window",
                    "Re-check that arrival rate and response-time measurements correspond to the same period",
                ],
                expected_impact="Improves confidence in downstream capacity calculations that depend on this consistency check.",
                confidence="Medium",
            )
        )

    if signals.get("critical_occupancy") and not any(r["id"].startswith("LL-OCC") for r in recs):
        pass  # already covered by classification check above

    return recs


# --- G. Response-time recommendations ---

def _response_time_recommendations(id_gen: _IdGenerator, results: Dict[str, Any]) -> List[Dict[str, Any]]:
    recs: List[Dict[str, Any]] = []
    response_time_ms = results.get("current_response_time")
    p95 = _get(results, "runtime", "p95_response_time")  # not present in results directly; kept for completeness

    if response_time_ms is not None and response_time_ms > RESPONSE_TIME_THRESHOLD_MS:
        severity = Priority.CRITICAL if response_time_ms > RESPONSE_TIME_THRESHOLD_MS * 2 else Priority.HIGH
        recs.append(
            _rec(
                id_gen,
                "RT-AVG",
                severity,
                "Response Time",
                title="Average response time exceeds threshold",
                problem=(
                    f"Average response time is {_round(response_time_ms)}ms, exceeding the "
                    f"configured performance threshold of {RESPONSE_TIME_THRESHOLD_MS}ms."
                ),
                evidence={
                    "response_time_ms": _round(response_time_ms),
                    "threshold_ms": RESPONSE_TIME_THRESHOLD_MS,
                },
                recommendation=(
                    "Response time exceeds the configured performance threshold. Profile slow "
                    "endpoints and identify expensive operations."
                ),
                actions=[
                    "Profile the slowest endpoints under representative load",
                    "Identify and optimize expensive database queries or external calls",
                    "Check whether CPU, memory, or queue pressure is contributing to latency",
                ],
                expected_impact="Reduces end-user latency and the number of requests held concurrently in the system.",
                confidence="High",
            )
        )

    return recs


# --- H. Error-rate recommendations ---

def _error_rate_recommendations(id_gen: _IdGenerator, results: Dict[str, Any]) -> List[Dict[str, Any]]:
    error_rate = results.get("current_error_rate")
    if error_rate is None or error_rate < ERROR_RATE_THRESHOLDS["Moderate"]:
        return []

    if error_rate >= ERROR_RATE_THRESHOLDS["High"]:
        priority = Priority.CRITICAL
        message = (
            "Error rate is high and may indicate overload, resource exhaustion, application "
            "failures, or dependency failures."
        )
        actions = [
            "Inspect application logs for the failing time window",
            "Inspect which endpoints/dependencies are producing failures",
            "Check for resource exhaustion (CPU, memory, connection pools) at the time of errors",
            "Reduce workload or increase capacity until the error rate returns to baseline",
        ]
    else:
        priority = Priority.MEDIUM
        message = "Error rate is elevated. Investigate failed requests before increasing workload."
        actions = [
            "Sample and inspect failed requests to identify common causes",
            "Confirm whether errors correlate with resource pressure or specific endpoints",
        ]

    return [
        _rec(
            id_gen,
            "ERR",
            priority,
            "Error Rate",
            title="Elevated error rate detected",
            problem=f"Current error rate is {_round(error_rate)}%.",
            evidence={
                "error_rate_percent": _round(error_rate),
                "moderate_threshold_percent": ERROR_RATE_THRESHOLDS["Moderate"],
                "high_threshold_percent": ERROR_RATE_THRESHOLDS["High"],
            },
            recommendation=message,
            actions=actions,
            expected_impact="Restores request success rate and prevents error-driven capacity loss.",
            confidence="High",
        )
    ]


# --- I. Disk I/O and Network I/O recommendations ---

def _disk_io_recommendations(id_gen: _IdGenerator, results: Dict[str, Any]) -> List[Dict[str, Any]]:
    pressure = _get(results, "resource_pressure", "disk")
    if pressure not in ("High", "Critical"):
        return []

    priority = Priority.HIGH if pressure == "Critical" else Priority.MEDIUM
    return [
        _rec(
            id_gen,
            "DISK",
            priority,
            "Disk I/O",
            title="Disk I/O pressure elevated",
            problem=f"Disk I/O pressure is classified as '{pressure}' relative to the configured disk I/O limit.",
            evidence={"disk_io_pressure": pressure},
            recommendation=(
                "Disk I/O pressure is high. Investigate frequent file access, excessive logging, "
                "and inefficient storage operations."
            ),
            actions=[
                "Investigate excessive disk reads/writes on the request path",
                "Optimize or batch file operations",
                "Introduce or improve caching to reduce repeated disk access",
                "Reduce logging overhead or move to asynchronous/buffered logging",
                "Consider faster storage if I/O remains the bottleneck",
            ],
            expected_impact="Reduces disk-driven latency and contention under load.",
            confidence="Medium",
        )
    ]


def _network_io_recommendations(id_gen: _IdGenerator, results: Dict[str, Any]) -> List[Dict[str, Any]]:
    pressure = _get(results, "resource_pressure", "network")
    if pressure not in ("High", "Critical"):
        return []

    priority = Priority.HIGH if pressure == "Critical" else Priority.MEDIUM
    return [
        _rec(
            id_gen,
            "NET",
            priority,
            "Network I/O",
            title="Network I/O pressure elevated",
            problem=f"Network I/O pressure is classified as '{pressure}' relative to the configured network I/O limit.",
            evidence={"network_io_pressure": pressure},
            recommendation=(
                "Network I/O pressure is high. Reduce response payload sizes and unnecessary "
                "network communication before increasing workload."
            ),
            actions=[
                "Enable response compression",
                "Reduce response payload size; return only required fields",
                "Use pagination for large collections",
                "Increase caching to avoid repeated network calls",
                "Reuse connections where possible; consider a CDN for static content",
                "Reduce unnecessary internal API calls",
            ],
            expected_impact="Reduces network-driven latency and bandwidth pressure under load.",
            confidence="Medium",
        )
    ]


# ---------------------------------------------------------------------------
# J2. Bottleneck Analysis recommendations (Utilization Law / bottleneck.py)
#
# This is strictly more rigorous evidence than the raw CPU/Disk/Network
# threshold checks above: instead of comparing each resource against its
# own independent threshold, it identifies which resource has the
# highest per-request service demand (D_i) - the one that will actually
# limit throughput first - and derives a hard theoretical throughput
# ceiling from it. When this data is available and confirms a specific
# resource as the bottleneck, generate_recommendations() suppresses the
# corresponding threshold-based CPU/Disk/Network recommendation for that
# resource so the two don't duplicate each other (spec section 20).
# ---------------------------------------------------------------------------

_BOTTLENECK_SEVERITY_PRIORITY = {
    "At or beyond theoretical ceiling": Priority.CRITICAL,
    "Near ceiling": Priority.HIGH,
    "Moderate headroom": Priority.LOW,
    # "Substantial headroom" and "Unknown" are intentionally absent -
    # nothing actionable to report when there's ample headroom.
}


def _bottleneck_recommendations(id_gen: _IdGenerator, results: Dict[str, Any]) -> List[Dict[str, Any]]:
    bottleneck_resource = results.get("bottleneck_resource")
    if bottleneck_resource is None:
        return []  # bottleneck.py data wasn't supplied for this run - nothing to report

    severity = results.get("bottleneck_severity")
    service_demand = results.get("bottleneck_service_demand")
    asymptotic_bound = results.get("asymptotic_throughput_bound")
    n_star = results.get("optimal_concurrency_n_star")
    exceeds_bound = bool(results.get("throughput_exceeds_asymptotic_bound"))
    current_throughput = results.get("current_throughput")
    current_users = results.get("current_users")

    resource_label = "CPU" if bottleneck_resource == "cpu" else str(bottleneck_resource).capitalize()

    evidence = {
        "bottleneck_resource": bottleneck_resource,
        "service_demand_seconds_per_request": _round(service_demand, 6),
        "asymptotic_throughput_bound": _round(asymptotic_bound),
        "optimal_concurrency_n_star": _round(n_star),
        "current_throughput": _round(current_throughput),
        "current_users": _round(current_users),
        "bottleneck_severity": severity,
    }

    if exceeds_bound:
        return [
            _rec(
                id_gen,
                "BOTTLENECK",
                Priority.CRITICAL,
                "Bottleneck Analysis",
                title="Measured performance doesn't match what the numbers say should be possible",
                problem=(
                    f"The app is currently handling {_round(current_throughput)} requests/sec, "
                    f"but based on how much work {resource_label} does per request "
                    f"({_round(service_demand * 1000, 2)}ms), it shouldn't be able to sustain more "
                    f"than about {_round(asymptotic_bound)} requests/sec. Since that number is "
                    f"meant to be a hard ceiling given the current measurements, either something "
                    f"was measured incorrectly, or the configured maximum capacity for "
                    f"{resource_label} is set lower than what the deployment can actually handle."
                ),
                evidence=evidence,
                recommendation=(
                    "Double-check the configured maximum-capacity assumption used for this "
                    "resource (e.g. disk/network throughput limits) and confirm runtime metrics "
                    "are being captured correctly - a measured value shouldn't be able to exceed "
                    "a correctly-configured ceiling."
                ),
                actions=[
                    f"Verify the configured maximum capacity used for {resource_label} reflects the real deployment",
                    "Cross-check runtime metric collection for measurement errors",
                    "Re-run bottleneck analysis once the configuration is corrected",
                ],
                expected_impact="Resolves the inconsistency so the derived throughput ceiling can be trusted for capacity planning.",
                confidence="Medium",
            )
        ]

    priority = _BOTTLENECK_SEVERITY_PRIORITY.get(severity)
    if priority is None:
        return []  # Substantial headroom / unknown - nothing actionable to report

    return [
        _rec(
            id_gen,
            "BOTTLENECK",
            priority,
            "Bottleneck Analysis",
            title=f"{resource_label} is the main thing limiting speed right now",
            problem=(
                f"Among the resources being monitored (CPU, disk, network), {resource_label} does "
                f"the most work per request ({_round(service_demand * 1000, 2)}ms), which makes it "
                f"the first one likely to run out of headroom as traffic grows. Based on that, the "
                f"app shouldn't be able to sustain much more than {_round(asymptotic_bound)} "
                f"requests/sec - current throughput ({_round(current_throughput)} req/s) is "
                f"classified as '{severity}' relative to that limit."
            ),
            evidence=evidence,
            recommendation=(
                f"{resource_label} is the main constraint right now. Prioritize {resource_label} "
                f"optimization or capacity increases over other resources - improving something "
                f"else won't raise the ceiling while {resource_label} remains the limiting factor."
            ),
            actions=[
                f"Prioritize {resource_label} capacity increases or optimization over other resources",
                f"Re-run bottleneck analysis after addressing {resource_label} to confirm the bottleneck has shifted",
                "Keep in mind this only covers monitored resources - an unmonitored one (e.g. a database) could also be a limiting factor",
            ],
            expected_impact=(
                f"Addressing the {resource_label} bottleneck directly raises how much traffic the "
                f"app can handle, unlike improving a resource that isn't the constraint."
            ),
            confidence="High",
        )
    ]


# ---------------------------------------------------------------------------
# J2b. Optimization Ceiling recommendations (Amdahl's Law / amdahl.py)
#
# Bottleneck Analysis above answers "which resource limits throughput
# first". This answers the natural follow-up question it doesn't:
# "how much would actually fixing it help, and is that worth the
# effort?" A resource can be the clear bottleneck and still offer a
# disappointing ceiling if it's not a large share of total demand - this
# is what catches that before engineering time is spent on it.
# ---------------------------------------------------------------------------

def _amdahl_recommendations(
    id_gen: _IdGenerator,
    amdahl_results: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not isinstance(amdahl_results, dict) or not amdahl_results.get("available"):
        return []  # amdahl.py data wasn't supplied, or bottleneck data underlying it wasn't usable

    bottleneck_analysis = amdahl_results.get("bottleneck_analysis")
    if not isinstance(bottleneck_analysis, dict):
        return []

    resource = bottleneck_analysis.get("resource")
    if not resource:
        return []

    resource_label = "CPU" if resource == "cpu" else str(resource).capitalize()
    fraction = bottleneck_analysis.get("fraction_of_total_demand")
    max_speedup = bottleneck_analysis.get("max_theoretical_speedup")
    unbounded = bool(bottleneck_analysis.get("max_theoretical_speedup_unbounded"))
    illustrative = bottleneck_analysis.get("illustrative_speedups") or {}
    implied_max_throughput = bottleneck_analysis.get("implied_max_throughput")
    next_bottleneck = bottleneck_analysis.get("next_bottleneck_after_optimization")
    current_throughput = amdahl_results.get("current_throughput")

    evidence = {
        "resource": resource,
        "fraction_of_total_demand": fraction,
        "max_theoretical_speedup": max_speedup,
        "illustrative_speedups": illustrative,
        "implied_max_throughput": implied_max_throughput,
        "current_throughput": current_throughput,
        "next_bottleneck_after_optimization": next_bottleneck,
    }

    if unbounded:
        priority = Priority.INFO
        ceiling_phrase = "no meaningful ceiling could be computed (only one resource is currently monitored)"
    elif isinstance(max_speedup, (int, float)) and max_speedup < 1.3:
        priority = Priority.LOW
        ceiling_phrase = f"a maximum theoretical speedup of only {max_speedup:.2f}x"
    else:
        priority = Priority.MEDIUM
        ceiling_phrase = (
            f"a maximum theoretical speedup of {max_speedup:.2f}x" if isinstance(max_speedup, (int, float))
            else "an unbounded ceiling"
        )

    problem = (
        f"{resource_label} accounts for {fraction * 100:.1f}% of total measured service demand "
        f"among monitored resources. Even optimizing it to be infinitely fast caps overall "
        f"throughput improvement at {ceiling_phrase}"
    )
    if isinstance(implied_max_throughput, (int, float)) and isinstance(current_throughput, (int, float)):
        problem += f" (from {current_throughput:.1f} req/s up to at most {implied_max_throughput:.1f} req/s)."
    else:
        problem += "."
    if next_bottleneck:
        problem += (
            f" Beyond that point, {next_bottleneck.get('resource')} would become the new limiting "
            f"factor (doing {next_bottleneck.get('service_demand') * 1000:.2f}ms of work per request)."
        )

    recommendation_text = f"Before investing significant engineering effort into optimizing {resource_label}, weigh it against this ceiling"
    recommendation_text += f" of {max_speedup:.2f}x. " if isinstance(max_speedup, (int, float)) else ". "
    if next_bottleneck:
        recommendation_text += (
            f"Since {next_bottleneck.get('resource')} would become the next constraint, a durable "
            f"improvement likely needs both resources addressed, not just {resource_label} alone."
        )
    else:
        recommendation_text += (
            f"Among monitored resources, optimizing {resource_label} is the highest-leverage single change available."
        )

    actions = [
        f"Weigh the engineering cost of optimizing {resource_label} against its throughput ceiling before committing effort",
    ]
    if next_bottleneck:
        actions.append(
            f"Plan for {next_bottleneck.get('resource')} improvements alongside {resource_label}, "
            f"since it becomes the next constraint once {resource_label} is addressed"
        )
    actions.append("Re-run bottleneck and Amdahl analysis after any optimization to confirm the ceiling moved as expected")

    return [
        _rec(
            id_gen,
            "AMDAHL",
            priority,
            "Optimization Ceiling",
            title=f"Optimization ceiling for {resource_label}: {ceiling_phrase}",
            problem=problem,
            evidence=evidence,
            recommendation=recommendation_text,
            actions=actions,
            expected_impact=(
                f"Sets a realistic upper bound on throughput improvement from optimizing "
                f"{resource_label} alone, so effort can be weighed against the likely payoff "
                f"before it's spent."
            ),
            confidence="Medium",
        )
    ]


# --- J. Database recommendations (evidence-gated, never asserted as fact) ---

def _database_recommendations(
    id_gen: _IdGenerator,
    results: Dict[str, Any],
    usl_results: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    sigma = _get(usl_results, "parameters", "sigma")
    high_contention = sigma is not None and sigma >= _USL_SIGMA_HIGH_THRESHOLD
    high_response_time = (results.get("current_response_time") or 0) > RESPONSE_TIME_THRESHOLD_MS
    queue_pressure = _get(results, "resource_pressure", "queue") in ("High", "Critical")

    if not (high_contention or high_response_time or queue_pressure):
        return []

    reasons = []
    if high_contention:
        reasons.append("throughput is being limited by a shared resource under concurrent load")
    if high_response_time:
        reasons.append("average response time exceeds the configured threshold")
    if queue_pressure:
        reasons.append("queue pressure is high")

    return [
        _rec(
            id_gen,
            "DB",
            Priority.MEDIUM,
            "Database",
            title="Investigate potential database contention",
            problem=(
                "Multiple indicators (" + "; ".join(reasons) + ") are consistent with, but do not "
                "confirm, database-related contention as a contributing factor."
            ),
            evidence={
                "usl_sigma": _round(sigma, 4) if sigma is not None else None,
                "response_time_ms": _round(results.get("current_response_time")),
                "queue_pressure": _get(results, "resource_pressure", "queue"),
            },
            recommendation=(
                "Investigate database connection contention, slow queries, locking, and "
                "connection-pool exhaustion. This is a suggested investigation area, not a "
                "confirmed root cause — the available metrics do not directly measure the "
                "database layer."
            ),
            actions=[
                "Profile database query latency and identify slow queries",
                "Check connection-pool utilization and exhaustion under load",
                "Review locking behavior during concurrent write-heavy operations",
                "Confirm whether database operations are limiting request-processing capacity",
            ],
            expected_impact="If confirmed, resolving database contention typically improves both response time and USL scalability efficiency.",
            confidence="Low",
        )
    ]


# --- K. Scalability prediction recommendations ---

def _scalability_prediction_recommendations(
    id_gen: _IdGenerator,
    scalability_results: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    recs: List[Dict[str, Any]] = []
    summary = _get(scalability_results, "summary") or {}

    first_overloaded = summary.get("first_overloaded_point")
    first_collapsed = summary.get("first_collapsed_point")
    recommended_max = summary.get("recommended_max_users")
    highest_cpu = summary.get("highest_cpu")
    highest_memory = summary.get("highest_memory")

    if first_overloaded is not None:
        recs.append(
            _rec(
                id_gen,
                "SCALE-OVER",
                Priority.HIGH,
                "Scalability Prediction",
                title="Predicted overload point identified",
                problem=(
                    f"The scalability model predicts the system will reach an 'Overloaded' "
                    f"classification starting at approximately {_round(first_overloaded)} "
                    f"concurrent users."
                ),
                evidence={
                    "first_overloaded_point_users": _round(first_overloaded),
                    "recommended_max_users": _round(recommended_max),
                },
                recommendation=(
                    f"Predicted overload begins at approximately {_round(first_overloaded)} "
                    f"concurrent users. Avoid operating beyond this level without additional "
                    f"capacity or optimization."
                ),
                actions=[
                    "Plan capacity increases before reaching the predicted overload point",
                    "Re-validate the prediction with real load tests as traffic approaches this level",
                    "Prioritize the CPU/memory/queue/USL findings in this report, since they drive this prediction",
                ],
                expected_impact="Provides advance warning to add capacity or optimize before real users experience degradation.",
                confidence="Medium",
            )
        )

    if first_collapsed is not None:
        recs.append(
            _rec(
                id_gen,
                "SCALE-COLLAPSE",
                Priority.CRITICAL,
                "Scalability Prediction",
                title="Predicted critical degradation region identified",
                problem=(
                    f"The scalability model predicts a critical failure/degradation region "
                    f"(modelled, not observed) beginning around {_round(first_collapsed)} "
                    f"concurrent users."
                ),
                evidence={"first_collapsed_point_users": _round(first_collapsed)},
                recommendation=(
                    f"The scalability model predicts a critical failure/degradation region "
                    f"beginning around {_round(first_collapsed)} concurrent users. This is a "
                    f"modelled projection, not an observed crash — treat it as an upper bound "
                    f"to avoid without validation."
                ),
                actions=[
                    "Treat this level as an operational ceiling until capacity is increased",
                    "Do not schedule marketing/traffic events that would approach this user level",
                    "Re-run load tests at intermediate levels to validate the predicted trend",
                ],
                expected_impact="Prevents planning for user levels the current architecture is not modelled to sustain.",
                confidence="Medium",
            )
        )

    if highest_cpu is not None and highest_cpu >= CPU_SAFE_LIMIT:
        recs.append(
            _rec(
                id_gen,
                "SCALE-CPU",
                Priority.MEDIUM,
                "Scalability Prediction",
                title="Predicted CPU usage approaches safe limit at higher load",
                problem=(
                    f"At the highest evaluated prediction target, predicted CPU usage reaches "
                    f"{_round(highest_cpu)}%, at or above the {CPU_SAFE_LIMIT}% safe threshold."
                ),
                evidence={"predicted_highest_cpu_percent": _round(highest_cpu), "cpu_safe_threshold": CPU_SAFE_LIMIT},
                recommendation=(
                    "Predicted CPU utilization approaches the safe threshold at higher load "
                    "levels. Plan CPU capacity increases or optimization ahead of that growth."
                ),
                actions=[
                    "Plan vertical or horizontal CPU capacity ahead of predicted growth",
                    "Prioritize CPU optimization work identified elsewhere in this report",
                ],
                expected_impact="Avoids CPU-driven degradation as traffic approaches predicted levels.",
                confidence="Medium",
            )
        )

    if highest_memory is not None and highest_memory >= MEMORY_SAFE_LIMIT:
        recs.append(
            _rec(
                id_gen,
                "SCALE-MEM",
                Priority.MEDIUM,
                "Scalability Prediction",
                title="Predicted memory usage approaches safe limit at higher load",
                problem=(
                    f"At the highest evaluated prediction target, predicted memory usage reaches "
                    f"{_round(highest_memory)}%, at or above the {MEMORY_SAFE_LIMIT}% safe threshold."
                ),
                evidence={"predicted_highest_memory_percent": _round(highest_memory), "memory_safe_threshold": MEMORY_SAFE_LIMIT},
                recommendation=(
                    "Predicted memory utilization approaches the safe threshold at higher load "
                    "levels. Plan memory capacity increases or optimization ahead of that growth."
                ),
                actions=[
                    "Plan vertical or horizontal memory capacity ahead of predicted growth",
                    "Prioritize memory optimization work identified elsewhere in this report",
                ],
                expected_impact="Avoids memory-driven degradation as traffic approaches predicted levels.",
                confidence="Medium",
            )
        )

    return recs


def _asymptotic_bound_recommendations(
    id_gen: _IdGenerator,
    scalability_results: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Flags the point where USL's own fitted curve exceeds the independently
    -derived asymptotic throughput ceiling (bottleneck.py's Utilization
    Law / Asymptotic Bounds analysis, cross-checked per prediction inside
    scalability.py). This is a stronger, more direct signal than
    SCALE-COLLAPSE above: that one says "the curve shape suggests
    collapse"; this one says "the curve's own prediction is not
    physically achievable, independent of its shape" - two different
    models agreeing is significant evidence the extrapolation has broken
    down at this point.
    """
    summary = _get(scalability_results, "summary") or {}
    first_bound_exceeded = summary.get("first_bound_exceeded_point")
    if first_bound_exceeded is None:
        return []

    predictions = _get(scalability_results, "predictions") or []
    matching = next((p for p in predictions if p.get("users") == first_bound_exceeded), None)

    evidence = {"first_bound_exceeded_point_users": _round(first_bound_exceeded)}
    if matching:
        evidence.update({
            "predicted_throughput": matching.get("predicted_throughput"),
            "asymptotic_throughput_bound": matching.get("asymptotic_throughput_bound"),
            "bound_binding_constraint": matching.get("bound_binding_constraint"),
        })

    return [
        _rec(
            id_gen,
            "SCALE-BOUND",
            Priority.CRITICAL,
            "Scalability Prediction",
            title="Our growth model predicts more traffic than the system can physically handle",
            problem=(
                f"Starting around {_round(first_bound_exceeded)} concurrent users, our growth "
                f"prediction calls for more throughput than the app's own resource usage says is "
                f"actually possible. Two independent calculations disagreeing like this is a "
                f"stronger warning sign than a simple slowdown - it means the growth prediction "
                f"itself can no longer be trusted at and beyond this point, regardless of what "
                f"shape the prediction curve follows."
            ),
            evidence=evidence,
            recommendation=(
                "Don't rely on the raw growth prediction at or beyond this point. Use the "
                "capped/bounded number reported alongside each prediction instead - it reflects "
                "what's actually achievable given current resource usage, not just where the "
                "growth curve extrapolates to."
            ),
            actions=[
                "Prioritize resolving the identified bottleneck resource before planning for load beyond this level",
                "Use each prediction's bounded throughput value, not the raw USL prediction, beyond this point",
                "Re-validate with real load tests as traffic approaches this level - two independent models now agree it's a problem area",
            ],
            expected_impact="Prevents capacity planning decisions based on a USL extrapolation that has diverged from physical reality.",
            confidence="High",
        )
    ]


def _amdahl_ceiling_scalability_recommendations(
    id_gen: _IdGenerator,
    scalability_results: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    A stronger sibling of SCALE-BOUND above: that one flags predictions
    exceeding the CURRENT asymptotic bound (impossible today, but
    possibly fixable). This flags predictions exceeding the OPTIMIZED-
    bottleneck ceiling (amdahl.py's implied_max_throughput) - meaning
    even fixing the current bottleneck entirely wouldn't be enough to
    reach that load level, so planning needs to look beyond a single
    resource fix.
    """
    summary = _get(scalability_results, "summary") or {}
    first_point = summary.get("first_amdahl_ceiling_exceeded_point")
    if first_point is None:
        return []

    predictions = _get(scalability_results, "predictions") or []
    matching = next((p for p in predictions if p.get("users") == first_point), None)

    evidence = {"first_amdahl_ceiling_exceeded_point_users": _round(first_point)}
    if matching:
        evidence.update({
            "predicted_throughput": matching.get("predicted_throughput"),
            "amdahl_max_throughput_ceiling": matching.get("amdahl_max_throughput_ceiling"),
        })

    return [
        _rec(
            id_gen,
            "SCALE-AMDAHL",
            Priority.CRITICAL,
            "Scalability Prediction",
            title="Reaching this load needs more than fixing one resource",
            problem=(
                f"Starting around {_round(first_point)} concurrent users, the predicted traffic is "
                f"more than the app could handle even if the current bottleneck resource were "
                f"completely eliminated. Fixing just that one resource - whatever it is - won't be "
                f"enough to get there; something else would take over as the limiting factor first."
            ),
            evidence=evidence,
            recommendation=(
                "A single-resource optimization will not be sufficient to reach this load level. "
                "Plan capacity or optimization work across multiple resources - see the "
                "Optimization Ceiling recommendation's next-bottleneck evidence for what else "
                "would need improvement - before targeting this user level."
            ),
            actions=[
                "Review the Optimization Ceiling recommendation to identify what resource becomes the constraint after the current bottleneck is fixed",
                "Consider horizontal scaling in addition to per-resource optimization",
                "Re-validate with real load tests as traffic approaches this level",
            ],
            expected_impact="Prevents planning for load levels that a single-resource optimization cannot realistically reach.",
            confidence="Medium",
        )
    ]


# --- L. Scaling-strategy recommendation (vertical vs. horizontal) ---

def _scaling_strategy_recommendation(
    id_gen: _IdGenerator,
    results: Dict[str, Any],
    signals: Dict[str, Any],
    health_summary: Dict[str, Any],
) -> List[Dict[str, Any]]:
    scaling = health_summary.get("recommended_scaling")
    if scaling in (None, "none"):
        return []

    resource_pressure = results.get("resource_pressure", {})
    scalability_status = results.get("scalability_status")

    if scaling == "horizontal":
        reasons = []
        if signals.get("queue_pressure"):
            reasons.append("queue pressure is high")
        if scalability_status in ("Limited", "Poor"):
            reasons.append(f"scalability status is '{scalability_status}'")
        reason_text = " and ".join(reasons) if reasons else "current signals favor distributing load"

        rec_text = (
            "Horizontal scaling is recommended because the workload is approaching service "
            "capacity and additional application instances can distribute concurrent requests."
        )
        priority = Priority.HIGH if signals.get("queue_pressure") else Priority.MEDIUM
        actions = [
            "Add additional application instances behind the load balancer",
            "Ensure the application is stateless (or externalize session state) to support horizontal scaling",
            "Configure autoscaling based on queue utilization and CPU/memory pressure",
        ]
    else:  # vertical
        # capacity.py can trigger vertical scaling for a reason threshold-based
        # resource_pressure alone can't express: a demand-based bottleneck
        # (bottleneck.py) confirming disk or network as the actual limiting
        # resource. Check that first, since it's more specific evidence than
        # "cpu_pressure is High" - falling back to cpu/memory only when no
        # bottleneck data is driving the vertical-scaling signal.
        bottleneck_resource = results.get("bottleneck_resource")
        bottleneck_severity = results.get("bottleneck_severity")
        bottleneck_drove_decision = (
            bottleneck_resource in ("disk", "network")
            and bottleneck_severity in ("Near ceiling", "At or beyond theoretical ceiling")
        )

        if bottleneck_drove_decision:
            dominant = bottleneck_resource
        elif resource_pressure.get("cpu") in ("High", "Critical"):
            dominant = "cpu"
        else:
            dominant = "memory"

        dominant_label = "CPU" if dominant == "cpu" else dominant.capitalize()
        reason_text = f"{dominant_label} is the primary resource constraint and queue pressure is not dominant"
        rec_text = (
            f"Vertical scaling is recommended because {dominant_label} utilization is the primary "
            f"resource constraint. Increase {dominant_label} resources before increasing workload."
        )
        priority = Priority.HIGH
        actions = [
            f"Increase {dominant_label} allocation/capacity for the current instance(s)",
            f"Re-evaluate {dominant_label} pressure after the increase before adding further load",
        ]

    return [
        _rec(
            id_gen,
            "SCALING",
            priority,
            "Scaling Strategy",
            title=f"Recommended scaling strategy: {scaling}",
            problem=f"Current resource and queue signals indicate {reason_text}.",
            evidence={
                "recommended_scaling": scaling,
                "resource_pressure": resource_pressure,
                "scalability_status": scalability_status,
                "queue_pressure": signals.get("queue_pressure"),
                "cpu_pressure": signals.get("cpu_pressure"),
                "memory_pressure": signals.get("memory_pressure"),
                "bottleneck_resource": results.get("bottleneck_resource"),
                "bottleneck_severity": results.get("bottleneck_severity"),
            },
            recommendation=rec_text,
            actions=actions,
            expected_impact="Directs infrastructure investment toward the scaling approach most likely to relieve the current bottleneck.",
            confidence="Medium",
        )
    ]


# ---------------------------------------------------------------------------
# Merge/dedupe: combined "High Load Capacity Risk" when capacity, queue,
# and scalability signals independently point at the same underlying risk
# (spec section 20 — avoid duplicate recommendations).
# ---------------------------------------------------------------------------

def _merged_high_load_risk(
    id_gen: _IdGenerator,
    results: Dict[str, Any],
    scalability_results: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    classification = results.get("capacity_classification")
    queue_pressure = _get(results, "resource_pressure", "queue")
    scalability_status = results.get("scalability_status")
    first_overloaded = _get(scalability_results, "summary", "first_overloaded_point")
    current_users = results.get("current_users")

    signals_present = 0
    if classification in ("Near Capacity", "At Capacity", "Overloaded", "Critical"):
        signals_present += 1
    if queue_pressure in ("High", "Critical"):
        signals_present += 1
    if scalability_status in ("Limited", "Poor") or (
        first_overloaded is not None and current_users is not None and first_overloaded <= current_users * 1.2
    ):
        signals_present += 1

    if signals_present < 2:
        return None

    priority = Priority.CRITICAL if classification == "Critical" else Priority.HIGH

    return _rec(
        id_gen,
        "RISK",
        priority,
        "Capacity",
        title="High load capacity risk",
        problem=(
            "Multiple independent models agree the system is approaching or exceeding "
            "sustainable capacity: capacity planning, queueing, and scalability prediction "
            "converge on the same underlying risk."
        ),
        evidence={
            "capacity_classification": classification,
            "queue_utilization": _round(results.get("queue_utilization")),
            "safe_users": _round(results.get("safe_users")),
            "current_users": _round(current_users),
            "scalability_status": scalability_status,
            "first_overloaded_point": _round(first_overloaded) if first_overloaded is not None else None,
        },
        recommendation=(
            "Current workload is close to or beyond the safe operating range across multiple "
            "independent models (capacity, queueing, and scalability). Treat this as a single "
            "combined risk rather than separate issues."
        ),
        actions=[
            "Increase processing capacity (horizontal scaling is the primary lever here)",
            "Optimize request-processing/service time to relieve queue pressure",
            "Avoid further load increases until capacity or optimization work lands",
        ],
        expected_impact="Addressing this single combined risk resolves the capacity, queue, and scalability warnings simultaneously.",
        confidence="High",
    )


# --- Ranking, summary, executive narrative ---

def _rank_recommendations(recs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        recs,
        key=lambda r: (
            _PRIORITY_RANK.get(r["priority"], len(_PRIORITY_ORDER)),
            _CATEGORY_RANK.get(r["category"], _DEFAULT_CATEGORY_RANK),
        ),
    )


def _counts_by_priority(recs: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {p: 0 for p in _PRIORITY_ORDER}
    for r in recs:
        counts[r["priority"]] = counts.get(r["priority"], 0) + 1
    return counts


def _overall_priority(recs: List[Dict[str, Any]]) -> str:
    if not recs:
        return Priority.INFO
    return min(recs, key=lambda r: _PRIORITY_RANK.get(r["priority"], len(_PRIORITY_ORDER)))["priority"]


def _primary_risk(results: Dict[str, Any], health_summary: Dict[str, Any]) -> str:
    return health_summary.get("main_risk") or "None"


def _build_executive_summary(
    recs: List[Dict[str, Any]],
    results: Dict[str, Any],
    health_summary: Dict[str, Any],
    scalability_summary: Dict[str, Any],
) -> str:
    classification = results.get("capacity_classification", "Unknown")
    scaling = health_summary.get("recommended_scaling", "none")
    critical = [r for r in recs if r["priority"] == Priority.CRITICAL]
    high = [r for r in recs if r["priority"] == Priority.HIGH]

    sentences = []

    if classification == "Healthy" and not critical and not high:
        sentences.append(
            "The application is currently operating within its calculated safe capacity, "
            "with no critical or high-priority issues identified."
        )
    else:
        sentences.append(
            f"The application's capacity classification is currently '{classification}'."
        )

    if critical:
        titles = "; ".join(r["title"] for r in critical[:3])
        sentences.append(f"Critical issues requiring immediate attention: {titles}.")
    elif high:
        titles = "; ".join(r["title"] for r in high[:3])
        sentences.append(f"High-priority issues to address soon: {titles}.")

    if scaling != "none":
        sentences.append(f"{scaling.capitalize()} scaling is recommended as the primary infrastructure lever.")

    first_overloaded = scalability_summary.get("first_overloaded_point")
    first_collapsed = scalability_summary.get("first_collapsed_point")
    if first_overloaded is not None:
        sentences.append(
            f"The scalability model projects overload beginning around {_round(first_overloaded)} "
            f"concurrent users"
            + (f" and a critical degradation region near {_round(first_collapsed)} users." if first_collapsed else ".")
        )

    first_bound_exceeded = scalability_summary.get("first_bound_exceeded_point")
    if first_bound_exceeded is not None:
        sentences.append(
            f"Independently, predicted throughput exceeds the physically-derived resource "
            f"ceiling beginning around {_round(first_bound_exceeded)} concurrent users - two "
            f"separate models now agree this level is not achievable as currently provisioned."
        )

    bottleneck_resource = results.get("bottleneck_resource")
    bottleneck_severity = results.get("bottleneck_severity")
    if bottleneck_resource and bottleneck_severity in ("Near ceiling", "At or beyond theoretical ceiling"):
        label = "CPU" if bottleneck_resource == "cpu" else str(bottleneck_resource).capitalize()
        sentences.append(f"{label} is the primary bottleneck among monitored resources ({bottleneck_severity}).")

    if not critical and not high:
        sentences.append("Further load increases can proceed with routine monitoring.")
    else:
        sentences.append("Further load increases should be avoided until the identified issues are addressed.")

    return " ".join(sentences)


# --- Public entry point ---

def generate_recommendations(
    capacity_results: Dict[str, Any],
    scalability_results: Optional[Dict[str, Any]] = None,
    usl_results: Optional[Dict[str, Any]] = None,
    little_law_results: Optional[Dict[str, Any]] = None,
    queueing_results: Optional[Dict[str, Any]] = None,
    amdahl_results: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Generate the final, structured set of recommendations.

    Args:
        capacity_results: full output of capacity.analyze_capacity(). Required.
            If capacity.py was given bottleneck data (see capacity.py's optional
            "bottleneck" input), its results carry bottleneck_resource/
            bottleneck_severity/asymptotic_throughput_bound/etc. — this module
            reads those directly from capacity_results, no separate bottleneck
            argument is needed here.
        scalability_results: full output of scalability.predict_scalability().
            Optional — if omitted, scalability-prediction recommendations are skipped.
            If scalability.py was given bottleneck_results, its summary carries
            first_bound_exceeded_point, which is read here too. If it was also
            given amdahl_results, first_amdahl_ceiling_exceeded_point is read too.
        amdahl_results: full output of amdahl.analyze_amdahl(). Optional — enables
            the "Optimization Ceiling" category (how much fixing the bottleneck
            would actually help, and what becomes the next constraint).
        usl_results: full output of usl.UniversalScalabilityModel.analyze() /
            run_usl_analysis(). Optional — enables sigma/kappa/reliability recommendations.
        little_law_results: full output of little_law.analyze_workload(). Optional —
            enables occupancy/consistency-detail recommendations (capacity_results
            already carries a basic little_law_consistency check even without this).
        queueing_results: full output of queueing.analyze_queue(). Optional — currently
            supplementary; queue evidence is primarily read from capacity_results.

    Returns:
        A JSON-serializable dict with keys: summary, recommendations, capacity,
        scalability, resource_analysis.

    Raises:
        RecommendationValidationError: if capacity_results is missing required
            sections ('results', 'signals', 'health_summary').
    """
    if not isinstance(capacity_results, dict):
        raise RecommendationValidationError("capacity_results must be a dictionary.")

    results = capacity_results.get("results")
    signals = capacity_results.get("signals")
    health_summary = capacity_results.get("health_summary")

    if not isinstance(results, dict):
        raise RecommendationValidationError("capacity_results is missing a 'results' section.")
    if not isinstance(signals, dict):
        raise RecommendationValidationError("capacity_results is missing a 'signals' section.")
    if not isinstance(health_summary, dict):
        raise RecommendationValidationError("capacity_results is missing a 'health_summary' section.")

    if scalability_results is not None and not isinstance(scalability_results, dict):
        raise RecommendationValidationError("scalability_results must be a dictionary if provided.")

    id_gen = _IdGenerator()
    scalability_summary = _get(scalability_results, "summary") or {}

    recs: List[Dict[str, Any]] = []

    # --- Merged high-load risk (capacity + queue + scalability convergence) ---
    merged = _merged_high_load_risk(id_gen, results, scalability_results)
    merged_covers_queue = merged is not None and _get(results, "resource_pressure", "queue") in ("High", "Critical")
    merged_covers_capacity = merged is not None

    if merged is not None:
        recs.append(merged)

    # --- A/Zero-capacity special case (always evaluated independently — CRITICAL) ---
    recs.extend(_zero_capacity_special_case(id_gen, results))

    # --- A. Capacity (skip if already folded into the merged risk rec) ---
    if not merged_covers_capacity:
        recs.extend(_capacity_recommendations(id_gen, results))

    # --- Bottleneck Analysis (Utilization Law / bottleneck.py, if data was supplied) ---
    # When this identifies a specific resource as the bottleneck with real
    # severity, it's strictly more informative than the corresponding raw
    # CPU/Disk/Network threshold check below - suppress that resource's
    # threshold-based recommendation so the two don't duplicate each other
    # (spec section 20). Resources bottleneck.py doesn't cover (memory) are
    # never suppressed, since there's nothing more informative to defer to.
    recs.extend(_bottleneck_recommendations(id_gen, results))
    bottleneck_severity = results.get("bottleneck_severity")
    bottleneck_confirmed_resource = (
        results.get("bottleneck_resource")
        if bottleneck_severity in ("Near ceiling", "At or beyond theoretical ceiling")
        else None
    )

    # --- J2b. Optimization Ceiling (Amdahl's Law, if amdahl_results was supplied) ---
    recs.extend(_amdahl_recommendations(id_gen, amdahl_results))

    # --- B/C. CPU / Memory ---
    if bottleneck_confirmed_resource != "cpu":
        recs.extend(_cpu_recommendations(id_gen, results))
    recs.extend(_memory_recommendations(id_gen, results))  # memory isn't covered by bottleneck.py - never suppressed

    # --- D. Queueing (skip if already folded into the merged risk rec) ---
    if not merged_covers_queue:
        recs.extend(_queue_recommendations(id_gen, results))

    # --- E. USL ---
    recs.extend(_usl_recommendations(id_gen, usl_results, results))

    # --- F. Little's Law ---
    recs.extend(_little_law_recommendations(id_gen, little_law_results, results))

    # --- G. Response time ---
    recs.extend(_response_time_recommendations(id_gen, results))

    # --- H. Error rate ---
    recs.extend(_error_rate_recommendations(id_gen, results))

    # --- I. Disk / Network (suppressed per-resource if bottleneck analysis already covered it) ---
    if bottleneck_confirmed_resource != "disk":
        recs.extend(_disk_io_recommendations(id_gen, results))
    if bottleneck_confirmed_resource != "network":
        recs.extend(_network_io_recommendations(id_gen, results))

    # --- J. Database (evidence-gated, "investigate" language only) ---
    recs.extend(_database_recommendations(id_gen, results, usl_results))

    # --- K. Scalability prediction ---
    recs.extend(_scalability_prediction_recommendations(id_gen, scalability_results))
    recs.extend(_asymptotic_bound_recommendations(id_gen, scalability_results))
    recs.extend(_amdahl_ceiling_scalability_recommendations(id_gen, scalability_results))

    # --- L. Scaling strategy (vertical vs horizontal) ---
    recs.extend(_scaling_strategy_recommendation(id_gen, results, signals, health_summary))

    recs = _rank_recommendations(recs)
    counts = _counts_by_priority(recs)
    overall_priority = _overall_priority(recs)
    primary_risk = _primary_risk(results, health_summary)
    executive_summary = _build_executive_summary(recs, results, health_summary, scalability_summary)

    summary = {
        "overall_priority": overall_priority,
        "primary_risk": primary_risk,
        "recommended_scaling": health_summary.get("recommended_scaling", "none"),
        "executive_summary": executive_summary,
        "recommendation_count": len(recs),
        "critical_count": counts[Priority.CRITICAL],
        "high_count": counts[Priority.HIGH],
        "medium_count": counts[Priority.MEDIUM],
        "low_count": counts[Priority.LOW],
        "info_count": counts[Priority.INFO],
        "safe_users": results.get("safe_users"),
        "current_users": results.get("current_users"),
        "breaking_point": results.get("breaking_point"),
        "first_overloaded_point": scalability_summary.get("first_overloaded_point"),
        "first_collapsed_point": scalability_summary.get("first_collapsed_point"),
        "first_bound_exceeded_point": scalability_summary.get("first_bound_exceeded_point"),
        "bottleneck_resource": results.get("bottleneck_resource"),
        "bottleneck_severity": results.get("bottleneck_severity"),
    }

    capacity_block = {
        "current_users": results.get("current_users"),
        "safe_users": results.get("safe_users"),
        "remaining_capacity": results.get("growth_potential_users"),
        "capacity_margin_percent": results.get("capacity_margin"),
        "breaking_point": results.get("breaking_point"),
        "capacity_classification": results.get("capacity_classification"),
    }

    scalability_block = {
        "first_overloaded_point": scalability_summary.get("first_overloaded_point"),
        "first_collapsed_point": scalability_summary.get("first_collapsed_point"),
        "first_bound_exceeded_point": scalability_summary.get("first_bound_exceeded_point"),
        "recommended_max_users": scalability_summary.get("recommended_max_users"),
        "safe_prediction_limit": scalability_summary.get("safe_prediction_limit"),
    }

    resource_analysis = {
        "cpu": _get(results, "resource_pressure", "cpu"),
        "memory": _get(results, "resource_pressure", "memory"),
        "disk": _get(results, "resource_pressure", "disk"),
        "network": _get(results, "resource_pressure", "network"),
        "queue": _get(results, "resource_pressure", "queue"),
        "bottleneck": {
            "resource": results.get("bottleneck_resource"),
            "severity": results.get("bottleneck_severity"),
            "service_demand": results.get("bottleneck_service_demand"),
            "asymptotic_throughput_bound": results.get("asymptotic_throughput_bound"),
            "optimal_concurrency_n_star": results.get("optimal_concurrency_n_star"),
            "throughput_exceeds_asymptotic_bound": results.get("throughput_exceeds_asymptotic_bound"),
        },
    }

    return {
        "summary": summary,
        "recommendations": recs,
        "capacity": capacity_block,
        "scalability": scalability_block,
        "resource_analysis": resource_analysis,
    }


# Alias matching the run_*_analysis naming convention used by the other
# pipeline modules (usl.run_usl_analysis, etc.).
run_recommendation_engine = generate_recommendations