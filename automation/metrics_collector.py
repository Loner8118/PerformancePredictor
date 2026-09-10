from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import psutil


class MetricsCollectorError(ValueError):
    """Bad input to MetricsCollector (invalid interval/duration/users/throughput)."""


# ==========================================================================
# What this class is, and isn't
# ==========================================================================
#
# Samples HOST-machine resource usage (CPU, memory, disk I/O, network I/O)
# via psutil - the whole machine this code runs on, not any specific
# Docker container. This is a fundamentally different measurement from
# RuntimeMonitor, which samples the ANALYZED CONTAINER's usage
# specifically via Docker's stats API. RuntimeMonitor is what the
# pipeline's actual capacity/scalability math needs (the container's own
# resource pressure under load), and that's the only one currently wired
# into locust_runner.py / app.py.
#
# As of this pass, MetricsCollector has no caller anywhere in the
# pipeline - it's currently unused. It's still finalized properly here
# since it may be worth wiring in for a real, different purpose: a
# pre-flight host-load check. If the machine running this whole tool is
# itself under heavy CPU/memory pressure before a load test even starts
# (from the Flask API server, Locust's own process, other unrelated
# load), that contention can bias RuntimeMonitor's container-level
# readings without it being obvious why. A short collect() call before
# starting Locust could catch that and surface a warning. That wiring
# decision is left for the app.py integration pass, not made here.


class MetricsCollector:
    """Samples host-level system metrics over a fixed collection window."""

    def __init__(self, interval: float = 1.0) -> None:
        if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
            raise MetricsCollectorError("interval must be a positive number.")
        self.interval = float(interval)

    def collect(self, users: Any = 0, throughput: Any = 0.0, duration: float = 5) -> Dict[str, Any]:
        """
        Sample host system metrics for `duration` seconds and return the
        aggregated result. Each sampling round uses psutil.cpu_percent()'s
        own blocking-interval mechanism to pace itself; the round's
        interval is capped at whatever time remains in `duration`, so a
        short duration (or an interval larger than duration) no longer
        overshoots by up to a full extra interval.
        """
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
            raise MetricsCollectorError("duration must be a positive number.")

        try:
            users_value = int(users)
        except (TypeError, ValueError):
            raise MetricsCollectorError(f"users must be numeric, got {users!r}.")

        try:
            throughput_value = float(throughput)
        except (TypeError, ValueError):
            raise MetricsCollectorError(f"throughput must be numeric, got {throughput!r}.")

        print("Collecting system runtime metrics...")

        cpu_values: List[float] = []
        memory_values: List[float] = []
        disk_read_rates: List[float] = []
        disk_write_rates: List[float] = []
        network_rx_rates: List[float] = []
        network_tx_rates: List[float] = []

        previous_disk, previous_disk_time = self._safe_disk_counters()
        previous_network, previous_network_time = self._safe_network_counters()

        start_time = time.time()

        while True:
            remaining = duration - (time.time() - start_time)
            if remaining <= 0:
                break
            sample_interval = min(self.interval, remaining)

            cpu = self._safe_cpu_percent(sample_interval)
            if cpu is not None:
                cpu_values.append(cpu)

            memory = self._safe_memory_percent()
            if memory is not None:
                memory_values.append(memory)

            current_disk, current_disk_time = self._safe_disk_counters()
            if current_disk is not None:
                if previous_disk is not None and previous_disk_time is not None:
                    elapsed = current_disk_time - previous_disk_time
                    if elapsed > 0:
                        read_delta = max(current_disk.read_bytes - previous_disk.read_bytes, 0)
                        write_delta = max(current_disk.write_bytes - previous_disk.write_bytes, 0)
                        disk_read_rates.append(read_delta / elapsed / 1024 / 1024)
                        disk_write_rates.append(write_delta / elapsed / 1024 / 1024)
                previous_disk, previous_disk_time = current_disk, current_disk_time
            # else: leave the previous baseline in place and try again next round -
            # a longer elapsed window on the next successful read still gives a
            # correct average rate, it just skips this one round's contribution.

            current_network, current_network_time = self._safe_network_counters()
            if current_network is not None:
                if previous_network is not None and previous_network_time is not None:
                    elapsed = current_network_time - previous_network_time
                    if elapsed > 0:
                        rx_delta = max(current_network.bytes_recv - previous_network.bytes_recv, 0)
                        tx_delta = max(current_network.bytes_sent - previous_network.bytes_sent, 0)
                        network_rx_rates.append(rx_delta / elapsed / 1024 / 1024)
                        network_tx_rates.append(tx_delta / elapsed / 1024 / 1024)
                previous_network, previous_network_time = current_network, current_network_time

        return {
            "current_users": users_value,
            "throughput": round(throughput_value, 2),

            "cpu_usage": self._average(cpu_values),
            "cpu_peak": self._maximum(cpu_values),

            "memory_usage": self._average(memory_values),
            "memory_peak": self._maximum(memory_values),

            "disk_read_mb_s": self._average(disk_read_rates),
            "disk_write_mb_s": self._average(disk_write_rates),
            "disk_read_peak_mb_s": self._maximum(disk_read_rates),
            "disk_write_peak_mb_s": self._maximum(disk_write_rates),

            "network_rx_mb_s": self._average(network_rx_rates),
            "network_tx_mb_s": self._average(network_tx_rates),
            "network_rx_peak_mb_s": self._maximum(network_rx_rates),
            "network_tx_peak_mb_s": self._maximum(network_tx_rates),

            "cpu_samples": len(cpu_values),
            "memory_samples": len(memory_values),
            "disk_samples": len(disk_read_rates),
            "network_samples": len(network_rx_rates),

            "collection_duration": round(time.time() - start_time, 2),
            "sampling_interval": self.interval,
        }

    # ------------------------------------------------------------------
    # Per-metric sampling, each independently resilient to a psutil
    # failure - one bad reading no longer aborts the whole collection
    # window and discards every sample gathered up to that point.
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_cpu_percent(interval: float) -> Optional[float]:
        try:
            return psutil.cpu_percent(interval=interval)
        except Exception as e:  # psutil's own exception hierarchy varies by platform
            print(f"Warning: CPU sampling failed: {e}")
            time.sleep(interval)  # preserve pacing even though the measurement itself failed
            return None

    @staticmethod
    def _safe_memory_percent() -> Optional[float]:
        try:
            return psutil.virtual_memory().percent
        except Exception as e:
            print(f"Warning: Memory sampling failed: {e}")
            return None

    @staticmethod
    def _safe_disk_counters():
        try:
            counters = psutil.disk_io_counters()
            if counters is None:
                return None, None
            return counters, time.time()
        except Exception as e:
            print(f"Warning: Disk I/O sampling failed: {e}")
            return None, None

    @staticmethod
    def _safe_network_counters():
        try:
            counters = psutil.net_io_counters()
            if counters is None:
                return None, None
            return counters, time.time()
        except Exception as e:
            print(f"Warning: Network I/O sampling failed: {e}")
            return None, None

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