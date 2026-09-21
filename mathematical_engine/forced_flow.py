from __future__ import annotations

from typing import Any, Dict, List, Optional


class ForcedFlowValidationError(ValueError):
    """Bad input (not a real component-instrumentation payload)."""


class ForcedFlowCalculationError(RuntimeError):
    """A calculation received a mathematically invalid argument."""


# ==========================================================================
# What this module does
# ==========================================================================
#
# The Forced Flow Law - X_i = X * V_i - and its companion, D_i = V_i * S_i,
# let this pipeline reason about components that don't have an OS-level
# %utilization metric to build on: a database, a cache, an external API.
# bottleneck.py's Utilization Law (D_i = U_i / X) works for CPU/disk/
# network because Docker's stats API reports %utilization directly. It
# has no equivalent for "how loaded is the database" - db_monitor.py
# reports connection-pool pressure, not a %utilization figure Utilization
# Law could consume. Forced Flow sidesteps that entirely: if you know how
# many times a single system-level request visits a component (V_i) and
# how long each visit takes on average (S_i), you get that component's
# service demand directly, with no %utilization reading needed at all.
#
# This is why forced_flow.py is its own file rather than folded into
# bottleneck.py: it has a completely different data requirement (visits/
# request + per-visit service time, typically from application-level
# instrumentation or query logs) that usually ISN'T available for an
# arbitrary cloned repository with no instrumentation added. When it
# isn't, this returns {"available": False, ...} cleanly rather than
# guessing - matching amdahl.py's _extract_bottleneck_data() pattern for
# the same kind of optional, sometimes-absent input.
#
# The resulting per-component service_demand values are meant to be
# merged into bottleneck.py's own resource/demand table - see
# to_bottleneck_resources() below. Once merged, amdahl.py, capacity.py,
# and everything else downstream can reason about "the database" exactly
# like they already reason about "CPU", with zero component-specific
# logic anywhere else in the pipeline.


DEFAULT_THROUGHPUT_TOLERANCE = 0.30


# --- Core equations (pure functions, no state) ---

def calculate_component_throughput(system_throughput: float, visits_per_request: float) -> float:
    """
    X_i = X * V_i - the Forced Flow Law itself. How often this component
    is actually invoked per second, given the system's overall measured
    throughput and how many times each system-level request visits it.
    """
    if system_throughput < 0:
        raise ForcedFlowCalculationError("system_throughput must not be negative.")
    if visits_per_request < 0:
        raise ForcedFlowCalculationError("visits_per_request must not be negative.")
    return system_throughput * visits_per_request


def calculate_service_demand(visits_per_request: float, service_time_seconds: float) -> float:
    """
    D_i = V_i * S_i - this component's total service demand per system-
    level request, in seconds. Directly comparable to (and mergeable
    with) bottleneck.py's Utilization-Law-derived D_i for CPU/disk/network,
    since both express "seconds of this resource consumed per request".
    """
    if visits_per_request < 0:
        raise ForcedFlowCalculationError("visits_per_request must not be negative.")
    if service_time_seconds < 0:
        raise ForcedFlowCalculationError("service_time_seconds must not be negative.")
    return visits_per_request * service_time_seconds


# --- Observed-vs-predicted comparison ---

def compare_component_throughput(
    calculated_throughput: float,
    observed_throughput: Optional[float],
    tolerance: float = DEFAULT_THROUGHPUT_TOLERANCE,
) -> Optional[Dict[str, Any]]:
    """
    Compare Forced Flow's predicted component throughput (X_i = X*V_i)
    against an actually-measured one - e.g. Redis's own
    instantaneous_ops_per_sec from db_monitor.py's INFO-based sampling.
    Persistent disagreement usually means visits_per_request was
    estimated rather than directly measured, or this component is
    visited at an uneven rate rather than a constant multiple of the
    system-level request rate (e.g. a cache with a warm-up period, or a
    background job that also hits the same database independent of
    incoming requests).
    """
    if observed_throughput is None:
        return None
    if (
        not isinstance(observed_throughput, (int, float))
        or isinstance(observed_throughput, bool)
        or observed_throughput < 0
    ):
        raise ForcedFlowValidationError("observed_throughput must be a non-negative number.")
    if not 0 < tolerance:
        raise ForcedFlowValidationError("tolerance must be greater than zero.")

    absolute_difference = calculated_throughput - observed_throughput
    relative_difference = absolute_difference / observed_throughput if observed_throughput != 0 else None
    consistent = abs(relative_difference) <= tolerance if relative_difference is not None else None

    return {
        "calculated_throughput": round(calculated_throughput, 4),
        "observed_throughput": observed_throughput,
        "absolute_difference": round(absolute_difference, 4),
        "relative_difference": round(relative_difference, 4) if relative_difference is not None else None,
        "tolerance": tolerance,
        "consistent": consistent,
        "note": (
            "Predicted component throughput diverges from the actually-measured rate by more "
            "than the tolerance - visits_per_request may be an estimate rather than a direct "
            "measurement, or this component isn't visited at a uniform rate per system-level "
            "request."
        ) if consistent is False else None,
    }


# --- Analyzer ---

