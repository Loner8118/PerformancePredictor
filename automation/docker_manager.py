from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
from typing import Any, Dict, List, Optional, Tuple


class DockerfileGenerationError(RuntimeError):
    """Raised when a Dockerfile can't be auto-generated (missing manifest, no run command
    resolvable, unsupported framework, etc.)."""


# ==========================================================================
# What this module does
# ==========================================================================
#
# Builds and runs a Docker image for the cloned repository, and - new in
# this version - auto-generates a Dockerfile from a per-framework
# template when the repository doesn't have one, instead of requiring
# every cloned repo to already contain a working Dockerfile.
#
# One deliberate, non-obvious decision worth spelling out: for
# auto-generated Flask and Django Dockerfiles, the CMD always uses
# gunicorn instead of "python app.py" / "manage.py runserver", even when
# that would technically run the app. Both frameworks' built-in dev
# servers are single-threaded - serving a container through one would
# make every load test measure the dev server's own concurrency ceiling,
# not the application's. Since this pipeline's entire point is
# extrapolating real capacity from load-test data, feeding it numbers
# from a dev server would poison the mathematical engine's input with a
# bottleneck that has nothing to do with the actual code. FastAPI/Express/
# Spring Boot don't have this problem (uvicorn, Node's event loop, and
# Spring's embedded server are all already concurrent), so no override is
# needed there.
#
# Container port handling: an auto-generated Dockerfile always uses one
# consistent internal port (DEFAULT_GENERATED_CONTAINER_PORT) regardless
# of framework, since this module controls what the app binds to and can
# just pick one. For an EXISTING (user-provided) Dockerfile, the port is
# resolved from its EXPOSE line if present, falling back to the
# framework's conventional default port only if EXPOSE is absent.
# Either way, build_image() returns the resolved port and run_container()
# takes it explicitly - health_checker.py doesn't need its own
# framework-port-awareness, it just polls whatever host_port this module
# reports.
#
# For that resolved port to be correct, the app actually has to bind to
# it: Flask/FastAPI/Django get there via an explicit gunicorn/uvicorn
# --bind/--port flag. Express and Spring Boot have no such flag, so this
# module sets ENV PORT / ENV SERVER_PORT instead, relying on
# process.env.PORT (Express) and Spring's relaxed env-var binding
# (Spring Boot) - both are the standard convention, but not guaranteed if
# the app hardcodes its port. One case this doesn't cover: a resolved
# Procfile run command that references "$PORT" - exec-form CMD (see
# _to_exec_form below) doesn't invoke a shell, so it can't expand that
# variable; it would be passed through literally. Procfiles are rare
# outside Heroku-style repos, so this is left as a known gap rather than
# reworked into shell-form CMD, which would reintroduce the signal-
# forwarding problem exec form exists to avoid.


_FRAMEWORK_DEFAULT_PORTS: Dict[str, int] = {
    "flask": 5000,
    "fastapi": 8000,
    "django": 8000,
    "express": 3000,
    "springboot": 8080,
}

_EXPOSE_PATTERN = re.compile(r"^\s*EXPOSE\s+(\d+)", re.IGNORECASE | re.MULTILINE)
_DOCKER_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


def _validate_docker_name(name: Any, label: str) -> str:
    if not isinstance(name, str) or not _DOCKER_NAME_PATTERN.match(name):
        raise ValueError(f"Invalid Docker {label}: {name!r}.")
    return name


def _resolve_container_port(dockerfile_content: Optional[str], framework: Optional[str], default: int) -> int:
    if dockerfile_content:
        match = _EXPOSE_PATTERN.search(dockerfile_content)
        if match:
            return int(match.group(1))
    return _FRAMEWORK_DEFAULT_PORTS.get(framework, default) if framework else default


def _to_exec_form(command: str) -> str:
    """
    Convert a shell command string into a Docker CMD exec-form JSON array,
    e.g. 'gunicorn app:app' -> '["gunicorn", "app:app"]'. Exec form
    properly forwards signals (SIGTERM on `docker stop`) to the process;
    shell form silently wraps it in an extra /bin/sh -c layer that eats
    the signal instead of forwarding it.
    """
    tokens = shlex.split(command)
    if not tokens:
        raise DockerfileGenerationError(f"Run command resolved to nothing runnable: {command!r}")
    return "[" + ", ".join(json.dumps(t) for t in tokens) + "]"


