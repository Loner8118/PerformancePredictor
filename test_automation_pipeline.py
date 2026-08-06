from automation.github_manager import GitHubManager
from automation.docker_manager import DockerManager
from automation.health_checker import HealthChecker
from automation.route_discovery import RouteDiscovery
from automation.locust_generator import LocustGenerator
from automation.locust_runner import LocustRunner
from automation.metrics_collector import MetricsCollector

from prediction.predictor import analyze_system
from prediction.recommendation import generate_recommendations


GITHUB_URL = input("Enter GitHub repository URL: ").strip()

if not GITHUB_URL:
    print("Repository URL cannot be empty.")
    exit()


def main():

    print("=" * 60)
    print("PERFORMANCE PREDICTION AUTOMATION PIPELINE")
    print("=" * 60)

    # --------------------------------------------------
    # Step 1
    # --------------------------------------------------

    github = GitHubManager()

    project_path = github.clone_repository(GITHUB_URL)

    print("\nRepository:", project_path)

    # --------------------------------------------------
    # Step 2
    # --------------------------------------------------

    docker = DockerManager()

    if not docker.build_image(project_path):
        return

    container = docker.run_container()

    if not container:
        return

    # --------------------------------------------------
    # Step 3
    # --------------------------------------------------

    health = HealthChecker()

    if not health.wait_until_ready():
        docker.stop_container(container)
        return

    # --------------------------------------------------
    # Step 4
    # --------------------------------------------------

    routes = RouteDiscovery().discover(project_path)

    print("\nRoutes Found")

    for r in routes:
        print(r)

    # --------------------------------------------------
    # Step 5
    # --------------------------------------------------

    LocustGenerator().generate(project_path, routes)

    # --------------------------------------------------
    # Step 6
    # --------------------------------------------------

    load = LocustRunner().run(project_path)

    # --------------------------------------------------
    # Step 7
    # --------------------------------------------------

    runtime = MetricsCollector().collect(
        load["user_levels"][-1],
        load["throughput"][-1]
    )

    # --------------------------------------------------
    # Step 8
    # Mathematical Prediction
    # --------------------------------------------------

    print("\nRunning Mathematical Prediction...")

    prediction_input = {

        "load_test": {

            "user_levels": load["user_levels"],

            "throughput": load["throughput"]

        },

        "runtime": runtime,

        "queue": {

            "arrival_rate": runtime["throughput"],

            "service_rate": runtime["throughput"] * 1.5

        }

    }


    prediction = analyze_system(prediction_input)

    if prediction["status"] == "success":

        recommendation = prediction["recommendation"]

    else:

        print("Prediction not available.")
        print("Using sample recommendation for demonstration.")

        recommendation = generate_recommendations({

            "inputs": {

                "runtime": runtime,

                "queueing": {

                    "utilization": 0.67

                }

            },

            "results": {

                "safe_users": 350,

                "growth_potential_users": 150,

                "capacity_used_percent": 72,

                "scalability_efficiency": 0.82

            },

            "resource_pressure": {

                "cpu": "Moderate",

                "memory": "Moderate",

                "queue": "Moderate"

            }

        })

    # --------------------------------------------------
    # Final Report
    # --------------------------------------------------

    print("\n" + "=" * 60)
    print("FINAL REPORT")
    print("=" * 60)

    print("System Health :", recommendation["system_health"])

    print("Bottleneck    :", recommendation["bottleneck"])

    print("Scaling       :", recommendation["scaling_recommendation"])

    print("Capacity      :", recommendation["capacity_summary"])

    print()

    print(recommendation["summary"])

    print("\n" + "=" * 60)
    print("SCALABILITY REPORT")
    print("=" * 60)

    from pprint import pprint

    pprint(prediction["scalability"]["summary"])

    print("\nPredictions")

    for prediction in prediction["scalability"]["predictions"]:
        print(
            f"{prediction['users']:>6} users | "
            f"Throughput={prediction['predicted_throughput']:.2f} | "
            f"CPU={prediction['predicted_cpu']:.2f}% | "
            f"Memory={prediction['predicted_memory']:.2f}% | "
            f"Response={prediction['predicted_response_time']:.2f} ms | "
            f"Risk={prediction['saturation_risk']} | "
            f"Status={prediction['classification']}"
        )

    docker.stop_container(container)


if __name__ == "__main__":
    main()