class ForcedFlowAnalyzer:
    """
    Turns per-component visits/request + service-time instrumentation
    into component-level throughput and service demand. Entirely
    optional: with no usable instrumentation, analyze() returns
    {"available": False, "reason": ...} rather than guessing or raising -
    this is expected to be missing for most arbitrary cloned repositories,
    which have no query-level instrumentation added.
    """

    def __init__(self, throughput_tolerance: float = DEFAULT_THROUGHPUT_TOLERANCE) -> None:
        if (
            not isinstance(throughput_tolerance, (int, float))
            or isinstance(throughput_tolerance, bool)
            or throughput_tolerance <= 0
        ):
            raise ForcedFlowValidationError("throughput_tolerance must be a positive number.")
        self.throughput_tolerance = float(throughput_tolerance)

    def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Args:
            data: {
                "system_throughput": float,  # X - overall measured system throughput (req/s)
                "components": {
                    name: {
                        "visits_per_request": float,    # V_i
                        "service_time_seconds": float,  # S_i, average time per single visit
                        "observed_throughput": float,   # optional - enables the predicted-vs-observed check
                    },
                    ...
                },
            }

        Returns:
            {"available": False, "reason": ...} if there's no usable
            component-level instrumentation at all, otherwise a dict with
            per-component results (component_throughput, service_demand,
            throughput_comparison), ranked_by_demand, and notes.
        """
        if not isinstance(data, dict):
            return {
                "available": False,
                "reason": "Forced Flow analysis requires a dict of component instrumentation data.",
            }

        system_throughput = data.get("system_throughput")
        if (
            not isinstance(system_throughput, (int, float))
            or isinstance(system_throughput, bool)
            or system_throughput < 0
        ):
            return {
                "available": False,
                "reason": (
                    "No valid system_throughput (X) was supplied - Forced Flow requires the "
                    "overall measured system throughput to compute component-level throughput."
                ),
            }

        components = data.get("components")
        if not isinstance(components, dict) or not components:
            return {
                "available": False,
                "reason": (
                    "No component-level instrumentation was detected (visits/request and service "
                    "time per component) - this typically comes from application-level "
                    "instrumentation or query logs, which most cloned repositories don't have."
                ),
            }

        per_component: Dict[str, Any] = {}
        notes: List[str] = []

        for name, info in components.items():
            if not isinstance(info, dict):
                notes.append(f"Component '{name}' skipped: expected a dict of instrumentation values.")
                continue

            visits = info.get("visits_per_request")
            service_time = info.get("service_time_seconds")

            if not isinstance(visits, (int, float)) or isinstance(visits, bool):
                notes.append(f"Component '{name}' skipped: 'visits_per_request' is missing or not numeric.")
                continue
            if not isinstance(service_time, (int, float)) or isinstance(service_time, bool):
                notes.append(f"Component '{name}' skipped: 'service_time_seconds' is missing or not numeric.")
                continue

            try:
                component_throughput = calculate_component_throughput(system_throughput, visits)
                service_demand = calculate_service_demand(visits, service_time)
            except ForcedFlowCalculationError as exc:
                notes.append(f"Component '{name}' skipped: {exc}")
                continue

            observed_throughput = info.get("observed_throughput")
            try:
                comparison = compare_component_throughput(
                    component_throughput, observed_throughput, tolerance=self.throughput_tolerance
                )
            except ForcedFlowValidationError as exc:
                notes.append(f"Component '{name}': observed_throughput ignored: {exc}")
                comparison = None

            per_component[name] = {
                "component": name,
                "visits_per_request": visits,
                "service_time_seconds": service_time,
                "component_throughput": round(component_throughput, 4),
                "service_demand": round(service_demand, 6),
                "throughput_comparison": comparison,
            }

        if not per_component:
            return {
                "available": False,
                "reason": "No component had usable visits_per_request/service_time_seconds data.",
            }

        ranked_by_demand = sorted(
            per_component.keys(), key=lambda n: per_component[n]["service_demand"], reverse=True
        )

        return {
            "available": True,
            "system_throughput": system_throughput,
            "per_component": per_component,
            "ranked_by_demand": ranked_by_demand,
            "dominant_component": ranked_by_demand[0] if ranked_by_demand else None,
            "notes": notes,
        }


def to_bottleneck_resources(forced_flow_result: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Reshape analyze()'s per_component output into the same
    {name: {"service_demand": ...}} shape bottleneck.py's own resources
    dict uses (see amdahl.py's _extract_bottleneck_data(), which already
    expects exactly this shape from bottleneck.py) - so bottleneck.py can
    merge component-level demand (database, cache, external API)
    alongside its own CPU/disk/network entries with a single dict.update(),
    no component-specific branching needed anywhere downstream.

    Returns {} if forced_flow_result["available"] is False - merging an
    empty dict is always safe, so callers don't need a separate
    availability check before merging this in.
    """
    if not isinstance(forced_flow_result, dict) or not forced_flow_result.get("available"):
        return {}

    return {
        name: {"service_demand": info["service_demand"], "source": "forced_flow"}
        for name, info in forced_flow_result.get("per_component", {}).items()
    }


def analyze_forced_flow(
    data: Dict[str, Any],
    throughput_tolerance: float = DEFAULT_THROUGHPUT_TOLERANCE,
) -> Dict[str, Any]:
    """One-shot: construct a ForcedFlowAnalyzer and run analyze()."""
    return ForcedFlowAnalyzer(throughput_tolerance=throughput_tolerance).analyze(data)
