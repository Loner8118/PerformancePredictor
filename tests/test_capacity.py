"""
test_capacity.py
================
Unit and integration tests for prediction/capacity.py using pytest.
"""

import os
import sys
import pytest

# Add project root directory to Python path
sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
)

from prediction.capacity import (
    analyze_capacity,
    validate_input,
    calculate_safe_capacity,
    calculate_remaining_capacity,
    calculate_capacity_used_percent,
    calculate_capacity_margin,
    calculate_breaking_point,
    analyze_resource_pressure,
    determine_scalability_status,
    classify_capacity,
    estimate_resource_scaling,
    calculate_growth_potential,
    generate_signals,
    CapacityPlanningError,
    MissingSectionError,
    MissingFieldError,
    InvalidTypeError,
    InvalidRangeError,
)

from sample_data.sample_metrics import (
    CAPACITY_HEALTHY,
    CAPACITY_NEAR_LIMIT,
    CAPACITY_OVERLOADED,
    CAPACITY_INVALID_MISSING_SECTION,
    CAPACITY_INVALID_MISSING_FIELD,
    CAPACITY_INVALID_BOOLEAN,
    CAPACITY_INVALID_UTILIZATION,
    CAPACITY_INVALID_CPU,
    CAPACITY_INVALID_MEMORY,
    CAPACITY_INVALID_THROUGHPUT,
    CAPACITY_INVALID_USERS,
)


# ==============================================================================
# Validation Tests
# ==============================================================================

class TestValidation:
    """Tests input validation rules."""

    def test_missing_section(self):
        with pytest.raises(MissingSectionError):
            analyze_capacity(CAPACITY_INVALID_MISSING_SECTION)

    def test_missing_field(self):
        with pytest.raises(MissingFieldError):
            analyze_capacity(CAPACITY_INVALID_MISSING_FIELD)

    def test_boolean_type(self):
        with pytest.raises(InvalidTypeError):
            analyze_capacity(CAPACITY_INVALID_BOOLEAN)

    def test_invalid_utilization(self):
        with pytest.raises(InvalidRangeError):
            analyze_capacity(CAPACITY_INVALID_UTILIZATION)

    def test_invalid_cpu(self):
        with pytest.raises(InvalidRangeError):
            analyze_capacity(CAPACITY_INVALID_CPU)

    def test_invalid_memory(self):
        with pytest.raises(InvalidRangeError):
            analyze_capacity(CAPACITY_INVALID_MEMORY)

    def test_invalid_throughput(self):
        with pytest.raises(InvalidRangeError):
            analyze_capacity(CAPACITY_INVALID_THROUGHPUT)

    def test_invalid_users(self):
        with pytest.raises(InvalidRangeError):
            analyze_capacity(CAPACITY_INVALID_USERS)


# ==============================================================================
# Mathematical Calculation Tests
# ==============================================================================

class TestCapacityCalculations:
    """Tests individual mathematical calculations."""

    def test_safe_capacity(self):
        safe = calculate_safe_capacity(
            CAPACITY_HEALTHY["usl"],
            CAPACITY_HEALTHY["queueing"],
            0.85,
        )

        assert safe > 0

    def test_remaining_capacity(self):
        remaining = calculate_remaining_capacity(500, 200)

        assert remaining == pytest.approx(300)

    def test_capacity_used_percent(self):
        used = calculate_capacity_used_percent(700, 1400)

        assert used == pytest.approx(50.0)

    def test_capacity_margin(self):
        margin = calculate_capacity_margin(300, 600)

        assert margin == pytest.approx(50.0)

    def test_breaking_point(self):
        bp = calculate_breaking_point(
            CAPACITY_HEALTHY["usl"],
            CAPACITY_HEALTHY["queueing"],
            CAPACITY_HEALTHY["runtime"],
        )

        assert bp > 0

    def test_growth_potential(self):
        assert calculate_growth_potential(250) == 250


# ==============================================================================
# Resource Pressure Tests
# ==============================================================================

