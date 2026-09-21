from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class EntryPointLocationError(ValueError):
    """Bad input (e.g. repo_path doesn't exist, or detection_result is malformed)."""


# ==========================================================================
# What this module does
# ==========================================================================
#
# framework_detector.py answers "what framework is this, and roughly
# which files look relevant". This module takes that output and resolves
# it down to ONE definitive, actionable answer per framework:
#
#   - entry_point_file      - the file to point route discovery / a run
#                              command at
#   - entry_point_directory - the directory a run command should execute
#                              FROM (not always the same directory as the
#                              entry file - e.g. Spring Boot is run from
#                              wherever pom.xml/build.gradle lives, not
#                              from the Application.java's directory)
#   - entry_point_module     - dotted module path, when meaningful
#                              (Flask/FastAPI: "backend.src.app";
#                              Django: the wsgi module)
#   - run_command_hint       - a best-guess command to run the app
#
# Each framework has a different "most authoritative" signal, checked
# before falling back to heuristic ranking:
#
#   - Express.js: package.json's "main" field or "scripts.start" - the
#     project author's own explicit declaration, more authoritative than
#     any code pattern.
#   - Flask/FastAPI: a Procfile's "web:" line, if present (same idea -
#     explicit author intent) - otherwise ranked by heuristics (a
#     `if __name__ == "__main__":` block is the strongest signal that a
#     given .py file is meant to be run directly).
#   - Django: manage.py's location IS the project root by convention -
#     there's rarely more than one in a real project, so this is close
#     to unambiguous once framework_detector has found it.
#   - Spring Boot: prefer a class matched by BOTH the
#     @SpringBootApplication annotation AND a SpringApplication.run(...)
#     call (framework_detector records these as separate signals; this
#     module cross-references which file satisfies both).
#
# Like framework_detector.py, this never claims more confidence than the
# evidence supports - confidence here is a SEPARATE question from
# framework_detector's own confidence ("is Flask the right framework" vs
# "is this specific file the right entry point"), and results say so
# explicitly (ambiguous=True, alternative_candidates) rather than
# silently picking one of several equally-plausible files.


DEFAULT_MAX_FILE_SIZE_BYTES = 200_000  # 200 KB - matches framework_detector.py's default


# --- Configuration ---

@dataclass
class LocatorWeights:
    main_block_present: float = 5.0       # `if __name__ == "__main__":` - strong "run me directly" signal
    app_instantiation_present: float = 5.0  # e.g. `app = Flask(__name__)` in the file - the most direct
                                             # evidence a file IS the app, not just related to it
    conventional_basename: float = 3.0    # app.py/main.py/index.js/etc.
    depth_penalty_per_level: float = 0.5  # shallower files are slightly preferred as a tiebreak


@dataclass
class LocatorConfidenceThresholds:
    high_min_score: float = 8.0
    medium_min_score: float = 3.0
    ambiguous_gap: float = 2.0


# --- Shared helpers ---

def _read_text_safe(absolute_path: str, max_bytes: int) -> str:
    try:
        with open(absolute_path, "rb") as f:
            raw = f.read(max_bytes)
    except OSError:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="ignore")


def _classify_locator_confidence(
    top_score: float,
    second_score: Optional[float],
    thresholds: LocatorConfidenceThresholds,
) -> Tuple[str, bool]:
    ambiguous = (
        second_score is not None
        and second_score > 0
        and (top_score - second_score) < thresholds.ambiguous_gap
    )
    if top_score >= thresholds.high_min_score and not ambiguous:
        return "High", ambiguous
    if top_score >= thresholds.medium_min_score:
        return "Medium", ambiguous
    return "Low", ambiguous


_MAIN_BLOCK_PATTERN = re.compile(r'if\s+__name__\s*==\s*[\'"]__main__[\'"]\s*:')
_FLASK_INSTANTIATION = re.compile(r"(\w+)\s*=\s*Flask\s*\(")
_FASTAPI_INSTANTIATION = re.compile(r"(\w+)\s*=\s*FastAPI\s*\(")
_NODE_SCRIPT_FILE_PATTERN = re.compile(r"([^\s\'\"]+\.(?:js|mjs|cjs|ts))\b")


