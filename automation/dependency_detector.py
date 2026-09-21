from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse


class DependencyDetectionError(ValueError):
    """Bad input (e.g. repo_path doesn't exist)."""


# ==========================================================================
# What this module does
# ==========================================================================
#
# Static, best-effort detection of which database and cache a repository
# depends on - PostgreSQL, MySQL, MongoDB, Redis - from whatever evidence
# is sitting in the repository: dependency manifests, docker-compose
# service images, .env/config files, and (for Django specifically) the
# DATABASES setting's own backend string.
#
# Deliberately does NOT connect to a running database or container to
# verify anything - that's a different, much heavier feature (see the
# project roadmap's later "monitor the DB container" phase) with its own
# security and scope tradeoffs. This module only answers "what does this
# repo's own configuration claim it depends on", the same way
# framework_detector.py only answers "what does this repo's code/manifest
# evidence suggest", not "does it actually run".
#
# Output feeds application_descriptor.py's "database"/"cache"/
# "other_dependencies" fields directly - see that module's docstring for
# the exact contract this must satisfy.


# --- Scan bounds (mirrors framework_detector.py's approach for
#     consistency, even though this module tracks far fewer filenames) ---

DEFAULT_MAX_DEPTH = 6

_SKIP_DIR_NAMES = {
    "node_modules", "venv", "env", "__pycache__", "target", "build",
    "dist", "out", "bin", "obj", "vendor", "site-packages", "coverage",
}

_MANIFEST_FILENAMES = {
    "requirements.txt", "pyproject.toml", "Pipfile",
    "package.json",
    "pom.xml", "build.gradle", "build.gradle.kts",
}

_COMPOSE_FILENAMES = {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}

_ENV_FILENAMES = {
    ".env", ".env.example", ".env.sample", ".env.local",
    ".env.development", ".env.production", ".env.test",
}

_CONFIG_FILENAMES = {"application.properties", "application.yml", "application.yaml", "settings.py"}

_TRACKED_FILENAMES = _MANIFEST_FILENAMES | _COMPOSE_FILENAMES | _ENV_FILENAMES | _CONFIG_FILENAMES

_MAX_FILE_SIZE_BYTES = 200_000
_MAX_FILES_PER_NAME = 10  # a repo with more than this many settings.py/.env copies is unusual; cap for safety


def _should_skip_dir(name: str) -> bool:
    return name.startswith(".") or name in _SKIP_DIR_NAMES


# --- Configuration ---

@dataclass
class DependencySignalWeights:
    """
    docker_compose_image / connection_string_scheme / django_database_backend
    are weighted equally and higher than a bare manifest dependency,
    because each is direct evidence of actual configured usage (a real
    service image, a real connection string, a real Django backend
    setting) rather than just a client library sitting in a dependency
    list, which could be unused, leftover, or declared transitively.
    """
    manifest_dependency: float = 2.5
    docker_compose_image: float = 3.5
    connection_string_scheme: float = 3.5
    django_database_backend: float = 3.5
    env_var_name_hint: float = 1.0


@dataclass
class DependencyConfidenceThresholds:
    high_min_score: float = 5.0
    medium_min_score: float = 2.5
    ambiguous_score_gap: float = 1.5


# --- Vendor definitions (data, not code) ---

@dataclass(frozen=True)
class DependencyDefinition:
    key: str
    display_name: str
    role: str  # "database" or "cache"

    python_packages: Tuple[str, ...] = ()
    node_packages: Tuple[str, ...] = ()
    java_dependency_substrings: Tuple[str, ...] = ()
    compose_image_substrings: Tuple[str, ...] = ()
    connection_string_schemes: Tuple[str, ...] = ()
    env_var_name_prefixes: Tuple[str, ...] = ()
    django_backend_substrings: Tuple[str, ...] = ()


