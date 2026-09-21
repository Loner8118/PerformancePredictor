from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from automation.docker_manager import DockerManager


class HealthCheckError(ValueError):
    """Bad input to HealthChecker (e.g. missing/invalid host or port)."""


# ==========================================================================
# What this module does
# ==========================================================================
#
# Polls a running container's HTTP endpoint until it responds, or until a
# timeout or an early container crash is detected.
#
# Deliberately has NO framework-specific logic of its own. host/port come
# directly from docker_manager.run_container()'s result, which already
# resolves the correct port for every framework - auto-generated
# Dockerfiles all use one consistent internal port, and existing
# Dockerfiles are parsed for their real EXPOSE line (see
# docker_manager.py's module docstring). There was originally a plan for
# this file to carry its own framework-to-default-port table, but once
# docker_manager.py was resolving that correctly and returning it, adding
# a second, redundant port-guessing mechanism here would just be a second
# place for that logic to drift out of sync - so it doesn't.
#
# One naming collision worth calling out explicitly: docker_manager's
# run_container() result has a "host" key, but that value is already a
# full base URL ("http://localhost:32768"), not a bare hostname
# ("localhost") - it bakes in the resolved host port. wait_until_ready()
# accepts either shape (detecting "://") specifically so passing that
# field straight through doesn't silently build a broken double-scheme,
# double-port URL.
#
# "Ready" means the HTTP server is accepting connections and speaking
# HTTP at all - ANY status code counts (200, 404, 500 all prove the
# server process is up and listening), since this pipeline has no way of
# knowing whether a specific route exists at "/". Only connection-level
# failures (refused, timed out) count as "not ready yet".
#
# The one thing worth actually building well here: a genuinely crashed
# container should fail FAST with the container's logs attached, not
# silently poll until the full timeout elapses and report a generic
# "didn't respond" with no explanation. That distinction - "still
# starting up" vs. "already dead" - is what turns a health-check timeout
# from a dead end into something the person can actually debug.


