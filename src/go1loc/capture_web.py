"""Local-only camera dashboard. No connection, installation or motion on startup."""
from __future__ import annotations

import io
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image

from .capture_control import CaptureController
from .capture_transport import PipeSource, SSHSource, load_connection
from .capture_ui import health_message

ASSETS = Path(__file__).with_name('capture_frontend')
CAMERAS = [(1, '前视相机', 'FRONT'), (2, '下巴相机', 'CHIN'), (3, '左侧相机', 'LEFT'),
           (4, '右侧相机', 'RIGHT'), (5, '后下相机', 'REAR / DOWN'),
           (6, 'Gemini 335L RGB', '720P / 15 FPS')]


def open_directory(path):
    if os.name == 'nt':
        os.startfile(str(path))
    else:
        subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(path)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class LimitedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(*args)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class CaptureWeb:
    def __init__(self, config_path, output, *, port=8772, source_factory=None):
        self.config_path, self.output = Path(config_path).resolve(), Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.controller = CaptureController()
        self.source_factory = source_factory or self._new_source
        self.camera, self.mode = 1, 'robot'
        self.snapshot_path = self.export_path = None
        self.record_started = None
        self.action_error = None
        self.action_name = None
        self.busy = threading.Lock()
        self.stop = threading.Event()
        self.worker = self.thread = None
        self.workspace = None
        self.token, self.csrf, self.instance_id = (secrets.token_hex(24) for _ in range(3))
        self.image_lock = threading.Lock()
        self.image_key, self.image_bytes = None, None
        self.action_times = deque(maxlen=60)
        self.rate_lock = threading.Lock()
        self.server = LimitedHTTPServer(('127.0.0.1', port), self._handler())
        self.url = 'http://127.0.0.1:%d/' % self.server.server_port
        self.origin = self.url.rstrip('/')
        self.cookie_name = 'go1_capture_' + str(self.server.server_port)

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name='capture-local-http')
        self.thread.start()

    def _new_source(self, mode, camera):
        if mode == 'demo':
            # pythonw is suitable for the desktop launcher, not a binary stdout pipe.
            executable = Path(sys.executable)
            if executable.name.lower() == 'pythonw.exe':
                executable = executable.with_name('python.exe')
            return PipeSource([str(executable), '-m', 'go1loc.capture_synthetic', '--camera', str(camera),
                               '--seconds', '300'], camera, source_kind='synthetic_test')
        return SSHSource(load_connection(self.config_path, camera), camera, authorized=True)

    def view(self):
        status, frame = self.controller.view()
        metadata = frame.header if frame else {}
        status.update(mode=self.mode, camera=self.camera, busy=self.busy.locked(), action=self.action_name,
                      action_error=self.action_error, snapshot=self.snapshot_path, export=self.export_path,
                      save_directory=str(self.output), config_path=str(self.config_path),
                      frame_key=(metadata.get('session_id', '') + ':' + str(metadata.get('agent_seq'))) if frame else None,
                      width=metadata.get('width'), height=metadata.get('height'),
                      recording_seconds=max(0., time.monotonic() - self.record_started)
                      if self.record_started and status['state'] == 'recording' else 0.)
        status['health_message'] = health_message(status)
        status.pop('source_metadata', None)
        return status

    def preview(self):
        return self.preview_sample()[0]

    def preview_sample(self):
        _, frame = self.controller.view()
        if frame is None:
            return None, None
        key = (frame.header['session_id'], frame.header['agent_seq'])
        with self.image_lock:
            if key != self.image_key:
                if frame.preview_jpeg is not None:
                    self.image_bytes = frame.preview_jpeg
                else:
                    image = io.BytesIO()
                    Image.fromarray(frame.rgb()).save(image, format='PNG', compress_level=1)
                    self.image_bytes = image.getvalue()
                self.image_key = key
            return self.image_bytes, key[0] + ':' + str(key[1])

    def submit(self, data):
        if not isinstance(data, dict):
            raise ValueError('invalid_action')
        action = data.get('action')
        fields = {'connect': {'mode', 'camera', 'authorized'}, 'start': {'name'}, 'snapshot': {'name'},
                  'stop': set(), 'disconnect': set(), 'export': set(), 'open-folder': set(), 'shutdown': set()}
        if self.workspace:
            fields.update({'mapping-recording': {'capture_id'}, 'navigation-live': set()})
        if action not in fields or set(data) - fields[action] - {'action'}:
            raise ValueError('unknown_action_or_fields')
        if action == 'connect':
            if data.get('mode') not in ('robot', 'demo') or type(data.get('camera')) is not int or data['camera'] not in range(1, 7):
                raise ValueError('invalid_camera_or_mode')
            if data['mode'] == 'robot' and data.get('authorized') is not True:
                raise PermissionError('manual_readonly_authorization_required')
        name = data.get('name', 'go1_camera')
        if not isinstance(name, str) or not re.fullmatch(r'[\w\- ]{1,64}', name):
            raise ValueError('invalid_session_name')
        if not self.busy.acquire(blocking=False):
            return False
        if self.stop.is_set():
            self.busy.release()
            raise RuntimeError('application_stopping')
        self.action_name, self.action_error = action, None

        def run():
            try:
                if action in ('mapping-recording', 'navigation-live'):
                    self.workspace.action(action, data)
                elif action == 'connect':
                    if self.controller.source is not None:
                        raise RuntimeError('disconnect_before_connecting_again')
                    source = self.source_factory(data['mode'], data['camera'])
                    self.camera, self.mode = data['camera'], data['mode']
                    self.controller.connect(source)
                elif action == 'disconnect':
                    self.controller.disconnect()
                elif action == 'start':
                    self.controller.start_recording(self.output, name)
                    self.record_started = time.monotonic()
                elif action == 'stop':
                    self.controller.stop_recording()
                elif action == 'snapshot':
                    self.snapshot_path = str(self.controller.snapshot(self.output, name))
                elif action == 'export':
                    from .capture_import import export_ffv1
                    status, _ = self.controller.view()
                    if status['state'] in ('recording', 'finalizing') or not status['output']:
                        raise RuntimeError('stop_recording_before_export')
                    self.export_path = export_ffv1(Path(status['output']))['video']
                elif action == 'open-folder':
                    open_directory(self.output)
                elif action == 'shutdown':
                    self.stop.set()
            except (ValueError, RuntimeError, TimeoutError, PermissionError) as exc:
                self.action_error = str(exc)[:400]
            except OSError:
                self.action_error = 'local_io_or_ssh_start_failed_check_log'
                logging.exception('Capture action I/O failure')
            except Exception:  # noqa: BLE001 - HTTP action boundary must always unlock
                self.action_error = 'internal_action_failed_check_log'
                logging.exception('Capture action failed')
            finally:
                self.busy.release()
        self.worker = threading.Thread(target=run, daemon=True, name='capture-web-action')
        self.worker.start()
        return True

    def close(self):
        self.stop.set()
        if self.thread:
            self.server.shutdown()
            self.thread.join(timeout=2.)
        if self.worker:
            self.worker.join(timeout=8.)
        self.controller.disconnect()
        self.server.server_close()

    def _handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = 'Go1Capture/1'

            def setup(self):
                super().setup()
                self.connection.settimeout(4.)

            def log_message(self, *_):
                pass  # Never log cookies, tokens or source images.

            def send(self, code, body=b'', content_type='application/json', *, cookie=False, frame_key=None):
                self.send_response(code)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                if frame_key is not None:
                    self.send_header('X-Go1-Frame-Key', frame_key)
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Referrer-Policy', 'no-referrer')
                frame_source = (f"http://127.0.0.1:{app.workspace.viewer.server.get_port()}" if app.workspace else "'none'")
                self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; "
                                 "img-src 'self' blob: data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; "
                                 f"frame-src {frame_source}; base-uri 'none'; form-action 'none'")
                if cookie:
                    self.send_header('Set-Cookie', f'{app.cookie_name}={app.token}; HttpOnly; SameSite=Strict; Path=/')
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (OSError, TimeoutError):
                    pass

            def reply(self, code, value, *, cookie=False):
                self.send(code, json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'), cookie=cookie)

            def allowed(self, authenticated=False):
                # A user may open the public shell from a shortcut/link. This
                # exception never covers API requests, iframes, or any POST.
                public_navigation = (self.command == 'GET' and self.path == '/' and not authenticated
                    and self.headers.get('Origin') is None and self.headers.get('Sec-Fetch-Mode') == 'navigate'
                    and self.headers.get('Sec-Fetch-Dest') == 'document' and self.headers.get('Sec-Fetch-User') == '?1')
                if (self.headers.get('Host') != app.origin.removeprefix('http://')
                        or (self.headers.get('Sec-Fetch-Site') == 'cross-site' and not public_navigation)
                        or self.headers.get('Origin', app.origin) != app.origin):
                    self.reply(403, {'error': {'code': 'local_origin_required'}})
                    return False
                if authenticated:
                    cookie = SimpleCookie()
                    try:
                        cookie.load(self.headers.get('Cookie', ''))
                        valid = app.cookie_name in cookie and secrets.compare_digest(cookie[app.cookie_name].value, app.token)
                    except (ValueError, TypeError, CookieError):
                        valid = False
                    if not valid:
                        self.reply(403, {'error': {'code': 'local_session_required'}})
                        return False
                return True

            def do_GET(self):
                path = urlsplit(self.path).path
                if not self.allowed(authenticated=path.startswith('/api/v1/') and path != '/api/v1/session'):
                    return
                if path == '/api/v1/session':
                    # Custom header prevents cross-origin simple requests/navigation.
                    # No CORS or OPTIONS allowance; renew grants no robot action.
                    if self.headers.get('X-Go1-Session') != 'renew':
                        self.reply(403, {'error': {'code': 'same_origin_session_header_required'}})
                        return
                    self.reply(200, {'data': {'csrf': app.csrf, 'instance_id': app.instance_id}}, cookie=True)
                elif path == '/health':
                    self.reply(200, {'app': 'go1-capture', 'instance_id': app.instance_id})
                elif path == '/api/v1/bootstrap':
                    self.reply(200, {'data': {'csrf': app.csrf, 'cameras': CAMERAS}})
                elif path == '/api/v1/state':
                    self.reply(200, {'data': app.view()})
                elif path == '/api/v1/workspace':
                    self.reply(200, {'data': app.workspace.summary() if app.workspace else {'enabled': False}})
                elif path == '/api/v1/frame.png':
                    image, frame_key = app.preview_sample()
                    mime = 'image/jpeg' if image and image.startswith(b'\xff\xd8') else 'image/png'
                    self.send(200 if image else 204, image or b'', mime, frame_key=frame_key)
                elif path in ('/', '/app.js', '/session.js', '/preview-timing.js', '/workspace.js', '/style.css', '/icon.svg'):
                    file, mime = {'/': ('index.html', 'text/html; charset=utf-8'),
                                  '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                                  '/session.js': ('session.js', 'text/javascript; charset=utf-8'),
                                  '/preview-timing.js': ('preview-timing.js', 'text/javascript; charset=utf-8'),
                                  '/workspace.js': ('workspace.js', 'text/javascript; charset=utf-8'),
                                  '/style.css': ('style.css', 'text/css; charset=utf-8'),
                                  '/icon.svg': ('icon.svg', 'image/svg+xml')}[path]
                    self.send(200, (ASSETS / file).read_bytes(), mime, cookie=path == '/')
                else:
                    self.reply(404, {'error': {'code': 'not_found'}})

            def do_POST(self):
                if not self.allowed(authenticated=True):
                    return
                if not secrets.compare_digest(self.headers.get('X-Go1-CSRF', ''), app.csrf):
                    self.reply(403, {'error': {'code': 'csrf_required'}})
                    return
                if self.path != '/api/v1/actions':
                    self.reply(404, {'error': {'code': 'not_found'}})
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if (not 0 < length <= 4096 or self.headers.get('Transfer-Encoding')
                            or self.headers.get('Content-Type', '').split(';')[0] != 'application/json'):
                        raise ValueError('invalid_json_body')
                    with app.rate_lock:
                        now = time.monotonic()
                        if len(app.action_times) == 60 and now - app.action_times[0] < 60:
                            self.reply(429, {'error': {'code': 'too_many_actions'}})
                            return
                        app.action_times.append(now)
                    data = json.loads(self.rfile.read(length))
                    if not app.submit(data):
                        self.reply(409, {'error': {'code': 'action_busy'}})
                    else:
                        self.reply(202, {'data': {'accepted': True}})
                except PermissionError as exc:
                    self.reply(403, {'error': {'code': str(exc)}})
                except (ValueError, TypeError, UnicodeError):
                    self.reply(422, {'error': {'code': 'invalid_action_or_parameters'}})
                except RuntimeError:
                    self.reply(409, {'error': {'code': 'application_stopping'}})

        return Handler