def _extract_node_script_file(start_script: str) -> Optional[str]:
    """
    Pull the actual script path out of an npm "start" command - e.g.
    "node server.js", "node --inspect ./src/index.js", or
    "NODE_ENV=production node dist/main.js". Looks for a token ending in
    a JS/TS extension anywhere in the command rather than assuming it's
    the token immediately after "node", since flags (--inspect,
    --experimental-modules, etc.) commonly sit between the two.
    """
    match = _NODE_SCRIPT_FILE_PATTERN.search(start_script)
    return match.group(1) if match else None


# --- Flask / FastAPI resolution ---

def _read_procfile_web_command(repo_path_abs: str, max_bytes: int) -> Optional[str]:
    procfile_path = os.path.join(repo_path_abs, "Procfile")
    if not os.path.isfile(procfile_path):
        return None
    for line in _read_text_safe(procfile_path, max_bytes).splitlines():
        line = line.strip()
        if line.lower().startswith("web:"):
            return line.split(":", 1)[1].strip()
    return None


def _score_python_candidate(rel_path: str, content: str, weights: LocatorWeights, instantiation_pattern: "re.Pattern[str]") -> float:
    score = 0.0
    basename = os.path.basename(rel_path).lower()
    if instantiation_pattern.search(content):
        score += weights.app_instantiation_present
    if _MAIN_BLOCK_PATTERN.search(content):
        score += weights.main_block_present
    if basename in ("app.py", "main.py", "wsgi.py", "asgi.py", "run.py"):
        score += weights.conventional_basename
    score -= rel_path.count(os.sep) * weights.depth_penalty_per_level
    return score


def _resolve_flask_or_fastapi(
    framework: str,
    repo_path_abs: str,
    detection_result: Dict[str, Any],
    weights: LocatorWeights,
    max_file_size_bytes: int,
) -> Dict[str, Any]:
    notes: List[str] = []

    procfile_command = _read_procfile_web_command(repo_path_abs, max_file_size_bytes)
    if procfile_command:
        return {
            "entry_point_file": None,
            "entry_point_directory": ".",
            "entry_point_module": None,
            "app_variable": None,
            "build_tool": "pip",
            "run_command_hint": procfile_command,
            "resolution_method": "procfile",
            "confidence_override": "High",
            "alternatives": [],
            "notes": ["Resolved from Procfile's 'web:' process, which takes priority over heuristic detection."],
        }

    candidates = detection_result.get("entry_point_candidates") or []
    if not candidates:
        return {
            "entry_point_file": None,
            "entry_point_directory": None,
            "entry_point_module": None,
            "app_variable": None,
            "build_tool": "pip",
            "run_command_hint": None,
            "resolution_method": "none",
            "confidence_override": "None",
            "alternatives": [],
            "notes": [f"No candidate entry-point files were found for {framework}."],
        }

    scored = []
    instantiation_pattern = _FLASK_INSTANTIATION if framework == "flask" else _FASTAPI_INSTANTIATION
    for rel_path in candidates:
        content = _read_text_safe(os.path.join(repo_path_abs, rel_path), max_file_size_bytes)
        scored.append((rel_path, _score_python_candidate(rel_path, content, weights, instantiation_pattern), content))
    scored.sort(key=lambda item: item[1], reverse=True)

    best_path, best_score, best_content = scored[0]
    second_score = scored[1][1] if len(scored) > 1 else None

    var_match = instantiation_pattern.search(best_content)
    app_variable = var_match.group(1) if var_match else "app"
    if not var_match:
        display = "Flask" if framework == "flask" else "FastAPI"
        notes.append(
            f"Could not find an explicit {display}(...) instantiation in the chosen file; "
            f"assuming the app variable is named 'app'."
        )

    entry_point_module = os.path.splitext(best_path)[0].replace(os.sep, ".")
    entry_point_directory = os.path.dirname(best_path) or "."
    has_main_block = bool(_MAIN_BLOCK_PATTERN.search(best_content))

    if framework == "flask":
        if has_main_block and re.search(r"\.run\s*\(", best_content):
            run_command_hint = f"python {best_path}"
        else:
            run_command_hint = f"gunicorn {entry_point_module}:{app_variable}"
    else:
        if has_main_block and "uvicorn" in best_content:
            run_command_hint = f"python {best_path}"
        else:
            run_command_hint = f"uvicorn {entry_point_module}:{app_variable} --host 0.0.0.0 --port 8000"

    # A single candidate with any positive evidence isn't "uncertain" in
    # the way a heuristic pick among several plausible files is - there
    # was nothing else to weigh it against, so score-threshold-based
    # Medium/Low classification would understate confidence here.
    confidence_override = "High" if (second_score is None and best_score > 0) else None

    return {
        "entry_point_file": best_path,
        "entry_point_directory": entry_point_directory,
        "entry_point_module": entry_point_module,
        "app_variable": app_variable,
        "build_tool": "pip",
        "run_command_hint": run_command_hint,
        "resolution_method": "code_pattern_ranking" if len(scored) > 1 else "single_candidate",
        "confidence_override": confidence_override,
        "score": best_score,
        "second_score": second_score,
        "alternatives": [path for path, _, _ in scored[1:6]],
        "notes": notes,
    }


