from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple


class DescriptorBuildError(ValueError):
    """Bad input to build_application_descriptor() - a detection_result
    that isn't a dict, or a later-stage result that disagrees with
    detection_result on which framework was detected."""


SCHEMA_VERSION = "1.0"

_CONFIDENCE_RANK: Dict[Optional[str], int] = {"None": 0, None: 0, "Low": 1, "Medium": 2, "High": 3}
_RANK_TO_CONFIDENCE = {0: "None", 1: "Low", 2: "Medium", 3: "High"}


# ==========================================================================
# What this module does
# ==========================================================================
#
# Every automation-layer module (framework_detector, entry_point_locator,
# route_discovery, docker_manager, and the upcoming dependency_detector)
# returns its own dict shape, evolved independently. That's fine for the
# automation layer itself, but it means anything downstream (the
# mathematical engine, the frontend, a future report generator) that
# wants "the port" or "the entry point" has to know which of five
# different dicts to dig into, and under which key.
#
# ApplicationDescriptor is the one normalized object those consumers are
# meant to use instead - built ONCE from whichever stage results are
# available so far, so nothing downstream needs any framework-specific
# knowledge at all.
#
# It is intentionally NOT required to be built only at the end of the
# pipeline. Every input except detection_result is optional, so it can
# be built (or rebuilt) at any checkpoint - right after entry-point
# resolution, again after routes are discovered, again after the
# container starts - always producing a complete, if partially empty,
# descriptor. There's no incremental "update" API beyond just calling
# build_application_descriptor() again with more arguments filled in;
# every input here is already a cheap, already-computed dict, so there's
# nothing to optimize by avoiding a rebuild.
#
# Matches the rest of the automation layer's public-API convention:
# a dataclass internally for structure, but the public entry point
# (build_application_descriptor) returns a plain dict, directly
# jsonify()-able in Flask with no custom encoder.


@dataclass(frozen=True)
class ApplicationDescriptor:
    schema_version: str

    framework: Optional[str]
    language: Optional[str]
    support_tier: Optional[str]

    entry_point_file: Optional[str]
    entry_point_directory: Optional[str]
    entry_point_module: Optional[str]
    app_variable: Optional[str]
    build_tool: Optional[str]
    run_command_hint: Optional[str]

    container_port: Optional[int]
    host: Optional[str]
    host_port: Optional[int]
    container_id: Optional[str]
    docker_image_name: Optional[str]
    dockerfile_generated: Optional[bool]

    routes: List[Dict[str, Any]]
    route_count: int
    route_discovery_tier: Optional[str]

    database: Optional[Dict[str, Any]]
    cache: Optional[Dict[str, Any]]
    other_dependencies: List[Dict[str, Any]]

    overall_confidence: str
    ambiguous: bool
    readiness: Dict[str, bool]
    notes: List[Dict[str, str]]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ApplicationDescriptor":
        """Reconstruct a descriptor from a previously-serialized to_dict()
        result - e.g. loading a saved run from history/regression storage.
        Does not re-validate against live stage results; it trusts the
        stored data was produced by this same class."""
        return cls(**{f: data.get(f) for f in cls.__dataclass_fields__})


def _check_framework_agreement(reference: Optional[str], candidate_result: Optional[Dict[str, Any]], candidate_field: str, candidate_label: str) -> None:
    if candidate_result is None:
        return
    if not isinstance(candidate_result, dict):
        raise DescriptorBuildError(f"{candidate_label} must be a dict, got {type(candidate_result).__name__}.")
    candidate_framework = candidate_result.get(candidate_field)
    if candidate_framework != reference:
        raise DescriptorBuildError(
            f"{candidate_label} disagrees with detection_result on the detected framework "
            f"({candidate_framework!r} vs {reference!r}) - make sure it was produced from this same run."
        )


def _rollup_confidence(*confidences: Optional[str]) -> str:
    """Weakest-link confidence across every stage that reported one -
    the descriptor's single confidence figure is only as trustworthy as
    its least confident input, not its most confident one."""
    present = [c for c in confidences if c is not None]
    if not present:
        return "None"
    worst_rank = min(_CONFIDENCE_RANK.get(c, 0) for c in present)
    return _RANK_TO_CONFIDENCE[worst_rank]


def _collect_notes(*sources: Tuple[str, Optional[List[str]]]) -> List[Dict[str, str]]:
    merged: List[Dict[str, str]] = []
    for stage, notes in sources:
        for message in (notes or []):
            merged.append({"stage": stage, "message": message})
    return merged


