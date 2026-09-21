from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional


class LocustGenerationError(ValueError):
    """Bad input (e.g. malformed route_discovery_result)."""


# ==========================================================================
# What this module does
# ==========================================================================
#
# Generates a Locust load-testing script from the routes discovered by
# route_discovery.py, for whichever framework was detected - Flask,
# FastAPI, Django, Express.js, or Spring Boot.
#
# The one thing that genuinely differs per framework is path-parameter
# syntax, and it has to be substituted with an actual value BEFORE Locust
# can request the path (a literal "/users/<int:id>" or "/users/{id}" is
# not a real URL). This is resolved at GENERATION time (baking a concrete
# path like "/users/1" directly into the generated file) rather than at
# Locust runtime, since route_discovery.py already extracted each route's
# typed parameter list - generation time is where that structured data is
# available.
#
# Substitution applies every known placeholder syntax unconditionally
# rather than branching on the detected framework - route_discovery.py
# doesn't always preserve a framework's native syntax verbatim (Django's
# re_path() regex groups get normalized to {id} form before this module
# ever sees them, while path() converters stay as <int:id>), so a strict
# per-framework mapping silently missed real cases. Trying all four
# patterns is safe: none of them can match another syntax's placeholder
# shape.
#
# Everything else (the status-code tracking listener, the HttpUser task
# structure) is already fully framework-agnostic, since it only cares
# about HTTP requests/responses, not what's serving them - unchanged from
# before.


_SUPPORTED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_MAX_TASKS = 200  # defensive cap - a repo with an unusually large number of routes shouldn't produce an unbounded locustfile

# --- Path-parameter substitution ---
#
# Each framework embeds path parameters differently:
#   Flask / Django path():   <int:id>  or  <id>
#   Django re_path():        (?P<id>\d+)   - a literal regex group, not a
#                             clean placeholder, so it must be substituted
#                             BEFORE the angle-bracket pattern below (it
#                             contains "<id>" as a substring, which would
#                             otherwise be wrongly matched on its own)
#   FastAPI / Spring Boot:   {id}
#   Express.js:               :id

_ANGLE_PARAM_PATTERN = re.compile(r"<(?:\w+:)?(\w+)>")
_CURLY_PARAM_PATTERN = re.compile(r"\{(\w+)\}")
_COLON_PARAM_PATTERN = re.compile(r":(\w+)")
_DJANGO_REGEX_GROUP_PATTERN = re.compile(r"\(\?P<(\w+)>[^)]*\)")

_SAMPLE_VALUES_BY_TYPE = {
    "int": "1", "integer": "1", "long": "1",
    "float": "1.0", "double": "1.0",
    "uuid": "00000000-0000-0000-0000-000000000001",
    "path": "sample/path",
    "slug": "sample-slug",
}
_DEFAULT_SAMPLE_VALUE = "1"


def _build_param_values(params: Optional[List[Dict[str, str]]]) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for p in params or []:
        name = p.get("name")
        if not name:
            continue
        param_type = str(p.get("type", "")).lower()
        values[name] = _SAMPLE_VALUES_BY_TYPE.get(param_type, _DEFAULT_SAMPLE_VALUE)
    return values


def _substitute(pattern: "re.Pattern[str]", path: str, values: Dict[str, str]) -> str:
    return pattern.sub(lambda m: values.get(m.group(1), _DEFAULT_SAMPLE_VALUE), path)


def _make_safe_path(path: str, params: Optional[List[Dict[str, str]]]) -> str:
    """Replace this route's path-parameter placeholders with concrete test
    values. Applies every known placeholder syntax unconditionally rather
    than branching on the detected framework - route_discovery.py doesn't
    always preserve a framework's native syntax verbatim (Django's
    re_path() regex groups get normalized to {name} form before this
    module ever sees them, while path() converters stay as <name>), so
    assuming one syntax per framework silently missed real cases. Applying
    all four patterns is safe: none of them can spuriously match another
    syntax's placeholder shape, and a path with no placeholders at all is
    simply left untouched by every pattern."""
    values = _build_param_values(params)

    # Order matters only for the first two: (?P<name>...) must be consumed
    # as a whole unit before the angle-bracket pattern, since it contains
    # "<name>" as a substring that would otherwise be partially (and
    # incorrectly) matched on its own.
    path = _substitute(_DJANGO_REGEX_GROUP_PATTERN, path, values)
    path = _substitute(_ANGLE_PARAM_PATTERN, path, values)
    path = _substitute(_CURLY_PARAM_PATTERN, path, values)
    path = _substitute(_COLON_PARAM_PATTERN, path, values)

    if not path.startswith("/"):
        path = "/" + path
    return path


