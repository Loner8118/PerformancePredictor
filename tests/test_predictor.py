"""
test_predictor.py
=================
Unit and integration tests for prediction/predictor.py.
"""

import os
import sys

import pytest

# Add project root directory to Python path
sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
)

from prediction.predictor import analyze_system

from sample_data.sample_metrics import (
    PREDICTOR_VALID_METRICS,
    PREDICTOR_MISSING_QUEUE,
    PREDICTOR_MISSING_RUNTIME_KEY,
    PREDICTOR_INVALID_LENGTH,
    PREDICTOR_NOT_DICT,
)


class TestPredictorValidation:
    """Tests orchestration layer validation."""

    def test_metrics_must_be_dictionary(self):
        result = analyze_system(PREDICTOR_NOT_DICT)

        assert result["status"] == "failed"
        assert result["failed_module"] == "validation"

    def test_missing_top_level_section(self):
        result = analyze_system(PREDICTOR_MISSING_QUEUE)

        assert result["status"] == "failed"
        assert result["failed_module"] == "validation"

    def test_missing_runtime_key(self):
        result = analyze_system(PREDICTOR_MISSING_RUNTIME_KEY)

        assert result["status"] == "failed"
        assert result["failed_module"] == "validation"

    def test_invalid_user_level_lengths(self):
        result = analyze_system(PREDICTOR_INVALID_LENGTH)

        assert result["status"] == "failed"
        assert result["failed_module"] == "validation"


class TestPipelineExecution:
    """Tests complete mathematical pipeline."""

    def test_complete_pipeline(self):
        result = analyze_system(PREDICTOR_VALID_METRICS)

        assert result["status"] == "success"

        assert "usl" in result
        assert "little_law" in result
        assert "queueing" in result
        assert "capacity" in result

    def test_metadata_exists(self):
        result = analyze_system(PREDICTOR_VALID_METRICS)

        metadata = result["metadata"]

        assert metadata["analysis_time_ms"] >= 0

        assert metadata["usl_time_ms"] >= 0
        assert metadata["little_law_time_ms"] >= 0
        assert metadata["queue_time_ms"] >= 0
        assert metadata["capacity_time_ms"] >= 0

        assert metadata["version"] == "1.0"
        assert metadata["engine"] == "Mathematical Prediction Engine"

    def test_timestamp_generated(self):
        result = analyze_system(PREDICTOR_VALID_METRICS)

        assert "timestamp" in result["metadata"]

    def test_return_structure(self):
        result = analyze_system(PREDICTOR_VALID_METRICS)

        assert isinstance(result, dict)

        expected = {
            "status",
            "usl",
            "little_law",
            "queueing",
            "capacity",
            "metadata",
        }

        assert expected.issubset(result.keys())