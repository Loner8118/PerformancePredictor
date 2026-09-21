from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

from scipy import stats as _scipy_stats


class StatisticsError(ValueError):
    """Bad input (empty/insufficient data, mismatched paired samples)."""


# ==========================================================================
# What this module does
# ==========================================================================
#
# Pure, model-agnostic statistics shared by every other file in this
# validation layer - no USL, no queueing, no bottleneck logic anywhere
# below. Two jobs:
#
#   1. Turn n repeated measurements of the same quantity (e.g. 3
#      repetitions of the 500-user load test, per this project's own
#      methodology) into a mean, standard deviation, and confidence
#      interval - via the t-distribution, not a normal/z-based interval.
#      That distinction is not pedantry at n=3: the correct t-critical
#      value at 95% confidence with df=2 is ~4.30, against a z-critical
#      value of ~1.96 - a z-based interval here would be roughly half as
#      wide as the honest one, understating uncertainty by 2x on exactly
#      the small samples this project actually produces.
#
#   2. Answer "is configuration/model B actually significantly different
#      from A, or could this be the same 3-5 repetitions' worth of
#      noise" via a paired t-test - the statistical backbone of
#      ablation.py's "the full system beats naive approaches" claim.
#      ablation.py itself produces one point estimate per configuration
#      per run; turning that into a defensible significance claim means
#      running the experiment (or several different applications)
#      multiple times and feeding the resulting arrays of per-run errors
#      into paired_t_test()/compare_against_baseline() here - this
#      module doesn't know or care that the numbers came from an
#      ablation study specifically, which is deliberate (see the file
#      list this was planned against: "pure stats, no model-specific
#      logic").
#
# Uses scipy.stats directly for the t-distribution and the paired t-test
# itself, rather than hand-rolling either - both are easy to get subtly
# wrong (a hand-rolled paired t-test forgetting that it's the standard
# error of the DIFFERENCES that matters, not of each sample separately,
# is a classic mistake), and this project already depends on scipy for
# usl.py's curve_fit.


DEFAULT_CONFIDENCE_LEVEL = 0.95
DEFAULT_ALPHA = 0.05


def mean_stdev_ci(
    values: Sequence[float],
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
) -> Dict[str, Any]:
    """
    Sample mean, sample standard deviation (Bessel-corrected, n-1
    denominator), and a t-distribution confidence interval.

    Returns {"n", "mean", "stdev", "stderr", "ci_low", "ci_high",
    "confidence_level"}. stdev/stderr/ci_low/ci_high are None when
    n < 2 - a standard deviation and CI are undefined for a single
    observation, but the mean is still well-defined and reported.

    Raises:
        StatisticsError: values is empty, or confidence_level isn't in (0, 1).
    """
    if not values:
        raise StatisticsError("values must not be empty.")
    if not 0.0 < confidence_level < 1.0:
        raise StatisticsError("confidence_level must be between 0 and 1.")

    n = len(values)
    mean = sum(values) / n

    if n < 2:
        return {
            "n": n, "mean": round(mean, 6), "stdev": None, "stderr": None,
            "ci_low": None, "ci_high": None, "confidence_level": confidence_level,
        }

    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    stdev = math.sqrt(variance)
    stderr = stdev / math.sqrt(n)

    alpha = 1.0 - confidence_level
    t_critical = float(_scipy_stats.t.ppf(1.0 - alpha / 2.0, df=n - 1))
    margin = t_critical * stderr

    return {
        "n": n,
        "mean": round(mean, 6),
        "stdev": round(stdev, 6),
        "stderr": round(stderr, 6),
        "ci_low": round(mean - margin, 6),
        "ci_high": round(mean + margin, 6),
        "confidence_level": confidence_level,
    }


def aggregate_repetitions(
    values: Sequence[float],
    metric_name: str = "value",
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
) -> Dict[str, Any]:
    """
    Aggregate repeated measurements of the SAME quantity at the SAME
    load level (e.g. throughput from 3 repetitions of the 500-user load
    test) into mean/stdev/CI, plus min/max/the raw values themselves -
    for a report table that wants to show the underlying spread, not
    just a single summary number.
    """
    summary = mean_stdev_ci(values, confidence_level=confidence_level)
    return {
        "metric": metric_name,
        **summary,
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "raw_values": list(values),
    }


