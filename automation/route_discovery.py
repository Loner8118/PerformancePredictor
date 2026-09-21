from __future__ import annotations

import ast
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple


class RouteDiscoveryError(ValueError):
    """Bad input (e.g. repo_path doesn't exist, or detection_result/location_result malformed)."""


# ==========================================================================
# What this module does
# ==========================================================================
#
# Extracts HTTP routes (method, path, path parameters) from a cloned
# repository, normalized into one common format regardless of framework -
# {"method": "GET", "path": "/users/{id}", "params": [...], "handler":
# ..., "source_file": ...} - which is what locust_generator.py consumes.
#
# Route discovery rigor is a genuinely different question from framework
# *detection* rigor (framework_detector.py's support_tier), so this file
# has its own, separate tiering:
#
#   - Flask, FastAPI:  "full"        - AST-based (Python's stdlib `ast`
#     module), so this reads the actual parsed syntax tree, not text
#     patterns. Handles Blueprints/APIRouters and their prefixes.
#   - Express.js:       "good"       - regex-based (no JS parser in the
#     Python stdlib). Handles the common app.METHOD(...)/router.use(...)
#     patterns well, but can miss dynamically-constructed paths, template
#     literals, or unusual formatting - it is not a real parser.
#   - Django, Spring Boot: "best-effort" - Django's urls.py include(...)
#     chains aren't resolved (multi-file URL composition is genuinely
#     hard to do reliably with static analysis), and Django doesn't
#     declare HTTP methods at the routing level at all, so every Django
#     route is reported as GET with that assumption stated explicitly.
#     Spring Boot assumes one controller class per file (the common
#     convention) and uses regex on its annotations.
#
# Flask/FastAPI route discovery needs entry_point_locator.py's resolved
# app_variable to know which decorated object is actually the app
# instance (vs. some unrelated object that happens to have a .get()
# method) - so those two frameworks require location_result, not just
# detection_result.


DEFAULT_MAX_DEPTH = 6
DEFAULT_MAX_FILES_SCANNED = 500
DEFAULT_MAX_FILE_SIZE_BYTES = 200_000  # 200 KB - consistent with framework_detector.py's default

_SKIP_DIR_NAMES = {
    "node_modules", "venv", "env", "__pycache__", "target", "build",
    "dist", "out", "bin", "obj", "vendor", "site-packages", "coverage",
}

_LANGUAGE_EXTENSIONS: Dict[str, Set[str]] = {
    "python": {".py"},
    "javascript": {".js", ".mjs", ".cjs", ".ts", ".tsx"},
    "java": {".java"},
}

_SUPPORT_TIER: Dict[str, str] = {
    "flask": "full",
    "fastapi": "full",
    "express": "good",
    "django": "best-effort",
    "springboot": "best-effort",
}


def _should_skip_dir(name: str) -> bool:
    return name.startswith(".") or name in _SKIP_DIR_NAMES


def _find_source_files(repo_path_abs: str, extensions: Set[str], max_depth: int, max_files: int) -> List[str]:
    files: List[str] = []
    for dirpath, dirnames, filenames in os.walk(repo_path_abs):
        rel_dir = os.path.relpath(dirpath, repo_path_abs)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        if depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]

        for filename in filenames:
            _, ext = os.path.splitext(filename)
            if ext in extensions:
                rel_path = filename if rel_dir == "." else os.path.normpath(os.path.join(rel_dir, filename))
                files.append(rel_path)
                if len(files) >= max_files:
                    return files
    return files


def _read_text_safe(absolute_path: str, max_bytes: int) -> str:
    try:
        with open(absolute_path, "rb") as f:
            raw = f.read(max_bytes)
    except OSError:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="ignore")


def _join_path(prefix: str, path: str) -> str:
    if not prefix:
        return path or "/"
    combined = prefix.rstrip("/") + "/" + path.lstrip("/")
    combined = re.sub(r"/+", "/", combined)
    return combined or "/"


