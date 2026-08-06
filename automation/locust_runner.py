import os
import subprocess

from metrics.csv_parser import CSVParser


class LocustRunner:

    def run(
        self,
        project_path,
        user_levels=[20,40,60,80,100,125,150,175,200,225,250,275,300,325,350,375,400,450,500],
        spawn_rate=25,
        run_time="1m"
    ):

        locust_file = os.path.join(
            project_path,
            "locustfile.py"
        )

        parser = CSVParser()

        throughput = []

        for users in user_levels:

            print(f"\nRunning Locust with {users} users...")

            result = subprocess.run(
                [
                    "locust",
                    "-f",
                    locust_file,
                    "--host=http://localhost:5000",
                    "--headless",
                    "-u",
                    str(users),
                    "-r",
                    str(spawn_rate),
                    "-t",
                    run_time,
                    "--csv",
                    os.path.join(project_path, "results")
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore"
            )

            if result.returncode != 0:

                print(result.stderr)
                return None

            metrics = parser.parse(project_path)

            throughput.append(metrics["throughput"])

            print(
                f"Users: {users} | Throughput: {metrics['throughput']:.2f} req/sec"
            )

        print("\nAll load tests completed.")

        return {
            "user_levels": user_levels,
            "throughput": throughput
        }