def paired_t_test(
    sample_a: Sequence[float],
    sample_b: Sequence[float],
    alpha: float = DEFAULT_ALPHA,
    alternative: str = "two-sided",
) -> Dict[str, Any]:
    """
    Paired t-test: does sample_b differ significantly from sample_a,
    across the same matched trials (e.g. ablation.py's per-run
    prediction error for two configurations, matched by which run/level
    produced each pair, so every pair reflects the same underlying test
    conditions)?

    Args:
        sample_a, sample_b: equal-length sequences, paired element-wise
            (sample_a[i] and sample_b[i] must come from the same trial).
        alpha: significance threshold.
        alternative: "two-sided" (do they differ at all - the default),
            "less" (is A's mean less than B's), or "greater" (is A's
            mean greater than B's) - matches scipy.stats.ttest_rel's own
            convention exactly rather than redefining one-sided-test
            semantics by hand.

    Returns:
        {"n", "mean_difference" (mean of B - A), "difference_ci",
         "t_statistic", "p_value", "alpha", "significant", "alternative", "note"}.

    Raises:
        StatisticsError: unequal lengths, fewer than 2 paired
            observations, or an invalid `alternative`.
    """
    if len(sample_a) != len(sample_b):
        raise StatisticsError(
            f"sample_a and sample_b must be the same length (paired samples), "
            f"got {len(sample_a)} and {len(sample_b)}."
        )
    if len(sample_a) < 2:
        raise StatisticsError("Need at least 2 paired observations to run a t-test.")
    if alternative not in ("two-sided", "less", "greater"):
        raise StatisticsError("alternative must be 'two-sided', 'less', or 'greater'.")

    differences = [b - a for a, b in zip(sample_a, sample_b)]
    diff_stats = mean_stdev_ci(differences, confidence_level=1.0 - alpha)

    test_result = _scipy_stats.ttest_rel(sample_b, sample_a, alternative=alternative)
    t_statistic = float(test_result.statistic)
    p_value = float(test_result.pvalue)
    significant = p_value < alpha

    return {
        "n": len(sample_a),
        "mean_difference": diff_stats["mean"],
        "difference_ci": {"low": diff_stats["ci_low"], "high": diff_stats["ci_high"]},
        "t_statistic": round(t_statistic, 6),
        "p_value": round(p_value, 6),
        "alpha": alpha,
        "significant": significant,
        "alternative": alternative,
        "note": (
            f"Mean difference (B - A) is statistically significant at alpha={alpha} (p={p_value:.4g})."
            if significant else
            f"No statistically significant difference detected at alpha={alpha} (p={p_value:.4g}). "
            f"This can mean a genuine absence of difference, or simply too few paired observations "
            f"(n={len(sample_a)}) to detect one that's really there - do not report 'no difference' "
            f"as a confirmed finding without more repetitions or trials."
        ),
    }


def compare_against_baseline(
    samples: Dict[str, Sequence[float]],
    baseline: str,
    alpha: float = DEFAULT_ALPHA,
    alternative: str = "two-sided",
) -> Dict[str, Any]:
    """
    Runs paired_t_test() of every OTHER key in `samples` against
    `baseline` - the common "is each configuration significantly
    different from the baseline" question (e.g. ablation.py's Config A
    as baseline, B/C/D each compared against it, using arrays of
    per-run/per-application prediction error for each configuration).

    Reports a Bonferroni-corrected threshold (alpha / number of
    comparisons) alongside each test's raw p-value, since running
    several comparisons against the same baseline at once inflates the
    chance of a false positive if each is naively judged against the
    original alpha. Bonferroni is the simplest, most conservative, most
    reviewer-expected correction - reported as one reasonable choice,
    not the only valid one; the raw p-values are also included so a
    less conservative correction (Holm, Benjamini-Hochberg) can be
    applied instead if preferred.

    Raises:
        StatisticsError: baseline isn't a key in samples, or samples has
            no other keys to compare it against.
    """
    if baseline not in samples:
        raise StatisticsError(f"baseline {baseline!r} not found in samples.")

    others = {name: values for name, values in samples.items() if name != baseline}
    if not others:
        raise StatisticsError("samples must contain at least one non-baseline key to compare.")

    num_comparisons = len(others)
    bonferroni_alpha = alpha / num_comparisons

    comparisons: Dict[str, Any] = {}
    for name, values in others.items():
        test = paired_t_test(samples[baseline], values, alpha=alpha, alternative=alternative)
        test["significant_after_bonferroni"] = test["p_value"] < bonferroni_alpha
        comparisons[name] = test

    return {
        "baseline": baseline,
        "num_comparisons": num_comparisons,
        "bonferroni_alpha": round(bonferroni_alpha, 6),
        "comparisons": comparisons,
    }
