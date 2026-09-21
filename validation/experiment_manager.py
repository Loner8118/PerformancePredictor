from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class ExperimentError(ValueError):
    """Bad input, or an operation that would silently overwrite existing
    experiment evidence."""


# ==========================================================================
# What this module does
# ==========================================================================
#
# Owns the experiments/EXPxxx/ directory: creates it, writes metadata.json
# with the reproducibility information every experimental-methodology
# section of this project's own planning asked for (git commit, machine
# info, software versions, load-test config), and hands back canonical
# paths for every other artifact (model_results.json,
# predictions.json/validation_actual.json/validated_result.json from
# prediction_validation.py, ablation_result.json, the raw Locust CSVs
# directory, report.pdf) so no other module has to hardcode a directory
# layout or guess a filename.
#
# metadata.json deliberately distinguishes TWO different commits:
#   - tool_commit: which version of THIS analysis pipeline produced the
#     experiment (auto-detected from THIS repository's own git history).
#   - target_commit: which commit of the APPLICATION BEING ANALYZED was
#     tested - this pipeline analyzes arbitrary external repositories, so
#     "the commit" is ambiguous without this distinction; conflating them
#     would make an experiment impossible to reproduce correctly (you'd
#     know the pipeline version but not what was actually tested, or vice
#     versa).
#
# Every piece of machine/software detection below (git, platform info,
# Docker/Locust versions) is wrapped defensively - a missing `git`
# binary, an unreadable version string, or any other environmental
# surprise degrades that ONE field to null with a note in
# "detection_warnings", rather than failing metadata capture entirely.
# An experiment with slightly incomplete provenance is still far more
# valuable than no experiment at all.
#
# initialize() refuses to overwrite an existing experiment's
# metadata.json unless force=True is explicit - silently overwriting
# metadata for an experiment_id that already has predictions.json/
# validation_actual.json sitting next to it would leave those artifacts
# paired with the wrong provenance record, which is a reproducibility
# hazard, not a convenience.


