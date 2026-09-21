from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence


class AmdahlValidationError(ValueError):
    """Bad input (not a real bottleneck.py analyze() result)."""


class AmdahlCalculationError(RuntimeError):
    """A calculation received a mathematically invalid argument."""


# ==========================================================================
# What this module does
# ==========================================================================
#
# Amdahl's Law answers a question none of the other modules can:
# "even if I fully optimize the bottleneck resource, how much would that
# actually help?" bottleneck.py can say "CPU is the bottleneck" and
# "prioritize fixing it" - it can't say whether that's worth an
# afternoon's work or a quarter's worth of engineering effort. This
# module adds that number.
#
#     Speedup(S) = 1 / ((1-P) + P/S)
#
# This is a deliberate REINTERPRETATION of Amdahl's Law, not the
# classical serial-vs-parallel-code framing it was originally stated
# for. Here:
#
#     P = D_i / D_total
#
# - the fraction of total per-request service demand (from bottleneck.py's
#   Utilization Law analysis) attributable to the resource being improved.
#   S is how many times faster that resource is made. As S -> infinity,
#   Speedup -> 1/(1-P), the theoretical ceiling - the maximum possible
#   improvement from optimizing that ONE resource, no matter how good the
#   optimization is, because the OTHER resources' demand is unaffected.
#
# This needs zero new instrumentation: bottleneck.py already computes
# D_i for every monitored resource and D_total, so this is a formula
# applied to existing numbers, not a new measurement. It also
# automatically improves if forced_flow.py (database/API demand) is
# ever added to bottleneck.py's total - this module reads whatever
# resources and total_service_demand it's given, generically, with no
# hardcoded assumption that only cpu/disk/network exist.
#
# Honesty note, consistent with bottleneck.py's own: this only accounts
# for resources bottleneck.py actually monitors. If a real bottleneck
# lives somewhere unmeasured (e.g. an unmonitored database), P for the
# monitored resources will look artificially high relative to the true
# picture - the ceiling computed here is only as complete as
# bottleneck.py's own inputs.


DEFAULT_ILLUSTRATIVE_SPEEDUPS: Sequence[float] = (2.0, 5.0, 10.0)


# --------------------------------------------------------------------------
# Core equations (pure functions, no state)
# --------------------------------------------------------------------------

def calculate_resource_fraction(resource_demand: float, total_demand: float) -> float:
    """P = D_i / D_total - this resource's share of total measured service demand."""
    if resource_demand < 0 or total_demand < 0:
        raise AmdahlCalculationError("demands must not be negative.")
    if total_demand <= 0:
        raise AmdahlCalculationError("total_demand must be greater than zero to compute a fraction.")
    if resource_demand > total_demand * (1 + 1e-9):
        raise AmdahlCalculationError("resource_demand cannot exceed total_demand.")
    return min(resource_demand / total_demand, 1.0)


def calculate_amdahl_speedup(p: float, s: float) -> float:
    """
    Speedup(S) = 1 / ((1-P) + P/S)

    p: fraction of total service demand attributable to the resource
       being improved (0 <= p <= 1).
    s: how many times faster that resource is made (s > 0). s == 1 means
       no change (speedup == 1); larger s approaches the theoretical
       maximum speedup, calculate_max_speedup(p).
    """
    if not (0.0 <= p <= 1.0):
        raise AmdahlCalculationError("p must be between 0 and 1.")
    if s <= 0:
        raise AmdahlCalculationError("s must be greater than zero.")

    denominator = (1.0 - p) + (p / s)
    if denominator <= 0:
        return math.inf  # only reachable in the s -> infinity limit
    return 1.0 / denominator


def calculate_max_speedup(p: float) -> float:
    """
    Theoretical maximum speedup as S -> infinity: 1 / (1-p).

    Returns math.inf when p == 1 (the resource accounts for the entire
    measured workload - see the "only one resource monitored" caveat
    surfaced in analyze() below; this is usually a monitoring-scope
    artifact, not a genuinely unbounded real-world ceiling).
    """
    if not (0.0 <= p <= 1.0):
        raise AmdahlCalculationError("p must be between 0 and 1.")
    serial_fraction = 1.0 - p
    if serial_fraction <= 0:
        return math.inf
    return 1.0 / serial_fraction


