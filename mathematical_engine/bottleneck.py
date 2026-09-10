from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple


class BottleneckValidationError(ValueError):
    """Bad input data."""


class BottleneckCalculationError(RuntimeError):
    """A bottleneck calculation received a mathematically invalid argument."""


# ==========================================================================
# What this module does
# ==========================================================================
#
# Three related pieces of classical operational analysis, layered on top
# of metrics you already collect (cpu_usage, disk_io, network_io,
# throughput, current_users) - no new instrumentation required to use it:
#
#   1. Utilization Law:   U_i = X * D_i   ->   D_i = U_i / X
#      "How many seconds of resource i does one request consume, on
#      average?" Rearranged from the usual X_i = X * V_i form since we
#      have U_i (utilization) directly rather than visit counts/service
#      times per resource.
#
#   2. Bottleneck Analysis:   D_max = max(D_i)
#      Whichever resource has the highest per-request demand is the one
#      that will limit throughput first as load grows - not necessarily
#      whichever resource has the highest raw utilization right now.
#
#   3. Asymptotic Bounds:   X <= min(1/D_max, N/(D+Z))
#      Two independent ceilings on throughput: the bottleneck resource
#      itself (1/D_max), and the number of concurrent users cycling
#      between think time Z and total service demand D (N/(D+Z)). This
#      gives an upper bound to sanity-check USL's fitted predictions
#      against, especially useful once you're extrapolating well past
#      the tested load range.
#
# Deliberately excluded: memory. Memory is a capacity ceiling (you run
# out or you don't), not a "service center" a request queues for and
# releases - it doesn't have a meaningful utilization-law demand the way
# CPU/disk/network do. It stays exactly where it already is, as a
# separate pressure check in capacity.py.
#
# Also deliberately excluded: database, external APIs, and anything else
# you're not currently monitoring. This module reports the bottleneck
# *among monitored resources* - it cannot flag a resource it never saw.
# Once per-request visit-ratio tracking exists for those (queries/
# request, API calls/request), that's Forced Flow Law territory, a
# separate module.
#
# CPU utilization is a true measured busy-time fraction. Disk/network
# utilization here is an *approximation*: observed throughput rate
# divided by a configured maximum rate, not a measured busy-time
# fraction - every place that matters, this is labeled explicitly via
# is_approximate=True.


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

@dataclass
class ResourceCapacityConfig:
    """
    Configured maximum sustained throughput for the rate-based
    (approximate) resources. This is a deployment fact, not something
    measured at runtime - set these to your actual disk/network limits
    (disk type, RAID config, NIC speed, cloud instance network cap,
    etc.) for the disk/network utilization figures to mean anything.
    The defaults are placeholders, not recommendations.
    """
    max_disk_mb_s: float = 200.0
    max_network_mb_s: float = 125.0

    def __post_init__(self) -> None:
        if self.max_disk_mb_s <= 0:
            raise BottleneckValidationError("max_disk_mb_s must be greater than zero.")
        if self.max_network_mb_s <= 0:
            raise BottleneckValidationError("max_network_mb_s must be greater than zero.")


@dataclass
class BottleneckSeverityThresholds:
    """
    Bands for how close observed/predicted throughput is to the derived
    asymptotic ceiling (ratio = throughput / asymptotic_bound):
        ratio >= 1.0             -> "At or beyond theoretical ceiling"
        ratio >= near_ceiling_min -> "Near ceiling"
        ratio >= moderate_min     -> "Moderate headroom"
        otherwise                 -> "Substantial headroom"
    """
    near_ceiling_min: float = 0.85
    moderate_min: float = 0.60


@dataclass
class BottleneckSignalThresholds:
    dominant_margin: float = 0.20  # bottleneck must lead the runner-up by this fraction of D_max to count as "clear"


# --------------------------------------------------------------------------
# Validated workload container
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ResourceUtilization:
    """
    One resource's utilization, ready for the D_i = U_i / X calculation.

    utilization: 0-1 fraction, clipped for calculation purposes.
    raw_utilization: unclipped - can exceed 1.0, which is itself a signal
        (either the resource is genuinely over its configured capacity,
        or, for disk/network, the configured max rate is set too low).
    is_approximate: True for disk/network (rate-based ratio, not a true
        measured busy-time fraction like CPU).
    """
    name: str
    utilization: float
    raw_utilization: float
    is_approximate: bool
    exceeds_configured_capacity: bool