class HealthChecker:
    DEFAULT_TIMEOUT_SECONDS = 60.0
    DEFAULT_INITIAL_INTERVAL_SECONDS = 0.5
    DEFAULT_MAX_INTERVAL_SECONDS = 5.0
    DEFAULT_REQUEST_TIMEOUT_SECONDS = 5.0

    def __init__(
        self,
        docker_manager: Optional[DockerManager] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        initial_interval_seconds: float = DEFAULT_INITIAL_INTERVAL_SECONDS,
        max_interval_seconds: float = DEFAULT_MAX_INTERVAL_SECONDS,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise HealthCheckError("timeout_seconds must be greater than zero.")
        if initial_interval_seconds <= 0:
            raise HealthCheckError("initial_interval_seconds must be greater than zero.")
        if max_interval_seconds < initial_interval_seconds:
            raise HealthCheckError("max_interval_seconds must be >= initial_interval_seconds.")
        if request_timeout_seconds <= 0:
            raise HealthCheckError("request_timeout_seconds must be greater than zero.")

        # Only used for crash detection (get_container_status) and crash
        # diagnostics (get_container_logs) when container_id is supplied -
        # a caller doing pure HTTP polling without a container_id never
        # touches this.
        self.docker_manager = docker_manager or DockerManager()
        self.timeout_seconds = timeout_seconds
        self.initial_interval_seconds = initial_interval_seconds
        self.max_interval_seconds = max_interval_seconds
        self.request_timeout_seconds = request_timeout_seconds

    def wait_until_ready(
        self,
        host: str,
        port: Optional[int] = None,
        path: str = "/",
        container_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Args:
            host: either a bare hostname ("localhost") - in which case
                `port` is required - or a full base URL
                ("http://localhost:32768") - in which case `port` is
                ignored for URL construction. Accepting both matters in
                practice: docker_manager.run_container()'s result has a
                "host" key that is ALREADY a full base URL (it bakes in
                the resolved host port), not a bare hostname, despite the
                name - passing that value straight through used to
                silently build a broken double-scheme URL like
                "http://http://localhost:32768:32768/". Detecting "://"
                here means callers don't have to remember that gotcha.
            port: port to poll - required only when `host` is a bare
                hostname. Use the "port" value from
                docker_manager.run_container()'s result in that case.
            path: URL path to request. Defaults to "/" - any HTTP
                response (including 404/500) counts as ready, so this
                rarely needs to be anything else.
            container_id: optional - if given, the container's status is
                checked between polls so a crashed container is reported
                immediately (with logs attached) instead of silently
                polling until timeout_seconds elapses.

        Returns:
            {"ready": bool, "elapsed_seconds": float, "attempts": int,
             "status_code": int | None, "reason": str,
             "container_crashed": bool, "container_logs": str | None}
        """
        if not host:
            raise HealthCheckError("host is required.")

        if "://" in host:
            base_url = host.rstrip("/")
        else:
            if not isinstance(port, int) or isinstance(port, bool) or not (0 < port <= 65535):
                raise HealthCheckError(f"port must be a valid port number, got {port!r}.")
            base_url = f"http://{host}:{port}"

        normalized_path = path if path.startswith("/") else f"/{path}"
        url = f"{base_url}{normalized_path}"

        start = time.monotonic()
        interval = self.initial_interval_seconds
        attempts = 0

        while True:
            attempts += 1

            status_code = self._poll_once(url)
            if status_code is not None:
                return self._result(
                    ready=True,
                    elapsed=time.monotonic() - start,
                    attempts=attempts,
                    status_code=status_code,
                    reason=f"Received HTTP {status_code} from {url}.",
                    container_crashed=False,
                    container_logs=None,
                )

            if container_id:
                crash = self._check_for_crash(container_id)
                if crash is not None:
                    return self._result(
                        ready=False,
                        elapsed=time.monotonic() - start,
                        attempts=attempts,
                        status_code=None,
                        reason=crash["reason"],
                        container_crashed=True,
                        container_logs=crash["logs"],
                    )

            elapsed = time.monotonic() - start
            if elapsed >= self.timeout_seconds:
                return self._result(
                    ready=False,
                    elapsed=elapsed,
                    attempts=attempts,
                    status_code=None,
                    reason=f"Timed out after {self.timeout_seconds}s waiting for {url} to respond.",
                    container_crashed=False,
                    container_logs=None,
                )

            remaining = self.timeout_seconds - elapsed
            time.sleep(min(interval, max(0.0, remaining)))
            interval = min(interval * 2, self.max_interval_seconds)

    def _poll_once(self, url: str) -> Optional[int]:
        """Returns the HTTP status code if the server answered at all, None if the connection failed."""
        try:
            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request, timeout=self.request_timeout_seconds) as response:
                return response.getcode()
        except urllib.error.HTTPError as e:
            # A non-2xx response still proves the server is up and
            # speaking HTTP - that's what "ready" means here.
            return e.code
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            # Deliberately NOT a bare `except Exception` - connection-
            # level failures mean "not ready yet, keep polling"; anything
            # else (e.g. a real bug) should still propagate and be visible
            # rather than being silently swallowed as "not ready" forever.
            return None

    def _check_for_crash(self, container_id: str) -> Optional[Dict[str, Any]]:
        status_result = self.docker_manager.get_container_status(container_id)
        if not status_result.get("success"):
            return None  # inspect itself failed transiently - not evidence of a crash, keep polling

        status = status_result.get("status")
        if status in ("exited", "dead"):
            logs_result = self.docker_manager.get_container_logs(container_id)
            logs = logs_result.get("logs") if logs_result.get("success") else None
            return {
                "reason": f"Container exited unexpectedly (status={status}) before becoming ready.",
                "logs": logs,
            }
        return None

    @staticmethod
    def _result(
        ready: bool,
        elapsed: float,
        attempts: int,
        status_code: Optional[int],
        reason: str,
        container_crashed: bool,
        container_logs: Optional[str],
    ) -> Dict[str, Any]:
        return {
            "ready": ready,
            "elapsed_seconds": round(elapsed, 2),
            "attempts": attempts,
            "status_code": status_code,
            "reason": reason,
            "container_crashed": container_crashed,
            "container_logs": container_logs,
        }


def check_health(
    host: str,
    port: Optional[int] = None,
    path: str = "/",
    container_id: Optional[str] = None,
    docker_manager: Optional[DockerManager] = None,
    **checker_kwargs: Any,
) -> Dict[str, Any]:
    """One-shot: construct a HealthChecker and poll until ready or timeout.
    See HealthChecker for configurable options (timeout_seconds,
    initial_interval_seconds, max_interval_seconds, request_timeout_seconds)."""
    checker = HealthChecker(docker_manager=docker_manager, **checker_kwargs)
    return checker.wait_until_ready(host=host, port=port, path=path, container_id=container_id)