def _find_next_bottleneck(resources: Dict[str, Any], eliminated_resource: str) -> Optional[Dict[str, Any]]:
    """
    If the current bottleneck were optimized away entirely, which
    resource becomes the new limiting factor? Answers the natural
    follow-up question Amdahl's Law raises on its own: a ceiling below
    what you hoped for usually means something else takes over long
    before you'd notice the first resource stopped mattering.
    """
    remaining = {
        name: info for name, info in resources.items()
        if name != eliminated_resource and info.get("service_demand") is not None
    }
    if not remaining:
        return None
    next_name = max(remaining, key=lambda n: remaining[n]["service_demand"])
    return {"resource": next_name, "service_demand": remaining[next_name]["service_demand"]}


def _extract_bottleneck_data(bottleneck_results: Any) -> Optional[Dict[str, Any]]:
    """
    Accepts the direct output of bottleneck.analyze_bottleneck() /
    analyze_bottleneck_safe(). Returns it unchanged if usable, or None if
    missing/malformed/"unavailable" - lets analyze() degrade gracefully
    rather than erroring, matching capacity.py's and scalability.py's own
    handling of optional bottleneck.py data.
    """
    if not isinstance(bottleneck_results, dict):
        return None
    if bottleneck_results.get("bottleneck_analysis") == "unavailable":
        return None
    if "resources" not in bottleneck_results or "total_service_demand" not in bottleneck_results:
        return None
    return bottleneck_results


# --------------------------------------------------------------------------
# Analyzer
# --------------------------------------------------------------------------