def build_application_descriptor(
    detection_result: Dict[str, Any],
    location_result: Optional[Dict[str, Any]] = None,
    route_discovery_result: Optional[Dict[str, Any]] = None,
    docker_build_result: Optional[Dict[str, Any]] = None,
    docker_run_result: Optional[Dict[str, Any]] = None,
    dependency_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Assemble the normalized application descriptor from whichever
    pipeline stage results are available so far. Only detection_result
    is required; everything else may be None if that stage hasn't run
    yet, and the corresponding descriptor fields are simply left empty.

    Args:
        detection_result: output of framework_detector.detect_framework().
        location_result: output of entry_point_locator.locate_entry_point()
            for this same detection_result, if entry-point resolution has run.
        route_discovery_result: output of route_discovery.discover_routes()
            directly (the dict with "framework"/"routes"/"route_count"/
            "support_tier" keys) - NOT app.py's /api/routes response
            wrapper, which nests this same dict one level deeper under
            its own "routes" key. Pass result["routes"] from that HTTP
            response if that's what you have.
        docker_build_result: output of DockerManager.build_image(), if the
            image has been built.
        docker_run_result: output of DockerManager.run_container(), if the
            container has been started.
        dependency_result: output of the upcoming dependency_detector.py -
            expected shape:
                {
                  "database": {"type": str, "source": str, "confidence": str} | None,
                  "cache": {"type": str, "source": str, "confidence": str} | None,
                  "other_dependencies": [{"name": str, "source": str}, ...],
                  "notes": [str, ...],
                }
            None until that module exists; database/cache are simply
            reported as not-detected in that case, matching this
            pipeline's existing pattern of degrading gracefully rather
            than blocking on optional data.

    Returns:
        The descriptor as a plain dict (ApplicationDescriptor.to_dict()).

    Raises:
        DescriptorBuildError: detection_result isn't a dict, or a later
            stage result disagrees with it on the detected framework.
    """
    if not isinstance(detection_result, dict):
        raise DescriptorBuildError(f"detection_result must be a dict, got {type(detection_result).__name__}.")

    framework = detection_result.get("detected_framework")

    _check_framework_agreement(framework, location_result, "framework", "location_result")
    _check_framework_agreement(framework, route_discovery_result, "framework", "route_discovery_result")

    location_result = location_result or {}
    route_discovery_result = route_discovery_result or {}
    docker_build_result = docker_build_result or {}
    docker_run_result = docker_run_result or {}
    dependency_result = dependency_result or {}

    routes = route_discovery_result.get("routes", [])
    database = dependency_result.get("database")
    cache = dependency_result.get("cache")

    readiness = {
        "framework_detected": framework is not None,
        "entry_point_resolved": bool(location_result.get("entry_point_file") or location_result.get("run_command_hint")),
        "routes_discovered": len(routes) > 0,
        "docker_image_built": bool(docker_build_result.get("success")),
        "container_running": bool(docker_run_result.get("success")),
        "dependency_data_available": bool(dependency_result),
    }
    readiness["ready_for_load_testing"] = (
        readiness["container_running"] and readiness["routes_discovered"]
    )

    overall_confidence = _rollup_confidence(
        detection_result.get("confidence"),
        location_result.get("confidence"),
    )
    ambiguous = bool(detection_result.get("ambiguous")) or bool(location_result.get("ambiguous"))

    notes = _collect_notes(
        ("framework_detection", detection_result.get("notes")),
        ("entry_point_resolution", location_result.get("notes")),
        ("route_discovery", route_discovery_result.get("notes")),
        ("docker_build", docker_build_result.get("generation_notes")),
        ("dependency_detection", dependency_result.get("notes")),
    )

    descriptor = ApplicationDescriptor(
        schema_version=SCHEMA_VERSION,
        framework=framework,
        language=detection_result.get("language"),
        support_tier=detection_result.get("support_tier"),
        entry_point_file=location_result.get("entry_point_file"),
        entry_point_directory=location_result.get("entry_point_directory"),
        entry_point_module=location_result.get("entry_point_module"),
        app_variable=location_result.get("app_variable"),
        build_tool=location_result.get("build_tool"),
        run_command_hint=location_result.get("run_command_hint"),
        container_port=docker_build_result.get("container_port"),
        host=docker_run_result.get("host"),
        host_port=docker_run_result.get("port"),
        container_id=docker_run_result.get("container_id"),
        docker_image_name=docker_build_result.get("image_name") or docker_run_result.get("image_name"),
        dockerfile_generated=docker_build_result.get("dockerfile_generated"),
        routes=routes,
        route_count=len(routes),
        route_discovery_tier=route_discovery_result.get("support_tier"),
        database=database,
        cache=cache,
        other_dependencies=dependency_result.get("other_dependencies", []),
        overall_confidence=overall_confidence,
        ambiguous=ambiguous,
        readiness=readiness,
        notes=notes,
    )

    return descriptor.to_dict()
