"""
test_usl.py
===========
Unit and integration tests for prediction/usl.py using pytest.
"""

import sys
import os
import pytest

# Add project root directory to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from prediction.usl import (
    UniversalScalabilityModel,
    USLValidationError,
    USLFitError,
    run_usl_analysis
)

# Import sample metrics from sample_data folder
from sample_data.sample_metrics import (
    GOOD_SCALING_DATA,
    CONTENTION_HEAVY_DATA,
    RETROGRADE_SCALING_DATA,
    TOO_FEW_POINTS_DATA,
    MISMATCHED_LENGTH_DATA,
    NEGATIVE_VALUES_DATA
)


class TestUSLValidation:
    """Tests input data validation rules."""

    def test_mismatched_lengths_raises_error(self):
        with pytest.raises(USLValidationError, match="same length"):
            run_usl_analysis(MISMATCHED_LENGTH_DATA)

    def test_insufficient_points_raises_error(self):
        with pytest.raises(USLValidationError, match="At least 3 observations"):
            run_usl_analysis(TOO_FEW_POINTS_DATA)

    def test_negative_values_raises_error(self):
        with pytest.raises(USLValidationError, match="negative values"):
            run_usl_analysis(NEGATIVE_VALUES_DATA)


class TestUSLFittingAndPredictions:
    """Tests curve fitting, metrics generation, and extrapolations."""

    def test_good_scaling_fit(self):
        result = run_usl_analysis(GOOD_SCALING_DATA, predict_at=[300, 500])
        
        # Check fit quality
        assert result["fit_quality"]["r2"] > 0.95
        assert result["sigma"] < 0.05  # Low contention
        
        # Check predictions exist
        assert 300.0 in result["predicted_curve"]
        assert 500.0 in result["predicted_curve"]
        assert result["predicted_curve"][500.0] > result["predicted_curve"][300.0]

    def test_contention_heavy_scaling(self):
        model = UniversalScalabilityModel()
        model.fit(CONTENTION_HEAVY_DATA)
        analysis = model.analyze()

        # Contention (sigma) should be clearly elevated compared to good scaling
        assert analysis["sigma"] > 0.01
        assert analysis["saturation_point"] is not None

    def test_retrograde_scaling_detection(self):
        result = run_usl_analysis(RETROGRADE_SCALING_DATA)

        # High kappa should trigger negative scalability classification
        assert result["kappa"] > 0
        assert result["optimal_users"] is not None
        assert result["classification"] == "Negative Scalability"


def test_standalone_fit_then_analyze_workflow():
    """Tests class-based method chaining workflow."""
    model = UniversalScalabilityModel()
    model.fit(GOOD_SCALING_DATA)
    
    assert model._fitted is True
    
    predictions = model.predict([100, 200])
    assert len(predictions) == 2
    assert predictions[100] > 0