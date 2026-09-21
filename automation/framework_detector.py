from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


class FrameworkDetectionError(ValueError):
    """Bad input (e.g. repo_path doesn't exist)."""


# --- Scan bounds (configurable via FrameworkDetector.__init__) ---

DEFAULT_MAX_DEPTH = 6
DEFAULT_MAX_FILES_SCANNED = 500
DEFAULT_MAX_FILE_SIZE_BYTES = 200_000  # 200 KB - source/manifest files are always far smaller than this

_SKIP_DIR_NAMES = {
    "node_modules", "venv", "env", "__pycache__", "target", "build",
    "dist", "out", "bin", "obj", "vendor", "site-packages", "coverage",
}

_MANIFEST_FILENAMES = {
    "requirements.txt", "pyproject.toml", "Pipfile",
    "package.json",
    "pom.xml", "build.gradle", "build.gradle.kts",
}

_CODE_EXTENSIONS = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".java"}


def _should_skip_dir(name: str) -> bool:
    return name.startswith(".") or name in _SKIP_DIR_NAMES


# --- Configuration ---

@dataclass
class SignalWeights:
    """
    Point value of each evidence category. A dependency declaration is
    worth as much as a structural marker (both are strong, low-noise
    signals); an individual code pattern match is worth less on its own
    but several of them corroborating each other add up.
    """
    dependency_declared: float = 3.0
    structural_marker: float = 3.0
    code_pattern: float = 2.0


@dataclass
class ConfidenceThresholds:
    """
    high_min_score / medium_min_score: minimum total score for that
    confidence band.
    ambiguous_score_gap: if the top two candidates' scores differ by less
    than this, the result is flagged ambiguous regardless of the top
    score - a close two-way race is not a confident detection even if
    both numbers look individually high (e.g. a full-stack repo with a
    Python backend AND a Node frontend).
    """
    high_min_score: float = 6.0
    medium_min_score: float = 3.0
    ambiguous_score_gap: float = 2.0


_SUPPORT_TIER: Dict[str, str] = {
    "flask": "full",
    "fastapi": "full",
    "express": "full",
    "django": "full",
    "springboot": "full",
}


# --- Framework definitions (data, not code) ---

@dataclass(frozen=True)
class FrameworkDefinition:
    key: str
    display_name: str
    language: str

    # Which manifest filenames are relevant, and which exact package
    # name(s) inside them count as a strong dependency signal.
    manifest_extensions: Tuple[str, ...] = ()
    dependency_names: Tuple[str, ...] = ()
    # For frameworks without one clean package name (Spring Boot's
    # dependencies are always named "spring-boot-starter-*"), match any
    # dependency string containing this substring instead.
    dependency_substring_fallback: Optional[str] = None

    # Filenames that structurally imply this framework (manage.py for
    # Django), regardless of file extension.
    structural_filenames: Tuple[str, ...] = ()
    # (label, compiled pattern) pairs checked inside the content of
    # whichever structural_filenames were found.
    structural_content_patterns: Tuple[Tuple[str, "re.Pattern[str]"], ...] = ()

    # (label, compiled pattern) pairs checked across every source file
    # with an extension in source_extensions.
    code_patterns: Tuple[Tuple[str, "re.Pattern[str]"], ...] = ()
    source_extensions: Tuple[str, ...] = ()


