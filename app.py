import os
import math
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

from metrics.runtime_monitor import RuntimeMonitor

from mathematical_engine.usl import run_usl_analysis
from mathematical_engine.little_law import analyze_workload_levels
from mathematical_engine.queueing import analyze_queue_safe
from mathematical_engine.bottleneck import (
    analyze_bottleneck_safe,
    ResourceCapacityConfig,
)
from mathematical_engine.capacity import analyze_capacity
from mathematical_engine.scalability import predict_scalability
from mathematical_engine.recommendation import generate_recommendations


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
# Load testing + full mathematical pipeline
# ---------------------------------------------------------------------------

@app.route("/api/load-testing/run", methods=["POST"])
def run_load_testing():
    try:
        data = request.get_json(silent=True) or {}
        project_name = data.get("project_name")
        container_id = data.get("container_id")
        container_host_port = data.get("port")

        if not project_name:
            return jsonify({"success": False, "message": "Project name is required."}), 400
        if not container_id:
            return jsonify({"success": False, "message": "Container ID is required."}), 400
        if not container_host_port:
            return jsonify({
                "success": False,
                "message": "Container host port is required (the \"port\" returned by /api/docker/run)."
            }), 400

        project_path = os.path.join(github_manager.workspace, project_name)
        if not os.path.exists(project_path):
            return jsonify({"success": False, "message": f"Project not found: {project_name}"}), 404

        # --------------------------------------------------------------
        # Run Locust across the configured user levels
        # --------------------------------------------------------------
        runner = LocustRunner(
            host=f"http://localhost:{container_host_port}",
            repetitions=1,
            run_time="15s",
            spawn_rate=10,
        )
        result = runner.run(project_path=project_path, container_id=container_id)
        levels = result.get("levels", [])

        # --------------------------------------------------------------
        # USL — fit sigma/kappa to the tested load levels
        # --------------------------------------------------------------
        usl_result = None
        try:
            user_levels = [level["users"] for level in levels]
            throughput = [level["mean_throughput"] for level in levels]

            print("\n=== USL INPUT DATA ===")
            print("User Levels :", user_levels)
            print("Throughput  :", throughput)

            usl_result = run_usl_analysis(
                {"user_levels": user_levels, "throughput": throughput},
                predict_at=[1000, 2000, 5000],
            )

            print("\n=== USL RESULT ===")
            print(usl_result)

            result["prediction"] = {"usl": usl_result}

        except Exception as usl_error:
            print("\nUSL Prediction Error:", str(usl_error))
            result["prediction"] = {"usl": {"success": False, "error": str(usl_error)}}

        # --------------------------------------------------------------
        # Little's Law — one snapshot per tested load level
        # --------------------------------------------------------------
        little_law_inputs = [
            {
                "arrival_rate": level["mean_throughput"],
                "average_response_time": level["mean_average_response_time"],
                "response_time_unit": "ms",
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
                "metadata": {
                    "users": level.get("users", 0),
                    "throughput_source": "locust",
                    "service_capacity_source": service_rate_source,
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
            "model": "M/M/1",
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
        # BOTTLENECK ANALYSIS (Utilization Law / Bottleneck Analysis /
        # Asymptotic Bounds) - feeds into both Capacity Planning and
        # Scalability Prediction below.
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

            scalability_result = predict_scalability(
                usl_results=usl_prediction_input,
                capacity_results=capacity_prediction_input,
                runtime_metrics=runtime_prediction_input,
                prediction_targets=prediction_targets,
                bottleneck_results=bottleneck_result,
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
# Standalone prediction endpoints (for testing modules independently)
# ---------------------------------------------------------------------------

@app.route("/api/prediction/usl", methods=["POST"])
def predict_usl():
    try:
        data = request.get_json()
        result = run_usl_analysis(
            {
                "user_levels": data.get("user_levels"),
                "throughput": data.get("throughput"),
            },
            predict_at=data.get("predict_at"),
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
            "think_time": <seconds, optional>
        }

    At least one of cpu_usage/disk_io/network_io should be present, or the
    result will come back as "unavailable" rather than an error. Uses the
    same disk/network capacity limits as the full pipeline
    (DISK_IO_LIMIT_MBPS / NETWORK_IO_LIMIT_MBPS) so results stay consistent.
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
            "bottleneck_results": {...}       # optional
        }
    """
    try:
        data = request.get_json(silent=True) or {}

        usl_results = data.get("usl_results")
        capacity_results = data.get("capacity_results")
        runtime_metrics = data.get("runtime_metrics")
        prediction_targets = data.get("prediction_targets")
        bottleneck_results = data.get("bottleneck_results")

        if not usl_results:
            return jsonify({"success": False, "error": "usl_results is required."}), 400
        if not capacity_results:
            return jsonify({"success": False, "error": "capacity_results is required."}), 400
        if not runtime_metrics:
            return jsonify({"success": False, "error": "runtime_metrics is required."}), 400
        if not prediction_targets:
            return jsonify({"success": False, "error": "prediction_targets is required."}), 400

        result = predict_scalability(
            usl_results=usl_results,
            capacity_results=capacity_results,
            runtime_metrics=runtime_metrics,
            prediction_targets=prediction_targets,
            bottleneck_results=bottleneck_results,
        )
        return jsonify({"success": True, "result": sanitize_for_json(result)}), 200

    except Exception as exc:
        print("Scalability Prediction Error:", str(exc))
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
            "queueing_results": {...}         # optional
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