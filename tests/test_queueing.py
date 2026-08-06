"""
test_queueing.py
================
Unit and integration tests for queueing.py (M/M/1 model) using pytest.
"""

import math
import os
import sys
import pytest

# Add project root directory to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from prediction.queueing import (
    QueueingAnalyzer,
    QueueingValidationError,
    QueueingCalculationError,
    QueueWorkload,
    QueueStatusThresholds,
    StabilityThresholds,
    CongestionRiskThresholds,
    calculate_utilization,
    calculate_queue_length,
    calculate_requests_in_system,
    calculate_waiting_time,
    calculate_system_time,
    calculate_service_time,
    calculate_idle_probability,
    classify_queue_status,
    classify_stability,
    classify_congestion_risk,
    analyze_queue,
)

from sample_data.sample_metrics import (
    QUEUE_LIGHT_WORKLOAD,
    QUEUE_MODERATE_WORKLOAD,
    QUEUE_NEAR_SATURATION_WORKLOAD,
    QUEUE_UNSTABLE_WORKLOAD,
    QUEUE_INVALID_BOOLEAN,
    QUEUE_INVALID_ZERO_RATE,
    QUEUE_INVALID_MISSING_KEY,
)


class TestQueueingValidation:
    """Tests input parsing and defensive validation guardrails."""

    def test_valid_workload_instantiation(self):
        workload = QueueWorkload.from_raw(QUEUE_LIGHT_WORKLOAD)
        assert workload.arrival_rate == 100.0
        assert workload.service_rate == 500.0
        assert workload.metadata["tier"] == "api-gateway"

    def test_missing_required_field_raises_error(self):
        with pytest.raises(QueueingValidationError, match="Missing required field"):
            QueueWorkload.from_raw(QUEUE_INVALID_MISSING_KEY)

    def test_boolean_type_rejection(self):
        with pytest.raises(QueueingValidationError, match="must be a numeric value"):
            QueueWorkload.from_raw(QUEUE_INVALID_BOOLEAN)

    def test_zero_or_negative_rate_raises_error(self):
        with pytest.raises(QueueingValidationError, match="must be greater than zero"):
            QueueWorkload.from_raw(QUEUE_INVALID_ZERO_RATE)

    def test_non_dict_input_raises_error(self):
        with pytest.raises(QueueingValidationError, match="Input must be a dictionary"):
            QueueWorkload.from_raw(["not", "a", "dict"])


class TestCoreEquations:
    """Tests standalone mathematical M/M/1 helper functions."""

    def test_utilization(self):
        # rho = 100 / 500 = 0.2
        assert calculate_utilization(100.0, 500.0) == pytest.approx(0.2)

    def test_utilization_zero_service_rate_guard(self):
        with pytest.raises(QueueingCalculationError, match="must be greater than zero"):
            calculate_utilization(100.0, 0.0)

    def test_queue_length_steady_state(self):
        # rho = 0.5 => Lq = 0.5^2 / (1 - 0.5) = 0.25 / 0.5 = 0.5
        assert calculate_queue_length(0.5) == pytest.approx(0.5)

    def test_queue_length_unstable_state(self):
        # rho >= 1.0 => inf
        assert math.isinf(calculate_queue_length(1.0))
        assert math.isinf(calculate_queue_length(1.5))

    def test_requests_in_system_steady_state(self):
        # rho = 0.5 => L = 0.5 / (1 - 0.5) = 1.0
        assert calculate_requests_in_system(0.5) == pytest.approx(1.0)

    def test_requests_in_system_unstable_state(self):
        assert math.isinf(calculate_requests_in_system(1.0))

    def test_waiting_time_and_system_time(self):
        # lambda = 100, Lq = 0.5, L = 1.0
        # Wq = 0.5 / 100 = 0.005 s
        # W  = 1.0 / 100 = 0.010 s
        assert calculate_waiting_time(0.5, 100.0) == pytest.approx(0.005)
        assert calculate_system_time(1.0, 100.0) == pytest.approx(0.010)

    def test_waiting_time_infinite_propagation(self):
        assert math.isinf(calculate_waiting_time(math.inf, 100.0))
        assert math.isinf(calculate_system_time(math.inf, 100.0))

    def test_idle_probability(self):
        # rho = 0.2 => P0 = 0.8
        assert calculate_idle_probability(0.2) == pytest.approx(0.8)
        # Clamped to 0.0 when rho >= 1.0
        assert calculate_idle_probability(1.2) == 0.0


class TestClassificationsAndThresholds:
    """Tests load classification, stability, and congestion risk bands."""

    def test_queue_status_bands(self):
        assert classify_queue_status(0.05) == "Idle"
        assert classify_queue_status(0.20) == "Light"
        assert classify_queue_status(0.40) == "Moderate"
        assert classify_queue_status(0.80) == "Busy"
        assert classify_queue_status(0.95) == "Congested"
        assert classify_queue_status(1.10) == "Critical"

    def test_stability_bands(self):
        assert classify_stability(0.5) == "Stable"
        assert classify_stability(0.85) == "Near Saturation"
        assert classify_stability(1.0) == "Unstable"

    def test_congestion_risk_escalation_by_queue_length(self):
        # Low utilization (rho=0.4) normally gives "Medium" or "Low" risk,
        # but a huge queue (>= 20) escalates risk to "Critical"
        risk = classify_congestion_risk(rho=0.4, queue_length=25.0)
        assert risk == "Critical"


class TestFullAnalysisAndConvenienceWrapper:
    """Tests full pipeline integration using analyze_queue()."""

    def test_light_workload_analysis(self):
        res = analyze_queue(QUEUE_LIGHT_WORKLOAD)
        assert res["utilization"] == pytest.approx(0.2)
        assert res["queue_status"] == "Light"
        assert res["stability"] == "Stable"
        assert res["congestion_risk"] == "Low"
        assert res["signals"]["unstable_system"] is False

    def test_near_saturation_analysis(self):
        res = analyze_queue(QUEUE_NEAR_SATURATION_WORKLOAD)
        assert res["utilization"] == pytest.approx(0.9)
        assert res["queue_status"] == "Congested"
        assert res["stability"] == "Near Saturation"
        assert res["signals"]["near_saturation"] is True

    def test_unstable_workload_graceful_handling(self):
        res = analyze_queue(QUEUE_UNSTABLE_WORKLOAD)
        assert res["utilization"] == pytest.approx(1.2)
        assert math.isinf(res["queue_length"])
        assert math.isinf(res["requests_in_system"])
        assert math.isinf(res["waiting_time"])
        assert math.isinf(res["system_time"])
        assert res["queue_status"] == "Critical"
        assert res["stability"] == "Unstable"
        assert res["congestion_risk"] == "Critical"
        assert res["signals"]["unstable_system"] is True