DEPENDENCY_DEFINITIONS: Tuple[DependencyDefinition, ...] = (
    DependencyDefinition(
        key="postgresql",
        display_name="PostgreSQL",
        role="database",
        python_packages=("psycopg2", "psycopg2-binary", "asyncpg", "aiopg"),
        node_packages=("pg", "postgres", "pg-promise"),
        java_dependency_substrings=("postgresql",),
        compose_image_substrings=("postgres", "timescaledb", "postgis"),
        connection_string_schemes=("postgres://", "postgresql://"),
        env_var_name_prefixes=("POSTGRES_", "PG_", "PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGPORT"),
        django_backend_substrings=("django.db.backends.postgresql", "django.db.backends.postgresql_psycopg2"),
    ),
    DependencyDefinition(
        key="mysql",
        display_name="MySQL",
        role="database",
        python_packages=("pymysql", "mysqlclient", "mysql-connector-python", "aiomysql"),
        node_packages=("mysql", "mysql2"),
        java_dependency_substrings=("mysql-connector",),
        compose_image_substrings=("mysql", "mariadb"),
        connection_string_schemes=("mysql://", "mysql2://"),
        env_var_name_prefixes=("MYSQL_",),
        django_backend_substrings=("django.db.backends.mysql",),
    ),
    DependencyDefinition(
        key="mongodb",
        display_name="MongoDB",
        role="database",
        python_packages=("pymongo", "motor", "djongo", "mongoengine"),
        node_packages=("mongodb", "mongoose"),
        java_dependency_substrings=("mongodb-driver", "spring-boot-starter-data-mongodb"),
        compose_image_substrings=("mongo",),
        connection_string_schemes=("mongodb://", "mongodb+srv://"),
        env_var_name_prefixes=("MONGO_", "MONGODB_"),
    ),
    DependencyDefinition(
        key="redis",
        display_name="Redis",
        role="cache",
        python_packages=("redis", "aioredis", "django-redis"),
        node_packages=("redis", "ioredis"),
        java_dependency_substrings=("spring-boot-starter-data-redis", "jedis", "lettuce-core"),
        compose_image_substrings=("redis",),
        connection_string_schemes=("redis://", "rediss://"),
        env_var_name_prefixes=("REDIS_",),
    ),
)


# --- Evidence records ---

@dataclass(frozen=True)
class DependencySignal:
    name: str
    weight: float
    source_file: str
    detail: str


@dataclass(frozen=True)
class DependencyCandidate:
    key: str
    display_name: str
    role: str
    score: float = 0.0
    signals: List[DependencySignal] = field(default_factory=list)


# --- File reading / lightweight parsing ---

def _read_text_safe(absolute_path: str, max_bytes: int = _MAX_FILE_SIZE_BYTES) -> str:
    try:
        with open(absolute_path, "rb") as f:
            raw = f.read(max_bytes)
    except OSError:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="ignore")


def _extract_package_json_packages(content: str) -> set:
    packages = set()
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return packages
    if not isinstance(data, dict):
        return packages
    for key in ("dependencies", "devDependencies"):
        deps = data.get(key)
        if isinstance(deps, dict):
            packages.update(name.lower() for name in deps.keys() if isinstance(name, str))
    return packages


def _manifest_declares_package(manifest_name: str, content: str, package_name: str) -> bool:
    """
    package.json is parsed as real JSON to avoid false positives from
    unrelated prose elsewhere in the file (a description mentioning
    "redis", say). requirements.txt/pyproject.toml/Pipfile/pom.xml/
    build.gradle are matched with a word-boundary regex directly against
    the file content instead of a full parser - these package names are
    distinctive enough (psycopg2, ioredis, mongodb-driver) that this
    doesn't meaningfully risk false positives, and it avoids needing a
    second TOML/XML/Gradle parser on top of framework_detector.py's.
    """
    if manifest_name == "package.json":
        return package_name.lower() in _extract_package_json_packages(content)
    return re.search(r"\b" + re.escape(package_name) + r"\b", content, re.IGNORECASE) is not None


# --- docker-compose scanning ---

