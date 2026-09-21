import math
from typing import Any, Dict, Optional, Union

Number = Union[int, float]

# --- Custom Exceptions ---

class CapacityPlanningError(Exception):
    """Base exception for capacity planning errors."""

class MissingSectionError(CapacityPlanningError):
    """Raised when required section is missing."""

class MissingFieldError(CapacityPlanningError):
    """Raised when required field is missing."""

class InvalidTypeError(CapacityPlanningError):
    """Raised when field type is invalid."""

class InvalidRangeError(CapacityPlanningError):
    """Raised when value is outside valid range."""

# --- Configurable Constants ---

DEFAULT_SAFETY_MARGIN_RATIO = 0.80

CPU_SAFE_LIMIT = 85.0
MEMORY_SAFE_LIMIT = 85.0

CPU_PRESSURE_THRESHOLDS = {
    "Low": 50.0,
    "Moderate": 70.0,
    "High": 85.0,
}

MEMORY_PRESSURE_THRESHOLDS = {
    "Low": 50.0,
    "Moderate": 70.0,
    "High": 85.0,
}

DISK_IO_THRESHOLDS = {
    "Low": 40.0,
    "Moderate": 60.0,
    "High": 80.0,
}

NETWORK_IO_THRESHOLDS = {
    "Low": 40.0,
    "Moderate": 60.0,
    "High": 80.0,
}

DEFAULT_DISK_IO_LIMIT_MBPS = 100.0
DEFAULT_NETWORK_IO_LIMIT_MBPS = 100.0

QUEUE_UTILIZATION_THRESHOLDS = {
    "Low": 0.5,
    "Moderate": 0.7,
    "High": 0.85,
}

QUEUE_LENGTH_CRITICAL_THRESHOLD = 10.0

CAPACITY_MARGIN_THRESHOLDS = {
    "Healthy": 40.0,
    "Near Capacity": 20.0,
    "At Capacity": 5.0,
}

ERROR_RATE_THRESHOLDS = {
    "Moderate": 1.0,
    "High": 5.0,
}

RESPONSE_TIME_THRESHOLD_MS = 2000.0
DEFAULT_P95_RESPONSE_TIME_THRESHOLD_MS = 1000.0

# --- Required Input Structure ---

REQUIRED_SECTIONS = (
    "usl",
    "little_law",
    "queueing",
    "runtime",
)

# "bottleneck" is optional - if provided, it should be the direct output
# of bottleneck.analyze_bottleneck() / analyze_bottleneck_safe(). When
# present, it upgrades main_risk / scalability_status / capacity
# classification to use the demand-based (Utilization Law) bottleneck
# instead of independent thresholds for cpu/disk/network, and surfaces
# the asymptotic throughput bound as extra evidence. When absent (or an
# "unavailable" result), capacity.py behaves exactly as it did before
# bottleneck.py existed - nothing here is required.
OPTIONAL_SECTIONS = ("bottleneck",)

REQUIRED_FIELDS = {
    "usl": (
        "peak_throughput",
        "optimal_users",
        "saturation_point",
        "scalability_efficiency",
        "prediction_reliability",
        "observed_min_users",
        "observed_max_users",
    ),
    "little_law": (
        "requests_in_system",
        "time_in_system",
    ),
    "queueing": (
        "utilization",
        "queue_length",
        "waiting_time",
        "stability",
    ),
    "runtime": (
        "current_users",
        "throughput",
        "response_time",
        "cpu_usage",
        "memory_usage",
        "disk_io",
        "network_io",
        "error_rate",
    ),
}

NUMERIC_FIELDS = {
    "usl": (
        "peak_throughput",
        "optimal_users",
        "saturation_point",
        "scalability_efficiency",
        "observed_min_users",
        "observed_max_users",
    ),
    "little_law": (
        "requests_in_system",
        "time_in_system",
    ),
    "queueing": (
        "utilization",
        "queue_length",
        "waiting_time",
    ),
    "runtime": (
        "current_users",
        "throughput",
        "response_time",
        "cpu_usage",
        "memory_usage",
        "disk_io",
        "network_io",
        "error_rate",
    ),
}

OPTIONAL_NUMERIC_FIELDS = {
    "runtime": (
        "p95_response_time",
    ),
}

# --- Validation Helpers ---

def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )

