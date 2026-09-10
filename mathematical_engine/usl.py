from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

import numpy as np
from scipy.optimize import curve_fit


class USLValidationError(ValueError):
    """Bad input data."""


class USLFitError(RuntimeError):
    """curve_fit didn't converge."""


@dataclass
class USLObservations:
    """Validated, sorted (N, throughput) pairs."""

    user_levels: np.ndarray
    throughput: np.ndarray

    @staticmethod
    def from_raw(user_levels: Sequence[float], throughput: Sequence[float]) -> "USLObservations":
        if user_levels is None or throughput is None:
            raise USLValidationError("user_levels and throughput must both be provided.")

        if len(user_levels) != len(throughput):
            raise USLValidationError(
                f"user_levels and throughput must have the same length "
                f"(got {len(user_levels)} and {len(throughput)})."
            )

        # sigma+kappa fit needs 3 points minimum. If N=1 isn't in the data
        # we're also fitting X(1), which needs a 4th - checked later in
        # fit() once we know whether N=1 was actually measured.
        if len(user_levels) < 3:
            raise USLValidationError(
                "At least 3 observations are required to fit the USL model."
            )

        u_arr = np.asarray(user_levels, dtype=float)
        t_arr = np.asarray(throughput, dtype=float)

        if not np.all(np.isfinite(u_arr)):
            raise USLValidationError("user_levels must contain only finite numeric values.")

        if not np.all(np.isfinite(t_arr)):
            raise USLValidationError("throughput must contain only finite numeric values.")

        if np.any(u_arr <= 0):
            raise USLValidationError("user_levels must contain only positive values.")

        if np.any(t_arr <= 0):
            raise USLValidationError("throughput must contain only positive values.")

        if np.ptp(u_arr) == 0:
            raise USLValidationError("User levels must contain more than one distinct value.")

        if np.ptp(t_arr) == 0:
            raise USLValidationError("Throughput values must contain variation for USL fitting.")

        unique_users, counts = np.unique(u_arr, return_counts=True)
        if np.any(counts > 1):
            raise USLValidationError(f"Duplicate user counts found: {unique_users[counts > 1].tolist()}")

        sort_idx = np.argsort(u_arr)
        return USLObservations(user_levels=u_arr[sort_idx], throughput=t_arr[sort_idx])


def usl_capacity(n: np.ndarray, sigma: float, kappa: float) -> np.ndarray:
    """C(N) = N / (1 + sigma*(N-1) + kappa*N*(N-1))"""
    n = np.asarray(n, dtype=float)
    denominator = 1.0 + sigma * (n - 1.0) + kappa * n * (n - 1.0)

    if np.any(~np.isfinite(denominator)):
        raise USLValidationError("USL denominator produced a non-finite value.")
    if np.any(denominator <= 0):
        raise USLValidationError("USL denominator must remain positive.")

    return n / denominator


def usl_throughput(n: np.ndarray, x1: float, sigma: float, kappa: float) -> np.ndarray:
    """X(N) = X(1) * C(N)"""
    return x1 * usl_capacity(n, sigma, kappa)


