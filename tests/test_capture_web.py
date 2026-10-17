"""Loopback HTTP integration tests; robot connection is replaced by a fake."""
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from go1loc.capture_web import CaptureWeb
from tests.test_capture_control import ManualSource
from tests.test_capture_session import frame


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = ManualSource()
        self.app = CaptureWeb(Path(self.temp.name) / 'unused.json', self.temp.name, port=0,
                              source_factory=lambda *_: self.source)
        self.app.start()
        self.port = self.app.server.server_port
        self.origin = f'http://127.0.0.1:{self.port}'
        status, headers, _ = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        status, _, body = self.request('GET', '/api/v1/bootstrap', auth=True)
        self.csrf = json.loads(body)['data']['csrf']

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def request(self, method, path, data=None, *, auth=False, headers=None):
        hdr = dict(headers or {})
        if auth:
            hdr['Cookie'] = self.cookie
        if method == 'POST':
            hdr.setdefault('Content-Type', 'application/json')
            hdr.setdefault('Origin', self.origin)
            hdr.setdefault('X-Go1-CSRF', getattr(self, 'csrf', ''))
        body = json.dumps(data).encode() if data is not None else None
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5.)
        try:
            conn.request(method, path, body, hdr)
            reply = conn.getresponse()
            return reply.status, dict(reply.getheaders()), reply.read()
        finally:
            conn.close()

    def action(self, action, **kwargs):
        code, _, body = self.request('POST', '/api/v1/actions', dict(action=action, **kwargs), auth=True)
        if code == 202:
            deadline = time.monotonic() + 5.
            while self.app.view()['busy'] and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(self.app.view()['busy'])
        return code, json.loads(body)

    def test_startup_static_assets_and_security(self):
        self.assertIsNone(self.app.controller.source)
        for route in ('/', '/app.js', '/style.css', '/icon.svg'):
            code, headers, _ = self.request('GET', route)
            self.assertEqual(code, 200)
            self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertEqual(self.request('GET', '/../../pyproject.toml')[0], 404)
        self.assertEqual(self.request('GET', '/api/v1/state')[0], 403)
        self.assertEqual(self.request('GET', '/', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.request('GET', '/', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.request('POST', '/api/v1/actions', {'action': 'disconnect'}, auth=True,
                                      headers={'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('POST', '/api/v1/actions', {'action': 'disconnect'}, auth=True,
                                      headers={'X-Go1-CSRF': 'wrong'})[0], 403)

    def test_manual_authorization_and_validation_before_launch(self):
        with patch.object(self.app, 'source_factory') as factory:
            self.assertEqual(self.action('connect', mode='robot', camera=1)[0], 403)
            self.assertEqual(self.action('connect', mode='demo', camera=99)[0], 422)
            self.assertEqual(self.action('motion')[0], 422)
            self.assertEqual(self.action('start', name='../escape')[0], 422)
            factory.assert_not_called()

    def test_fast_preview_remains_lossless_cached_and_identifies_actual_sample(self):
        import io
        from PIL import Image
        self.action('connect', mode='demo', camera=1)
        sample = frame()
        self.source.on_frame(sample)
        expected_key = sample.header['session_id'] + ':' + str(sample.header['agent_seq'])
        code, headers, payload = self.request('GET', '/api/v1/frame.png?sample=outdated', auth=True)
        self.assertEqual(code, 200)
        self.assertEqual(headers.get('X-Go1-Frame-Key'), expected_key)
        self.assertEqual(Image.open(io.BytesIO(payload)).tobytes(), sample.rgb().tobytes())
        with patch('go1loc.capture_web.Image.fromarray') as encode:
            self.assertEqual(self.app.preview(), payload)
            encode.assert_not_called()
        self.assertEqual(self.request('GET', '/api/v1/frame.png')[0], 403)
        self.assertEqual(self.request('GET', '/preview-timing.js')[0], 200)

    def test_session_renewal_requires_same_origin_header_and_never_connects(self):
        self.assertEqual(self.request('GET', '/api/v1/session')[0], 403)
        self.assertEqual(self.request('GET', '/api/v1/session', headers={
            'X-Go1-Session': 'renew', 'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.request('GET', '/api/v1/session', headers={
            'X-Go1-Session': 'renew', 'Origin': 'https://evil.example'})[0], 403)
        old_cookie, old_csrf = self.cookie, self.csrf
        self.app.token, self.app.csrf = 'b' * 48, 'c' * 48
        self.assertEqual(self.request('GET', '/api/v1/state', auth=True)[0], 403)
        code, headers, body = self.request('GET', '/api/v1/session',
                                         headers={'X-Go1-Session': 'renew', 'Cookie': old_cookie})
        self.assertEqual(code, 200)
        self.assertIn('HttpOnly; SameSite=Strict', headers['Set-Cookie'])
        self.cookie = headers['Set-Cookie'].split(';')[0]
        renewed = json.loads(body)['data']
        self.assertEqual(renewed['instance_id'], self.app.instance_id)
        self.assertNotEqual(renewed['csrf'], old_csrf)
        self.assertEqual(self.request('GET', '/api/v1/state', auth=True)[0], 200)
        # The old command token stays invalid, and renewal never opens a source.
        self.assertEqual(self.action('connect', mode='robot', camera=1, authorized=True)[0], 403)
        self.assertIsNone(self.app.controller.source)

    def test_record_snapshot_fault_and_no_automatic_resume(self):
        self.assertEqual(self.action('connect', mode='demo', camera=2)[0], 202)
        self.source.on_frame(frame(camera=2))
        self.assertEqual(self.action('start', name='test')[0], 202)
        self.source.on_frame(frame(2, camera=2))
        code, _, payload = self.request('GET', '/api/v1/frame.png', auth=True)
        self.assertEqual(code, 200)
        self.assertTrue(payload.startswith(b'\x89PNG'))
        timer = threading.Timer(.05, lambda: self.source.on_frame(frame(3, camera=2)))
        timer.start()
        self.action('snapshot', name='photo')
        timer.join()
        self.assertTrue(Path(self.app.view()['snapshot']).is_dir())
        self.source.on_end('frame_timeout_no_live_preview')
        state = self.app.view()
        self.assertEqual(state['state'], 'error')
        self.assertIsNone(state['frame_key'])
        self.assertEqual(self.request('GET', '/api/v1/frame.png', auth=True)[0], 204)
        self.assertEqual(json.loads((Path(state['output']) / 'session.json').read_text())['status'], 'interrupted')
        self.action('disconnect')
        self.action('connect', mode='demo', camera=1)
        self.assertIsNone(self.app.controller.recorder)

    def test_busy_error_and_shutdown(self):
        self.action('stop')
        self.assertIn('not_recording', self.app.view()['action_error'])
        gate = threading.Event()
        with patch.object(self.app.controller, 'disconnect', side_effect=lambda: gate.wait(2.)):
            self.request('POST', '/api/v1/actions', {'action': 'disconnect'}, auth=True)
            self.assertEqual(self.action('disconnect')[0], 409)
            gate.set()
            while self.app.view()['busy']:
                time.sleep(.01)
        self.assertEqual(self.action('shutdown')[0], 202)
        self.assertTrue(self.app.stop.is_set())

    def test_saved_folder_is_fixed_and_exports_use_existing_recording(self):
        with patch('go1loc.capture_web.open_directory') as opened:
            self.action('open-folder')
            opened.assert_called_once_with(Path(self.temp.name).resolve())
        self.action('export')
        self.assertIn('stop_recording_before_export', self.app.view()['action_error'])

    def test_bounded_requests_and_shutdown_rejection(self):
        self.assertEqual(self.request('GET', '/health')[0], 200)
        self.assertEqual(self.request('GET', '/api/v1/state', auth=True)[0], 200)
        self.assertEqual(self.request('GET', '/api/v1/state', headers={'Cookie': 'bad[cookie]=x'})[0], 403)
        self.assertEqual(self.request('POST', '/missing', {}, auth=True)[0], 404)
        self.assertEqual(self.request('POST', '/api/v1/actions', {}, auth=True,
                                      headers={'Content-Type': 'text/plain'})[0], 422)
        self.assertEqual(self.request('POST', '/api/v1/actions', {'name': 'x' * 4100}, auth=True)[0], 422)
        self.assertEqual(self.request('POST', '/api/v1/actions', [], auth=True)[0], 422)
        self.app.stop.set()
        self.assertEqual(self.action('disconnect')[0], 409)
        self.app.action_times.extend([time.monotonic()] * 60)
        self.assertEqual(self.action('disconnect')[0], 429)

    def test_real_adapter_is_only_constructed_after_explicit_authorization(self):
        self.app.source_factory = self.app._new_source
        with patch('go1loc.capture_web.load_connection', return_value={'fake': True}) as config, \
                patch('go1loc.capture_web.SSHSource', return_value=self.source) as ssh:
            self.action('connect', mode='robot', camera=3, authorized=True)
            config.assert_called_once_with(self.app.config_path, 3)
            ssh.assert_called_once_with({'fake': True}, 3, authorized=True)
            self.action('connect', mode='robot', camera=4, authorized=True)
            self.assertIn('disconnect_before_connecting_again', self.app.view()['action_error'])

    def test_normal_stop_export_and_io_errors(self):
        self.action('connect', mode='demo', camera=1)
        self.source.on_frame(frame())
        self.action('start', name='normal')
        self.source.on_frame(frame(2))
        self.action('stop')
        with patch('go1loc.capture_import.export_ffv1', return_value={'video': str(Path(self.temp.name) / 'video.mkv')}) as export:
            self.action('export')
            self.assertIsNone(self.app.view()['action_error'])
            self.assertTrue(self.app.view()['export'].endswith('video.mkv'))
            export.assert_called_once()
        with patch('go1loc.capture_web.open_directory', side_effect=OSError('test disk')):
            self.action('open-folder')
            self.assertIn('local_io_or_ssh', self.app.view()['action_error'])
        with patch('go1loc.capture_web.open_directory', side_effect=ZeroDivisionError('test unexpected')):
            self.action('open-folder')
            self.assertIn('internal_action', self.app.view()['action_error'])

    def test_synthetic_subprocess_preview(self):
        self.app.source_factory = self.app._new_source
        self.action('connect', mode='demo', camera=5)
        deadline = time.monotonic() + 5
        while not self.app.view()['frame_key'] and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(self.app.view()['state'], 'previewing')
        self.assertEqual(self.request('GET', '/api/v1/frame.png', auth=True)[0], 200)
        self.assertEqual(self.app.controller.latest.header['source_kind'], 'synthetic_test')


if __name__ == '__main__':
    unittest.main()