def validate_input(data: Dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise CapacityPlanningError("Input to analyze_capacity must be a dictionary.")

    for section in REQUIRED_SECTIONS:
        if section not in data:
            raise MissingSectionError(f"Missing required section: {section}")
        if not isinstance(data[section], dict):
            raise CapacityPlanningError(f"Section {section} must be dictionary.")

    for section in OPTIONAL_SECTIONS:
        if section in data and data[section] is not None and not isinstance(data[section], dict):
            raise CapacityPlanningError(f"Section {section} must be a dictionary if provided.")

    for section, fields in REQUIRED_FIELDS.items():
        for field in fields:
            if field not in data[section]:
                raise MissingFieldError(f"Missing field '{field}' in '{section}'.")

    for section, fields in NUMERIC_FIELDS.items():
        for field in fields:
            value = data[section][field]
            if not _is_number(value):
                raise InvalidTypeError(f"{section}.{field} must be numeric.")

    for section, fields in OPTIONAL_NUMERIC_FIELDS.items():
        for field in fields:
            if field not in data[section]:
                continue
            value = data[section][field]
            if not _is_number(value):
                raise InvalidTypeError(f"{section}.{field} must be numeric if provided.")

    if not (0 <= data["usl"]["scalability_efficiency"] <= 1):
        raise InvalidRangeError("usl.scalability_efficiency must be between 0 and 1.")

    if data["usl"]["peak_throughput"] <= 0:
        raise InvalidRangeError("usl.peak_throughput must be greater than zero.")

    if data["usl"]["optimal_users"] <= 0:
        raise InvalidRangeError("usl.optimal_users must be greater than zero.")

    if data["usl"]["saturation_point"] <= 0:
        raise InvalidRangeError("usl.saturation_point must be greater than zero.")

    # Validation for observed ranges
    if data["usl"]["observed_min_users"] <= 0:
        raise InvalidRangeError("usl.observed_min_users must be greater than zero.")
    
    if data["usl"]["observed_max_users"] <= 0:
        raise InvalidRangeError("usl.observed_max_users must be greater than zero.")
        
    if data["usl"]["observed_min_users"] > data["usl"]["observed_max_users"]:
        raise InvalidRangeError("usl.observed_min_users cannot exceed observed_max_users.")

    if data["little_law"]["requests_in_system"] < 0:
        raise InvalidRangeError("little_law.requests_in_system cannot be negative.")

    if data["little_law"]["time_in_system"] <= 0:
        raise InvalidRangeError("little_law.time_in_system must be greater than zero.")

    if data["queueing"]["utilization"] < 0:
        raise InvalidRangeError("queueing.utilization cannot be negative.")

    if data["queueing"]["queue_length"] < 0:
        raise InvalidRangeError("queue_length cannot be negative.")

    if data["queueing"]["waiting_time"] < 0:
        raise InvalidRangeError("waiting_time cannot be negative.")

    runtime = data["runtime"]
    if not 0 <= runtime["cpu_usage"] <= 100:
        raise InvalidRangeError("cpu_usage must be between 0 and 100.")
    if not 0 <= runtime["memory_usage"] <= 100:
        raise InvalidRangeError("memory_usage must be between 0 and 100.")
    if runtime["disk_io"] < 0:
        raise InvalidRangeError("disk_io cannot be negative.")
    if runtime["network_io"] < 0:
        raise InvalidRangeError("network_io cannot be negative.")
    if not 0 <= runtime["error_rate"] <= 100:
        raise InvalidRangeError("error_rate must be between 0 and 100.")
    if runtime["throughput"] <= 0:
        raise InvalidRangeError("throughput must be greater than zero.")
    if runtime["response_time"] <= 0:
        raise InvalidRangeError("response_time must be greater than zero.")
    if runtime["current_users"] <= 0:
        raise InvalidRangeError("current_users must be greater than zero.")
    if "p95_response_time" in runtime and runtime["p95_response_time"] <= 0:
        raise InvalidRangeError("p95_response_time must be greater than zero if provided.")

# --- Bottleneck Data Handling (optional bottleneck.py integration) ---

_BOTTLENECK_SEVERITY_RANK = {
    "Unknown": 0,
    "Substantial headroom": 0,
    "Moderate headroom": 1,
    "Near ceiling": 2,
    "At or beyond theoretical ceiling": 3,
}


def _extract_bottleneck_data(bottleneck: Any) -> Optional[Dict[str, Any]]:
    """
    Accepts whatever came in under data["bottleneck"] - the direct output
    of bottleneck.analyze_bottleneck() / analyze_bottleneck_safe(), or
    None/absent - and returns it unchanged if it looks like a real
    analysis, or None otherwise. This lets capacity.py degrade
    gracefully (identical to its pre-bottleneck.py behavior) whenever
    this optional data is missing, malformed, or an explicit
    "unavailable" result, rather than erroring on something optional.
    """
    if not isinstance(bottleneck, dict):
        return None
    if bottleneck.get("bottleneck_analysis") == "unavailable":
        return None
    if "bottleneck" not in bottleneck or "bottleneck_severity" not in bottleneck:
        return None
    return bottleneck


def _bottleneck_summary_fields(bottleneck: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Flattened, capacity.py-results-ready view of the bottleneck analysis."""
    if bottleneck is None:
        return {
            "bottleneck_resource": None,
            "bottleneck_service_demand": None,
            "bottleneck_severity": None,
            "asymptotic_throughput_bound": None,
            "optimal_concurrency_n_star": None,
            "throughput_exceeds_asymptotic_bound": False,
        }

    bottleneck_block = bottleneck.get("bottleneck", {}) or {}
    bounds_block = bottleneck.get("asymptotic_bounds", {}) or {}
    comparison_block = bottleneck.get("observed_vs_bound", {}) or {}

    return {
        "bottleneck_resource": bottleneck_block.get("resource"),
        "bottleneck_service_demand": bottleneck_block.get("service_demand"),
        "bottleneck_severity": bottleneck.get("bottleneck_severity"),
        "asymptotic_throughput_bound": bounds_block.get("asymptotic_throughput_bound"),
        "optimal_concurrency_n_star": bounds_block.get("optimal_concurrency_n_star"),
        "throughput_exceeds_asymptotic_bound": bool(comparison_block.get("exceeds_bound", False)),
    }


def _display_resource_name(resource: str) -> str:
    return "CPU" if resource == "cpu" else resource.capitalize()

# --- Capacity Calculations ---

def calculate_arrival_rate_from_little_law(
    little_law: Dict[str, Any]
) -> float:
    """
    Calculate arrival/request rate using Little's Law:

        L = λW
        λ = L / W

    requests_in_system (L): number of requests
    time_in_system (W): seconds
    result: requests per second
    """
    time_in_system = little_law["time_in_system"]

    if time_in_system <= 0:
        return 0.0

    return round(
        little_law["requests_in_system"] / time_in_system,
        2
    )
def calculate_request_pressure(arrival_rate: float, peak_throughput: float) -> float:
    if peak_throughput <= 0:
        return 0.0
    return round(arrival_rate / peak_throughput, 2)

def calculate_little_law_consistency(
    arrival_rate: float,
    observed_throughput: float
) -> Dict[str, Any]:
    """
    Compare Little's Law derived arrival rate with observed throughput.
    """

    if observed_throughput <= 0:
        return {
            "difference_percent": 0.0,
            "consistent": False
        }

    difference_percent = abs(
        arrival_rate - observed_throughput
    ) / observed_throughput * 100

    return {
        "difference_percent": round(difference_percent, 2),
        "consistent": difference_percent <= 20.0
    }

def calculate_safe_capacity(
    breaking_point: float,
    safety_margin_ratio: float
) -> float:
    """
    Safe operating capacity: a safety-margined fraction of the breaking
    point, not an independently-computed fraction of capacity_reference.

    This is a deliberate fix, not the original design: safe_users used
    to be calculated straight from capacity_reference * margin, entirely
    independently of calculate_breaking_point()'s own min() of queue/CPU/
    memory limits. Nothing then prevented breaking_point from coming out
    LOWER than safe_users - e.g. capacity_reference=500 gives
    safe_users=400, but if CPU is already running hot relative to
    current_users (say 90% at only 100 users), breaking_point's CPU-limit
    term could come out around 94 - producing a report that claims the
    system is "safe" up to 400 users while also claiming it "breaks" at
    94. Deriving safe_users from breaking_point instead makes
    safe_users <= breaking_point true by construction, for any input,
    rather than true only when the CPU/memory/queue limits happen to be
    generous - which is what "one consistent classification policy"
    actually requires.

    breaking_point already folds in queue stability (see
    calculate_breaking_point) - there's deliberately no separate
    stability check here anymore, since checking it again with different
    logic (previously: exact-match "unstable" here vs. "not stable" in
    calculate_breaking_point) was the second way these two numbers could
    end up contradicting each other.
    """
    return round(max(breaking_point, 0.0) * safety_margin_ratio, 2)

def calculate_remaining_capacity(safe_users: float, current_users: Number) -> float:
    return round(max(safe_users - current_users, 0.0), 2)

def calculate_throughput_capacity_used_percent(throughput: Number, peak_throughput: Number) -> float:
    """Calculates what percentage of the modeled maximum throughput is being utilized."""
    return round((throughput / peak_throughput) * 100, 2)

def calculate_user_capacity_used_percent(current_users: Number, optimal_users: Number) -> float:
    """Calculates what percentage of the USL theoretical optimal user capacity is active."""
    if optimal_users <= 0:
        return 0.0
    return round((current_users / optimal_users) * 100, 2)

def calculate_capacity_margin(remaining_capacity: float, safe_users: float) -> float:
    """Margin relative to safe capacity."""
    if safe_users <= 0:
        return 0.0
    return round((remaining_capacity / safe_users) * 100, 2)

def calculate_breaking_point(
    capacity_reference: float,
    queueing: Dict[str, Any],
    runtime: Dict[str, Any]
) -> float:
    """
    Estimate the maximum sustainable concurrent users.

    The breaking point is bounded by:
        1. Mathematical capacity reference
        2. Queue stability
        3. CPU safe limit
        4. Memory safe limit

    CPU and memory limits use linear extrapolation from the
    current observed workload.
    """
    current_users = runtime["current_users"]

    if current_users <= 0:
        return 0.0

    # If the queue is already unstable, do not extrapolate
    # beyond the currently observed workload.
    if str(queueing["stability"]).lower() != "stable":
        queue_limit = current_users
    else:
        queue_limit = capacity_reference

    # CPU-based limit
    cpu_usage = runtime["cpu_usage"]

    if cpu_usage > 0:
        cpu_limit = (
            current_users * CPU_SAFE_LIMIT
        ) / cpu_usage
    else:
        cpu_limit = capacity_reference

    cpu_limit = min(cpu_limit, capacity_reference)

    # Memory-based limit
    memory_usage = runtime["memory_usage"]

    if memory_usage > 0:
        memory_limit = (
            current_users * MEMORY_SAFE_LIMIT
        ) / memory_usage
    else:
        memory_limit = capacity_reference

    memory_limit = min(memory_limit, capacity_reference)

    return round(
        max(
            0.0,
            min(
                capacity_reference,
                queue_limit,
                cpu_limit,
                memory_limit
            )
        ),
        2
    )

# --- Resource Pressure Analysis ---

def _classify_by_thresholds(value: float, thresholds: Dict[str, float]) -> str:
    if value < thresholds["Low"]: return "Low"
    if value < thresholds["Moderate"]: return "Moderate"
    if value < thresholds["High"]: return "High"
    return "Critical"

def _normalize_to_percent(raw_value: float, limit: float) -> float:
    if limit <= 0:
        return 0.0
    return (raw_value / limit) * 100

def analyze_resource_pressure(
    runtime: Dict[str, Any],
    queueing: Dict[str, Any],
    disk_io_limit_mbps: float = DEFAULT_DISK_IO_LIMIT_MBPS,
    network_io_limit_mbps: float = DEFAULT_NETWORK_IO_LIMIT_MBPS,
) -> Dict[str, str]:
    cpu_pressure = _classify_by_thresholds(runtime["cpu_usage"], CPU_PRESSURE_THRESHOLDS)
    memory_pressure = _classify_by_thresholds(runtime["memory_usage"], MEMORY_PRESSURE_THRESHOLDS)
    disk_utilization_percent = _normalize_to_percent(runtime["disk_io"], disk_io_limit_mbps)
    network_utilization_percent = _normalize_to_percent(runtime["network_io"], network_io_limit_mbps)
    disk_pressure = _classify_by_thresholds(disk_utilization_percent, DISK_IO_THRESHOLDS)
    network_pressure = _classify_by_thresholds(network_utilization_percent, NETWORK_IO_THRESHOLDS)
    queue_pressure = _classify_by_thresholds(queueing["utilization"], QUEUE_UTILIZATION_THRESHOLDS)

    if queueing["queue_length"] >= QUEUE_LENGTH_CRITICAL_THRESHOLD:
        queue_pressure = "Critical"

    return {
        "cpu": cpu_pressure,
        "memory": memory_pressure,
        "disk": disk_pressure,
        "network": network_pressure,
        "queue": queue_pressure,
    }

# --- Status Classification ---

_PRESSURE_RANK = {
    "Low": 0, "Normal": 0, "Moderate": 1, "High": 2, "Critical": 3,
}

def determine_scalability_status(
    usl: Dict[str, Any],
    queueing: Dict[str, Any],
    throughput_capacity_used_percent: float,
    resource_pressure: Dict[str, str],
    runtime: Dict[str, Any],
    p95_response_time_threshold_ms: float = DEFAULT_P95_RESPONSE_TIME_THRESHOLD_MS,
    bottleneck: Optional[Dict[str, Any]] = None,
) -> str:
    penalty = 0

    if usl["scalability_efficiency"] < 0.5:
        penalty += 2

    if throughput_capacity_used_percent >= 90:
        penalty += 2
    elif throughput_capacity_used_percent >= 75:
        penalty += 1

    utilization = queueing["utilization"]
    if utilization >= 0.85:
        penalty += 2
    elif utilization >= 0.7:
        penalty += 1

    if str(queueing["stability"]).lower() != "stable":
        penalty += 2

    worst_pressure = max(resource_pressure.values(), key=lambda x: _PRESSURE_RANK[x])
    penalty += _PRESSURE_RANK[worst_pressure]

    if runtime["error_rate"] >= ERROR_RATE_THRESHOLDS["High"]:
        penalty += 2
    elif runtime["error_rate"] >= ERROR_RATE_THRESHOLDS["Moderate"]:
        penalty += 1

    if runtime["response_time"] > RESPONSE_TIME_THRESHOLD_MS:
        penalty += 2

    p95_response_time = runtime.get("p95_response_time")
    if p95_response_time is not None and p95_response_time > p95_response_time_threshold_ms:
        penalty += 2

    # Demand-based bottleneck ceiling (bottleneck.py) - independent of the
    # threshold-based worst_pressure check above, so a system can be
    # penalized here even if no individual resource threshold looks bad.
    if bottleneck is not None:
        severity = bottleneck.get("bottleneck_severity")
        if severity == "At or beyond theoretical ceiling":
            penalty += 2
        elif severity == "Near ceiling":
            penalty += 1

    if penalty <= 2: return "Excellent"
    if penalty <= 5: return "Good"
    if penalty <= 8: return "Moderate"
    if penalty <= 11: return "Limited"
    return "Poor"

def classify_capacity(
    capacity_margin: float,
    throughput_capacity_used_percent: float,
    queueing: Dict[str, Any],
    resource_pressure: Dict[str, str],
    runtime: Dict[str, Any],
    p95_response_time_threshold_ms: float = DEFAULT_P95_RESPONSE_TIME_THRESHOLD_MS,
    bottleneck: Optional[Dict[str, Any]] = None,
) -> str:
    if capacity_margin >= CAPACITY_MARGIN_THRESHOLDS["Healthy"]:
        status = "Healthy"
    elif capacity_margin >= CAPACITY_MARGIN_THRESHOLDS["Near Capacity"]:
        status = "Near Capacity"
    elif capacity_margin >= CAPACITY_MARGIN_THRESHOLDS["At Capacity"]:
        status = "At Capacity"
    else:
        status = "Overloaded"

    p95_response_time = runtime.get("p95_response_time")
    p95_breached = (
        p95_response_time is not None
        and p95_response_time > p95_response_time_threshold_ms
    )

    bottleneck_ceiling_exceeded = bool(
        bottleneck and bottleneck.get("signals", {}).get("exceeds_theoretical_ceiling")
    )

    escalate = (
        str(queueing["stability"]).lower() != "stable"
        or "Critical" in resource_pressure.values()
        or throughput_capacity_used_percent > 100
        or runtime["error_rate"] >= ERROR_RATE_THRESHOLDS["High"]
        or runtime["response_time"] > RESPONSE_TIME_THRESHOLD_MS
        or p95_breached
        or bottleneck_ceiling_exceeded
    )

    order = ["Healthy", "Near Capacity", "At Capacity", "Overloaded", "Critical"]

    if escalate:
        index = order.index(status)
        return order[min(index + 1, len(order)-1)]
    return status

# --- Resource Scaling Estimates & Health Summary ---

def estimate_resource_scaling_at_breaking_point(
    runtime: Dict[str, Any],
    breaking_point: float
) -> Dict[str, float]:
    """
    Estimate required resource capacity multiplier.
    NOTE: This is a rough linear extrapolation of resource headroom
    requirement under a linear usage assumption, not an actual
    infrastructure scaling prediction or a USL-derived limit.
    """
    current_users = runtime["current_users"]
    if current_users <= 0:
        return {"cpu": 1.0, "memory": 1.0}

    growth_ratio = breaking_point / current_users

    def calculate_multiplier(usage: float, safe_limit: float) -> float:
        if usage <= 0:
            return 1.0
        multiplier = (usage * growth_ratio) / safe_limit
        return round(max(1.0, min(multiplier, 5.0)), 2)

    return {
        "cpu": calculate_multiplier(runtime["cpu_usage"], CPU_SAFE_LIMIT),
        "memory": calculate_multiplier(runtime["memory_usage"], MEMORY_SAFE_LIMIT)
    }

def determine_main_risk(
    resource_pressure: Dict[str, str],
    bottleneck: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Identify the single resource most likely to limit performance first.

    memory and queue aren't covered by the Utilization Law / demand
    model bottleneck.py computes - memory is a capacity ceiling rather
    than a "service center" a request queues for, and queue behavior is
    downstream of resource contention rather than a resource itself -
    so they're always ranked by their own pressure threshold here.

    cpu/disk/network: when bottleneck data is available, the
    demand-based bottleneck (D_max = max(service demand)) replaces the
    old independent threshold comparison for this trio, since D_max is
    a rigorous answer to "which resource limits throughput first" where
    comparing three separate raw thresholds against each other is not.
    Falls back to the threshold comparison for all five resources when
    bottleneck data isn't supplied, matching the original behavior
    exactly.
    """
    candidates: Dict[str, int] = {
        "memory": _PRESSURE_RANK[resource_pressure["memory"]],
        "queue": _PRESSURE_RANK[resource_pressure["queue"]],
    }

    if bottleneck is not None:
        bottleneck_resource = bottleneck.get("bottleneck", {}).get("resource")
        severity = bottleneck.get("bottleneck_severity")
        if bottleneck_resource:
            candidates[bottleneck_resource] = _BOTTLENECK_SEVERITY_RANK.get(severity, 0)
    else:
        candidates["cpu"] = _PRESSURE_RANK[resource_pressure["cpu"]]
        candidates["disk"] = _PRESSURE_RANK[resource_pressure["disk"]]
        candidates["network"] = _PRESSURE_RANK[resource_pressure["network"]]

    if not candidates or max(candidates.values()) <= 0:
        return "None"

    resource = max(candidates, key=candidates.get)
    return _display_resource_name(resource)

# --- Recommendation Engine Signals ---

def generate_signals(
    capacity_margin: float,
    throughput_capacity_used_percent: float,
    resource_pressure: Dict[str, str],
    scalability_status: str,
    bottleneck: Optional[Dict[str, Any]] = None,
) -> Dict[str, bool]:
    throughput_capacity_exceeded = (throughput_capacity_used_percent > 100)

    safe_capacity_exceeded = (capacity_margin <= 0)

    capacity_exceeded = safe_capacity_exceeded
    capacity_nearly_full = (not capacity_exceeded and capacity_margin <= 20)
    capacity_low = (not capacity_exceeded and not capacity_nearly_full and capacity_margin <= 40)
    capacity_available = not (capacity_exceeded or capacity_nearly_full or capacity_low)

    cpu_pressure = resource_pressure["cpu"] in ("High", "Critical")
    memory_pressure = resource_pressure["memory"] in ("High", "Critical")
    queue_pressure = resource_pressure["queue"] in ("High", "Critical")
    disk_pressure = resource_pressure["disk"] in ("High", "Critical")
    network_pressure = resource_pressure["network"] in ("High", "Critical")

    resource_bottleneck = (cpu_pressure or memory_pressure or queue_pressure or disk_pressure or network_pressure)
    horizontal_scaling_candidate = (queue_pressure or scalability_status in ("Limited", "Poor"))

    # Demand-based bottleneck severity (bottleneck.py). Previously only
    # cpu_pressure/memory_pressure could trigger vertical_scaling_candidate
    # - disk/network pressure never did, even at "Critical", which was a
    # gap. Now, when the demand-based bottleneck confirms disk or network
    # as the actual limiting resource (not just independently over its own
    # threshold), it triggers vertical scaling the same way cpu/memory do.
    bottleneck_resource = bottleneck.get("bottleneck", {}).get("resource") if bottleneck else None
    bottleneck_severity = bottleneck.get("bottleneck_severity") if bottleneck else None
    bottleneck_severe = bottleneck_severity in ("Near ceiling", "At or beyond theoretical ceiling")

    vertical_scaling_candidate = (
        (cpu_pressure or memory_pressure or (bottleneck_severe and bottleneck_resource in ("cpu", "disk", "network")))
        and not queue_pressure
    )

    return {
        "capacity_available": capacity_available,
        "capacity_low": capacity_low,
        "capacity_nearly_full": capacity_nearly_full,
        "capacity_exceeded": capacity_exceeded,
        "throughput_capacity_exceeded": throughput_capacity_exceeded,
        "safe_capacity_exceeded": safe_capacity_exceeded,
        "cpu_pressure": cpu_pressure,
        "memory_pressure": memory_pressure,
        "queue_pressure": queue_pressure,
        "disk_pressure": disk_pressure,
        "network_pressure": network_pressure,
        "resource_bottleneck": resource_bottleneck,
        "horizontal_scaling_candidate": horizontal_scaling_candidate,
        "vertical_scaling_candidate": vertical_scaling_candidate,
        "bottleneck_data_available": bottleneck is not None,
        "near_theoretical_ceiling": bool(bottleneck and bottleneck.get("signals", {}).get("near_theoretical_ceiling")),
        "exceeds_theoretical_ceiling": bool(bottleneck and bottleneck.get("signals", {}).get("exceeds_theoretical_ceiling")),
    }

# --- Public API ---

def analyze_capacity(
    data: Dict[str, Any],
    safety_margin_ratio: float = DEFAULT_SAFETY_MARGIN_RATIO,
    disk_io_limit_mbps: float = DEFAULT_DISK_IO_LIMIT_MBPS,
    network_io_limit_mbps: float = DEFAULT_NETWORK_IO_LIMIT_MBPS,
    p95_response_time_threshold_ms: float = DEFAULT_P95_RESPONSE_TIME_THRESHOLD_MS,
) -> Dict[str, Any]:
    """
    data["bottleneck"] is optional. If provided, pass in the direct
    output of bottleneck.analyze_bottleneck() / analyze_bottleneck_safe()
    - see OPTIONAL_SECTIONS above for what changes when it's present.
    """

    validate_input(data)

    if not (0 < safety_margin_ratio <= 1):
        raise InvalidRangeError("Safety margin must be between 0 and 1.")
    if disk_io_limit_mbps <= 0:
        raise InvalidRangeError("disk_io_limit_mbps must be greater than zero.")
    if network_io_limit_mbps <= 0:
        raise InvalidRangeError("network_io_limit_mbps must be greater than zero.")
    if p95_response_time_threshold_ms <= 0:
        raise InvalidRangeError("p95_response_time_threshold_ms must be greater than zero.")

    usl = data["usl"]
    little_law = data["little_law"]
    queueing = data["queueing"]
    runtime = data["runtime"]
    bottleneck_data = _extract_bottleneck_data(data.get("bottleneck"))

    # ---------------------------------------------------
    # Extrapolation Safeguards & Reference Injection
    # ---------------------------------------------------
    usl_optimal_users = usl["optimal_users"]
    observed_max_users = usl["observed_max_users"]

    prediction_reliability = usl.get("prediction_reliability", {})
    usable_for_extrapolation = prediction_reliability.get(
        "usable_for_extrapolation",
        True
    )
    prediction_confidence = prediction_reliability.get(
        "status",
        "unknown"
    )

    if usable_for_extrapolation:
        capacity_reference = usl_optimal_users
        capacity_reference_reason = "USL theoretical optimal capacity"
    else:
        capacity_reference = observed_max_users
        capacity_reference_reason = (
            "Observed maximum capacity because USL extrapolation "
            "is not reliable"
        )

    # ---------------------------------------------------
    # Mathematical calculations
    # ---------------------------------------------------
    arrival_rate = calculate_arrival_rate_from_little_law(little_law)

    little_law_consistency = calculate_little_law_consistency(
        arrival_rate,
        runtime["throughput"]
    )

    request_pressure = calculate_request_pressure(
        arrival_rate,
        usl["peak_throughput"]
    )
    
    # Safe users & Breaking Point now depend on the extrapolation-aware reference.
    # breaking_point is computed FIRST - safe_users is now derived from it
    # (see calculate_safe_capacity's docstring for why the order matters).
    breaking_point = calculate_breaking_point(capacity_reference, queueing, runtime)
    safe_users = calculate_safe_capacity(breaking_point, safety_margin_ratio)
    remaining_capacity = calculate_remaining_capacity(safe_users, runtime["current_users"])
    
    throughput_capacity_used = calculate_throughput_capacity_used_percent(
        runtime["throughput"], usl["peak_throughput"]
    )
    usl_user_capacity_used = calculate_user_capacity_used_percent(
        runtime["current_users"],
        usl_optimal_users
    )
    user_capacity_used = calculate_user_capacity_used_percent(
        runtime["current_users"],
        capacity_reference
    )
    
    capacity_margin = calculate_capacity_margin(remaining_capacity, safe_users)

    # ---------------------------------------------------
    # Analysis
    # ---------------------------------------------------
    resource_pressure = analyze_resource_pressure(
        runtime, queueing,
        disk_io_limit_mbps=disk_io_limit_mbps,
        network_io_limit_mbps=network_io_limit_mbps,
    )

    scalability_status = determine_scalability_status(
        usl, queueing, throughput_capacity_used,
        resource_pressure, runtime,
        p95_response_time_threshold_ms=p95_response_time_threshold_ms,
        bottleneck=bottleneck_data,
    )

    capacity_classification = classify_capacity(
        capacity_margin, throughput_capacity_used,
        queueing, resource_pressure, runtime,
        p95_response_time_threshold_ms=p95_response_time_threshold_ms,
        bottleneck=bottleneck_data,
    )

    scaling = estimate_resource_scaling_at_breaking_point(runtime, breaking_point)

    signals = generate_signals(
        capacity_margin, throughput_capacity_used,
        resource_pressure, scalability_status,
        bottleneck=bottleneck_data,
    )

    # ---------------------------------------------------
    # Health Summary
    # ---------------------------------------------------
    if signals["horizontal_scaling_candidate"]:
        scaling_recommendation = "horizontal"
    elif signals["vertical_scaling_candidate"]:
        scaling_recommendation = "vertical"
    else:
        scaling_recommendation = "none"

    health_summary = {
        "overall_status": capacity_classification,
        "main_risk": determine_main_risk(resource_pressure, bottleneck=bottleneck_data),
        "recommended_scaling": scaling_recommendation
    }

    bottleneck_fields = _bottleneck_summary_fields(bottleneck_data)

    # ---------------------------------------------------
    # Final Output
    # ---------------------------------------------------
    return {
        "inputs": {
            "usl": usl,
            "little_law": little_law,
            "queueing": queueing,
            "runtime": runtime,
            "bottleneck": bottleneck_data,
        },
        "calculations": {
            "safety_margin_ratio_used": safety_margin_ratio,
            "capacity_reference_users": capacity_reference,
            "arrival_rate": arrival_rate,
            "request_pressure": request_pressure,
            "little_law_consistency": little_law_consistency,
            "safe_users": safe_users,
            "remaining_capacity": remaining_capacity,
            "throughput_capacity_used_percent": throughput_capacity_used,
            "user_capacity_used_percent": user_capacity_used,
            "usl_user_capacity_used_percent": usl_user_capacity_used,
            "capacity_margin": capacity_margin,
            "breaking_point": breaking_point,
            "disk_io_limit_mbps": disk_io_limit_mbps,
            "network_io_limit_mbps": network_io_limit_mbps,
            "p95_response_time_threshold_ms": p95_response_time_threshold_ms
        },
        "results": {
            "safe_users": safe_users,
            "growth_potential_users": remaining_capacity,
            
            "throughput_capacity_used_percent": throughput_capacity_used,
            "user_capacity_used_percent": user_capacity_used,
            "usl_user_capacity_used_percent": usl_user_capacity_used,
            "capacity_margin": capacity_margin,
            "breaking_point": breaking_point,
            
            "usl_optimal_users": usl_optimal_users,
            "usl_saturation_point": usl["saturation_point"],
            "usl_peak_throughput": usl["peak_throughput"],
            
            "observed_max_users": observed_max_users,
            "observed_min_users": usl["observed_min_users"],
            "tested_user_range": (
                f"{usl['observed_min_users']:.0f} – "
                f"{observed_max_users:.0f}"
            ),

            "capacity_reference_users": capacity_reference,
            "capacity_reference_reason": capacity_reference_reason,

            "prediction_confidence": prediction_confidence,
            "extrapolation_reliable": usable_for_extrapolation,

            "request_pressure": request_pressure,
            "little_law_consistency": little_law_consistency,

            "queue_utilization": queueing["utilization"],
            "queue_length": queueing["queue_length"],
            "queue_waiting_time": queueing["waiting_time"],
            "queue_stability": queueing["stability"],
            "resource_pressure": resource_pressure,
            "scalability_efficiency": usl["scalability_efficiency"],
            "scalability_status": scalability_status,
            "capacity_classification": capacity_classification,
            "estimated_cpu_capacity_multiplier": scaling["cpu"],
            "estimated_memory_capacity_multiplier": scaling["memory"],

            "current_users": runtime["current_users"],
            "current_throughput": runtime["throughput"],
            "current_response_time": runtime["response_time"],
            "current_cpu_usage": runtime["cpu_usage"],
            "current_memory_usage": runtime["memory_usage"],
            "current_error_rate": runtime["error_rate"],

            **bottleneck_fields,
        },
        "health_summary": health_summary,
        "signals": signals
    }