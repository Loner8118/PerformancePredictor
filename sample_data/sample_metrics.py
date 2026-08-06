"""
sample_metrics.py
=================
Contains synthetic load test datasets representing different real-world 
application scaling characteristics for testing the USL module.
"""

# 1. Ideal / Good Scaling App
# Example: Lightweight stateless web service (scaling linearly with minimal contention)
GOOD_SCALING_DATA = {
    "user_levels": [10, 20, 50, 100, 200],
    "throughput": [100.0, 198.0, 480.0, 930.0, 1750.0]
}

# 2. Database Contention Bottleneck (High Sigma)
# Example: App hitting connection pool limits or lock contention as users grow
# sample_data/sample_metrics.py
CONTENTION_HEAVY_DATA = {
    "user_levels": [10, 25, 50, 100, 150, 200],
    "throughput": [100.0, 180.0, 220.0, 235.0, 240.0, 242.0]
}

# 3. Crosstalk / Coherency Bottleneck (High Kappa -> Retrograde/Negative Scaling)
# Example: Distributed app with excessive inter-node communication or cache coherence storms
RETROGRADE_SCALING_DATA = {
    "user_levels": [10, 20, 40, 80, 120, 160],
    "throughput": [150.0, 280.0, 450.0, 410.0, 320.0, 210.0]
}

# 4. Invalid Datasets (For Testing Exception Handling)
TOO_FEW_POINTS_DATA = {
    "user_levels": [10, 20],
    "throughput": [100.0, 190.0]
}

MISMATCHED_LENGTH_DATA = {
    "user_levels": [10, 20, 30],
    "throughput": [100.0, 190.0]
}

NEGATIVE_VALUES_DATA = {
    "user_levels": [-10, 20, 30],
    "throughput": [100.0, 190.0, 280.0]
}

# ==============================================================================
# LITTLE'S LAW SAMPLE DATASETS
# ==============================================================================

# 1. Normal/Light Workload (Response time in seconds)
# L = 20.0 * 0.15 = 3.0 requests in system ("Very Light")
LL_LIGHT_WORKLOAD = {
    "arrival_rate": 20.0,
    "average_response_time": 0.15,
    "response_time_unit": "s",
    "metadata": {"environment": "staging", "endpoint": "/api/v1/users"}
}

# 2. Moderate Workload with Millisecond Conversion
# L = 500.0 * (200.0 / 1000.0) = 100.0 requests in system ("Moderate")
LL_MS_WORKLOAD = {
    "arrival_rate": 500.0,
    "average_response_time": 200.0,
    "response_time_unit": "ms",
    "metadata": {"environment": "production", "endpoint": "/api/v1/search"}
}

# 3. High/Critical Workload
# L = 1000.0 * 1.2 = 1200.0 requests in system ("Critical")
LL_CRITICAL_WORKLOAD = {
    "arrival_rate": 1000.0,
    "average_response_time": 1.2,
    "response_time_unit": "seconds"
}

# 4. Previous Snapshot (Used for testing trend detection)
# L = 400.0 * 0.2 = 80.0 requests in system
LL_PREVIOUS_SNAPSHOT = {
    "arrival_rate": 400.0,
    "average_response_time": 0.2,
    "response_time_unit": "s"
}

# 5. Invalid Payloads (For Exception Handling Tests)
LL_INVALID_MISSING_KEY = {
    "arrival_rate": 100.0
    # missing 'average_response_time'
}

LL_INVALID_BOOLEAN_TYPE = {
    "arrival_rate": True,  # Python treats bool as int subclass; code must reject this
    "average_response_time": 0.5
}

LL_INVALID_NEGATIVE_RATE = {
    "arrival_rate": -50.0,
    "average_response_time": 0.5
}

LL_INVALID_UNIT = {
    "arrival_rate": 100.0,
    "average_response_time": 0.5,
    "response_time_unit": "fortnights"
}