@dataclass(frozen=True)
class BottleneckWorkload:
    throughput: float
    current_users: Optional[float]
    think_time: float
    resources: List[ResourceUtilization]
    metadata: Dict[str, Any]

    @staticmethod
    def from_raw(
        data: Dict[str, Any],
        capacity_config: Optional[ResourceCapacityConfig] = None,
    ) -> "BottleneckWorkload":
        """
        Validation rules:
            1. "throughput" (X) must be present, numeric, finite, > 0.
            2. "current_users" (N) is optional - enables the concurrency
               bound and N* alongside the bottleneck-resource bound.
            3. "think_time" (Z) is optional, defaults to 0.0 seconds.
            4. At least one of "cpu_usage" (percent, 0-100),
               "disk_io" (MB/s), "network_io" (MB/s) must be present -
               these map directly onto the runtime metrics already
               flowing through the rest of the pipeline. memory_usage is
               intentionally not accepted (see module docstring).
            5. "metadata" is passed through unchanged.
        """
        if not isinstance(data, dict):
            raise BottleneckValidationError("Input must be a dictionary.")

        if "throughput" not in data:
            raise BottleneckValidationError("Missing required field: 'throughput'.")

        throughput_raw = data["throughput"]
        if isinstance(throughput_raw, bool) or not isinstance(throughput_raw, (int, float)):
            raise BottleneckValidationError(
                f"'throughput' must be a numeric value (int or float), got {type(throughput_raw).__name__}."
            )
        throughput = float(throughput_raw)
        if not math.isfinite(throughput):
            raise BottleneckValidationError("'throughput' must be finite.")
        if throughput <= 0:
            raise BottleneckValidationError("'throughput' must be greater than zero.")

        current_users = None
        if "current_users" in data and data["current_users"] is not None:
            cu_raw = data["current_users"]
            if isinstance(cu_raw, bool) or not isinstance(cu_raw, (int, float)):
                raise BottleneckValidationError(
                    f"'current_users' must be a numeric value (int or float), got {type(cu_raw).__name__}."
                )
            if not math.isfinite(cu_raw):
                raise BottleneckValidationError("'current_users' must be finite.")
            if cu_raw <= 0:
                raise BottleneckValidationError("'current_users' must be greater than zero if provided.")
            current_users = float(cu_raw)

        think_time_raw = data.get("think_time", 0.0)
        if isinstance(think_time_raw, bool) or not isinstance(think_time_raw, (int, float)):
            raise BottleneckValidationError(
                f"'think_time' must be a numeric value (int or float), got {type(think_time_raw).__name__}."
            )
        if not math.isfinite(think_time_raw):
            raise BottleneckValidationError("'think_time' must be finite.")
        if think_time_raw < 0:
            raise BottleneckValidationError("'think_time' must not be negative.")
        think_time = float(think_time_raw)

        config = capacity_config or ResourceCapacityConfig()
        resources: List[ResourceUtilization] = []

        # --- CPU: true measured busy-time fraction ---
        if "cpu_usage" in data and data["cpu_usage"] is not None:
            cpu_raw = data["cpu_usage"]
            if isinstance(cpu_raw, bool) or not isinstance(cpu_raw, (int, float)):
                raise BottleneckValidationError(
                    f"'cpu_usage' must be a numeric value (int or float), got {type(cpu_raw).__name__}."
                )
            if not math.isfinite(cpu_raw):
                raise BottleneckValidationError("'cpu_usage' must be finite.")
            if cpu_raw < 0:
                raise BottleneckValidationError("'cpu_usage' must not be negative.")
            cpu_fraction = float(cpu_raw) / 100.0
            resources.append(ResourceUtilization(
                name="cpu",
                utilization=min(cpu_fraction, 1.0),
                raw_utilization=cpu_fraction,
                is_approximate=False,
                exceeds_configured_capacity=cpu_fraction > 1.0,
            ))

        # --- Disk: rate-based approximation ---
        if "disk_io" in data and data["disk_io"] is not None:
            disk_raw = data["disk_io"]
            if isinstance(disk_raw, bool) or not isinstance(disk_raw, (int, float)):
                raise BottleneckValidationError(
                    f"'disk_io' must be a numeric value (int or float), got {type(disk_raw).__name__}."
                )
            if not math.isfinite(disk_raw):
                raise BottleneckValidationError("'disk_io' must be finite.")
            if disk_raw < 0:
                raise BottleneckValidationError("'disk_io' must not be negative.")
            disk_fraction = float(disk_raw) / config.max_disk_mb_s
            resources.append(ResourceUtilization(
                name="disk",
                utilization=min(disk_fraction, 1.0),
                raw_utilization=disk_fraction,
                is_approximate=True,
                exceeds_configured_capacity=disk_fraction > 1.0,
            ))

        # --- Network: rate-based approximation ---
        if "network_io" in data and data["network_io"] is not None:
            net_raw = data["network_io"]
            if isinstance(net_raw, bool) or not isinstance(net_raw, (int, float)):
                raise BottleneckValidationError(
                    f"'network_io' must be a numeric value (int or float), got {type(net_raw).__name__}."
                )
            if not math.isfinite(net_raw):
                raise BottleneckValidationError("'network_io' must be finite.")
            if net_raw < 0:
                raise BottleneckValidationError("'network_io' must not be negative.")
            net_fraction = float(net_raw) / config.max_network_mb_s
            resources.append(ResourceUtilization(
                name="network",
                utilization=min(net_fraction, 1.0),
                raw_utilization=net_fraction,
                is_approximate=True,
                exceeds_configured_capacity=net_fraction > 1.0,
            ))

        if not resources:
            raise BottleneckValidationError(
                "At least one monitored resource ('cpu_usage', 'disk_io', or 'network_io') "
                "must be provided."
            )

        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise BottleneckValidationError("'metadata' must be a dictionary if provided.")

        return BottleneckWorkload(
            throughput=throughput,
            current_users=current_users,
            think_time=think_time,
            resources=resources,
            metadata=metadata,
        )


