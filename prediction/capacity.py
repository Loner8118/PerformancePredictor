import math
from typing import Any, Dict, Union


Number = Union[int, float]


# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------


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



# ---------------------------------------------------------------------------
# Configurable Constants
# ---------------------------------------------------------------------------


DEFAULT_SAFETY_MARGIN_RATIO = 0.85


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



# ---------------------------------------------------------------------------
# Required Input Structure
# ---------------------------------------------------------------------------


REQUIRED_SECTIONS = (
    "usl",
    "little_law",
    "queueing",
    "runtime",
)


REQUIRED_FIELDS = {

    "usl": (
        "peak_throughput",
        "optimal_users",
        "saturation_point",
        "scalability_efficiency",
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



# ---------------------------------------------------------------------------
# Validation Helpers
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    """
    Check whether value is a valid finite number.
    """

    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )



def validate_input(data: Dict[str, Any]) -> None:
    """
    Validate complete input structure.
    """


    if not isinstance(data, dict):
        raise CapacityPlanningError(
            "Input to analyze_capacity must be a dictionary."
        )


    # Check sections

    for section in REQUIRED_SECTIONS:

        if section not in data:
            raise MissingSectionError(
                f"Missing required section: {section}"
            )


        if not isinstance(data[section], dict):
            raise CapacityPlanningError(
                f"Section {section} must be dictionary."
            )



    # Check fields

    for section, fields in REQUIRED_FIELDS.items():

        for field in fields:

            if field not in data[section]:

                raise MissingFieldError(
                    f"Missing field '{field}' in '{section}'."
                )



    # Check numeric values

    for section, fields in NUMERIC_FIELDS.items():

        for field in fields:

            value = data[section][field]


            if not _is_number(value):

                raise InvalidTypeError(
                    f"{section}.{field} must be numeric."
                )



    # -------------------------------------------------------
    # Range Validation
    # -------------------------------------------------------


    if not (
        0 <= data["usl"]["scalability_efficiency"] <= 1
    ):
        raise InvalidRangeError(
            "usl.scalability_efficiency must be between 0 and 1."
        )



    if data["queueing"]["utilization"] < 0 or data["queueing"]["utilization"] > 1:

        raise InvalidRangeError(
            "queueing.utilization must be between 0 and 1."
        )



    if data["queueing"]["queue_length"] < 0:

        raise InvalidRangeError(
            "queue_length cannot be negative."
        )



    if data["queueing"]["waiting_time"] < 0:

        raise InvalidRangeError(
            "waiting_time cannot be negative."
        )



    runtime = data["runtime"]


    if not 0 <= runtime["cpu_usage"] <= 100:

        raise InvalidRangeError(
            "cpu_usage must be between 0 and 100."
        )



    if not 0 <= runtime["memory_usage"] <= 100:

        raise InvalidRangeError(
            "memory_usage must be between 0 and 100."
        )



    if runtime["disk_io"] < 0:

        raise InvalidRangeError(
            "disk_io cannot be negative."
        )



    if runtime["network_io"] < 0:

        raise InvalidRangeError(
            "network_io cannot be negative."
        )



    if not 0 <= runtime["error_rate"] <= 100:

        raise InvalidRangeError(
            "error_rate must be between 0 and 100."
        )



    if runtime["throughput"] <= 0:

        raise InvalidRangeError(
            "throughput must be greater than zero."
        )



    if runtime["response_time"] <= 0:

        raise InvalidRangeError(
            "response_time must be greater than zero."
        )



    if runtime["current_users"] <= 0:

        raise InvalidRangeError(
            "current_users must be greater than zero."
        )

# ---------------------------------------------------------------------------
# Capacity Calculations
# ---------------------------------------------------------------------------


def calculate_arrival_rate_from_little_law(
    little_law: Dict[str, Any]
) -> float:
    """
    Little's Law:

        λ = L / W

    L = requests currently in system
    W = average time in system
    """

    time_in_system = little_law["time_in_system"]

    if time_in_system <= 0:
        return 0.0

    return round(
        little_law["requests_in_system"] / time_in_system,
        2
    )



def calculate_request_pressure(
    arrival_rate: float,
    peak_throughput: float
) -> float:
    """
    Request pressure:

        arrival_rate / peak_throughput

    Values:
        < 0.5  -> Low demand
        ~1.0   -> Near capacity
        >1.0   -> Overloaded
    """

    if peak_throughput <= 0:
        return 0.0

    return round(
        arrival_rate / peak_throughput,
        2
    )



def calculate_safe_capacity(
    usl: Dict[str, Any],
    queueing: Dict[str, Any],
    safety_margin_ratio: float
) -> float:
    """
    Safe operating users.

    Based on:

        min(USL optimal users,
            USL saturation users)

    with safety margin.

    If queue is unstable, margin is reduced.
    """

    reference_point = min(
        usl["optimal_users"],
        usl["saturation_point"]
    )


    effective_margin = safety_margin_ratio


    if str(queueing["stability"]).lower() != "stable":

        effective_margin *= 0.9



    return round(
        reference_point * effective_margin,
        2
    )



def calculate_remaining_capacity(
    safe_users: float,
    current_users: Number
) -> float:

    return round(
        max(
            safe_users - current_users,
            0.0
        ),
        2
    )



def calculate_capacity_used_percent(
    throughput: Number,
    peak_throughput: Number
) -> float:

    return round(
        (throughput / peak_throughput) * 100,
        2
    )



def calculate_capacity_margin(
    remaining_capacity: float,
    safe_users: float
) -> float:

    if safe_users <= 0:
        return 0.0


    return round(
        (remaining_capacity / safe_users) * 100,
        2
    )



def calculate_breaking_point(
    usl: Dict[str, Any],
    queueing: Dict[str, Any],
    runtime: Dict[str, Any]
) -> float:
    """
    Estimate maximum sustainable users.

    Uses:

    1. USL saturation point
    2. Queue stability
    3. CPU extrapolation

    CPU assumption:

    CPU consumption increases approximately
    linearly within tested workload range.
    """

    saturation_point = usl["saturation_point"]



    # Queue limitation

    if str(queueing["stability"]).lower() != "stable":

        queue_limit = runtime["current_users"]

    else:

        queue_limit = saturation_point



    # CPU limitation

    cpu_usage = runtime["cpu_usage"]

    current_users = runtime["current_users"]


    if cpu_usage > 0:

        cpu_limit = (
            current_users / cpu_usage
        ) * CPU_SAFE_LIMIT


        cpu_limit = min(
            cpu_limit,
            saturation_point
        )

    else:

        cpu_limit = saturation_point



    return round(
        min(
            saturation_point,
            queue_limit,
            cpu_limit
        ),
        2
    )



# ---------------------------------------------------------------------------
# Resource Pressure Analysis
# ---------------------------------------------------------------------------


def _classify_by_thresholds(
    value: float,
    thresholds: Dict[str, float]
) -> str:

    if value < thresholds["Low"]:
        return "Low"


    if value < thresholds["Moderate"]:
        return "Moderate"


    if value < thresholds["High"]:
        return "High"


    return "Critical"




def analyze_resource_pressure(
    runtime: Dict[str, Any],
    queueing: Dict[str, Any]
) -> Dict[str, str]:
    """
    Classifies resource pressure.

    Resources:

        CPU
        Memory
        Disk
        Network
        Queue
    """


    cpu_pressure = _classify_by_thresholds(
        runtime["cpu_usage"],
        CPU_PRESSURE_THRESHOLDS
    )


    memory_pressure = _classify_by_thresholds(
        runtime["memory_usage"],
        MEMORY_PRESSURE_THRESHOLDS
    )


    disk_pressure = _classify_by_thresholds(
        runtime["disk_io"],
        DISK_IO_THRESHOLDS
    )


    network_pressure = _classify_by_thresholds(
        runtime["network_io"],
        NETWORK_IO_THRESHOLDS
    )


    queue_pressure = _classify_by_thresholds(
        queueing["utilization"],
        QUEUE_UTILIZATION_THRESHOLDS
    )


    if queueing["queue_length"] >= QUEUE_LENGTH_CRITICAL_THRESHOLD:

        queue_pressure = "Critical"



    return {

        "cpu": cpu_pressure,

        "memory": memory_pressure,

        "disk": disk_pressure,

        "network": network_pressure,

        "queue": queue_pressure,

    }



# ---------------------------------------------------------------------------
# Status Classification
# ---------------------------------------------------------------------------


_PRESSURE_RANK = {

    "Low": 0,

    "Normal": 0,

    "Moderate": 1,

    "High": 2,

    "Critical": 3,

}




def determine_scalability_status(
    usl: Dict[str, Any],
    queueing: Dict[str, Any],
    capacity_used_percent: float,
    resource_pressure: Dict[str, str],
    runtime: Dict[str, Any]
) -> str:


    penalty = 0



    # USL scalability efficiency

    if usl["scalability_efficiency"] < 0.5:

        penalty += 2



    # Capacity usage

    if capacity_used_percent >= 90:

        penalty += 2

    elif capacity_used_percent >= 75:

        penalty += 1



    # Queue utilization

    utilization = queueing["utilization"]


    if utilization >= 0.85:

        penalty += 2

    elif utilization >= 0.7:

        penalty += 1



    if str(queueing["stability"]).lower() != "stable":

        penalty += 2



    # Resource pressure

    worst_pressure = max(
        resource_pressure.values(),
        key=lambda x: _PRESSURE_RANK[x]
    )


    penalty += _PRESSURE_RANK[worst_pressure]



    # Error rate

    if runtime["error_rate"] >= 5:

        penalty += 2

    elif runtime["error_rate"] >= 1:

        penalty += 1



    # Response time

    if runtime["response_time"] > RESPONSE_TIME_THRESHOLD_MS:

        penalty += 2



    if penalty <= 2:

        return "Excellent"


    if penalty <= 5:

        return "Good"


    if penalty <= 8:

        return "Moderate"


    if penalty <= 11:

        return "Limited"


    return "Poor"




def classify_capacity(
    capacity_margin: float,
    capacity_used_percent: float,
    queueing: Dict[str, Any],
    resource_pressure: Dict[str, str],
    runtime: Dict[str, Any]
) -> str:


    if capacity_margin >= 40:

        status = "Healthy"


    elif capacity_margin >= 20:

        status = "Near Capacity"


    elif capacity_margin >= 5:

        status = "At Capacity"


    else:

        status = "Overloaded"



    escalate = (

        str(queueing["stability"]).lower() != "stable"

        or "Critical" in resource_pressure.values()

        or capacity_used_percent > 100

        or runtime["error_rate"] >= 5

        or runtime["response_time"] > RESPONSE_TIME_THRESHOLD_MS

    )



    order = [

        "Healthy",

        "Near Capacity",

        "At Capacity",

        "Overloaded",

        "Critical"

    ]



    if escalate:

        index = order.index(status)

        return order[
            min(index + 1, len(order)-1)
        ]



    return status

# ---------------------------------------------------------------------------
# Resource Scaling Estimates & Health Summary
# ---------------------------------------------------------------------------


def estimate_resource_scaling(
    runtime: Dict[str, Any],
    breaking_point: float
) -> Dict[str, float]:
    """
    Estimate required resource capacity multiplier.

    Example:

        cpu_multiplier = 2.0

    means approximately 2x current CPU capacity
    may be required to safely reach breaking point.
    """


    current_users = runtime["current_users"]


    if current_users <= 0:

        return {
            "cpu": 1.0,
            "memory": 1.0
        }



    growth_ratio = breaking_point / current_users



    def calculate_multiplier(
        usage: float,
        safe_limit: float
    ) -> float:


        if usage <= 0:

            return 1.0



        multiplier = (
            usage * growth_ratio
        ) / safe_limit



        return round(
            max(
                1.0,
                min(multiplier, 5.0)
            ),
            2
        )



    return {

        "cpu": calculate_multiplier(
            runtime["cpu_usage"],
            CPU_SAFE_LIMIT
        ),


        "memory": calculate_multiplier(
            runtime["memory_usage"],
            MEMORY_SAFE_LIMIT
        )

    }




def determine_main_risk(
    resource_pressure: Dict[str, str]
) -> str:
    """
    Finds highest pressure resource.
    """


    highest = 0

    risk = "None"



    for resource, status in resource_pressure.items():

        rank = _PRESSURE_RANK.get(
            status,
            0
        )


        if rank > highest:

            highest = rank

            risk = resource.capitalize()



    return risk




# ---------------------------------------------------------------------------
# Recommendation Engine Signals
# ---------------------------------------------------------------------------


def generate_signals(
    capacity_margin: float,
    capacity_used_percent: float,
    resource_pressure: Dict[str, str],
    scalability_status: str
) -> Dict[str, bool]:
    """
    Generates machine-readable signals
    for Recommendation Engine.
    """


    capacity_exceeded = (

        capacity_used_percent > 100

        or capacity_margin <= 0

    )


    capacity_nearly_full = (

        not capacity_exceeded

        and capacity_margin <= 20

    )


    capacity_low = (

        not capacity_exceeded

        and not capacity_nearly_full

        and capacity_margin <= 40

    )


    capacity_available = not (

        capacity_exceeded

        or capacity_nearly_full

        or capacity_low

    )



    cpu_pressure = resource_pressure["cpu"] in (
        "High",
        "Critical"
    )


    memory_pressure = resource_pressure["memory"] in (
        "High",
        "Critical"
    )


    queue_pressure = resource_pressure["queue"] in (
        "High",
        "Critical"
    )


    disk_pressure = resource_pressure["disk"] in (
        "High",
        "Critical"
    )


    network_pressure = resource_pressure["network"] in (
        "High",
        "Critical"
    )



    resource_bottleneck = (

        cpu_pressure

        or memory_pressure

        or queue_pressure

        or disk_pressure

        or network_pressure

    )



    horizontal_scaling_candidate = (

        queue_pressure

        or scalability_status in (
            "Limited",
            "Poor"
        )

    )



    vertical_scaling_candidate = (

        (cpu_pressure or memory_pressure)

        and not queue_pressure

    )



    return {


        "capacity_available":
            capacity_available,


        "capacity_low":
            capacity_low,


        "capacity_nearly_full":
            capacity_nearly_full,


        "capacity_exceeded":
            capacity_exceeded,


        "cpu_pressure":
            cpu_pressure,


        "memory_pressure":
            memory_pressure,


        "queue_pressure":
            queue_pressure,


        "disk_pressure":
            disk_pressure,


        "network_pressure":
            network_pressure,


        "resource_bottleneck":
            resource_bottleneck,


        "horizontal_scaling_candidate":
            horizontal_scaling_candidate,


        "vertical_scaling_candidate":
            vertical_scaling_candidate,

    }




# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_capacity(
    data: Dict[str, Any],
    safety_margin_ratio: float = DEFAULT_SAFETY_MARGIN_RATIO
) -> Dict[str, Any]:
    """
    Main Capacity Planning Engine.

    Combines:

        USL
        Little's Law
        Queueing Theory
        Runtime Metrics

    and produces:

        Capacity Analysis
        Health Summary
        Recommendation Signals
    """



    validate_input(data)



    if not (
        0 < safety_margin_ratio <= 1
    ):

        raise InvalidRangeError(
            "Safety margin must be between 0 and 1."
        )



    usl = data["usl"]

    little_law = data["little_law"]

    queueing = data["queueing"]

    runtime = data["runtime"]




    # ---------------------------------------------------
    # Mathematical calculations
    # ---------------------------------------------------


    arrival_rate = calculate_arrival_rate_from_little_law(
        little_law
    )


    request_pressure = calculate_request_pressure(
        arrival_rate,
        usl["peak_throughput"]
    )


    safe_users = calculate_safe_capacity(
        usl,
        queueing,
        safety_margin_ratio
    )


    remaining_capacity = calculate_remaining_capacity(
        safe_users,
        runtime["current_users"]
    )


    capacity_used = calculate_capacity_used_percent(
        runtime["throughput"],
        usl["peak_throughput"]
    )


    capacity_margin = calculate_capacity_margin(
        remaining_capacity,
        safe_users
    )


    breaking_point = calculate_breaking_point(
        usl,
        queueing,
        runtime
    )




    # ---------------------------------------------------
    # Analysis
    # ---------------------------------------------------


    resource_pressure = analyze_resource_pressure(
        runtime,
        queueing
    )


    scalability_status = determine_scalability_status(
        usl,
        queueing,
        capacity_used,
        resource_pressure,
        runtime
    )



    capacity_classification = classify_capacity(
        capacity_margin,
        capacity_used,
        queueing,
        resource_pressure,
        runtime
    )



    scaling = estimate_resource_scaling(
        runtime,
        breaking_point
    )



    signals = generate_signals(
        capacity_margin,
        capacity_used,
        resource_pressure,
        scalability_status
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


        "overall_status":
            capacity_classification,


        "main_risk":
            determine_main_risk(
                resource_pressure
            ),


        "recommended_scaling":
            scaling_recommendation

    }




    # ---------------------------------------------------
    # Final Output
    # ---------------------------------------------------


    return {


        "inputs": {

            "usl": usl,

            "little_law": little_law,

            "queueing": queueing,

            "runtime": runtime

        },



        "calculations": {


            "safety_margin_ratio_used":
                safety_margin_ratio,


            "reference_point":
                min(
                    usl["optimal_users"],
                    usl["saturation_point"]
                ),


            "arrival_rate":
                arrival_rate,


            "request_pressure":
                request_pressure,


            "safe_users":
                safe_users,


            "remaining_capacity":
                remaining_capacity,


            "capacity_used_percent":
                capacity_used,


            "capacity_margin":
                capacity_margin,


            "breaking_point":
                breaking_point

        },



        "results": {


            "safe_users":
                safe_users,


            "growth_potential_users":
                remaining_capacity,


            "capacity_used_percent":
                capacity_used,


            "capacity_margin":
                capacity_margin,


            "breaking_point":
                breaking_point,


            "request_pressure":
                request_pressure,


            "resource_pressure":
                resource_pressure,


            "scalability_efficiency":
                usl["scalability_efficiency"],


            "scalability_status":
                scalability_status,


            "capacity_classification":
                capacity_classification,


            "estimated_cpu_capacity_multiplier":
                scaling["cpu"],


            "estimated_memory_capacity_multiplier":
                scaling["memory"]

        },



        "health_summary":
            health_summary,



        "signals":
            signals

    }