FRAMEWORK_DEFINITIONS: Tuple[FrameworkDefinition, ...] = (
    FrameworkDefinition(
        key="flask",
        display_name="Flask",
        language="python",
        manifest_extensions=("requirements.txt", "pyproject.toml", "Pipfile"),
        dependency_names=("flask",),
        code_patterns=(
            ("Flask instantiation", re.compile(r"\w+\s*=\s*Flask\s*\(")),
            ("Flask import", re.compile(r"\bfrom\s+flask\s+import\b")),
            ("Flask app factory", re.compile(r"\bdef\s+create_app\s*\(")),
        ),
        source_extensions=(".py",),
    ),
    FrameworkDefinition(
        key="fastapi",
        display_name="FastAPI",
        language="python",
        manifest_extensions=("requirements.txt", "pyproject.toml", "Pipfile"),
        dependency_names=("fastapi",),
        code_patterns=(
            ("FastAPI instantiation", re.compile(r"\w+\s*=\s*FastAPI\s*\(")),
            ("FastAPI import", re.compile(r"\bfrom\s+fastapi\s+import\b")),
        ),
        source_extensions=(".py",),
    ),
    FrameworkDefinition(
        key="django",
        display_name="Django",
        language="python",
        manifest_extensions=("requirements.txt", "pyproject.toml", "Pipfile"),
        dependency_names=("django",),
        structural_filenames=("manage.py", "settings.py", "wsgi.py", "asgi.py", "urls.py"),
        structural_content_patterns=(
            ("execute_from_command_line", re.compile(r"execute_from_command_line")),
            ("INSTALLED_APPS", re.compile(r"\bINSTALLED_APPS\b")),
            ("get_wsgi_application", re.compile(r"get_wsgi_application\s*\(")),
            ("get_asgi_application", re.compile(r"get_asgi_application\s*\(")),
        ),
        # Structural markers alone (manage.py, settings.py) tell you "this is
        # probably Django" but not much else. These patterns are checked
        # across every .py file, so they also help pin down which app module
        # actually wires up the URLs/models - useful when settings.py and the
        # real app code live in different subpackages.
        code_patterns=(
            ("django.urls import", re.compile(r"\bfrom\s+django\.urls\s+import\b")),
            ("django.db model definition", re.compile(r"class\s+\w+\s*\(\s*models\.Model\s*\)")),
            ("django.contrib.admin import", re.compile(r"\bfrom\s+django\.contrib\s+import\s+admin\b")),
            ("django views/shortcuts import", re.compile(r"\bfrom\s+django\.(?:shortcuts|views)\b")),
        ),
        source_extensions=(".py",),
    ),
    FrameworkDefinition(
        key="express",
        display_name="Express.js",
        language="javascript",
        manifest_extensions=("package.json",),
        dependency_names=("express",),
        code_patterns=(
            ("express require", re.compile(r"""require\(\s*['"]express['"]\s*\)""")),
            ("express import", re.compile(r"""\bimport\s+\w+\s+from\s+['"]express['"]""")),
            ("express instantiation", re.compile(r"\w+\s*=\s*express\s*\(\s*\)")),
            ("express.Router usage", re.compile(r"express\.Router\s*\(\s*\)")),
            ("app.listen call", re.compile(r"\.listen\s*\(\s*(?:\d|process\.env)")),
        ),
        source_extensions=(".js", ".mjs", ".cjs", ".ts", ".tsx"),
    ),
    FrameworkDefinition(
        key="springboot",
        display_name="Spring Boot",
        language="java",
        manifest_extensions=("pom.xml", "build.gradle", "build.gradle.kts"),
        dependency_substring_fallback="spring-boot-starter",
        # application.properties/.yml aren't Java files, but the structural-
        # marker mechanism only cares about exact filename, so this works the
        # same way manage.py does for Django - and server.port here is often
        # the only place the app's actual listen port is declared.
        structural_filenames=("application.properties", "application.yml", "application.yaml"),
        structural_content_patterns=(
            ("server.port config", re.compile(r"server\.port\s*[:=]")),
            ("spring.application.name config", re.compile(r"spring\.application\.name")),
        ),
        code_patterns=(
            ("@SpringBootApplication", re.compile(r"@SpringBootApplication\b")),
            ("SpringApplication.run", re.compile(r"SpringApplication\.run\s*\(")),
            ("@RestController", re.compile(r"@RestController\b")),
            ("@Controller", re.compile(r"@Controller\b")),
            ("@RequestMapping", re.compile(r"@RequestMapping\b")),
            ("@*Mapping annotation", re.compile(r"@(?:Get|Post|Put|Delete|Patch)Mapping\b")),
            ("@Configuration", re.compile(r"@Configuration\b")),
        ),
        source_extensions=(".java",),
    ),
)