def _write_json_atomic(path: str, data: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(temp_path, path)
    except BaseException:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        raise ExperimentError(f"File not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ExperimentError(f"Could not read {path} as JSON: {e}") from e
    if not isinstance(data, dict):
        raise ExperimentError(f"{path} does not contain a JSON object.")
    return data


def _run_command_raw(args: List[str]) -> Optional[str]:
    """
    Like _run_command, but preserves the distinction between "command
    failed" (None) and "command succeeded with empty output" (empty
    string) - needed for `git status --porcelain`, where empty output on
    success is the meaningful "working tree is clean" signal, not a
    failure to detect anything.
    """
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _run_command(args: List[str]) -> Optional[str]:
    """
    For commands where empty output on success is ALSO effectively a
    detection failure (a version string, a commit hash) - collapses
    empty-but-successful output to None too, unlike _run_command_raw.
    """
    return _run_command_raw(args) or None


def _detect_git_commit(repo_path: str) -> Optional[str]:
    return _run_command(["git", "-C", repo_path, "rev-parse", "HEAD"])


def _detect_git_dirty(repo_path: str) -> Optional[bool]:
    status = _run_command_raw(["git", "-C", repo_path, "status", "--porcelain"])
    if status is None:
        return None
    return len(status) > 0


def _detect_machine_info() -> Dict[str, Any]:
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python_version": sys.version.split()[0],
        "cpu_count": os.cpu_count(),
    }


def _detect_software_versions() -> Dict[str, Optional[str]]:
    versions: Dict[str, Optional[str]] = {"docker": None, "locust": None}

    docker_version = _run_command(["docker", "--version"])
    if docker_version:
        versions["docker"] = docker_version

    locust_version = _run_command([sys.executable, "-m", "locust", "--version"])
    if locust_version:
        versions["locust"] = locust_version

    return versions


class Experiment:
    """
    One experiment's directory, metadata, and canonical artifact paths.
    """

    ARTIFACT_FILENAMES = {
        "metadata": "metadata.json",
        "model_results": "model_results.json",
        "predictions": "predictions.json",
        "validation_actual": "validation_actual.json",
        "validated_result": "validated_result.json",
        "model_validation_result": "model_validation_result.json",
        "ablation_result": "ablation_result.json",
        "report": "report.pdf",
    }
    LOCUST_RESULTS_DIRNAME = "locust_results"

    def __init__(self, base_dir: str, experiment_id: str) -> None:
        if not experiment_id:
            raise ExperimentError("experiment_id must not be empty.")
        self.base_dir = base_dir
        self.experiment_id = experiment_id
        self.dir = os.path.join(base_dir, experiment_id)

    @property
    def paths(self) -> Dict[str, str]:
        """Canonical path for every artifact this experiment can hold,
        plus the raw-Locust-CSV subdirectory - other modules should read
        from here rather than constructing filenames themselves."""
        result = {name: os.path.join(self.dir, filename) for name, filename in self.ARTIFACT_FILENAMES.items()}
        result["locust_results_dir"] = os.path.join(self.dir, self.LOCUST_RESULTS_DIRNAME)
        result["dir"] = self.dir
        return result

    def exists(self) -> bool:
        return os.path.isfile(self.paths["metadata"])

    def initialize(
        self,
        target_repository: Optional[str] = None,
        target_commit: Optional[str] = None,
        framework: Optional[str] = None,
        docker_image: Optional[str] = None,
        load_test_config: Optional[Dict[str, Any]] = None,
        tool_repo_path: Optional[str] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """
        Creates the experiment directory and writes metadata.json.

        Args:
            target_repository: the analyzed application's repository URL,
                if known (e.g. from github_manager.py's clone result).
            target_commit: the analyzed application's commit SHA - pass
                this explicitly rather than relying on auto-detection,
                since this pipeline clones arbitrary external
                repositories and has no fixed location to look in.
            framework: the detected framework (e.g. from
                framework_detector.detect_framework()'s
                "detected_framework").
            docker_image: the image name used for the analyzed
                application's container, if known.
            load_test_config: {"user_levels", "spawn_rate", "run_time",
                "repetitions"} or similar - whatever locust_runner.run()
                was actually called with.
            tool_repo_path: path to THIS pipeline's own repository, for
                tool_commit auto-detection via `git rev-parse HEAD`.
                Defaults to the current working directory.
            force: overwrite an existing metadata.json for this
                experiment_id. Default False - see module overview for
                why overwriting silently is a reproducibility hazard.

        Returns:
            The metadata record written to disk.

        Raises:
            ExperimentError: metadata.json already exists and force is False.
        """
        if self.exists() and not force:
            raise ExperimentError(
                f"Experiment {self.experiment_id!r} already has metadata.json at "
                f"{self.paths['metadata']} - pass force=True to knowingly overwrite it, "
                f"or use a different experiment_id."
            )

        detection_warnings: List[str] = []

        tool_commit = _detect_git_commit(tool_repo_path or os.getcwd())
        if tool_commit is None:
            detection_warnings.append("Could not detect tool_commit (git not available, or not a git repository).")

        tool_dirty = _detect_git_dirty(tool_repo_path or os.getcwd())
        if tool_dirty is None:
            detection_warnings.append("Could not determine whether the tool repository has uncommitted changes.")

        software_versions = _detect_software_versions()
        if software_versions["docker"] is None:
            detection_warnings.append("Could not detect Docker version (docker not on PATH, or not runnable).")
        if software_versions["locust"] is None:
            detection_warnings.append("Could not detect Locust version (locust not importable in this environment).")

        metadata = {
            "experiment_id": self.experiment_id,
            "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool_commit": tool_commit,
            "tool_repository_dirty": tool_dirty,
            "target_repository": target_repository,
            "target_commit": target_commit,
            "framework": framework,
            "docker_image": docker_image,
            "load_test_config": load_test_config,
            "machine_info": _detect_machine_info(),
            "software_versions": software_versions,
            "detection_warnings": detection_warnings,
        }

        _write_json_atomic(self.paths["metadata"], metadata)
        os.makedirs(self.paths["locust_results_dir"], exist_ok=True)

        if detection_warnings:
            print(f"Experiment {self.experiment_id!r} initialized with incomplete provenance:")
            for warning in detection_warnings:
                print(f"  - {warning}")
        else:
            print(f"Experiment {self.experiment_id!r} initialized at {self.dir}")

        return metadata

    def load_metadata(self) -> Dict[str, Any]:
        return _load_json(self.paths["metadata"])

    def save_artifact(self, artifact_name: str, data: Dict[str, Any]) -> str:
        """
        Write an arbitrary artifact (model_results, ablation_result,
        etc.) to its canonical path. Prefer calling the producing
        module's own writer (e.g. prediction_validation.freeze_predictions())
        directly where one exists - this is for artifacts (model_results,
        ablation_result, model_validation_result) that don't have a
        dedicated writer of their own.
        """
        if artifact_name not in self.ARTIFACT_FILENAMES:
            raise ExperimentError(
                f"Unknown artifact {artifact_name!r}. Known artifacts: "
                f"{sorted(self.ARTIFACT_FILENAMES)}."
            )
        path = self.paths[artifact_name]
        _write_json_atomic(path, data)
        return path

    def load_artifact(self, artifact_name: str) -> Dict[str, Any]:
        if artifact_name not in self.ARTIFACT_FILENAMES:
            raise ExperimentError(
                f"Unknown artifact {artifact_name!r}. Known artifacts: "
                f"{sorted(self.ARTIFACT_FILENAMES)}."
            )
        return _load_json(self.paths[artifact_name])


def list_experiments(base_dir: str) -> List[str]:
    """
    Lists experiment_ids that have a valid metadata.json under base_dir,
    sorted by creation time (oldest first) where that's recoverable, else
    alphabetically - for a report.py that wants to enumerate what's
    available to aggregate across.
    """
    if not os.path.isdir(base_dir):
        return []

    candidates = []
    for entry in sorted(os.listdir(base_dir)):
        metadata_path = os.path.join(base_dir, entry, Experiment.ARTIFACT_FILENAMES["metadata"])
        if os.path.isfile(metadata_path):
            try:
                metadata = _load_json(metadata_path)
                created_at = metadata.get("created_at_utc", "")
            except ExperimentError:
                created_at = ""
            candidates.append((created_at, entry))

    candidates.sort()
    return [entry for _, entry in candidates]
