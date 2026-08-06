from prediction.predictor import analyze_system


metrics = {

    "load_test": {

        # USL observations
        "user_levels": [
            50,
            100,
            200,
            400
        ],

        "throughput": [
            120,
            220,
            390,
            520
        ]
    },


    "runtime": {

        "current_users": 200,

        "throughput": 390,

        "response_time": 250,

        "cpu_usage": 65,

        "memory_usage": 55,

        "disk_io": 20,

        "network_io": 30,

        "error_rate": 0.2
    },


    "queue": {

        "arrival_rate": 400,

        "service_rate": 600
    }

}


result = analyze_system(metrics)


print("\n======================")
print("PIPELINE STATUS")
print("======================")

print(result["status"])


print("\n======================")
print("USL RESULT")
print("======================")

print(result["usl"])


print("\n======================")
print("LITTLE LAW RESULT")
print("======================")

print(result["little_law"])


print("\n======================")
print("QUEUE RESULT")
print("======================")

print(result["queueing"])


print("\n======================")
print("CAPACITY RESULT")
print("======================")

print(result["capacity"])


print("\n======================")
print("HEALTH SUMMARY")
print("======================")

print(
    result["capacity"]["health_summary"]
)

print("\n======================")
print("RECOMMENDATION RESULT")
print("======================")
print(result["recommendation"])