# ==============================================================================
# QUEUEING THEORY (M/M/1) SAMPLE DATASETS
# ==============================================================================

# 1. Normal / Light Workload
# lambda = 100, mu = 500 => rho = 0.2 (20% utilization)
QUEUE_LIGHT_WORKLOAD = {
    "arrival_rate": 100.0,
    "service_rate": 500.0,
    "metadata": {"tier": "api-gateway", "region": "us-east-1"}
}

# 2. Busy / Moderate Workload
# lambda = 350, mu = 500 => rho = 0.7 (70% utilization)
QUEUE_MODERATE_WORKLOAD = {
    "arrival_rate": 350.0,
    "service_rate": 500.0,
    "metadata": {"tier": "api-gateway", "region": "us-east-1"}
}

# 3. Near Saturation Workload
# lambda = 450, mu = 500 => rho = 0.9 (90% utilization)
QUEUE_NEAR_SATURATION_WORKLOAD = {
    "arrival_rate": 450.0,
    "service_rate": 500.0,
    "metadata": {"tier": "checkout-service", "region": "us-east-1"}
}

# 4. Overloaded / Unstable Workload (rho >= 1.0)
# lambda = 600, mu = 500 => rho = 1.2 (120% utilization)
QUEUE_UNSTABLE_WORKLOAD = {
    "arrival_rate": 600.0,
    "service_rate": 500.0,
    "metadata": {"tier": "checkout-service", "region": "us-east-1"}
}

# 5. Invalid Payloads (For Error Path Tests)
QUEUE_INVALID_BOOLEAN = {
    "arrival_rate": True,
    "service_rate": 500.0
}

QUEUE_INVALID_ZERO_RATE = {
    "arrival_rate": 0.0,
    "service_rate": 500.0
}

QUEUE_INVALID_MISSING_KEY = {
    "arrival_rate": 100.0
    # missing 'service_rate'
}

import copy

# -------------------------------------------------------------------------
# VALID DATASETS
# -------------------------------------------------------------------------

CAPACITY_HEALTHY = {
    "usl": {
        "peak_throughput": 1400.0,
        "optimal_users": 500.0,
        "saturation_point": 550.0
    },
    "little_law": {
        "requests_in_system": 120.0,
        "system_occupancy": 120.0
    },
    "queueing": {
        "utilization": 0.55,
        "queue_length": 2.5,
        "waiting_time": 0.010,
        "stability": "Stable"
    },
    "runtime": {
        "current_users": 150.0,
        "throughput": 850.0,
        "response_time": 170.0,
        "cpu_usage": 52.0,
        "memory_usage": 48.0,
        "error_rate": 0.2
    }
}

CAPACITY_NEAR_LIMIT = {
    "usl": {
        "peak_throughput": 1400.0,
        "optimal_users": 500.0,
        "saturation_point": 550.0
    },
    "little_law": {
        "requests_in_system": 380.0,
        "system_occupancy": 380.0
    },
    "queueing": {
        "utilization": 0.88,
        "queue_length": 8.0,
        "waiting_time": 0.090,
        "stability": "Stable"
    },
    "runtime": {
        "current_users": 420.0,
        "throughput": 1320.0,
        "response_time": 420.0,
        "cpu_usage": 84.0,
        "memory_usage": 81.0,
        "error_rate": 1.5
    }
}

CAPACITY_OVERLOADED = {
    "usl": {
        "peak_throughput": 1400.0,
        "optimal_users": 500.0,
        "saturation_point": 550.0
    },
    "little_law": {
        "requests_in_system": 700.0,
        "system_occupancy": 700.0
    },
    "queueing": {
        "utilization": 0.99,
        "queue_length": 18.0,
        "waiting_time": 0.450,
        "stability": "Unstable"
    },
    "runtime": {
        "current_users": 600.0,
        "throughput": 1450.0,
        "response_time": 1500.0,
        "cpu_usage": 97.0,
        "memory_usage": 95.0,
        "error_rate": 8.0
    }
}

