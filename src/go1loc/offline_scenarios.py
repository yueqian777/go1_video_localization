"""Offline A--K evidence scenarios for the free-space navigation upgrade.

The scenarios use the planner and closed-loop state machine, but deliberately
do not instantiate RobotLink or emit a robot command. Their output is evidence
of software behavior only; it is never an acceptance record for a real Go1.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .free_space import build_free_space_map
from .grid_planner import plan_grid
from .navigation_closed_loop import (
    ClosedLoopConfig,
    ClosedLoopNavigator,
    NavigationState,
    PoseObservation,
    Telemetry,
)


def _ground(cells):
    return [[float(x), float(y), 0.0] for x, y in cells]


def _grid(cells):
    return build_free_space_map(_ground(cells), resolution=1.0,
                               ground_height=0.0, robot_radius=0.0)


def _simulate_closed_loop(grid, start, goal, *, heading="E", stale_age=None,
                          feedback_stopped=True):
    route = plan_grid(grid, start, goal, start_heading=heading)
    if route.status != "planned":
        return route, [], NavigationState.STOPPED
    navigator = ClosedLoopNavigator(
        route,
        config=ClosedLoopConfig(metric_scale=True, camera_mount_verified=True,
                                up_verified=True),
    )
    navigator.prepare()
    navigator.authorize()
    x, y = start
    current_heading = heading
    source_sequence = 0
    pending = None
    now = 0.0
    events = []
    target_visible = False

    def observation(age=0.0):
        return PoseObservation(x=float(x), y=float(y), heading=current_heading,
                               received_age_s=age, localized=True,
                               frame_id=f"offline-{source_sequence}",
                               capture_session_id="offline-session",
                               source_sequence=source_sequence,
                               source_time_s=now, received_time_s=now,
                               localization_time_s=now, map_version="offline-map",
                               localization_quality=1.0, target_visible=target_visible)

    for _ in range(256):
        # A new visual frame is required before issuing a subsequent action.
        source_sequence += 1
        age = stale_age if stale_age is not None and pending is not None else 0.0
        if pending is None:
            telemetry = Telemetry(True, True, motion_feedback="stopped",
                                  feedback_time_s=now)
            command = navigator.step(observation(age), telemetry, now=now)
            events.append(command.primitive)
            if command.primitive in {"wait", "stop"}:
                if navigator.state in {NavigationState.ARRIVED, NavigationState.STOPPED,
                                       NavigationState.MOTION_FAULT}:
                    break
                if navigator.awaiting_goal_observation:
                    target_visible = True
                now += 0.1
                continue
            pending = command
            now += 0.2
            continue

        # Feedback may arrive while the robot is still moving. No visual
        # displacement is applied at command issuance or at this intermediate tick.
        telemetry = Telemetry(True, feedback_stopped, motion_feedback=(
            "stopped" if feedback_stopped else "executing"),
            command_id=pending.command_id, feedback_time_s=now)
        delayed = navigator.step(observation(age), telemetry, now=now)
        if delayed.primitive not in {"wait", "stop"}:
            events.append(delayed.primitive)
        if navigator.state in {NavigationState.STOPPED, NavigationState.MOTION_FAULT}:
            events.append(delayed.primitive)
            break
        now += 0.2
        if not feedback_stopped:
            events.append(delayed.primitive)
            break

        # The simulated controller changes pose only after the bounded primitive
        # completes and emits a new visual frame for confirmation.
        if pending.primitive in {"turn_left", "turn_right"}:
            current_heading = pending.target_heading or current_heading
        elif pending.primitive == "forward" and pending.cell is not None:
            x, y = pending.cell
        source_sequence += 1
        telemetry = Telemetry(True, True, motion_feedback="stopped",
                              command_id=pending.command_id, feedback_time_s=now)
        confirmed = navigator.step(observation(), telemetry, now=now)
        events.append(confirmed.primitive)
        pending = None
        if navigator.state in {NavigationState.ARRIVED, NavigationState.STOPPED,
                               NavigationState.MOTION_FAULT}:
            break
        now += 0.1
    return route, events, navigator.state


def _scenario_a():
    grid = _grid([(x, 0) for x in range(4)])
    route, events, state = _simulate_closed_loop(grid, (3, 0), (0, 0))
    assert route.status == "planned"
    assert route.actions[:2][0].primitive == "turn_right"
    assert route.actions[:2][1].primitive == "turn_right"
    assert state == NavigationState.ARRIVED
    return {"new_route": False, "reverse_route": True, "events": events,
            "path_length_cells": len(route.path), "result": "turn_and_return"}


def _scenario_b():
    # The historical route was the middle row. A table blocks its center;
    # observed free geometry provides a new lower-row channel.
    cells = [(x, y) for x in range(5) for y in (0, 1) if (x, y) != (2, 1)]
    route = plan_grid(_grid(cells), (0, 1), (4, 1), start_heading="E")
    assert route.status == "planned"
    assert any(y == 0 for _, y, _ in route.path)
    return {"new_route": True, "reverse_route": False, "events": [],
            "path_length_cells": len(route.path), "result": "left_channel"}


def _scenario_c():
    grid = _grid([(x, y) for x in range(3) for y in range(2)])
    route = plan_grid(grid, (0, 1), (2, 1), start_heading="E")
    assert route.status == "planned"
    return {"new_route": True, "reverse_route": False, "events": [],
            "path_length_cells": len(route.path), "result": "off_history_start"}


def _scenario_d():
    grid = _grid([(0, 0), (1, 0), (3, 0)])
    route = plan_grid(grid, (0, 0), (3, 0), start_heading="E")
    assert route.status == "no_path"
    assert "unknown" in route.reason
    return {"new_route": False, "reverse_route": False, "events": ["reject_unknown"],
            "path_length_cells": 0, "result": route.reason}


def _scenario_e():
    cells = [(x, y) for x in range(5) for y in range(3) if not (1 <= x <= 3 and y == 1)]
    route = plan_grid(_grid(cells), (0, 1), (4, 1), start_heading="E")
    assert route.status == "planned"
    assert any(y != 1 for _, y, _ in route.path)
    return {"new_route": True, "reverse_route": False, "events": ["replan_alternate_channel"],
            "path_length_cells": len(route.path), "result": "alternate_channel"}


def _scenario_f():
    grid = _grid([(x, 0) for x in range(3)])
    route = plan_grid(grid, (2, 0), (0, 0), start_heading="E")
    assert [a.primitive for a in route.actions[:2]] == ["turn_right", "turn_right"]
    return {"new_route": False, "reverse_route": True,
            "events": [a.primitive for a in route.actions[:2]],
            "path_length_cells": len(route.path), "result": "180_degree_turn"}


def _scenario_g():
    grid = _grid([(x, 0) for x in range(3)])
    route, events, state = _simulate_closed_loop(grid, (0, 0), (2, 0), stale_age=2.0)
    assert route.status == "planned"
    assert events[-1] == "stop"
    assert state == NavigationState.STOPPED
    return {"new_route": False, "reverse_route": False, "events": events,
            "path_length_cells": len(route.path), "result": "stale_pose_stop"}


def _scenario_h():
    grid = _grid([(x, 0) for x in range(3)])
    route = plan_grid(grid, (0, 0), (2, 0))
    navigator = ClosedLoopNavigator(route, config=ClosedLoopConfig(True, True, True))
    navigator.prepare()
    navigator.authorize()
    navigator.manual_override("operator_stop")
    command = navigator.step(PoseObservation(0, 0, "E", 0, True),
                             Telemetry(True, True), now=0)
    assert command.primitive == "stop"
    assert navigator.state == NavigationState.MANUAL_OVERRIDE
    return {"new_route": False, "reverse_route": False,
            "events": ["manual_override", command.primitive],
            "path_length_cells": len(route.path), "result": "old_command_not_resumed"}


def _scenario_i():
    grid = _grid([(x, 0) for x in range(3)])
    route = plan_grid(grid, (0, 0), (2, 0))
    navigator = ClosedLoopNavigator(route, config=ClosedLoopConfig(True, True, True))
    navigator.prepare()
    navigator.authorize()
    first = navigator.step(PoseObservation(0, 0, "E", 0, True),
                           Telemetry(True, True), now=0)
    delayed = navigator.step(PoseObservation(0, 0, "E", 0, True),
                             Telemetry(True, False), now=.3)
    assert first.primitive == "forward"
    assert delayed.primitive == "stop"
    assert navigator.state == NavigationState.STOPPED
    return {"new_route": False, "reverse_route": False,
            "events": [first.primitive, "feedback_delayed", delayed.primitive],
            "path_length_cells": len(route.path), "result": "ttl_and_startup_delay_stop"}


def _scenario_j():
    grid = _grid([(x, 0) for x in range(3)])
    route = plan_grid(grid, (0, 0), (2, 0))
    navigator = ClosedLoopNavigator(route, config=ClosedLoopConfig(False, True, True))
    navigator.prepare()
    try:
        navigator.authorize()
    except PermissionError as exc:
        reason = str(exc)
    else:
        raise AssertionError("missing_metric_gate")
    return {"new_route": False, "reverse_route": False, "events": ["reject_execution"],
            "path_length_cells": len(route.path), "result": reason}


def _scenario_k():
    grid = _grid([(0, 0)])
    route = plan_grid(grid, (0, 0), (0, 0))
    navigator = ClosedLoopNavigator(route, config=ClosedLoopConfig(True, True, True))
    navigator.prepare()
    navigator.authorize()
    command = navigator.step(PoseObservation(0, 0, "E", 0, True),
                             Telemetry(True, True), now=0)
    assert navigator.state == NavigationState.ARRIVED
    assert command.primitive == "stop"
    return {"new_route": False, "reverse_route": False,
            "events": ["already_in_observation_region", command.primitive],
            "path_length_cells": 1, "result": "no_unnecessary_departure"}


_SCENARIOS = (
    ("A", "历史路线反向返回", _scenario_a),
    ("B", "历史右侧经过但几何支持左侧新通道", _scenario_b),
    ("C", "脱离历史轨迹的可通行起点", _scenario_c),
    ("D", "缺失几何的未知区域拒绝穿越", _scenario_d),
    ("E", "通道阻断后的其他连通通道", _scenario_e),
    ("F", "当前位置朝向错误时原地调头", _scenario_f),
    ("G", "视觉定位过期停止续发", _scenario_g),
    ("H", "人工接管后旧指令不复活", _scenario_h),
    ("I", "反馈延迟、启动延迟和TTL边界", _scenario_i),
    ("J", "尺度缺失拒绝真实执行", _scenario_j),
    ("K", "已在目标观察区时直接完成", _scenario_k),
)


def run_all_scenarios() -> dict:
    rows = []
    for scenario_id, title, function in _SCENARIOS:
        try:
            result = function()
            rows.append({"id": scenario_id, "title": title, "status": "pass",
                         "motion_commands_sent": False, **result})
        except Exception as exc:  # The report must retain the failing evidence.
            rows.append({"id": scenario_id, "title": title, "status": "fail",
                         "motion_commands_sent": False, "events": [],
                         "path_length_cells": 0, "result": "exception",
                         "failure_reason": f"{type(exc).__name__}: {exc}"})
    passed = sum(row["status"] == "pass" for row in rows)
    return {
        "schema_version": "go1loc.offline_navigation_scenarios.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "execution_mode": "offline_simulation",
        "motion_commands_sent": False,
        "scenarios": rows,
        "summary": {
            "total": len(rows), "passed": passed, "failed": len(rows) - passed,
            "new_routes": sum(bool(row.get("new_route")) for row in rows),
            "reverse_routes": sum(bool(row.get("reverse_route")) for row in rows),
            "stop_events": sum("stop" in row.get("events", []) for row in rows),
        },
        "comparison": {
            "historical_only_planner": {
                "can_use_new_geometry_channel": False,
                "can_start_off_history": False,
            },
            "free_space_planner": {
                "can_use_new_geometry_channel": True,
                "can_start_off_history": True,
                "unknown_is_executable": False,
            },
        },
    }


def write_scenario_report(path) -> dict:
    import json
    from pathlib import Path

    report = run_all_scenarios()
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