_ENV_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _command_to_dockerfile_lines(command: str) -> str:
    """
    Builds the ENV + CMD Dockerfile lines for a run command. Procfile-
    sourced commands commonly prefix inline env-var assignments (e.g.
    'FLASK_ENV=production gunicorn app:app', a standard Heroku Procfile
    pattern) - exec-form CMD has no shell to interpret that syntax, so
    passed straight through it would try to execute a program literally
    named "FLASK_ENV=production". Split those off into separate ENV
    instructions instead, leaving a clean, actually-runnable CMD. A no-op
    for the gunicorn/uvicorn commands this module builds itself, which
    never start with an env assignment.
    """
    tokens = shlex.split(command)
    if not tokens:
        raise DockerfileGenerationError(f"Run command resolved to nothing runnable: {command!r}")

    split_at = 0
    while split_at < len(tokens) and _ENV_ASSIGNMENT_PATTERN.match(tokens[split_at]):
        split_at += 1
    env_tokens, exec_tokens = tokens[:split_at], tokens[split_at:]

    if not exec_tokens:
        raise DockerfileGenerationError(f"Run command resolved to nothing runnable: {command!r}")

    env_lines = "".join(f"ENV {tok}\n" for tok in env_tokens)
    exec_array = "[" + ", ".join(json.dumps(t) for t in exec_tokens) + "]"
    return f"{env_lines}CMD {exec_array}\n"


def _find_manifest_rel_path(detection_result: Dict[str, Any], preferred_order: Tuple[str, ...]) -> Optional[str]:
    manifest_files = detection_result.get("scan_summary", {}).get("manifest_files_found", {})
    for manifest_name in preferred_order:
        paths = manifest_files.get(manifest_name)
        if paths:
            return paths[0]
    return None


def _python_install_line(manifest_rel_path: str) -> str:
    basename = os.path.basename(manifest_rel_path)
    if basename == "requirements.txt":
        return f"pip install --no-cache-dir -r {manifest_rel_path}"
    if basename == "Pipfile":
        project_dir = os.path.dirname(manifest_rel_path) or "."
        return f"pip install --no-cache-dir pipenv && cd {project_dir} && pipenv install --system --deploy"
    # pyproject.toml - best effort, assumes a PEP 517-compatible build
    # backend (setuptools, poetry-core, etc. with a [build-system] table).
    # Not guaranteed to work for every pyproject.toml setup.
    project_dir = os.path.dirname(manifest_rel_path) or "."
    return f"pip install --no-cache-dir {project_dir}"


def _python_copy_lines(manifest_rel_path: str) -> Tuple[str, str]:
    """
    Returns (copy_before_install, copy_after_install). requirements.txt
    and Pipfile only need the manifest file itself to resolve
    dependencies, so copying just that (before RUN pip install) lets
    Docker cache the install layer across rebuilds that only touch
    application code. pyproject.toml's `pip install {dir}` is different -
    it installs the local project ITSELF via its build backend, which
    needs the full source tree already present, not just the manifest -
    so for that case the full copy has to happen up front instead,
    sacrificing that caching benefit for a build that actually works.
    """
    if os.path.basename(manifest_rel_path) == "pyproject.toml":
        return "COPY . .\n", ""
    return f"COPY {manifest_rel_path} {manifest_rel_path}\n", "COPY . .\n"


# --- Per-framework Dockerfile generators ---

_PYTHON_MANIFEST_ORDER = ("requirements.txt", "pyproject.toml", "Pipfile")


