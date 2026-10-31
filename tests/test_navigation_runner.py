import unittest

from go1loc.free_space import build_free_space_map
from go1loc.grid_planner import plan_grid
from go1loc.navigation_closed_loop import (
    CalibrationEvidence,
    ClosedLoopConfig,
    ClosedLoopNavigator,
    PoseObservation,
    Telemetry,
)
from go1loc.navigation_runner import NavigationRunner, RunnerConfig, RunnerMode


class FakeAdapter:
    def __init__(self, simulation=True, fail_motion=False):
        self.calls = []
        self.authorized = False
        self.authorization_token = "token"
        self.simulation = simulation
        self.fail_motion = fail_motion

    def snapshot(self):
        return {"simulation": self.simulation}

    def authorize_current_task(self, token):
        self.calls.append("authorize")
        self.authorized = True

    def execute_motion_action(self, action):
        self.calls.append(("motion", action.command_id, action.primitive))
        if self.fail_motion:
            raise RuntimeError("simulated_control_failure")
        return {"accepted": True, "command_id": action.command_id}

    def pause(self, reason):
        self.calls.append(("pause", reason))
        return {"paused": True}

    def resume(self):
        self.calls.append("resume")
        return {"active": True, "paused": False}

    def request_stop(self, reason):
        self.calls.append(("stop", reason))
        self.authorized = False
        return {"accepted": True}


def route_and_navigator():
    grid = build_free_space_map([[0, 0, 0], [1, 0, 0]], resolution=1.0,
                                ground_height=0.0, robot_radius=0.0)
    route = plan_grid(grid, (0, 0), (1, 0), start_heading="E")
    navigator = ClosedLoopNavigator(
        route,
        config=ClosedLoopConfig(metric_scale=True, camera_mount_verified=True,
                                up_verified=True, max_forward_cells=1),
        grid=grid,
    )
    navigator.prepare()
    return navigator


class NavigationRunnerTests(unittest.TestCase):
    def test_default_shadow_startup_is_inert(self):
        adapter = FakeAdapter()
        runner = NavigationRunner(route_and_navigator(), adapter=adapter)
        status = runner.startup()
        self.assertEqual(status["mode"], "SHADOW")
        self.assertEqual(adapter.calls, [])

    def test_shadow_runs_one_action_without_http_submission(self):
        adapter = FakeAdapter()
        runner = NavigationRunner(route_and_navigator(), adapter=adapter)
        runner.authorize()
        observation = PoseObservation(0, 0, "E", 0.0, True,
                                      capture_time_s=1.0, capture_clock_id="sim-clock",
                                      capture_time_uncertainty_s=.01)
        action = runner.step(observation, Telemetry(True, True), now=0.0)
        self.assertEqual(action.primitive, "forward")
        self.assertEqual(adapter.calls, [])

    def test_live_requires_operator_and_calibration_evidence(self):
        evidence = CalibrationEvidence(1.0, True, True, True, "field-20261030")
        navigator = route_and_navigator()
        adapter = FakeAdapter(simulation=False)
        runner = NavigationRunner(
            navigator, adapter=adapter,
            config=RunnerConfig(RunnerMode.LIVE_SUPERVISED, False, evidence),
        )
        with self.assertRaisesRegex(PermissionError, "operator_authorization"):
            runner.authorize()

    def test_simulation_authorization_does_not_take_or_arm(self):
        adapter = FakeAdapter(simulation=True)
        runner = NavigationRunner(route_and_navigator(), adapter=adapter,
                                  config=RunnerConfig(RunnerMode.SIMULATION))
        runner.authorize()
        self.assertEqual(adapter.calls, ["authorize"])
        self.assertNotIn("take", adapter.calls)
        self.assertNotIn("arm", adapter.calls)

    def test_motion_request_failure_pauses_and_requests_stop(self):
        adapter = FakeAdapter(fail_motion=True)
        runner = NavigationRunner(
            route_and_navigator(), adapter=adapter,
            config=RunnerConfig(RunnerMode.SIMULATION),
        )
        runner.authorize()
        observation = PoseObservation(0, 0, "E", 0.0, True,
                                      capture_time_s=1.0, capture_clock_id="sim-clock",
                                      capture_time_uncertainty_s=.01)
        with self.assertRaisesRegex(RuntimeError, "simulated_control_failure"):
            runner.step(observation, Telemetry(True, True), now=0.0)
        self.assertEqual(adapter.calls[0], "authorize")
        self.assertEqual(adapter.calls[1][0], "motion")
        self.assertEqual(adapter.calls[2][0], "stop")
        self.assertEqual(runner.navigator.state.value, "Paused")


if __name__ == "__main__":
    unittest.main(verbosity=2)
