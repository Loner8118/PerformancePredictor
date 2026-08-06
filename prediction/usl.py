from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy.optimize import curve_fit


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------

class USLValidationError(ValueError):
    """Raised when input data fails validation rules."""


class USLFitError(RuntimeError):
    """Raised when nonlinear curve fitting fails to converge."""


# --------------------------------------------------------------------------
# Data container for validated, sorted input
# --------------------------------------------------------------------------

@dataclass
class USLObservations:
    """
    Validated and sorted set of (user_level, throughput) observations.

    Attributes:
        user_levels: Sorted array of concurrent user counts (N).
        throughput:  Corresponding measured throughput values (X(N)).
    """
    user_levels: np.ndarray
    throughput: np.ndarray

    @staticmethod
    def from_raw(user_levels: Sequence[float], throughput: Sequence[float]) -> "USLObservations":
        """
        Validate raw input lists and return a clean, sorted USLObservations
        instance.

        Validation rules enforced (per functional requirements):
            1. user_levels and throughput must be the same length.
            2. At least 3 observations are required to fit two free
               parameters (sigma, kappa) without an under-determined system.
            3. No negative values are allowed (negative load or negative
               throughput is physically meaningless).
            4. No duplicate user counts are allowed (each load level must
               map to exactly one throughput measurement).
            5. Data is automatically sorted by ascending user_levels so
               downstream logic (baseline detection, monotonicity checks,
               curve search) can assume ordering.
        """
        if user_levels is None or throughput is None:
            raise USLValidationError("user_levels and throughput must both be provided.")

        if len(user_levels) != len(throughput):
            raise USLValidationError(
                f"user_levels and throughput must have the same length "
                f"(got {len(user_levels)} and {len(throughput)})."
            )

        if len(user_levels) < 3:
            raise USLValidationError(
                "At least 3 observations are required to fit the USL model "
                "(two free parameters: sigma, kappa)."
            )

        u_arr = np.asarray(user_levels, dtype=float)
        t_arr = np.asarray(throughput, dtype=float)

        if np.any(u_arr < 0) or np.any(t_arr < 0):
            raise USLValidationError("user_levels and throughput must not contain negative values.")

        if np.any(u_arr == 0):
            raise USLValidationError(
                "user_levels must not contain 0; USL is defined for N >= 1."
            )

        unique_users, counts = np.unique(u_arr, return_counts=True)
        if np.any(counts > 1):
            duplicates = unique_users[counts > 1].tolist()
            raise USLValidationError(f"Duplicate user counts found: {duplicates}")

        # Sort ascending by user level so all downstream logic can rely on order.
        sort_idx = np.argsort(u_arr)
        u_sorted = u_arr[sort_idx]
        t_sorted = t_arr[sort_idx]

        return USLObservations(user_levels=u_sorted, throughput=t_sorted)


# --------------------------------------------------------------------------
# Core USL math (pure functions, no state)
# --------------------------------------------------------------------------

def usl_capacity(n: np.ndarray, sigma: float, kappa: float) -> np.ndarray:
    """
    Compute normalized USL capacity C(N).

        C(N) = N / (1 + sigma * (N - 1) + kappa * N * (N - 1))

    Args:
        n: Load level(s), N.
        sigma: Contention coefficient.
        kappa: Coherency coefficient.

    Returns:
        Normalized capacity C(N), elementwise if n is an array.
    """
    n = np.asarray(n, dtype=float)
    denominator = 1.0 + sigma * (n - 1.0) + kappa * n * (n - 1.0)
    return n / denominator


def usl_throughput(n: np.ndarray, x1: float, sigma: float, kappa: float) -> np.ndarray:
    """
    Compute predicted throughput X(N) = X(1) * C(N).

    Args:
        n: Load level(s), N.
        x1: Baseline throughput X(1).
        sigma: Contention coefficient.
        kappa: Coherency coefficient.

    Returns:
        Predicted throughput at load level(s) n.
    """
    return x1 * usl_capacity(n, sigma, kappa)


# --------------------------------------------------------------------------
# Main USL Model
# --------------------------------------------------------------------------

