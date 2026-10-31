"""Client for the Go1 remote controller's explicit navigation authority."""
from __future__ import annotations

import json
import math
import time
import urllib.request

from .navigation_closed_loop import Telemetry


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

    def keepalive_navigation(self, task_id, token):
        return self._request("/api/navigation/keepalive", {"task_id": task_id})

    def pause_navigation(self, task_id, token, reason="visual_wait"):
        return self._request("/api/navigation/pause", {"task_id": task_id, "reason": reason})

    def resume_navigation(self, task_id, token):
        return self._request("/api/navigation/resume", {"task_id": task_id})


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

    @staticmethod
    def telemetry_from_snapshot(snapshot: dict, *, expected_command_id: str | None = None) -> Telemetry:
        """Convert receiver feedback into closed-loop telemetry.

        ``actual_yaw`` is an angular rate from the Go1 state stream. It is
        intentionally exposed only as ``actual_yaw_rate``; global heading is
        still supplied by visual localization.
        """
        robot = snapshot.get("robot") or {}
        navigation = snapshot.get("navigation") or {}
        feedback_record = snapshot.get("navigation_feedback") or {}
        executing = robot.get("motion_executing")
        stopped = robot.get("motion_stopped")
        if stopped is None:
            values = [robot.get("actual_vx"), robot.get("actual_vy"), robot.get("actual_yaw")]
            stopped = (all(isinstance(value, (int, float)) and math.isfinite(value) for value in values)
                       and math.hypot(float(values[0]), float(values[1])) < .05
                       and abs(float(values[2])) < .05 and not executing)
        receiver_sequence = feedback_record.get("receiver_command_sequence")
        send_sequence = feedback_record.get("move_send_sequence")
        sequence_matches = (isinstance(send_sequence, int)
                            and receiver_sequence == send_sequence)
        if executing is True and sequence_matches:
            feedback = "executing"
        elif stopped is True and sequence_matches:
            feedback = "stopped"
        else:
            feedback = "accepted"
        intent_id = feedback_record.get("command_id")
        command_id = intent_id
        lease_remaining = navigation.get("lease_remaining_s")
        lease_valid = (navigation.get("active") is True and
                       isinstance(lease_remaining, (int, float)) and lease_remaining > 0)
        if not navigation:
            lease_valid = snapshot.get("connected") is True
        actual_yaw_rate = robot.get("actual_yaw_rate", robot.get("actual_yaw"))
        started_time = feedback_record.get("motion_started_time_s")
        stopped_time = feedback_record.get("motion_stopped_time_s")
        feedback_time = (feedback_record.get("receiver_ack_time_s")
                         or feedback_record.get("status_received_time_s"))
        causal_feedback = bool(
            feedback_record.get("causal_feedback") is True
            and sequence_matches
            and feedback_record.get("receiver_ack_sequence") == send_sequence
            and (expected_command_id is None or intent_id == expected_command_id)
        )
        return Telemetry(
            healthy=robot.get("health") == "OK",
            stopped=bool(stopped),
            fault=robot.get("fault_reason") if robot.get("fault") else None,
            command_id=command_id,
            motion_feedback=feedback,
            feedback_time_s=feedback_time,
            control_active=robot.get("control_active") is True,
            armed=robot.get("armed") is True,
            lease_valid=lease_valid,
            manual_override=robot.get("remote_active") is True,
            telemetry_time_s=feedback_record.get("status_received_time_s")
            or snapshot.get("status_received_time_s"),
            actual_vx=robot.get("actual_vx"),
            actual_vy=robot.get("actual_vy"),
            actual_yaw_rate=actual_yaw_rate,
            motion_executing=executing,
            motion_stopped=bool(stopped),
            navigation_active=navigation.get("active", True) is True,
            navigation_paused=navigation.get("paused", False) is True,
            command_created_time_s=feedback_record.get("command_created_time_s"),
            intent_accepted_time_s=feedback_record.get("intent_accepted_time_s"),
            move_sent_time_s=feedback_record.get("move_sent_time_s"),
            receiver_ack_time_s=feedback_record.get("receiver_ack_time_s"),
            motion_started_time_s=started_time,
            motion_stopped_time_s=stopped_time,
            send_sequence=send_sequence,
            receiver_command_sequence=receiver_sequence,
            receiver_clock_id=feedback_record.get("receiver_clock_id"),
            feedback_clock_id=feedback_record.get("feedback_clock_id"),
            time_uncertainty_s=(float(feedback_record.get("time_uncertainty_s"))
                                if isinstance(feedback_record.get("time_uncertainty_s"), (int, float))
                                else float("inf")),
            causal_feedback=causal_feedback,
        )

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

    def execute_motion_action(self, action):
        """Submit a closed-loop MotionAction with its bounded intent duration."""
        return self.execute_motion_primitive(action.primitive,
                                             command_id=action.command_id,
                                             ttl_s=action.duration_s)

    def request_stop(self, reason: str = "navigation_stop"):
        if not self.authorized:
            raise PermissionError("navigation_authorization_required")
        result = self.client.stop_navigation(self.task_id, self.authorization_token, reason)
        if not result or result.get("accepted") is not True:
            raise RuntimeError("navigation_stop_not_accepted")
        self.active_primitive = None
        self.authorized = False
        return result

    def keepalive(self):
        if not self.authorized:
            raise PermissionError("navigation_authorization_required")
        result = self.client.keepalive_navigation(self.task_id, self.authorization_token)
        if not result or result.get("active") is not True:
            raise RuntimeError("navigation_keepalive_not_accepted")
        return result

    def pause(self, reason: str = "visual_wait"):
        if not self.authorized:
            raise PermissionError("navigation_authorization_required")
        result = self.client.pause_navigation(self.task_id, self.authorization_token, reason)
        if not result or result.get("paused") is not True:
            raise RuntimeError("navigation_pause_not_accepted")
        self.active_primitive = None
        return result

    def resume(self):
        if not self.authorized:
            raise PermissionError("navigation_authorization_required")
        result = self.client.resume_navigation(self.task_id, self.authorization_token)
        if not result or result.get("active") is not True or result.get("paused") is True:
            raise RuntimeError("navigation_resume_not_accepted")
        self.active_primitive = None
        return result

    def release_control(self):
        if not self.authorized:
            return
        result = self.client.release_navigation(self.task_id, self.authorization_token)
        if not result or result.get("accepted") is not True:
            raise RuntimeError("navigation_release_not_accepted")
        self.authorized = False
        self.active_primitive = None