class AmdahlAnalyzer:
    """
    Turns bottleneck.py's per-resource service demand into an
    optimization ceiling: how much would fixing each resource actually
    help, at best, and does the next bottleneck kick in before that
    ceiling is worth chasing.
    """

    def __init__(self, illustrative_speedups: Sequence[float] = DEFAULT_ILLUSTRATIVE_SPEEDUPS) -> None:
        if not illustrative_speedups or any(s <= 0 for s in illustrative_speedups):
            raise AmdahlValidationError("illustrative_speedups must be a non-empty sequence of positive numbers.")
        self.illustrative_speedups = list(illustrative_speedups)

    def analyze(self, bottleneck_results: Dict[str, Any]) -> Dict[str, Any]:
        """
        Args:
            bottleneck_results: the dict returned by
                bottleneck.analyze_bottleneck() / analyze_bottleneck_safe().

        Returns:
            {"available": False, "reason": ...} if bottleneck data isn't
            usable (missing, malformed, "unavailable", or zero total
            demand), otherwise a dict with per_resource ceilings,
            ranked_resources, bottleneck_analysis, and notes.
        """
        data = _extract_bottleneck_data(bottleneck_results)
        if data is None:
            return {
                "available": False,
                "reason": "No usable bottleneck.py data was supplied (missing, malformed, or unavailable).",
            }

        total_demand = data["total_service_demand"]
        resources = data["resources"]
        current_throughput = data.get("throughput")

        if not isinstance(total_demand, (int, float)) or total_demand <= 0:
            return {
                "available": False,
                "reason": (
                    "Total service demand is zero or invalid - no resource shows measurable "
                    "demand, so an Amdahl's Law fraction cannot be computed."
                ),
            }

        per_resource: Dict[str, Any] = {}
        notes: List[str] = []
        for name, info in resources.items():
            d_i = info.get("service_demand")
            if not isinstance(d_i, (int, float)):
                continue

            try:
                p = calculate_resource_fraction(d_i, total_demand)
            except AmdahlCalculationError as exc:
                # Shouldn't happen if bottleneck.py's total_service_demand
                # is genuinely the sum of every resource's D_i - if it does,
                # that's a real upstream data inconsistency worth a trace,
                # not a silently-dropped resource.
                notes.append(f"Resource '{name}' excluded from Amdahl analysis: {exc}")
                continue

            max_speedup = calculate_max_speedup(p)
            max_speedup_finite = math.isfinite(max_speedup)

            illustrative = {
                self._format_speedup_key(s): round(calculate_amdahl_speedup(p, s), 4)
                for s in self.illustrative_speedups
            }

            implied_max_throughput = None
            if max_speedup_finite and isinstance(current_throughput, (int, float)):
                implied_max_throughput = round(current_throughput * max_speedup, 2)

            per_resource[name] = {
                "resource": name,
                "service_demand": d_i,
                "fraction_of_total_demand": round(p, 4),
                "max_theoretical_speedup": round(max_speedup, 4) if max_speedup_finite else None,
                "max_theoretical_speedup_unbounded": not max_speedup_finite,
                "illustrative_speedups": illustrative,
                "implied_max_throughput": implied_max_throughput,
            }

        if not per_resource:
            return {
                "available": False,
                "reason": "No resource in bottleneck.py's output had a usable service_demand value.",
            }

        ranked_resources = sorted(
            per_resource.keys(),
            key=lambda n: (
                per_resource[n]["max_theoretical_speedup"]
                if per_resource[n]["max_theoretical_speedup"] is not None
                else math.inf
            ),
            reverse=True,
        )

        sum_of_known_demands = sum(info["service_demand"] for info in per_resource.values())
        if sum_of_known_demands > total_demand * (1 + 1e-6):
            notes.append(
                f"The sum of known per-resource service demands ({sum_of_known_demands:.6f}s/req) "
                f"exceeds total_service_demand ({total_demand:.6f}s/req) reported by bottleneck.py - "
                f"the fractions below are computed against the reported total regardless, but this "
                f"inconsistency should be checked upstream."
            )

        if len(per_resource) == 1:
            notes.append(
                "Only one resource is monitored, so its fraction of total demand is trivially 1.0 "
                "- this reflects the limited monitoring scope, not a meaningful optimization "
                "priority signal."
            )

        bottleneck_resource = data.get("bottleneck", {}).get("resource")
        bottleneck_analysis = per_resource.get(bottleneck_resource) if bottleneck_resource else None

        if bottleneck_resource and bottleneck_analysis:
            next_bottleneck = _find_next_bottleneck(resources, bottleneck_resource)
            if next_bottleneck:
                bottleneck_analysis = {
                    **bottleneck_analysis,
                    "next_bottleneck_after_optimization": next_bottleneck,
                }
                notes.append(
                    f"Even with {bottleneck_resource} optimized to be infinitely fast, "
                    f"{next_bottleneck['resource']} would become the new limiting factor "
                    f"(service demand {next_bottleneck['service_demand']:.6f}s/req) - the "
                    f"computed ceiling for {bottleneck_resource} assumes every OTHER resource's "
                    f"demand stays fixed, which stops being true once {bottleneck_resource} is no "
                    f"longer the constraint."
                )

        return {
            "available": True,
            "current_throughput": current_throughput,
            "total_service_demand": total_demand,
            "per_resource": per_resource,
            "ranked_resources": ranked_resources,
            "bottleneck_resource": bottleneck_resource,
            "bottleneck_analysis": bottleneck_analysis,
            "notes": notes,
        }

    @staticmethod
    def _format_speedup_key(s: float) -> str:
        return f"{s:g}x"


def analyze_amdahl(
    bottleneck_results: Dict[str, Any],
    illustrative_speedups: Sequence[float] = DEFAULT_ILLUSTRATIVE_SPEEDUPS,
) -> Dict[str, Any]:
    """One-shot: construct an AmdahlAnalyzer and run analyze()."""
    return AmdahlAnalyzer(illustrative_speedups=illustrative_speedups).analyze(bottleneck_results)


