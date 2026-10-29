"""Bounded observe-plan-command-observe navigation loop.

This module is transport-agnostic. It emits one primitive at a time and never
converts command duration into travelled distance.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .grid_planner import GridRoute, MotionAction


class NavigationState(str, Enum):
    IDLE = "Idle"
    PREPARED = "AwaitingAuthorization"
    MOVING_SEGMENT = "MovingSegment"
    WAITING_FOR_OBSERVATION = "WaitingForObservation"
    REPLANNING = "Replanning"
    ARRIVED = "Arrived"
    STOPPED = "Stopped"
    MOTION_FAULT = "MotionFault"
    MANUAL_OVERRIDE = "ManualOverride"


@dataclass(frozen=True)
class ClosedLoopConfig:
    metric_scale: bool
    camera_mount_verified: bool
    up_verified: bool
    max_pose_age_s: float = 0.75


@dataclass(frozen=True)
class PoseObservation:
    x: float
    y: float
    heading: str
    received_age_s: float
    localized: bool


@dataclass(frozen=True)
class Telemetry:
    healthy: bool
    stopped: bool
    fault: str | None = None


class ClosedLoopNavigator:
    def __init__(self, route: GridRoute, *, config: ClosedLoopConfig):
        self.route = route
        self.config = config
        self.state = NavigationState.IDLE
        self.index = 0
        self.last_command = MotionAction("stop")
        self.authorized = False
        self.pending_observation_target = None

    def prepare(self):
        if self.route.status != "planned":
            raise ValueError("route_not_executable")
        self.state = NavigationState.PREPARED

    def authorize(self):
        if not (self.config.metric_scale and self.config.camera_mount_verified and self.config.up_verified):
            raise PermissionError("execution_calibration_gate_failed")
        if self.state is not NavigationState.PREPARED:
            raise PermissionError("navigation_not_prepared")
        self.authorized = True

    def _stop(self, state):
        self.last_command = MotionAction("stop")
        self.state = state
        return self.last_command

    def step(self, observation: PoseObservation, telemetry: Telemetry, *, now: float):
        del now
        if self.state in {NavigationState.MANUAL_OVERRIDE, NavigationState.STOPPED,
                          NavigationState.MOTION_FAULT, NavigationState.ARRIVED}:
            return self._stop(self.state)
        if not self.authorized:
            return self._stop(NavigationState.PREPARED)
        if (not observation.localized or observation.received_age_s > self.config.max_pose_age_s
                or not telemetry.healthy or telemetry.fault):
            return self._stop(NavigationState.MOTION_FAULT if telemetry.fault else NavigationState.STOPPED)
        # A forward primitive is a bounded segment, not permission to replay
        # the next queued command. Require a fresh observation at its target
        # and a stopped feedback state before advancing the route.
        if self.pending_observation_target is not None:
            at_target = (round(observation.x), round(observation.y)) == self.pending_observation_target
            if not at_target:
                return self._stop(NavigationState.STOPPED if not telemetry.stopped
                                  else NavigationState.REPLANNING)
            if not telemetry.stopped:
                return self._stop(NavigationState.STOPPED)
        if self.state is NavigationState.WAITING_FOR_OBSERVATION:
            if not telemetry.stopped:
                return self._stop(NavigationState.STOPPED)
            self.state = NavigationState.REPLANNING
        if self.pending_observation_target is not None and (
                round(observation.x), round(observation.y)) == self.pending_observation_target:
            self.pending_observation_target = None
            return self._stop(NavigationState.WAITING_FOR_OBSERVATION)
        if self.index >= len(self.route.actions):
            return self._stop(NavigationState.ARRIVED)
        if not telemetry.stopped and self.state is NavigationState.WAITING_FOR_OBSERVATION:
            return self._stop(NavigationState.STOPPED)
        action = self.route.actions[self.index]
        self.last_command = action
        if action.primitive in {"turn_left", "turn_right", "forward", "backward", "strafe_left", "strafe_right"}:
            self.state = NavigationState.MOVING_SEGMENT
        self.index += 1
        if action.primitive == "forward" and action.cell is not None:
            self.pending_observation_target = action.cell
        return action

    def manual_override(self, reason: str):
        self.authorized = False
        self.last_command = MotionAction("stop")
        self.state = NavigationState.MANUAL_OVERRIDE
        return MotionAction("stop", None)