# --------------------------------------------------------------------------
# Core equations (pure functions, no state)
# --------------------------------------------------------------------------

def calculate_service_demand(utilization: float, throughput: float) -> float:
    """
    Utilization Law, rearranged: D_i = U_i / X.

    Seconds of resource i consumed per request, on average. This is the
    same D_i the classical form X_i = X * V_i * S_i implies (since
    U_i = X_i * S_i = X * V_i * S_i = X * D_i), just derived from
    utilization + overall throughput directly instead of needing visit
    counts and per-visit service times separately.
    """
    if throughput <= 0:
        raise BottleneckCalculationError("throughput must be greater than zero.")
    if utilization < 0:
        raise BottleneckCalculationError("utilization must not be negative.")
    return utilization / throughput


def find_bottleneck(demands: Dict[str, float]) -> Tuple[str, float]:
    """D_max = max(D_i). Returns (resource_name, demand_seconds)."""
    if not demands:
        raise BottleneckCalculationError("At least one resource demand is required.")
    name = max(demands, key=demands.get)
    return name, demands[name]


def calculate_asymptotic_bound(
    total_demand: float,
    bottleneck_demand: float,
    current_users: Optional[float],
    think_time: float,
) -> Dict[str, Any]:
    """
    X <= min(1/D_max, N/(D+Z))

    1/D_max - the bottleneck-resource bound: throughput can never exceed
    what the busiest monitored resource can sustain, no matter how much
    concurrency is added.

    N/(D+Z) - the concurrency bound: with N concurrent users each
    cycling through D seconds of total service demand plus Z seconds of
    think time, the system cannot physically complete more than N/(D+Z)
    requests/sec. Only computed if current_users (N) is supplied.

    N* = (D+Z)/D_max - the crossover concurrency: below N*, the
    concurrency bound is what's actually limiting throughput (adding
    capacity to the bottleneck resource wouldn't help yet); above N*,
    the bottleneck resource itself is the limiting factor.

    If every monitored resource shows zero utilization (bottleneck_demand
    == 0), the bound is reported as infinite rather than raising - it
    correctly reflects that the monitored data doesn't constrain
    throughput at all (the real bottleneck, if any, is unmonitored).
    """
    if bottleneck_demand < 0:
        raise BottleneckCalculationError("bottleneck_demand must not be negative.")
    if total_demand < 0:
        raise BottleneckCalculationError("total_demand must not be negative.")
    if think_time < 0:
        raise BottleneckCalculationError("think_time must not be negative.")

    bottleneck_bound = (1.0 / bottleneck_demand) if bottleneck_demand > 0 else math.inf
    n_star = ((total_demand + think_time) / bottleneck_demand) if bottleneck_demand > 0 else math.inf

    concurrency_bound = None
    binding_constraint = None
    asymptotic_bound = bottleneck_bound

    if current_users is not None:
        if current_users <= 0:
            raise BottleneckCalculationError("current_users must be greater than zero.")
        denom = total_demand + think_time
        concurrency_bound = (current_users / denom) if denom > 0 else math.inf
        asymptotic_bound = min(bottleneck_bound, concurrency_bound)
        binding_constraint = "bottleneck_resource" if bottleneck_bound <= concurrency_bound else "concurrency"

    return {
        "bottleneck_throughput_bound": bottleneck_bound,
        "concurrency_throughput_bound": concurrency_bound,
        "asymptotic_throughput_bound": asymptotic_bound,
        "binding_constraint": binding_constraint,
        "optimal_concurrency_n_star": n_star,
    }