def _generate_flask_dockerfile(
    detection_result: Dict[str, Any],
    location_result: Dict[str, Any],
    port: int,
    workers: int,
) -> Tuple[str, List[str]]:
    notes: List[str] = []
    manifest_rel_path = _find_manifest_rel_path(detection_result, _PYTHON_MANIFEST_ORDER)
    if manifest_rel_path is None:
        raise DockerfileGenerationError("No Python dependency manifest found to build a Dockerfile from.")
    install_line = _python_install_line(manifest_rel_path)

    run_command = None
    if location_result.get("resolution_method") == "procfile" and location_result.get("run_command_hint"):
        run_command = location_result["run_command_hint"]
        notes.append("Using the run command declared in the repository's Procfile.")
    else:
        module = location_result.get("entry_point_module")
        app_variable = location_result.get("app_variable") or "app"
        if module:
            run_command = f"gunicorn --bind 0.0.0.0:{port} --workers {workers} {module}:{app_variable}"
            notes.append(
                "Using gunicorn instead of Flask's built-in dev server - the dev server is "
                "single-threaded and would produce misleading (artificially bottlenecked) "
                "load-test results that reflect the dev server, not the application."
            )
        elif location_result.get("run_command_hint"):
            run_command = location_result["run_command_hint"]
            notes.append("Could not construct a gunicorn command; falling back to the resolved run command hint.")

    if run_command is None:
        raise DockerfileGenerationError("Could not determine a run command for this Flask application.")

    copy_before, copy_after = _python_copy_lines(manifest_rel_path)
    dockerfile = (
        f"FROM python:3.11-slim\n"
        f"WORKDIR /app\n"
        f"{copy_before}"
        f"RUN {install_line}\n"
        f"RUN pip install --no-cache-dir gunicorn\n"
        f"{copy_after}"
        f"EXPOSE {port}\n"
    ) + _command_to_dockerfile_lines(run_command)
    return dockerfile, notes


def _generate_fastapi_dockerfile(
    detection_result: Dict[str, Any],
    location_result: Dict[str, Any],
    port: int,
    workers: int,
) -> Tuple[str, List[str]]:
    notes: List[str] = []
    manifest_rel_path = _find_manifest_rel_path(detection_result, _PYTHON_MANIFEST_ORDER)
    if manifest_rel_path is None:
        raise DockerfileGenerationError("No Python dependency manifest found to build a Dockerfile from.")
    install_line = _python_install_line(manifest_rel_path)

    run_command = None
    if location_result.get("resolution_method") == "procfile" and location_result.get("run_command_hint"):
        run_command = location_result["run_command_hint"]
        notes.append("Using the run command declared in the repository's Procfile.")
    else:
        module = location_result.get("entry_point_module")
        app_variable = location_result.get("app_variable") or "app"
        if module:
            run_command = f"uvicorn {module}:{app_variable} --host 0.0.0.0 --port {port} --workers {workers}"
        elif location_result.get("run_command_hint"):
            run_command = location_result["run_command_hint"]
            notes.append("Could not construct a uvicorn command; falling back to the resolved run command hint.")

    if run_command is None:
        raise DockerfileGenerationError("Could not determine a run command for this FastAPI application.")

    copy_before, copy_after = _python_copy_lines(manifest_rel_path)
    dockerfile = (
        f"FROM python:3.11-slim\n"
        f"WORKDIR /app\n"
        f"{copy_before}"
        f"RUN {install_line}\n"
        f"RUN pip install --no-cache-dir uvicorn\n"
        f"{copy_after}"
        f"EXPOSE {port}\n"
    ) + _command_to_dockerfile_lines(run_command)
    return dockerfile, notes


def _generate_django_dockerfile(
    detection_result: Dict[str, Any],
    location_result: Dict[str, Any],
    port: int,
    workers: int,
) -> Tuple[str, List[str]]:
    notes: List[str] = []
    manifest_rel_path = _find_manifest_rel_path(detection_result, _PYTHON_MANIFEST_ORDER)
    if manifest_rel_path is None:
        raise DockerfileGenerationError("No Python dependency manifest found to build a Dockerfile from.")
    install_line = _python_install_line(manifest_rel_path)

    wsgi_module = location_result.get("entry_point_module")
    project_root = location_result.get("entry_point_directory") or "."

    if wsgi_module:
        run_command = f"gunicorn --bind 0.0.0.0:{port} --workers {workers} --chdir {project_root} {wsgi_module}:application"
        notes.append(
            "Using gunicorn against the resolved WSGI module instead of 'manage.py runserver' - "
            "Django's dev server is single-threaded and would produce misleading load-test results."
        )
    else:
        manage_path = "manage.py" if project_root == "." else f"{project_root}/manage.py"
        run_command = f"python {manage_path} runserver 0.0.0.0:{port}"
        notes.append(
            "Could not resolve a WSGI module; falling back to Django's development server. "
            "This is single-threaded, so load-test results may understate real concurrency "
            "capacity - a WSGI-based Procfile entry would give more representative results."
        )

    copy_before, copy_after = _python_copy_lines(manifest_rel_path)
    dockerfile = (
        f"FROM python:3.11-slim\n"
        f"WORKDIR /app\n"
        f"{copy_before}"
        f"RUN {install_line}\n"
        f"RUN pip install --no-cache-dir gunicorn\n"
        f"{copy_after}"
        f"EXPOSE {port}\n"
    ) + _command_to_dockerfile_lines(run_command)
    return dockerfile, notes


