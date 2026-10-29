"""Explicitly authorized adapter to the existing go1_remote_control RobotLink."""
from __future__ import annotations


PRIMITIVE_KEYS = {
    "forward": "w", "backward": "s", "strafe_left": "j", "strafe_right": "l",
    "turn_left": "z", "turn_right": "c",
}


class Go1RemoteControlAdapter:
    def __init__(self, robot_link, *, authorization_token: str):
        self.link = robot_link
        self.authorization_token = authorization_token
        self.authorized = False
        self.active_primitive = None

    def snapshot(self):
        return self.link.snapshot()

    def authorize_current_task(self, token: str):
        if token != self.authorization_token:
            raise PermissionError("navigation_authorization_rejected")
        state = self.snapshot()
        robot = state.get("robot") or {}
        if not state.get("connected") or not robot.get("control_active") or not robot.get("armed"):
            raise PermissionError("robot_control_session_not_ready")
        self.authorized = True

    def _check(self):
        state = self.snapshot()
        robot = state.get("robot") or {}
        if not self.authorized:
            raise PermissionError("navigation_authorization_required")
        if not state.get("connected") or not robot.get("control_active") or not robot.get("armed"):
            self.authorized = False
            raise RuntimeError("robot_control_session_lost")
        # RobotLink.snapshot() keeps freshness at the top level. Accept the
        # nested form too so this adapter remains easy to exercise with a
        # small fake link, but never treat a missing freshness value as fresh.
        fresh = state.get("robot_fresh", robot.get("robot_fresh"))
        if robot.get("health") != "OK" or fresh is not True:
            self.authorized = False
            raise RuntimeError("robot_feedback_not_fresh_or_healthy")

    def execute_motion_primitive(self, primitive: str):
        self._check()
        if primitive not in PRIMITIVE_KEYS:
            raise ValueError("unsupported_motion_primitive")
        self.link.submit("heartbeat", PRIMITIVE_KEYS[primitive])
        self.active_primitive = primitive

    def request_stop(self, reason: str = "navigation_stop"):
        self.link.submit("heartbeat", "")
        self.active_primitive = None
        return {"primitive": "stop", "reason": reason, "command_sent": True}

    def release_control(self):
        self.link.submit("release")
        self.authorized = False
        self.active_primitive = None