def compare_to_observed(asymptotic_bound: float, observed_throughput: float) -> Dict[str, Any]:
    """
    How close a throughput figure (measured, or a USL/scalability
    prediction) is to the derived theoretical ceiling. A value that
    exceeds the bound isn't just "high" - it's a sign that either the
    demand estimates or the prediction being checked have a problem,
    since the ceiling is a hard physical limit given the monitored data.
    """
    if asymptotic_bound < 0:
        raise BottleneckCalculationError("asymptotic_bound must not be negative.")
    if observed_throughput < 0:
        raise BottleneckCalculationError("observed_throughput must not be negative.")

    ratio = (observed_throughput / asymptotic_bound) if asymptotic_bound > 0 else math.inf

    return {
        "observed_throughput": observed_throughput,
        "asymptotic_bound": asymptotic_bound,
        "ratio_of_bound": ratio,
        "exceeds_bound": bool(math.isfinite(ratio) and ratio > 1.0 + 1e-6) or (asymptotic_bound == 0 and observed_throughput > 0),
    }


def classify_bottleneck_severity(
    ratio_of_bound: Optional[float],
    thresholds: Optional[BottleneckSeverityThresholds] = None,
) -> str:
    if thresholds is None:
        thresholds = BottleneckSeverityThresholds()
    if ratio_of_bound is None or not math.isfinite(ratio_of_bound):
        return "Unknown"
    if ratio_of_bound >= 1.0:
        return "At or beyond theoretical ceiling"
    if ratio_of_bound >= thresholds.near_ceiling_min:
        return "Near ceiling"
    if ratio_of_bound >= thresholds.moderate_min:
        return "Moderate headroom"
    return "Substantial headroom"


# --------------------------------------------------------------------------
# Signals (indicators only - no recommendations generated here)
# --------------------------------------------------------------------------

def _generate_signals(
    demands: Dict[str, float],
    bottleneck_name: str,
    ratio_of_bound: Optional[float],
    exceeds_any_capacity: bool,
    thresholds: BottleneckSignalThresholds,
) -> Dict[str, bool]:
    sorted_demands = sorted(demands.values(), reverse=True)
    bottleneck_value = demands[bottleneck_name]
    runner_up = sorted_demands[1] if len(sorted_demands) > 1 else 0.0

    clear_dominant = (
        bottleneck_value > 0
        and (runner_up == 0 or (bottleneck_value - runner_up) / bottleneck_value >= thresholds.dominant_margin)
    )

    return {
        "clear_dominant_bottleneck": clear_dominant,
        "near_theoretical_ceiling": ratio_of_bound is not None and math.isfinite(ratio_of_bound) and ratio_of_bound >= 0.85,
        "exceeds_theoretical_ceiling": ratio_of_bound is not None and math.isfinite(ratio_of_bound) and ratio_of_bound > 1.0 + 1e-6,
        "resource_exceeds_configured_capacity": exceeds_any_capacity,
    }


# --------------------------------------------------------------------------
# Unavailable-result helper
# --------------------------------------------------------------------------

