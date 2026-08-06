from automation.github_manager import GitHubManager
from automation.validator import FlaskValidator
from automation.docker_manager import DockerManager
from automation.health_checker import HealthChecker
from automation.route_discovery import RouteDiscovery
from automation.locust_generator import LocustGenerator
from automation.locust_runner import LocustRunner

from metrics.csv_parser import CSVParser
from metrics.runtime_monitor import RuntimeMonitor

# Next Step
# from prediction.predictor import analyze_system


# -------------------------------------------------
# Clone Repository
# -------------------------------------------------

manager = GitHubManager()

path = manager.clone_repository(
    "https://github.com/Loner8118/Performance-Testing-Demo"
)

validator = FlaskValidator(path)

valid, errors = validator.validate()

if valid:

    docker = DockerManager()

    if docker.build_image(path):

        container_id = docker.run_container()

        if container_id:

            checker = HealthChecker()

            if checker.wait_until_ready():

                # ---------------------------------
                # Discover Flask Routes
                # ---------------------------------

                discoverer = RouteDiscovery()

                routes = discoverer.discover(path)

                print("\nDiscovered Routes")
                print(routes)

                # ---------------------------------
                # Generate Locust File
                # ---------------------------------

                generator = LocustGenerator()

                locust_file = generator.generate(
                    path,
                    routes
                )

                print("\nGenerated Locust File")
                print(locust_file)

                # ---------------------------------
                # Start Runtime Monitoring
                # ---------------------------------

                monitor = RuntimeMonitor()

                monitor.start(container_id)

                # ---------------------------------
                # Run Load Test
                # ---------------------------------

                runner = LocustRunner()

                load_test_metrics = runner.run(path)

                # ---------------------------------
                # Stop Runtime Monitoring
                # ---------------------------------

                monitor.stop()

                if load_test_metrics:

                    # -----------------------------
                    # Performance Metrics
                    # -----------------------------

                    parser = CSVParser()

                    performance_metrics = parser.parse(path)

                    # -----------------------------
                    # Runtime Metrics
                    # -----------------------------

                    runtime_metrics = monitor.summary(
                        performance_metrics=performance_metrics,
                        current_users=max(
                            load_test_metrics["user_levels"]
                        )
                    )

                    # -----------------------------
                    # Display Results
                    # -----------------------------

                    print("\nLoad Test Metrics")
                    print(load_test_metrics)

                    print("\nPerformance Metrics")
                    print(performance_metrics)

                    print("\nRuntime Metrics")
                    print(runtime_metrics)

                    # -------------------------------------------------
                    # Mathematical Prediction (Next Step)
                    # -------------------------------------------------

                    # prediction_input = {
                    #     "load_test": load_test_metrics,
                    #     "runtime": runtime_metrics,
                    #     "queue": {
                    #         "arrival_rate": performance_metrics["throughput"],
                    #         "service_rate": performance_metrics["throughput"]
                    #     }
                    # }
                    #
                    # prediction = analyze_system(prediction_input)
                    #
                    # print("\nPrediction Results")
                    # print(prediction)

                    print("\nAutomation pipeline completed successfully.")
                    print("Ready for Mathematical Prediction Engine.")

                docker.stop_container(container_id)

else:

    print(errors)