def _path_to_identifier(path: str) -> str:
    """Fallback task-name source for routes with no handler name (most
    non-Flask/FastAPI extractions don't have one) - strips parameter
    syntax down to just the param names so the identifier stays readable,
    e.g. '/users/<int:id>' -> 'users_id'.

    Same ordering requirement as _make_safe_path(): the Django regex-group
    pattern must be consumed before the angle-bracket pattern, since
    "(?P<id>\\d+)" contains "<id>" as a substring that the angle pattern
    would otherwise partially match on its own."""
    cleaned = _DJANGO_REGEX_GROUP_PATTERN.sub(lambda m: m.group(1), path)
    cleaned = _ANGLE_PARAM_PATTERN.sub(lambda m: m.group(1), cleaned)
    cleaned = _CURLY_PARAM_PATTERN.sub(lambda m: m.group(1), cleaned)
    cleaned = _COLON_PARAM_PATTERN.sub(lambda m: m.group(1), cleaned)
    identifier = re.sub(r"[^a-zA-Z0-9]+", "_", cleaned).strip("_")
    return identifier or "root"


# --- Generator ---

class LocustGenerator:
    """
    Generates a Locust load-testing script from the routes discovered by
    route_discovery.py, for any supported framework.

    The generated file creates one task per (method, path), substitutes
    concrete test values for path parameters using the framework-
    appropriate syntax, and tracks HTTP status codes in
    status_counts.json - unchanged from before, since that part never
    depended on the framework being tested.
    """

    def __init__(
        self,
        max_tasks: int = _MAX_TASKS,
        wait_time_min: float = 0.1,
        wait_time_max: float = 0.5,
    ) -> None:
        if max_tasks <= 0:
            raise LocustGenerationError("max_tasks must be greater than zero.")
        if wait_time_min < 0 or wait_time_max < wait_time_min:
            raise LocustGenerationError("wait_time_min must be >= 0 and <= wait_time_max.")
        self.max_tasks = max_tasks
        self.wait_time_min = wait_time_min
        self.wait_time_max = wait_time_max

    def generate(self, project_path: str, route_discovery_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Args:
            project_path: directory to write locustfile.py / status_counts.json into.
            route_discovery_result: the dict returned by
                route_discovery.discover_routes() (or RouteDiscovery.discover()).

        Returns:
            {"locustfile_path", "status_file_path", "framework",
             "route_count", "task_count", "skipped_unsupported_method_count",
             "used_fallback_route", "truncated"}
        """
        if not project_path:
            raise LocustGenerationError("project_path is required.")
        if not isinstance(route_discovery_result, dict) or "routes" not in route_discovery_result:
            raise LocustGenerationError(
                "route_discovery_result must be the dict returned by route_discovery.discover_routes()."
            )

        framework = route_discovery_result.get("framework")
        routes = route_discovery_result.get("routes") or []

        locust_path = os.path.join(project_path, "locustfile.py")
        status_file = os.path.join(project_path, "status_counts.json")
        self._initialize_status_file(status_file)

        tasks, skipped_unsupported_method_count, truncated = self._build_tasks(routes)
        used_fallback_route = not tasks

        with open(locust_path, "w", encoding="utf-8") as f:
            self._write_header(f, status_file)
            self._write_status_listener(f)
            self._write_status_saver(f)
            self._write_user_class(f, tasks, self.wait_time_min, self.wait_time_max)

        print("locustfile.py generated successfully.")

        return {
            "locustfile_path": locust_path,
            "status_file_path": status_file,
            "framework": framework,
            "route_count": len(routes),
            "task_count": len(tasks) if tasks else 1,  # the fallback route itself counts as one task
            "skipped_unsupported_method_count": skipped_unsupported_method_count,
            "used_fallback_route": used_fallback_route,
            "truncated": truncated,
        }

    # --- Task construction ---

    def _build_tasks(self, routes: List[Dict[str, Any]]):
        tasks: List[Dict[str, str]] = []
        used_names = set()
        skipped_unsupported_method_count = 0
        truncated = False

        for route in routes:
            if len(tasks) >= self.max_tasks:
                truncated = True
                break

            path = route.get("path", "/")
            if not isinstance(path, str) or not path:
                path = "/"

            method = str(route.get("method", "GET")).upper()
            if method not in _SUPPORTED_METHODS:
                skipped_unsupported_method_count += 1
                continue

            safe_path = _make_safe_path(path, route.get("params"))

            handler = route.get("handler")
            base_name = handler if isinstance(handler, str) and handler else _path_to_identifier(path)
            task_name = re.sub(r"[^a-zA-Z0-9_]", "_", f"{base_name}_{method.lower()}")
            if not task_name or not task_name[0].isalpha():
                task_name = f"route_{task_name}".strip("_") or f"route_{method.lower()}"

            # Locust tasks are methods on the same class, so names must be unique.
            original_name = task_name
            counter = 2
            while task_name in used_names:
                task_name = f"{original_name}_{counter}"
                counter += 1
            used_names.add(task_name)

            tasks.append({"name": task_name, "method": method, "path": safe_path})

        return tasks, skipped_unsupported_method_count, truncated

    # --- File writing ---

    @staticmethod
    def _write_header(f, status_file: str) -> None:
        f.write(
            "import json\n"
            "import re\n"
            "import threading\n"
            "from locust import HttpUser, task, between, events\n\n"
            f"STATUS_FILE = {status_file!r}\n\n"
            "status_counts = {\n"
            "    'total_events': 0,\n"
            "    'responses_received': 0,\n"
            "    'status_2xx': 0,\n"
            "    'status_3xx': 0,\n"
            "    'status_4xx': 0,\n"
            "    'status_5xx': 0\n"
            "}\n\n"
            "status_lock = threading.Lock()\n\n"
        )

    @staticmethod
    def _write_status_listener(f) -> None:
        # Records every request event and classifies the response status.
        f.write(
            "@events.request.add_listener\n"
            "def record_http_status(\n"
            "    request_type,\n"
            "    name,\n"
            "    response_time,\n"
            "    response_length,\n"
            "    response,\n"
            "    context,\n"
            "    exception,\n"
            "    start_time,\n"
            "    url,\n"
            "    **kwargs\n"
            "):\n"
            "    with status_lock:\n"
            "        status_counts['total_events'] += 1\n\n"
            "    if response is None:\n"
            "        return\n\n"
            "    with status_lock:\n"
            "        status_counts['responses_received'] += 1\n\n"
            "    status_code = response.status_code\n\n"
            "    if 200 <= status_code < 300:\n"
            "        key = 'status_2xx'\n"
            "    elif 300 <= status_code < 400:\n"
            "        key = 'status_3xx'\n"
            "    elif 400 <= status_code < 500:\n"
            "        key = 'status_4xx'\n"
            "    elif 500 <= status_code < 600:\n"
            "        key = 'status_5xx'\n"
            "    else:\n"
            "        return\n\n"
            "    with status_lock:\n"
            "        status_counts[key] += 1\n\n"
        )

    @staticmethod
    def _write_status_saver(f) -> None:
        f.write(
            "@events.test_stop.add_listener\n"
            "def save_http_status_counts(environment, **kwargs):\n"
            "    try:\n"
            "        with status_lock:\n"
            "            counts = dict(status_counts)\n\n"
            "        with open(\n"
            "            STATUS_FILE,\n"
            "            'w',\n"
            "            encoding='utf-8'\n"
            "        ) as file:\n"
            "            json.dump(counts, file)\n"
            "    except OSError as error:\n"
            "        print(\n"
            "            f'Warning: Could not save '\n"
            "            f'status counts: {error}'\n"
            "        )\n\n"
        )

    @staticmethod
    def _write_user_class(f, tasks: List[Dict[str, str]], wait_time_min: float, wait_time_max: float) -> None:
        f.write(
            "class WebsiteUser(HttpUser):\n"
            f"    wait_time = between({wait_time_min!r}, {wait_time_max!r})\n\n"
            "    def _safe_path(self, path):\n"
            "        \"\"\"Defense-in-depth: strip any leftover route-parameter\n"
            "        syntax that generation-time substitution may have missed,\n"
            "        regardless of which framework's syntax it is.\"\"\"\n"
            "        path = re.sub(r'<[^>]+>', '1', path)\n"
            "        path = re.sub(r'\\{[^}]+\\}', '1', path)\n"
            "        return path\n\n"
            "    def _get(self, path):\n"
            "        return self.client.get(self._safe_path(path))\n\n"
            "    def _post(self, path):\n"
            "        return self.client.post(self._safe_path(path), data={})\n\n"
            "    def _put(self, path):\n"
            "        return self.client.put(self._safe_path(path), data={})\n\n"
            "    def _patch(self, path):\n"
            "        return self.client.patch(self._safe_path(path), data={})\n\n"
            "    def _delete(self, path):\n"
            "        return self.client.delete(self._safe_path(path))\n\n"
        )

        helper = {"GET": "_get", "POST": "_post", "PUT": "_put", "PATCH": "_patch", "DELETE": "_delete"}

        if not tasks:
            f.write(
                "    @task\n"
                "    def default_route(self):\n"
                "        self._get('/')\n\n"
            )
            return

        for t in tasks:
            f.write(
                "    @task\n"
                f"    def {t['name']}(self):\n"
                f"        self.{helper[t['method']]}({t['path']!r})\n\n"
            )

    @staticmethod
    def _initialize_status_file(status_file: str) -> None:
        """Create/reset status_counts.json before a new load-testing experiment."""
        directory = os.path.dirname(status_file)
        if directory:
            os.makedirs(directory, exist_ok=True)

        default_counts = {
            "total_events": 0,
            "responses_received": 0,
            "status_2xx": 0,
            "status_3xx": 0,
            "status_4xx": 0,
            "status_5xx": 0,
        }

        try:
            with open(status_file, "w", encoding="utf-8") as file:
                json.dump(default_counts, file)
        except OSError as e:
            raise RuntimeError(f"Could not initialize status_counts.json: {e}") from e


def generate_locustfile(project_path: str, route_discovery_result: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
    """One-shot: construct a LocustGenerator and run generate(). See
    LocustGenerator for configurable options (max_tasks, wait_time_min,
    wait_time_max)."""
    return LocustGenerator(**kwargs).generate(project_path, route_discovery_result)