# --- Django resolution ---

def _resolve_django(
    repo_path_abs: str,
    detection_result: Dict[str, Any],
    max_file_size_bytes: int,
) -> Dict[str, Any]:
    candidates = detection_result.get("entry_point_candidates") or []
    manage_candidates = sorted(
        (c for c in candidates if os.path.basename(c) == "manage.py"),
        key=lambda p: (p.count(os.sep), p),
    )
    notes: List[str] = []

    if not manage_candidates:
        return {
            "entry_point_file": None,
            "entry_point_directory": None,
            "entry_point_module": None,
            "app_variable": None,
            "build_tool": "pip",
            "run_command_hint": None,
            "resolution_method": "none",
            "confidence_override": "None",
            "alternatives": [],
            "notes": ["No manage.py was found - cannot resolve a Django project root."],
        }

    manage_py = manage_candidates[0]
    project_root = os.path.dirname(manage_py) or "."

    wsgi_candidates = [c for c in candidates if os.path.basename(c) == "wsgi.py"]
    wsgi_module = None
    if wsgi_candidates:
        rel = os.path.relpath(wsgi_candidates[0], project_root) if project_root != "." else wsgi_candidates[0]
        wsgi_module = os.path.splitext(rel)[0].replace(os.sep, ".")
        notes.append(f"For production, consider: gunicorn {wsgi_module}:application (run from {project_root}).")

    asgi_candidates = [c for c in candidates if os.path.basename(c) == "asgi.py"]
    if asgi_candidates:
        rel = os.path.relpath(asgi_candidates[0], project_root) if project_root != "." else asgi_candidates[0]
        asgi_module = os.path.splitext(rel)[0].replace(os.sep, ".")
        notes.append(f"For ASGI (async) deployments, consider: uvicorn {asgi_module}:application (run from {project_root}).")

    confidence_override = "High"
    if len(manage_candidates) > 1:
        confidence_override = "Medium"
        notes.append(f"Multiple manage.py files were found; using the shallowest one ({manage_py}).")

    return {
        "entry_point_file": manage_py,
        "entry_point_directory": project_root,
        "entry_point_module": wsgi_module,
        "app_variable": None,
        "build_tool": "pip",
        "run_command_hint": "python manage.py runserver 0.0.0.0:8000",
        "resolution_method": "manage_py_convention",
        "confidence_override": confidence_override,
        "alternatives": manage_candidates[1:6],
        "notes": notes,
    }


# --- Express.js resolution ---