def _deduplicate_routes(routes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for route in routes:
        key = (route["method"], route["path"])
        if key not in seen:
            seen[key] = route
    return sorted(seen.values(), key=lambda r: (r["path"], r["method"]))


# --- Shared Python AST helpers (Flask + FastAPI) ---

def _extract_string_const(node: Optional[ast.AST]) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _extract_python_param_types(node: Any, path_param_names: List[str]) -> Dict[str, str]:
    """Cross-references a route function's argument type hints against its
    path parameters - e.g. `def get_user(id: int):` for `/users/{id}` gives
    a more accurate type than the URL pattern alone provides (FastAPI's
    {id} syntax carries no type info by itself, unlike Flask's <int:id>)."""
    types: Dict[str, str] = {}
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return types
    for arg in node.args.args:
        if arg.arg in path_param_names and isinstance(arg.annotation, ast.Name):
            types[arg.arg] = arg.annotation.id
    return types


# --- Flask ---

_FLASK_METHOD_SHORTCUTS = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH"}
_FLASK_PARAM_PATTERN = re.compile(r"<(?:(\w+):)?(\w+)>")


def _extract_flask_params(path: str) -> List[Dict[str, str]]:
    return [{"name": m.group(2), "type": m.group(1) or "string"} for m in _FLASK_PARAM_PATTERN.finditer(path)]


def _match_flask_decorator(decorator: ast.AST, owner_prefixes: Dict[str, str]) -> List[Tuple[str, str]]:
    if not isinstance(decorator, ast.Call):
        return []
    func = decorator.func
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
        return []
    owner, attr = func.value.id, func.attr
    if owner not in owner_prefixes:
        return []

    path_arg = decorator.args[0] if decorator.args else None
    path = _extract_string_const(path_arg)
    if path is None:
        return []
    full_path = _join_path(owner_prefixes[owner], path)

    if attr == "route":
        methods = ["GET"]
        for kw in decorator.keywords:
            if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                extracted = [
                    elt.value for elt in kw.value.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                ]
                if extracted:
                    methods = extracted
        return [(m.upper(), full_path) for m in methods]

    if attr in _FLASK_METHOD_SHORTCUTS:
        return [(_FLASK_METHOD_SHORTCUTS[attr], full_path)]

    return []


def _discover_flask_routes(
    repo_path_abs: str,
    app_variable: str,
    source_files: List[str],
    max_file_size_bytes: int,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    notes: List[str] = []
    blueprint_prefixes: Dict[str, str] = {}
    parsed_trees: Dict[str, ast.AST] = {}

    for rel_path in source_files:
        content = _read_text_safe(os.path.join(repo_path_abs, rel_path), max_file_size_bytes)
        if not content:
            continue
        try:
            tree = ast.parse(content, filename=rel_path)
        except SyntaxError:
            continue
        parsed_trees[rel_path] = tree

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                call = node.value
                if isinstance(call.func, ast.Name) and call.func.id == "Blueprint":
                    prefix = ""
                    for kw in call.keywords:
                        if kw.arg == "url_prefix":
                            prefix = _extract_string_const(kw.value) or ""
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            blueprint_prefixes[target.id] = prefix

    owner_prefixes = {app_variable: "", **blueprint_prefixes}
    routes: List[Dict[str, Any]] = []

    for rel_path, tree in parsed_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                for method, full_path in _match_flask_decorator(decorator, owner_prefixes):
                    routes.append({
                        "method": method,
                        "path": full_path,
                        "params": _extract_flask_params(full_path),
                        "handler": node.name,
                        "source_file": rel_path,
                    })

    if not blueprint_prefixes:
        notes.append("No Flask Blueprints were found; only routes on the main app instance were scanned.")
    else:
        notes.append(
            "Blueprint url_prefix values are read from the Blueprint(...) call itself - any "
            "additional prefix passed to app.register_blueprint(bp, url_prefix=...) at "
            "registration time is not resolved."
        )

    return routes, notes


# --- FastAPI ---

_FASTAPI_METHOD_SHORTCUTS = {
    "get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE",
    "patch": "PATCH", "options": "OPTIONS", "head": "HEAD",
}
_CURLY_PARAM_PATTERN = re.compile(r"\{(\w+)\}")


def _extract_curly_params(path: str) -> List[Dict[str, str]]:
    return [{"name": m.group(1), "type": "string"} for m in _CURLY_PARAM_PATTERN.finditer(path)]


def _discover_fastapi_routes(
    repo_path_abs: str,
    app_variable: str,
    source_files: List[str],
    max_file_size_bytes: int,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    notes: List[str] = []
    router_prefixes: Dict[str, str] = {}
    parsed_trees: Dict[str, ast.AST] = {}

    for rel_path in source_files:
        content = _read_text_safe(os.path.join(repo_path_abs, rel_path), max_file_size_bytes)
        if not content:
            continue
        try:
            tree = ast.parse(content, filename=rel_path)
        except SyntaxError:
            continue
        parsed_trees[rel_path] = tree

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                call = node.value
                if isinstance(call.func, ast.Name) and call.func.id == "APIRouter":
                    prefix = ""
                    for kw in call.keywords:
                        if kw.arg == "prefix":
                            prefix = _extract_string_const(kw.value) or ""
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            router_prefixes[target.id] = prefix

    owner_prefixes = {app_variable: "", **router_prefixes}
    routes: List[Dict[str, Any]] = []

    for rel_path, tree in parsed_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
                    continue
                owner, attr = func.value.id, func.attr
                if owner not in owner_prefixes or attr not in _FASTAPI_METHOD_SHORTCUTS:
                    continue

                path_arg = decorator.args[0] if decorator.args else None
                path = _extract_string_const(path_arg)
                if path is None:
                    continue

                full_path = _join_path(owner_prefixes[owner], path)
                params = _extract_curly_params(full_path)
                type_hints = _extract_python_param_types(node, [p["name"] for p in params])
                for p in params:
                    if p["name"] in type_hints:
                        p["type"] = type_hints[p["name"]]

                routes.append({
                    "method": _FASTAPI_METHOD_SHORTCUTS[attr],
                    "path": full_path,
                    "params": params,
                    "handler": node.name,
                    "source_file": rel_path,
                })

    if not router_prefixes:
        notes.append("No FastAPI APIRouters were found; only routes on the main app instance were scanned.")

    return routes, notes


# --- Express.js ---

_EXPRESS_METHOD_PATTERN = re.compile(r"""\b(\w+)\.(get|post|put|delete|patch)\s*\(\s*(['"])((?:(?!\3).)*)\3""")
_EXPRESS_APP_DECL_PATTERN = re.compile(r"\b(\w+)\s*=\s*express\s*\(\s*\)")
_EXPRESS_ROUTER_DECL_PATTERN = re.compile(r"\b(\w+)\s*=\s*express\.Router\s*\(")
_EXPRESS_USE_MOUNT_PATTERN = re.compile(r"""\b\w+\.use\s*\(\s*(['"])((?:(?!\1).)*)\1\s*,\s*(\w+)\s*\)""")
_EXPRESS_REQUIRE_PATTERN = re.compile(r"""\b(\w+)\s*=\s*require\s*\(\s*(['"])(\.[^'"]*)\2\s*\)""")
_COLON_PARAM_PATTERN = re.compile(r":(\w+)")


def _extract_colon_params(path: str) -> List[Dict[str, str]]:
    return [{"name": m.group(1), "type": "string"} for m in _COLON_PARAM_PATTERN.finditer(path)]


def _resolve_require_path(importing_file_rel: str, require_spec: str) -> str:
    """
    Resolve a relative require('./foo') spec to a repo-relative file path.
    A local variable mounted via app.use(prefix, someRouter) is almost
    always require()'d from another file, where the router has a
    DIFFERENT local variable name than at its point of declaration (e.g.
    index.js imports it as `usersRouter`, but inside routes/users.js it's
    declared as `router`) - matching by variable name alone can't bridge
    that, so mount prefixes are resolved by FILE instead (see
    _discover_express_routes below).
    """
    base_dir = os.path.dirname(importing_file_rel)
    resolved = os.path.normpath(os.path.join(base_dir, require_spec)) if base_dir else os.path.normpath(require_spec)
    if os.path.splitext(resolved)[1]:
        return resolved
    return resolved + ".js"  # require() allows omitting the extension - doesn't handle index.js-in-directory resolution


def _discover_express_routes(
    repo_path_abs: str,
    source_files: List[str],
    max_file_size_bytes: int,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    notes: List[str] = []
    file_contents: Dict[str, str] = {}
    require_maps: Dict[str, Dict[str, str]] = {}  # file -> {local_var: resolved_target_file}
    known_owners_by_file: Dict[str, Set[str]] = {}  # file -> {app/router variable names declared in it}
    router_declared_anywhere = False
    unrestricted_files: Set[str] = set()

    for rel_path in source_files:
        content = _read_text_safe(os.path.join(repo_path_abs, rel_path), max_file_size_bytes)
        if not content:
            continue
        file_contents[rel_path] = content

        owners = {m.group(1) for m in _EXPRESS_APP_DECL_PATTERN.finditer(content)}
        owners |= {m.group(1) for m in _EXPRESS_ROUTER_DECL_PATTERN.finditer(content)}
        if owners:
            known_owners_by_file[rel_path] = owners
            router_declared_anywhere = router_declared_anywhere or bool(_EXPRESS_ROUTER_DECL_PATTERN.search(content))
        else:
            # Neither express() nor express.Router() was declared locally -
            # likely imported from elsewhere (e.g. `const app = require('./app')`).
            # Fall back to unrestricted owner matching for this file rather
            # than silently dropping its routes.
            unrestricted_files.add(rel_path)

        require_map: Dict[str, str] = {}
        for m in _EXPRESS_REQUIRE_PATTERN.finditer(content):
            var_name, _, spec = m.groups()
            require_map[var_name] = _resolve_require_path(rel_path, spec)
        require_maps[rel_path] = require_map

    # Resolve app.use(prefix, X) mounts to a target FILE where possible
    # (the common cross-file router pattern), falling back to a same-file
    # variable-name match for a router declared and mounted in one file.
    mount_prefixes_by_file: Dict[str, str] = {}
    mount_prefixes_by_local_var: Dict[Tuple[str, str], str] = {}

    for rel_path, content in file_contents.items():
        require_map = require_maps.get(rel_path, {})
        for m in _EXPRESS_USE_MOUNT_PATTERN.finditer(content):
            prefix, mounted_var = m.group(2), m.group(3)
            if mounted_var in require_map:
                mount_prefixes_by_file[require_map[mounted_var]] = prefix
            else:
                mount_prefixes_by_local_var[(rel_path, mounted_var)] = prefix

    routes: List[Dict[str, Any]] = []
    for rel_path, content in file_contents.items():
        file_prefix = mount_prefixes_by_file.get(rel_path, "")
        allowed_owners = known_owners_by_file.get(rel_path)
        for m in _EXPRESS_METHOD_PATTERN.finditer(content):
            owner, method, _, path = m.groups()
            # Restricting to known app/router variables (rather than
            # matching ANY object with a .get(string)/.post(string) call)
            # avoids false positives from unrelated calls that happen to
            # share the shape, e.g. `cache.get('some-key')`.
            if allowed_owners is not None and owner not in allowed_owners:
                continue
            prefix = file_prefix or mount_prefixes_by_local_var.get((rel_path, owner), "")
            full_path = _join_path(prefix, path)
            routes.append({
                "method": method.upper(),
                "path": full_path,
                "params": _extract_colon_params(full_path),
                "handler": None,
                "source_file": rel_path,
            })

    if not router_declared_anywhere:
        notes.append("No express.Router() instances were found; only direct app.<method>(...) calls were scanned.")
    if unrestricted_files:
        notes.append(
            f"{len(unrestricted_files)} file(s) call .get()/.post()/etc without a local express()/"
            f"Router() declaration (likely imported from elsewhere) - matched without owner "
            f"restriction, so an unrelated object with a same-shaped call could be misreported "
            f"as a route in those files."
        )
    notes.append(
        "Express route discovery uses regex pattern matching, not a full JavaScript parser - "
        "dynamically-constructed paths, template literals, or unusual formatting may be missed. "
        "Router mount prefixes are resolved by following require('./relative/path') imports; "
        "routers imported via a package name, a re-exported/aliased module, or index.js directory "
        "resolution are not resolved."
    )
    return routes, notes


# --- Django (best-effort) ---

_DJANGO_CONVERTER_PARAM_PATTERN = re.compile(r"<(?:(\w+):)?(\w+)>")
_DJANGO_REGEX_NAMED_GROUP_PATTERN = re.compile(r"\(\?P<(\w+)>")
_DJANGO_REGEX_NAMED_GROUP_INLINE_PATTERN = re.compile(r"\(\?P<(\w+)>[^)]*\)")


def _extract_django_params(raw_path: str) -> List[Dict[str, str]]:
    params = [
        {"name": m.group(2), "type": m.group(1) or "string"}
        for m in _DJANGO_CONVERTER_PARAM_PATTERN.finditer(raw_path)
    ]
    if not params:
        params = [{"name": m.group(1), "type": "string"} for m in _DJANGO_REGEX_NAMED_GROUP_PATTERN.finditer(raw_path)]
    return params


def _normalize_django_regex_groups(raw_path: str) -> str:
    """re_path()'s named regex groups, e.g. (?P<id>\\d+), aren't a usable
    HTTP path for anything downstream - unlike path()'s <type:name> or
    FastAPI's {name}, raw regex source isn't a bounded, recognizable
    placeholder. Collapse each named group down to a plain {name}
    placeholder instead; a no-op for path()-style routes, which don't
    contain this syntax."""
    return _DJANGO_REGEX_NAMED_GROUP_INLINE_PATTERN.sub(r"{\1}", raw_path)


def _discover_django_routes(
    repo_path_abs: str,
    source_files: List[str],
    max_file_size_bytes: int,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    notes: List[str] = []
    urls_files = [f for f in source_files if os.path.basename(f) == "urls.py"]

    if not urls_files:
        notes.append("No urls.py files were found.")
        return [], notes

    routes: List[Dict[str, Any]] = []
    skipped_includes = 0

    for rel_path in urls_files:
        content = _read_text_safe(os.path.join(repo_path_abs, rel_path), max_file_size_bytes)
        if not content:
            continue
        try:
            tree = ast.parse(content, filename=rel_path)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("path", "re_path")):
                continue
            if not node.args:
                continue

            raw_path = _extract_string_const(node.args[0])
            if raw_path is None:
                continue

            is_include = (
                len(node.args) > 1
                and isinstance(node.args[1], ast.Call)
                and isinstance(node.args[1].func, ast.Name)
                and node.args[1].func.id == "include"
            )
            if is_include:
                skipped_includes += 1
                continue

            normalized_path = "/" + _normalize_django_regex_groups(raw_path).lstrip("^").rstrip("$").strip("/")
            routes.append({
                "method": "GET",
                "path": normalized_path or "/",
                "params": _extract_django_params(raw_path),
                "handler": None,
                "source_file": rel_path,
            })

    if skipped_includes:
        notes.append(
            f"{skipped_includes} include(...) reference(s) to other URL configs were found but not "
            f"resolved - nested app URLs behind an include() are not discovered."
        )
    notes.append(
        "Django doesn't declare HTTP methods at the URL-routing level - every discovered route is "
        "reported as GET. The real supported methods are determined by the view function/class "
        "and aren't reflected here."
    )
    return routes, notes


# --- Spring Boot (best-effort) ---

_SPRING_METHOD_MAPPING_PATTERN = re.compile(
    r'@(GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping)\s*(?:\(\s*(?:value\s*=\s*)?"([^"]*)"\s*\))?'
)
_SPRING_GENERIC_MAPPING_PATTERN = re.compile(
    r'@RequestMapping\s*\(\s*(?:value\s*=\s*)?"([^"]*)"(?:\s*,\s*method\s*=\s*RequestMethod\.(\w+))?\s*\)'
)
_SPRING_MAPPING_TO_METHOD = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
    "DeleteMapping": "DELETE", "PatchMapping": "PATCH",
}


def _discover_springboot_routes(
    repo_path_abs: str,
    source_files: List[str],
    max_file_size_bytes: int,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    notes: List[str] = []
    routes: List[Dict[str, Any]] = []

    for rel_path in source_files:
        content = _read_text_safe(os.path.join(repo_path_abs, rel_path), max_file_size_bytes)
        if not content or ("@RestController" not in content and "@Controller" not in content):
            continue  # only scan controller files - reduces false positives from unrelated annotations

        class_match = re.search(r"\bclass\s+\w+", content)
        pre_class_region = content[: class_match.start()] if class_match else content
        post_class_region = content[class_match.end():] if class_match else content

        prefix_match = re.search(r'@RequestMapping\s*\(\s*(?:value\s*=\s*)?"([^"]*)"', pre_class_region)
        prefix = prefix_match.group(1) if prefix_match else ""

        for m in _SPRING_METHOD_MAPPING_PATTERN.finditer(post_class_region):
            annotation, path = m.group(1), m.group(2) or ""
            full_path = _join_path(prefix, path) if path else (prefix or "/")
            routes.append({
                "method": _SPRING_MAPPING_TO_METHOD[annotation],
                "path": full_path,
                "params": _extract_curly_params(full_path),
                "handler": None,
                "source_file": rel_path,
            })

        for m in _SPRING_GENERIC_MAPPING_PATTERN.finditer(post_class_region):
            path, method = m.group(1), (m.group(2) or "GET")
            full_path = _join_path(prefix, path) if path else (prefix or "/")
            routes.append({
                "method": method.upper(),
                "path": full_path,
                "params": _extract_curly_params(full_path),
                "handler": None,
                "source_file": rel_path,
            })

    notes.append(
        "Spring Boot route discovery uses regex pattern matching on @RequestMapping/@GetMapping-"
        "style annotations, not a full Java parser - assumes one controller class per file (the "
        "common convention); multiple controllers in a single file are not fully disambiguated."
    )
    return routes, notes


# --- Route Discovery ---

class RouteDiscovery:
    def __init__(
        self,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_files_scanned: int = DEFAULT_MAX_FILES_SCANNED,
        max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
    ) -> None:
        self.max_depth = max_depth
        self.max_files_scanned = max_files_scanned
        self.max_file_size_bytes = max_file_size_bytes

    def discover(
        self,
        repo_path: str,
        detection_result: Dict[str, Any],
        location_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Args:
            repo_path: path to the cloned repository.
            detection_result: output of framework_detector.detect_framework(repo_path).
            location_result: output of entry_point_locator.locate_entry_point(...) -
                REQUIRED for Flask/FastAPI (needed to know which decorated
                object is the actual app instance), optional/unused for
                Django, Express, and Spring Boot.

        Returns:
            {"framework", "routes", "route_count", "support_tier",
             "files_scanned", "notes"}.

        Raises:
            RouteDiscoveryError: bad repo_path, malformed detection_result,
                or missing location_result for a framework that needs it.
        """
        if not isinstance(repo_path, str) or not repo_path.strip():
            raise RouteDiscoveryError("repo_path must be a non-empty string.")
        repo_path_abs = os.path.abspath(repo_path)
        if not os.path.isdir(repo_path_abs):
            raise RouteDiscoveryError(f"Not a directory: {repo_path_abs}")

        if not isinstance(detection_result, dict) or "detected_framework" not in detection_result:
            raise RouteDiscoveryError(
                "detection_result must be the dict returned by framework_detector.detect_framework()."
            )

        framework = detection_result.get("detected_framework")
        if framework is None:
            return self._empty_result(None, ["No framework was detected, so route discovery cannot run."])

        extensions = _LANGUAGE_EXTENSIONS.get(detection_result.get("language"), set())
        if not extensions:
            return self._empty_result(
                framework, [f"No source file extensions are registered for language '{detection_result.get('language')}'."]
            )

        source_files = _find_source_files(repo_path_abs, extensions, self.max_depth, self.max_files_scanned)

        if framework in ("flask", "fastapi"):
            if not isinstance(location_result, dict) or not location_result.get("app_variable"):
                raise RouteDiscoveryError(
                    f"{framework} route discovery requires location_result with a resolved "
                    f"app_variable - run entry_point_locator.locate_entry_point() first and pass "
                    f"its result here."
                )
            app_variable = location_result["app_variable"]
            if framework == "flask":
                routes, notes = _discover_flask_routes(repo_path_abs, app_variable, source_files, self.max_file_size_bytes)
            else:
                routes, notes = _discover_fastapi_routes(repo_path_abs, app_variable, source_files, self.max_file_size_bytes)
        elif framework == "express":
            routes, notes = _discover_express_routes(repo_path_abs, source_files, self.max_file_size_bytes)
        elif framework == "django":
            routes, notes = _discover_django_routes(repo_path_abs, source_files, self.max_file_size_bytes)
        elif framework == "springboot":
            routes, notes = _discover_springboot_routes(repo_path_abs, source_files, self.max_file_size_bytes)
        else:
            return self._empty_result(framework, [f"No route discovery is implemented for framework '{framework}'."])

        deduplicated = _deduplicate_routes(routes)
        if not deduplicated:
            notes.append("No routes were discovered - the pipeline will have nothing to load-test.")

        return {
            "framework": framework,
            "routes": deduplicated,
            "route_count": len(deduplicated),
            "support_tier": _SUPPORT_TIER.get(framework, "unknown"),
            "files_scanned": len(source_files),
            "notes": notes,
        }

    @staticmethod
    def _empty_result(framework: Optional[str], notes: List[str]) -> Dict[str, Any]:
        return {
            "framework": framework,
            "routes": [],
            "route_count": 0,
            "support_tier": _SUPPORT_TIER.get(framework) if framework else None,
            "files_scanned": 0,
            "notes": notes,
        }


def discover_routes(
    repo_path: str,
    detection_result: Dict[str, Any],
    location_result: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """One-shot: construct a RouteDiscovery and run discover(). See
    RouteDiscovery for configurable options (scan bounds)."""
    return RouteDiscovery(**kwargs).discover(repo_path, detection_result, location_result)