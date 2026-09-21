from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import docker


class RuntimeMonitorError(RuntimeError):
    """Raised when RuntimeMonitor can't connect to Docker, or receives invalid configuration."""


# ==========================================================================
# What this class is, and why it needed no framework-related changes
# ==========================================================================
#
# Monitors a Docker CONTAINER's resource usage (CPU, memory, disk I/O,
# network I/O) via Docker's own stats API - entirely at the container
# level, completely independent of what framework is running inside it.
# Flask, FastAPI, Django, Express, Spring Boot all look identical to
# `docker stats` - it has no idea what's inside the container, and
# doesn't need to. Confirmed this rather than assumed it while reviewing
# the file: nothing here references routes, entry points, or any
# framework-specific concept.
#
# What DID need fixing: this instance is constructed once and reused
# across every load-test run in locust_runner.py's loop (start()/stop()
# called repeatedly across different user levels and repetitions) - so a
# background monitoring thread that doesn't actually finish when stop()
# returns isn't just a minor edge case, it's a real risk of two
# monitoring threads racing against the same metric lists on the very
# next start(). See stop()'s docstring below for the fix.


class RuntimeMonitor:
    """
    Monitors runtime resource usage (CPU, memory, disk I/O, network I/O)
    of a Docker container. Runs in a background thread so usage can be
    captured while Locust performs the load test.
    """

    DEFAULT_DOCKER_CLIENT_TIMEOUT_SECONDS = 30

    def __init__(
        self,
        interval: float = 1.0,
        docker_client_timeout_seconds: float = DEFAULT_DOCKER_CLIENT_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
            raise RuntimeMonitorError("interval must be a positive number.")
        if (
            not isinstance(docker_client_timeout_seconds, (int, float))
            or isinstance(docker_client_timeout_seconds, bool)
            or docker_client_timeout_seconds <= 0
        ):
            raise RuntimeMonitorError("docker_client_timeout_seconds must be a positive number.")

        self.interval = float(interval)
        self.docker_client_timeout_seconds = docker_client_timeout_seconds

        try:
            # timeout applies to every API call this client makes,
            # including container.stats() - without it, a hung Docker
            # daemon call could block the monitoring thread indefinitely
            # with no exception ever raised to trigger the existing
            # error-handling in monitor() below.
            self.client = docker.from_env(timeout=docker_client_timeout_seconds)
        except docker.errors.DockerException as e:
            raise RuntimeMonitorError(f"Could not connect to Docker: {e}") from e

        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None

        self.cpu: List[float] = []
        self.memory: List[float] = []
        self.disk_read: List[float] = []
        self.disk_write: List[float] = []
        self.network_rx: List[float] = []
        self.network_tx: List[float] = []

    def start(self, container_id: str) -> None:
        if self.running:
            print("Runtime monitoring is already running; start() ignored.")
            return

        self._reset_metrics()
        self.running = True

        self.thread = threading.Thread(target=self.monitor, args=(container_id,), daemon=True)
        self.thread.start()

        print(f"Runtime monitoring started for container: {container_id}")

    def stop(self) -> None:
        """
        Stop monitoring and wait for the monitoring thread to actually
        finish.

        The join timeout has to cover the worst case the thread could be
        blocked in, not just one normal sampling interval - a single
        container.stats() call can take up to docker_client_timeout_seconds
        if the Docker daemon is slow to respond, and stop() needs to wait
        at least that long before giving up, or the thread can still be
        running (and still appending to self.cpu/self.memory/etc.) after
        stop() returns and the caller starts the next run.
        """
        self.running = False

        if self.thread and self.thread.is_alive():
            join_timeout = max(self.interval, self.docker_client_timeout_seconds) + 2
            self.thread.join(timeout=join_timeout)

            if self.thread.is_alive():
                print(
                    f"Warning: monitoring thread did not stop within {join_timeout}s - it may "
                    f"still be running in the background. Metrics from this point may be unreliable."
                )
            self.thread = None

        print("Runtime monitoring stopped.")

    def monitor(self, container_id: str) -> None:
        try:
            container = self.client.containers.get(container_id)

        except docker.errors.NotFound:
            message = f"Runtime monitoring failed: container {container_id} not found."
            print(message)
            self.error = message
            self.running = False
            return

        except docker.errors.DockerException as e:
            message = f"Runtime monitoring failed to access Docker: {e}"
            print(message)
            self.error = message
            self.running = False
            return

        previous_stats = None
        previous_time = None

        while self.running:
            try:
                stats = container.stats(stream=False)
                current_time = time.time()

                if previous_stats is not None:
                    cpu_percent = self._calculate_cpu_percent(stats, previous_stats)
                    if cpu_percent is not None:
                        self.cpu.append(cpu_percent)

                memory_percent = self._calculate_memory_percent(stats)
                if memory_percent is not None:
                    self.memory.append(memory_percent)

                disk_read_bytes, disk_write_bytes = self._get_disk_bytes(stats)
                network_rx_bytes, network_tx_bytes = self._get_network_bytes(stats)

                if previous_stats is not None and previous_time is not None:
                    elapsed = current_time - previous_time

                    if elapsed > 0:
                        previous_read, previous_write = self._get_disk_bytes(previous_stats)
                        previous_rx, previous_tx = self._get_network_bytes(previous_stats)

                        disk_read_rate = max(disk_read_bytes - previous_read, 0) / elapsed
                        disk_write_rate = max(disk_write_bytes - previous_write, 0) / elapsed
                        network_rx_rate = max(network_rx_bytes - previous_rx, 0) / elapsed
                        network_tx_rate = max(network_tx_bytes - previous_tx, 0) / elapsed

                        self.disk_read.append(disk_read_rate / 1024 / 1024)
                        self.disk_write.append(disk_write_rate / 1024 / 1024)
                        self.network_rx.append(network_rx_rate / 1024 / 1024)
                        self.network_tx.append(network_tx_rate / 1024 / 1024)

                previous_stats = stats
                previous_time = current_time

                time.sleep(self.interval)

            except docker.errors.NotFound:
                message = "Runtime monitoring stopped: Docker container no longer exists."
                print(message)
                self.error = message
                break

            except docker.errors.APIError as e:
                print(f"Docker statistics error: {e}")
                self.error = f"Docker statistics error: {e}"
                time.sleep(self.interval)

            except Exception as e:
                print(f"Runtime monitoring error: {e}")
                self.error = f"Runtime monitoring error: {e}"
                time.sleep(self.interval)

        self.running = False

    # ------------------------------------------------------------------
    # Docker stats parsing
    #
    # Note on cgroup v1 vs v2: Docker's stats JSON shape differs somewhat
    # between hosts running the legacy cgroup v1 hierarchy and newer
    # cgroup v2 hosts (e.g. some field names/locations under blkio/memory
    # stats can differ). The methods below use .get(..., default) at every
    # level specifically so an unrecognized/missing field degrades to "no
    # data this sample" rather than raising - but that also means a host
    # where a given field genuinely lives somewhere else under cgroup v2
    # will silently under-report that one metric (e.g. disk I/O reading
    # as 0) rather than error loudly. This is a known limitation, not
    # something guessed at and "fixed" here without being able to verify
    # the exact cgroup v2 key layout against a real host.
    # ------------------------------------------------------------------

    @staticmethod
    def _calculate_cpu_percent(stats: Dict[str, Any], previous_stats: Dict[str, Any]) -> Optional[float]:
        """Calculate container CPU utilization percentage from two consecutive Docker stats samples."""
        try:
            cpu_stats = stats.get("cpu_stats", {})
            previous_cpu_stats = previous_stats.get("cpu_stats", {})

            cpu_usage = cpu_stats.get("cpu_usage", {})
            previous_cpu_usage = previous_cpu_stats.get("cpu_usage", {})

            total_usage = cpu_usage.get("total_usage", 0)
            previous_total_usage = previous_cpu_usage.get("total_usage", 0)

            system_usage = cpu_stats.get("system_cpu_usage", 0)
            previous_system_usage = previous_cpu_stats.get("system_cpu_usage", 0)

            cpu_delta = total_usage - previous_total_usage
            system_delta = system_usage - previous_system_usage
            online_cpus = cpu_stats.get("online_cpus", 1)

            if cpu_delta < 0 or system_delta <= 0 or online_cpus <= 0:
                return None

            cpu_percent = (cpu_delta / system_delta) * online_cpus * 100.0
            return round(max(cpu_percent, 0.0), 2)

        except (TypeError, KeyError, ZeroDivisionError):
            return None

    @staticmethod
    def _calculate_memory_percent(stats: Dict[str, Any]) -> Optional[float]:
        """Calculate container memory utilization percentage using the memory limit reported by Docker."""
        try:
            memory_stats = stats.get("memory_stats", {})
            usage = memory_stats.get("usage", 0)
            limit = memory_stats.get("limit", 0)

            if limit <= 0:
                return None

            # Docker's memory usage can include cache; exclude inactive_file when available.
            inactive_file = memory_stats.get("stats", {}).get("inactive_file", 0)
            if inactive_file:
                usage = max(usage - inactive_file, 0)

            memory_percent = (usage / limit) * 100.0
            return round(max(memory_percent, 0.0), 2)

        except (TypeError, KeyError, ZeroDivisionError):
            return None

    @staticmethod
    def _get_disk_bytes(stats: Dict[str, Any]) -> Tuple[int, int]:
        """Return cumulative disk read/write bytes from Docker stats."""
        read_bytes = 0
        write_bytes = 0

        entries = stats.get("blkio_stats", {}).get("io_service_bytes_recursive", []) or []

        for entry in entries:
            operation = str(entry.get("op", "")).lower()
            value = entry.get("value", 0)

            if operation == "read":
                read_bytes += value
            elif operation == "write":
                write_bytes += value

        return read_bytes, write_bytes

    @staticmethod
    def _get_network_bytes(stats: Dict[str, Any]) -> Tuple[int, int]:
        """Return cumulative network receive/transmit bytes."""
        rx_bytes = 0
        tx_bytes = 0

        networks = stats.get("networks", {}) or {}

        for interface in networks.values():
            rx_bytes += interface.get("rx_bytes", 0)
            tx_bytes += interface.get("tx_bytes", 0)

        return rx_bytes, tx_bytes

    def summary(self, performance_metrics: Optional[Dict[str, Any]] = None, current_users: int = 0) -> Dict[str, Any]:
        """
        Return aggregated runtime metrics. If Locust performance metrics
        are supplied, they're combined with the runtime measurements for
        the prediction engine.

        "monitoring_error" is set if something went wrong during
        collection (container not found, Docker unreachable, a stats
        call repeatedly failing) - check this before trusting a run with
        suspiciously low sample counts or all-zero metrics, since zero
        usage and "monitoring never actually worked" look identical in
        the aggregated numbers alone.
        """
        metrics: Dict[str, Any] = {
            "cpu_avg": self._average(self.cpu),
            "cpu_peak": self._maximum(self.cpu),
            "memory_avg": self._average(self.memory),
            "memory_peak": self._maximum(self.memory),
            "disk_read_mb_s": self._average(self.disk_read),
            "disk_write_mb_s": self._average(self.disk_write),
            "disk_read_peak_mb_s": self._maximum(self.disk_read),
            "disk_write_peak_mb_s": self._maximum(self.disk_write),
            "network_rx_mb_s": self._average(self.network_rx),
            "network_tx_mb_s": self._average(self.network_tx),
            "network_rx_peak_mb_s": self._maximum(self.network_rx),
            "network_tx_peak_mb_s": self._maximum(self.network_tx),
            "cpu_samples": len(self.cpu),
            "memory_samples": len(self.memory),
            "io_samples": len(self.disk_read),
            "network_samples": len(self.network_rx),
            "monitoring_interval": self.interval,
            "monitoring_error": self.error,
        }

        if performance_metrics:
            metrics.update({
                "current_users": current_users,
                "throughput": round(float(performance_metrics.get("throughput", 0)), 2),
                "average_response_time": round(float(performance_metrics.get("average_response_time", 0)), 2),
                "p95": round(float(performance_metrics.get("p95", 0)), 2),
                "p99": round(float(performance_metrics.get("p99", 0)), 2),
                "error_rate": round(
                    float(performance_metrics.get("failure_rate", performance_metrics.get("error_rate", 0))), 2
                ),
            })

        return metrics

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

    def _reset_metrics(self) -> None:
        self.cpu.clear()
        self.memory.clear()
        self.disk_read.clear()
        self.disk_write.clear()
        self.network_rx.clear()
        self.network_tx.clear()
        self.error = None