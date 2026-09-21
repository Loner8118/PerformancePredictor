from __future__ import annotations

import importlib.util
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


class DBMonitorError(RuntimeError):
    """Base exception for DBMonitor-specific failures."""


class DBMonitorUnsupportedEngineError(DBMonitorError, ValueError):
    """engine isn't one of the supported keys - also a ValueError, since
    this is a caller/configuration mistake, not a runtime failure."""


class DBMonitorMissingDriverError(DBMonitorError):
    """The Python client library needed to speak this engine's protocol
    isn't installed. Raised synchronously from __init__, not from inside
    the background thread - a missing driver is knowable immediately and
    is far more useful to fail on before an entire load-testing
    experiment starts than to discover silently in a background thread
    partway through one."""


# ==========================================================================
# What this module does, and what it deliberately doesn't
# ==========================================================================
#
# runtime_monitor.py answers "how much CPU/memory/disk/network is this
# CONTAINER using" via Docker's stats API - true for any container
# regardless of what's running inside it. This module answers a
# different question that Docker's stats API has no way to answer:
# "how loaded is the DATABASE/CACHE ITSELF" - active connections,
# connection-pool utilization, cache hit ratio - which means actually
# speaking each engine's own wire protocol (a SQL query, a Mongo
# command, a Redis INFO call), not reading container-level counters.
# There is no way to unify this across engines the way Docker stats
# unifies CPU/memory across frameworks; PostgreSQL, MySQL, MongoDB, and
# Redis simply don't share a introspection protocol. What IS unified is
# the shape of the result and the start()/stop()/summary() lifecycle,
# both deliberately mirroring runtime_monitor.py's - same background-
# thread pattern, same join-with-generous-timeout fix for the same
# "reused across every load level and repetition" reason, same
# philosophy of one bad sample not aborting the whole collection window.
#
# Explicitly out of scope:
#   - Discovering connection credentials on its own. This takes a host,
#     port, and credentials as explicit input (typically the same values
#     dependency_detector.py found evidence of - e.g. a docker-compose
#     POSTGRES_PASSWORD - but reconstructing and trusting arbitrary
#     credentials parsed automatically out of a cloned repo's config
#     files is a different, much riskier problem than detecting that a
#     config key merely exists, which is all dependency_detector.py
#     claims to do).
#   - Starting, stopping, or otherwise orchestrating the database
#     container. This assumes something else already has it running and
#     reachable, exactly like runtime_monitor.py assumes the application
#     container is already running and just takes its container_id.
#   - Query-level profiling (slow query logs, explain plans, etc.) -
#     this reports connection/pool pressure, not query optimization
#     advice.


@dataclass
class ConnectionParams:
    host: str
    port: Optional[int] = None  # falls back to the engine's conventional default port if None
    user: Optional[str] = None
    password: Optional[str] = None
    database: Optional[str] = None


@dataclass
class _EngineProbe:
    key: str
    display_name: str
    default_port: int
    required_package: str
    install_hint: str
    connect: Callable[[ConnectionParams, float], Any]
    sample: Callable[[Any], Dict[str, Any]]
    close: Callable[[Any], None]


# --- PostgreSQL ---

def _pg_connect(params: ConnectionParams, timeout: float) -> Any:
    import psycopg2
    conn = psycopg2.connect(
        host=params.host,
        port=params.port or 5432,
        user=params.user,
        password=params.password,
        dbname=params.database,
        connect_timeout=max(1, int(timeout)),
    )
    # Autocommit avoids leaving this monitoring connection's own SELECTs
    # sitting in an open transaction between polls - which would
    # otherwise show up as one more "active connection" in the very
    # count this is trying to measure.
    conn.autocommit = True
    return conn


def _pg_sample(conn: Any) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database();")
        active = cur.fetchone()[0]
        cur.execute("SHOW max_connections;")
        max_connections = int(cur.fetchone()[0])

    utilization = (active / max_connections * 100.0) if max_connections > 0 else None
    return {
        "active_connections": active,
        "max_connections": max_connections,
        "connection_pool_utilization_pct": round(utilization, 2) if utilization is not None else None,
        "extra": {},
    }


def _pg_close(conn: Any) -> None:
    conn.close()


# --- MySQL ---

def _mysql_connect(params: ConnectionParams, timeout: float) -> Any:
    import pymysql
    return pymysql.connect(
        host=params.host,
        port=params.port or 3306,
        user=params.user,
        password=params.password or "",
        database=params.database,
        connect_timeout=max(1, int(timeout)),
        autocommit=True,  # same reasoning as PostgreSQL above
    )


def _mysql_sample(conn: Any) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SHOW STATUS LIKE 'Threads_connected';")
        active = int(cur.fetchone()[1])
        cur.execute("SHOW VARIABLES LIKE 'max_connections';")
        max_connections = int(cur.fetchone()[1])

    utilization = (active / max_connections * 100.0) if max_connections > 0 else None
    return {
        "active_connections": active,
        "max_connections": max_connections,
        "connection_pool_utilization_pct": round(utilization, 2) if utilization is not None else None,
        "extra": {},
    }