class UniversalScalabilityModel:
    """
    Fits the Universal Scalability Law to empirical load-test data and
    exposes prediction, efficiency, and classification utilities.

    Typical usage:

        model = UniversalScalabilityModel()
        result = model.fit({
            "user_levels": [10, 20, 50, 100],
            "throughput": [95, 181, 390, 610],
        })
        result = model.analyze(predict_at=[200, 500, 1000])
    """

    # Thresholds used for scalability classification. These are heuristic,
    # commonly cited rules-of-thumb built on top of Gunther's USL
    # coefficients (sigma = contention loss rate, kappa = coherency loss
    # rate per additional unit of concurrency). They are intentionally
    # exposed as class attributes so they can be tuned per-deployment
    # without touching the fitting logic.
    SIGMA_EXCELLENT = 0.02
    SIGMA_GOOD = 0.05
    SIGMA_MODERATE = 0.10

    KAPPA_EXCELLENT = 1e-5
    KAPPA_GOOD = 1e-4
    KAPPA_MODERATE = 1e-3

    def __init__(self) -> None:
        self._observations: Optional[USLObservations] = None
        self.sigma: Optional[float] = None
        self.kappa: Optional[float] = None
        self.baseline_throughput: Optional[float] = None
        self._param_covariance: Optional[np.ndarray] = None
        self._fitted = False

    # ---------------------------------------------------------------- fit

    def fit(self, data: Dict[str, Sequence[float]]) -> "UniversalScalabilityModel":
        """
        Validate input, estimate baseline throughput, and fit sigma/kappa
        (and, when necessary, X(1)) via nonlinear least squares.

        Args:
            data: Dict with keys "user_levels" and "throughput".

        Returns:
            self, to allow method chaining (e.g. model.fit(data).analyze(...)).

        Raises:
            USLValidationError: If input fails validation.
            USLFitError: If curve fitting fails to converge.
        """
        obs = USLObservations.from_raw(
            data.get("user_levels"),
            data.get("throughput"),
        )
        self._observations = obs

        # ---- Step 1: determine baseline throughput X(1) ------------------
        # If N=1 was directly measured, use it as a fixed, known baseline.
        # This reduces the fit to 2 free parameters (sigma, kappa), which
        # is both more numerically stable and mathematically faithful to
        # the classic USL formulation.
        one_idx = np.where(obs.user_levels == 1)[0]

        if one_idx.size > 0:
            self.baseline_throughput = float(obs.throughput[one_idx[0]])
            self._fit_fixed_baseline(obs)
        else:
            # Otherwise, X(1) is unknown and must be estimated jointly with
            # sigma/kappa. We seed the optimizer with the per-user
            # throughput at the smallest tested load level
            # (throughput / N), which is a reasonable first-order estimate
            # of "ideal single-user" throughput before contention/coherency
            # losses are accounted for.
            self._fit_free_baseline(obs)

        self._fitted = True
        return self

    def _fit_fixed_baseline(self, obs: USLObservations) -> None:
        """Fit sigma, kappa with X(1) held fixed to the measured value."""
        x1 = self.baseline_throughput

        def _model(n: np.ndarray, sigma: float, kappa: float) -> np.ndarray:
            return usl_throughput(n, x1, sigma, kappa)

        initial_guess = (0.01, 0.0001)
        bounds = ([0.0, 0.0], [np.inf, np.inf])  # sigma, kappa physically >= 0

        try:
            popt, pcov = curve_fit(
                _model,
                obs.user_levels,
                obs.throughput,
                p0=initial_guess,
                bounds=bounds,
                maxfev=20000,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised as domain error
            raise USLFitError(f"USL curve fitting failed (fixed baseline): {exc}") from exc

        self.sigma, self.kappa = float(popt[0]), float(popt[1])
        self._param_covariance = pcov

    def _fit_free_baseline(self, obs: USLObservations) -> None:
        """Fit X(1), sigma, and kappa jointly when N=1 was not measured."""
        smallest_n = obs.user_levels[0]
        smallest_x = obs.throughput[0]
        x1_seed = smallest_x / smallest_n  # naive per-user throughput estimate

        initial_guess = (x1_seed, 0.01, 0.0001)
        bounds = ([1e-9, 0.0, 0.0], [np.inf, np.inf, np.inf])

        try:
            popt, pcov = curve_fit(
                usl_throughput,
                obs.user_levels,
                obs.throughput,
                p0=initial_guess,
                bounds=bounds,
                maxfev=20000,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised as domain error
            raise USLFitError(f"USL curve fitting failed (free baseline): {exc}") from exc

        self.baseline_throughput, self.sigma, self.kappa = (
            float(popt[0]),
            float(popt[1]),
            float(popt[2]),
        )
        self._param_covariance = pcov

    # ------------------------------------------------------------ predict

    def predict(self, user_counts: Sequence[float]) -> Dict[float, float]:
        """
        Predict throughput for arbitrary (including future/untested) user
        counts using the fitted USL curve.

        Args:
            user_counts: Iterable of N values to predict throughput for.

        Returns:
            Dict mapping each requested N to its predicted throughput.
        """
        self._require_fitted()
        n_arr = np.asarray(user_counts, dtype=float)
        if np.any(n_arr <= 0):
            raise USLValidationError("Prediction user counts must be positive (N >= 1).")

        predicted = usl_throughput(n_arr, self.baseline_throughput, self.sigma, self.kappa)
        return {float(n): float(x) for n, x in zip(n_arr, predicted)}

    def capacity(self, user_counts: Sequence[float]) -> Dict[float, float]:
        """
        Compute normalized capacity C(N) = X(N) / X(1) for given user counts.
        """
        self._require_fitted()
        n_arr = np.asarray(user_counts, dtype=float)
        cap = usl_capacity(n_arr, self.sigma, self.kappa)
        return {float(n): float(c) for n, c in zip(n_arr, cap)}

    def efficiency(self, user_counts: Sequence[float]) -> Dict[float, float]:
        """
        Compute scalability efficiency = C(N) / N.
        """

        self._require_fitted()

        n_arr = np.asarray(user_counts, dtype=float)

        if np.any(n_arr <= 0):
            raise USLValidationError(
                "Efficiency user counts must be positive."
            )

        cap = usl_capacity(
            n_arr,
            self.sigma,
            self.kappa
        )

        eff = cap / n_arr

        return {
            float(n): float(e)
            for n, e in zip(n_arr, eff)
        }


    def scalability_efficiency(self) -> float:
        """
        Return a single scalability efficiency value
        for downstream capacity analysis.
        """

        self._require_fitted()

        optimal = self.optimal_users()

        if optimal is None:
            return 1.0

        efficiency = self.efficiency([optimal])

        return round(
            list(efficiency.values())[0],
            4
        )

    def optimal_users(self) -> Optional[float]:
        """
        Compute the theoretical user count N* that maximizes throughput.

        Derivation:
            USL's throughput X(N) has a closed-form maximum at

                N* = sqrt( (1 - sigma) / kappa )

            This comes from setting dX/dN = 0 and solving for N, which is
            a well-known analytical result of the USL model (Gunther,
            2007). It only exists when kappa > 0; if kappa == 0, the model
            has no coherency-driven falloff and throughput increases
            monotonically (no finite maximum), so None is returned.

        Returns:
            The optimal user count N*, or None if the fitted model has no
            finite maximum (kappa <= 0) or the maximum is not physically
            reachable (sigma >= 1).
        """
        self._require_fitted()
        if self.kappa is None or self.kappa <= 0:
            return None
        if self.sigma >= 1:
            # sigma >= 1 implies the system is contention-bound to the
            # point of never scaling past N=1; no meaningful optimum.
            return None
        return float(np.sqrt((1.0 - self.sigma) / self.kappa))

    def peak_throughput(self) -> Optional[float]:
        """
        Throughput evaluated at the optimal user count N* (see
        optimal_users()). Returns None if no finite optimum exists.
        """
        self._require_fitted()
        n_star = self.optimal_users()
        if n_star is None:
            return None
        return float(usl_throughput(n_star, self.baseline_throughput, self.sigma, self.kappa))

    def saturation_point(self, search_upper_bound: Optional[float] = None,
        marginal_gain_threshold: float = 0.10) -> Optional[float]:
        """
        Estimate the user count at which scalability begins to flatten
        (the "saturation point").

        Method:
            The saturation point is defined here as the smallest N (beyond
            the smallest tested load level) at which the *marginal*
            throughput gain per additional user, dX/dN, has dropped to
            less than `marginal_gain_threshold` (default 10%) of the
            marginal gain observed at the smallest tested load level.

            This is a standard diminishing-returns heuristic: instead of
            waiting for throughput to actually decrease (which only
            happens if kappa > 0 and N passes N*), it flags the point
            where each additional user is contributing much less value
            than earlier users did -- i.e. where scaling benefits become
            marginal, typically *before* the theoretical peak N*.

            The derivative dX/dN is computed numerically (np.gradient)
            over a fine grid of N values from the smallest tested load up
            to `search_upper_bound` (defaults to 3x the theoretical
            optimum N* if it exists, otherwise 10x the max tested load).

        Args:
            search_upper_bound: Optional explicit upper bound for the
                search grid.
            marginal_gain_threshold: Fraction (0-1) of the initial
                marginal gain below which the curve is considered
                "saturated".

        Returns:
            Estimated saturation user count, or None if the curve never
            drops below the threshold within the search range (i.e. the
            system keeps scaling efficiently throughout the tested and
            projected range).
        """
        self._require_fitted()
        obs = self._observations

        lower = float(obs.user_levels[0])
        if search_upper_bound is None:
            n_star = self.optimal_users()
            if n_star is not None:
                upper = max(n_star * 3.0, float(obs.user_levels[-1]) * 2.0)
            else:
                upper = float(obs.user_levels[-1]) * 10.0
        else:
            upper = float(search_upper_bound)

        grid = np.linspace(lower, upper, 2000)
        curve = usl_throughput(grid, self.baseline_throughput, self.sigma, self.kappa)

        # Numerical derivative of throughput w.r.t. N across the grid.
        gradient = np.gradient(curve, grid)

        initial_gain = gradient[0]
        if initial_gain <= 0:
            # Already saturated/declining at the smallest tested point.
            return lower

        threshold = initial_gain * marginal_gain_threshold
        below_threshold = np.where(gradient <= threshold)[0]

        if below_threshold.size == 0:
            return None  # Never saturates within the search range.

        return float(grid[below_threshold[0]])

    # -------------------------------------------------------- fit quality

    def fit_quality(self) -> Dict[str, float]:
        """
        Compute goodness-of-fit statistics (R^2 and RMSE) comparing the
        fitted USL curve against the original observed data points.
        """
        self._require_fitted()
        obs = self._observations
        predicted = usl_throughput(obs.user_levels, self.baseline_throughput, self.sigma, self.kappa)

        residuals = obs.throughput - predicted
        ss_res = float(np.sum(residuals ** 2))
        ss_tot = float(np.sum((obs.throughput - np.mean(obs.throughput)) ** 2))

        # Guard against a degenerate all-identical-throughput dataset,
        # which would make ss_tot == 0 and R^2 undefined.
        r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 1.0
        rmse = float(np.sqrt(np.mean(residuals ** 2)))

        return {"r2": float(r2), "rmse": rmse}

    # -------------------------------------------------------- classification

    def classify(self) -> str:
        """
        Classify overall scalability behavior based on the fitted sigma
        and kappa values.

        Classification rules (heuristic, documented thresholds):
            - "Negative Scalability":
                kappa > 0 and the theoretical optimum N* falls at or
                before the largest *tested* load level -- meaning the
                system is already past its peak and throughput is
                measurably declining within the observed range.
            - "Excellent Scalability":
                sigma <= SIGMA_EXCELLENT and kappa <= KAPPA_EXCELLENT
                (near-linear scaling; negligible contention/coherency loss)
            - "Good Scalability":
                sigma <= SIGMA_GOOD and kappa <= KAPPA_GOOD
            - "Moderate Scalability":
                sigma <= SIGMA_MODERATE and kappa <= KAPPA_MODERATE
            - "Poor Scalability":
                anything worse than "Moderate" but not yet retrograde
                within the tested range.

        Returns:
            One of: "Excellent Scalability", "Good Scalability",
            "Moderate Scalability", "Poor Scalability",
            "Negative Scalability".
        """
        self._require_fitted()
        obs = self._observations
        sigma, kappa = self.sigma, self.kappa

        n_star = self.optimal_users()
        max_tested_n = float(obs.user_levels[-1])

        if kappa is not None and kappa > 0 and n_star is not None and n_star <= max_tested_n:
            return "Negative Scalability"

        if sigma <= self.SIGMA_EXCELLENT and kappa <= self.KAPPA_EXCELLENT:
            return "Excellent Scalability"
        if sigma <= self.SIGMA_GOOD and kappa <= self.KAPPA_GOOD:
            return "Good Scalability"
        if sigma <= self.SIGMA_MODERATE and kappa <= self.KAPPA_MODERATE:
            return "Moderate Scalability"

        return "Poor Scalability"

    # ------------------------------------------------------------- helpers

    def _require_fitted(self) -> None:
        if not self._fitted:
            raise USLFitError("Model has not been fit yet. Call fit() before this operation.")

    # ------------------------------------------------------------- report

    def analyze(self, predict_at: Optional[Sequence[float]] = None) -> Dict:
        """
        Produce the full structured USL analysis output.

        Args:
            predict_at: Optional list of future/untested user counts to
                generate predictions for. If omitted, predictions are
                generated for the originally observed user levels plus a
                default extrapolation set (2x and 5x the max tested load).

        Returns:
            Structured dictionary matching the module's documented output
            contract.
        """
        self._require_fitted()
        obs = self._observations

        if predict_at is None:
            max_tested = float(obs.user_levels[-1])
            predict_at = sorted(set(obs.user_levels.tolist()) | {max_tested * 2, max_tested * 5})

        predicted_curve = self.predict(predict_at)
        capacity = self.capacity(predict_at)
        efficiency = self.efficiency(predict_at)

        return {
            "sigma": self.sigma,
            "kappa": self.kappa,
            "baseline_throughput": self.baseline_throughput,

            "capacity": capacity,
            "predicted_curve": predicted_curve,

            "peak_throughput": self.peak_throughput(),
            "optimal_users": self.optimal_users(),
            "saturation_point": self.saturation_point(),

            "scalability_efficiency": self.scalability_efficiency(),

            "efficiency_curve": efficiency,

            "classification": self.classify(),

            "fit_quality": self.fit_quality(),
        }


# --------------------------------------------------------------------------
# Convenience functional wrapper (for simple/one-shot usage)
# --------------------------------------------------------------------------

def run_usl_analysis(
    data: Dict[str, Sequence[float]],
    predict_at: Optional[Sequence[float]] = None,
) -> Dict:
    """
    One-shot convenience function: fit a USL model to `data` and return the
    full structured analysis, optionally predicting at custom user counts.

    This is the primary entry point intended for use by the Flask layer
    (e.g. a `/api/usl/analyze` endpoint would call this directly).

    Args:
        data: Dict with "user_levels" and "throughput" keys.
        predict_at: Optional custom list of user counts to predict.

    Returns:
        Structured USL analysis dictionary.
    """
    model = UniversalScalabilityModel()
    model.fit(data)
    return model.analyze(predict_at=predict_at)


# --------------------------------------------------------------------------
# Example usage / smoke test
# --------------------------------------------------------------------------

if __name__ == "__main__":
    sample_data = {
        "user_levels": [10, 20, 50, 100],
        "throughput": [95, 181, 390, 610],
    }

    result = run_usl_analysis(sample_data, predict_at=[200, 500, 1000])

    print("=== USL Fit Parameters ===")
    print(f"sigma (contention): {result['sigma']:.6f}")
    print(f"kappa (coherency):  {result['kappa']:.8f}")
    print(f"baseline X(1):      {result['baseline_throughput']:.4f}")

    print("\n=== Fit Quality ===")
    print(f"R^2:  {result['fit_quality']['r2']:.4f}")
    print(f"RMSE: {result['fit_quality']['rmse']:.4f}")

    print("\n=== Predicted Throughput ===")
    for n, x in sorted(result["predicted_curve"].items()):
        print(f"N={n:>7.1f} -> X(N)={x:>10.2f}")

    print("\n=== Normalized Capacity ===")
    for n, c in sorted(result["capacity"].items()):
        print(f"N={n:>7.1f} -> C(N)={c:>10.4f}")

    print("\n=== Scalability Efficiency ===")
    for n, e in sorted(result["efficiency"].items()):
        print(f"N={n:>7.1f} -> Efficiency={e:>8.5f}")

    print("\n=== Key Scalability Points ===")
    print(f"Peak throughput:    {result['peak_throughput']}")
    print(f"Optimal user count: {result['optimal_users']}")
    print(f"Saturation point:   {result['saturation_point']}")

    print("\n=== Classification ===")
    print(result["classification"])


