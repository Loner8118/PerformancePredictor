import subprocess


class DockerManager:

    def build_image(self, project_path, image_name="performance-image"):
        """
        Build a Docker image from a project folder.
        """

        print("Building Docker image...")

        result = subprocess.run(
            [
                "docker",
                "build",
                "-t",
                image_name,
                "."
            ],
            cwd=project_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore"
        )

        if result.returncode == 0:
            print("Docker image built successfully.")
            return True

        print("Docker build failed.")
        print(result.stderr)

        return False

    def run_container(
        self,
        image_name="performance-image",
        container_name="performance-container"
    ):
        """
        Run a Docker container from the image.
        """

        print("Starting Docker container...")

        # Remove any existing container
        subprocess.run(
            [
                "docker",
                "rm",
                "-f",
                container_name
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore"
        )

        result = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container_name,
                "-p",
                "5000:5000",
                image_name
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore"
        )

        if result.returncode == 0:

            print("Docker container started successfully.")

            container_id = result.stdout.strip()

            print("Container ID:", container_id)

            return container_id

        print("Failed to start container.")
        print(result.stderr)

        return False

    def stop_container(self, container_id):
        """
        Stop and remove the Docker container.
        """

        print("Stopping Docker container...")

        result = subprocess.run(
            [
                "docker",
                "rm",
                "-f",
                container_id
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore"
        )

        if result.returncode == 0:

            print("Docker container stopped and removed successfully.")

            return True

        print("Failed to stop container.")
        print(result.stderr)

        return False