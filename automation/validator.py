import os


class FlaskValidator:

    def __init__(self, project_path):
        self.project_path = project_path

    def validate(self):

        errors = []

        # Check requirements.txt
        requirements = os.path.join(
            self.project_path,
            "requirements.txt"
        )

        if not os.path.exists(requirements):
            errors.append("requirements.txt not found")

        # Check Dockerfile
        dockerfile = os.path.join(
            self.project_path,
            "Dockerfile"
        )

        if not os.path.exists(dockerfile):
            errors.append("Dockerfile not found")

        # Check Flask entry point
        app = os.path.join(
            self.project_path,
            "app.py"
        )

        wsgi = os.path.join(
            self.project_path,
            "wsgi.py"
        )

        if not os.path.exists(app) and not os.path.exists(wsgi):
            errors.append("Neither app.py nor wsgi.py found")

        if len(errors) == 0:
            return True, []

        return False, errors