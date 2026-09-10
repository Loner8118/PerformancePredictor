from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from metrics.csv_parser import CSVParser
from metrics.runtime_monitor import RuntimeMonitor

SERVICE_RATE_SOURCE = "estimated_from_lowest_load_response_time"


class LocustRunnerError(RuntimeError):
    """Base exception for Locust-runner-specific failures."""


class LocustNotInstalledError(LocustRunnerError):
    """The `locust` executable could not be found on PATH."""


class LocustConfigurationError(LocustRunnerError, ValueError):
    """Invalid run() configuration - also a ValueError, so existing
    `except ValueError` callers keep working unchanged."""


# ==========================================================================
# Framework-agnostic by construction
# ==========================================================================
#
# This orchestrates Locust CLI runs and aggregates their results - it
# never inspects the target application at all, just the locustfile.py a
# prior pipeline stage (locust_generator.py) already wrote, and Locust's
# own CSV output (parsed by csv_parser.py) plus Docker container stats
# (via runtime_monitor.py). Nothing here needed to change for the
# Flask -> multi-framework generalization; verified rather than assumed,
# since it was already framework-agnostic before this pass.
#
# What DID need fixing: the Locust subprocess call had no timeout at
# all. Locust's own -t flag is supposed to make it self-terminate, but
# nothing forced that if Locust got stuck (unreachable host, some retry
# loop, or any other reason it doesn't exit cleanly) - a single stuck run
# would hang the entire pipeline indefinitely, the same class of bug
# already fixed in github_manager.py's git clone and docker_manager.py's
# docker build/run. See _execute_locust_test() below.


_RUN_TIME_UNIT_PATTERN = re.compile(r"(\d+)\s*([hms])", re.IGNORECASE)
_RUN_TIME_UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}


def _parse_locust_run_time(run_time: Any) -> float:
    """
    Parse Locust's -t duration string (e.g. '15s', '2m', '1h30m') into
    seconds. Also accepts a bare number of seconds (int/float, or a
    numeric string with no unit), matching what Locust itself accepts.
    """
    if isinstance(run_time, bool):
        raise LocustConfigurationError(f"Invalid run_time: {run_time!r}")
    if isinstance(run_time, (int, float)):
        if run_time <= 0:
            raise LocustConfigurationError("run_time must be greater than zero.")
        return float(run_time)

    text = str(run_time).strip()

    if text.startswith("-"):
        raise LocustConfigurationError(f"run_time must be positive, got {run_time!r}.")

    matches = _RUN_TIME_UNIT_PATTERN.findall(text)

    if matches:
        total = sum(int(value) * _RUN_TIME_UNIT_SECONDS[unit.lower()] for value, unit in matches)
        if total <= 0:
            raise LocustConfigurationError(f"run_time must be greater than zero: {run_time!r}")
        return float(total)

    try:
        seconds = float(text)
    except (TypeError, ValueError):
        raise LocustConfigurationError(
            f"Could not parse run_time {run_time!r} - expected a format like '15s', '2m', '1h30m', or a bare number of seconds."
        )
    if seconds <= 0:
        raise LocustConfigurationError("run_time must be greater than zero.")
    return seconds