def _scan_compose_services(content: str) -> List[Dict[str, str]]:
    """
    Lightweight, indentation-based scan for "<service>: \\n  image: <image>"
    pairs - not a full YAML parser. Avoids a hard PyYAML dependency for
    what's meant to be a best-effort presence signal, the same tradeoff
    framework_detector.py already makes for pyproject.toml/Pipfile
    (regex-based extraction instead of a real TOML parser). Handles the
    common, conventional docker-compose shape; unusual formatting
    (anchors, merge keys, inline flow-style mappings) is not resolved.
    """
    services: List[Dict[str, str]] = []
    current_service: Optional[str] = None
    current_indent: Optional[int] = None

    for line in content.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()

        service_match = re.match(r"^([A-Za-z0-9_.\-]+):\s*$", stripped)
        if service_match and indent > 0:
            if current_indent is None or indent <= current_indent + 2:
                current_service = service_match.group(1)
                current_indent = indent
            continue

        image_match = re.match(r"^image:\s*[\"']?([^\"'\s]+)", stripped)
        if image_match and current_service:
            services.append({"service_name": current_service, "image": image_match.group(1)})

    return services


# --- .env / config scanning ---

_ENV_LINE_PATTERN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*[\"']?([^\"'\s]*)")


def _scan_env_style_lines(content: str) -> List[Tuple[str, str]]:
    """Returns (var_name, value) pairs from .env-style KEY=value lines,
    Spring's key: value / key=value properties, or similar."""
    pairs = []
    for line in content.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        match = _ENV_LINE_PATTERN.match(line)
        if match:
            pairs.append((match.group(1), match.group(2)))
    return pairs

# ---------------------------------------------------------------------------
# Runtime database configuration resolution
# ---------------------------------------------------------------------------
#
# Dependency detection answers:
#   "Which database does this repository appear to use?"
#
# Runtime configuration resolution answers:
#   "Can we obtain enough connection information to monitor that database?"
#
# This function does NOT connect to the database.
# It only reads configuration already present in the cloned repository.
#
# Credentials are returned to the backend only. They must never be included
# in the dependency detection response sent to the frontend.
# ---------------------------------------------------------------------------

_DB_DEFAULT_PORTS = {
    "postgresql": 5432,
    "mysql": 3306,
    "mongodb": 27017,
    "redis": 6379,
}

_DB_ENV_KEYS = {
    "postgresql": {
        "host": ("POSTGRES_HOST", "PGHOST", "DB_HOST", "DATABASE_HOST"),
        "port": ("POSTGRES_PORT", "PGPORT", "DB_PORT", "DATABASE_PORT"),
        "user": ("POSTGRES_USER", "PGUSER", "DB_USER", "DATABASE_USER"),
        "password": ("POSTGRES_PASSWORD", "PGPASSWORD", "DB_PASSWORD", "DATABASE_PASSWORD"),
        "database": ("POSTGRES_DB", "PGDATABASE", "DB_NAME", "DATABASE_NAME"),
        "url": ("DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL", "DB_URL"),
    },
    "mysql": {
        "host": ("MYSQL_HOST", "DB_HOST", "DATABASE_HOST"),
        "port": ("MYSQL_PORT", "DB_PORT", "DATABASE_PORT"),
        "user": ("MYSQL_USER", "DB_USER", "DATABASE_USER"),
        "password": ("MYSQL_PASSWORD", "DB_PASSWORD", "DATABASE_PASSWORD"),
        "database": ("MYSQL_DATABASE", "MYSQL_DB", "DB_NAME", "DATABASE_NAME"),
        "url": ("DATABASE_URL", "MYSQL_URL", "DB_URL"),
    },
    "mongodb": {
        "host": ("MONGO_HOST", "MONGODB_HOST", "DB_HOST", "DATABASE_HOST"),
        "port": ("MONGO_PORT", "MONGODB_PORT", "DB_PORT", "DATABASE_PORT"),
        "user": ("MONGO_USER", "MONGODB_USER", "DB_USER", "DATABASE_USER"),
        "password": ("MONGO_PASSWORD", "MONGODB_PASSWORD", "DB_PASSWORD", "DATABASE_PASSWORD"),
        "database": ("MONGO_DATABASE", "MONGODB_DATABASE", "MONGO_DB", "MONGODB_DB", "DB_NAME", "DATABASE_NAME"),
        "url": ("MONGO_URL", "MONGODB_URL", "DATABASE_URL", "DB_URL"),
    },
    "redis": {
        "host": ("REDIS_HOST", "DB_HOST"),
        "port": ("REDIS_PORT", "DB_PORT"),
        "user": ("REDIS_USER", "DB_USER"),
        "password": ("REDIS_PASSWORD", "REDIS_AUTH", "DB_PASSWORD"),
        "database": ("REDIS_DB", "DB_NAME"),
        "url": ("REDIS_URL", "REDIS_URI", "DATABASE_URL", "DB_URL"),
    },
}


