import time
import requests


class HealthChecker:

    def __init__(self, url="http://localhost:5000"):
        self.url = url

    def wait_until_ready(self, timeout=30):

        print("Waiting for application to start...")

        start_time = time.time()

        while time.time() - start_time < timeout:

            try:

                response = requests.get(self.url)

                if response.status_code == 200:

                    print("Application is ready.")

                    return True

            except requests.exceptions.ConnectionError:
                pass

            print("Waiting...")

            time.sleep(2)

        print("Application failed to start.")

        return False