# --- USL / Amdahl theoretical consistency ---
#
# Gunther's Universal Scalability Law generalizes Amdahl's Law: setting
# kappa=0 in USL's C(N) = N / (1 + sigma*(N-1) + kappa*N*(N-1)) leaves
# C(N) = N / (1 + sigma*(N-1)), which is algebraically identical to
# Amdahl's classical Speedup(N) = 1 / (f + (1-f)/N) with f = sigma (the
# serial/contention fraction that never scales, regardless of N):
#
#     1 / (f + (1-f)/N)  =  N / (f*N + (1-f))  =  N / (1 + f*(N-1))
#
# Note this f is the COMPLEMENT of calculate_amdahl_speedup()'s own `p`
# parameter above - that function's p is "the fraction being improved"
# (the parallelizable part that DOES speed up), so f = 1 - p there. USL's
# sigma plays the role of f directly (the part that never scales), which
# is why the call below passes (1 - serial_fraction) as p, not
# serial_fraction itself - passing it directly was an early mistake here,
# caught by the numeric cross-check this function performs (they
# disagreed until the sign was fixed - which is exactly why this checks
# the actual formulas against each other instead of asserting they match).
#
# The function below doesn't just assert the algebra - it computes both
# formulas independently (Amdahl's own closed form here, USL's actual
# usl_capacity() from usl.py with kappa forced to 0) and checks they
# agree numerically. That's what "verified" should mean, rather than
# "the algebra works out on paper" - this also catches a real typo or
# sign error in either formula, which is exactly what happened during
# this function's own development.

def verify_usl_amdahl_equivalence(
    serial_fraction: float,
    user_counts: Sequence[float],
    tolerance: float = 1e-9,
) -> Dict[str, Any]:
    """
    Confirms USL(kappa=0) and classical Amdahl's Law produce identical
    speedup/capacity curves for the same serial_fraction (= USL's sigma).

    Args:
        serial_fraction: Amdahl's f / USL's sigma, 0 <= f <= 1. Call this
            right after fitting a USL model with model.sigma and the
            same user_levels the model was fit on, e.g.:
                verify_usl_amdahl_equivalence(model.sigma, data["user_levels"])
        user_counts: N values to compare the two formulas at.
        tolerance: maximum allowed relative difference before flagging
            disagreement - should only ever be hit by floating-point
            noise, not a real mismatch, since both formulas reduce to
            the same algebraic expression.

    Raises:
        AmdahlValidationError: bad serial_fraction, empty/invalid
            user_counts, or usl.py's usl_capacity() isn't importable
            (needs numpy/scipy - this check is optional/supplementary,
            not required for the rest of this module).
    """
    if not (0.0 <= serial_fraction <= 1.0):
        raise AmdahlValidationError("serial_fraction must be between 0 and 1.")
    if not user_counts:
        raise AmdahlValidationError("user_counts must not be empty.")

    try:
        from mathematical_engine.usl import usl_capacity
    except ImportError as exc:
        raise AmdahlValidationError(
            "verify_usl_amdahl_equivalence() requires usl.py to be importable as "
            "mathematical_engine.usl (and numpy/scipy installed) - this is a "
            "theoretical cross-check, not required for the rest of this module."
        ) from exc

    amdahl_values: Dict[float, float] = {}
    usl_values: Dict[float, float] = {}
    max_relative_difference = 0.0

    for n in user_counts:
        if n <= 0:
            raise AmdahlValidationError(f"user_counts must all be positive, got {n}.")

        amdahl_speedup = calculate_amdahl_speedup(1.0 - serial_fraction, float(n))
        usl_value = float(usl_capacity(float(n), sigma=serial_fraction, kappa=0.0))

        amdahl_values[float(n)] = round(amdahl_speedup, 10)
        usl_values[float(n)] = round(usl_value, 10)

        if usl_value != 0:
            relative_difference = abs(amdahl_speedup - usl_value) / usl_value
            max_relative_difference = max(max_relative_difference, relative_difference)

    relationship_confirmed = max_relative_difference <= tolerance

    return {
        "serial_fraction": serial_fraction,
        "amdahl_classical": amdahl_values,
        "usl_kappa_zero": usl_values,
        "max_relative_difference": max_relative_difference,
        "relationship_confirmed": relationship_confirmed,
        "note": (
            "USL's capacity function with kappa=0 and classical Amdahl's Law with f=sigma "
            "agree to within floating-point precision across every tested N, confirming "
            "USL is a generalization of Amdahl's Law (kappa adds a coherency/coordination "
            "term Amdahl's original formulation doesn't have)."
        ) if relationship_confirmed else (
            f"USL(kappa=0) and Amdahl's Law diverge by up to {max_relative_difference:.2%} "
            f"at the tested N values - this should not happen given both reduce to the same "
            f"algebraic expression; check for a calculation error in one of the two formulas."
        ),
    }