def _clean_config_value(value: Optional[str]) -> Optional[str]:
    """Normalize a configuration value and remove surrounding quotes."""
    if value is None:
        return None

    value = str(value).strip()

    if len(value) >= 2:
        if (value[0] == '"' and value[-1] == '"') or (
            value[0] == "'" and value[-1] == "'"
        ):
            value = value[1:-1].strip()

    return value or None


def _is_placeholder_value(value: Optional[str]) -> bool:
    """
    Detect values that look like examples/placeholders rather than real
    connection credentials.
    """
    if not value:
        return True

    normalized = value.strip().lower()

    placeholders = {
        "changeme",
        "change_me",
        "your_password",
        "yourpassword",
        "password",
        "example",
        "localhost:5432",
        "localhost:3306",
        "localhost:27017",
        "localhost:6379",
    }

    if normalized in placeholders:
        return True

    if normalized.startswith("${") and normalized.endswith("}"):
        return True

    if normalized.startswith("<") and normalized.endswith(">"):
        return True

    if "your_" in normalized:
        return True

    if "your-" in normalized:
        return True

    return False


def _resolve_env_reference(value: Optional[str], variables: Dict[str, str]) -> Optional[str]:
    """
    Resolve common ${VARIABLE} / $VARIABLE references using values already
    collected from repository configuration.
    """
    value = _clean_config_value(value)

    if not value:
        return None

    match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
    if match:
        return _clean_config_value(variables.get(match.group(1)))

    match = re.fullmatch(r"\$([A-Za-z_][A-Za-z0-9_]*)", value)
    if match:
        return _clean_config_value(variables.get(match.group(1)))

    return value


def _collect_repository_config(repo_path_abs: str) -> Tuple[Dict[str, str], List[str]]:
    """
    Collect configuration variables from tracked .env/config files and
    docker-compose files.

    This is intentionally lightweight and does not require PyYAML.
    """
    variables: Dict[str, str] = {}
    sources: List[str] = []

    tracked_files: Dict[str, List[str]] = {}

    for dirpath, dirnames, filenames in os.walk(repo_path_abs):
        rel_dir = os.path.relpath(dirpath, repo_path_abs)

        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1

        if depth >= DEFAULT_MAX_DEPTH:
            dirnames[:] = []
            continue

        dirnames[:] = [
            d for d in dirnames
            if not _should_skip_dir(d)
        ]

        for filename in filenames:
            if filename in _TRACKED_FILENAMES:
                rel_path = (
                    filename
                    if rel_dir == "."
                    else os.path.normpath(os.path.join(rel_dir, filename))
                )

                tracked_files.setdefault(filename, []).append(rel_path)

    # ---------------------------------------------------------------
    # .env / application.properties / application.yml
    # ---------------------------------------------------------------
    config_file_names = (
        _ENV_FILENAMES
        | {
            "application.properties",
            "application.yml",
            "application.yaml",
        }
    )

    for filename in config_file_names:
        for rel_path in tracked_files.get(filename, [])[:_MAX_FILES_PER_NAME]:
            absolute_path = os.path.join(repo_path_abs, rel_path)
            content = _read_text_safe(absolute_path)

            if not content:
                continue

            for key, value in _scan_env_style_lines(content):
                cleaned_value = _clean_config_value(value)

                if cleaned_value is None:
                    continue

                variables[key] = cleaned_value

                if rel_path not in sources:
                    sources.append(rel_path)

    # ---------------------------------------------------------------
    # docker-compose environment variables
    #
    # Supports common forms such as:
    #
    # environment:
    #   POSTGRES_USER: postgres
    #   POSTGRES_PASSWORD: secret
    #
    # and:
    #
    # environment:
    #   - POSTGRES_USER=postgres
    #   - POSTGRES_PASSWORD=secret
    # ---------------------------------------------------------------
    for compose_name in _COMPOSE_FILENAMES:
        for rel_path in tracked_files.get(compose_name, [])[:_MAX_FILES_PER_NAME]:
            absolute_path = os.path.join(repo_path_abs, rel_path)
            content = _read_text_safe(absolute_path)

            if not content:
                continue

            in_environment_block = False
            environment_indent = None

            for line in content.splitlines():
                if not line.strip() or line.strip().startswith("#"):
                    continue

                indent = len(line) - len(line.lstrip(" "))
                stripped = line.strip()

                if re.match(r"^environment\s*:\s*$", stripped):
                    in_environment_block = True
                    environment_indent = indent
                    continue

                if in_environment_block and indent <= environment_indent:
                    in_environment_block = False
                    environment_indent = None

                if not in_environment_block:
                    continue

                # List style:
                # - POSTGRES_USER=postgres
                list_match = re.match(
                    r"^-\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$",
                    stripped,
                )

                if list_match:
                    key = list_match.group(1)
                    value = _clean_config_value(list_match.group(2))

                    if value:
                        variables[key] = value
                        if rel_path not in sources:
                            sources.append(rel_path)

                    continue

                # Mapping style:
                # POSTGRES_USER: postgres
                map_match = re.match(
                    r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$",
                    stripped,
                )

                if map_match:
                    key = map_match.group(1)
                    value = _clean_config_value(map_match.group(2))

                    if value:
                        variables[key] = value
                        if rel_path not in sources:
                            sources.append(rel_path)

    return variables, sources


