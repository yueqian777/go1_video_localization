import math
import unittest

from go1loc.free_space import (
    CellState,
    build_free_space_map,
    free_space_from_payload,
    free_space_payload,
)
from go1loc.grid_planner import plan_grid
from go1loc.navigation_closed_loop import (
    ClosedLoopConfig,
    ClosedLoopNavigator,
    NavigationState,
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


class AdapterTests(unittest.TestCase):
    class Link:
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
            return {"connected": True, "robot": dict(self.robot)}

        def submit(self, action, key="", **kwargs):
            self.calls.append((action, key, kwargs))
            return None

    def test_adapter_uses_existing_one_axis_protocol(self):
        link = self.Link()
        adapter = Go1RemoteControlAdapter(link, authorization_token="test")
        adapter.authorize_current_task("test")
        adapter.execute_motion_primitive("forward")
        adapter.execute_motion_primitive("turn_left")
        adapter.request_stop("test")
        self.assertEqual([call[:2] for call in link.calls], [
            ("heartbeat", "w"), ("heartbeat", "z"), ("heartbeat", ""),
        ])

    def test_adapter_rejects_motion_without_authorization(self):
        link = self.Link()
        adapter = Go1RemoteControlAdapter(link, authorization_token="test")
        with self.assertRaises(PermissionError):
            adapter.execute_motion_primitive("forward")

    def test_adapter_uses_robotlink_top_level_freshness(self):
        link = self.Link()
        link.robot.pop("robot_fresh")
        original_snapshot = link.snapshot
        link.snapshot = lambda: {**original_snapshot(), "robot_fresh": True}
        adapter = Go1RemoteControlAdapter(link, authorization_token="test")
        adapter.authorize_current_task("test")
        adapter.execute_motion_primitive("forward")
        self.assertEqual(link.calls[-1][0:2], ("heartbeat", "w"))

        link.snapshot = lambda: {**original_snapshot(), "robot_fresh": False}
        with self.assertRaisesRegex(RuntimeError, "feedback"):
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