def _resolve_express(
    repo_path_abs: str,
    detection_result: Dict[str, Any],
    weights: LocatorWeights,
    max_file_size_bytes: int,
) -> Dict[str, Any]:
    manifest_files = detection_result.get("scan_summary", {}).get("manifest_files_found", {})
    package_json_paths = manifest_files.get("package.json", [])

    for pj_rel_path in package_json_paths:
        content = _read_text_safe(os.path.join(repo_path_abs, pj_rel_path), max_file_size_bytes)
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue

        pj_dir = os.path.dirname(pj_rel_path)
        scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
        start_script = scripts.get("start") if isinstance(scripts, dict) else None
        main_field = data.get("main")

        entry_file = None
        resolution_method = None
        if isinstance(main_field, str) and main_field.strip():
            entry_file = os.path.normpath(os.path.join(pj_dir, main_field.strip()))
            resolution_method = "package_json_main"
        elif isinstance(start_script, str):
            entry_file_rel = _extract_node_script_file(start_script)
            if entry_file_rel:
                entry_file = os.path.normpath(os.path.join(pj_dir, entry_file_rel))
                resolution_method = "package_json_start_script"

        run_command_hint = "npm start" if isinstance(start_script, str) else (f"node {entry_file}" if entry_file else None)

        if entry_file or run_command_hint:
            return {
                "entry_point_file": entry_file,
                "entry_point_directory": pj_dir or ".",
                "entry_point_module": None,
                "app_variable": None,
                "build_tool": "npm",
                "run_command_hint": run_command_hint,
                "resolution_method": resolution_method or "package_json_start_script",
                "confidence_override": "High",
                "alternatives": [],
                "notes": [],
            }

    # No usable package.json signal - fall back to framework_detector's
    # code-pattern candidates, ranked by filename convention + depth.
    candidates = detection_result.get("entry_point_candidates") or []
    if not candidates:
        return {
            "entry_point_file": None,
            "entry_point_directory": None,
            "entry_point_module": None,
            "app_variable": None,
            "build_tool": "npm",
            "run_command_hint": None,
            "resolution_method": "none",
            "confidence_override": "None",
            "alternatives": [],
            "notes": ["No package.json main/start script and no code-pattern candidates were found."],
        }

    def _score(rel_path: str) -> float:
        basename = os.path.basename(rel_path).lower()
        score = weights.conventional_basename if basename in ("index.js", "app.js", "server.js", "main.js") else 0.0
        return score - rel_path.count(os.sep) * weights.depth_penalty_per_level

    scored = sorted(((c, _score(c)) for c in candidates), key=lambda item: item[1], reverse=True)
    best_path, best_score = scored[0]
    second_score = scored[1][1] if len(scored) > 1 else None
    confidence_override = "High" if (second_score is None and len(candidates) == 1) else None

    return {
        "entry_point_file": best_path,
        "entry_point_directory": os.path.dirname(best_path) or ".",
        "entry_point_module": None,
        "app_variable": None,
        "build_tool": "npm",
        "run_command_hint": f"node {best_path}",
        "resolution_method": "code_pattern_ranking",
        "confidence_override": confidence_override,
        "score": best_score,
        "second_score": second_score,
        "alternatives": [c for c, _ in scored[1:6]],
        "notes": ["No package.json main/start field was usable; falling back to code-pattern heuristics."],
    }


# --- Spring Boot resolution ---

