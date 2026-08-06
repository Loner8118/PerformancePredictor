import ast
import os


class RouteDiscovery:

    def discover(self, project_path):

        app_file = os.path.join(project_path, "app.py")

        if not os.path.exists(app_file):

            raise FileNotFoundError("app.py not found")

        with open(app_file, "r", encoding="utf-8") as f:

            tree = ast.parse(f.read())

        routes = []

        for node in ast.walk(tree):

            if isinstance(node, ast.FunctionDef):

                for decorator in node.decorator_list:

                    if isinstance(decorator, ast.Call):

                        if hasattr(decorator.func, "attr"):

                            if decorator.func.attr == "route":

                                path = "/"
                                methods = ["GET"]

                                # Route path
                                if decorator.args:

                                    if isinstance(decorator.args[0], ast.Constant):

                                        path = decorator.args[0].value

                                # Route methods
                                for keyword in decorator.keywords:

                                    if keyword.arg == "methods":

                                        methods = []

                                        for item in keyword.value.elts:

                                            methods.append(item.value)

                                routes.append({

                                    "function": node.name,
                                    "path": path,
                                    "methods": methods

                                })

        return routes