class LocustRunner:
    """
    Executes controlled Locust load tests against the analyzed application
    and collects performance and runtime metrics.

    Tests multiple concurrent-user levels, repeating each level for
    statistical reliability, monitors Docker runtime resources during each
    run, parses the Locust CSV output, and aggregates repetitions for the
    capacity-modeling stage.
    """

    DEFAULT_USER_LEVELS = [20, 50, 100, 200, 300, 500]
    DEFAULT_REPETITIONS = 3
    DEFAULT_RUN_TIME = "15s"
    DEFAULT_SPAWN_RATE = 10
    DEFAULT_SUBPROCESS_TIMEOUT_BUFFER_SECONDS = 60

    def __init__(
        self,
        host: str = "http://localhost:5000",
        repetitions: int = DEFAULT_REPETITIONS,
        run_time: str = DEFAULT_RUN_TIME,
        spawn_rate: int = DEFAULT_SPAWN_RATE,
        subprocess_timeout_buffer_seconds: float = DEFAULT_SUBPROCESS_TIMEOUT_BUFFER_SECONDS,
    ) -> None:
        self.host = host
        self.repetitions = repetitions
        self.run_time = run_time
        self.spawn_rate = spawn_rate
        self.subprocess_timeout_buffer_seconds = subprocess_timeout_buffer_seconds

        self.parser = CSVParser()
        self.runtime_monitor = RuntimeMonitor()

    def run(
        self,
        project_path: str,
        container_id: str,
        user_levels: Optional[Sequence[int]] = None,
        repetitions: Optional[int] = None,
        spawn_rate: Optional[int] = None,
        run_time: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Execute the complete load-testing experiment.

        Returns a dict with success, message, configuration, service_capacity,
        runs and levels.
        """

        if user_levels is None:
            user_levels = list(self.DEFAULT_USER_LEVELS)
        if repetitions is None:
            repetitions = self.repetitions
        if spawn_rate is None:
            spawn_rate = self.spawn_rate
        if run_time is None:
            run_time = self.run_time

        self._validate_configuration(
            project_path=project_path,
            user_levels=user_levels,
            repetitions=repetitions,
            spawn_rate=spawn_rate,
            run_time=run_time,
        )
        run_time_seconds = _parse_locust_run_time(run_time)

        locust_file = os.path.join(project_path, "locustfile.py")
        if not os.path.isfile(locust_file):
            raise FileNotFoundError(f"locustfile.py not found: {locust_file}")

        all_runs: List[Dict[str, Any]] = []
        level_results: List[Dict[str, Any]] = []
        total_runs = len(user_levels) * repetitions

        print("\n" + "=" * 60)
        print("STARTING LOCUST LOAD TEST")
        print("=" * 60)
        print(f"Host          : {self.host}")
        print(f"User Levels   : {user_levels}")
        print(f"Repetitions   : {repetitions}")
        print(f"Spawn Rate    : {spawn_rate} users/sec")
        print(f"Run Time      : {run_time}")
        print(f"Total Runs    : {total_runs}")

        run_number = 0

        for users in user_levels:
            print("\n" + "-" * 60)
            print(f"LOAD LEVEL: {users} USERS")
            print("-" * 60)

            level_runs = []

            for repetition in range(1, repetitions + 1):
                run_number += 1
                run_id = f"{users}users_run{repetition}_{uuid.uuid4().hex[:8]}"
                timestamp = datetime.now().isoformat(timespec="seconds")

                print(
                    f"\nRun {run_number}/{total_runs} "
                    f"| Users: {users} "
                    f"| Repetition: {repetition}/{repetitions}"
                )

                results_dir = os.path.join(project_path, "locust_results")
                os.makedirs(results_dir, exist_ok=True)

                # Locust writes <prefix>_stats.csv, _failures.csv, _exceptions.csv
                csv_prefix = os.path.join(results_dir, run_id)

                self._clean_result_files(csv_prefix)
                self._reset_status_counts(project_path)

                result = self._execute_locust_test(
                    locust_file=locust_file,
                    users=users,
                    spawn_rate=spawn_rate,
                    run_time=run_time,
                    run_time_seconds=run_time_seconds,
                    csv_prefix=csv_prefix,
                    container_id=container_id,
                )

                if result.returncode != 0:
                    print("\nLocust execution failed.")
                    print(result.stderr)
                    return {
                        "success": False,
                        "message": f"Locust failed at {users} users, repetition {repetition}.",
                        "runs": all_runs,
                        "levels": level_results,
                    }

                try:
                    performance_metrics = self.parser.parse(project_path, csv_prefix=csv_prefix)
                except Exception as e:
                    return {
                        "success": False,
                        "message": (
                            f"Failed to parse Locust results for {users} users, "
                            f"repetition {repetition}: {e}"
                        ),
                        "runs": all_runs,
                        "levels": level_results,
                    }

                runtime_metrics = self.runtime_monitor.summary(
                    performance_metrics=performance_metrics,
                    current_users=users,
                )

                if runtime_metrics.get("monitoring_error"):
                    print(f"Warning: runtime monitoring reported an issue for this run: {runtime_metrics['monitoring_error']}")

                status_counts = self._read_status_counts(project_path)
                self._save_run_status_counts(
                    project_path=project_path,
                    run_id=run_id,
                    status_counts=status_counts,
                )

                status_total = (
                    status_counts.get("status_2xx", 0)
                    + status_counts.get("status_3xx", 0)
                    + status_counts.get("status_4xx", 0)
                    + status_counts.get("status_5xx", 0)
                )
                locust_requests = performance_metrics.get("requests", 0)
                status_difference = status_total - locust_requests
                unclassified = max(0, locust_requests - status_total)
                status_match = status_difference == 0

                status_validation = {
                    "status_total": status_total,
                    "status_match": status_match,
                    "status_difference": status_difference,
                    "unclassified": unclassified,
                }

                if status_match:
                    print(f"HTTP status counts verified: {status_total}/{locust_requests}")
                else:
                    print("WARNING: HTTP status count mismatch!")
                    print(f"Locust requests : {locust_requests}")
                    print(f"2xx             : {status_counts.get('status_2xx', 0)}")
                    print(f"3xx             : {status_counts.get('status_3xx', 0)}")
                    print(f"4xx             : {status_counts.get('status_4xx', 0)}")
                    print(f"5xx             : {status_counts.get('status_5xx', 0)}")
                    print(f"Status total    : {status_total}")
                    print(f"Difference      : {status_difference:+d}")
                    print(
                        "Note: HTTP status event counts are diagnostic "
                        "and are not used as the authoritative request count."
                    )

                run_data = self._build_run_data(
                    run_id=run_id,
                    timestamp=timestamp,
                    users=users,
                    repetition=repetition,
                    spawn_rate=spawn_rate,
                    run_time=run_time,
                    performance_metrics=performance_metrics,
                    runtime_metrics=runtime_metrics,
                    status_counts=status_counts,
                    status_validation=status_validation,
                )

                all_runs.append(run_data)
                level_runs.append(run_data)
                self._print_run_summary(run_data)

            aggregated = self._aggregate_level(users, level_runs)
            level_results.append(aggregated)
            self._print_level_summary(aggregated)

        service_time, service_rate = self._estimate_service_time(level_results)
        for level in level_results:
            level["service_time"] = service_time
            level["service_rate"] = service_rate
            level["service_rate_source"] = SERVICE_RATE_SOURCE

        print("\n" + "=" * 60)
        print("SERVICE CAPACITY ESTIMATION")
        print("=" * 60)
        if service_rate > 0:
            print(f"Baseline Service Time : {service_time:.6f} sec")
            print(f"Estimated Service Rate : {service_rate:.2f} req/sec")
        else:
            print("Service rate could not be estimated.")

        return {
            "success": True,
            "message": "Locust load testing completed successfully.",
            "configuration": {
                "host": self.host,
                "user_levels": user_levels,
                "repetitions": repetitions,
                "spawn_rate": spawn_rate,
                "run_time": run_time,
                "total_runs": total_runs,
            },
            "service_capacity": {
                "service_time": service_time,
                "service_rate": service_rate,
                "service_time_unit": "seconds",
                "service_rate_unit": "requests/sec",
                "source": SERVICE_RATE_SOURCE,
            },
            "runs": all_runs,
            "levels": level_results,
        }

    def _execute_locust_test(
        self,
        locust_file: str,
        users: int,
        spawn_rate: int,
        run_time: str,
        run_time_seconds: float,
        csv_prefix: str,
        container_id: str,
    ) -> subprocess.CompletedProcess:
        """
        Start Docker runtime monitoring and execute one Locust test, with a
        hard subprocess timeout.

        The timeout has to cover more than just run_time: Locust's -t clock
        starts roughly when the test begins, but ramping up to `users`
        concurrent users at `spawn_rate` users/sec can itself take
        users/spawn_rate seconds - for a large user count and a modest
        spawn rate, that ramp-up can be longer than run_time itself. This
        deliberately adds both together as a generous upper bound rather
        than trying to model exactly how much they overlap - being
        generous here is the safe direction to be wrong in; too tight a
        timeout would kill legitimate slow-to-spawn runs.
        """
        spawn_time_seconds = (users / spawn_rate) if spawn_rate > 0 else 0.0
        subprocess_timeout = run_time_seconds + spawn_time_seconds + self.subprocess_timeout_buffer_seconds

        command = [
            "locust",
            "-f", locust_file,
            f"--host={self.host}",
            "--headless",
            "-u", str(users),
            "-r", str(spawn_rate),
            "-t", str(run_time),
            "--csv", csv_prefix,
            "--only-summary",
        ]

        try:
            self.runtime_monitor.start(container_id)

            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    timeout=subprocess_timeout,
                )
                return result

            except subprocess.TimeoutExpired as e:
                # subprocess.run already killed the process before raising
                # this - just report it in a shape the caller's existing
                # `if result.returncode != 0` handling already knows how
                # to deal with, so nothing else needs to change downstream.
                partial_stderr = (e.stderr or "")
                if isinstance(partial_stderr, bytes):
                    partial_stderr = partial_stderr.decode("utf-8", errors="ignore")
                partial_stdout = (e.stdout or "")
                if isinstance(partial_stdout, bytes):
                    partial_stdout = partial_stdout.decode("utf-8", errors="ignore")

                return subprocess.CompletedProcess(
                    args=command,
                    returncode=-1,
                    stdout=partial_stdout,
                    stderr=(
                        partial_stderr
                        + f"\nLocust did not complete within {subprocess_timeout:.0f}s "
                        f"(run_time={run_time}, users={users}, spawn_rate={spawn_rate}) and was terminated."
                    ).strip(),
                )

        except FileNotFoundError:
            raise LocustNotInstalledError(
                "Locust was not found. Make sure Locust is installed and available in PATH."
            )
        finally:
            # Stop monitoring even if Locust fails, so it doesn't leak into the next run
            self.runtime_monitor.stop()

    @staticmethod
    def _build_run_data(
        run_id: str,
        timestamp: str,
        users: int,
        repetition: int,
        spawn_rate: int,
        run_time: str,
        performance_metrics: Dict[str, Any],
        runtime_metrics: Dict[str, Any],
        status_counts: Dict[str, Any],
        status_validation: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Combine Locust and Docker runtime metrics into one experiment record."""
        return {
            "run_id": run_id,
            "timestamp": timestamp,
            "users": users,
            "repetition": repetition,
            "spawn_rate": spawn_rate,
            "test_duration": run_time,

            "status_total": status_validation.get("status_total", 0),
            "status_match": status_validation.get("status_match", False),
            "status_unclassified": status_validation.get("unclassified", 0),
            "status_difference": status_validation.get("status_difference", 0),

            "requests": performance_metrics.get("requests", 0),
            "failures": performance_metrics.get("failures", 0),
            "throughput": performance_metrics.get("throughput", 0.0),
            "arrival_rate": performance_metrics.get("throughput", 0.0),
            "error_rate": performance_metrics.get("failure_rate", 0.0),

            "average_response_time": performance_metrics.get("average_response_time", 0.0),
            "median_response_time": performance_metrics.get("median_response_time", 0.0),
            "p95": performance_metrics.get("p95", 0.0),
            "p99": performance_metrics.get("p99", 0.0),
            "min_response_time": performance_metrics.get("min_response_time", 0.0),
            "max_response_time": performance_metrics.get("max_response_time", 0.0),

            "average_content_size": performance_metrics.get("average_content_size", 0.0),

            "status_2xx": status_counts.get("status_2xx", 0),
            "status_3xx": status_counts.get("status_3xx", 0),
            "status_4xx": status_counts.get("status_4xx", 0),
            "status_5xx": status_counts.get("status_5xx", 0),

            "cpu_usage": runtime_metrics.get("cpu_avg", 0.0),
            "cpu_peak": runtime_metrics.get("cpu_peak", 0.0),

            "memory_usage": runtime_metrics.get("memory_avg", 0.0),
            "memory_peak": runtime_metrics.get("memory_peak", 0.0),

            "disk_read_mb_s": runtime_metrics.get("disk_read_mb_s", 0.0),
            "disk_write_mb_s": runtime_metrics.get("disk_write_mb_s", 0.0),

            "network_rx_mb_s": runtime_metrics.get("network_rx_mb_s", 0.0),
            "network_tx_mb_s": runtime_metrics.get("network_tx_mb_s", 0.0),

            # New: surfaces RuntimeMonitor's failure diagnostics (added in
            # runtime_monitor.py's own finalization) all the way through to
            # the final report - previously computed but silently dropped.
            "monitoring_error": runtime_metrics.get("monitoring_error"),
        }

    @staticmethod
    def _read_status_counts(project_path: str) -> Dict[str, int]:
        """Read the HTTP status counts written by the generated locustfile."""
        status_file = os.path.join(project_path, "status_counts.json")
        default_counts = {
            "status_2xx": 0,
            "status_3xx": 0,
            "status_4xx": 0,
            "status_5xx": 0,
        }

        if not os.path.exists(status_file):
            print("Warning: status_counts.json not found.")
            return default_counts

        try:
            with open(status_file, "r", encoding="utf-8") as file:
                data = json.load(file)

            return {
                "status_2xx": int(data.get("status_2xx", 0)),
                "status_3xx": int(data.get("status_3xx", 0)),
                "status_4xx": int(data.get("status_4xx", 0)),
                "status_5xx": int(data.get("status_5xx", 0)),
            }

        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            print("Warning: Could not read status_counts.json.")
            return default_counts

    @staticmethod
    def _save_run_status_counts(project_path: str, run_id: str, status_counts: Dict[str, Any]) -> None:
        results_dir = os.path.join(project_path, "locust_results")
        os.makedirs(results_dir, exist_ok=True)
        status_file = os.path.join(results_dir, f"{run_id}_status.json")

        try:
            with open(status_file, "w", encoding="utf-8") as file:
                json.dump(status_counts, file, indent=2)
        except OSError as e:
            print(f"Warning: Could not save run status counts: {e}")

    @staticmethod
    def _estimate_service_time(level_results: List[Dict[str, Any]]) -> Tuple[float, float]:
        """
        Estimate effective service capacity from observed throughput.

        The service rate is taken from the load level with the highest
        observed mean throughput, treated as the empirical maximum
        processing capacity reached during the test. This is an
        effective application-level service capacity, not a physical
        CPU service time. Returns (service_time_seconds, service_rate_req_per_sec).
        """

        if not level_results:
            return 0.0, 0.0

        valid_levels = [
            level for level in level_results
            if float(level.get("mean_throughput", 0.0)) > 0
        ]
        if not valid_levels:
            return 0.0, 0.0

        peak_level = max(valid_levels, key=lambda level: float(level.get("mean_throughput", 0.0)))
        service_rate = float(peak_level.get("mean_throughput", 0.0))
        if service_rate <= 0:
            return 0.0, 0.0

        service_time_seconds = 1.0 / service_rate  # S = 1 / μ
        return round(service_time_seconds, 6), round(service_rate, 4)

    @staticmethod
    def _print_run_summary(run_data: Dict[str, Any]) -> None:
        print(f"Requests       : {run_data['requests']}")
        print(f"Throughput     : {run_data['throughput']:.2f} req/sec")
        print(f"Avg Response   : {run_data['average_response_time']:.2f} ms")
        print(f"P95            : {run_data['p95']:.2f} ms")
        print(f"P99            : {run_data['p99']:.2f} ms")
        print(f"Error Rate     : {run_data['error_rate']:.2f}%")
        print(f"CPU            : {run_data['cpu_usage']:.2f}%")
        print(f"Memory         : {run_data['memory_usage']:.2f}%")
        print(f"2xx            : {run_data['status_2xx']}")
        print(f"3xx            : {run_data['status_3xx']}")
        print(f"4xx            : {run_data['status_4xx']}")
        print(f"5xx            : {run_data['status_5xx']}")
        if run_data.get("monitoring_error"):
            print(f"Monitoring     : WARNING - {run_data['monitoring_error']}")

    @staticmethod
    def _print_level_summary(aggregated: Dict[str, Any]) -> None:
        print("\nLEVEL SUMMARY")
        print(f"Users              : {aggregated['users']}")
        print(f"Mean Throughput    : {aggregated['mean_throughput']:.2f} req/sec")
        print(f"Throughput Std Dev : {aggregated['std_throughput']:.2f}")
        print(f"Mean Response      : {aggregated['mean_average_response_time']:.2f} ms")
        print(f"Mean P95           : {aggregated['mean_p95']:.2f} ms")
        print(f"Mean Error Rate    : {aggregated['mean_error_rate']:.2f}%")
        print(f"Mean CPU           : {aggregated['mean_cpu_usage']:.2f}%")
        print(f"Mean Memory        : {aggregated['mean_memory_usage']:.2f}%")

    @staticmethod
    def _validate_configuration(
        project_path: str,
        user_levels: Sequence[int],
        repetitions: int,
        spawn_rate: int,
        run_time: str,
    ) -> None:
        if not os.path.isdir(project_path):
            raise FileNotFoundError(f"Project path does not exist: {project_path}")

        if repetitions < 1:
            raise LocustConfigurationError("Repetitions must be at least 1.")

        if not user_levels:
            raise LocustConfigurationError("At least one user level is required.")

        if any(not isinstance(users, int) or users <= 0 for users in user_levels):
            raise LocustConfigurationError("All user levels must be positive integers.")

        if spawn_rate <= 0:
            raise LocustConfigurationError("Spawn rate must be greater than 0.")

        if not run_time:
            raise LocustConfigurationError("Run time must be specified.")

        # Validated for its side effect here (fail fast with a clear message
        # before any Locust runs are attempted) - the parsed value itself is
        # recomputed by the caller, since this is a @staticmethod.
        _parse_locust_run_time(run_time)

    def _aggregate_level(self, users: int, runs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Aggregate repeated experiments performed at the same concurrent-user
        level. Mean, standard deviation, min and max are computed so the
        capacity-modeling stage receives stable measurements.
        """

        if not runs:
            return {"users": users, "repetitions": 0, "runs": []}

        throughputs = [run["throughput"] for run in runs]
        arrival_rates = [run["arrival_rate"] for run in runs]
        average_response_times = [run["average_response_time"] for run in runs]
        p95_values = [run["p95"] for run in runs]
        p99_values = [run["p99"] for run in runs]
        error_rates = [run["error_rate"] for run in runs]
        requests = [run["requests"] for run in runs]
        failures = [run["failures"] for run in runs]

        status_2xx = [run["status_2xx"] for run in runs]
        status_3xx = [run["status_3xx"] for run in runs]
        status_4xx = [run["status_4xx"] for run in runs]
        status_5xx = [run["status_5xx"] for run in runs]
        status_unclassified = [run["status_unclassified"] for run in runs]

        cpu_values = [run["cpu_usage"] for run in runs]
        cpu_peaks = [run["cpu_peak"] for run in runs]
        memory_values = [run["memory_usage"] for run in runs]
        memory_peaks = [run["memory_peak"] for run in runs]
        disk_read_values = [run["disk_read_mb_s"] for run in runs]
        disk_write_values = [run["disk_write_mb_s"] for run in runs]
        network_rx_values = [run["network_rx_mb_s"] for run in runs]
        network_tx_values = [run["network_tx_mb_s"] for run in runs]

        monitoring_errors = [run["monitoring_error"] for run in runs if run.get("monitoring_error")]

        return {
            "users": users,
            "repetitions": len(runs),

            "mean_throughput": self._mean(throughputs),
            "mean_arrival_rate": self._mean(arrival_rates),
            "std_throughput": self._std(throughputs),
            "min_throughput": self._min(throughputs),
            "max_throughput": self._max(throughputs),

            "mean_average_response_time": self._mean(average_response_times),
            "std_average_response_time": self._std(average_response_times),
            "mean_p95": self._mean(p95_values),
            "std_p95": self._std(p95_values),
            "mean_p99": self._mean(p99_values),
            "std_p99": self._std(p99_values),

            "mean_error_rate": self._mean(error_rates),
            "std_error_rate": self._std(error_rates),

            "mean_requests": self._mean(requests),
            "total_requests": sum(requests),
            "mean_failures": self._mean(failures),

            "mean_status_2xx": self._mean(status_2xx),
            "mean_status_3xx": self._mean(status_3xx),
            "mean_status_4xx": self._mean(status_4xx),
            "mean_status_5xx": self._mean(status_5xx),
            "mean_status_unclassified": self._mean(status_unclassified),

            "mean_cpu_usage": self._mean(cpu_values),
            "max_cpu_usage": self._max(cpu_peaks),

            "mean_memory_usage": self._mean(memory_values),
            "max_memory_usage": self._max(memory_peaks),

            "mean_disk_read_mb_s": self._mean(disk_read_values),
            "mean_disk_write_mb_s": self._mean(disk_write_values),

            "mean_network_rx_mb_s": self._mean(network_rx_values),
            "mean_network_tx_mb_s": self._mean(network_tx_values),

            "monitoring_error_count": len(monitoring_errors),

            "runs": runs,
        }

    @staticmethod
    def _mean(values: Sequence[float]) -> float:
        if not values:
            return 0.0
        return round(float(np.mean(values)), 4)

    @staticmethod
    def _std(values: Sequence[float]) -> float:
        """Sample standard deviation; zero when there's only one repetition."""
        if len(values) <= 1:
            return 0.0
        return round(float(np.std(values, ddof=1)), 4)

    @staticmethod
    def _min(values: Sequence[float]) -> float:
        if not values:
            return 0.0
        return round(float(np.min(values)), 4)

    @staticmethod
    def _max(values: Sequence[float]) -> float:
        if not values:
            return 0.0
        return round(float(np.max(values)), 4)

    @staticmethod
    def _reset_status_counts(project_path: str) -> None:
        status_file = os.path.join(project_path, "status_counts.json")
        default_counts = {
            "status_2xx": 0,
            "status_3xx": 0,
            "status_4xx": 0,
            "status_5xx": 0,
        }
        try:
            with open(status_file, "w", encoding="utf-8") as file:
                json.dump(default_counts, file)
        except OSError as e:
            print(f"Warning: Could not reset status_counts.json: {e}")

    @staticmethod
    def _clean_result_files(csv_prefix: str) -> None:
        """
        Remove old Locust result files for this CSV prefix, e.g.
        run123_stats.csv, run123_failures.csv, run123_exceptions.csv.
        """
        directory = os.path.dirname(csv_prefix)
        prefix = os.path.basename(csv_prefix)

        if not os.path.exists(directory):
            return

        for filename in os.listdir(directory):
            if not filename.startswith(prefix):
                continue

            path = os.path.join(directory, filename)
            try:
                if os.path.isfile(path):
                    os.remove(path)
                elif os.path.isdir(path):
                    shutil.rmtree(path)
            except OSError as e:
                print(f"Warning: Could not remove {path}: {e}")