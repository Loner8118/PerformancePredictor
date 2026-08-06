import os
from git import Repo


class GitHubManager:

    def __init__(self):

        self.workspace = "workspace"

        os.makedirs(self.workspace, exist_ok=True)

    def clone_repository(self, github_url):

        if not github_url.startswith("https://github.com/"):
            raise ValueError("Invalid GitHub URL")

        project_name = github_url.rstrip("/").split("/")[-1]

        destination = os.path.join(
            self.workspace,
            project_name
        )

        # If already cloned, reuse it
        if os.path.exists(destination):

            print("Repository already exists.")

            return destination

        try:

            print("Cloning repository...")

            Repo.clone_from(
                github_url,
                destination
            )

            print("Repository cloned successfully.")

            return destination

        except Exception as e:

            print(e)

            return None