import math
import unittest

from go1loc.free_space import (
    CellState,
    build_free_space_map,
    free_space_diagnostics,
    free_space_from_payload,
    free_space_payload,
    inflate_obstacles,
)
from go1loc.grid_planner import PlanningMode, plan_grid
from go1loc.navigation_closed_loop import (
    ClosedLoopConfig,
    ClosedLoopNavigator,
    NavigationState,
    PrimitivePhase,
    PoseObservation,
    Telemetry,
)
from go1loc.remote_control_adapter import Go1RemoteControlAdapter


def ground_points(cells):
    return [[float(x), float(y), 0.0] for x, y in cells]


class FreeSpaceTests(unittest.TestCase):
    def test_missing_points_are_unknown_and_ground_support_is_free(self):
        result = build_free_space_map(
            ground_points([(0, 0)]) + [[1.0, 0.0, 1.0]],
            resolution=1.0,
            ground_height=0.0,
            ground_tolerance=0.08,
            obstacle_height=0.30,
        )
        self.assertEqual(result.cell(0, 0).state, CellState.FREE)
        self.assertEqual(result.cell(1, 0).state, CellState.OCCUPIED)
        self.assertEqual(result.cell(4, 4).state, CellState.UNKNOWN)

    def test_conflicting_ground_and_obstacle_evidence_is_uncertain(self):
        result = build_free_space_map(
            [[0.0, 0.0, 0.0], [0.0, 0.0, 0.9]],
            resolution=1.0,
            ground_height=0.0,
            ground_tolerance=0.08,
            obstacle_height=0.30,
        )
        self.assertEqual(result.cell(0, 0).state, CellState.UNCERTAIN)

    def test_history_only_adds_narrow_free_evidence(self):
        result = build_free_space_map(
            ground_points([(0, 0)]),
            resolution=1.0,
            ground_height=0.0,
            historical_corridor=[[(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]],
        )
        self.assertEqual(result.cell(1, 0).state, CellState.FREE)
        self.assertEqual(result.cell(2, 1).state, CellState.UNKNOWN)

    def test_free_space_payload_round_trips_without_execution_permission(self):
        grid = build_free_space_map(ground_points([(0, 0), (1, 0)]),
                                    resolution=0.5, ground_height=0.0)
        payload = free_space_payload(grid, map_id="m", map_hash="h", component_id="c0",
                                     units="map_units", scale_status="arbitrary_monocular_scale")
        restored = free_space_from_payload(payload)
        self.assertEqual(restored.cell(0, 0).state, CellState.FREE)
        self.assertEqual(payload["execution_permitted"], False)
        self.assertEqual(payload["map_hash"], "h")

    def test_nonzero_origin_and_resolution_have_one_coordinate_contract(self):
        result = build_free_space_map(
            [[10.15, 20.05, 0.0], [10.25, 20.05, 0.0]],
            resolution=0.1, origin=(10.0, 20.0), ground_height=0.0,
        )
        self.assertEqual(result.world_to_cell(10.15, 20.05), (1, 0))
        self.assertEqual(result.world_to_cell(10.25, 20.05), (2, 0))
        self.assertEqual(result.cell_to_world((1, 0)), (10.15, 20.05))
        self.assertEqual(result.key(10.25, 20.05), (2, 0))

    def test_footprint_inflation_blocks_a_cell_adjacent_to_obstacle(self):
        grid = build_free_space_map(
            ground_points([(x, 0) for x in (0, 1, 3, 4)]) + [[2.0, 0.0, 1.0]],
            resolution=1.0, ground_height=0.0,
        )
        inflated = inflate_obstacles(grid, radius=1.0)
        self.assertEqual(inflated.cell(1, 0).state, CellState.OCCUPIED)
        self.assertIn("inflated_obstacle", inflated.cell(1, 0).evidence)

    def test_diagnostics_marks_monocular_sidecar_preview_only(self):
        grid = build_free_space_map(ground_points([(0, 0), (1, 0)]),
                                    resolution=1.0, ground_height=0.0)
        diagnostics = free_space_diagnostics(
            grid, units="map_units", scale_status="arbitrary_monocular_scale",
            footprint_radius=0.25)
        self.assertTrue(diagnostics["preview_only"])
        self.assertFalse(diagnostics["execution_geometry_ready"])
        self.assertEqual(diagnostics["cell_counts"]["free"], 2)

    def test_diagnostics_reports_unknown_hole_candidates(self):
        ring = [(x, y) for x in range(3) for y in range(3) if (x, y) != (1, 1)]
        grid = build_free_space_map(ground_points(ring), resolution=1.0, ground_height=0.0)
        diagnostics = free_space_diagnostics(grid, units="m", scale_status="metric")
        self.assertEqual(diagnostics["unknown_hole_components"], 1)
        self.assertEqual(diagnostics["unknown_hole_cells"], 1)


class GridPlannerTests(unittest.TestCase):
    def setUp(self):
        cells = [(x, y) for x in range(5) for y in range(3)]
        self.grid = build_free_space_map(
            ground_points(cells),
            resolution=1.0,
            ground_height=0.0,
            robot_radius=0.0,
        )

    def test_plans_new_left_route_without_history_edges(self):
        route = plan_grid(self.grid, (0, 1), (4, 1), start_heading="E")
        self.assertEqual(route.status, "planned")
        self.assertNotEqual(route.source, "historical_route")
        self.assertEqual(route.path[0][:2], (0, 1))
        self.assertEqual(route.path[-1][:2], (4, 1))

    def test_unknown_cells_are_not_treated_as_free(self):
        cells = [(0, 0), (1, 0), (3, 0)]
        grid = build_free_space_map(ground_points(cells), resolution=1.0, ground_height=0.0)
        route = plan_grid(grid, (0, 0), (3, 0), start_heading="E")
        self.assertEqual(route.status, "no_path")
        self.assertIn("unknown", route.reason)

    def test_180_degree_return_contains_turn_before_forward(self):
        route = plan_grid(self.grid, (3, 1), (0, 1), start_heading="E")
        self.assertEqual(route.status, "planned")
        self.assertEqual(route.actions[0].primitive, "turn_right")
        self.assertEqual(route.actions[1].primitive, "turn_right")
        self.assertIn("forward", [action.primitive for action in route.actions])

    def test_blocked_channel_can_use_other_connected_space(self):
        cells = [(x, y) for x in range(5) for y in range(3) if not (1 <= x <= 3 and y == 1)]
        grid = build_free_space_map(ground_points(cells), resolution=1.0, ground_height=0.0)
        route = plan_grid(grid, (0, 1), (4, 1), start_heading="E")
        self.assertEqual(route.status, "planned")
        self.assertTrue(any(y != 1 for _, y, _ in route.path))

    def test_uncertain_cell_is_rejected(self):
        cells = [(x, 0) for x in range(3)]
        points = ground_points(cells) + [[1.0, 0.0, 0.9]]
        grid = build_free_space_map(points, resolution=1.0, ground_height=0.0)
        route = plan_grid(grid, (0, 0), (2, 0), start_heading="E")
        self.assertEqual(route.status, "no_path")
        self.assertIn("uncertain", route.reason)

    def test_allow_unknown_never_allows_occupied_cell(self):
        grid = build_free_space_map(
            ground_points([(0, 0), (2, 0)]) + [[1.0, 0.0, 0.9]],
            resolution=1.0, ground_height=0.0,
        )
        for mode in (PlanningMode.NORMAL, PlanningMode.EXPLORATION_CANDIDATE):
            with self.subTest(mode=mode):
                route = plan_grid(grid, (0, 0), (2, 0), planning_mode=mode)
                if mode is PlanningMode.NORMAL:
                    self.assertEqual(route.status, "no_path")
                else:
                    self.assertEqual(route.status, "exploration_candidate")
                    self.assertTrue(route.requires_exploration)
                self.assertNotIn((1, 0), [state[:2] for state in route.path])

    def test_allow_unknown_uses_explicit_exploration_status(self):
        grid = build_free_space_map(ground_points([(0, 0), (2, 0)]),
                                    resolution=1.0, ground_height=0.0)
        route = plan_grid(grid, (0, 0), (2, 0), planning_mode=PlanningMode.EXPLORATION_CANDIDATE)
        self.assertEqual(route.status, "exploration_candidate")
        self.assertTrue(route.requires_exploration)
        self.assertIn("unknown", route.reason)


class AdapterTests(unittest.TestCase):
    class Authority:
        def __init__(self):
            self.calls = []
            self.robot = {
                "control_active": True,
                "armed": True,
                "health": "OK",
                "robot_fresh": True,
                "avoidance_selected": False,
                "actual_vx": 0.0,
                "actual_vy": 0.0,
                "actual_yaw": 0.0,
            }

        def snapshot(self):
            return {"connected": True, "robot_fresh": self.robot.get("robot_fresh", True),
                    "robot": dict(self.robot)}

        def authorize_navigation(self, task_id, token):
            self.calls.append(("authorize", task_id, token))
            return {"active": True}

        def submit_navigation(self, task_id, token, command_id, primitive, ttl_s=None):
            self.calls.append(("command", primitive, command_id))
            return {"accepted": True, "command_id": command_id}

        def stop_navigation(self, task_id, token, reason):
            self.calls.append(("stop", reason))
            return {"accepted": True, "primitive": "stop"}

        def release_navigation(self, task_id, token):
            self.calls.append(("release", task_id))
            return {"accepted": True}

    def test_adapter_uses_existing_one_axis_protocol(self):
        link = self.Authority()
        adapter = Go1RemoteControlAdapter(link, authorization_token="test")
        adapter.authorize_current_task("test")
        adapter.execute_motion_primitive("forward")
        adapter.execute_motion_primitive("turn_left")
        adapter.request_stop("test")
        self.assertEqual([call[0] for call in link.calls], ["authorize", "command", "command", "stop"])

    def test_adapter_rejects_motion_without_authorization(self):
        link = self.Authority()
        adapter = Go1RemoteControlAdapter(link, authorization_token="test")
        with self.assertRaises(PermissionError):
            adapter.execute_motion_primitive("forward")

    def test_adapter_uses_robotlink_top_level_freshness(self):
        link = self.Authority()
        link.robot.pop("robot_fresh")
        original_snapshot = link.snapshot
        link.snapshot = lambda: {**original_snapshot(), "robot_fresh": True}
        adapter = Go1RemoteControlAdapter(link, authorization_token="test")
        adapter.authorize_current_task("test")
        adapter.execute_motion_primitive("forward")
        self.assertEqual(link.calls[-1][0], "command")

        link.snapshot = lambda: {**original_snapshot(), "robot_fresh": False}
        with self.assertRaisesRegex(RuntimeError, "session_lost"):
            adapter.execute_motion_primitive("forward")


class ClosedLoopTests(unittest.TestCase):
    def setUp(self):
        cells = [(x, 0) for x in range(4)]
        self.grid = build_free_space_map(ground_points(cells), resolution=1.0, ground_height=0.0)
        self.route = plan_grid(self.grid, (0, 0), (3, 0), start_heading="E")

    def observation(self, x, heading="E", age=0.0):
        return PoseObservation(x=x, y=0.0, heading=heading, received_age_s=age, localized=True)

    def test_closed_loop_emits_segment_then_waits_for_observation(self):
        navigator = ClosedLoopNavigator(
            self.route,
            config=ClosedLoopConfig(metric_scale=True, camera_mount_verified=True, up_verified=True),
        )
        navigator.prepare()
        navigator.authorize()
        command = navigator.step(self.observation(0), Telemetry(healthy=True, stopped=True), now=0.0)
        self.assertEqual(navigator.state, NavigationState.MOVING_SEGMENT)
        self.assertEqual(command.primitive, "forward")
        navigator.step(self.observation(1), Telemetry(healthy=True, stopped=True), now=1.0)
        self.assertEqual(navigator.state, NavigationState.WAITING_FOR_OBSERVATION)

    def test_stale_pose_requests_stop_and_never_renews_motion(self):
        navigator = ClosedLoopNavigator(
            self.route,
            config=ClosedLoopConfig(metric_scale=True, camera_mount_verified=True, up_verified=True),
        )
        navigator.prepare()
        navigator.authorize()
        navigator.step(self.observation(0), Telemetry(healthy=True, stopped=True), now=0.0)
        command = navigator.step(self.observation(0, age=2.0), Telemetry(healthy=True, stopped=False), now=1.0)
        self.assertEqual(navigator.state, NavigationState.STOPPED)
        self.assertEqual(command.primitive, "stop")

    def test_bad_scale_blocks_authorized_motion(self):
        navigator = ClosedLoopNavigator(
            self.route,
            config=ClosedLoopConfig(metric_scale=False, camera_mount_verified=True, up_verified=True),
        )
        navigator.prepare()
        with self.assertRaises(PermissionError):
            navigator.authorize()
        self.assertEqual(navigator.state, NavigationState.PREPARED)

    def test_manual_override_stops_and_cannot_resume_old_command(self):
        navigator = ClosedLoopNavigator(
            self.route,
            config=ClosedLoopConfig(metric_scale=True, camera_mount_verified=True, up_verified=True),
        )
        navigator.prepare()
        navigator.authorize()
        navigator.step(self.observation(0), Telemetry(healthy=True, stopped=True), now=0.0)
        command = navigator.manual_override("handheld_stop")
        self.assertEqual(command.primitive, "stop")
        self.assertEqual(navigator.state, NavigationState.MANUAL_OVERRIDE)
        self.assertEqual(navigator.step(self.observation(0), Telemetry(healthy=True, stopped=True), now=1.0).primitive, "stop")

    def test_feedback_fault_stops_without_declaring_arrival(self):
        navigator = ClosedLoopNavigator(
            self.route,
            config=ClosedLoopConfig(metric_scale=True, camera_mount_verified=True, up_verified=True),
        )
        navigator.prepare()
        navigator.authorize()
        navigator.step(self.observation(0), Telemetry(healthy=True, stopped=True), now=0.0)
        command = navigator.step(self.observation(0), Telemetry(healthy=False, stopped=False, fault="STALE_STATE"), now=1.0)
        self.assertEqual(command.primitive, "stop")
        self.assertEqual(navigator.state, NavigationState.MOTION_FAULT)


class ClosedLoopDeepRegressionTests(unittest.TestCase):
    def setUp(self):
        self.grid = build_free_space_map(
            [[10.15, 20.05, 0.0], [10.25, 20.05, 0.0],
             [10.15, 20.15, 0.0], [10.25, 20.15, 0.0]],
            resolution=0.1, origin=(10.0, 20.0), ground_height=0.0,
        )
        self.config = ClosedLoopConfig(
            metric_scale=True, camera_mount_verified=True, up_verified=True,
            map_version="map-v1", max_command_age_s=1.0,
        )

    def obs(self, x, y, heading, frame, *, target_visible=False, quality=0.9):
        return PoseObservation(
            x=x, y=y, heading=heading, received_age_s=0.0, localized=True,
            frame_id=frame, capture_session_id="session-a", source_sequence=int(frame[1:]),
            source_time_s=float(int(frame[1:])), received_time_s=float(int(frame[1:])) + .01,
            localization_time_s=float(int(frame[1:])) + .005, map_version="map-v1",
            localization_quality=quality, target_visible=target_visible,
        )

    def test_same_frame_cannot_complete_forward_twice(self):
        route = plan_grid(self.grid, (1, 0), (2, 0))
        navigator = ClosedLoopNavigator(route, config=self.config, grid=self.grid)
        navigator.prepare(); navigator.authorize()
        first = navigator.step(self.obs(10.15, 20.05, "E", "f1"),
                               Telemetry(True, True), now=0.0)
        self.assertEqual(first.primitive, "forward")
        repeated = navigator.step(self.obs(10.25, 20.05, "E", "f1", target_visible=True),
                                  Telemetry(True, True, command_id=first.command_id,
                                            motion_feedback="stopped"), now=.2)
        self.assertIn(repeated.primitive, ("wait", "stop"))
        self.assertEqual(navigator.index, 0)
        confirmed = navigator.step(self.obs(10.25, 20.05, "E", "f2", target_visible=True),
                                   Telemetry(True, True, command_id=first.command_id,
                                             motion_feedback="stopped", feedback_time_s=.3), now=.3)
        self.assertEqual(navigator.index, 1)
        self.assertNotEqual(confirmed.primitive, "forward")

    def test_turn_requires_new_frame_and_absolute_heading_before_next_action(self):
        route = plan_grid(self.grid, (1, 0), (1, 1), start_heading="E")
        navigator = ClosedLoopNavigator(route, config=self.config, grid=self.grid)
        navigator.prepare(); navigator.authorize()
        turn = navigator.step(self.obs(10.15, 20.05, "E", "f1"),
                               Telemetry(True, True), now=0.0)
        self.assertEqual(turn.primitive, "turn_left")
        self.assertEqual(navigator.phase, PrimitivePhase.REQUESTED)
        same_frame = navigator.step(self.obs(10.15, 20.05, "N", "f1"),
                                    Telemetry(True, True, command_id=turn.command_id,
                                              motion_feedback="stopped"), now=.2)
        self.assertIn(same_frame.primitive, ("wait", "stop"))
        self.assertEqual(navigator.index, 0)
        confirmed = navigator.step(self.obs(10.15, 20.05, "N", "f2"),
                                   Telemetry(True, True, command_id=turn.command_id,
                                              motion_feedback="stopped", feedback_time_s=.3), now=.3)
        self.assertEqual(navigator.index, 1)
        self.assertNotEqual(confirmed.primitive, "turn_left")

    def test_command_timeout_enters_replanning_without_consuming_action(self):
        route = plan_grid(self.grid, (1, 0), (2, 0))
        navigator = ClosedLoopNavigator(route, config=self.config, grid=self.grid)
        navigator.prepare(); navigator.authorize()
        command = navigator.step(self.obs(10.15, 20.05, "E", "f1"),
                                 Telemetry(True, True), now=0.0)
        stopped = navigator.step(self.obs(10.15, 20.05, "E", "f2"),
                                 Telemetry(True, False, command_id=command.command_id,
                                           motion_feedback="executing"), now=2.0)
        self.assertEqual(stopped.primitive, "stop")
        self.assertEqual(navigator.state, NavigationState.REPLANNING)
        self.assertEqual(navigator.index, 0)

    def test_goal_requires_target_observation_before_arrived(self):
        route = plan_grid(self.grid, (1, 0), (2, 0))
        navigator = ClosedLoopNavigator(route, config=self.config, grid=self.grid)
        navigator.prepare(); navigator.authorize()
        command = navigator.step(self.obs(10.15, 20.05, "E", "f1"),
                                 Telemetry(True, True), now=0.0)
        waiting = navigator.step(self.obs(10.25, 20.05, "E", "f2"),
                                 Telemetry(True, True, command_id=command.command_id,
                                           motion_feedback="stopped"), now=.3)
        self.assertNotEqual(navigator.state, NavigationState.ARRIVED)
        self.assertIn(waiting.primitive, ("wait", "stop"))


class ScenarioTests(unittest.TestCase):
    def test_all_offline_scenarios_pass_and_never_send_robot_motion(self):
        from go1loc.offline_scenarios import run_all_scenarios

        report = run_all_scenarios()
        self.assertEqual(report["summary"]["failed"], 0)
        self.assertEqual(len(report["scenarios"]), 11)
        self.assertFalse(report["motion_commands_sent"])

    def test_delayed_feedback_does_not_claim_arrival(self):
        from go1loc.offline_scenarios import run_all_scenarios

        report = run_all_scenarios()
        scenario = next(row for row in report["scenarios"] if row["id"] == "I")
        self.assertEqual(scenario["status"], "pass")
        self.assertIn("stop", scenario["events"])



if __name__ == "__main__":
    unittest.main()