def _resolve_springboot(
    repo_path_abs: str,
    detection_result: Dict[str, Any],
    max_file_size_bytes: int,
) -> Dict[str, Any]:
    manifest_files = detection_result.get("scan_summary", {}).get("manifest_files_found", {})
    notes: List[str] = []

    if manifest_files.get("pom.xml"):
        build_tool = "maven"
        build_manifest_path = manifest_files["pom.xml"][0]
        run_command_hint = "mvn spring-boot:run"
    elif manifest_files.get("build.gradle") or manifest_files.get("build.gradle.kts"):
        build_tool = "gradle"
        build_manifest_path = (manifest_files.get("build.gradle") or manifest_files.get("build.gradle.kts"))[0]
        run_command_hint = "./gradlew bootRun"
    else:
        build_tool = None
        build_manifest_path = None
        run_command_hint = None

    # Run commands for Maven/Gradle execute from wherever the build
    # manifest lives - NOT from the Application.java's directory, which
    # is why this is tracked separately from entry_point_file below.
    project_root = (os.path.dirname(build_manifest_path) or ".") if build_manifest_path else "."

    # application.properties/.yml are tracked as structural markers by
    # framework_detector (useful there for server.port corroboration), so
    # they can show up in entry_point_candidates alongside real source
    # files - but a config file is never a runnable entry point, so only
    # .java files are eligible here.
    candidates = detection_result.get("entry_point_candidates") or []
    java_candidates = [c for c in candidates if c.endswith(".java")]

    if not java_candidates:
        return {
            "entry_point_file": None,
            "entry_point_directory": project_root,
            "entry_point_module": None,
            "app_variable": None,
            "build_tool": build_tool,
            "run_command_hint": run_command_hint,
            "resolution_method": "build_manifest_only",
            "confidence_override": "Low" if build_tool else "None",
            "alternatives": [],
            "notes": (
                ["No @SpringBootApplication class with a main method was pinpointed; "
                 "only the build manifest location is known."]
                if build_tool else ["No Spring Boot build manifest or application class was found."]
            ),
        }

    # Cross-reference framework_detector's per-signal evidence: prefer a
    # file matched by BOTH the annotation and the SpringApplication.run(...)
    # call over one matched by only one of the two.
    signal_files: Dict[str, Set[str]] = {}
    for candidate in detection_result.get("candidates", []):
        if candidate.get("framework") != "springboot":
            continue
        for signal in candidate.get("signals", []):
            if signal["name"].startswith("code_pattern:"):
                signal_files.setdefault(signal["name"], set()).add(signal["source_file"])

    annotation_files = signal_files.get("code_pattern:@SpringBootApplication", set())
    run_call_files = signal_files.get("code_pattern:SpringApplication.run", set())
    both = annotation_files & run_call_files

    if both:
        chosen = sorted(both, key=lambda p: (p.count(os.sep), p))[0]
        confidence_override = "High"
        resolution_method = "annotation_and_main_method"
    elif annotation_files:
        chosen = sorted(annotation_files, key=lambda p: (p.count(os.sep), p))[0]
        confidence_override = "Medium"
        resolution_method = "annotation_only"
        notes.append(
            "Found @SpringBootApplication but could not confirm a matching "
            "SpringApplication.run(...) call in the same file."
        )
    else:
        chosen = sorted(java_candidates, key=lambda p: (p.count(os.sep), p))[0]
        confidence_override = "Low"
        resolution_method = "candidate_fallback"

    return {
        "entry_point_file": chosen,
        "entry_point_directory": project_root,
        "entry_point_module": None,
        "app_variable": None,
        "build_tool": build_tool,
        "run_command_hint": run_command_hint,
        "resolution_method": resolution_method,
        "confidence_override": confidence_override,
        "alternatives": [p for p in java_candidates if p != chosen][:5],
        "notes": notes,
    }


# --- Locator ---

_Resolver = Callable[[str, Dict[str, Any], LocatorWeights, int], Dict[str, Any]]

_RESOLVERS: Dict[str, _Resolver] = {
    "flask": lambda repo, det, w, mfs: _resolve_flask_or_fastapi("flask", repo, det, w, mfs),
    "fastapi": lambda repo, det, w, mfs: _resolve_flask_or_fastapi("fastapi", repo, det, w, mfs),
    "django": lambda repo, det, w, mfs: _resolve_django(repo, det, mfs),
    "express": lambda repo, det, w, mfs: _resolve_express(repo, det, w, mfs),
    "springboot": lambda repo, det, w, mfs: _resolve_springboot(repo, det, mfs),
}


