import psutil
import time


class MetricsCollector:


    def collect(self, users, throughput, duration=5):

        print("Collecting runtime metrics...")


        cpu_values = []
        memory_values = []


        start = time.time()


        while time.time() - start < duration:

            cpu_values.append(
                psutil.cpu_percent(interval=1)
            )

            memory_values.append(
                psutil.virtual_memory().percent
            )


        metrics = {

            "current_users": users,

            "throughput": round(
                throughput,
                2
            ),

            "response_time": 250,

            "cpu_usage": round(
                sum(cpu_values)/len(cpu_values),
                2
            ),

            "memory_usage": round(
                sum(memory_values)/len(memory_values),
                2
            ),

            "disk_io": 0,

            "network_io": 0,

            "error_rate": 0

        }


        return metrics