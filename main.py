import os
import math
from unittest import result
import uuid
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

from automation.github_manager import GitHubManager
from automation.framework_detector import detect_framework
from automation.entry_point_locator import locate_entry_point
from automation.validator import RepositoryValidator
from automation.docker_manager import DockerManager
from automation.health_checker import HealthChecker
from automation.route_discovery import RouteDiscovery
from automation.locust_generator import LocustGenerator
from automation.locust_runner import LocustRunner
from automation.dependency_detector import ( detect_dependencies, resolve_database_monitor_config )
from automation.application_descriptor import (build_application_descriptor, DescriptorBuildError,)

from metrics.runtime_monitor import RuntimeMonitor
from metrics.db_monitor import (
    DBMonitor,
    ConnectionParams,
    DBMonitorError,
)

from mathematical_engine.usl import run_usl_analysis
from mathematical_engine.little_law import analyze_workload_levels
from mathematical_engine.queueing import analyze_queue_safe
from mathematical_engine.bottleneck import (
    analyze_bottleneck_safe,
    ResourceCapacityConfig,
)
from mathematical_engine.forced_flow import analyze_forced_flow, to_bottleneck_resources
from mathematical_engine.amdahl import analyze_amdahl
from mathematical_engine.capacity import analyze_capacity
from mathematical_engine.scalability import predict_scalability
from mathematical_engine.slo import analyze_slo, SLOThresholds
from mathematical_engine.recommendation import generate_recommendations

from validation.experiment_manager import Experiment, ExperimentError, list_experiments
from validation.prediction_validation import (
    freeze_predictions,
    record_validation_actuals,
    validate_predictions,
    PredictionValidationError,
    ModelIntegrityError,
)
from validation.model_validation import validate_levels, ModelValidationError
from validation.ablation import run_ablation, AblationError
from validation.report import render_experiment_report, ReportError


# Shared disk/network capacity assumptions - kept in sync between
# capacity.py's threshold-based resource-pressure check and
# bottleneck.py's demand-based (Utilization Law) analysis so the two
# never disagree purely because of different configured limits.
DISK_IO_LIMIT_MBPS = 100.0
NETWORK_IO_LIMIT_MBPS = 100.0
BOTTLENECK_CAPACITY_CONFIG = ResourceCapacityConfig(
    max_disk_mb_s=DISK_IO_LIMIT_MBPS,
    max_network_mb_s=NETWORK_IO_LIMIT_MBPS,
)
EXPERIMENTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "experiments")
# Default SLO budget used by the pipeline's SLO analysis stage and by the
# standalone /api/prediction/slo endpoint when the caller doesn't override
# them. Kept as named constants (rather than inlined) so both call sites
# can never silently drift apart.
DEFAULT_SLO_MAX_RESPONSE_TIME_SECONDS = 0.5
DEFAULT_SLO_MAX_ERROR_RATE_PERCENT = 1.0

# In-memory registry of running DBMonitor instances, keyed by a generated
# monitor_id handed back from /api/db-monitor/start. DBMonitor's own
# start()/stop() lifecycle is a long-lived background thread (like
# RuntimeMonitor's), so it can't be a one-shot per-request object the way
# most other endpoints here are - it has to survive between the "start"
# request and the later "stop" request. This is process-local state: it
# does not survive an app restart, and (like the rest of this Flask app)
# assumes a single worker process.
active_db_monitors = {}


