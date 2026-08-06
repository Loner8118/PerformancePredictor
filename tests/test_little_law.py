"""
test_little_law.py
==================
Unit and integration tests for prediction/little_law.py using pytest.
"""

import os
import sys
import pytest

# Add project root directory to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from prediction.little_law import (
    LittleLawAnalyzer,
    LittleLawValidationError,
    LittleLawCalculationError,
    LoadClassificationThresholds,
    analyze_workload,
    calculate_requests,
    calculate_arrival_rate,
    calculate_response_time,
    classify_load,
)

from sample_data.sample_metrics import (
    LL_LIGHT_WORKLOAD,
    LL_MS_WORKLOAD,
    LL_CRITICAL_WORKLOAD,
    LL_PREVIOUS_SNAPSHOT,
    LL_INVALID_MISSING_KEY,
    LL_INVALID_BOOLEAN_TYPE,
    LL_INVALID_NEGATIVE_RATE,
    LL_INVALID_UNIT,
)


class TestValidationAndUnitConversion:
    """Tests input validation rules and unit conversions."""

    def test_missing_required_field_raises_error(self):
        with pytest.raises(LittleLawValidationError, match="Missing required field"):
            analyze_workload(LL_INVALID_MISSING_KEY)

    def test_boolean_type_rejection(self):
        with pytest.raises(LittleLawValidationError, match="must be a numeric value"):
            analyze_workload(LL_INVALID_BOOLEAN_TYPE)

    def test_negative_arrival_rate_raises_error(self):
        with pytest.raises(LittleLawValidationError, match="must be greater than zero"):
            analyze_workload(LL_INVALID_NEGATIVE_RATE)

    def test_unsupported_unit_raises_error(self):
        with pytest.raises(LittleLawValidationError, match="Unsupported response_time_unit"):
            analyze_workload(LL_INVALID_UNIT)

    def test_millisecond_conversion(self):
        # 500 req/s @ 200 ms = 500 * 0.2s = 100 requests in system
        result = analyze_workload(LL_MS_WORKLOAD)
        assert result["response_time"] == pytest.approx(0.2)
        assert result["requests_in_system"] == pytest.approx(100.0)


class TestCoreMathematics:
    """Tests pure mathematical helper functions for Little's Law."""

    def test_calculate_requests(self):
        # L = 100 * 0.5 = 50
        assert calculate_requests(100.0, 0.5) == pytest.approx(50.0)

    def test_calculate_arrival_rate(self):
        # lambda = 50 / 0.5 = 100
        assert calculate_arrival_rate(50.0, 0.5) == pytest.approx(100.0)

    def test_calculate_response_time(self):
        # W = 50 / 100 = 0.5
        assert calculate_response_time(50.0, 100.0) == pytest.approx(0.5)

    def test_calculation_zero_division_guard(self):
        with pytest.raises(LittleLawCalculationError):
            calculate_arrival_rate(50.0, 0.0)

        with pytest.raises(LittleLawCalculationError):
            calculate_response_time(50.0, 0.0)


class TestClassificationAndThresholds:
    """Tests load classification boundaries."""

    def test_default_classification_bands(self):
        thresholds = LoadClassificationThresholds()
        assert classify_load(5.0, thresholds) == "Very Light"
        assert classify_load(25.0, thresholds) == "Light"
        assert classify_load(100.0, thresholds) == "Moderate"
        assert classify_load(250.0, thresholds) == "High"
        assert classify_load(600.0, thresholds) == "Very High"
        assert classify_load(1000.0, thresholds) == "Critical"

    def test_custom_thresholds(self):
        custom_thresholds = LoadClassificationThresholds(very_light_max=2.0, light_max=10.0)
        assert classify_load(5.0, custom_thresholds) == "Light"


class TestAnalyzerAndTrendSignals:
    """Tests full analysis execution and trend signal logic."""

    def test_trend_detection_increasing(self):
        # Run previous snapshot first
        prev_result = analyze_workload(LL_PREVIOUS_SNAPSHOT)  # L = 80.0 (400 req/s, 0.2s)

        # Run current workload with higher load
        curr_result = analyze_workload(LL_MS_WORKLOAD, previous=prev_result)  # L = 100.0 (500 req/s, 0.2s)

        signals = curr_result["signals"]
        assert signals["occupancy_trend"] == "increasing"
        assert signals["arrival_rate_trend"] == "increasing"
        assert signals["response_time_trend"] == "stable"

    def test_critical_workload_analysis(self):
        result = analyze_workload(LL_CRITICAL_WORKLOAD)
        assert result["load_classification"] == "Critical"
        assert result["signals"]["critical_occupancy"] is True
        assert result["signals"]["high_occupancy"] is True
        assert "Average of 1200 requests" in result["analysis_summary"]