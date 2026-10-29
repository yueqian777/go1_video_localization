"""Client for the Go1 remote controller's explicit navigation authority."""
from __future__ import annotations

import json
import urllib.request


PRIMITIVE_KEYS = {
    "forward": "w", "backward": "s", "strafe_left": "j", "strafe_right": "l",
    "turn_left": "z", "turn_right": "c",
}


class NavigationHttpClient:
    """Loopback HTTP client; the remote server remains the RobotLink owner."""

    def __init__(self, base_url: str, *, local_token: str, navigation_token: str,
                 timeout_s: float = .8):
        self.base_url = base_url.rstrip("/")
        self.local_token = local_token
        self.navigation_token = navigation_token
        self.timeout_s = timeout_s

    def _request(self, path: str, body: dict | None = None):
        headers = {"X-Go1-Local-Token": self.local_token,
                   "X-Go1-Navigation-Token": self.navigation_token}
        if body is None:
            request = urllib.request.Request(self.base_url + path, headers=headers)
        else:
            data = json.dumps(body, allow_nan=False).encode("utf-8")
            headers.update({"Content-Type": "application/json", "Origin": self.base_url})
            request = urllib.request.Request(self.base_url + path, data=data,
                                              headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))

    def snapshot(self):
        return self._request("/api/state")

    def authorize_navigation(self, task_id, token):
        return self._request("/api/navigation/authorize", {"task_id": task_id})

    def submit_navigation(self, task_id, token, command_id, primitive, ttl_s=None):
        body = {"task_id": task_id, "command_id": command_id, "primitive": primitive}
        if ttl_s is not None:
            body["ttl_s"] = ttl_s
        return self._request("/api/navigation/command", body)

    def stop_navigation(self, task_id, token, reason):
        return self._request("/api/navigation/stop", {"task_id": task_id, "reason": reason})

    def release_navigation(self, task_id, token):
        return self._request("/api/navigation/release", {"task_id": task_id})


class Go1RemoteControlAdapter:
    def __init__(self, navigation_client, *, authorization_token: str,
                 task_id: str = "go1loc-navigation"):
        self.client = navigation_client
        self.authorization_token = authorization_token
        self.task_id = task_id
        self.authorized = False
        self.active_primitive = None
        self.command_counter = 0

    def snapshot(self):
        return self.client.snapshot()

    def authorize_current_task(self, token: str):
        if token != self.authorization_token:
            raise PermissionError("navigation_authorization_rejected")
        required = ("authorize_navigation", "submit_navigation", "stop_navigation",
                    "release_navigation")
        if not all(hasattr(self.client, name) for name in required):
            raise PermissionError("navigation_authority_required")
        state = self.snapshot()
        robot = state.get("robot") or {}
        if not state.get("connected") or state.get("robot_fresh") is not True:
            raise PermissionError("robot_control_session_not_ready")
        if not robot.get("control_active") or not robot.get("armed"):
            raise PermissionError("robot_control_session_not_ready")
        result = self.client.authorize_navigation(self.task_id, token)
        if not result or result.get("active") is False:
            raise PermissionError("navigation_authorization_rejected")
        self.authorized = True

    def _check(self):
        state = self.snapshot()
        robot = state.get("robot") or {}
        if not self.authorized:
            raise PermissionError("navigation_authorization_required")
        if not state.get("connected") or not state.get("robot_fresh"):
            self.authorized = False
            raise RuntimeError("robot_control_session_lost")
        fresh = state.get("robot_fresh", robot.get("robot_fresh"))
        if robot.get("health") != "OK" or fresh is not True:
            self.authorized = False
            raise RuntimeError("robot_feedback_not_fresh_or_healthy")

    def execute_motion_primitive(self, primitive: str, *, command_id: str | None = None,
                                 ttl_s: float | None = None):
        self._check()
        if primitive not in PRIMITIVE_KEYS:
            raise ValueError("unsupported_motion_primitive")
        self.command_counter += 1
        command_id = command_id or f"adapter-{self.command_counter:06d}"
        result = self.client.submit_navigation(self.task_id, self.authorization_token,
                                                command_id, primitive, ttl_s)
        if not result or result.get("accepted") is not True:
            raise RuntimeError("navigation_command_not_accepted")
        self.active_primitive = primitive
        return result

    def request_stop(self, reason: str = "navigation_stop"):
        if not self.authorized:
            raise PermissionError("navigation_authorization_required")
        result = self.client.stop_navigation(self.task_id, self.authorization_token, reason)
        if not result or result.get("accepted") is not True:
            raise RuntimeError("navigation_stop_not_accepted")
        self.active_primitive = None
        self.authorized = False
        return result

    def release_control(self):
        if not self.authorized:
            return
        result = self.client.release_navigation(self.task_id, self.authorization_token)
        if not result or result.get("accepted") is not True:
            raise RuntimeError("navigation_release_not_accepted")
        self.authorized = False
        self.active_primitive = None
