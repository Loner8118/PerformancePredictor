import os


class LocustGenerator:

    def generate(self, project_path, routes):

        locust_path = os.path.join(project_path, "locustfile.py")

        with open(locust_path, "w", encoding="utf-8") as f:

            f.write("from locust import HttpUser, task\n\n")
            f.write("class WebsiteUser(HttpUser):\n\n")

            if not routes:
                f.write("    @task\n")
                f.write("    def default(self):\n")
                f.write("        self.client.get('/')\n")
                return locust_path

            for route in routes:

                function = route["function"]
                path = route["path"]

                for method in route["methods"]:

                    task_name = f"{function}_{method.lower()}"

                    f.write("    @task\n")

                    f.write(f"    def {task_name}(self):\n")

                    if method == "GET":

                        f.write(
                            f"        self.client.get('{path}')\n\n"
                        )

                    elif method == "POST":

                        f.write(
                            f"        self.client.post('{path}')\n\n"
                        )

        print("locustfile.py generated successfully.")

        return locust_path