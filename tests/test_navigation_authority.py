import pathlib
import sys
import time
import unittest
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pc'))
from server import App, make_handler


class FakeLink:
    def __init__(self):
        self.calls = []
        self.connected = True
        self.robot = {
            'control_active': True, 'armed': True, 'health': 'OK',
            'motion_stopped': True, 'motion_executing': False,
            'navigation_feedback_version': 1,
            'capabilities': ['motion_stopped', 'motion_executing',
                             'actual_yaw_rate', 'motion_command_seq',
                             'receiver_clock_id'],
        }
        self.navigation_intent = None
        self.navigation_feedback = {}

    def snapshot(self):
        return {'connected': self.connected, 'robot_fresh': True, 'robot': dict(self.robot),
                'navigation_intent_active': self.navigation_intent is not None,
                'navigation_command_id': self.navigation_intent[0] if self.navigation_intent else None,
                'navigation_feedback': dict(self.navigation_feedback),
                'recovery_required': False}

    def submit(self, action, key='', **kwargs):
        self.calls.append((action, key, kwargs))

    def submit_navigation_intent(self, key, command_id, ttl_s):
        self.navigation_intent = (command_id, key, ttl_s)
        self.navigation_feedback = {'command_id': command_id}
        self.calls.append(('navigation_intent', key, {'command_id': command_id, 'ttl_s': ttl_s}))

    def renew_navigation_intent(self, key, command_id, ttl_s):
        if not self.navigation_intent or self.navigation_intent[0] != command_id:
            raise ValueError('navigation_intent_invalid')
        self.navigation_intent = (command_id, key, ttl_s)
        self.calls.append(('navigation_renew', key, {'command_id': command_id, 'ttl_s': ttl_s}))

    def clear_navigation_intent(self, reason='navigation_stop', preserve_arm=False):
        had_intent = self.navigation_intent is not None
        self.navigation_intent = None
        self.calls.append(('stop', '', {'reason': reason}))
        return had_intent


class NavigationAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.app = App({'host': '127.0.0.1', 'port': 8765, 'token': '1' * 64,
                        'navigation_token': 'nav-secret', 'simulation': True}, 8099)
        self.app.link = FakeLink()

    def tearDown(self):
        self.app.navigation.close()

    def test_navigation_reuses_same_link_and_never_takes_or_arms(self):
        self.app.navigation.authorize('task-1', 'nav-secret')
        result = self.app.navigation.command('task-1', 'nav-secret', 'cmd-1', 'forward')
        self.assertTrue(result['accepted'])
        self.assertEqual(self.app.link.calls[0][0], 'navigation_intent')
        self.assertEqual(self.app.link.calls[0][1], 'w')
        with self.assertRaisesRegex(ValueError, 'duplicate_command_id'):
            self.app.navigation.command('task-1', 'nav-secret', 'cmd-1', 'forward')

    def test_task_switch_cannot_overwrite_active_navigation(self):
        self.app.navigation.authorize('task-1', 'nav-secret')
        with self.assertRaisesRegex(PermissionError, 'navigation_authority_conflict'):
            self.app.navigation.authorize('task-2', 'nav-secret')
        self.assertEqual(self.app.navigation.snapshot()['task_id'], 'task-1')

    def test_command_id_is_unique_for_the_whole_task(self):
        self.app.navigation.authorize('task-1', 'nav-secret')
        self.app.navigation.command('task-1', 'nav-secret', 'cmd-a', 'forward')
        self.app.link.clear_navigation_intent('simulated_stop')
        self.app.link.robot['motion_stopped'] = True
        self.app.link.navigation_feedback.update({
            'motion_started_time_s': 1.0, 'motion_stopped_time_s': 1.2,
        })
        self.app.navigation.command('task-1', 'nav-secret', 'cmd-b', 'forward')
        with self.assertRaisesRegex(ValueError, 'duplicate_command_id'):
            self.app.navigation.command('task-1', 'nav-secret', 'cmd-a', 'forward')

    def test_navigation_requires_healthy_robot_feedback(self):
        self.app.link.robot['health'] = 'STALE_STATE'
        with self.assertRaisesRegex(PermissionError, 'navigation_control_session_not_ready'):
            self.app.navigation.authorize('task-1', 'nav-secret')

    def test_keepalive_is_allowed_while_current_motion_is_executing(self):
        self.app.navigation.authorize('task-1', 'nav-secret')
        self.app.navigation.command('task-1', 'nav-secret', 'cmd-moving', 'forward')
        self.app.link.robot['motion_stopped'] = False
        self.app.link.robot['motion_executing'] = True
        result = self.app.navigation.keepalive('task-1', 'nav-secret')
        self.assertTrue(result['active'])
        self.assertTrue(self.app.link.navigation_intent is not None)
        self.assertEqual(self.app.link.navigation_intent[0], 'cmd-moving')

    def test_keepalive_cannot_refresh_revoked_or_disconnected_task(self):
        self.app.navigation.authorize('task-1', 'nav-secret')
        self.app.navigation.command('task-1', 'nav-secret', 'cmd-moving', 'forward')
        self.app.owner = ''
        with self.assertRaisesRegex(PermissionError, 'authority_revoked'):
            self.app.navigation.keepalive('task-1', 'nav-secret')
        self.app.owner = '__navigation__'
        self.app.link.connected = False
        with self.assertRaisesRegex(PermissionError, 'control_session_not_ready'):
            self.app.navigation.keepalive('task-1', 'nav-secret')

    def test_previous_motion_must_have_stop_feedback_before_switch(self):
        self.app.navigation.authorize('task-1', 'nav-secret')
        self.app.navigation.command('task-1', 'nav-secret', 'cmd-a', 'forward')
        self.app.link.robot['motion_stopped'] = False
        self.app.link.robot['motion_executing'] = True
        with self.assertRaisesRegex(PermissionError, 'motion_not_stopped'):
            self.app.navigation.command('task-1', 'nav-secret', 'cmd-b', 'turn_left')

    def test_old_receiver_feedback_is_rejected_for_navigation(self):
        self.app.link.robot['navigation_feedback_version'] = 0
        self.app.link.robot['capabilities'] = []
        with self.assertRaisesRegex(PermissionError, 'feedback_capabilities_required'):
            self.app.navigation.authorize('task-1', 'nav-secret')

    def test_stop_clears_intent_even_while_motion_is_active(self):
        self.app.navigation.authorize('task-1', 'nav-secret')
        self.app.navigation.command('task-1', 'nav-secret', 'cmd-moving', 'forward')
        self.app.link.robot['motion_stopped'] = False
        self.app.link.robot['motion_executing'] = True
        self.app.navigation.stop('task-1', 'nav-secret', 'operator_stop')
        self.assertFalse(self.app.navigation.snapshot()['active'])
        self.assertIsNone(self.app.link.navigation_intent)

    def test_pause_stops_intent_but_preserves_task_lease(self):
        self.app.navigation.authorize('task-1', 'nav-secret')
        self.app.navigation.command('task-1', 'nav-secret', 'cmd-a', 'forward')
        result = self.app.navigation.pause('task-1', 'nav-secret', 'visual_wait')
        self.assertTrue(result['paused'])
        state = self.app.navigation.snapshot()
        self.assertTrue(state['active'])
        self.assertTrue(state['paused'])
        self.assertEqual(self.app.link.calls[-1][0], 'stop')

    def test_keepalive_does_not_create_nonzero_intent(self):
        self.app.navigation.authorize('task-1', 'nav-secret')
        result = self.app.navigation.keepalive('task-1', 'nav-secret')
        self.assertTrue(result['active'])
        self.assertIsNone(self.app.link.navigation_intent)

    def test_resume_requires_a_new_motion_command(self):
        self.app.navigation.authorize('task-1', 'nav-secret')
        self.app.navigation.command('task-1', 'nav-secret', 'cmd-a', 'forward')
        self.app.navigation.pause('task-1', 'nav-secret')
        self.app.navigation.resume('task-1', 'nav-secret')
        self.assertFalse(self.app.link.navigation_intent is not None)
        with self.assertRaisesRegex(ValueError, 'duplicate_command_id'):
            self.app.navigation.command('task-1', 'nav-secret', 'cmd-a', 'forward')

    def test_manual_stop_revokes_navigation_lease(self):
        self.app.navigation.authorize('task-1', 'nav-secret')
        self.app.navigation.command('task-1', 'nav-secret', 'cmd-1', 'turn_left')
        body = {'action': 'stop', 'browser': 'a' * 32, 'seq': 1, 'tick': 0}
        self.app.control(body)
        self.assertFalse(self.app.navigation.snapshot()['active'])
        self.assertEqual(self.app.link.calls[-1][0], 'stop')

    def test_lease_expiry_requests_stop_and_clears_owner(self):
        self.app.navigation.authorize('task-1', 'nav-secret')
        self.app.navigation.lease_until = time.monotonic() - 1
        state = self.app.navigation.snapshot()
        self.assertFalse(state['active'])
        self.assertEqual(self.app.link.calls[-1][0], 'stop')

    def test_expired_task_cannot_be_revived_by_keepalive(self):
        self.app.navigation.authorize('task-1', 'nav-secret')
        self.app.navigation.lease_until = time.monotonic() - 1
        with self.assertRaisesRegex(PermissionError, 'lease_invalid'):
            self.app.navigation.keepalive('task-1', 'nav-secret')
        self.assertFalse(self.app.navigation.snapshot()['active'])
        self.assertIsNone(self.app.link.navigation_intent)

    def test_missing_navigation_token_does_not_enable_feature(self):
        app = App({'host': '127.0.0.1', 'port': 8765, 'token': '1' * 64,
                   'simulation': True}, 8099)
        app.link = FakeLink()
        with self.assertRaises(PermissionError):
            app.navigation.authorize('task-1', 'anything')

    def test_loopback_http_exposes_navigation_lease_without_new_transport(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.app))
        self.app.port = server.server_port
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        try:
            headers = {'Origin': base, 'X-Go1-Local-Token': self.app.token,
                       'X-Go1-Navigation-Token': 'nav-secret',
                       'Content-Type': 'application/json'}
            body = json.dumps({'task_id': 'task-http'}).encode()
            with urllib.request.urlopen(urllib.request.Request(
                    base + '/api/navigation/authorize', body, headers)) as response:
                self.assertTrue(json.load(response)['active'])
            body = json.dumps({'task_id': 'task-http', 'command_id': 'cmd-http',
                               'primitive': 'forward'}).encode()
            with urllib.request.urlopen(urllib.request.Request(
                    base + '/api/navigation/command', body, headers)) as response:
                self.assertTrue(json.load(response)['accepted'])
            self.assertEqual([call[0] for call in self.app.link.calls], ['navigation_intent'])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
