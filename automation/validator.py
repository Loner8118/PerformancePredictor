from __future__ import annotations

import ast
import os
from typing import Any, Dict, List, Optional, Tuple

from automation.framework_detector import FRAMEWORK_DEFINITIONS, FrameworkDefinition, detect_framework, FrameworkDefinition
from automation.entry_point_locator import locate_entry_point


class ValidationInputError(ValueError):
    """
    Bad input to validate() - a malformed project_path, or a
    detection_result/location_result that wasn't produced by
    framework_detector.py / entry_point_locator.py for this same repo.

    This is distinct from a *validation failure* (returned as
    (False, errors, warnings)): that means "this repository has a real
    problem"; this exception means "this function was called wrong".
    Matches the same raise-vs-return split framework_detector.py and
    entry_point_locator.py already use.
    """


_DEFINITIONS_BY_KEY: Dict[str, FrameworkDefinition] = {d.key: d for d in FRAMEWORK_DEFINITIONS}


# ==========================================================================
# What this module does
# ==========================================================================
#
# The final gate before a repository enters the Docker/load-testing
# pipeline. Deliberately does NOT re-scan the filesystem - it consumes
# the already-computed outputs of framework_detector.detect_framework()
# and entry_point_locator.locate_entry_point(), and converts their
# (necessarily heuristic/probabilistic) findings into a hard pass/fail
# decision with specific, actionable messages.
#
# Per-framework requirements come from framework_detector.py's own
# FRAMEWORK_DEFINITIONS table (manifest filenames, dependency names,
# language) rather than a second, separately-maintained table - so the
# two files can't drift out of sync as frameworks are added.
#
# Two severities, not one:
#   - errors:   block the pipeline (valid=False). Reserved for things
#     with no reasonable fallback: no framework detected, no dependency
#     manifest on disk, no resolvable entry point, invalid syntax in the
#     entry point.
#   - warnings: don't block, but are worth surfacing. This is where a
#     missing Dockerfile now lives (docker_manager.py can auto-generate
#     one - see the automation upgrade plan), along with low-confidence
#     detection, ambiguous results, and best-effort-tier frameworks.
#     Treating "no Dockerfile" as fatal, as the previous Flask-only
#     version did, would defeat the point of adding auto-generation.