def unavailable_bottleneck_result(
    reason: str,
    throughput: Optional[float] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Explicit "we don't have what we need" result, for when none of
    cpu_usage/disk_io/network_io are available yet. Mirrors
    queueing.unavailable_queueing_result() - keeps the pipeline running
    with an honest "unavailable" instead of a silently empty analysis.
    """
    return {
        "bottleneck_analysis": "unavailable",
        "reason": reason,
        "throughput": throughput,
        "metadata": metadata or {},
    }


# --------------------------------------------------------------------------
# Analyzer
# --------------------------------------------------------------------------

class BottleneckAnalyzer:
    """
    Utilization Law -> per-resource service demand, Bottleneck Analysis
    -> which monitored resource limits throughput first, Asymptotic
    Bounds -> the hard throughput ceiling that implies.

    Stateful by design (unlike the pure functions above): after
    analyze(), project_bound() and check_prediction() reuse the fitted
    demand figures to evaluate the bound at other, hypothetical
    concurrency levels - e.g. the same prediction targets USL and
    scalability.py use, so their forward predictions can be checked
    against an independently-derived ceiling.
    """

    def __init__(
        self,
        capacity_config: Optional[ResourceCapacityConfig] = None,
        severity_thresholds: Optional[BottleneckSeverityThresholds] = None,
        signal_thresholds: Optional[BottleneckSignalThresholds] = None,
    ) -> None:
        self.capacity_config = capacity_config or ResourceCapacityConfig()
        self.severity_thresholds = severity_thresholds or BottleneckSeverityThresholds()
        self.signal_thresholds = signal_thresholds or BottleneckSignalThresholds()

        self._workload: Optional[BottleneckWorkload] = None
        self._demands: Optional[Dict[str, float]] = None
        self._bottleneck_name: Optional[str] = None
        self._bottleneck_demand: Optional[float] = None
        self._total_demand: Optional[float] = None
        self._bottleneck_bound: Optional[float] = None
        self._analyzed = False

    def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Args:
            data: {"throughput": ..., "cpu_usage": ... (optional),
                   "disk_io": ... (optional), "network_io": ... (optional),
                   "current_users": ... (optional), "think_time": ... (optional),
                   "metadata": {...} (optional)}. At least one of
                   cpu_usage/disk_io/network_io is required.

        Raises:
            BottleneckValidationError: bad/missing input.
        """
        workload = BottleneckWorkload.from_raw(data, capacity_config=self.capacity_config)

        demands = {
            resource.name: calculate_service_demand(resource.utilization, workload.throughput)
            for resource in workload.resources
        }
        bottleneck_name, bottleneck_demand = find_bottleneck(demands)
        total_demand = sum(demands.values())

        bound_info = calculate_asymptotic_bound(
            total_demand=total_demand,
            bottleneck_demand=bottleneck_demand,
            current_users=workload.current_users,
            think_time=workload.think_time,
        )

        observed_comparison = compare_to_observed(
            bound_info["asymptotic_throughput_bound"], workload.throughput
        )
        severity = classify_bottleneck_severity(observed_comparison["ratio_of_bound"], self.severity_thresholds)
        exceeds_any_capacity = any(r.exceeds_configured_capacity for r in workload.resources)

        signals = _generate_signals(
            demands=demands,
            bottleneck_name=bottleneck_name,
            ratio_of_bound=observed_comparison["ratio_of_bound"],
            exceeds_any_capacity=exceeds_any_capacity,
            thresholds=self.signal_thresholds,
        )

        # Cache the fitted figures for project_bound()/check_prediction().
        self._workload = workload
        self._demands = demands
        self._bottleneck_name = bottleneck_name
        self._bottleneck_demand = bottleneck_demand
        self._total_demand = total_demand
        self._bottleneck_bound = bound_info["bottleneck_throughput_bound"]
        self._analyzed = True

        bottleneck_note = (
            "This is the bottleneck among monitored resources (cpu/disk/network) only - "
            "unmonitored resources such as a database or external API cannot be identified "
            "as the bottleneck here."
        )
        if len(workload.resources) == 1:
            bottleneck_note += (
                f" Only '{bottleneck_name}' is currently monitored, so this reflects the "
                f"only available signal, not a comparison against alternatives."
            )

        return {
            "model": "Utilization Law / Bottleneck Analysis / Asymptotic Bounds",
            "throughput": workload.throughput,
            "current_users": workload.current_users,
            "think_time": workload.think_time,
            "resources": {
                r.name: {
                    "utilization": r.utilization,
                    "raw_utilization": r.raw_utilization,
                    "is_approximate": r.is_approximate,
                    "exceeds_configured_capacity": r.exceeds_configured_capacity,
                    "service_demand": demands[r.name],
                }
                for r in workload.resources
            },
            "bottleneck": {
                "resource": bottleneck_name,
                "service_demand": bottleneck_demand,
                "note": bottleneck_note,
            },
            "total_service_demand": total_demand,
            "asymptotic_bounds": bound_info,
            "observed_vs_bound": observed_comparison,
            "bottleneck_severity": severity,
            "signals": signals,
            "units": {
                "throughput": "requests/sec",
                "think_time": "seconds",
                "service_demand": "seconds/request",
                "total_service_demand": "seconds/request",
                "bottleneck_throughput_bound": "requests/sec",
                "concurrency_throughput_bound": "requests/sec",
                "asymptotic_throughput_bound": "requests/sec",
                "optimal_concurrency_n_star": "users",
            },
            "metadata": workload.metadata,
        }

    def project_bound(self, user_counts: Sequence[float]) -> Dict[float, Dict[str, Any]]:
        """
        Evaluate the asymptotic throughput bound at arbitrary hypothetical
        concurrency levels - e.g. the same prediction_targets USL/
        scalability.py project forward to - so those predictions can be
        checked against a ceiling derived independently of the USL curve
        fit.

        Assumes per-request service demand (D_i) stays constant as N
        grows. That's a simplifying assumption: contention effects that
        make demand grow with concurrency are exactly what USL's
        sigma/kappa already model. This bound is a complementary, harder
        physical constraint on top of that curve, not a replacement for it.
        """
        self._require_analyzed()
        results: Dict[float, Dict[str, Any]] = {}

        for n in user_counts:
            if isinstance(n, bool) or not isinstance(n, (int, float)):
                raise BottleneckValidationError("user_counts must contain only numeric values.")
            if not math.isfinite(n) or n <= 0:
                raise BottleneckValidationError("user_counts must be finite and positive.")

            denom = self._total_demand + self._workload.think_time
            concurrency_bound = (float(n) / denom) if denom > 0 else math.inf
            bound = min(self._bottleneck_bound, concurrency_bound)
            binding = "bottleneck_resource" if self._bottleneck_bound <= concurrency_bound else "concurrency"

            results[float(n)] = {
                "concurrency_throughput_bound": concurrency_bound,
                "bottleneck_throughput_bound": self._bottleneck_bound,
                "asymptotic_throughput_bound": bound,
                "binding_constraint": binding,
            }

        return results

    def check_prediction(self, user_count: float, predicted_throughput: float) -> Dict[str, Any]:
        """
        project_bound() for a single N, plus a direct comparison against
        a predicted throughput value at that N (e.g. a USL prediction).
        This is the entry point scalability.py uses to flag predictions
        that exceed what's theoretically possible given the monitored
        resource demands.
        """
        bound_at_n = self.project_bound([user_count])[float(user_count)]
        comparison = compare_to_observed(bound_at_n["asymptotic_throughput_bound"], predicted_throughput)
        return {**bound_at_n, **comparison}

    def _require_analyzed(self) -> None:
        if not self._analyzed:
            raise BottleneckCalculationError("analyze() must be called before this operation.")


# --------------------------------------------------------------------------
# Convenience functional wrappers
# --------------------------------------------------------------------------

def analyze_bottleneck(
    data: Dict[str, Any],
    capacity_config: Optional[ResourceCapacityConfig] = None,
    severity_thresholds: Optional[BottleneckSeverityThresholds] = None,
    signal_thresholds: Optional[BottleneckSignalThresholds] = None,
) -> Dict[str, Any]:
    """
    One-shot: validate + analyze a single snapshot. Raises if none of
    cpu_usage/disk_io/network_io are present. For project_bound()/
    check_prediction() (cross-checking forward predictions), construct
    a BottleneckAnalyzer directly and keep the instance around instead.
    """
    analyzer = BottleneckAnalyzer(
        capacity_config=capacity_config,
        severity_thresholds=severity_thresholds,
        signal_thresholds=signal_thresholds,
    )
    return analyzer.analyze(data)


def analyze_bottleneck_safe(data: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
    """
    Same as analyze_bottleneck(), except when none of
    cpu_usage/disk_io/network_io are present in `data`, it returns
    unavailable_bottleneck_result() instead of raising - keeps pipeline
    code running when resource metrics aren't wired up for a given call.
    """
    has_any_resource = isinstance(data, dict) and any(
        data.get(key) is not None for key in ("cpu_usage", "disk_io", "network_io")
    )

    if not has_any_resource:
        return unavailable_bottleneck_result(
            reason="No resource metrics ('cpu_usage', 'disk_io', or 'network_io') were provided.",
            throughput=data.get("throughput") if isinstance(data, dict) else None,
            metadata=data.get("metadata") if isinstance(data, dict) else None,
        )

    return analyze_bottleneck(data, **kwargs)