class UniversalScalabilityModel:
    """
    USL math engine. Fits sigma/kappa (and X(1) if needed) to load-test
    data and exposes prediction + the standard USL derived quantities
    (capacity, efficiency, optimal N, peak throughput, saturation point,
    fit quality), plus two additions that matter a lot when you're
    extrapolating 10-20x past the tested range:

      - predict_with_confidence(): propagates the curve_fit covariance
        matrix through Monte Carlo sampling so predictions come with an
        honest interval instead of a single misleadingly-precise number.
      - loo_cv_r2(): leave-one-out cross-validated R^2, which is a much
        better generalization estimate than in-sample R^2 when you only
        have a handful of tested load levels.

    Interpretation of these numbers (is this "good" scalability, what
    should we recommend, etc.) is deliberately not this class's job -
    that lives in scalability.py / recommendation.py. This file just
    answers "what does the model say, and how much should you trust it".
    """

    def __init__(self) -> None:
        self._observations: Optional[USLObservations] = None
        self.sigma: Optional[float] = None
        self.kappa: Optional[float] = None
        self.baseline_throughput: Optional[float] = None
        self.baseline_source: Optional[str] = None  # "measured" or "estimated"
        self._param_covariance: Optional[np.ndarray] = None
        self._fitted = False

    # ---------------------------------------------------------------- fit

    def fit(self, data: Dict[str, Sequence[float]]) -> "UniversalScalabilityModel":
        obs = USLObservations.from_raw(data.get("user_levels"), data.get("throughput"))
        self._observations = obs

        one_idx = np.where(obs.user_levels == 1)[0]
        if one_idx.size > 0:
            # N=1 was actually measured, use it directly and fit sigma/kappa
            # only - more stable than a 3-parameter fit.
            self.baseline_throughput = float(obs.throughput[one_idx[0]])
            self.baseline_source = "measured"
            self._fit_fixed_baseline(obs)
        else:
            # No single-user baseline in the data (e.g. Locust runs starting
            # at 20 users). X(1) has to be estimated jointly with sigma/kappa,
            # which needs a 4th observation to not be underdetermined.
            if len(obs.user_levels) < 4:
                raise USLValidationError(
                    "At least 4 observations are required when N=1 is not measured, "
                    "because X(1), sigma, and kappa must all be estimated."
                )
            self.baseline_source = "estimated"
            self._fit_free_baseline(obs)

        self._fitted = True
        return self

    def _fit_fixed_baseline(self, obs: USLObservations) -> None:
        x1 = self.baseline_throughput

        def _model(n, sigma, kappa):
            return usl_throughput(n, x1, sigma, kappa)

        try:
            popt, pcov = curve_fit(
                _model,
                obs.user_levels,
                obs.throughput,
                p0=(0.01, 0.0001),
                bounds=([0.0, 0.0], [np.inf, np.inf]),
                maxfev=20000,
            )
        except Exception as exc:
            raise USLFitError(f"USL curve fitting failed (fixed baseline): {exc}") from exc

        self.sigma, self.kappa = float(popt[0]), float(popt[1])
        self._param_covariance = pcov

    def _fit_free_baseline(self, obs: USLObservations) -> None:
        # seed X(1) with the naive per-user throughput at the lowest tested
        # load - not accurate but a reasonable starting point for the solver
        x1_seed = obs.throughput[0] / obs.user_levels[0]

        try:
            popt, pcov = curve_fit(
                usl_throughput,
                obs.user_levels,
                obs.throughput,
                p0=(x1_seed, 0.01, 0.0001),
                bounds=([1e-9, 0.0, 0.0], [np.inf, np.inf, np.inf]),
                maxfev=20000,
            )
        except Exception as exc:
            raise USLFitError(f"USL curve fitting failed (free baseline): {exc}") from exc

        self.baseline_throughput, self.sigma, self.kappa = (
            float(popt[0]),
            float(popt[1]),
            float(popt[2]),
        )
        self._param_covariance = pcov

    # ------------------------------------------------------------ predict

    def predict(self, user_counts: Sequence[float]) -> Dict[float, float]:
        self._require_fitted()
        n_arr = np.asarray(user_counts, dtype=float)
        if not np.all(np.isfinite(n_arr)):
            raise USLValidationError("Prediction user counts must be finite.")
        if np.any(n_arr <= 0):
            raise USLValidationError("Prediction user counts must be positive (N >= 1).")

        predicted = usl_throughput(n_arr, self.baseline_throughput, self.sigma, self.kappa)
        return {float(n): float(x) for n, x in zip(n_arr, predicted)}

    def predict_with_confidence(
        self,
        user_counts: Sequence[float],
        confidence_level: float = 0.95,
        n_samples: int = 2000,
        random_seed: Optional[int] = 42,
    ) -> Dict[float, Dict[str, Any]]:
        """
        Same as predict(), but propagates parameter uncertainty (from the
        curve_fit covariance matrix) through Monte Carlo sampling to attach
        a confidence interval to every predicted throughput value. Turns

            "5000 users -> 87.9 req/s"

        into the more honest

            "5000 users -> 87.9 req/s (95% CI: 71.2 - 104.8)"

        which matters a lot once you're predicting several multiples past
        the highest tested load level.

        random_seed is fixed by default so re-running the pipeline on the
        same fitted model gives the same interval - pass None for a fresh
        random draw each call.
        """
        self._require_fitted()
        n_arr = np.asarray(user_counts, dtype=float)
        if not np.all(np.isfinite(n_arr)):
            raise USLValidationError("Prediction user counts must be finite.")
        if np.any(n_arr <= 0):
            raise USLValidationError("Prediction user counts must be positive (N >= 1).")
        if not 0 < confidence_level < 1:
            raise USLValidationError("confidence_level must be between 0 and 1.")

        point = usl_throughput(n_arr, self.baseline_throughput, self.sigma, self.kappa)
        no_interval = self._param_covariance is None
        if not no_interval:
            diag = np.diag(self._param_covariance)
            no_interval = bool(np.any(~np.isfinite(diag)) or np.any(diag < 0))

        if no_interval:
            return {
                float(n): {
                    "point_estimate": float(p),
                    "lower": None,
                    "upper": None,
                    "confidence_level": confidence_level,
                    "confidence_interval_available": False,
                }
                for n, p in zip(n_arr, point)
            }

        rng = np.random.default_rng(random_seed)
        cov = self._param_covariance

        if self.baseline_source == "measured":
            mean = np.array([self.sigma, self.kappa])
            samples = np.clip(rng.multivariate_normal(mean, cov, size=n_samples), 0.0, None)
            sigma_s, kappa_s = samples[:, 0], samples[:, 1]
            x1_s = np.full(n_samples, self.baseline_throughput)
        else:
            mean = np.array([self.baseline_throughput, self.sigma, self.kappa])
            samples = rng.multivariate_normal(mean, cov, size=n_samples)
            samples[:, 0] = np.clip(samples[:, 0], 1e-9, None)  # X(1) > 0
            samples[:, 1:] = np.clip(samples[:, 1:], 0.0, None)  # sigma, kappa >= 0
            x1_s, sigma_s, kappa_s = samples[:, 0], samples[:, 1], samples[:, 2]

        lower_pct = (1 - confidence_level) / 2 * 100
        upper_pct = (1 + confidence_level) / 2 * 100

        results: Dict[float, Dict[str, Any]] = {}
        for n, p in zip(n_arr, point):
            denom = 1.0 + sigma_s * (n - 1.0) + kappa_s * n * (n - 1.0)
            valid = denom > 0
            sampled = np.where(valid, x1_s * n / np.where(valid, denom, 1.0), np.nan)
            sampled = sampled[np.isfinite(sampled)]

            # If parameter uncertainty is so large that most Monte Carlo
            # draws produce a degenerate (non-finite) prediction, the
            # interval itself isn't trustworthy - say so instead of
            # reporting a percentile computed from a handful of samples.
            if sampled.size < n_samples * 0.5:
                results[float(n)] = {
                    "point_estimate": float(p),
                    "lower": None,
                    "upper": None,
                    "confidence_level": confidence_level,
                    "confidence_interval_available": False,
                }
                continue

            results[float(n)] = {
                "point_estimate": float(p),
                "lower": float(np.percentile(sampled, lower_pct)),
                "upper": float(np.percentile(sampled, upper_pct)),
                "confidence_level": confidence_level,
                "confidence_interval_available": True,
            }

        return results

    def capacity(self, user_counts: Sequence[float]) -> Dict[float, float]:
        self._require_fitted()
        n_arr = np.asarray(user_counts, dtype=float)
        if np.any(n_arr <= 0):
            raise USLValidationError("Capacity user counts must be positive.")
        if not np.all(np.isfinite(n_arr)):
            raise USLValidationError("Capacity user counts must be finite.")

        cap = usl_capacity(n_arr, self.sigma, self.kappa)
        return {float(n): float(c) for n, c in zip(n_arr, cap)}

    def efficiency(self, user_counts: Sequence[float]) -> Dict[float, float]:
        """Efficiency = C(N) / N, i.e. how close to ideal linear scaling."""
        self._require_fitted()
        n_arr = np.asarray(user_counts, dtype=float)
        if not np.all(np.isfinite(n_arr)):
            raise USLValidationError("Efficiency user counts must be finite.")
        if np.any(n_arr <= 0):
            raise USLValidationError("Efficiency user counts must be positive.")

        cap = usl_capacity(n_arr, self.sigma, self.kappa)
        eff = cap / n_arr
        return {float(n): float(e) for n, e in zip(n_arr, eff)}

    def optimal_users(self) -> Optional[float]:
        """
        N* = sqrt((1 - sigma) / kappa), the closed-form throughput maximum
        from Gunther's USL. Only exists when kappa > 0 and sigma < 1.
        """
        self._require_fitted()
        if self.kappa is None or self.kappa <= 0:
            return None
        if self.sigma >= 1:
            return None
        n_star = float(np.sqrt((1.0 - self.sigma) / self.kappa))
        return max(1.0, n_star)

    def peak_throughput(self) -> Optional[float]:
        self._require_fitted()
        n_star = self.optimal_users()
        if n_star is None:
            return None
        return float(usl_throughput(n_star, self.baseline_throughput, self.sigma, self.kappa))

    def saturation_point(
        self,
        search_upper_bound: Optional[float] = None,
        marginal_gain_threshold: float = 0.10,
    ) -> Optional[float]:
        """
        Smallest N where dX/dN has dropped below marginal_gain_threshold
        (default 10%) of the marginal gain at the lowest tested N. This
        usually shows up before the theoretical peak N* - it's flagging
        "diminishing returns", not "throughput actually falling".
        """
        self._require_fitted()
        obs = self._observations

        lower = float(obs.user_levels[0])

        if not 0 < marginal_gain_threshold < 1:
            raise USLValidationError("marginal_gain_threshold must be between 0 and 1.")

        if search_upper_bound is not None:
            if not np.isfinite(search_upper_bound):
                raise USLValidationError("search_upper_bound must be finite.")
            if search_upper_bound <= lower:
                raise USLValidationError(
                    "search_upper_bound must be greater than the minimum tested user level."
                )

        if search_upper_bound is None:
            n_star = self.optimal_users()
            upper = (
                max(n_star * 3.0, float(obs.user_levels[-1]) * 2.0)
                if n_star is not None
                else float(obs.user_levels[-1]) * 10.0
            )
        else:
            upper = float(search_upper_bound)

        grid = np.linspace(lower, upper, 2000)
        curve = usl_throughput(grid, self.baseline_throughput, self.sigma, self.kappa)
        gradient = np.gradient(curve, grid)

        initial_gain = gradient[0]
        if initial_gain <= 0:
            return lower

        threshold = initial_gain * marginal_gain_threshold
        below_threshold = np.where(gradient <= threshold)[0]

        if below_threshold.size == 0:
            return None

        return float(grid[below_threshold[0]])

    # -------------------------------------------------------- fit quality

    def fit_quality(self) -> Dict[str, Any]:
        self._require_fitted()
        obs = self._observations
        predicted = usl_throughput(obs.user_levels, self.baseline_throughput, self.sigma, self.kappa)

        residuals = obs.throughput - predicted
        ss_res = float(np.sum(residuals ** 2))
        ss_tot = float(np.sum((obs.throughput - np.mean(obs.throughput)) ** 2))
        r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 1.0
        rmse = float(np.sqrt(np.mean(residuals ** 2)))

        return {
            "r2": float(r2),
            "rmse": rmse,
            "observations": int(len(obs.user_levels)),
            "residuals": {float(n): float(r) for n, r in zip(obs.user_levels, residuals)},
        }

    def loo_cv_r2(self) -> Dict[str, Any]:
        """
        Leave-one-out cross-validated R^2 - refits the model once per
        tested point with that point held out, and scores how well each
        refit predicts the point it never saw. In-sample R^2 looks great
        almost by construction when you have 2-3 parameters and only 5-7
        data points; this is the more honest generalization estimate, and
        it's what prediction_reliability() actually weighs most heavily.
        """
        self._require_fitted()
        obs = self._observations
        n_obs = len(obs.user_levels)
        n_params = 2 if self.baseline_source == "measured" else 3
        # Leave one out for each fold, and still want >=2 residual degrees
        # of freedom in the held-out fit, or the fold's fit is just as
        # underdetermined as the fold it's supposed to validate.
        min_required = n_params + 3

        if n_obs < min_required:
            return {
                "available": False,
                "reason": (
                    f"At least {min_required} observations are needed for leave-one-out "
                    f"cross-validation with this model (have {n_obs}); in-sample R^2 is "
                    f"the only fit-quality signal available."
                ),
            }

        predicted_holdout, actual_holdout, failed = [], [], 0

        for i in range(n_obs):
            mask = np.ones(n_obs, dtype=bool)
            mask[i] = False
            held_out_n = float(obs.user_levels[i])
            held_out_x = float(obs.throughput[i])

            try:
                fold_model = UniversalScalabilityModel()
                fold_model.fit({
                    "user_levels": obs.user_levels[mask].tolist(),
                    "throughput": obs.throughput[mask].tolist(),
                })
                predicted_holdout.append(fold_model.predict([held_out_n])[held_out_n])
                actual_holdout.append(held_out_x)
            except Exception:
                # A fold failing to converge is itself informative (the
                # fit is sensitive to which points are included) but
                # doesn't block scoring the folds that did work.
                failed += 1

        if len(actual_holdout) < 2:
            return {
                "available": False,
                "reason": "Too many leave-one-out folds failed to converge to compute a cross-validated score.",
                "folds_failed": failed,
            }

        actual_arr = np.array(actual_holdout)
        pred_arr = np.array(predicted_holdout)
        ss_res = float(np.sum((actual_arr - pred_arr) ** 2))
        ss_tot = float(np.sum((actual_arr - np.mean(actual_arr)) ** 2))
        loo_r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 1.0

        return {
            "available": True,
            "loo_r2": float(loo_r2),
            "folds_used": len(actual_holdout),
            "folds_failed": failed,
        }

    def observed_predictions(self) -> Dict[float, Dict[str, float]]:
        """
        Measured throughput vs. what the fitted curve predicts, at every
        tested load level. E.g. "at 100 users we measured 121.43 req/s,
        the model says X" - this is what the report/UI wants to show.
        """
        self._require_fitted()
        obs = self._observations
        predicted = usl_throughput(obs.user_levels, self.baseline_throughput, self.sigma, self.kappa)

        return {
            float(n): {
                "observed": float(actual),
                "predicted": float(pred),
                "residual": float(actual - pred),
            }
            for n, actual, pred in zip(obs.user_levels, obs.throughput, predicted)
        }

    def parameter_uncertainty(self) -> Optional[Dict[str, float]]:
        """
        Std error on sigma/kappa (and X(1) if it was fitted) from the
        covariance matrix curve_fit returns. None if covariance couldn't
        be estimated (curve_fit sometimes returns inf on a bad fit).
        """
        self._require_fitted()
        if self._param_covariance is None:
            return None

        diag = np.diag(self._param_covariance)
        if np.any(~np.isfinite(diag)) or np.any(diag < 0):
            return None

        std_errors = np.sqrt(diag)

        if self.baseline_source == "measured":
            return {"sigma_stderr": float(std_errors[0]), "kappa_stderr": float(std_errors[1])}
        return {
            "x1_stderr": float(std_errors[0]),
            "sigma_stderr": float(std_errors[1]),
            "kappa_stderr": float(std_errors[2]),
        }

    def kappa_relative_uncertainty(self) -> Optional[float]:
        """
        kappa's standard error as a fraction of kappa itself. High values
        (>50%) mean kappa is weakly identified by the tested data - this
        is common when every tested load level is still on the rising
        part of the throughput curve, since kappa's effect only becomes
        visible near and past the peak.
        """
        self._require_fitted()
        if not self.kappa:  # None or 0.0 - relative uncertainty undefined
            return None
        unc = self.parameter_uncertainty()
        if unc is None:
            return None
        return float(unc["kappa_stderr"] / self.kappa)

    def peak_observed_in_tested_range(self) -> Optional[bool]:
        """
        Whether the predicted optimal-users point N* actually falls inside
        the tested user range. If it's beyond the highest tested load
        level, the saturation/peak prediction is itself an extrapolation
        on top of the throughput extrapolation - worth flagging separately
        since it specifically affects kappa-derived outputs (optimal_users,
        peak_throughput, saturation_point), not the throughput curve as a
        whole.

        Returns None if kappa is effectively zero (no finite peak is
        predicted at all - not a data problem, just a different scaling
        shape, see scalability_classification()).
        """
        self._require_fitted()
        n_star = self.optimal_users()
        if n_star is None:
            return None
        obs = self._observations
        return bool(obs.user_levels[0] <= n_star <= obs.user_levels[-1])

    def scalability_classification(self) -> Dict[str, str]:
        """Plain-language shape of the fitted curve, based on sigma/kappa."""
        self._require_fitted()
        sigma, kappa = self.sigma, self.kappa

        if sigma <= 1e-6 and kappa <= 1e-9:
            return {
                "classification": "Linear",
                "description": (
                    "No measurable contention or coherency overhead detected; throughput "
                    "grows close to linearly with concurrency across the tested range."
                ),
            }
        if kappa <= 1e-9:
            return {
                "classification": "Sublinear (contention-limited)",
                "description": (
                    "Throughput growth slows as concurrency increases due to contention "
                    "(sigma > 0), but no coherency/coordination overhead was detected, so "
                    "the model does not predict a decline at higher concurrency."
                ),
            }
        return {
            "classification": "Retrograde (coherency-limited)",
            "description": (
                "Throughput growth slows and is predicted to decline beyond the optimal "
                "concurrency point due to coordination/coherency overhead (kappa > 0)."
            ),
        }

    def prediction_reliability(self) -> Dict[str, Any]:
        """
        Whether the fit is good enough to trust extrapolated predictions -
        and, separately, whether the kappa-derived optimal-users/peak/
        saturation numbers specifically are trustworthy, since those can
        be unreliable even when the throughput curve itself fits well.

        Deliberately conservative: this is what should stop capacity.py /
        scalability.py from leaning hard on a 5000-user prediction off a
        curve fit to 6 points below 500 users.
        """
        self._require_fitted()
        r2 = self.fit_quality()["r2"]
        observations = len(self._observations.user_levels)
        loo = self.loo_cv_r2()
        peak_in_range = self.peak_observed_in_tested_range()
        kappa_rel_unc = self.kappa_relative_uncertainty()
        notes: list[str] = []

        if r2 >= 0.95:
            in_sample_status = "high"
        elif r2 >= 0.85:
            in_sample_status = "moderate"
        else:
            in_sample_status = "low"

        if loo.get("available"):
            loo_r2 = loo["loo_r2"]
            if loo_r2 < 0.70:
                status = "low"
                notes.append(
                    f"Leave-one-out cross-validated R^2 is {loo_r2:.2f}, well below the "
                    f"in-sample R^2 of {r2:.2f} - the fit may not generalize past the "
                    f"tested range."
                )
            elif loo_r2 < 0.85:
                status = "low" if in_sample_status == "low" else "moderate"
            else:
                status = in_sample_status
        else:
            status = in_sample_status
            notes.append(loo.get("reason", "Cross-validation unavailable."))

        usable_for_extrapolation = (
            status != "low"
            and observations >= 5
            and (loo["loo_r2"] >= 0.70 if loo.get("available") else r2 >= 0.85)
        )

        saturation_point_reliable = usable_for_extrapolation
        if peak_in_range is False:
            saturation_point_reliable = False
            notes.append(
                "The predicted optimal-users/saturation point falls outside the tested "
                "user range - kappa is itself extrapolated here, so treat this specific "
                "prediction as directional rather than precise."
            )
        elif peak_in_range is None:
            notes.append(
                "No finite optimal-users point is predicted (kappa is effectively zero) "
                "- the model currently shows no coherency-driven decline within the "
                "tested range."
            )

        if kappa_rel_unc is not None and kappa_rel_unc > 0.5:
            saturation_point_reliable = False
            notes.append(
                f"kappa's standard error is {kappa_rel_unc:.0%} of its fitted value - "
                f"the coherency parameter is weakly identified from the current data, "
                f"most often because no tested load level is past the throughput peak."
            )

        return {
            "status": status,
            "r2": r2,
            "loo_cv": loo,
            "usable_for_extrapolation": usable_for_extrapolation,
            "saturation_point_reliable": saturation_point_reliable,
            "peak_observed_in_tested_range": peak_in_range,
            "kappa_relative_uncertainty": kappa_rel_unc,
            "notes": notes,
        }

    def fit_quality_status(self) -> str:
        self._require_fitted()
        r2 = self.fit_quality().get("r2")

        if r2 is None or not np.isfinite(r2):
            return "Unknown"
        if r2 >= 0.95:
            return "Excellent"
        if r2 >= 0.90:
            return "Good"
        if r2 >= 0.75:
            return "Moderate"
        return "Poor"

    def parameter_interpretation(self) -> Dict[str, str]:
        self._require_fitted()
        if self.sigma is None or self.kappa is None:
            return {}

        return {
            "sigma": "Contention overhead caused by competition for shared resources.",
            "kappa": "Coherency overhead caused by coordination and synchronization.",
        }

    # ------------------------------------------------------------- helpers

    def _require_fitted(self) -> None:
        if not self._fitted:
            raise USLFitError("Model has not been fit yet. Call fit() before this operation.")

    def is_extrapolated(self, n: float) -> bool:
        """True if N falls outside the range of the data the model was fit on."""
        self._require_fitted()
        obs = self._observations
        return n < obs.user_levels[0] or n > obs.user_levels[-1]

    # ------------------------------------------------------------- report

    def analyze(self, predict_at: Optional[Sequence[float]] = None) -> Dict[str, Any]:
        self._require_fitted()
        obs = self._observations
        observations = len(obs.user_levels)

        if predict_at is None:
            max_tested = float(obs.user_levels[-1])
            predict_at = sorted(set(obs.user_levels.tolist()) | {max_tested * 2, max_tested * 5})

        predictions = self.predict(predict_at)
        extrapolated = [n for n in predictions if self.is_extrapolated(n)]

        return {
            "model": "USL",
            "parameters": {
                "baseline_throughput": self.baseline_throughput,
                "sigma": self.sigma,
                "kappa": self.kappa,
                "baseline_source": self.baseline_source,
            },
            "predictions": predictions,
            "predictions_with_confidence": self.predict_with_confidence(predict_at),
            "observations": observations,
            "extrapolated_points": extrapolated,
            "extrapolation_warning": (
                "Predictions beyond the maximum tested user level are "
                "model-based extrapolations and should be interpreted with caution."
            ) if extrapolated else None,
            "usl_metrics": {
                # N* is the core USL result - closed form off sigma/kappa.
                "optimal_users": self.optimal_users(),
                "peak_throughput": self.peak_throughput(),
                # heuristic, not part of the USL equation itself - see saturation_point() docstring
                "saturation_point": self.saturation_point(),
            },
            "scalability_classification": self.scalability_classification(),
            "efficiency": self.efficiency(predict_at),
            "fit_quality": self.fit_quality(),
            "fit_quality_status": self.fit_quality_status(),
            "parameter_interpretation": self.parameter_interpretation(),
            "observed_vs_predicted": self.observed_predictions(),
            "parameter_uncertainty": self.parameter_uncertainty(),
            "prediction_reliability": self.prediction_reliability(),
            "observed_range": {
                "min_users": float(obs.user_levels[0]),
                "max_users": float(obs.user_levels[-1]),
            },
        }


def run_usl_analysis(data: Dict[str, Sequence[float]], predict_at: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    """One-shot helper for the Flask layer - fit + analyze in one call."""
    model = UniversalScalabilityModel()
    model.fit(data)
    return model.analyze(predict_at=predict_at)