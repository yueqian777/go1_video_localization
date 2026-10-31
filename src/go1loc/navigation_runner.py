"""Small observe-command-observe runner with an explicit execution mode.

The runner is deliberately inert by default.  It never creates a robot control
session and never sends TAKE, ARM, or a non-zero primitive during construction
or startup.  A caller must choose SIMULATION or LIVE_SUPERVISED and authorize
the existing control session explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import time

from .grid_planner import GridRoute, MotionAction
from .navigation_closed_loop import (
    CalibrationEvidence,
    ClosedLoopNavigator,
    NavigationState,
    PoseObservation,
    Telemetry,
)


class RunnerMode(str, Enum):
    SHADOW = "SHADOW"
    SIMULATION = "SIMULATION"
    LIVE_SUPERVISED = "LIVE_SUPERVISED"


@dataclass(frozen=True)
class RunnerConfig:
    mode: RunnerMode = RunnerMode.SHADOW
    operator_authorized: bool = False
    calibration_evidence: CalibrationEvidence | None = None


@dataclass
class NavigationRunner:
    navigator: ClosedLoopNavigator
    adapter: object | None = None
    config: RunnerConfig = field(default_factory=RunnerConfig)
    audit: list[dict] = field(default_factory=list)
    _authorized: bool = False
    _paused_sent: bool = False
    _last_submitted_command_id: str | None = None

    def __post_init__(self):
        if isinstance(self.config.mode, str):
            self.config = RunnerConfig(
                mode=RunnerMode(self.config.mode),
                operator_authorized=self.config.operator_authorized,
                calibration_evidence=self.config.calibration_evidence,
            )
        if self.config.mode is not RunnerMode.SHADOW and self.adapter is None:
            raise ValueError("execution_adapter_required")

    @property
    def mode(self) -> RunnerMode:
        return self.config.mode

    def startup(self):
        """Prepare the runner without connecting, arming, or moving the robot."""
        self.audit.append({"event": "startup", "mode": self.mode.value,
                           "time_s": time.monotonic()})
        return self.status()

    def authorize(self):
        if self.mode is RunnerMode.SHADOW:
            self.navigator.authorize(shadow=True)
        elif self.mode is RunnerMode.SIMULATION:
            state = self.adapter.snapshot()
            if state.get("simulation") is not True:
                raise PermissionError("simulation_receiver_required")
            self.navigator.authorize(shadow=True)
            self.adapter.authorize_current_task(self.adapter.authorization_token)
        else:
            evidence = self.config.calibration_evidence
            if self.config.operator_authorized is not True:
                raise PermissionError("live_operator_authorization_required")
            if evidence is None or not evidence.valid_for_motion():
                raise PermissionError("live_calibration_evidence_required")
            navigator_evidence = self.navigator.config.calibration_evidence
            if (navigator_evidence is None
                    or not navigator_evidence.valid_for_motion()
                    or navigator_evidence.evidence_id != evidence.evidence_id):
                raise PermissionError("navigator_calibration_evidence_mismatch")
            self.navigator.authorize()
            self.adapter.authorize_current_task(self.adapter.authorization_token)
        self._authorized = True
        self.audit.append({"event": "authorized", "mode": self.mode.value,
                           "time_s": time.monotonic()})
        return self.status()

    def _send_stop_or_pause(self, state: NavigationState, reason: str):
        if self.adapter is None or not getattr(self.adapter, "authorized", False):
            return
        if state in {NavigationState.WAIT_RELOCALIZATION, NavigationState.PAUSED,
                     NavigationState.RESUMING}:
            if not self._paused_sent:
                try:
                    self.adapter.pause(reason)
                except Exception:
                    self._force_stop_after_error("pause_request_failed")
                    raise
                self._paused_sent = True
                self.audit.append({"event": "pause_sent", "reason": reason,
                                   "time_s": time.monotonic()})
        else:
            try:
                self.adapter.request_stop(reason)
            except Exception:
                self._force_stop_after_error("stop_request_failed")
                raise
            self.audit.append({"event": "stop_sent", "reason": reason,
                               "time_s": time.monotonic()})

    def _force_stop_after_error(self, reason: str):
        """Best-effort zero intent after a failed control request."""
        self.audit.append({"event": "control_request_failed", "reason": reason,
                           "time_s": time.monotonic()})
        if self.adapter is None or not getattr(self.adapter, "authorized", False):
            return
        try:
            self.adapter.request_stop(reason)
        except Exception as stop_error:
            self.audit.append({"event": "emergency_stop_request_failed",
                               "error": type(stop_error).__name__,
                               "time_s": time.monotonic()})

    def step(self, observation: PoseObservation, telemetry: Telemetry, *, now: float):
        if not self._authorized:
            raise PermissionError("runner_authorization_required")
        action = self.navigator.step(observation, telemetry, now=now)
        self.audit.append({"event": "navigator_action", "primitive": action.primitive,
                           "command_id": action.command_id, "state": self.navigator.state.value,
                           "time_s": now})
        if self.mode is RunnerMode.SHADOW:
            return action
        if action.primitive == "stop":
            self._send_stop_or_pause(self.navigator.state, self.navigator.failure_reason or "stop")
        elif action.primitive in {"forward", "backward", "strafe_left", "strafe_right",
                                  "turn_left", "turn_right"} and action.command_id != self._last_submitted_command_id:
            try:
                self.adapter.execute_motion_action(action)
            except Exception:
                self.navigator.pause("control_request_failed")
                self._force_stop_after_error("motion_request_failed")
                raise
            self._last_submitted_command_id = action.command_id
            self._paused_sent = False
            self.audit.append({"event": "motion_submitted", "primitive": action.primitive,
                               "command_id": action.command_id, "time_s": time.monotonic()})
        return action

    def pause(self, reason: str = "operator_pause"):
        action = self.navigator.pause(reason)
        self._send_stop_or_pause(NavigationState.PAUSED, reason)
        return action

    def resume_and_replan(self, route: GridRoute, observation: PoseObservation,
                          telemetry: Telemetry):
        self.navigator.resume(observation, telemetry)
        self.navigator.resume_with_route(route, observation)
        if self.adapter is not None and getattr(self.adapter, "authorized", False):
            self.adapter.resume()
        self._paused_sent = False
        self.audit.append({"event": "resume_replan", "time_s": time.monotonic()})
        return self.status()

    def stop(self, reason: str = "operator_stop"):
        if self.adapter is not None and getattr(self.adapter, "authorized", False):
            self.adapter.request_stop(reason)
        self.navigator.manual_override(reason)
        self._authorized = False
        self.audit.append({"event": "stop", "reason": reason, "time_s": time.monotonic()})
        return self.status()

    def status(self):
        return {
            "mode": self.mode.value,
            "authorized": self._authorized,
            "navigation_state": self.navigator.state.value,
            "last_command_id": self.navigator.current_command_id,
            "audit_events": len(self.audit),
        }


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Go1 observe-command-observe runner")
    parser.add_argument("--mode", choices=[mode.value for mode in RunnerMode],
                        default=RunnerMode.SHADOW.value)
    args = parser.parse_args(argv)
    print(json.dumps({"mode": args.mode, "startup": "inert",
                      "auto_take": False, "auto_arm": False,
                      "nonzero_motion_on_start": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
