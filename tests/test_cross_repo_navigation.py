"""Cross-repository loopback test: native receiver + PC service + vision client."""
import os
import pathlib
import sys
import time
import unittest
from http.server import ThreadingHTTPServer
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
VISION = ROOT.parent.parent / "go1_video_localization"
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "pc"))
sys.path.insert(0, str(VISION / "src"))

from test_system import BINARY, receiver
from server import App, make_handler
from go1loc.remote_control_adapter import Go1RemoteControlAdapter, NavigationHttpClient


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(.03)
    raise AssertionError(f"condition timed out: {last!r}")


@unittest.skipUnless(BINARY.exists() or os.environ.get("GO1_SIM_BINARY"),
                     "native Go1 simulator binary is required")
class CrossRepositoryNavigationTests(unittest.TestCase):
    def test_visual_http_client_observes_bounded_intent_and_recovery(self):
        with receiver() as (_, robot_port, robot_token, connection):
            connection.close()
            # The native simulator intentionally serves one client at a time;
            # let it observe the probe close before the PC service connects.
            time.sleep(.15)
            app = App({"host": "127.0.0.1", "port": robot_port,
                       "token": robot_token, "navigation_token": "nav-secret",
                       "simulation": True}, 0)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
            app.port = server.server_port
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f"http://127.0.0.1:{server.server_port}"
            client = NavigationHttpClient(base, local_token=app.token,
                                          navigation_token="nav-secret")
            adapter = Go1RemoteControlAdapter(client, authorization_token="nav-secret",
                                              task_id="integration-task")
            try:
                app.link.connect()
                wait_until(lambda: app.link.snapshot()["connected"])
                # TAKE/ARM are explicit test setup, never performed by the
                # navigation adapter itself.
                app.link.submit("take", speed=.2, lateral=.1, turn=.1, standing=True)
                wait_until(lambda: app.link.snapshot()["robot"].get("control_active"))
                time.sleep(.7)  # native receiver's healthy stationary ARM gate
                app.link.submit("arm")
                wait_until(lambda: app.link.snapshot()["robot"].get("armed"))

                adapter.authorize_current_task("nav-secret")
                first = adapter.execute_motion_primitive("forward", command_id="integration-cmd-1",
                                                         ttl_s=1.0)
                self.assertEqual(first["command_id"], "integration-cmd-1")
                state = wait_until(lambda: client.snapshot()
                                    if client.snapshot()["navigation"]["motion_intent_active"] else None)
                def causal_snapshot():
                    candidate = client.snapshot()
                    telemetry = adapter.telemetry_from_snapshot(
                        candidate, expected_command_id="integration-cmd-1")
                    return candidate if (candidate["robot"].get("motion_executing")
                                         and telemetry.causal_feedback) else None
                moving = wait_until(causal_snapshot)
                for _ in range(3):
                    self.assertTrue(adapter.keepalive()["active"])
                    time.sleep(.08)
                self.assertTrue(moving["robot"]["motion_executing"])
                telemetry = adapter.telemetry_from_snapshot(moving,
                                                             expected_command_id="integration-cmd-1")
                self.assertEqual(telemetry.command_id, "integration-cmd-1")
                self.assertTrue(telemetry.causal_feedback)
                self.assertEqual(telemetry.motion_feedback, "executing")
                self.assertEqual(moving["navigation_feedback"]["command_id"], "integration-cmd-1")
                self.assertEqual(moving["robot"]["motion_command_id"], "integration-cmd-1")

                paused = adapter.pause("temporary_visual_gap")
                self.assertTrue(paused["paused"])
                paused_state = wait_until(lambda: client.snapshot()
                                          if client.snapshot()["navigation"]["paused"]
                                          and not client.snapshot()["navigation"]["motion_intent_active"]
                                          and client.snapshot()["robot"].get("motion_stopped") is True
                                          else None)
                paused_telemetry = adapter.telemetry_from_snapshot(paused_state)
                self.assertTrue(paused_telemetry.navigation_paused)
                self.assertFalse(paused_telemetry.motion_executing is True)

                adapter.resume()
                resumed = client.snapshot()["navigation"]
                self.assertTrue(resumed["active"])
                self.assertFalse(resumed["paused"])
                self.assertFalse(resumed["motion_intent_active"])

                second = adapter.execute_motion_primitive("turn_left", command_id="integration-cmd-2",
                                                          ttl_s=.8)
                self.assertEqual(second["command_id"], "integration-cmd-2")
                expired = wait_until(lambda: client.snapshot()
                                      if not client.snapshot()["navigation"]["motion_intent_active"]
                                      and client.snapshot()["navigation_intent_expirations"] >= 1
                                      else None, timeout=3.0)
                self.assertFalse(expired["navigation"]["motion_intent_active"])
                self.assertEqual(adapter.request_stop("integration_stop")["primitive"], "stop")
                self.assertFalse(client.snapshot()["navigation"]["active"])
            finally:
                if adapter.authorized:
                    try:
                        adapter.request_stop("test_cleanup")
                    except Exception:
                        pass
                server.shutdown()
                server.server_close()
                worker.join(2)
                app.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