def _mysql_close(conn: Any) -> None:
    conn.close()


# --- MongoDB ---

def _mongo_connect(params: ConnectionParams, timeout: float) -> Any:
    import pymongo
    client = pymongo.MongoClient(
        host=params.host,
        port=params.port or 27017,
        username=params.user,
        password=params.password,
        serverSelectionTimeoutMS=int(timeout * 1000),
        connectTimeoutMS=int(timeout * 1000),
    )
    client.admin.command("ping")  # force the connection now so a bad host/auth fails fast, not on the first sample
    return client


def _mongo_sample(client: Any) -> Dict[str, Any]:
    status = client.admin.command("serverStatus")
    connections = status.get("connections", {})
    current = connections.get("current")
    available = connections.get("available")
    max_connections = (current + available) if (current is not None and available is not None) else None

    utilization = (
        (current / max_connections * 100.0) if (max_connections and max_connections > 0) else None
    )
    return {
        "active_connections": current,
        "max_connections": max_connections,
        "connection_pool_utilization_pct": round(utilization, 2) if utilization is not None else None,
        "extra": {"available_connections": available},
    }


def _mongo_close(client: Any) -> None:
    client.close()


# --- Redis ---

def _redis_connect(params: ConnectionParams, timeout: float) -> Any:
    import redis
    client = redis.Redis(
        host=params.host,
        port=params.port or 6379,
        password=params.password,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
        decode_responses=True,
    )
    client.ping()  # force the connection now, same reasoning as MongoDB above
    return client


def _redis_hit_rate_pct(info: Dict[str, Any]) -> Optional[float]:
    hits = info.get("keyspace_hits")
    misses = info.get("keyspace_misses")
    if hits is None or misses is None or (hits + misses) == 0:
        return None
    return round(hits / (hits + misses) * 100.0, 2)


def _redis_sample(client: Any) -> Dict[str, Any]:
    info = client.info()
    active = info.get("connected_clients")
    max_connections = info.get("maxclients")
    utilization = (
        (active / max_connections * 100.0) if (active is not None and max_connections) else None
    )

    used_memory = info.get("used_memory")
    max_memory = info.get("maxmemory")  # 0 means "unlimited" in Redis, not a real ceiling
    memory_pct = (used_memory / max_memory * 100.0) if (used_memory is not None and max_memory) else None

    return {
        "active_connections": active,
        "max_connections": max_connections,
        "connection_pool_utilization_pct": round(utilization, 2) if utilization is not None else None,
        "extra": {
            "ops_per_sec": info.get("instantaneous_ops_per_sec"),
            "used_memory_mb": round(used_memory / 1024 / 1024, 2) if used_memory is not None else None,
            "memory_utilization_pct": round(memory_pct, 2) if memory_pct is not None else None,
            "keyspace_hit_rate_pct": _redis_hit_rate_pct(info),
        },
    }


def _redis_close(client: Any) -> None:
    client.close()


_ENGINE_PROBES: Dict[str, _EngineProbe] = {
    "postgresql": _EngineProbe(
        key="postgresql", display_name="PostgreSQL", default_port=5432,
        required_package="psycopg2", install_hint="psycopg2-binary",
        connect=_pg_connect, sample=_pg_sample, close=_pg_close,
    ),
    "mysql": _EngineProbe(
        key="mysql", display_name="MySQL", default_port=3306,
        required_package="pymysql", install_hint="PyMySQL",
        connect=_mysql_connect, sample=_mysql_sample, close=_mysql_close,
    ),
    "mongodb": _EngineProbe(
        key="mongodb", display_name="MongoDB", default_port=27017,
        required_package="pymongo", install_hint="pymongo",
        connect=_mongo_connect, sample=_mongo_sample, close=_mongo_close,
    ),
    "redis": _EngineProbe(
        key="redis", display_name="Redis", default_port=6379,
        required_package="redis", install_hint="redis",
        connect=_redis_connect, sample=_redis_sample, close=_redis_close,
    ),
}


def _check_driver_installed(probe: _EngineProbe) -> None:
    if importlib.util.find_spec(probe.required_package) is None:
        raise DBMonitorMissingDriverError(
            f"{probe.display_name} monitoring requires the '{probe.required_package}' package "
            f"(pip install {probe.install_hint}), which isn't installed."
        )


