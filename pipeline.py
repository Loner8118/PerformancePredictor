"""
Main Performance Prediction Pipeline

Automates:

GitHub Repository
        |
        v
Docker Deployment
        |
        v
API Discovery
        |
        v
Load Testing
        |
        v
Runtime Metrics
        |
        v
Mathematical Prediction
        |
        v
Recommendation
"""


import time
import logging


from automation.github_manager import GitHubManager
from automation.docker_manager import DockerManager
from automation.route_discovery import RouteDiscovery
from automation.locust_generator import LocustGenerator
from automation.locust_runner import LocustRunner


from prediction.predictor import analyze_system



logger = logging.getLogger(__name__)



def run_pipeline(repository_url):


    start = time.perf_counter()


    github = GitHubManager()

    docker = DockerManager()

    routes = RouteDiscovery()

    locust = LocustGenerator()

    runner = LocustRunner()



    try:


        # --------------------------------------
        # 1. Clone Repository
        # --------------------------------------

        print("\n[1] Cloning repository")


        project_path = github.clone_repository(
            repository_url
        )


        if not project_path:
            raise Exception(
                "Repository cloning failed"
            )



        # --------------------------------------
        # 2. Docker Build
        # --------------------------------------

        print("\n[2] Building Docker image")


        image_status = docker.build_image(
            project_path
        )


        if not image_status:
            raise Exception(
                "Docker build failed"
            )



        # --------------------------------------
        # 3. Start Application
        # --------------------------------------

        print("\n[3] Starting container")


        container_id = docker.run_container()


        if not container_id:
            raise Exception(
                "Container failed"
            )



        # --------------------------------------
        # 4. Discover APIs
        # --------------------------------------

        print("\n[4] Discovering routes")


        discovered_routes = routes.discover(
            project_path
        )


        print(
            "Routes found:",
            discovered_routes
        )



        # --------------------------------------
        # 5. Generate Locust
        # --------------------------------------

        print("\n[5] Generating Locust script")


        locust.generate(
            project_path,
            discovered_routes
        )



        # --------------------------------------
        # 6. Run Load Test
        # --------------------------------------

        print("\n[6] Running Locust")


        load_results = runner.run(
            project_path
        )


        if not load_results:
            raise Exception(
                "Load testing failed"
            )



        # --------------------------------------
        # 7. Runtime Metrics
        # --------------------------------------

        print("\n[7] Collecting metrics")


        # TEMPORARY
        # We will connect metrics_collector here next


        runtime_metrics = {

            "current_users": 200,

            "throughput": 
                load_results["throughput"][-1],

            "response_time":250,

            "cpu_usage":65,

            "memory_usage":55,

            "disk_io":20,

            "network_io":30,

            "error_rate":0.2

        }



        # --------------------------------------
        # 8. Mathematical Engine
        # --------------------------------------

        print("\n[8] Running Prediction Engine")


        prediction_result = analyze_system(

            metrics={

                "runtime":
                    runtime_metrics,


                "load":
                    load_results

            }

        )



        total_time = (
            time.perf_counter()
            -
            start
        )


        return {


            "status":"success",


            "repository":
                repository_url,


            "routes":
                discovered_routes,


            "prediction":
                prediction_result,


            "execution_time":
                round(total_time,2)

        }



    except Exception as e:


        logger.exception(
            "Pipeline failed"
        )


        return {


            "status":"failed",

            "error":
                str(e)

        }