class EntryPointLocator:
    def __init__(
        self,
        weights: Optional[LocatorWeights] = None,
        confidence_thresholds: Optional[LocatorConfidenceThresholds] = None,
        max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
    ) -> None:
        self.weights = weights or LocatorWeights()
        self.confidence_thresholds = confidence_thresholds or LocatorConfidenceThresholds()
        self.max_file_size_bytes = max_file_size_bytes

    def locate(self, repo_path: str, detection_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Args:
            repo_path: path to the cloned repository.
            detection_result: the dict returned by
                framework_detector.detect_framework() for this same repo.

        Returns a dict with: framework, entry_point_file,
        entry_point_directory, entry_point_module, app_variable,
        build_tool, run_command_hint, resolution_method, confidence
        (High/Medium/Low/None), ambiguous, alternative_candidates, notes.

        Raises:
            EntryPointLocationError: bad repo_path or malformed detection_result.
        """
        if not isinstance(repo_path, str) or not repo_path.strip():
            raise EntryPointLocationError("repo_path must be a non-empty string.")

        repo_path_abs = os.path.abspath(repo_path)
        if not os.path.isdir(repo_path_abs):
            raise EntryPointLocationError(f"Not a directory: {repo_path_abs}")

        if not isinstance(detection_result, dict) or "detected_framework" not in detection_result:
            raise EntryPointLocationError(
                "detection_result must be the dict returned by framework_detector.detect_framework()."
            )

        candidates_field = detection_result.get("entry_point_candidates")
        if candidates_field is not None and not isinstance(candidates_field, list):
            raise EntryPointLocationError(
                "detection_result['entry_point_candidates'] must be a list of file paths."
            )

        framework = detection_result.get("detected_framework")
        notes: List[str] = []

        if detection_result.get("ambiguous"):
            notes.append(
                f"framework_detector flagged the framework choice itself as ambiguous "
                f"('{framework}' was the top candidate) - entry-point resolution below assumes "
                f"that choice is correct."
            )

        if framework is None:
            return self._empty_result(None, notes + ["No framework was detected, so no entry point can be resolved."])

        resolver = _RESOLVERS.get(framework)
        if resolver is None:
            return self._empty_result(
                framework, notes + [f"No entry-point resolver is registered for framework '{framework}'."]
            )

        result = resolver(repo_path_abs, detection_result, self.weights, self.max_file_size_bytes)

        confidence_override = result.get("confidence_override")
        if confidence_override is not None:
            confidence = confidence_override
            ambiguous = confidence == "Medium" and bool(result.get("alternatives"))
        else:
            confidence, ambiguous = _classify_locator_confidence(
                result.get("score") or 0.0, result.get("second_score"), self.confidence_thresholds
            )

        return {
            "framework": framework,
            "entry_point_file": result.get("entry_point_file"),
            "entry_point_directory": result.get("entry_point_directory"),
            "entry_point_module": result.get("entry_point_module"),
            "app_variable": result.get("app_variable"),
            "build_tool": result.get("build_tool"),
            "run_command_hint": result.get("run_command_hint"),
            "resolution_method": result.get("resolution_method"),
            "confidence": confidence,
            "ambiguous": ambiguous,
            "alternative_candidates": result.get("alternatives", []),
            "notes": notes + result.get("notes", []),
        }

    @staticmethod
    def _empty_result(framework: Optional[str], notes: List[str]) -> Dict[str, Any]:
        return {
            "framework": framework,
            "entry_point_file": None,
            "entry_point_directory": None,
            "entry_point_module": None,
            "app_variable": None,
            "build_tool": None,
            "run_command_hint": None,
            "resolution_method": "none",
            "confidence": "None",
            "ambiguous": False,
            "alternative_candidates": [],
            "notes": notes,
        }


def locate_entry_point(repo_path: str, detection_result: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
    """One-shot: construct an EntryPointLocator and run locate(). See
    EntryPointLocator for configurable options (scoring weights,
    confidence thresholds)."""
    return EntryPointLocator(**kwargs).locate(repo_path, detection_result)