class RepositoryValidator:
    """Validates a cloned repository against its detected framework's
    requirements before it enters the Docker and load-testing pipeline."""

    def __init__(self, project_path: str) -> None:
        self.project_path = os.path.abspath(project_path)

    def validate(
        self,
        detection_result: Dict[str, Any],
        location_result: Dict[str, Any],
    ) -> Tuple[bool, List[str], List[str]]:
        """
        Args:
            detection_result: output of framework_detector.detect_framework(project_path).
            location_result: output of entry_point_locator.locate_entry_point(project_path, detection_result) -
                must be the result for this SAME detection_result (checked below).

        Returns:
            (valid, errors, warnings). valid is True iff errors is empty -
            warnings never affect it.

        Raises:
            ValidationInputError: project_path is missing/not a directory,
                detection_result/location_result are malformed, or the two
                don't agree on which framework was detected.
        """
        if not os.path.exists(self.project_path):
            raise ValidationInputError(f"Project directory does not exist: {self.project_path}")
        if not os.path.isdir(self.project_path):
            raise ValidationInputError(f"Project path is not a directory: {self.project_path}")

        if not isinstance(detection_result, dict) or "detected_framework" not in detection_result:
            raise ValidationInputError(
                "detection_result must be the dict returned by framework_detector.detect_framework()."
            )
        if not isinstance(location_result, dict) or "framework" not in location_result:
            raise ValidationInputError(
                "location_result must be the dict returned by entry_point_locator.locate_entry_point()."
            )
        if location_result.get("framework") != detection_result.get("detected_framework"):
            raise ValidationInputError(
                "detection_result and location_result disagree on the detected framework "
                f"({detection_result.get('detected_framework')!r} vs {location_result.get('framework')!r}) - "
                f"make sure location_result was produced from this same detection_result."
            )

        errors: List[str] = []
        warnings: List[str] = []

        framework_key = detection_result.get("detected_framework")
        if framework_key is None:
            errors.append(
                "No supported framework (Flask, FastAPI, Django, Express.js, or Spring Boot) "
                "could be detected in this repository."
            )
            return False, errors, warnings

        definition = _DEFINITIONS_BY_KEY.get(framework_key)
        if definition is None:
            errors.append(f"'{framework_key}' was detected but has no registered validation rules.")
            return False, errors, warnings

        self._check_detection_confidence(detection_result, definition, warnings)
        self._check_dependency_declared(detection_result, definition, warnings)
        self._check_manifest_exists(detection_result, definition, errors)
        self._check_entry_point(location_result, definition, errors, warnings)
        self._check_dockerfile(definition, warnings)

        return len(errors) == 0, errors, warnings

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    @staticmethod
    def _check_detection_confidence(
        detection_result: Dict[str, Any],
        definition: FrameworkDefinition,
        warnings: List[str],
    ) -> None:
        if detection_result.get("confidence") == "Low":
            warnings.append(
                f"Framework detection confidence is Low for {definition.display_name} - "
                f"proceed with caution; the Docker build stage is the real test."
            )
        if detection_result.get("ambiguous"):
            warnings.append(
                f"Framework detection was ambiguous - {definition.display_name} scored highest, but "
                f"another framework scored closely. This may be a multi-service repository "
                f"(e.g. separate frontend/backend)."
            )
        if detection_result.get("support_tier") == "best-effort":
            warnings.append(
                f"{definition.display_name} has best-effort support in this pipeline - route "
                f"discovery and entry-point resolution are less rigorous than for "
                f"Flask/FastAPI/Express."
            )

    @staticmethod
    def _check_dependency_declared(
        detection_result: Dict[str, Any],
        definition: FrameworkDefinition,
        warnings: List[str],
    ) -> None:
        dependency_declared = any(
            signal.get("name") == "dependency_declared"
            for candidate in detection_result.get("candidates", [])
            if candidate.get("framework") == definition.key
            for signal in candidate.get("signals", [])
        )
        if not dependency_declared:
            warnings.append(
                f"{definition.display_name} was detected structurally, but no explicit dependency "
                f"declaration was found in a manifest file - dependency installation may fail "
                f"during the Docker build."
            )

    def _check_manifest_exists(
        self,
        detection_result: Dict[str, Any],
        definition: FrameworkDefinition,
        errors: List[str],
    ) -> None:
        manifest_files_found = detection_result.get("scan_summary", {}).get("manifest_files_found", {})

        for manifest_name in definition.manifest_extensions:
            for rel_path in manifest_files_found.get(manifest_name, []):
                if os.path.isfile(os.path.join(self.project_path, rel_path)):
                    return  # at least one required manifest genuinely exists on disk

        if definition.manifest_extensions:
            errors.append(
                f"No dependency manifest ({', '.join(definition.manifest_extensions)}) was found "
                f"on disk for {definition.display_name}."
            )

    def _check_entry_point(
        self,
        location_result: Dict[str, Any],
        definition: FrameworkDefinition,
        errors: List[str],
        warnings: List[str],
    ) -> None:
        confidence = location_result.get("confidence")

        if confidence == "None":
            reason = "; ".join(location_result.get("notes", [])) or "no further detail available."
            errors.append(f"Could not resolve an entry point for {definition.display_name}: {reason}")
            return

        if confidence == "Low":
            warnings.append(
                f"Entry-point resolution confidence is Low - "
                f"{location_result.get('entry_point_file') or location_result.get('run_command_hint')!r} "
                f"is a best guess."
            )
        if location_result.get("ambiguous"):
            warnings.append(
                f"Multiple plausible entry-point files were found; chose "
                f"{location_result.get('entry_point_file')!r}. "
                f"Alternatives: {location_result.get('alternative_candidates', [])}"
            )

        entry_point_file = location_result.get("entry_point_file")

        if entry_point_file:
            entry_point_abs = os.path.join(self.project_path, entry_point_file)
            if not os.path.isfile(entry_point_abs):
                errors.append(f"Resolved entry point file does not exist on disk: {entry_point_file}")
                return

            if definition.language == "python":
                syntax_error = self._validate_python_syntax(entry_point_abs)
                if syntax_error:
                    errors.append(f"Invalid Python syntax in {entry_point_file}: {syntax_error}")
            else:
                warnings.append(
                    f"Syntax validation is not performed for {definition.language} entry points in "
                    f"this pipeline; syntax errors will surface as a Docker build failure instead."
                )

        elif location_result.get("run_command_hint"):
            # e.g. resolved via a Procfile: no single file, but a valid
            # run command - not an error.
            pass
        else:
            warnings.append(
                f"No specific entry-point file was pinpointed for {definition.display_name}; "
                f"pipeline stages that need one (e.g. route discovery) may not work."
            )

    def _check_dockerfile(self, definition: FrameworkDefinition, warnings: List[str]) -> None:
        # Informational only - a missing Dockerfile no longer blocks
        # validation, since docker_manager.py can auto-generate one from
        # a per-framework template.
        dockerfile_path = os.path.join(self.project_path, "Dockerfile")
        if os.path.isfile(dockerfile_path):
            warnings.append("Dockerfile found - will be used as-is.")
        else:
            warnings.append(
                f"No Dockerfile found for {definition.display_name} - one will be auto-generated "
                f"if the pipeline supports it for this framework."
            )

    @staticmethod
    def _validate_python_syntax(file_path: str) -> Optional[str]:
        """Check whether a Python file contains valid syntax. Returns None if valid, otherwise an error message."""
        try:
            with open(file_path, "r", encoding="utf-8") as file:
                source = file.read()
            ast.parse(source, filename=file_path)
            return None
        except SyntaxError as e:
            return str(e)
        except UnicodeDecodeError:
            return "File is not valid UTF-8 text."
        except OSError as e:
            return str(e)


def validate_repository(
    project_path: str,
    detector_kwargs: Optional[Dict[str, Any]] = None,
    locator_kwargs: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, List[str], List[str]]:
    """
    One-shot: runs framework detection, entry-point location, and
    validation in sequence for a single repository. Use this for
    standalone/simple calls; if the pipeline already ran
    detect_framework()/locate_entry_point() for other purposes (route
    discovery, Dockerfile generation), call RepositoryValidator.validate()
    directly with those cached results instead of re-scanning here.
    """
    detection_result = detect_framework(project_path, **(detector_kwargs or {}))
    location_result = locate_entry_point(project_path, detection_result, **(locator_kwargs or {}))
    return RepositoryValidator(project_path).validate(detection_result, location_result)