def sanitize_for_json(value):
    """Recursively replace NaN/Infinity with None so the response is valid JSON."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: sanitize_for_json(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_for_json(item) for item in value]
    return value


def build_capacity_input(usl_result, little_law_results, queueing_results, load_results):
    """
    Convert USL, Little's Law, Queueing Theory, and runtime/load-test results
    into the input structure capacity.analyze_capacity() expects.

    Not currently called from any route (capacity input is built inline
    inside run_load_testing()), but kept available for reuse elsewhere.
    """
    usl_metrics = usl_result.get("usl_metrics", {})
    efficiency = usl_result.get("efficiency", {})

    # Use the highest observed/predicted efficiency if we have any.
    scalability_efficiency = max(efficiency.values()) if efficiency else 0.0

    if not load_results:
        raise ValueError("No load test results available for capacity analysis.")

    latest = load_results[-1]

    current_users = float(latest.get("users", latest.get("concurrent_users", 0)))
    throughput = float(latest.get("throughput", 0))
    response_time = float(latest.get("avg_response_time", latest.get("response_time", 0)))
    cpu_usage = float(latest.get("cpu_usage", 0))
    memory_usage = float(latest.get("memory_usage", 0))
    disk_io = float(latest.get("disk_io", 0))
    network_io = float(latest.get("network_io", 0))
    error_rate = float(latest.get("error_rate", 0))

    latest_ll = little_law_results[-1]
    requests_in_system = float(latest_ll.get("requests_in_system", latest_ll.get("L", 0)))
    time_in_system = float(latest_ll.get("time_in_system", latest_ll.get("response_time", 0)))

    # little_law.py normally returns seconds - if this still looks like
    # milliseconds, convert it.
    if time_in_system > 10:
        time_in_system = time_in_system / 1000.0

    latest_queue = queueing_results[-1]
    utilization = float(latest_queue.get("utilization", latest_queue.get("rho", 0)))
    queue_length = float(latest_queue.get("queue_length", latest_queue.get("Lq", 0)))
    waiting_time = float(latest_queue.get("waiting_time", latest_queue.get("Wq", 0)))
    stability = latest_queue.get("stability", "Unknown")

    return {
        "usl": {
            "peak_throughput": float(usl_metrics.get("peak_throughput", 0)),
            "optimal_users": float(usl_metrics.get("optimal_users", 0)),
            "saturation_point": float(usl_metrics.get("saturation_point", 0)),
            "scalability_efficiency": float(scalability_efficiency),
        },
        "little_law": {
            "requests_in_system": requests_in_system,
            "time_in_system": time_in_system,
        },
        "queueing": {
            "utilization": utilization,
            "queue_length": queue_length,
            "waiting_time": waiting_time,
            "stability": stability,
        },
        "runtime": {
            "current_users": current_users,
            "throughput": throughput,
            "response_time": response_time,
            "cpu_usage": cpu_usage,
            "memory_usage": memory_usage,
            "disk_io": disk_io,
            "network_io": network_io,
            "error_rate": error_rate,
        },
    }


def cross_check_database_pressure(bottleneck_result, db_monitor_summary):
    """
    Cross-references db_monitor.py's connection-pool pressure reading
    against bottleneck.py's demand-based verdict - an app.py-level
    analogue of bottleneck.py's own queue_cross_check (see
    mathematical_engine/bottleneck.py), specifically for connection-pool
    pressure.

    This is deliberately NOT fed into bottleneck.py's component_demands:
    db_monitor.py measures connection/pool PRESSURE (active connections
    vs. max_connections), not per-request SERVICE DEMAND (visits/request
    x time-per-visit) the way forced_flow_components does - conflating
    the two would mix units and produce a meaningless "demand" number.
    What db_monitor's reading CAN still do is corroborate or contradict
    the bottleneck verdict as independent evidence, the same role
    queueing's congestion_risk already plays inside bottleneck.py itself.

    Returns None if there's nothing to cross-check (no db_monitor data,
    or no usable bottleneck result).
    """
    if not isinstance(db_monitor_summary, dict) or not isinstance(bottleneck_result, dict):
        return None

    pool_pct = db_monitor_summary.get("connection_pool_utilization_peak_pct")
    if pool_pct is None:
        return None

    engine = db_monitor_summary.get("engine")
    bottleneck_resource = (bottleneck_result.get("bottleneck") or {}).get("resource")

    high_pool_pressure = pool_pct >= 85.0
    # Anything other than the OS-level trio counts as "the bottleneck
    # already points at a component" - covers both a forced-flow-derived
    # component name and any future non-cpu/disk/network resource key.
    bottleneck_is_component = bottleneck_resource not in (None, "cpu", "disk", "network")

    agrees = (not high_pool_pressure) or bottleneck_is_component
    note = None
    if not agrees:
        note = (
            f"{engine or 'The database'}'s connection pool peaked at {pool_pct:.1f}% during this "
            f"run, but the identified bottleneck ('{bottleneck_resource}') doesn't reflect that - "
            f"if no 'forced_flow_components' instrumentation was supplied for {engine or 'this database'}, "
            f"supplying visits-per-request/service-time data for it would let the bottleneck analysis "
            f"weigh this evidence directly instead of only seeing it here."
        )

    return {
        "engine": engine,
        "peak_connection_pool_utilization_pct": pool_pct,
        "bottleneck_resource": bottleneck_resource,
        "agrees_with_bottleneck": agrees,
        "note": note,
    }


app = Flask(__name__)
CORS(app)

FRONTEND_FOLDER = "frontend"
github_manager = GitHubManager()
docker_manager = DockerManager()


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    return send_from_directory(FRONTEND_FOLDER, "index.html")


@app.route("/css/<path:filename>")
def serve_css(filename):
    return send_from_directory("frontend/css", filename)


@app.route("/js/<path:filename>")
def serve_js(filename):
    return send_from_directory("frontend/js", filename)


# ---------------------------------------------------------------------------
# App liveness (distinct from /api/health-check, which checks a specific
# cloned/dockerized project's container)
# ---------------------------------------------------------------------------

@app.route("/api/status", methods=["GET"])
def app_status():
    return jsonify({
        "success": True,
        "status": "ok",
        "message": "Performance prediction service is running."
    }), 200


# ---------------------------------------------------------------------------
# Centralized JSON error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def handle_not_found(error):
    return jsonify({
        "success": False,
        "message": "The requested endpoint does not exist."
    }), 404


@app.errorhandler(405)
def handle_method_not_allowed(error):
    return jsonify({
        "success": False,
        "message": "Method not allowed for this endpoint."
    }), 405


@app.errorhandler(500)
def handle_internal_error(error):
    return jsonify({
        "success": False,
        "message": "Internal server error."
    }), 500


# ---------------------------------------------------------------------------
# Repository management
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# GitHub Repository Management
# ---------------------------------------------------------------------------

@app.route("/api/github/clone", methods=["POST"])
def clone_repository():
    data = request.get_json(silent=True) or {}

    github_url = data.get("github_url")
    branch = data.get("branch")

    if not github_url:
        return jsonify({
            "success": False,
            "message": "GitHub URL is required."
        }), 400

    try:
        result = github_manager.clone_repository(
            github_url=github_url,
            branch=branch
        )

        if result["exists"]:
            return jsonify({
                "success": False,
                "exists": True,
                "project_name": result["project_name"],
                "path": result["path"],
                "message": "Repository already exists."
            }), 409

        return jsonify({
            "success": True,
            "exists": False,
            "project_name": result["project_name"],
            "path": result["path"],
            "url": result["url"],
            "branch": result["branch"],
            "message": "Repository cloned successfully."
        }), 200

    except ValueError as exc:
        return jsonify({
            "success": False,
            "message": str(exc)
        }), 400

    except RuntimeError as exc:
        return jsonify({
            "success": False,
            "message": str(exc)
        }), 500


@app.route("/api/github/delete", methods=["POST"])
def delete_repository():
    data = request.get_json(silent=True) or {}

    project_name = data.get("project_name")

    if not project_name:
        return jsonify({
            "success": False,
            "message": "Project name is required."
        }), 400

    try:
        github_manager.delete_repository(project_name)

        return jsonify({
            "success": True,
            "project_name": project_name,
            "message": "Repository deleted successfully."
        }), 200

    except ValueError as exc:
        return jsonify({
            "success": False,
            "message": str(exc)
        }), 400

    except RuntimeError as exc:
        return jsonify({
            "success": False,
            "message": str(exc)
        }), 500


@app.route("/api/github/repositories", methods=["GET"])
def list_repositories():
    try:
        repositories = github_manager.list_repositories()

        return jsonify({
            "success": True,
            "repositories": repositories,
            "count": len(repositories)
        }), 200

    except Exception as exc:
        return jsonify({
            "success": False,
            "message": f"Failed to list repositories: {str(exc)}"
        }), 500

# ---------------------------------------------------------------------------
# Framework Detection
# ---------------------------------------------------------------------------

@app.route("/api/framework/detect", methods=["POST"])
def detect_project_framework():
    data = request.get_json(silent=True) or {}

    project_name = data.get("project_name")

    if not project_name:
        return jsonify({
            "success": False,
            "message": "Project name is required."
        }), 400

    project_path = os.path.join(
        github_manager.workspace,
        project_name
    )

    if not os.path.isdir(project_path):
        return jsonify({
            "success": False,
            "message": "Repository not found."
        }), 404

    try:
        framework_result = detect_framework(project_path)

        return jsonify({
            "success": True,
            "project_name": project_name,
            "framework": framework_result,
            "message": "Framework detection completed successfully."
        }), 200

    except ValueError as exc:
        return jsonify({
            "success": False,
            "message": str(exc)
        }), 400

    except Exception as exc:
        return jsonify({
            "success": False,
            "message": f"Framework detection failed: {str(exc)}"
        }), 500


# ---------------------------------------------------------------------------
# Dependency Detection (database / cache)
# ---------------------------------------------------------------------------

@app.route("/api/dependencies/detect", methods=["POST"])
def detect_project_dependencies():
    """
    Static, best-effort detection of which database/cache a repository
    depends on (PostgreSQL, MySQL, MongoDB, Redis) from manifests,
    docker-compose service images, .env/config files, and Django's own
    DATABASES setting. Does not connect to anything - see
    dependency_detector.py's module docstring.
    """
    data = request.get_json(silent=True) or {}

    project_name = data.get("project_name")

    if not project_name:
        return jsonify({
            "success": False,
            "message": "Project name is required."
        }), 400

    project_path = os.path.join(
        github_manager.workspace,
        project_name
    )

    if not os.path.isdir(project_path):
        return jsonify({
            "success": False,
            "message": "Repository not found."
        }), 404

    try:
        dependency_result = detect_dependencies(project_path)

        return jsonify({
            "success": True,
            "project_name": project_name,
            "dependencies": dependency_result,
            "message": "Dependency detection completed successfully."
        }), 200

    except ValueError as exc:
        return jsonify({
            "success": False,
            "message": str(exc)
        }), 400

    except Exception as exc:
        return jsonify({
            "success": False,
            "message": f"Dependency detection failed: {str(exc)}"
        }), 500


# ---------------------------------------------------------------------------
# Entry Point Location
# ---------------------------------------------------------------------------

@app.route("/api/entry-point", methods=["POST"])
def locate_project_entry_point():
    data = request.get_json(silent=True) or {}

    project_name = data.get("project_name")
    detection_result = data.get("detection_result")

    if not project_name:
        return jsonify({
            "success": False,
            "message": "Project name is required."
        }), 400

    if not detection_result:
        return jsonify({
            "success": False,
            "message": "Framework detection result is required."
        }), 400

    project_path = os.path.join(
        github_manager.workspace,
        project_name
    )

    if not os.path.isdir(project_path):
        return jsonify({
            "success": False,
            "message": "Repository not found."
        }), 404

    try:
        location_result = locate_entry_point(
            project_path,
            detection_result
        )

        return jsonify({
            "success": True,
            "project_name": project_name,
            "entry_point": location_result,
            "message": "Entry point located successfully."
        }), 200

    except ValueError as exc:
        return jsonify({
            "success": False,
            "message": str(exc)
        }), 400

    except Exception as exc:
        return jsonify({
            "success": False,
            "message": f"Entry point location failed: {str(exc)}"
        }), 500


# ---------------------------------------------------------------------------
# Repository Validation
# ---------------------------------------------------------------------------

@app.route("/api/validate", methods=["POST"])
def validate_repository():
    data = request.get_json(silent=True) or {}

    project_name = data.get("project_name")
    detection_result = data.get("detection_result")
    location_result = data.get("location_result")

    if not project_name:
        return jsonify({
            "success": False,
            "message": "Project name is required."
        }), 400

    if not detection_result:
        return jsonify({
            "success": False,
            "message": "Framework detection result is required."
        }), 400

    if not location_result:
        return jsonify({
            "success": False,
            "message": "Entry point location result is required."
        }), 400

    project_path = os.path.join(
        github_manager.workspace,
        project_name
    )

    if not os.path.isdir(project_path):
        return jsonify({
            "success": False,
            "message": "Repository not found."
        }), 404

    try:
        validator = RepositoryValidator(project_path)

        valid, errors, warnings = validator.validate(
            detection_result,
            location_result
        )

        return jsonify({
            "success": True,
            "valid": valid,
            "project_name": project_name,
            "errors": errors,
            "warnings": warnings,
            "message": (
                "Repository validation passed."
                if valid
                else "Repository validation failed."
            )
        }), 200

    except ValueError as exc:
        return jsonify({
            "success": False,
            "message": str(exc)
        }), 400

    except Exception as exc:
        return jsonify({
            "success": False,
            "message": f"Repository validation failed: {str(exc)}"
        }), 500


# ---------------------------------------------------------------------------
# Docker Image Build
# ---------------------------------------------------------------------------

@app.route("/api/docker/build", methods=["POST"])
def build_docker_image():
    data = request.get_json(silent=True) or {}

    project_name = data.get("project_name")
    image_name = data.get("image_name", "performance-image")
    detection_result = data.get("detection_result")
    location_result = data.get("location_result")

    if not project_name:
        return jsonify({
            "success": False,
            "message": "Project name is required."
        }), 400

    project_path = os.path.join(
        github_manager.workspace,
        project_name
    )

    if not os.path.isdir(project_path):
        return jsonify({
            "success": False,
            "message": "Repository not found."
        }), 404

    try:
        result = docker_manager.build_image(
            project_path=project_path,
            image_name=image_name,
            detection_result=detection_result,
            location_result=location_result
        )

        status_code = 200 if result.get("success") else 500

        return jsonify(result), status_code

    except ValueError as exc:
        return jsonify({
            "success": False,
            "message": str(exc)
        }), 400

    except RuntimeError as exc:
        return jsonify({
            "success": False,
            "message": str(exc)
        }), 500

    except Exception as exc:
        return jsonify({
            "success": False,
            "message": f"Docker build failed: {str(exc)}"
        }), 500


# ---------------------------------------------------------------------------
# Docker Container Run
# ---------------------------------------------------------------------------

@app.route("/api/docker/run", methods=["POST"])
def run_docker_container():
    """
    Starts a container from a previously built image and returns the
    dynamically-allocated host port. Call this after /api/docker/build
    (pass its "container_port" here) and before /api/health-check /
    /api/load-testing/run - both of those need the "port" this returns.
    """
    data = request.get_json(silent=True) or {}

    image_name = data.get("image_name", "performance-image")
    container_name = data.get("container_name", "performance-container")
    container_port = data.get("container_port")

    if not container_port:
        return jsonify({
            "success": False,
            "message": "container_port is required (returned by /api/docker/build)."
        }), 400

    try:
        result = docker_manager.run_container(
            image_name=image_name,
            container_name=container_name,
            container_port=container_port,
        )

        status_code = 200 if result.get("success") else 500
        return jsonify(result), status_code

    except ValueError as exc:
        return jsonify({
            "success": False,
            "message": str(exc)
        }), 400

    except RuntimeError as exc:
        return jsonify({
            "success": False,
            "message": str(exc)
        }), 500

    except Exception as exc:
        return jsonify({
            "success": False,
            "message": f"Failed to start container: {str(exc)}"
        }), 500


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------

@app.route("/api/health-check", methods=["POST"])
def health_check():
    data = request.get_json(silent=True) or {}

    project_name = data.get("project_name")
    container_id = data.get("container_id")
    host = data.get("host", "localhost")
    port = data.get("port")

    if not project_name:
        return jsonify({
            "success": False,
            "message": "Project name is required."
        }), 400

    if not container_id:
        return jsonify({
            "success": False,
            "message": "Container ID is required."
        }), 400

    if not port:
        return jsonify({
            "success": False,
            "message": "Container port is required."
        }), 400

    project_path = os.path.join(
        github_manager.workspace,
        project_name
    )

    if not os.path.isdir(project_path):
        return jsonify({
            "success": False,
            "message": "Repository not found."
        }), 404

    try:
        checker = HealthChecker(
            docker_manager=docker_manager
        )

        result = checker.wait_until_ready(
            host=host,
            port=int(port),
            container_id=container_id
        )

        return jsonify({
            "success": result.get("ready", False),
            "healthy": result.get("ready", False),
            "project_name": project_name,
            "health_check": result,
            "message": (
                "Application is running and ready."
                if result.get("ready")
                else result.get("reason", "Application failed to become ready.")
            )
        }), 200 if result.get("ready") else 503

    except ValueError as exc:
        return jsonify({
            "success": False,
            "healthy": False,
            "message": str(exc)
        }), 400

    except Exception as exc:
        return jsonify({
            "success": False,
            "healthy": False,
            "message": f"Health check failed: {str(exc)}"
        }), 500


# ---------------------------------------------------------------------------
# Database / Cache Runtime Monitoring
# ---------------------------------------------------------------------------
#
# Distinct from dependency detection above: that's static ("what does this
# repo's config claim it depends on"); this is live ("how loaded is the
# actual running instance right now"). Requires real connection details -
# typically the same host/port/credentials dependency_detector.py found
# evidence of (e.g. a docker-compose POSTGRES_PASSWORD) - and an engine
# that's actually reachable, so this is opt-in and separate from the main
# pipeline rather than something /api/load-testing/run does automatically.
#
# Call /api/db-monitor/start before kicking off /api/load-testing/run,
# hang on to the returned monitor_id, then call /api/db-monitor/stop once
# the load test finishes to get the connection/pool-pressure summary for
# that window.

@app.route("/api/db-monitor/start", methods=["POST"])
def start_db_monitor():
    data = request.get_json(silent=True) or {}

    engine = data.get("engine")
    host = data.get("host")

    if not engine:
        return jsonify({
            "success": False,
            "message": "engine is required (postgresql, mysql, mongodb, or redis)."
        }), 400

    if not host:
        return jsonify({
            "success": False,
            "message": "host is required."
        }), 400

    try:
        connection_params = ConnectionParams(
            host=host,
            port=data.get("port"),
            user=data.get("user"),
            password=data.get("password"),
            database=data.get("database"),
        )

        monitor_kwargs = {}
        if "interval" in data:
            monitor_kwargs["interval"] = data["interval"]
        if "connect_timeout_seconds" in data:
            monitor_kwargs["connect_timeout_seconds"] = data["connect_timeout_seconds"]

        monitor = DBMonitor(
            engine=engine,
            connection_params=connection_params,
            **monitor_kwargs,
        )
        monitor.start()

        monitor_id = uuid.uuid4().hex
        active_db_monitors[monitor_id] = monitor

        return jsonify({
            "success": True,
            "monitor_id": monitor_id,
            "engine": engine,
            "host": host,
            "message": "Database monitoring started."
        }), 200

    except DBMonitorError as exc:
        return jsonify({
            "success": False,
            "message": str(exc)
        }), 400

    except Exception as exc:
        return jsonify({
            "success": False,
            "message": f"Failed to start database monitoring: {str(exc)}"
        }), 500


@app.route("/api/db-monitor/stop", methods=["POST"])
def stop_db_monitor():
    data = request.get_json(silent=True) or {}

    monitor_id = data.get("monitor_id")

    if not monitor_id:
        return jsonify({
            "success": False,
            "message": "monitor_id is required (returned by /api/db-monitor/start)."
        }), 400

    monitor = active_db_monitors.pop(monitor_id, None)

    if monitor is None:
        return jsonify({
            "success": False,
            "message": "No active database monitor found for that monitor_id - it may already have been stopped."
        }), 404

    try:
        monitor.stop()
        summary = monitor.summary()

        return jsonify({
            "success": True,
            "monitor_id": monitor_id,
            "summary": sanitize_for_json(summary),
            "message": "Database monitoring stopped."
        }), 200

    except Exception as exc:
        return jsonify({
            "success": False,
            "message": f"Failed to stop database monitoring cleanly: {str(exc)}"
        }), 500


# ---------------------------------------------------------------------------
# Application Descriptor
# ---------------------------------------------------------------------------
#
# Normalizes whichever pipeline stage results are available so far
# (framework detection, entry-point resolution, route discovery, Docker
# build/run, dependency detection) into the one ApplicationDescriptor shape
# the mathematical engine, frontend, and any future report generator can
# consume without framework-specific knowledge. Safe to call at any
# checkpoint - every input besides detection_result is optional.

@app.route("/api/application-descriptor", methods=["POST"])
def get_application_descriptor():
    data = request.get_json(silent=True) or {}

    detection_result = data.get("detection_result")

    if not detection_result:
        return jsonify({
            "success": False,
            "message": "Framework detection result is required."
        }), 400

    try:
        descriptor = build_application_descriptor(
            detection_result=detection_result,
            location_result=data.get("location_result"),
            route_discovery_result=data.get("route_discovery_result"),
            docker_build_result=data.get("docker_build_result"),
            docker_run_result=data.get("docker_run_result"),
            dependency_result=data.get("dependency_result"),
        )

        return jsonify({
            "success": True,
            "descriptor": descriptor,
            "message": "Application descriptor built successfully."
        }), 200

    except DescriptorBuildError as exc:
        return jsonify({
            "success": False,
            "message": str(exc)
        }), 400

    except Exception as exc:
        return jsonify({
            "success": False,
            "message": f"Failed to build application descriptor: {str(exc)}"
        }), 500


# ---------------------------------------------------------------------------
# Route Discovery
# ---------------------------------------------------------------------------

@app.route("/api/routes", methods=["POST"])
def discover_project_routes():
    data = request.get_json(silent=True) or {}

    project_name = data.get("project_name")
    detection_result = data.get("detection_result")
    location_result = data.get("location_result")

    if not project_name:
        return jsonify({
            "success": False,
            "message": "Project name is required."
        }), 400

    if not detection_result:
        return jsonify({
            "success": False,
            "message": "Framework detection result is required."
        }), 400

    project_path = os.path.join(
        github_manager.workspace,
        project_name
    )

    if not os.path.isdir(project_path):
        return jsonify({
            "success": False,
            "message": "Repository not found."
        }), 404

    try:
        result = RouteDiscovery().discover(
            repo_path=project_path,
            detection_result=detection_result,
            location_result=location_result
        )

        return jsonify({
            "success": True,
            "project_name": project_name,
            "routes": result,
            "count": result.get("route_count", 0),
            "message": "Routes discovered successfully."
        }), 200

    except ValueError as exc:
        return jsonify({
            "success": False,
            "message": str(exc)
        }), 400

    except Exception as exc:
        return jsonify({
            "success": False,
            "message": f"Route discovery failed: {str(exc)}"
        }), 500

# ---------------------------------------------------------------------------
# Locust File Generation
# ---------------------------------------------------------------------------

@app.route("/api/locust/generate", methods=["POST"])
def generate_locust():
    data = request.get_json(silent=True) or {}

    project_name = data.get("project_name")
    route_discovery_result = data.get("route_discovery_result")

    if not project_name:
        return jsonify({
            "success": False,
            "message": "Project name is required."
        }), 400

    if not route_discovery_result:
        return jsonify({
            "success": False,
            "message": "Route discovery result is required."
        }), 400

    project_path = os.path.join(
        github_manager.workspace,
        project_name
    )

    if not os.path.isdir(project_path):
        return jsonify({
            "success": False,
            "message": "Repository not found."
        }), 404

    try:
        result = LocustGenerator().generate(
            project_path=project_path,
            route_discovery_result=route_discovery_result
        )

        return jsonify({
            "success": True,
            "project_name": project_name,
            "locust": result,
            "message": "Locust file generated successfully."
        }), 200

    except ValueError as exc:
        return jsonify({
            "success": False,
            "message": str(exc)
        }), 400

    except Exception as exc:
        return jsonify({
            "success": False,
            "message": f"Locust generation failed: {str(exc)}"
        }), 500


# ---------------------------------------------------------------------------
# Load Testing + Full Mathematical Pipeline
# ---------------------------------------------------------------------------

@app.route("/api/load-testing/run", methods=["POST"])
def run_load_testing():
    try:
        data = request.get_json(silent=True) or {}

        project_name = data.get("project_name")
        container_id = data.get("container_id")
        container_host_port = data.get("port")

        if not project_name:
            return jsonify({
                "success": False,
                "message": "Project name is required."
            }), 400

        if not container_id:
            return jsonify({
                "success": False,
                "message": "Container ID is required."
            }), 400

        if not container_host_port:
            return jsonify({
                "success": False,
                "message": (
                    "Container host port is required "
                    "(the \"port\" returned by /api/docker/run)."
                )
            }), 400

        project_path = os.path.join(
            github_manager.workspace,
            project_name
        )

        if not os.path.isdir(project_path):
            return jsonify({
                "success": False,
                "message": f"Project not found: {project_name}"
            }), 404

        # Optional instrumentation for Forced Flow analysis.
        forced_flow_components = data.get("forced_flow_components")

        # Optional measured higher-load results for USL validation.
        actual_throughput_by_users = data.get(
            "actual_throughput_by_users"
        )


        # ---------------------------------------------------------------
        # Dependency detection
        # ---------------------------------------------------------------
        try:
            dependency_result = detect_dependencies(project_path)
        except Exception as dependency_error:
            print("\nDEPENDENCY DETECTION ERROR:")
            print(str(dependency_error))

            dependency_result = {
                "success": False,
                "error": str(dependency_error)
            }

        # ---------------------------------------------------------------
        # Database / Cache monitoring
        # ---------------------------------------------------------------
        #
        # Automatically resolve database configuration from the cloned
        # repository. The frontend does NOT send credentials.
        # ---------------------------------------------------------------
        db_monitor = None
        db_monitor_summary = None

        try:
            db_config = resolve_database_monitor_config(
                project_path,
                dependency_result=dependency_result,
            )

            # Keep only safe, non-secret information in the result returned
            # to the frontend.
            result_db_config = {
                "available": bool(db_config.get("enabled")),
                "engine": db_config.get("engine"),
                "host": db_config.get("host"),
                "port": db_config.get("port"),
                "source": db_config.get("source", []),
                "detection_confidence": db_config.get("detection_confidence"),
                "reason": db_config.get("reason"),
            }

            if db_config.get("enabled"):
                db_connection_params = ConnectionParams(
                    host=db_config.get("host"),
                    port=db_config.get("port"),
                    user=db_config.get("user"),
                    password=db_config.get("password"),
                    database=db_config.get("database"),
                )

                db_monitor = DBMonitor(
                    engine=db_config.get("engine"),
                    connection_params=db_connection_params,
                    interval=1.0,
                    connect_timeout_seconds=3.0,
                )

                db_monitor.start()

                print("\n=== DATABASE MONITORING STARTED ===")
                print(f"Engine: {db_config.get('engine')}")
                print(f"Host: {db_config.get('host')}")
                print(f"Port: {db_config.get('port')}")

            else:
                print("\n=== DATABASE MONITORING NOT AVAILABLE ===")
                print(db_config.get("reason", "No usable database configuration found."))

        except Exception as db_config_error:
            print("\nDATABASE CONFIGURATION ERROR:")
            print(str(db_config_error))

            result_db_config = {
                "available": False,
                "engine": None,
                "host": None,
                "port": None,
                "source": [],
                "detection_confidence": None,
                "reason": str(db_config_error),
            }

        # ---------------------------------------------------------------
        # Locust load testing
        # ---------------------------------------------------------------
        runner = LocustRunner(
            host=f"http://localhost:{container_host_port}",
            repetitions=1,
            run_time="15s",
            spawn_rate=10,
        )

        user_levels_override = data.get("user_levels")

        result = runner.run(
            project_path=project_path,
            container_id=container_id,
            user_levels=user_levels_override

        )

        result["database_monitoring"] = result_db_config

        # ---------------------------------------------------------------
        # Stop database monitoring after Locust finishes
        # ---------------------------------------------------------------
        if db_monitor is not None:
            try:
                db_monitor.stop()
                db_monitor_summary = db_monitor.summary()

                result["database_metrics"] = sanitize_for_json(
                    db_monitor_summary
                )

                print("\n=== DATABASE MONITORING RESULT ===")
                print(db_monitor_summary)

            except Exception as db_error:
                print("\nDATABASE MONITORING STOP ERROR:")
                print(str(db_error))

                result["database_metrics"] = {
                    "available": False,
                    "error": str(db_error),
                }

                result["database_monitoring"]["available"] = False
                result["database_monitoring"]["reason"] = str(db_error)

        # Stop immediately if Locust itself failed.
        if not result.get("success"):
            return jsonify({
                "success": False,
                "message": result.get(
                    "message",
                    "Load testing failed."
                ),
                "load_testing": result
            }), 500

        levels = result.get("levels", [])

        # ---------------------------------------------------------------
        # Normalize service-capacity API contract
        #
        # LocustRunner internally calculates:
        #
        #   service_rate = peak observed throughput
        #   service_time = 1 / service_rate
        #
        # Therefore the mathematical pipeline receives the standardized
        # public source name rather than the runner's old internal label.
        # ---------------------------------------------------------------
        service_capacity = result.get("service_capacity") or {}

        service_time = float(
            service_capacity.get("service_time", 0.0) or 0.0
        )

        service_rate = float(
            service_capacity.get("service_rate", 0.0) or 0.0
        )

        if service_time > 0 and service_rate > 0:
            service_rate_source = "derived_from_service_time"
        else:
            service_rate_source = None

        service_capacity["service_time"] = service_time
        service_capacity["service_rate"] = service_rate
        service_capacity["service_time_unit"] = "seconds"
        service_capacity["service_rate_unit"] = "requests/sec"
        service_capacity["source"] = service_rate_source

        result["service_capacity"] = service_capacity

        # Also keep the normalized value on every aggregated level.
        for level in levels:
            level["service_time"] = service_time
            level["service_rate"] = service_rate
            level["service_rate_source"] = service_rate_source

        result["dependencies"] = dependency_result

        # --------------------------------------------------------------
        # USL — Universal Scalability Law
        # --------------------------------------------------------------
        usl_result = None

        try:
            # Extract measured load-test data
            user_levels = [
                float(level["users"])
                for level in levels
                if level.get("users") is not None
                and level.get("mean_throughput") is not None
            ]

            throughput = [
                float(level["mean_throughput"])
                for level in levels
                if level.get("users") is not None
                and level.get("mean_throughput") is not None
            ]

            print("\n" + "=" * 70)
            print("USL — UNIVERSAL SCALABILITY LAW")
            print("=" * 70)

            print("Measured User Levels :", user_levels)
            print("Measured Throughput  :", throughput)

            # ----------------------------------------------------------
            # Run the final USL engine
            # ----------------------------------------------------------
            usl_result = run_usl_analysis(
                user_levels=user_levels,
                throughput=throughput,
                predict_at=[1000, 2000, 5000],
            )

            print("\n=== USL RESULT ===")
            print(usl_result)

            # ----------------------------------------------------------
            # Store result for API/frontend
            # ----------------------------------------------------------
            result["prediction"] = {
                "usl": usl_result
            }

        except Exception as usl_error:
        
            print("\n=== USL PREDICTION ERROR ===")
            print(str(usl_error))

            result["prediction"] = {
                "usl": {
                    "success": False,
                    "error": str(usl_error)
                }
            }

        # --------------------------------------------------------------
        # Little's Law — one snapshot per tested load level
        # --------------------------------------------------------------
        little_law_inputs = [
            {
                "arrival_rate": level["mean_throughput"],
                "average_response_time": level["mean_average_response_time"],
                "response_time_unit": "ms",
                # Locust's configured concurrent-user count for this level - enables
                # compare_concurrency()'s L-vs-observed-concurrency consistency check
                # (see mathematical_models.md Section 1) instead of leaving it silent.
                "observed_concurrency": level["users"],
                "metadata": {
                    "load_users": level["users"],
                    "throughput_source": "locust",
                    "response_time_source": "locust",
                },
            }
            for level in levels
        ]

        little_law_results = analyze_workload_levels(little_law_inputs)

        print("\n=== LITTLE'S LAW INPUT DATA ===")
        print(little_law_inputs)

        print("\n=== LITTLE'S LAW RESULT ===")
        for i, ll_result in enumerate(little_law_results):
            print(
                f"Users: {levels[i]['users']} | "
                f"Throughput: {ll_result.get('arrival_rate', 0):.2f} req/s | "
                f"Response Time: {ll_result.get('response_time', 0):.6f} s | "
                f"L: {ll_result.get('requests_in_system', 0):.2f} | "
                f"Classification: {ll_result.get('load_classification', 'N/A')} | "
                f"Concurrent Requests: {ll_result.get('concurrent_requests', 0):.2f} | "
                f"System Occupancy: {ll_result.get('system_occupancy', 0):.2f} | "
                f"Signals: {ll_result.get('signals', {})}"
            )

        result["little_law"] = {"levels": little_law_results}

        # --------------------------------------------------------------
        # Queueing Theory (M/M/1) — tested levels + USL-predicted levels
        # --------------------------------------------------------------
        service_capacity = result.get("service_capacity", {})
        service_rate = service_capacity.get("service_rate", 0)
        service_time = service_capacity.get("service_time", 0)
        service_rate_source = service_capacity.get("source", "estimated_from_lowest_load_response_time")

        queueing_results = []
        for level in levels:
            queue_input = {
                "arrival_rate": level.get("mean_throughput", 0),
                "service_rate": service_rate,
                "service_rate_source": service_rate_source,
                # M/M/1 -> M/G/1 upgrade (mathematical_models.md Section 2.3): the
                # aggregated Locust level doesn't retain a true median, so mean
                # response time stands in for P50 here - an approximation, not a
                # real percentile, but it lets Cs^2 be estimated from the response-
                # time SPREAD this pipeline already measures (mean_p95) instead of
                # silently assuming exponential service time (Cs^2=1) for every run.
                "service_time_p50": level.get("mean_average_response_time", 0),
                "service_time_p95": level.get("mean_p95", 0),
                "service_time_unit": "ms",
                "metadata": {
                    "users": level.get("users", 0),
                    "throughput_source": "locust",
                    "service_capacity_source": service_rate_source,
                    "service_time_p50_is_approximate": True,
                },
            }
            queueing_results.append(analyze_queue_safe(queue_input))

        print("\n=== QUEUEING THEORY INPUT DATA ===")
        for level, queue_result in zip(levels, queueing_results):
            print(
                f"Users: {level['users']} | "
                f"λ: {queue_result.get('arrival_rate', 0):.2f} req/s | "
                f"μ: {queue_result.get('service_rate', 0):.2f} req/s | "
                f"ρ: {queue_result.get('utilization', 0):.4f} | "
                f"Stability: {queue_result.get('stability', 'N/A')}"
            )

        # Same M/M/1 model, but fed the USL-predicted throughput at future
        # user levels instead of the actually-measured throughput.
        predicted_queueing_results = []
        usl_predictions = result.get("prediction", {}).get("usl", {}).get("predictions", {})

        for predicted_users, predicted_throughput in usl_predictions.items():
            queue_input = {
                "arrival_rate": float(predicted_throughput),
                "service_rate": service_rate,
                "service_rate_source": service_rate_source,
                "metadata": {
                    "users": float(predicted_users),
                    "throughput_source": "usl_prediction",
                    "service_capacity_source": service_rate_source,
                },
            }
            queue_result = analyze_queue_safe(queue_input)
            queue_result["predicted_users"] = float(predicted_users)
            queue_result["throughput_source"] = "usl_prediction"
            predicted_queueing_results.append(queue_result)

        print("\n=== PREDICTED QUEUEING THEORY ===")
        for queue_result in predicted_queueing_results:
            print(
                f"Predicted Users: {queue_result['predicted_users']:.0f} | "
                f"λ: {queue_result.get('arrival_rate', 0):.2f} req/s | "
                f"μ: {queue_result.get('service_rate', 0):.2f} req/s | "
                f"ρ: {queue_result.get('utilization', 0):.4f} | "
                f"Stability: {queue_result.get('stability', 'N/A')}"
            )

        result["queueing"] = {
            # Each level's own "queueing_model" (M/M/1 vs M/G/1) already
            # reflects whether real service-time variability data was
            # available for THAT level (see queueing.py's
            # service_time_variability_source) - this summary label
            # mirrors the latest level's rather than hardcoding "M/M/1",
            # which predates the M/G/1 upgrade and would misreport every
            # run that actually used it.
            "model": queueing_results[-1].get("queueing_model", "M/M/1") if queueing_results else "M/M/1",
            "service_time": service_time,
            "service_rate": service_rate,
            "service_rate_source": service_rate_source,
            "service_time_unit": "seconds",
            "service_rate_unit": "requests/sec",
            "levels": queueing_results,
            "predicted_levels": predicted_queueing_results,
        }

        # --------------------------------------------------------------
        # Shared "current state" snapshot, built once from the latest
        # tested load level. Reused by Bottleneck Analysis, Capacity
        # Planning, and Scalability Prediction below so all three see
        # exactly the same numbers (previously this was built twice,
        # slightly differently, in the Capacity and Scalability sections).
        # --------------------------------------------------------------
        latest_level = levels[-1] if levels else None

        current_runtime_snapshot = None
        if latest_level is not None:
            current_runtime_snapshot = {
                "current_users": float(latest_level["users"]),
                "throughput": float(latest_level["mean_throughput"]),
                "response_time": float(latest_level["mean_average_response_time"]),
                "cpu_usage": float(latest_level.get("mean_cpu_usage", 0.0)),
                "memory_usage": float(latest_level.get("mean_memory_usage", 0.0)),
                "disk_io": float(
                    latest_level.get("mean_disk_read_mb_s", 0.0)
                    + latest_level.get("mean_disk_write_mb_s", 0.0)
                ),
                "network_io": float(
                    latest_level.get("mean_network_rx_mb_s", 0.0)
                    + latest_level.get("mean_network_tx_mb_s", 0.0)
                ),
                "error_rate": float(latest_level.get("mean_error_rate", 0.0)),
            }

        # ================================================================
        # FORCED FLOW (component-level service demand) - only runs when the
        # caller supplied visits/service-time instrumentation for a
        # database/cache/external API (forced_flow_components). Merged into
        # the Bottleneck stage below so a component can be identified as the
        # bottleneck alongside cpu/disk/network, not just reported separately.
        # ================================================================
        print("\n" + "=" * 60)
        print("FORCED FLOW ANALYSIS")
        print("=" * 60)

        forced_flow_result = None
        try:
            if not forced_flow_components:
                forced_flow_result = {
                    "available": False,
                    "reason": (
                        "No component instrumentation (forced_flow_components) was supplied "
                        "for this run - most cloned repositories don't have this added."
                    ),
                }
            elif current_runtime_snapshot is None:
                raise ValueError("No load-test levels available for Forced Flow analysis.")
            else:
                forced_flow_result = analyze_forced_flow({
                    "system_throughput": current_runtime_snapshot["throughput"],
                    "components": forced_flow_components,
                })

            print("\n=== FORCED FLOW RESULT ===")
            print(forced_flow_result)

        except Exception as forced_flow_error:
            print("\nFORCED FLOW ANALYSIS ERROR:")
            print(str(forced_flow_error))
            forced_flow_result = {"available": False, "reason": str(forced_flow_error)}

        result["forced_flow"] = forced_flow_result

        # ================================================================
        # BOTTLENECK ANALYSIS (Utilization Law / Forced Flow / Bottleneck
        # Analysis / Asymptotic Bounds) - feeds into Amdahl, Capacity
        # Planning, and Scalability Prediction below.
        # ================================================================
        print("\n" + "=" * 60)
        print("BOTTLENECK ANALYSIS")
        print("=" * 60)

        bottleneck_result = None
        try:
            if current_runtime_snapshot is None:
                raise ValueError("No load-test levels available for bottleneck analysis.")

            bottleneck_input = {
                "throughput": current_runtime_snapshot["throughput"],
                "current_users": current_runtime_snapshot["current_users"],
                "cpu_usage": current_runtime_snapshot["cpu_usage"],
                "disk_io": current_runtime_snapshot["disk_io"],
                "network_io": current_runtime_snapshot["network_io"],
                # think_time (Z) isn't currently tracked - Locust task wait
                # times aren't surfaced to app.py - so this defaults to 0s
                # inside bottleneck.py. Wire in a real value here if/when
                # that becomes available.
            }

            # Merge Forced Flow's component-level demand (database, cache,
            # external API) in, if any was computed above - same shape
            # bottleneck.py already expects, so no adapter code is needed.
            component_demands = to_bottleneck_resources(forced_flow_result)
            if component_demands:
                bottleneck_input["component_demands"] = component_demands

            print("\n=== BOTTLENECK INPUT ===")
            print(bottleneck_input)

            bottleneck_result = analyze_bottleneck_safe(
                bottleneck_input,
                capacity_config=BOTTLENECK_CAPACITY_CONFIG,
            )

            print("\n=== BOTTLENECK RESULT ===")
            print(bottleneck_result)

        except Exception as bottleneck_error:
            print("\nBOTTLENECK ANALYSIS ERROR:")
            print(str(bottleneck_error))
            bottleneck_result = None

        result["bottleneck"] = bottleneck_result

        # Cross-reference db_monitor.py's connection-pool pressure (if
        # database monitoring was enabled for this run) against the
        # bottleneck verdict above - see cross_check_database_pressure()'s
        # docstring for why this is a corroboration check, not a
        # component_demands input.
        result["database_pressure_cross_check"] = cross_check_database_pressure(
            bottleneck_result, db_monitor_summary
        )

        # ================================================================
        # AMDAHL'S LAW - optimization ceiling for the identified bottleneck
        # (how much fixing it would actually help, and what becomes the new
        # constraint) - feeds into Scalability Prediction and Recommendations
        # below.
        # ================================================================
        print("\n" + "=" * 60)
        print("AMDAHL'S LAW ANALYSIS")
        print("=" * 60)

        try:
            if not bottleneck_result:
                amdahl_result = {
                    "available": False,
                    "reason": "Bottleneck analysis did not produce usable results.",
                }
            else:
                amdahl_result = analyze_amdahl(bottleneck_result)

            print("\n=== AMDAHL RESULT ===")
            print(amdahl_result)

        except Exception as amdahl_error:
            print("\nAMDAHL ANALYSIS ERROR:")
            print(str(amdahl_error))
            amdahl_result = {"available": False, "reason": str(amdahl_error)}

        result["amdahl"] = amdahl_result

        # ================================================================
        # CAPACITY PLANNING
        # ================================================================
        print("\n" + "=" * 60)
        print("CAPACITY PLANNING")
        print("=" * 60)

        try:
            if current_runtime_snapshot is None:
                raise ValueError("No load-test levels available for capacity planning.")
            if usl_result is None:
                raise ValueError("USL analysis did not complete successfully; capacity planning needs it.")

            runtime_input = current_runtime_snapshot

            latest_ll = little_law_results[-1]
            little_law_input = {
                "requests_in_system": latest_ll["requests_in_system"],
                "time_in_system": latest_ll["response_time"],
            }

            latest_queue = queueing_results[-1]
            queueing_input = {
                "utilization": latest_queue.get("utilization", 0.0),
                "queue_length": latest_queue.get("queue_length", 0.0),
                "waiting_time": latest_queue.get("waiting_time", 0.0),
                "stability": latest_queue.get("stability", "Unknown"),
            }

            # M/M/1 goes to infinity once rho >= 1, but capacity.py needs
            # finite numbers, so clamp those cases to 0 here.
            if not math.isfinite(queueing_input["queue_length"]):
                queueing_input["queue_length"] = 0.0
            if not math.isfinite(queueing_input["waiting_time"]):
                queueing_input["waiting_time"] = 0.0

            usl_input = {
                "peak_throughput": usl_result["usl_metrics"]["peak_throughput"],
                "optimal_users": usl_result["usl_metrics"]["optimal_users"],
                "saturation_point": usl_result["usl_metrics"]["saturation_point"],
                "scalability_efficiency": usl_result["efficiency"].get(
                    float(usl_result["usl_metrics"]["optimal_users"]), 1.0
                ),
                "observed_min_users": min(level["users"] for level in levels),
                "observed_max_users": max(level["users"] for level in levels),
                "prediction_reliability": usl_result.get(
                    "prediction_reliability",
                    {"usable_for_extrapolation": True, "status": "unknown"},
                ),
            }

            capacity_input = {
                "usl": usl_input,
                "little_law": little_law_input,
                "queueing": queueing_input,
                "runtime": runtime_input,
                "bottleneck": bottleneck_result,
            }

            print("\n=== CAPACITY INPUT ===")
            print(capacity_input)

            capacity_result = analyze_capacity(
                capacity_input,
                disk_io_limit_mbps=DISK_IO_LIMIT_MBPS,
                network_io_limit_mbps=NETWORK_IO_LIMIT_MBPS,
            )

            print("\n=== CAPACITY RESULT ===")
            print(capacity_result)

            result["capacity"] = capacity_result

        except Exception as exc:
            print("\nCAPACITY PLANNING ERROR:")
            print(str(exc))
            result["capacity"] = {"success": False, "error": str(exc)}

        # ================================================================
        # SCALABILITY PREDICTION
        # ================================================================
        print("\n" + "=" * 60)
        print("SCALABILITY PREDICTION")
        print("=" * 60)

        try:
            if current_runtime_snapshot is None:
                raise ValueError("No load-test levels available for scalability prediction.")
            if usl_result is None:
                raise ValueError("USL analysis did not complete successfully; scalability prediction needs it.")

            # Future loads to project forward to - not the already-tested levels.
            prediction_targets = [600, 700, 800, 1000, 2000, 3000, 4000, 5000, 10000]

            usl_parameters = usl_result.get("parameters", {})
            usl_prediction_input = {
                "sigma": usl_parameters["sigma"],
                "kappa": usl_parameters["kappa"],
                "baseline_throughput": usl_parameters["baseline_throughput"],
                "peak_throughput": usl_result["usl_metrics"]["peak_throughput"],
                "optimal_users": usl_result["usl_metrics"]["optimal_users"],
                "saturation_point": usl_result["usl_metrics"]["saturation_point"],
            }

            capacity_prediction_input = result.get("capacity", {})
            if not capacity_prediction_input.get("results"):
                raise ValueError("Capacity analysis did not produce valid results.")

            runtime_prediction_input = current_runtime_snapshot

            # JSON object keys are always strings, but predict_scalability()
            # expects numeric user counts as keys - normalize before passing
            # through. This is the "validated" leg of observed/predicted/
            # validated (mathematical_models.md Section 7): if the caller
            # already has real measurements at some of these prediction
            # targets (from a separate, already-run higher-load experiment),
            # each one gets checked against what USL predicted instead of
            # the prediction being left forever unverified.
            normalized_actual_throughput_by_users = None
            if actual_throughput_by_users:
                normalized_actual_throughput_by_users = {
                    float(users): float(throughput_value)
                    for users, throughput_value in actual_throughput_by_users.items()
                }

            scalability_result = predict_scalability(
                usl_results=usl_prediction_input,
                capacity_results=capacity_prediction_input,
                runtime_metrics=runtime_prediction_input,
                prediction_targets=prediction_targets,
                bottleneck_results=bottleneck_result,
                amdahl_results=amdahl_result,
                actual_throughput_by_users=normalized_actual_throughput_by_users,
            )

            print("\n=== SCALABILITY PREDICTION RESULT ===")
            print(scalability_result)

            result["prediction"]["scalability"] = scalability_result

        except Exception as scalability_error:
            print("\nSCALABILITY PREDICTION ERROR:")
            print(str(scalability_error))
            result["prediction"]["scalability_error"] = str(scalability_error)
            result["prediction"]["scalability"] = None

        # ================================================================
        # SLO ANALYSIS - reframes the same evidence as a single production-
        # readiness question ("does the app stay within budget, and up to
        # how many users") rather than sigma/kappa/rho. Combines the
        # ALREADY-TESTED levels (a real, measured SLO capacity) with USL's
        # PREDICTED levels (a projected, softer-signal capacity) - reported
        # separately by slo.py so a report never implies the extrapolated
        # figure is as solid as the measured one.
        # ================================================================
        print("\n" + "=" * 60)
        print("SLO ANALYSIS")
        print("=" * 60)

        try:
            slo_thresholds = SLOThresholds(
                max_response_time_seconds=data.get(
                    "slo_max_response_time_seconds", DEFAULT_SLO_MAX_RESPONSE_TIME_SECONDS
                ),
                max_error_rate_percent=data.get(
                    "slo_max_error_rate_percent", DEFAULT_SLO_MAX_ERROR_RATE_PERCENT
                ),
            )

            # Locust's response times/percentiles are in milliseconds;
            # slo.py works in seconds.
            observed_slo_levels = [
                {
                    "users": float(level["users"]),
                    "response_time": float(level.get("mean_average_response_time", 0.0)) / 1000.0,
                    "error_rate_percent": float(level.get("mean_error_rate", 0.0)),
                    "source": "observed",
                }
                for level in levels
            ]

            predicted_slo_levels = []
            scalability_for_slo = result.get("prediction", {}).get("scalability")
            if scalability_for_slo and scalability_for_slo.get("predictions"):
                predicted_slo_levels = [
                    {
                        "users": prediction["users"],
                        "response_time": prediction["predicted_response_time"] / 1000.0,
                        "error_rate_percent": prediction["predicted_error_rate"],
                        "source": "predicted",
                    }
                    for prediction in scalability_for_slo["predictions"]
                ]

            slo_result = analyze_slo(observed_slo_levels + predicted_slo_levels, thresholds=slo_thresholds)

            print("\n=== SLO RESULT ===")
            print(slo_result)

            result["slo"] = slo_result

        except Exception as slo_error:
            print("\nSLO ANALYSIS ERROR:")
            print(str(slo_error))
            result["slo"] = {"success": False, "error": str(slo_error)}

        # ================================================================
        # RECOMMENDATION ENGINE
        # ================================================================
        print("\n" + "=" * 60)
        print("RECOMMENDATION ENGINE")
        print("=" * 60)

        try:
            capacity_results = result.get("capacity", {})
            scalability_results = result.get("prediction", {}).get("scalability")

            if not capacity_results or not capacity_results.get("results"):
                raise ValueError("Capacity analysis did not produce valid results.")

            # Feed the engine the same latest-level snapshots capacity
            # planning used, not the full per-level history — the engine
            # expects a single Little's Law / queueing snapshot.
            little_law_recommendation_input = little_law_results[-1] if little_law_results else None
            queueing_recommendation_input = queueing_results[-1] if queueing_results else None

            recommendation_result = generate_recommendations(
                capacity_results=capacity_results,
                scalability_results=scalability_results,
                usl_results=usl_result,
                little_law_results=little_law_recommendation_input,
                queueing_results=queueing_recommendation_input,
                amdahl_results=amdahl_result,
            )

            result["recommendations"] = recommendation_result

            print("\n=== RECOMMENDATION RESULT ===")
            print(recommendation_result)

        except Exception as recommendation_error:
            print("\nRECOMMENDATION ENGINE ERROR:")
            print(str(recommendation_error))
            result["recommendations"] = {"success": False, "error": str(recommendation_error)}

        return jsonify(sanitize_for_json(result)), 200

    except Exception as e:
        print("Load Testing Error:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500

# ---------------------------------------------------------------------------
# Validation Layer (experiments/, Phase A/B/C, model validation, ablation)
# ---------------------------------------------------------------------------

@app.route("/api/experiment/initialize", methods=["POST"])
def initialize_experiment():
    data = request.get_json(silent=True) or {}

    experiment_id = data.get("experiment_id")
    if not experiment_id:
        return jsonify({"success": False, "message": "experiment_id is required."}), 400

    try:
        exp = Experiment(EXPERIMENTS_DIR, experiment_id)
        metadata = exp.initialize(
            target_repository=data.get("target_repository"),
            target_commit=data.get("target_commit"),
            framework=data.get("framework"),
            docker_image=data.get("docker_image"),
            load_test_config=data.get("load_test_config"),
            force=bool(data.get("force", False)),
        )
        return jsonify({"success": True, "metadata": metadata, "paths": exp.paths}), 200

    except ExperimentError as exc:
        return jsonify({"success": False, "message": str(exc)}), 409

    except Exception as exc:
        return jsonify({"success": False, "message": f"Experiment initialization failed: {str(exc)}"}), 500


@app.route("/api/experiment/list", methods=["GET"])
def list_all_experiments():
    try:
        return jsonify({"success": True, "experiments": list_experiments(EXPERIMENTS_DIR)}), 200
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@app.route("/api/experiment/<experiment_id>/paths", methods=["GET"])
def get_experiment_paths(experiment_id):
    exp = Experiment(EXPERIMENTS_DIR, experiment_id)
    if not exp.exists():
        return jsonify({"success": False, "message": f"Experiment '{experiment_id}' not found."}), 404
    return jsonify({"success": True, "paths": exp.paths, "metadata": exp.load_metadata()}), 200


@app.route("/api/validation/freeze-predictions", methods=["POST"])
def freeze_prediction_phase_a():
    """
    PHASE A. Call immediately after a fitting run (the normal 20-500
    /api/load-testing/run call) completes - pass through that response's
    usl_results/capacity_results/bottleneck_results/amdahl_results
    sections directly, plus the untested levels you intend to validate
    against later.
    """
    data = request.get_json(silent=True) or {}

    experiment_id = data.get("experiment_id")
    usl_results = data.get("usl_results")
    capacity_results = data.get("capacity_results")
    runtime_metrics = data.get("runtime_metrics")
    prediction_targets = data.get("prediction_targets")

    if not experiment_id:
        return jsonify({"success": False, "message": "experiment_id is required."}), 400
    if not usl_results or not capacity_results or not runtime_metrics or not prediction_targets:
        return jsonify({
            "success": False,
            "message": "usl_results, capacity_results, runtime_metrics, and prediction_targets are all required."
        }), 400

    try:
        exp = Experiment(EXPERIMENTS_DIR, experiment_id)
        record = freeze_predictions(
            usl_results=usl_results,
            capacity_results=capacity_results,
            runtime_metrics=runtime_metrics,
            prediction_targets=prediction_targets,
            output_path=exp.paths["predictions"],
            bottleneck_results=data.get("bottleneck_results"),
            amdahl_results=data.get("amdahl_results"),
            experiment_id=experiment_id,
        )
        return jsonify({"success": True, "record": sanitize_for_json(record)}), 200

    except PredictionValidationError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400

    except Exception as exc:
        return jsonify({"success": False, "message": f"Phase A failed: {str(exc)}"}), 500


@app.route("/api/validation/record-actuals", methods=["POST"])
def record_phase_b_actuals():
    """
    PHASE B. Call AFTER a genuinely separate /api/load-testing/run
    invocation at the predicted target levels (pass
    user_levels=predictions.json's prediction_targets to that call).
    Pass that run's response["levels"] straight through as level_results.
    """
    data = request.get_json(silent=True) or {}

    experiment_id = data.get("experiment_id")
    level_results = data.get("level_results")

    if not experiment_id:
        return jsonify({"success": False, "message": "experiment_id is required."}), 400
    if not level_results:
        return jsonify({"success": False, "message": "level_results is required (the levels[] from a load-testing/run response)."}), 400

    try:
        exp = Experiment(EXPERIMENTS_DIR, experiment_id)
        if not os.path.isfile(exp.paths["predictions"]):
            return jsonify({"success": False, "message": "Run Phase A (freeze-predictions) for this experiment first."}), 404

        record = record_validation_actuals(
            predictions_path=exp.paths["predictions"],
            level_results=level_results,
            output_path=exp.paths["validation_actual"],
            # locust_runner.py's real aggregated field names:
            throughput_field="mean_throughput",
            response_time_field="mean_average_response_time",
            error_rate_field="mean_error_rate",
        )
        return jsonify({"success": True, "record": sanitize_for_json(record)}), 200

    except PredictionValidationError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400

    except Exception as exc:
        return jsonify({"success": False, "message": f"Phase B failed: {str(exc)}"}), 500


@app.route("/api/validation/validate", methods=["POST"])
def validate_phase_c():
    """PHASE C. Reloads Phase A + B, verifies integrity, computes real MAE/RMSE/MAPE."""
    data = request.get_json(silent=True) or {}

    experiment_id = data.get("experiment_id")
    if not experiment_id:
        return jsonify({"success": False, "message": "experiment_id is required."}), 400

    try:
        exp = Experiment(EXPERIMENTS_DIR, experiment_id)
        for label, key in (("Phase A (predictions.json)", "predictions"), ("Phase B (validation_actual.json)", "validation_actual")):
            if not os.path.isfile(exp.paths[key]):
                return jsonify({"success": False, "message": f"{label} not found - run it first for this experiment."}), 404

        record = validate_predictions(
            predictions_path=exp.paths["predictions"],
            validation_actual_path=exp.paths["validation_actual"],
            output_path=exp.paths["validated_result"],
        )
        return jsonify({"success": True, "record": sanitize_for_json(record)}), 200

    except ModelIntegrityError as exc:
        return jsonify({"success": False, "message": str(exc), "integrity_failure": True}), 409

    except PredictionValidationError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400

    except Exception as exc:
        return jsonify({"success": False, "message": f"Phase C failed: {str(exc)}"}), 500


@app.route("/api/validation/model-validation", methods=["POST"])
def run_model_validation():
    """
    Little's Law / queueing predicted-vs-observed error tables across a
    series of tested levels (fitting range and/or Phase B validation
    range combined).

    Body: {"levels": [{"users", "arrival_rate", "response_time",
                        "observed_concurrency"?, "service_rate"|"service_time",
                        "service_time_p50"?, "service_time_p95"?, ...}, ...]}
    """
    try:
        data = request.get_json(silent=True) or {}
        levels = data.get("levels")
        if not levels:
            return jsonify({"success": False, "error": "levels is required."}), 400

        result = validate_levels(
            levels,
            concurrency_tolerance=float(data.get("concurrency_tolerance", 0.30)),
            response_time_tolerance=float(data.get("response_time_tolerance", 0.30)),
        )
        return jsonify({"success": True, "result": sanitize_for_json(result)}), 200

    except ModelValidationError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/validation/ablation", methods=["POST"])
def run_ablation_study():
    """
    RQ5: Runtime-only vs +Queueing vs Full-analytical vs +Forced-Flow,
    scored against REAL Phase B validation_levels (not predictions).

    Body: {"runtime_metrics", "capacity_results", "validation_levels" (required,
           real measured data), "queueing_results"?, "bottleneck_results"?,
           "bottleneck_results_with_components"?, "component_metrics"?,
           "slo_max_response_time_seconds"?, "slo_max_error_rate_percent"?}
    """
    try:
        data = request.get_json(silent=True) or {}

        slo_thresholds = None
        if "slo_max_response_time_seconds" in data or "slo_max_error_rate_percent" in data:
            slo_thresholds = SLOThresholds(
                max_response_time_seconds=float(data.get("slo_max_response_time_seconds", 0.5)),
                max_error_rate_percent=float(data.get("slo_max_error_rate_percent", 1.0)),
            )

        result = run_ablation(
            runtime_metrics=data.get("runtime_metrics"),
            capacity_results=data.get("capacity_results"),
            validation_levels=data.get("validation_levels"),
            queueing_results=data.get("queueing_results"),
            bottleneck_results=data.get("bottleneck_results"),
            bottleneck_results_with_components=data.get("bottleneck_results_with_components"),
            component_metrics=data.get("component_metrics"),
            slo_thresholds=slo_thresholds,
        )
        return jsonify({"success": True, "result": sanitize_for_json(result)}), 200

    except AblationError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/experiment/<experiment_id>/report", methods=["GET"])
def get_experiment_report(experiment_id):
    """Renders whatever sections this experiment currently has artifacts
    for as Markdown - available even if only Phase A has run so far."""
    try:
        exp = Experiment(EXPERIMENTS_DIR, experiment_id)
        report_text = render_experiment_report(exp)
        return jsonify({"success": True, "report_markdown": report_text}), 200

    except ReportError as exc:
        return jsonify({"success": False, "message": str(exc)}), 404

    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


# ---------------------------------------------------------------------------
# Standalone prediction endpoints (for testing modules independently)
# ---------------------------------------------------------------------------

@app.route("/api/prediction/usl", methods=["POST"])
def predict_usl():
    try:
        data = request.get_json()
        result = run_usl_analysis(
            user_levels=data.get("user_levels"),
            throughput=data.get("throughput"),
            predict_at=data.get("predict_at"),
            response_time=data.get("response_time"),
            error_rate=data.get("error_rate"),
            slo_max_response_time_seconds=data.get("slo_max_response_time_seconds"),
        )
        return jsonify({"success": True, "result": result})

    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route("/api/prediction/littles-law", methods=["POST"])
def predict_littles_law():
    try:
        data = request.get_json(silent=True) or {}
        levels = data.get("levels")

        if not levels:
            return jsonify({"success": False, "error": "Load-level data is required."}), 400

        result = analyze_workload_levels(levels)
        return jsonify({"success": True, "result": result}), 200

    except Exception as exc:
        print("Little's Law Error:", str(exc))
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route("/api/prediction/queueing", methods=["POST"])
def predict_queueing():
    """
    Standalone M/M/1 (or M/M/c) queueing analysis for a single snapshot.

    Body:
        {
            "arrival_rate": <req/s>,
            "service_rate": <req/s>,          # or "service_time" in seconds
            "num_servers": <int, optional>,   # defaults to 1
            "metadata": {...}                 # optional
        }
    """
    try:
        data = request.get_json(silent=True) or {}

        if not data:
            return jsonify({"success": False, "error": "Queueing input data is required."}), 400

        result = analyze_queue_safe(data)
        return jsonify({"success": True, "result": sanitize_for_json(result)}), 200

    except Exception as exc:
        print("Queueing Error:", str(exc))
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route("/api/prediction/bottleneck", methods=["POST"])
def predict_bottleneck():
    """
    Standalone Utilization Law / Bottleneck Analysis for a single snapshot.

    Body:
        {
            "throughput": <req/s>,
            "current_users": <int>,
            "cpu_usage": <0-100, optional>,
            "disk_io": <MB/s, optional>,
            "network_io": <MB/s, optional>,
            "think_time": <seconds, optional>,
            "component_demands": {                  # optional
                name: {"service_demand": <s/req>, "source": str}, ...
            }
        }

    At least one of cpu_usage/disk_io/network_io/component_demands should be
    present, or the result will come back as "unavailable" rather than an
    error. "component_demands" is the same shape
    forced_flow.to_bottleneck_resources() produces - pass that function's
    output straight through (e.g. from /api/prediction/forced-flow) to let a
    database/cache/external API compete as the identified bottleneck
    alongside cpu/disk/network. Uses the same disk/network capacity limits
    as the full pipeline (DISK_IO_LIMIT_MBPS / NETWORK_IO_LIMIT_MBPS) so
    results stay consistent.
    """
    try:
        data = request.get_json(silent=True) or {}

        if not data:
            return jsonify({"success": False, "error": "Bottleneck input data is required."}), 400

        result = analyze_bottleneck_safe(data, capacity_config=BOTTLENECK_CAPACITY_CONFIG)
        return jsonify({"success": True, "result": sanitize_for_json(result)}), 200

    except Exception as exc:
        print("Bottleneck Error:", str(exc))
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route("/api/prediction/forced-flow", methods=["POST"])
def predict_forced_flow():
    """
    Standalone Forced Flow Law analysis for components with no OS-level
    %utilization reading (database, cache, external API).

    Body:
        {
            "system_throughput": <req/s>,
            "components": {
                name: {
                    "visits_per_request": <float>,
                    "service_time_seconds": <float>,
                    "observed_throughput": <req/s, optional>
                },
                ...
            }
        }

    Feed the "per_component" section of the result (or run it through
    to_bottleneck_resources()-equivalent shape via /api/prediction/bottleneck's
    "component_demands" field) to let a component compete as the identified
    bottleneck.
    """
    try:
        data = request.get_json(silent=True) or {}

        if not data:
            return jsonify({"success": False, "error": "Forced Flow input data is required."}), 400

        result = analyze_forced_flow(data)
        return jsonify({"success": True, "result": sanitize_for_json(result)}), 200

    except Exception as exc:
        print("Forced Flow Error:", str(exc))
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route("/api/prediction/amdahl", methods=["POST"])
def predict_amdahl():
    """
    Standalone Amdahl's Law optimization-ceiling analysis.

    Body:
        {
            "bottleneck_results": {...}   # required, direct output of
                                           # bottleneck.analyze_bottleneck() /
                                           # analyze_bottleneck_safe()
        }
    """
    try:
        data = request.get_json(silent=True) or {}

        bottleneck_results = data.get("bottleneck_results")
        if not bottleneck_results:
            return jsonify({"success": False, "error": "bottleneck_results is required."}), 400

        result = analyze_amdahl(bottleneck_results)
        return jsonify({"success": True, "result": sanitize_for_json(result)}), 200

    except Exception as exc:
        print("Amdahl Error:", str(exc))
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route("/api/prediction/capacity", methods=["POST"])
def predict_capacity():
    """
    Standalone capacity planning analysis.

    Body must contain the "usl", "little_law", "queueing", and "runtime"
    sections analyze_capacity() expects (see mathematical_engine/capacity.py),
    with an optional "bottleneck" section holding the direct output of
    analyze_bottleneck()/analyze_bottleneck_safe(). Optional top-level
    overrides: "safety_margin_ratio", "disk_io_limit_mbps",
    "network_io_limit_mbps", "p95_response_time_threshold_ms".
    """
    try:
        data = request.get_json(silent=True) or {}

        if not data:
            return jsonify({"success": False, "error": "Capacity input data is required."}), 400

        capacity_kwargs = {}
        if "safety_margin_ratio" in data:
            capacity_kwargs["safety_margin_ratio"] = data["safety_margin_ratio"]
        if "p95_response_time_threshold_ms" in data:
            capacity_kwargs["p95_response_time_threshold_ms"] = data["p95_response_time_threshold_ms"]

        capacity_kwargs["disk_io_limit_mbps"] = data.get("disk_io_limit_mbps", DISK_IO_LIMIT_MBPS)
        capacity_kwargs["network_io_limit_mbps"] = data.get("network_io_limit_mbps", NETWORK_IO_LIMIT_MBPS)

        capacity_data = {
            key: value
            for key, value in data.items()
            if key in ("usl", "little_law", "queueing", "runtime", "bottleneck")
        }

        result = analyze_capacity(capacity_data, **capacity_kwargs)
        return jsonify({"success": True, "result": sanitize_for_json(result)}), 200

    except Exception as exc:
        print("Capacity Error:", str(exc))
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route("/api/prediction/scalability", methods=["POST"])
def predict_scalability_endpoint():
    """
    Standalone scalability prediction for future user levels.

    Body:
        {
            "usl_results": {...},            # from run_usl_analysis()
            "capacity_results": {...},        # from analyze_capacity()
            "runtime_metrics": {...},
            "prediction_targets": [600, 1000, 5000],
            "bottleneck_results": {...},      # optional
            "amdahl_results": {...},          # optional, from analyze_amdahl()
            "actual_throughput_by_users": {"600": 78, "800": 71}   # optional
        }

    "actual_throughput_by_users" is the "validated" leg of observed/
    predicted/validated: supply real measurements for any prediction_targets
    that have already actually been tested, and each gets checked against
    what USL predicted (MAE/RMSE/MAPE aggregated into
    result["validation_summary"]) instead of being left unverified.
    """
    try:
        data = request.get_json(silent=True) or {}

        usl_results = data.get("usl_results")
        capacity_results = data.get("capacity_results")
        runtime_metrics = data.get("runtime_metrics")
        prediction_targets = data.get("prediction_targets")
        bottleneck_results = data.get("bottleneck_results")
        amdahl_results = data.get("amdahl_results")
        actual_throughput_by_users = data.get("actual_throughput_by_users")

        if not usl_results:
            return jsonify({"success": False, "error": "usl_results is required."}), 400
        if not capacity_results:
            return jsonify({"success": False, "error": "capacity_results is required."}), 400
        if not runtime_metrics:
            return jsonify({"success": False, "error": "runtime_metrics is required."}), 400
        if not prediction_targets:
            return jsonify({"success": False, "error": "prediction_targets is required."}), 400

        # JSON object keys are always strings; predict_scalability() expects
        # numeric user counts as keys.
        normalized_actual_throughput_by_users = None
        if actual_throughput_by_users:
            normalized_actual_throughput_by_users = {
                float(users): float(throughput_value)
                for users, throughput_value in actual_throughput_by_users.items()
            }

        result = predict_scalability(
            usl_results=usl_results,
            capacity_results=capacity_results,
            runtime_metrics=runtime_metrics,
            prediction_targets=prediction_targets,
            bottleneck_results=bottleneck_results,
            amdahl_results=amdahl_results,
            actual_throughput_by_users=normalized_actual_throughput_by_users,
        )
        return jsonify({"success": True, "result": sanitize_for_json(result)}), 200

    except Exception as exc:
        print("Scalability Prediction Error:", str(exc))
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route("/api/prediction/slo", methods=["POST"])
def predict_slo():
    """
    Standalone SLO (response-time + error-rate budget) capacity analysis.

    Body:
        {
            "levels": [
                {"users": 100, "response_time": 0.2, "error_rate_percent": 0.1,
                 "source": "observed"},   # "source" optional, defaults "observed"
                ...
            ],
            "max_response_time_seconds": 0.5,   # optional, defaults DEFAULT_SLO_MAX_RESPONSE_TIME_SECONDS
            "max_error_rate_percent": 1.0        # optional, defaults DEFAULT_SLO_MAX_ERROR_RATE_PERCENT
        }

    "response_time" is in SECONDS - convert from Locust's milliseconds
    before calling this. Use slo.from_scalability_predictions() (or replicate
    its shape) to fold scalability.predict_scalability()'s predicted levels
    in alongside real observed ones.
    """
    try:
        data = request.get_json(silent=True) or {}

        levels = data.get("levels")
        if not levels:
            return jsonify({"success": False, "error": "levels is required."}), 400

        thresholds = SLOThresholds(
            max_response_time_seconds=data.get(
                "max_response_time_seconds", DEFAULT_SLO_MAX_RESPONSE_TIME_SECONDS
            ),
            max_error_rate_percent=data.get(
                "max_error_rate_percent", DEFAULT_SLO_MAX_ERROR_RATE_PERCENT
            ),
        )

        result = analyze_slo(levels, thresholds=thresholds)
        return jsonify({"success": True, "result": sanitize_for_json(result)}), 200

    except Exception as exc:
        print("SLO Error:", str(exc))
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route("/api/prediction/recommendation", methods=["POST"])
def predict_recommendation():
    """
    Standalone recommendation generation.

    Body:
        {
            "capacity_results": {...},        # required, from analyze_capacity()
            "scalability_results": {...},     # optional
            "usl_results": {...},             # optional
            "little_law_results": {...},      # optional
            "queueing_results": {...},        # optional
            "amdahl_results": {...}           # optional, from analyze_amdahl()
        }
    """
    try:
        data = request.get_json(silent=True) or {}

        capacity_results = data.get("capacity_results")
        if not capacity_results:
            return jsonify({"success": False, "error": "capacity_results is required."}), 400

        result = generate_recommendations(
            capacity_results=capacity_results,
            scalability_results=data.get("scalability_results"),
            usl_results=data.get("usl_results"),
            little_law_results=data.get("little_law_results"),
            queueing_results=data.get("queueing_results"),
            amdahl_results=data.get("amdahl_results"),
        )
        return jsonify({"success": True, "result": sanitize_for_json(result)}), 200

    except Exception as exc:
        print("Recommendation Error:", str(exc))
        return jsonify({"success": False, "error": str(exc)}), 400


if __name__ == "__main__":
    # Defaults match the original behavior exactly (Flask's own defaults:
    # host 127.0.0.1, port 5000, debug on) - only overridable via env vars
    # so nothing changes unless you explicitly set them, e.g. for
    # deploying behind Docker where APP_HOST=0.0.0.0 is usually needed.
    host = os.environ.get("APP_HOST", "127.0.0.1")
    port = int(os.environ.get("APP_PORT", "5000"))
    debug = os.environ.get("APP_DEBUG", "true").lower() == "true"

    app.run(host=host, port=port, debug=debug, use_reloader=False)