class TestResourcePressure:
    """Tests CPU, Memory and Queue pressure classification."""

    def test_healthy_pressure(self):
        pressure = analyze_resource_pressure(
            CAPACITY_HEALTHY["runtime"],
            CAPACITY_HEALTHY["queueing"],
        )

        assert pressure["cpu"] in ("Low", "Moderate")
        assert pressure["memory"] in ("Low", "Moderate")
        assert pressure["queue"] in ("Low", "Moderate")

    def test_overloaded_pressure(self):
        pressure = analyze_resource_pressure(
            CAPACITY_OVERLOADED["runtime"],
            CAPACITY_OVERLOADED["queueing"],
        )

        assert pressure["cpu"] == "Critical"
        assert pressure["memory"] == "Critical"
        assert pressure["queue"] == "Critical"


# ==============================================================================
# Classification Tests
# ==============================================================================

class TestClassifications:
    """Tests scalability and capacity classifications."""

    def test_scalability_status(self):
        pressure = analyze_resource_pressure(
            CAPACITY_HEALTHY["runtime"],
            CAPACITY_HEALTHY["queueing"],
        )

        status = determine_scalability_status(
            CAPACITY_HEALTHY["usl"],
            CAPACITY_HEALTHY["queueing"],
            60,
            pressure,
        )

        assert status in (
            "Excellent",
            "Good",
            "Moderate",
            "Limited",
            "Poor",
        )

    def test_capacity_classification(self):
        pressure = analyze_resource_pressure(
            CAPACITY_OVERLOADED["runtime"],
            CAPACITY_OVERLOADED["queueing"],
        )

        status = classify_capacity(
            0,
            110,
            CAPACITY_OVERLOADED["queueing"],
            pressure,
        )

        assert status in (
            "Overloaded",
            "Critical",
        )


# ==============================================================================
# Scaling & Signal Tests
# ==============================================================================

class TestScalingAndSignals:
    """Tests scaling estimation and recommendation signals."""

    def test_scaling_estimates(self):
        scaling = estimate_resource_scaling(
            CAPACITY_HEALTHY["runtime"],
            80,
        )

        assert scaling["cpu"] >= 1.0
        assert scaling["memory"] >= 1.0

    def test_signal_generation(self):
        pressure = analyze_resource_pressure(
            CAPACITY_OVERLOADED["runtime"],
            CAPACITY_OVERLOADED["queueing"],
        )

        signals = generate_signals(
            capacity_margin=0,
            capacity_used_percent=110,
            resource_pressure=pressure,
            scalability_status="Poor",
        )

        assert signals["capacity_exceeded"] is True
        assert signals["resource_bottleneck"] is True
        assert signals["horizontal_scaling_candidate"] is True


# ==============================================================================
# Full Integration Tests
# ==============================================================================

class TestIntegration:
    """Tests the complete capacity analysis workflow."""

    def test_full_analysis_healthy(self):
        result = analyze_capacity(CAPACITY_HEALTHY)

        assert "inputs" in result
        assert "calculations" in result
        assert "results" in result
        assert "signals" in result

        assert result["results"]["safe_users"] > 0
        assert result["results"]["remaining_capacity"] >= 0
        assert result["results"]["capacity_used_percent"] > 0

    def test_full_analysis_near_capacity(self):
        result = analyze_capacity(CAPACITY_NEAR_LIMIT)

        assert result["results"]["capacity_margin"] >= 0

        assert result["results"]["capacity_classification"] in (
            "Healthy",
            "Near Capacity",
            "At Capacity",
            "Overloaded",
            "Critical",
        )

    def test_full_analysis_overloaded(self):
        result = analyze_capacity(CAPACITY_OVERLOADED)

        assert result["results"]["capacity_classification"] in (
            "Overloaded",
            "Critical",
        )

        assert result["signals"]["resource_bottleneck"] is True
        assert result["signals"]["capacity_exceeded"] is True