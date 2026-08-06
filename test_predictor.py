from prediction.predictor import analyze_system
from pprint import pprint

sample_data = {
    "load_test": {
        "user_levels": [100, 200, 400, 800],
        "throughput": [95, 180, 320, 470],
    },

    "runtime": {
        "current_users": 800,
        "throughput": 470,
        "response_time": 850,
        "cpu_usage": 72,
        "memory_usage": 65,
        "disk_io": 45,
        "network_io": 35,
        "error_rate": 0.5,
    },

    "queue": {
        "arrival_rate": 500,
        "service_rate": 600,
    },
}

result = analyze_system(sample_data)

pprint(result, sort_dicts=False)