class DBMonitor:
    """
    Monitors connection/pool pressure on a running PostgreSQL, MySQL,
    MongoDB, or Redis instance. Runs in a background thread, the same
    start()/stop()/summary() lifecycle as RuntimeMonitor, so it can be
    polled alongside container-level monitoring during a Locust run.
    """

    DEFAULT_INTERVAL_SECONDS = 2.0
    DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0

    def __init__(
        self,
        engine: str,
        connection_params: ConnectionParams,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        probe = _ENGINE_PROBES.get(engine)
        if probe is None:
            raise DBMonitorUnsupportedEngineError(
                f"Unsupported engine {engine!r}. Supported: {', '.join(sorted(_ENGINE_PROBES))}."
            )
        if not isinstance(connection_params, ConnectionParams):
            raise DBMonitorError("connection_params must be a ConnectionParams instance.")
        if not connection_params.host:
            raise DBMonitorError("connection_params.host is required.")
        if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
            raise DBMonitorError("interval must be a positive number.")
        if (
            not isinstance(connect_timeout_seconds, (int, float))
            or isinstance(connect_timeout_seconds, bool)
            or connect_timeout_seconds <= 0
        ):
            raise DBMonitorError("connect_timeout_seconds must be a positive number.")

        # Fail fast on a missing driver, before spawning any thread or
        # touching the network - see DBMonitorMissingDriverError's docstring.
        _check_driver_installed(probe)

        self.probe = probe
        self.connection_params = connection_params
        self.interval = float(interval)
        self.connect_timeout_seconds = float(connect_timeout_seconds)

        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None
        self.samples: List[Dict[str, Any]] = []

    def start(self) -> None:
        if self.running:
            print("DB monitoring is already running; start() ignored.")
            return

        self._reset()
        self.running = True

        self.thread = threading.Thread(target=self._monitor, daemon=True)
        self.thread.start()

        print(f"DB monitoring started for {self.probe.display_name} at {self.connection_params.host}.")

    def stop(self) -> None:
        """Stop monitoring and wait for the background thread to actually
        finish - same reasoning as RuntimeMonitor.stop(): a single sample
        (connect included, on the first iteration) can take up to
        connect_timeout_seconds, so the join timeout has to cover that
        worst case, not just one normal interval, or the thread can still
        be running - and still holding/using the DB connection - after
        stop() returns and the caller starts the next run."""
        self.running = False

        if self.thread and self.thread.is_alive():
            join_timeout = max(self.interval, self.connect_timeout_seconds) + 2
            self.thread.join(timeout=join_timeout)

            if self.thread.is_alive():
                print(
                    f"Warning: DB monitoring thread did not stop within {join_timeout}s - it may "
                    f"still be running in the background. Metrics from this point may be unreliable."
                )
            self.thread = None

        print("DB monitoring stopped.")

    def _monitor(self) -> None:
        try:
            conn = self.probe.connect(self.connection_params, self.connect_timeout_seconds)
        except Exception as e:
            message = f"DB monitoring failed to connect to {self.probe.display_name}: {e}"
            print(message)
            self.error = message
            self.running = False
            return

        try:
            while self.running:
                try:
                    sample = self.probe.sample(conn)
                    sample["timestamp"] = time.time()
                    self.samples.append(sample)

                except Exception as e:
                    # One bad sample (a transient network blip, a query
                    # that briefly failed) shouldn't discard everything
                    # collected so far - same philosophy as
                    # runtime_monitor.py's monitor loop.
                    print(f"DB monitoring sample failed: {e}")
                    self.error = f"DB monitoring sample failed: {e}"

                time.sleep(self.interval)
        finally:
            # Closed on the same thread that created it - most DB client
            # connections aren't safe to close from a different thread.
            try:
                self.probe.close(conn)
            except Exception:
                pass  # best-effort cleanup; a close failure here doesn't invalidate already-collected samples

        self.running = False

    def summary(self) -> Dict[str, Any]:
        """
        Return aggregated connection/pool metrics.

        "monitoring_error" is set if something went wrong during
        collection (connect failure, a query repeatedly failing) - check
        this before trusting a run with suspiciously few samples, since
        "genuinely idle database" and "monitoring never actually worked"
        can look similar in the aggregated numbers alone.
        """
        active_values = [s["active_connections"] for s in self.samples if s.get("active_connections") is not None]
        utilization_values = [
            s["connection_pool_utilization_pct"] for s in self.samples
            if s.get("connection_pool_utilization_pct") is not None
        ]
        max_connections = next(
            (s["max_connections"] for s in reversed(self.samples) if s.get("max_connections") is not None), None
        )
        latest_extra = self.samples[-1]["extra"] if self.samples else {}

        return {
            "engine": self.probe.key,
            "active_connections_avg": self._average(active_values),
            "active_connections_peak": self._maximum(active_values),
            "max_connections": max_connections,
            "connection_pool_utilization_avg_pct": self._average(utilization_values),
            "connection_pool_utilization_peak_pct": self._maximum(utilization_values),
            "latest_extra": latest_extra,
            "samples": self.samples,
            "sample_count": len(self.samples),
            "monitoring_interval": self.interval,
            "monitoring_error": self.error,
        }

    @staticmethod
    def _average(values: List[float]) -> float:
        if not values:
            return 0.0
        return round(sum(values) / len(values), 2)

    @staticmethod
    def _maximum(values: List[float]) -> float:
        if not values:
            return 0.0
        return round(max(values), 2)

    def _reset(self) -> None:
        self.samples.clear()
        self.error = None