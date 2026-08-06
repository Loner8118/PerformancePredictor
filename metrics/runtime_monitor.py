import docker
import threading
import time


class RuntimeMonitor:

    def __init__(self):

        self.client = docker.from_env()

        self.running = False

        self.cpu = []

        self.memory = []

        self.disk_read = []

        self.disk_write = []

        self.network_rx = []

        self.network_tx = []

    def start(self, container_id):

        self.running = True

        thread = threading.Thread(
            target=self.monitor,
            args=(container_id,)
        )

        thread.daemon = True

        thread.start()

        self.thread = thread

    def stop(self):

        self.running = False

        if hasattr(self, "thread"):
            self.thread.join()

    def summary(
        self,
        performance_metrics=None,
        current_users=0
    ):

        metrics = {

            "cpu_avg": round(sum(self.cpu) / len(self.cpu), 2) if self.cpu else 0,
            "cpu_peak": round(max(self.cpu), 2) if self.cpu else 0,

            "memory_avg": round(sum(self.memory) / len(self.memory), 2) if self.memory else 0,
            "memory_peak": round(max(self.memory), 2) if self.memory else 0,

            "disk_read_mb": round(max(self.disk_read), 2) if self.disk_read else 0,
            "disk_write_mb": round(max(self.disk_write), 2) if self.disk_write else 0,

            "network_rx_mb": round(max(self.network_rx), 2) if self.network_rx else 0,
            "network_tx_mb": round(max(self.network_tx), 2) if self.network_tx else 0

        }

        # Ready for Mathematical Prediction Engine
        if performance_metrics:

            metrics.update({

                "current_users": current_users,

                "throughput": performance_metrics["throughput"],

                "response_time": performance_metrics["average_response_time"],

                "cpu_usage": metrics["cpu_avg"],

                "memory_usage": metrics["memory_avg"],

                "error_rate": performance_metrics["failure_rate"]

            })

        return metrics

    def monitor(self, container_id):

        container = self.client.containers.get(container_id)

        while self.running:

            stats = container.stats(stream=False)

            # ------------------------
            # MEMORY (MB)
            # ------------------------

            memory_usage = (
                stats["memory_stats"]["usage"]
                / 1024
                / 1024
            )

            self.memory.append(memory_usage)

            # ------------------------
            # CPU %
            # ------------------------

            cpu_stats = stats["cpu_stats"]
            precpu = stats["precpu_stats"]

            cpu_delta = (
                cpu_stats["cpu_usage"]["total_usage"]
                -
                precpu["cpu_usage"]["total_usage"]
            )

            system_delta = (
                cpu_stats["system_cpu_usage"]
                -
                precpu["system_cpu_usage"]
            )

            if system_delta > 0:

                cpu_percent = (
                    cpu_delta
                    / system_delta
                ) * cpu_stats["online_cpus"] * 100

            else:

                cpu_percent = 0

            self.cpu.append(cpu_percent)

            # ------------------------
            # DISK I/O
            # ------------------------

            blkio = stats.get(
                "blkio_stats",
                {}
            ).get(
                "io_service_bytes_recursive",
                []
            )

            read_bytes = 0
            write_bytes = 0

            for item in blkio:

                if item["op"] == "Read":

                    read_bytes += item["value"]

                elif item["op"] == "Write":

                    write_bytes += item["value"]

            self.disk_read.append(read_bytes / 1024 / 1024)

            self.disk_write.append(write_bytes / 1024 / 1024)

            # ------------------------
            # NETWORK I/O
            # ------------------------

            networks = stats.get("networks", {})

            rx = 0
            tx = 0

            for interface in networks.values():

                rx += interface["rx_bytes"]

                tx += interface["tx_bytes"]

            self.network_rx.append(rx / 1024 / 1024)

            self.network_tx.append(tx / 1024 / 1024)

            time.sleep(1)