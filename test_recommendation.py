from prediction.recommendation import generate_recommendations


sample_capacity = {

    "results": {

        "safe_users": 326,

        "growth_potential_users": 126,

        "capacity_used_percent": 74,

    },


    "inputs": {

        "runtime": {

            "current_users":200,

            "cpu_usage":65,

            "memory_usage":55,

        },


        "queueing": {

            "utilization":0.66

        }

    },


    "resource_pressure": {}

}



result = generate_recommendations(sample_capacity)


print("===================")
print("RECOMMENDATION")
print("===================")

print(result)