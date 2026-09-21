from __future__ import annotations

import os
from typing import Any, Dict, Optional

import pandas as pd


# ==========================================================================
# Framework-agnostic by construction
# ==========================================================================
#
# This parses Locust's OWN standard --csv output format (Name, Request
# Count, Failure Count, Requests/s, response-time percentiles, etc.) -
# columns Locust itself defines, completely independent of what
# framework the tested application uses. Nothing here needed to change
# for the Flask -> multi-framework generalization; it's already
# framework-agnostic because it was never framework-specific to begin
# with. Verified this rather than assumed it: every consumer field
# locust_runner.py depends on (requests, failures, throughput,
# failure_rate, average/median/min/max response time, p95, p99,
# average_content_size) is unchanged below.


class CSVParsingError(ValueError):
    """Base exception for Locust CSV parsing/validation failures."""


class CSVNotFoundError(CSVParsingError, FileNotFoundError):
    """The expected Locust results CSV file doesn't exist. Also a
    FileNotFoundError, so existing `except FileNotFoundError` callers
    still work unchanged."""


class CSVReadError(CSVParsingError, RuntimeError):
    """The CSV file exists but couldn't be read/parsed as valid CSV.
    Also a RuntimeError, for the same backward-compatibility reason."""


class CSVParser:
    """
    Parses Locust CSV statistics and returns normalized performance
    metrics for the analysis pipeline.

    Extracted metrics: request count, failure count, throughput, error
    rate, average/median/min/max response time, P95/P99 response time,
    and average content size.

    Note: the standard Locust *_stats.csv file does not provide HTTP
    2xx/4xx/5xx status-code counts, so this parser does not fabricate
    those (see locust_generator.py's status_counts.json for that data).
    """

    def parse(self, project_path: str, csv_prefix: Optional[str] = None) -> Dict[str, Any]:
        """
        Parse a Locust statistics CSV file into normalized performance metrics.

        Args:
            project_path: path to the analyzed project. Used to build the
                default results_stats.csv location, and as the base
                directory for csv_prefix if csv_prefix is a relative path.
            csv_prefix: the prefix used with Locust's --csv option. May be
                absolute (as locust_runner.py passes it - already
                pre-joined with project_path) or a bare relative prefix
                (resolved against project_path here). Either way, the
                actual file read is "<csv_prefix>_stats.csv".

        Raises:
            CSVNotFoundError: the results file doesn't exist (also a FileNotFoundError).
            CSVReadError: the file exists but isn't readable as CSV (also a RuntimeError).
            CSVParsingError: the CSV was read but its data is missing/invalid (also a ValueError).
        """
        csv_file = self._get_csv_path(project_path, csv_prefix)

        if not os.path.isfile(csv_file):
            raise CSVNotFoundError(f"Locust results file not found: {csv_file}")

        try:
            df = pd.read_csv(csv_file)
        except Exception as e:
            raise CSVReadError(f"Failed to read Locust CSV: {e}") from e

        required_columns = [
            "Name",
            "Request Count",
            "Failure Count",
            "Average Response Time",
            "Median Response Time",
            "Min Response Time",
            "Max Response Time",
            "Requests/s",
            "95%",
            "99%",
            "Average Content Size",
        ]

        missing_columns = [column for column in required_columns if column not in df.columns]
        if missing_columns:
            raise CSVParsingError("Locust CSV is missing required columns: " + ", ".join(missing_columns))

        if df.empty:
            raise CSVParsingError("Locust CSV contains no performance data.")

        aggregated = df[df["Name"].astype(str).str.strip().str.lower() == "aggregated"]
        if aggregated.empty:
            raise CSVParsingError("Aggregated Locust statistics were not found in the results CSV.")

        if len(aggregated) > 1:
            print(f"Warning: {len(aggregated)} rows matched 'Aggregated' in the Locust CSV; using the first.")

        row = aggregated.iloc[0]

        requests = self._to_int(row["Request Count"], "Request Count")
        failures = self._to_int(row["Failure Count"], "Failure Count")

        if requests < 0:
            raise CSVParsingError("Request Count cannot be negative.")

        if failures < 0:
            raise CSVParsingError("Failure Count cannot be negative.")

        if failures > requests:
            raise CSVParsingError("Failure Count cannot exceed Request Count.")

        if requests == 0:
            raise CSVParsingError(
                "Locust test produced zero requests. The application may be unreachable "
                "or the load test may not have executed correctly."
            )

        failure_rate = (failures / requests) * 100.0

        throughput = self._to_float(row["Requests/s"], "Requests/s")
        average_response_time = self._to_float(row["Average Response Time"], "Average Response Time")
        median_response_time = self._to_float(row["Median Response Time"], "Median Response Time")
        min_response_time = self._to_float(row["Min Response Time"], "Min Response Time")
        max_response_time = self._to_float(row["Max Response Time"], "Max Response Time")
        p95 = self._to_float(row["95%"], "95% response time")
        p99 = self._to_float(row["99%"], "99% response time")
        average_content_size = self._to_float(row["Average Content Size"], "Average Content Size")

        response_times = {
            "Average Response Time": average_response_time,
            "Median Response Time": median_response_time,
            "Min Response Time": min_response_time,
            "Max Response Time": max_response_time,
            "P95": p95,
            "P99": p99,
        }

        if min_response_time > max_response_time:
            raise CSVParsingError("Min Response Time cannot exceed Max Response Time.")

        if p95 > p99:
            raise CSVParsingError("P95 response time cannot exceed P99 response time.")

        if p99 > max_response_time:
            raise CSVParsingError("P99 response time cannot exceed maximum response time.")

        for name, value in response_times.items():
            if value < 0:
                raise CSVParsingError(f"{name} cannot be negative.")

        if throughput < 0:
            raise CSVParsingError("Throughput cannot be negative.")

        if average_content_size < 0:
            raise CSVParsingError("Average content size cannot be negative.")

        return {
            "requests": requests,
            "failures": failures,
            "throughput": round(throughput, 4),
            "failure_rate": round(failure_rate, 4),
            "average_response_time": round(average_response_time, 4),
            "median_response_time": round(median_response_time, 4),
            "min_response_time": round(min_response_time, 4),
            "max_response_time": round(max_response_time, 4),
            "p95": round(p95, 4),
            "p99": round(p99, 4),
            "average_content_size": round(average_content_size, 4),
        }

    @staticmethod
    def _get_csv_path(project_path: str, csv_prefix: Optional[str]) -> str:
        if csv_prefix:
            # locust_runner.py always passes an already-absolute csv_prefix
            # (pre-joined with project_path), so this branch is a no-op for
            # that caller. Resolving a relative csv_prefix against
            # project_path (instead of the process's CWD) makes this work
            # correctly for any other caller passing a bare prefix too,
            # rather than silently depending on one implicit convention.
            if os.path.isabs(csv_prefix):
                return f"{csv_prefix}_stats.csv"
            return os.path.join(project_path, f"{csv_prefix}_stats.csv")

        return os.path.join(project_path, "results_stats.csv")

    @staticmethod
    def _to_int(value: Any, field_name: str) -> int:
        try:
            if pd.isna(value):
                raise ValueError

            numeric_value = float(value)

            if numeric_value in (float("inf"), float("-inf")):
                raise ValueError

            if not numeric_value.is_integer():
                raise ValueError

            return int(numeric_value)

        except (TypeError, ValueError):
            raise CSVParsingError(f"Invalid numeric value for {field_name}: {value}")

    @staticmethod
    def _to_float(value: Any, field_name: str) -> float:
        try:
            if pd.isna(value):
                raise ValueError

            numeric_value = float(value)

            # A CSV cell containing the literal text "nan" passes pd.isna()
            # (it's a valid non-null string) but float("nan") still parses
            # it into an actual NaN - this catches that case too.
            if not pd.notna(numeric_value):
                raise ValueError

            if numeric_value in (float("inf"), float("-inf")):
                raise ValueError

            return numeric_value

        except (TypeError, ValueError):
            raise CSVParsingError(f"Invalid numeric value for {field_name}: {value}")


def parse_locust_csv(project_path: str, csv_prefix: Optional[str] = None) -> Dict[str, Any]:
    """One-shot: construct a CSVParser and run parse()."""
    return CSVParser().parse(project_path, csv_prefix=csv_prefix)