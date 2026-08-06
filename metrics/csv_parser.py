import os
import pandas as pd


class CSVParser:

    def parse(self, project_path):

        csv_file = os.path.join(
            project_path,
            "results_stats.csv"
        )

        if not os.path.exists(csv_file):
            raise FileNotFoundError(
                f"Results file not found: {csv_file}"
            )

        df = pd.read_csv(csv_file)

        # -----------------------------
        # Find the Aggregated row
        # -----------------------------

        aggregated = df[df["Name"] == "Aggregated"]

        if aggregated.empty:
            aggregated = df.tail(1)

        row = aggregated.iloc[0]

        # -----------------------------
        # Basic Metrics
        # -----------------------------

        requests = int(row["Request Count"])

        failures = int(row["Failure Count"])

        # -----------------------------
        # Failure Rate (%)
        # -----------------------------

        failure_rate = (
            (failures / requests) * 100
            if requests > 0
            else 0
        )

        # -----------------------------
        # Return Metrics Dictionary
        # -----------------------------

        return {

            "requests": requests,

            "failures": failures,

            "average_response_time": float(row["Average Response Time"]),

            "median_response_time": float(row["Median Response Time"]),

            "min_response_time": float(row["Min Response Time"]),

            "max_response_time": float(row["Max Response Time"]),

            "throughput": float(row["Requests/s"]),

            "failure_rate": round(failure_rate, 2),

            "p95": float(row["95%"]),

            "p99": float(row["99%"]),

            "average_content_size": float(row["Average Content Size"])
        }