def _generate_express_dockerfile(
    project_path_abs: str,
    detection_result: Dict[str, Any],
    location_result: Dict[str, Any],
    port: int,
) -> Tuple[str, List[str]]:
    notes: List[str] = []
    manifest_files = detection_result.get("scan_summary", {}).get("manifest_files_found", {})
    package_json_paths = manifest_files.get("package.json", [])
    if not package_json_paths:
        raise DockerfileGenerationError("No package.json found to build a Dockerfile from.")

    package_json_rel_path = package_json_paths[0]
    package_dir = os.path.dirname(package_json_rel_path) or "."

    run_command = location_result.get("run_command_hint")
    if not run_command:
        raise DockerfileGenerationError("Could not determine a run command for this Express application.")

    lock_file_rel = os.path.join(package_dir, "package-lock.json") if package_dir != "." else "package-lock.json"
    has_lock_file = os.path.isfile(os.path.join(project_path_abs, lock_file_rel))

    if has_lock_file:
        install_cmd = f"cd {package_dir} && npm ci"
        copy_lock_line = f"COPY {lock_file_rel} {lock_file_rel}\n"
    else:
        install_cmd = f"cd {package_dir} && npm install"
        copy_lock_line = ""
        notes.append("No package-lock.json found; using 'npm install' instead of the more reproducible 'npm ci'.")

    workdir = "/app" if package_dir == "." else f"/app/{package_dir}"

    # Nothing so far tells the app what port to listen on - only Flask/
    # FastAPI/Django get an explicit --bind/--port flag (via gunicorn/
    # uvicorn) below. Express has no equivalent CLI flag, but reading
    # process.env.PORT is the near-universal convention for Node HTTP
    # servers, so setting it here is the best available lever.
    notes.append(
        f"Setting ENV PORT={port} so the container's exposed port matches what the app "
        f"listens on - this assumes the app reads process.env.PORT (the standard Express "
        f"convention: `const port = process.env.PORT || ...`). If the app hardcodes a "
        f"specific port instead, the container will actually listen there, not on {port}, "
        f"and the health check/load test will fail to connect."
    )

    dockerfile = (
        f"FROM node:20-slim\n"
        f"WORKDIR /app\n"
        f"COPY {package_json_rel_path} {package_json_rel_path}\n"
        f"{copy_lock_line}"
        f"RUN {install_cmd}\n"
        f"COPY . .\n"
        f"WORKDIR {workdir}\n"
        f"ENV PORT={port}\n"
        f"EXPOSE {port}\n"
    ) + _command_to_dockerfile_lines(run_command)
    return dockerfile, notes


def _generate_springboot_dockerfile(
    location_result: Dict[str, Any],
    port: int,
) -> Tuple[str, List[str]]:
    notes: List[str] = []
    build_tool = location_result.get("build_tool")
    run_command = location_result.get("run_command_hint")
    project_root = location_result.get("entry_point_directory") or "."

    if not build_tool or not run_command:
        raise DockerfileGenerationError("Could not determine a build tool/run command for this Spring Boot application.")

    if build_tool == "maven":
        base_image = "maven:3.9-eclipse-temurin-17"
    elif build_tool == "gradle":
        base_image = "gradle:8-jdk17"
    else:
        raise DockerfileGenerationError(f"Unsupported Spring Boot build tool: {build_tool}")

    notes.append(
        f"Using a {base_image} image that bundles the JDK and {build_tool} together, running the "
        f"app directly via '{run_command}' rather than building a production JAR - simpler and "
        f"correct for load-testing purposes, though not how this would be shipped to production."
    )

    workdir = "/app" if project_root == "." else f"/app/{project_root}"

    # Spring Boot's relaxed environment-variable binding maps SERVER_PORT
    # to the server.port property automatically, with environment
    # variables taking precedence over application.properties/.yml - so
    # this reliably overrides whatever port the repo's own config
    # declares, without needing to parse/rewrite that config file.
    notes.append(
        f"Setting ENV SERVER_PORT={port} so the app listens on the port this container "
        f"exposes, regardless of any server.port value in application.properties/.yml."
    )

    dockerfile = (
        f"FROM {base_image}\n"
        f"WORKDIR /app\n"
        f"COPY . .\n"
        f"WORKDIR {workdir}\n"
        f"ENV SERVER_PORT={port}\n"
        f"EXPOSE {port}\n"
    ) + _command_to_dockerfile_lines(run_command)
    return dockerfile, notes