def _find_database_url(
    engine: str,
    variables: Dict[str, str],
) -> Optional[str]:
    """Find a configured connection URL for the selected database."""
    for key in _DB_ENV_KEYS.get(engine, {}).get("url", ()):
        value = variables.get(key)

        if not value:
            continue

        value = _resolve_env_reference(value, variables)

        if value and not _is_placeholder_value(value):
            return value

    return None


def _parse_database_url(
    engine: str,
    url: str,
) -> Dict[str, Any]:
    """Convert a database connection URL into DBMonitor parameters."""
    parsed = urlparse(url)

    result: Dict[str, Any] = {
        "host": parsed.hostname,
        "port": parsed.port,
        "user": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
        "database": parsed.path.lstrip("/") if parsed.path else None,
    }

    # MongoDB can contain the database in the path and Redis can contain
    # a database number.
    if engine == "redis" and parsed.path:
        database_value = parsed.path.lstrip("/")

        if database_value.isdigit():
            result["database"] = database_value

    # Use explicit query parameters if present.
    query = parse_qs(parsed.query)

    if not result["database"]:
        if "database" in query and query["database"]:
            result["database"] = query["database"][0]

    return result


def _find_database_field(
    engine: str,
    field_name: str,
    variables: Dict[str, str],
) -> Optional[str]:
    """Find one database configuration field from known environment names."""
    keys = _DB_ENV_KEYS.get(engine, {}).get(field_name, ())

    for key in keys:
        value = variables.get(key)

        if not value:
            continue

        value = _resolve_env_reference(value, variables)

        if value and not _is_placeholder_value(value):
            return value

    return None