# --- Evidence records ---

@dataclass(frozen=True)
class FrameworkSignal:
    name: str
    weight: float
    source_file: str
    detail: str


@dataclass(frozen=True)
class FrameworkCandidate:
    framework: str
    display_name: str
    language: str
    score: float
    signals: List[FrameworkSignal] = field(default_factory=list)


# --- Manifest content parsing (lightweight - good enough for detection, not
#     a substitute for a real dependency resolver) ---

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


def _extract_requirements_txt_packages(content: str) -> Set[str]:
    packages: Set[str] = set()
    for line in content.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+)", line)
        if match:
            packages.add(match.group(1).lower())
    return packages


def _extract_toml_like_packages(content: str) -> Set[str]:
    """
    Not a real TOML parser - pulls package-name-shaped tokens out of
    pyproject.toml (poetry/PEP 621 style, quoted names) and Pipfile
    (bare TOML keys under [packages]). Good enough to check "is package
    X declared", not for accurate dependency resolution.
    """
    packages: Set[str] = set()
    for match in re.finditer(r'["\']([A-Za-z][A-Za-z0-9_.\-]*)["\']\s*(?:=|,|\])', content):
        packages.add(match.group(1).lower())
    for match in re.finditer(r'^\s*([A-Za-z][A-Za-z0-9_.\-]*)\s*=\s*["{]', content, re.MULTILINE):
        packages.add(match.group(1).lower())
    return packages


def _extract_package_json_packages(content: str) -> Set[str]:
    packages: Set[str] = set()
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return packages
    if not isinstance(data, dict):
        return packages
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        deps = data.get(key)
        if isinstance(deps, dict):
            packages.update(name.lower() for name in deps.keys() if isinstance(name, str))
    return packages


