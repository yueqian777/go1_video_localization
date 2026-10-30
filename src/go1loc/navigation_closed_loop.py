"""Bounded observe-plan-command-observe navigation loop."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math

from .free_space import FreeSpaceMap
from .grid_planner import GridRoute, MotionAction


class NavigationState(str, Enum):
    IDLE = "Idle"
    PREPARED = "AwaitingAuthorization"
    MOVING_SEGMENT = "MovingSegment"
    WAITING_FOR_OBSERVATION = "WaitingForObservation"
    WAIT_RELOCALIZATION = "WaitRelocalization"
    PAUSED = "Paused"
    RESUMING = "Resuming"
    REPLANNING = "Replanning"
    ARRIVED = "Arrived"
    STOPPED = "Stopped"
    MOTION_FAULT = "MotionFault"
    MANUAL_OVERRIDE = "ManualOverride"


class PrimitivePhase(str, Enum):
    IDLE = "idle"
    REQUESTED = "requested"
    EXECUTING = "executing"
    STOP_REQUESTED = "stop_requested"
    CONFIRMED = "confirmed"
    FAILED = "failed"


@dataclass(frozen=True)
class ClosedLoopConfig:
    metric_scale: bool
    camera_mount_verified: bool
    up_verified: bool
    max_pose_age_s: float = 0.75
    max_command_age_s: float = 1.5
    map_version: str | None = None
    min_localization_quality: float = 0.0
    require_observation_identity: bool | None = None
    require_goal_observation: bool = True
    max_forward_cells: int = 3
    resume_window_s: float = 5.0
    forward_cell_duration_s: float = .35
    turn_intent_duration_s: float = .7

    def __post_init__(self):
        for value, name in ((self.max_pose_age_s, "max_pose_age_s"),
                            (self.max_command_age_s, "max_command_age_s"),
                            (self.min_localization_quality, "min_localization_quality"),
                            (self.resume_window_s, "resume_window_s"),
                            (self.forward_cell_duration_s, "forward_cell_duration_s"),
                            (self.turn_intent_duration_s, "turn_intent_duration_s")):
            if not math.isfinite(float(value)) or float(value) < 0:
                raise ValueError(f"{name}_invalid")
        if isinstance(self.max_forward_cells, bool) or int(self.max_forward_cells) < 1:
            raise ValueError("max_forward_cells_invalid")


@dataclass(frozen=True)
class PoseObservation:
    x: float
    y: float
    heading: str
    received_age_s: float
    localized: bool
    frame_id: str | None = None
    capture_session_id: str | None = None
    source_sequence: int | None = None
    source_time_s: float | None = None
    received_time_s: float | None = None
    localization_time_s: float | None = None
    map_version: str | None = None
    localization_quality: float | None = None
    target_visible: bool = False


@dataclass(frozen=True)
class Telemetry:
    healthy: bool
    stopped: bool
    fault: str | None = None
    command_id: str | None = None
    motion_feedback: str | None = None
    feedback_time_s: float | None = None
    control_active: bool = True
    armed: bool = True
    lease_valid: bool = True
    manual_override: bool = False
    telemetry_time_s: float | None = None
    actual_vx: float | None = None
    actual_vy: float | None = None
    actual_yaw_rate: float | None = None
    motion_executing: bool | None = None
    motion_stopped: bool | None = None
    navigation_active: bool = True
    navigation_paused: bool = False


@dataclass(frozen=True)
class PosePrediction:
    x: float
    y: float
    heading: str
    heading_delta_rad: float
    elapsed_s: float


def predict_pose(observation: PoseObservation, telemetry: Telemetry, *,
                 elapsed_s: float, max_elapsed_s: float = .5) -> PosePrediction:
    """Integrate short body-frame velocity while retaining visual heading.

    ``actual_yaw`` from Go1 is an angular rate. It is reported separately as
    ``actual_yaw_rate`` and never replaces the absolute visual heading.
    Prediction is deliberately bounded and is a bridge between observations,
    not a source of global map correction.
    """
    elapsed_s = float(elapsed_s)
    if not math.isfinite(elapsed_s) or elapsed_s < 0 or elapsed_s > max_elapsed_s:
        raise ValueError("pose_prediction_window_exceeded")
    if observation.heading not in {"N", "E", "S", "W"}:
        raise ValueError("pose_prediction_heading_invalid")
    vx = telemetry.actual_vx
    vy = telemetry.actual_vy
    yaw_rate = telemetry.actual_yaw_rate
    if vx is None or vy is None:
        raise ValueError("pose_prediction_velocity_unavailable")
    yaw_rate = 0.0 if yaw_rate is None else yaw_rate
    if not all(math.isfinite(float(value)) for value in (vx, vy, yaw_rate)):
        raise ValueError("pose_prediction_velocity_invalid")
    if observation.heading == "E":
        dx, dy = vx, vy
    elif observation.heading == "W":
        dx, dy = -vx, -vy
    elif observation.heading == "N":
        dx, dy = -vy, vx
    else:
        dx, dy = vy, -vx
    return PosePrediction(observation.x + dx * elapsed_s,
                          observation.y + dy * elapsed_s,
                          observation.heading, yaw_rate * elapsed_s, elapsed_s)


def coalesce_route_actions(route: GridRoute, max_forward_cells: int = 3) -> GridRoute:
    """Merge only consecutive same-heading forward cells; A* remains untouched."""
    if isinstance(max_forward_cells, bool) or int(max_forward_cells) < 1:
        raise ValueError("max_forward_cells_invalid")
    merged = []
    index = 0
    actions = route.actions
    while index < len(actions):
        action = actions[index]
        if action.primitive != "forward":
            merged.append(action)
            index += 1
            continue
        heading = action.target_heading
        final_cell = action.cell
        distance = 1
        index += 1
        while index < len(actions) and distance < max_forward_cells:
            candidate = actions[index]
            if candidate.primitive != "forward" or candidate.target_heading != heading:
                break
            final_cell = candidate.cell
            distance += 1
            index += 1
        merged.append(MotionAction("forward", final_cell, heading, None, distance))
    return GridRoute(route.status, list(route.path), merged, route.source, route.reason,
                     route.requires_exploration)


_MOTION_PRIMITIVES = {
    "turn_left", "turn_right", "forward", "backward", "strafe_left", "strafe_right",
}
_FEEDBACK_OK = {"accepted", "executing", "stopped"}


class ClosedLoopNavigator:
    def __init__(self, route: GridRoute, *, config: ClosedLoopConfig,
                 grid: FreeSpaceMap | None = None):
        self.config = config
        self.route = coalesce_route_actions(route, config.max_forward_cells)
        self.grid = grid or FreeSpaceMap(1.0, {})
        self.state = NavigationState.IDLE
        self.phase = PrimitivePhase.IDLE
        self.index = 0
        self.last_command = MotionAction("stop")
        self.authorized = False
        self.pending_observation_target = None
        self.current_action: MotionAction | None = None
        self.current_command_id: str | None = None
        self.command_started_at: float | None = None
        self.command_start_observation_key = None
        self.last_observation_key = None
        self.last_clock: float | None = None
        self.command_counter = 0
        self.awaiting_goal_observation = False
        self.failure_reason: str | None = None

    @property
    def identity_required(self) -> bool:
        if self.config.require_observation_identity is not None:
            return self.config.require_observation_identity
        return self.config.map_version is not None

    def prepare(self):
        if self.route.status != "planned" or self.route.requires_exploration:
            raise ValueError("route_not_executable")
        self.state = NavigationState.PREPARED

    def authorize(self):
        if not (self.config.metric_scale and self.config.camera_mount_verified and self.config.up_verified):
            raise PermissionError("execution_calibration_gate_failed")
        if self.state is not NavigationState.PREPARED:
            raise PermissionError("navigation_not_prepared")
        self.authorized = True

    def _new_command(self, action: MotionAction, now: float) -> MotionAction:
        self.command_counter += 1
        command_id = f"nav-{self.command_counter:06d}"
        if action.duration_s is not None:
            duration_s = action.duration_s
        elif action.primitive == "forward":
            duration_s = min(2.0, max(.05, action.distance_cells * self.config.forward_cell_duration_s))
        elif action.primitive in {"turn_left", "turn_right"}:
            duration_s = min(2.0, max(.05, self.config.turn_intent_duration_s))
        else:
            duration_s = .5
        issued = MotionAction(action.primitive, action.cell, action.target_heading,
                              command_id, action.distance_cells, duration_s)
        self.current_action = issued
        self.current_command_id = command_id
        self.command_started_at = now
        self.phase = PrimitivePhase.REQUESTED
        self.state = NavigationState.MOVING_SEGMENT
        self.last_command = issued
        return issued

    def _stop(self, state: NavigationState, reason: str | None = None) -> MotionAction:
        self.last_command = MotionAction("stop")
        self.state = state
        if reason:
            self.failure_reason = reason
        return self.last_command

    def _wait(self) -> MotionAction:
        self.last_command = MotionAction("wait")
        return self.last_command

    @staticmethod
    def _observation_key(observation: PoseObservation):
        if observation.frame_id is not None or observation.source_sequence is not None:
            return (observation.capture_session_id, observation.frame_id,
                    observation.source_sequence, observation.map_version)
        return ("legacy", observation.x, observation.y, observation.heading,
                observation.received_time_s, observation.source_time_s)

    def _validate_observation(self, observation: PoseObservation) -> bool:
        if not observation.localized or observation.received_age_s > self.config.max_pose_age_s:
            return False
        if observation.heading not in {"N", "E", "S", "W"}:
            return False
        if not all(math.isfinite(float(value)) for value in (observation.x, observation.y,
                                                             observation.received_age_s)):
            return False
        if self.identity_required:
            if (not observation.capture_session_id or not observation.frame_id
                    or observation.source_sequence is None or not observation.map_version):
                return False
        if self.config.map_version is not None and observation.map_version != self.config.map_version:
            return False
        if (observation.localization_quality is not None
                and observation.localization_quality < self.config.min_localization_quality):
            return False
        if self.config.min_localization_quality > 0 and observation.localization_quality is None:
            return False
        return True

    def _feedback_matches(self, telemetry: Telemetry) -> bool:
        if telemetry.command_id is not None and telemetry.command_id != self.current_command_id:
            return False
        if not self.identity_required:
            return telemetry.healthy and (telemetry.stopped or telemetry.motion_feedback in _FEEDBACK_OK)
        return (telemetry.command_id == self.current_command_id
                and telemetry.motion_feedback in _FEEDBACK_OK
                and telemetry.feedback_time_s is not None
                and (self.command_started_at is None or telemetry.feedback_time_s >= self.command_started_at))

    @staticmethod
    def _safe_telemetry(telemetry: Telemetry) -> bool:
        return (telemetry.healthy and not telemetry.fault and telemetry.control_active
                and telemetry.armed and telemetry.lease_valid
                and telemetry.navigation_active and not telemetry.navigation_paused)

    def _confirm_current(self, observation: PoseObservation, telemetry: Telemetry):
        action = self.current_action
        stopped = telemetry.motion_stopped if telemetry.motion_stopped is not None else telemetry.stopped
        if action is None or not self._feedback_matches(telemetry) or not stopped:
            return False
        key = self._observation_key(observation)
        if key == self.command_start_observation_key or key == self.last_observation_key:
            return False
        if action.primitive in {"turn_left", "turn_right"}:
            if observation.heading != action.target_heading:
                return False
        elif action.primitive == "forward" and action.cell is not None:
            if self.grid.world_to_cell(observation.x, observation.y) != action.cell:
                return False
        self.index += 1
        self.last_observation_key = key
        self.current_action = None
        self.current_command_id = None
        self.command_started_at = None
        self.command_start_observation_key = None
        self.phase = PrimitivePhase.CONFIRMED
        self.pending_observation_target = None
        if self.index >= len(self.route.actions):
            if self.config.require_goal_observation and not observation.target_visible:
                self.awaiting_goal_observation = True
                self.state = NavigationState.WAITING_FOR_OBSERVATION
            else:
                self.state = NavigationState.ARRIVED
        else:
            self.state = NavigationState.WAITING_FOR_OBSERVATION
        return True

    def replace_route(self, route: GridRoute):
        if route.status != "planned" or route.requires_exploration:
            raise ValueError("route_not_executable")
        self.route = coalesce_route_actions(route, self.config.max_forward_cells)
        self.index = 0
        self.current_action = None
        self.current_command_id = None
        self.command_started_at = None
        self.command_start_observation_key = None
        self.pending_observation_target = None
        self.awaiting_goal_observation = False
        self.failure_reason = None
        self.phase = PrimitivePhase.IDLE
        self.state = NavigationState.REPLANNING

    def pause(self, reason: str = "navigation_paused"):
        if not self.authorized:
            raise PermissionError("navigation_authorization_required")
        self.current_action = None
        self.current_command_id = None
        self.command_started_at = None
        self.command_start_observation_key = None
        self.phase = PrimitivePhase.STOP_REQUESTED
        self.failure_reason = reason
        self.state = NavigationState.PAUSED
        self.last_command = MotionAction("stop")
        return self.last_command

    def resume(self, observation: PoseObservation, telemetry: Telemetry):
        if self.state is not NavigationState.PAUSED:
            raise PermissionError("navigation_not_paused")
        if telemetry.manual_override or not self._safe_telemetry(telemetry):
            raise PermissionError("navigation_control_session_not_ready")
        if not self._validate_observation(observation):
            raise PermissionError("relocalization_required")
        self.state = NavigationState.RESUMING
        self.phase = PrimitivePhase.IDLE
        self.last_observation_key = self._observation_key(observation)
        self.failure_reason = None
        self.last_command = MotionAction("wait")
        return self.last_command

    def resume_with_route(self, route: GridRoute, observation: PoseObservation):
        if self.state is not NavigationState.RESUMING:
            raise PermissionError("navigation_not_resuming")
        if not self._validate_observation(observation):
            raise PermissionError("relocalization_required")
        if route.path and route.path[0][:2] != self.grid.world_to_cell(observation.x, observation.y):
            raise ValueError("replan_must_start_at_current_pose")
        self.replace_route(route)

    def step(self, observation: PoseObservation, telemetry: Telemetry, *, now: float):
        now = float(now)
        if not math.isfinite(now):
            raise ValueError("finite_now_required")
        if self.last_clock is not None and now < self.last_clock:
            raise ValueError("non_monotonic_clock")
        self.last_clock = now
        if self.state in {NavigationState.MANUAL_OVERRIDE, NavigationState.STOPPED,
                          NavigationState.MOTION_FAULT, NavigationState.ARRIVED}:
            return self._stop(self.state)
        if not self.authorized:
            return self._stop(NavigationState.PREPARED)
        if telemetry.manual_override:
            return self._stop(NavigationState.MANUAL_OVERRIDE, "manual_override")
        if telemetry.fault:
            self.phase = PrimitivePhase.FAILED
            return self._stop(NavigationState.MOTION_FAULT, telemetry.fault)
        if not self._safe_telemetry(telemetry):
            self.phase = PrimitivePhase.FAILED
            return self._stop(NavigationState.STOPPED, "control_session_not_safe")
        if self.state is NavigationState.PAUSED:
            return self._stop(self.state)
        valid_observation = self._validate_observation(observation)
        if not valid_observation:
            self.current_action = None
            self.current_command_id = None
            self.command_started_at = None
            self.command_start_observation_key = None
            self.phase = PrimitivePhase.STOP_REQUESTED
            self.failure_reason = "observation_or_localization_not_ready"
            self.state = NavigationState.WAIT_RELOCALIZATION
            return self._stop(self.state)

        if self.state is NavigationState.WAIT_RELOCALIZATION:
            self.state = NavigationState.RESUMING
            self.phase = PrimitivePhase.IDLE
            self.last_observation_key = self._observation_key(observation)
            self.failure_reason = None
            return self._wait()
        if self.state is NavigationState.RESUMING:
            return self._wait()
        if self.awaiting_goal_observation:
            key = self._observation_key(observation)
            if key != self.last_observation_key and observation.target_visible and telemetry.stopped:
                self.last_observation_key = key
                self.state = NavigationState.ARRIVED
                self.phase = PrimitivePhase.CONFIRMED
            return self._stop(self.state) if self.state is NavigationState.ARRIVED else self._wait()

        if self.current_action is not None:
            assert self.command_started_at is not None
            if now - self.command_started_at > self.config.max_command_age_s:
                self.current_action = None
                self.current_command_id = None
                self.command_started_at = None
                self.command_start_observation_key = None
                self.phase = PrimitivePhase.FAILED
                return self._stop(NavigationState.REPLANNING, "primitive_timeout")
            if telemetry.motion_feedback == "executing":
                self.phase = PrimitivePhase.EXECUTING
            if self._confirm_current(observation, telemetry):
                return self._stop(self.state)
            if telemetry.stopped and telemetry.motion_feedback == "stopped":
                return self._wait()
            # Feedback saying executing is a valid in-flight state. The
            # command is not considered complete, but it is also not a
            # terminal fault merely because a visual frame is delayed.
            return self._wait()

        if self.state is NavigationState.WAITING_FOR_OBSERVATION:
            if self.last_observation_key == self._observation_key(observation):
                return self._wait()
            self.state = NavigationState.REPLANNING

        if self.index >= len(self.route.actions):
            self.state = NavigationState.ARRIVED
            return self._stop(self.state)
        if not telemetry.stopped:
            return self._stop(NavigationState.STOPPED, "new_primitive_requires_stopped_feedback")
        action = self.route.actions[self.index]
        if action.primitive not in _MOTION_PRIMITIVES:
            raise ValueError("unsupported_motion_primitive")
        issued = self._new_command(action, now)
        self.command_start_observation_key = self._observation_key(observation)
        if issued.primitive == "forward" and issued.cell is not None:
            self.pending_observation_target = issued.cell
        return issued

    def manual_override(self, reason: str):
        self.authorized = False
        self.current_action = None
        self.current_command_id = None
        self.phase = PrimitivePhase.FAILED
        self.failure_reason = reason
        self.last_command = MotionAction("stop")
        self.state = NavigationState.MANUAL_OVERRIDE
        return self.last_command