def resolve_database_monitor_config(
    repo_path: str,
    dependency_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Resolve an internal DBMonitor configuration from repository configuration.

    Returns a backend-only configuration object.

    IMPORTANT:
    This result contains credentials and must NOT be sent to the frontend.
    """
    if not isinstance(repo_path, str) or not repo_path.strip():
        return {
            "enabled": False,
            "reason": "Repository path is required.",
        }

    repo_path_abs = os.path.abspath(repo_path)

    if not os.path.isdir(repo_path_abs):
        return {
            "enabled": False,
            "reason": f"Repository directory does not exist: {repo_path_abs}",
        }

    # Run dependency detection if the caller did not already provide it.
    if dependency_result is None:
        dependency_result = detect_dependencies(repo_path_abs)

    database = (
        dependency_result.get("database")
        if isinstance(dependency_result, dict)
        else None
    )

    if not isinstance(database, dict):
        return {
            "enabled": False,
            "reason": "No recognized database dependency was detected.",
        }

    engine = database.get("type")

    if engine not in _DB_DEFAULT_PORTS:
        return {
            "enabled": False,
            "reason": f"Unsupported database engine: {engine}",
        }

    variables, config_sources = _collect_repository_config(repo_path_abs)

    # ---------------------------------------------------------------
    # First preference: complete connection URL
    # ---------------------------------------------------------------
    database_url = _find_database_url(engine, variables)

    parsed_url = {}

    if database_url:
        try:
            parsed_url = _parse_database_url(engine, database_url)
        except ValueError:
            parsed_url = {}

    # ---------------------------------------------------------------
    # Second preference: individual environment/config values
    # ---------------------------------------------------------------
    host = parsed_url.get("host") or _find_database_field(
        engine,
        "host",
        variables,
    )

    port = parsed_url.get("port") or _find_database_field(
        engine,
        "port",
        variables,
    )

    user = parsed_url.get("user") or _find_database_field(
        engine,
        "user",
        variables,
    )

    password = parsed_url.get("password") or _find_database_field(
        engine,
        "password",
        variables,
    )

    database_name = parsed_url.get("database") or _find_database_field(
        engine,
        "database",
        variables,
    )

    # A configured database service without an explicit host generally
    # means localhost when DBMonitor runs on the same machine.
    if not host:
        host = "localhost"

    if not port:
        port = _DB_DEFAULT_PORTS[engine]

    try:
        port = int(port)
    except (TypeError, ValueError):
        port = _DB_DEFAULT_PORTS[engine]

    # DBMonitor requires a real reachable host and normally needs
    # authentication information for useful monitoring.
    if not database_url and not any(
        value for value in (user, password, database_name)
    ):
        return {
            "enabled": False,
            "engine": engine,
            "reason": (
                f"{database.get('type', engine)} was detected, but no usable "
                "runtime database configuration was found."
            ),
            "source": config_sources,
        }

    if not password and engine != "redis":
        return {
            "enabled": False,
            "engine": engine,
            "host": host,
            "port": port,
            "reason": (
                f"{database.get('type', engine)} was detected, but the "
                "database password/credential configuration was not found."
            ),
            "source": config_sources,
        }

    return {
        "enabled": True,
        "engine": engine,
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "database": database_name,
        "source": config_sources,
        "detection_confidence": database.get("confidence"),
    }


# --- Per-vendor evaluation ---

def _evaluate_vendor(
    definition: DependencyDefinition,
    repo_path_abs: str,
    tracked_files: Dict[str, List[str]],
    weights: DependencySignalWeights,
) -> DependencyCandidate:
    signals: List[DependencySignal] = []
    score = 0.0

    # Manifest dependency declarations
    package_sets = {
        "python": definition.python_packages,
        "node": definition.node_packages,
    }
    for manifest_name in ("requirements.txt", "pyproject.toml", "Pipfile"):
        for rel_path in tracked_files.get(manifest_name, [])[:_MAX_FILES_PER_NAME]:
            content = _read_text_safe(os.path.join(repo_path_abs, rel_path))
            for package_name in package_sets["python"]:
                if _manifest_declares_package(manifest_name, content, package_name):
                    signals.append(DependencySignal(
                        "manifest_dependency", weights.manifest_dependency, rel_path,
                        f"{definition.display_name} dependency '{package_name}' found in {manifest_name}",
                    ))
                    score += weights.manifest_dependency
                    break

    for rel_path in tracked_files.get("package.json", [])[:_MAX_FILES_PER_NAME]:
        content = _read_text_safe(os.path.join(repo_path_abs, rel_path))
        for package_name in definition.node_packages:
            if _manifest_declares_package("package.json", content, package_name):
                signals.append(DependencySignal(
                    "manifest_dependency", weights.manifest_dependency, rel_path,
                    f"{definition.display_name} dependency '{package_name}' found in package.json",
                ))
                score += weights.manifest_dependency
                break

    for manifest_name in ("pom.xml", "build.gradle", "build.gradle.kts"):
        for rel_path in tracked_files.get(manifest_name, [])[:_MAX_FILES_PER_NAME]:
            content = _read_text_safe(os.path.join(repo_path_abs, rel_path))
            for substring in definition.java_dependency_substrings:
                if substring.lower() in content.lower():
                    signals.append(DependencySignal(
                        "manifest_dependency", weights.manifest_dependency, rel_path,
                        f"{definition.display_name} dependency ('{substring}') found in {manifest_name}",
                    ))
                    score += weights.manifest_dependency
                    break

    # docker-compose service images
    for compose_name in _COMPOSE_FILENAMES:
        for rel_path in tracked_files.get(compose_name, [])[:_MAX_FILES_PER_NAME]:
            content = _read_text_safe(os.path.join(repo_path_abs, rel_path))
            for service in _scan_compose_services(content):
                image_lower = service["image"].lower()
                if any(pattern in image_lower for pattern in definition.compose_image_substrings):
                    signals.append(DependencySignal(
                        "docker_compose_image", weights.docker_compose_image, rel_path,
                        f"docker-compose service '{service['service_name']}' uses image '{service['image']}'",
                    ))
                    score += weights.docker_compose_image

    # Connection strings and env-var name hints in .env files, compose
    # "environment:" blocks, and Spring's application.properties/.yml
    scannable_names = _ENV_FILENAMES | _COMPOSE_FILENAMES | {"application.properties", "application.yml", "application.yaml"}
    for filename in scannable_names:
        for rel_path in tracked_files.get(filename, [])[:_MAX_FILES_PER_NAME]:
            content = _read_text_safe(os.path.join(repo_path_abs, rel_path))
            found_scheme = False
            for var_name, value in _scan_env_style_lines(content):
                value_lower = value.lower()
                if any(value_lower.startswith(scheme) for scheme in definition.connection_string_schemes):
                    signals.append(DependencySignal(
                        "connection_string_scheme", weights.connection_string_scheme, rel_path,
                        f"'{var_name}' in {filename} is a {definition.display_name} connection string",
                    ))
                    score += weights.connection_string_scheme
                    found_scheme = True
                    break
            if not found_scheme:
                for var_name, _value in _scan_env_style_lines(content):
                    if any(var_name.upper().startswith(prefix) for prefix in definition.env_var_name_prefixes):
                        signals.append(DependencySignal(
                            "env_var_name_hint", weights.env_var_name_hint, rel_path,
                            f"'{var_name}' in {filename} suggests {definition.display_name} configuration",
                        ))
                        score += weights.env_var_name_hint
                        break

    # Django's own DATABASES backend setting - direct, first-class evidence
    if definition.django_backend_substrings:
        for rel_path in tracked_files.get("settings.py", [])[:_MAX_FILES_PER_NAME]:
            content = _read_text_safe(os.path.join(repo_path_abs, rel_path))
            for substring in definition.django_backend_substrings:
                if substring in content:
                    signals.append(DependencySignal(
                        "django_database_backend", weights.django_database_backend, rel_path,
                        f"Django DATABASES ENGINE set to '{substring}' in {rel_path}",
                    ))
                    score += weights.django_database_backend
                    break

    return DependencyCandidate(
        key=definition.key,
        display_name=definition.display_name,
        role=definition.role,
        score=score,
        signals=signals,
    )


def _classify_confidence(top_score: float, second_score: Optional[float], thresholds: DependencyConfidenceThresholds) -> Tuple[str, bool]:
    ambiguous = (
        second_score is not None
        and second_score > 0
        and (top_score - second_score) <= thresholds.ambiguous_score_gap
    )
    if top_score >= thresholds.high_min_score and not ambiguous:
        return "High", ambiguous
    if top_score >= thresholds.medium_min_score:
        return "Medium", ambiguous
    return "Low", ambiguous


def _candidate_to_summary(candidate: DependencyCandidate) -> Dict[str, Any]:
    sources = sorted({s.source_file for s in candidate.signals})
    return {
        "type": candidate.key,
        "source": ", ".join(sources) if sources else "unknown",
        "confidence": None,  # filled in by the caller once ranked against same-role competitors
        "score": round(candidate.score, 2),
    }


class DependencyDetector:
    def __init__(
        self,
        max_depth: int = DEFAULT_MAX_DEPTH,
        weights: Optional[DependencySignalWeights] = None,
        confidence_thresholds: Optional[DependencyConfidenceThresholds] = None,
        definitions: Tuple[DependencyDefinition, ...] = DEPENDENCY_DEFINITIONS,
    ) -> None:
        self.max_depth = max_depth
        self.weights = weights or DependencySignalWeights()
        self.confidence_thresholds = confidence_thresholds or DependencyConfidenceThresholds()
        self.definitions = definitions

    def detect(self, repo_path: str) -> Dict[str, Any]:
        """
        Args:
            repo_path: path to a cloned repository (any directory).

        Returns:
            {"database": {...} | None, "cache": {...} | None,
             "other_dependencies": [...], "notes": [...], "scan_summary": {...}}
            matching the contract application_descriptor.py expects.

        Raises:
            DependencyDetectionError: repo_path is missing/not a directory.
        """
        if not isinstance(repo_path, str) or not repo_path.strip():
            raise DependencyDetectionError("repo_path must be a non-empty string.")

        repo_path_abs = os.path.abspath(repo_path)
        if not os.path.isdir(repo_path_abs):
            raise DependencyDetectionError(f"Not a directory: {repo_path_abs}")

        tracked_files = self._scan_repository(repo_path_abs)

        candidates = [
            _evaluate_vendor(definition, repo_path_abs, tracked_files, self.weights)
            for definition in self.definitions
        ]
        scored = [c for c in candidates if c.score > 0]

        database, cache, other_dependencies, notes = self._select_results(scored)

        if not database:
            notes.append("No recognized database dependency was found.")
        if not cache:
            notes.append("No recognized cache dependency was found.")

        return {
            "database": database,
            "cache": cache,
            "other_dependencies": other_dependencies,
            "notes": notes,
            "scan_summary": {
                "manifest_files_found": {k: v for k, v in tracked_files.items() if k in _MANIFEST_FILENAMES},
                "compose_files_found": {k: v for k, v in tracked_files.items() if k in _COMPOSE_FILENAMES},
                "env_files_found": {k: v for k, v in tracked_files.items() if k in _ENV_FILENAMES},
            },
        }

    def _scan_repository(self, repo_path_abs: str) -> Dict[str, List[str]]:
        tracked: Dict[str, List[str]] = {}
        for dirpath, dirnames, filenames in os.walk(repo_path_abs):
            rel_dir = os.path.relpath(dirpath, repo_path_abs)
            depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
            if depth >= self.max_depth:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]

            for filename in filenames:
                if filename in _TRACKED_FILENAMES:
                    rel_path = filename if rel_dir == "." else os.path.normpath(os.path.join(rel_dir, filename))
                    tracked.setdefault(filename, []).append(rel_path)

        return tracked

    def _select_results(
        self, scored: List[DependencyCandidate]
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], List[Dict[str, str]], List[str]]:
        notes: List[str] = []
        other_dependencies: List[Dict[str, str]] = []
        results: Dict[str, Optional[Dict[str, Any]]] = {"database": None, "cache": None}

        for role in ("database", "cache"):
            role_candidates = sorted((c for c in scored if c.role == role), key=lambda c: c.score, reverse=True)
            if not role_candidates:
                continue

            top = role_candidates[0]
            second_score = role_candidates[1].score if len(role_candidates) > 1 else None
            confidence, ambiguous = _classify_confidence(top.score, second_score, self.confidence_thresholds)

            summary = _candidate_to_summary(top)
            summary["confidence"] = confidence
            results[role] = summary

            if ambiguous:
                runner_up = role_candidates[1]
                notes.append(
                    f"Multiple {role} candidates were found - '{top.display_name}' scored highest "
                    f"({top.score:.1f}) but '{runner_up.display_name}' scored closely ({runner_up.score:.1f}); "
                    f"this may be a repo that genuinely uses more than one, or leftover/unused dependencies."
                )

            for extra in role_candidates[1:]:
                other_dependencies.append({
                    "name": extra.display_name,
                    "role": extra.role,
                    "source": ", ".join(sorted({s.source_file for s in extra.signals})),
                })

        return results["database"], results["cache"], other_dependencies, notes


def detect_dependencies(repo_path: str, **kwargs: Any) -> Dict[str, Any]:
    """One-shot: construct a DependencyDetector and run detect(). See
    DependencyDetector for all configurable options (scan bounds, signal
    weights, confidence thresholds)."""
    return DependencyDetector(**kwargs).detect(repo_path)