def _extract_maven_artifact_ids(content: str) -> Set[str]:
    return {m.group(1).lower() for m in re.finditer(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", content)}


def _extract_gradle_dependencies(content: str) -> Set[str]:
    return {
        m.group(1).lower()
        for m in re.finditer(r"""['"]([\w.\-]+:[\w.\-]+)(?::[\w.\-]+)?['"]""", content)
    }


_MANIFEST_PARSERS = {
    "requirements.txt": _extract_requirements_txt_packages,
    "pyproject.toml": _extract_toml_like_packages,
    "Pipfile": _extract_toml_like_packages,
    "package.json": _extract_package_json_packages,
    "pom.xml": _extract_maven_artifact_ids,
    "build.gradle": _extract_gradle_dependencies,
    "build.gradle.kts": _extract_gradle_dependencies,
}


# --- Repository scan ---

def _collect_tracked_filenames(definitions: Sequence[FrameworkDefinition]) -> Set[str]:
    """Every exact filename worth indexing during the walk: manifests plus
    every framework's structural marker filenames, in one pass."""
    names = set(_MANIFEST_FILENAMES)
    for definition in definitions:
        names.update(definition.structural_filenames)
    return names


def _scan_repository(
    repo_path_abs: str,
    max_depth: int,
    max_files_scanned: int,
    tracked_filenames: Set[str],
) -> Dict[str, Any]:
    tracked_files: Dict[str, List[str]] = {}
    source_files: List[str] = []
    files_seen = 0
    truncated = False

    for dirpath, dirnames, filenames in os.walk(repo_path_abs):
        rel_dir = os.path.relpath(dirpath, repo_path_abs)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1

        if depth >= max_depth:
            dirnames[:] = []
            continue

        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]

        for filename in filenames:
            files_seen += 1
            rel_path = filename if rel_dir == "." else os.path.normpath(os.path.join(rel_dir, filename))

            if filename in tracked_filenames:
                tracked_files.setdefault(filename, []).append(rel_path)

            _, ext = os.path.splitext(filename)
            if ext in _CODE_EXTENSIONS:
                if len(source_files) < max_files_scanned:
                    source_files.append(rel_path)
                else:
                    truncated = True

    return {
        "tracked_files": tracked_files,
        "source_files": source_files,
        "files_seen": files_seen,
        "source_files_truncated": truncated,
    }


# --- Per-framework evaluation ---

def _evaluate_framework(
    definition: FrameworkDefinition,
    repo_path_abs: str,
    scan: Dict[str, Any],
    weights: SignalWeights,
    max_file_size_bytes: int,
    max_evidence_files: int = 5,
) -> FrameworkCandidate:
    signals: List[FrameworkSignal] = []
    score = 0.0

    # --- Dependency manifest signal (scores once, regardless of how many
    #     manifest files declare it) ---
    dependency_found = False
    for manifest_name in definition.manifest_extensions:
        if dependency_found:
            break
        for rel_path in scan["tracked_files"].get(manifest_name, []):
            content = _read_text_safe(os.path.join(repo_path_abs, rel_path), max_file_size_bytes)
            packages = _MANIFEST_PARSERS[manifest_name](content)

            matched = bool(definition.dependency_names) and any(name in packages for name in definition.dependency_names)
            if not matched and definition.dependency_substring_fallback:
                matched = any(definition.dependency_substring_fallback in pkg for pkg in packages)

            if matched:
                signals.append(FrameworkSignal(
                    name="dependency_declared",
                    weight=weights.dependency_declared,
                    source_file=rel_path,
                    detail=f"{definition.display_name} dependency found in {manifest_name}",
                ))
                score += weights.dependency_declared
                dependency_found = True
                break

    # --- Structural marker signal (filename-based; scores once per
    #     framework regardless of how many matching files are found,
    #     but all matches are kept as entry-point evidence) ---
    structural_hits: List[str] = []
    for structural_name in definition.structural_filenames:
        structural_hits.extend(scan["tracked_files"].get(structural_name, []))

    if structural_hits:
        evidence = structural_hits[:max_evidence_files]
        signals.append(FrameworkSignal(
            name="structural_marker",
            weight=weights.structural_marker,
            source_file=evidence[0],
            detail=(
                f"Found {len(structural_hits)} structural marker file(s) for {definition.display_name} "
                f"(e.g. {evidence[0]})"
            ),
        ))
        score += weights.structural_marker

        content_pattern_hits: Dict[str, List[str]] = {}
        for rel_path in evidence:
            content = _read_text_safe(os.path.join(repo_path_abs, rel_path), max_file_size_bytes)
            if not content:
                continue
            for label, pattern in definition.structural_content_patterns:
                if pattern.search(content):
                    content_pattern_hits.setdefault(label, []).append(rel_path)

        for label, files_found in content_pattern_hits.items():
            signals.append(FrameworkSignal(
                name=f"structural_content:{label}",
                weight=weights.code_pattern,
                source_file=files_found[0],
                detail=f"'{label}' pattern found in {files_found[0]}",
            ))
            score += weights.code_pattern
            for extra_file in files_found[1:]:
                signals.append(FrameworkSignal(
                    name=f"structural_content:{label}",
                    weight=0.0,
                    source_file=extra_file,
                    detail=f"'{label}' pattern also found in {extra_file} (additional occurrence, not double-counted)",
                ))

    # --- Code pattern signals (each distinct pattern label scores once,
    #     regardless of how many files it's found in) ---
    if definition.code_patterns and definition.source_extensions:
        relevant_files = [
            rel_path for rel_path in scan["source_files"]
            if os.path.splitext(rel_path)[1] in definition.source_extensions
        ]

        pattern_hit_files: Dict[str, List[str]] = {}
        for rel_path in relevant_files:
            content = _read_text_safe(os.path.join(repo_path_abs, rel_path), max_file_size_bytes)
            if not content:
                continue
            for label, pattern in definition.code_patterns:
                if pattern.search(content):
                    pattern_hit_files.setdefault(label, []).append(rel_path)

        for label, files_found in pattern_hit_files.items():
            evidence_files = files_found[:max_evidence_files]
            signals.append(FrameworkSignal(
                name=f"code_pattern:{label}",
                weight=weights.code_pattern,
                source_file=evidence_files[0],
                detail=f"Pattern '{label}' found in {len(files_found)} file(s), e.g. {evidence_files[0]}",
            ))
            score += weights.code_pattern
            # Every additional matching file beyond the first is recorded
            # too (at weight 0 - the label already scored once above) so
            # consumers building an entry-point candidate list (see
            # entry_point_locator.py) see every plausible file, not just
            # the first one found.
            for extra_file in evidence_files[1:]:
                signals.append(FrameworkSignal(
                    name=f"code_pattern:{label}",
                    weight=0.0,
                    source_file=extra_file,
                    detail=f"Pattern '{label}' also found in {extra_file} (additional occurrence, not double-counted)",
                ))

    return FrameworkCandidate(
        framework=definition.key,
        display_name=definition.display_name,
        language=definition.language,
        score=score,
        signals=signals,
    )


def _classify_confidence(
    top_score: float,
    second_score: Optional[float],
    thresholds: ConfidenceThresholds,
) -> Tuple[str, bool]:
    ambiguous = (
        second_score is not None
        and second_score > 0
        and (top_score - second_score) <= thresholds.ambiguous_score_gap
    )

    if top_score >= thresholds.high_min_score and not ambiguous:
        return "High", ambiguous
    if top_score >= thresholds.medium_min_score:
        return "Medium", ambiguous
    return "Low", ambiguous


def _extract_entry_point_candidates(candidate: FrameworkCandidate, limit: int = 10) -> List[str]:
    """
    Candidate entry-point files for the winning framework, pulled from
    wherever its code/structural evidence was actually found. Shallower
    paths are preferred as a tiebreak (a root-level match is more likely
    to be "the" entry point than a deeply nested one), but nothing here
    stops a correctly-identified file from living in a subfolder - this
    is what makes "app.py isn't at the repo root" no longer break
    detection.
    """
    seen: List[str] = []
    for signal in candidate.signals:
        if signal.name == "structural_marker" or signal.name.startswith(("code_pattern:", "structural_content:")):
            if signal.source_file not in seen:
                seen.append(signal.source_file)

    seen.sort(key=lambda p: (p.count(os.sep), p))
    return seen[:limit]


def _candidate_to_dict(candidate: FrameworkCandidate) -> Dict[str, Any]:
    return {
        "framework": candidate.framework,
        "display_name": candidate.display_name,
        "language": candidate.language,
        "score": round(candidate.score, 2),
        "signals": [
            {
                "name": s.name,
                "weight": s.weight,
                "source_file": s.source_file,
                "detail": s.detail,
            }
            for s in candidate.signals
        ],
    }


# --- Detector ---

class FrameworkDetector:
    def __init__(
        self,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_files_scanned: int = DEFAULT_MAX_FILES_SCANNED,
        max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
        weights: Optional[SignalWeights] = None,
        confidence_thresholds: Optional[ConfidenceThresholds] = None,
        definitions: Sequence[FrameworkDefinition] = FRAMEWORK_DEFINITIONS,
    ) -> None:
        self.max_depth = max_depth
        self.max_files_scanned = max_files_scanned
        self.max_file_size_bytes = max_file_size_bytes
        self.weights = weights or SignalWeights()
        self.confidence_thresholds = confidence_thresholds or ConfidenceThresholds()
        self.definitions = definitions

    def detect(self, repo_path: str) -> Dict[str, Any]:
        """
        Args:
            repo_path: path to a cloned repository (any directory).

        Returns a dict with: detected_framework, language, confidence
        (High/Medium/Low/None), ambiguous, support_tier (full/best-effort),
        entry_point_candidates, candidates (full ranked evidence list),
        scan_summary, notes.

        Raises:
            FrameworkDetectionError: repo_path is missing/not a directory.
        """
        if not isinstance(repo_path, str) or not repo_path.strip():
            raise FrameworkDetectionError("repo_path must be a non-empty string.")

        repo_path_abs = os.path.abspath(repo_path)
        if not os.path.isdir(repo_path_abs):
            raise FrameworkDetectionError(f"Not a directory: {repo_path_abs}")

        tracked_filenames = _collect_tracked_filenames(self.definitions)
        scan = _scan_repository(repo_path_abs, self.max_depth, self.max_files_scanned, tracked_filenames)

        candidates = [
            _evaluate_framework(definition, repo_path_abs, scan, self.weights, self.max_file_size_bytes)
            for definition in self.definitions
        ]
        candidates.sort(key=lambda c: c.score, reverse=True)

        top = candidates[0] if candidates else None
        second_score = candidates[1].score if len(candidates) > 1 else None
        notes: List[str] = []

        if top is None or top.score <= 0:
            detected_framework = None
            language = None
            confidence = "None"
            ambiguous = False
            support_tier = None
            entry_point_candidates: List[str] = []
            notes.append(
                "No supported framework could be detected from the repository's dependency "
                "manifests or source files."
            )
        else:
            confidence, ambiguous = _classify_confidence(top.score, second_score, self.confidence_thresholds)
            detected_framework = top.framework
            language = top.language
            support_tier = _SUPPORT_TIER.get(top.framework)
            entry_point_candidates = _extract_entry_point_candidates(top)

            if ambiguous:
                runner_up = candidates[1]
                notes.append(
                    f"'{top.display_name}' scored highest ({top.score:.1f}) but '{runner_up.display_name}' "
                    f"scored closely ({runner_up.score:.1f}) - this may be a multi-service repository "
                    f"(e.g. separate frontend/backend), or the detection may simply be unreliable for this "
                    f"repo. Verify before relying on this result."
                )
            if not entry_point_candidates:
                notes.append(
                    f"'{top.display_name}' was detected from dependency/structural evidence, but no "
                    f"specific entry-point file could be pinpointed from code patterns alone."
                )
            if support_tier == "best-effort":
                notes.append(
                    f"'{top.display_name}' support is best-effort: detection itself is reliable, but "
                    f"route discovery and entry-point resolution for this framework are less rigorous "
                    f"than for Flask/FastAPI/Express."
                )
            if scan["source_files_truncated"]:
                notes.append(
                    f"Source file scan was capped at {self.max_files_scanned} files - this is a large "
                    f"repository, so detection is based on a subset of source files."
                )

        return {
            "detected_framework": detected_framework,
            "language": language,
            "confidence": confidence,
            "ambiguous": ambiguous,
            "support_tier": support_tier,
            "entry_point_candidates": entry_point_candidates,
            "candidates": [_candidate_to_dict(c) for c in candidates if c.score > 0],
            "scan_summary": {
                "files_seen": scan["files_seen"],
                "source_files_scanned": len(scan["source_files"]),
                "source_files_truncated": scan["source_files_truncated"],
                "manifest_files_found": {
                    name: paths for name, paths in scan["tracked_files"].items() if name in _MANIFEST_FILENAMES
                },
            },
            "notes": notes,
        }


def detect_framework(repo_path: str, **kwargs: Any) -> Dict[str, Any]:
    """One-shot: construct a FrameworkDetector and run detect(). See
    FrameworkDetector for all configurable options (scan bounds, signal
    weights, confidence thresholds)."""
    return FrameworkDetector(**kwargs).detect(repo_path)