# -------------------------------------------------------------------------
# INVALID PAYLOADS
# -------------------------------------------------------------------------

CAPACITY_INVALID_MISSING_SECTION = {
    "usl": {},
    "runtime": {}
}

CAPACITY_INVALID_MISSING_FIELD = copy.deepcopy(CAPACITY_HEALTHY)
del CAPACITY_INVALID_MISSING_FIELD["runtime"]["cpu_usage"]

CAPACITY_INVALID_BOOLEAN = copy.deepcopy(CAPACITY_HEALTHY)
CAPACITY_INVALID_BOOLEAN["runtime"]["cpu_usage"] = True

CAPACITY_INVALID_UTILIZATION = copy.deepcopy(CAPACITY_HEALTHY)
CAPACITY_INVALID_UTILIZATION["queueing"]["utilization"] = 1.5

CAPACITY_INVALID_CPU = copy.deepcopy(CAPACITY_HEALTHY)
CAPACITY_INVALID_CPU["runtime"]["cpu_usage"] = 150.0

CAPACITY_INVALID_MEMORY = copy.deepcopy(CAPACITY_HEALTHY)
CAPACITY_INVALID_MEMORY["runtime"]["memory_usage"] = -5.0

CAPACITY_INVALID_THROUGHPUT = copy.deepcopy(CAPACITY_HEALTHY)
CAPACITY_INVALID_THROUGHPUT["runtime"]["throughput"] = 0.0

CAPACITY_INVALID_USERS = copy.deepcopy(CAPACITY_HEALTHY)
CAPACITY_INVALID_USERS["runtime"]["current_users"] = -50.0

"""
sample_metrics.py
=================
Sample metrics payloads for testing predictor execution.
"""

PREDICTOR_VALID_METRICS = {
    "load_test": {
        "user_levels": [10, 20, 30, 40, 50],
        "throughput": [100.0, 180.0, 240.0, 280.0, 300.0],
    },
    "runtime": {
        "current_users": 25,
        "throughput": 220.0,
        "response_time": 0.05,
        "arrival_rate": 220.0,
        "cpu_usage": 65.0,
        "memory_usage": 70.0,
        "error_rate": 0.01,
    },
    "queue": {
        "arrival_rate": 220.0,
        "service_rate": 300.0,
    },
}

PREDICTOR_NOT_DICT = "invalid_type"

PREDICTOR_MISSING_QUEUE = {
    "load_test": {
        "user_levels": [10, 20, 30],
        "throughput": [100.0, 180.0, 240.0],
    },
    "runtime": {
        "current_users": 25,
        "throughput": 220.0,
        "response_time": 0.05,
        "arrival_rate": 220.0,
        "cpu_usage": 65.0,
        "memory_usage": 70.0,
        "error_rate": 0.01,
    },
}

PREDICTOR_MISSING_RUNTIME_KEY = {
    "load_test": {
        "user_levels": [10, 20, 30],
        "throughput": [100.0, 180.0, 240.0],
    },
    "runtime": {
        "current_users": 25,
        "throughput": 220.0,
        "response_time": 0.05,
        "cpu_usage": 65.0,
        "memory_usage": 70.0,
        # missing error_rate key on purpose
    },
    "queue": {
        "arrival_rate": 220.0,
        "service_rate": 300.0,
    },
}

PREDICTOR_INVALID_LENGTH = {
    "load_test": {
        "user_levels": [10, 20, 30],
        "throughput": [100.0, 180.0],  # Length mismatch
    },
    "runtime": {
        "current_users": 25,
        "throughput": 220.0,
        "response_time": 0.05,
        "arrival_rate": 220.0,
        "cpu_usage": 65.0,
        "memory_usage": 70.0,
        "error_rate": 0.01,
    },
    "queue": {
        "arrival_rate": 220.0,
        "service_rate": 300.0,
    },
}