# --- Docker Manager ---

class DockerManager:
    """Handles Docker image building (including auto-generating a
    Dockerfile when none exists), container creation, and cleanup for the
    performance analysis pipeline."""

    DEFAULT_GENERATED_CONTAINER_PORT = 8000
    DEFAULT_GUNICORN_WORKERS = 4
    DEFAULT_BUILD_TIMEOUT_SECONDS = 300
    DEFAULT_RUN_TIMEOUT_SECONDS = 30

    def __init__(
        self,
        host_port: Optional[int] = None,
        default_container_port: int = DEFAULT_GENERATED_CONTAINER_PORT,
        gunicorn_workers: int = DEFAULT_GUNICORN_WORKERS,
        build_timeout_seconds: int = DEFAULT_BUILD_TIMEOUT_SECONDS,
        run_timeout_seconds: int = DEFAULT_RUN_TIMEOUT_SECONDS,
    ) -> None:
        # host_port=None means "pick a free port at run_container() time" -
        # avoids collisions between concurrent/repeated pipeline runs
        # that a fixed default would cause.
        self.host_port = host_port
        self.default_container_port = default_container_port
        self.gunicorn_workers = gunicorn_workers
        self.build_timeout_seconds = build_timeout_seconds
        self.run_timeout_seconds = run_timeout_seconds

    @staticmethod
    def _find_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    @staticmethod
    def _run_command(command: List[str], cwd: Optional[str] = None, timeout: Optional[int] = None):
        try:
            return subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Docker command timed out after {timeout}s: {' '.join(command)}")
        except FileNotFoundError:
            raise RuntimeError("Docker executable was not found. Make sure Docker is installed and available in PATH.")
        except OSError as e:
            raise RuntimeError(f"Failed to execute Docker command: {e}")

    # --- Dockerfile generation ---

    def _generate_dockerfile(
        self,
        project_path_abs: str,
        detection_result: Dict[str, Any],
        location_result: Dict[str, Any],
    ) -> Tuple[str, int, List[str]]:
        framework = detection_result.get("detected_framework")
        if framework is None:
            raise DockerfileGenerationError("No framework was detected; cannot auto-generate a Dockerfile.")

        port = self.default_container_port

        if framework == "flask":
            content, notes = _generate_flask_dockerfile(detection_result, location_result, port, self.gunicorn_workers)
        elif framework == "fastapi":
            content, notes = _generate_fastapi_dockerfile(detection_result, location_result, port, self.gunicorn_workers)
        elif framework == "django":
            content, notes = _generate_django_dockerfile(detection_result, location_result, port, self.gunicorn_workers)
        elif framework == "express":
            content, notes = _generate_express_dockerfile(project_path_abs, detection_result, location_result, port)
        elif framework == "springboot":
            content, notes = _generate_springboot_dockerfile(location_result, port)
        else:
            raise DockerfileGenerationError(f"No Dockerfile template is available for framework '{framework}'.")

        return content, port, notes

    # --- Build ---

    def build_image(
        self,
        project_path: str,
        image_name: str = "performance-image",
        detection_result: Optional[Dict[str, Any]] = None,
        location_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Args:
            project_path: path to the (already validated) cloned repository.
            image_name: Docker image tag to build.
            detection_result / location_result: outputs of
                framework_detector.detect_framework() and
                entry_point_locator.locate_entry_point(). Required only
                if the project has no Dockerfile of its own - used to
                auto-generate one. If the project already has a
                Dockerfile, these are optional (only used to guess the
                container port if the Dockerfile has no EXPOSE line).
        """
        if not project_path:
            raise ValueError("Project path is required.")
        project_path = os.path.abspath(project_path)
        if not os.path.isdir(project_path):
            raise ValueError(f"Project directory does not exist: {project_path}")
        image_name = _validate_docker_name(image_name, "image name")

        dockerfile_path = os.path.join(project_path, "Dockerfile")
        generation_notes: List[str] = []
        dockerfile_generated = False

        if os.path.isfile(dockerfile_path):
            print("Using existing Dockerfile.")
            with open(dockerfile_path, "r", encoding="utf-8", errors="replace") as f:
                existing_content = f.read()
            framework = detection_result.get("detected_framework") if detection_result else None
            container_port = _resolve_container_port(existing_content, framework, self.default_container_port)
        else:
            if detection_result is None or location_result is None:
                raise ValueError(
                    "No Dockerfile exists in the project, and detection_result/location_result "
                    "were not provided to auto-generate one. Run framework_detector.detect_framework() "
                    "and entry_point_locator.locate_entry_point() first, and pass their results here."
                )
            print("No Dockerfile found - auto-generating one.")
            try:
                dockerfile_content, container_port, generation_notes = self._generate_dockerfile(
                    project_path, detection_result, location_result
                )
            except DockerfileGenerationError as e:
                return {
                    "success": False,
                    "image_name": image_name,
                    "message": "Could not auto-generate a Dockerfile for this repository.",
                    "error": str(e),
                    "dockerfile_generated": False,
                }

            with open(dockerfile_path, "w", encoding="utf-8") as f:
                f.write(dockerfile_content)
            dockerfile_generated = True

            for note in generation_notes:
                print("Dockerfile generation note:", note)

        print(f"Building Docker image: {image_name}")
        result = self._run_command(
            ["docker", "build", "-t", image_name, "."],
            cwd=project_path,
            timeout=self.build_timeout_seconds,
        )

        if result.returncode != 0:
            error_message = result.stderr.strip() or result.stdout.strip() or "Unknown Docker build error."
            print("Docker build failed.")
            print(error_message)
            return {
                "success": False,
                "image_name": image_name,
                "message": "Docker image build failed.",
                "error": error_message,
                "container_port": container_port,
                "dockerfile_generated": dockerfile_generated,
                "generation_notes": generation_notes,
            }

        print("Docker image built successfully.")
        return {
            "success": True,
            "image_name": image_name,
            "message": "Docker image built successfully.",
            "output": result.stdout.strip(),
            "container_port": container_port,
            "dockerfile_generated": dockerfile_generated,
            "generation_notes": generation_notes,
        }

    # --- Run / stop / status ---

    def run_container(
        self,
        image_name: str = "performance-image",
        container_name: str = "performance-container",
        container_port: Optional[int] = None,
        instrumentation_manager: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Start a Docker container and return info needed by the health-check and runtime-monitoring stages.

        container_port should be the value returned by build_image() -
        auto-generated Dockerfiles all use self.default_container_port
        internally, but an existing Dockerfile may expose a different
        port, so this must not be assumed constant across projects.
        """
        image_name = _validate_docker_name(image_name, "image name")
        container_name = _validate_docker_name(container_name, "container name")

        resolved_container_port = container_port if container_port is not None else self.default_container_port
        resolved_host_port = self.host_port if self.host_port is not None else self._find_free_port()

        print(f"Starting Docker container: {container_name}")

        self._run_command(["docker", "rm", "-f", container_name], timeout=self.run_timeout_seconds)

        docker_run_command = [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "-p",
            f"{resolved_host_port}:{resolved_container_port}",
        ]

        instrumentation_enabled = False

        if instrumentation_manager is not None:
            if not instrumentation_manager.is_installed():
                instrumentation_manager.install()

            instrumentation_args = instrumentation_manager.docker_run_args()

            if instrumentation_args:
                docker_run_command.extend(instrumentation_args)
                instrumentation_enabled = True

        docker_run_command.append(image_name)

        result = self._run_command(
            docker_run_command,
            timeout=self.run_timeout_seconds,
        )

        if result.returncode != 0:
            error_message = result.stderr.strip() or result.stdout.strip() or "Unknown Docker container error."
            print("Failed to start Docker container.")
            print(error_message)
            return {
                "success": False,
                "container_id": None,
                "container_name": container_name,
                "message": "Docker container failed to start.",
                "error": error_message,
            }

        container_id = result.stdout.strip()
        print("Docker container started successfully.")
        print("Container ID:", container_id)

        inspect_result = self._run_command(["docker", "inspect", container_id], timeout=self.run_timeout_seconds)
        if inspect_result.returncode != 0:
            return {
                "success": False,
                "container_id": container_id,
                "container_name": container_name,
                "message": "Container started but could not be inspected.",
                "error": inspect_result.stderr.strip(),
            }

        return {
            "success": True,
            "container_id": container_id,
            "container_name": container_name,
            "image_name": image_name,
            "host": f"http://localhost:{resolved_host_port}",
            "port": resolved_host_port,
            "container_port": resolved_container_port,
            "instrumentation_enabled": instrumentation_enabled,
            "message": "Docker container started successfully.",
        }

    def stop_container(self, container_id: str) -> Dict[str, Any]:
        if not container_id:
            return {"success": False, "message": "Container ID is required."}

        print(f"Stopping Docker container: {container_id}")
        result = self._run_command(["docker", "rm", "-f", container_id], timeout=self.run_timeout_seconds)

        if result.returncode == 0:
            print("Docker container stopped and removed successfully.")
            return {
                "success": True,
                "container_id": container_id,
                "message": "Docker container stopped and removed successfully.",
            }

        error_message = result.stderr.strip() or result.stdout.strip() or "Unknown Docker cleanup error."
        print("Failed to stop container.")
        print(error_message)
        return {
            "success": False,
            "container_id": container_id,
            "message": "Failed to stop Docker container.",
            "error": error_message,
        }

    def get_container_status(self, container_id: str) -> Dict[str, Any]:
        if not container_id:
            return {"success": False, "status": None, "message": "Container ID is required."}

        result = self._run_command(
            ["docker", "inspect", "--format", "{{.State.Status}}", container_id],
            timeout=self.run_timeout_seconds,
        )
        if result.returncode != 0:
            return {
                "success": False,
                "status": None,
                "message": "Container could not be inspected.",
                "error": result.stderr.strip(),
            }

        return {
            "success": True,
            "status": result.stdout.strip(),
            "container_id": container_id,
        }

    def get_container_logs(self, container_id: str, tail: int = 200) -> Dict[str, Any]:
        """Fetch recent container logs - used by health_checker.py to explain
        *why* a container crashed instead of just reporting that it did."""
        if not container_id:
            return {"success": False, "logs": None, "message": "Container ID is required."}

        result = self._run_command(
            ["docker", "logs", "--tail", str(tail), container_id],
            timeout=self.run_timeout_seconds,
        )
        if result.returncode != 0:
            return {
                "success": False,
                "logs": None,
                "message": "Could not retrieve container logs.",
                "error": result.stderr.strip(),
            }

        # docker logs interleaves the container's stdout/stderr depending
        # on how the app itself writes - combine both for a complete picture.
        combined = (result.stdout or "") + (result.stderr or "")
        return {"success": True, "logs": combined.strip(), "container_id": container_id}

    def remove_image(self, image_name: str) -> Dict[str, Any]:
        """Remove a built image. Not called automatically anywhere - the
        pipeline decides when cleanup should happen, this just provides
        the capability (mirrors stop_container())."""
        if not image_name:
            return {"success": False, "message": "Docker image name is required."}
        image_name = _validate_docker_name(image_name, "image name")

        print(f"Removing Docker image: {image_name}")
        result = self._run_command(["docker", "rmi", "-f", image_name], timeout=self.run_timeout_seconds)

        if result.returncode == 0:
            print("Docker image removed successfully.")
            return {"success": True, "image_name": image_name, "message": "Docker image removed successfully."}

        error_message = result.stderr.strip() or result.stdout.strip() or "Unknown Docker cleanup error."
        print("Failed to remove image.")
        print(error_message)
        return {
            "success": False,
            "image_name": image_name,
            "message": "Failed to remove Docker image.",
            "error": error_message,
        }