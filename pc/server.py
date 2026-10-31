"""Loopback-only web UI for the PC. Uses Python 3 standard library only."""
import argparse
import collections
import json
import pathlib
import re
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from transport import RobotLink
from navigation_authority import NavigationAuthority
from timeouts import BROWSER_FRESHNESS_S, OWNER_LEASE_S

ROOT = pathlib.Path(__file__).resolve().parent / 'web'
APP_VERSION = '0.3.4-20261029'


class App:
    def __init__(self, config, port):
        self.link = RobotLink(config)
        self.token = secrets.token_hex(32)
        self.port = port
        self.lock = threading.RLock()
        self.owner = ''
        self.owner_until = 0.
        self.sequences = {}
        self.ticks = collections.deque(maxlen=128)
        self.tick = 0
        # Navigation gets a separate configured token and reuses this App's
        # RobotLink. Missing token keeps the feature disabled; no secret is
        # silently generated into an inaccessible process.
        self.navigation = NavigationAuthority(self, config.get('navigation_token'))

    def close(self):
        self.navigation.close()
        self.link.close()

    def state(self):
        with self.lock:
            self.tick += 1
            self.ticks.append((self.tick, time.monotonic()))
            payload = dict(self.link.snapshot(), browser_tick=self.tick,
                           app_version=APP_VERSION, app_name='go1_remote_control')
            payload['navigation'] = self.navigation.snapshot()
            return payload

    def control(self, body):
        with self.lock:
            action = body.get('action')
            browser = body.get('browser', '')
            seq, tick = body.get('seq'), body.get('tick')
            if not re.fullmatch('[0-9a-f]{32}', browser) or type(seq) is not int or not 0 < seq <= 9007199254740991:
                raise ValueError('浏览器会话/序号无效')
            if seq <= self.sequences.get(browser, 0):
                raise ValueError('已拒绝过期或乱序浏览器请求')
            self.sequences[browser] = seq
            if len(self.sequences) > 64:
                self.sequences = {browser: seq, self.owner: self.sequences.get(self.owner, 0)}
            safe = action in ('stop', 'disarm', 'disconnect', 'release')
            if self.navigation.task_id and not safe:
                raise ValueError('导航任务正在占有控制权')
            if not safe and (type(tick) is not int or not any(n == tick and time.monotonic()-stamp <= BROWSER_FRESHNESS_S for n, stamp in self.ticks)):
                self.link.submit('disarm')
                raise ValueError('浏览器请求已过期；已请求停止并解除解锁')
            if self.owner and self.owner != browser and time.monotonic() < self.owner_until:
                if action not in ('stop', 'disarm', 'disconnect', 'release'):
                    raise ValueError('另一个页面正在控制；本页面仅可请求停止')
            else:
                if self.owner and self.owner != browser:
                    self.link.submit('disarm')
                self.owner = browser
            if action == 'heartbeat':
                self.owner_until = time.monotonic()+OWNER_LEASE_S
            elif action in ('disconnect', 'disarm'):
                self.owner_until = 0
            if safe and self.navigation.task_id:
                self.navigation.revoke('manual_'+action)
            self.link.submit(action, key=body.get('key', ''), speed=body.get('speed', .5), standing=body.get('standing', False),
                             lateral=body.get('lateral', .4), turn=body.get('turn', .6))

    def navigation_control(self, path, body, token):
        if path == '/api/navigation/authorize':
            return self.navigation.authorize(body.get('task_id'), token)
        if path == '/api/navigation/command':
            return self.navigation.command(body.get('task_id'), token, body.get('command_id'),
                                           body.get('primitive'), body.get('ttl_s'))
        if path == '/api/navigation/keepalive':
            return self.navigation.keepalive(body.get('task_id'), token)
        if path == '/api/navigation/pause':
            return self.navigation.pause(body.get('task_id'), token, body.get('reason', 'visual_wait'))
        if path == '/api/navigation/resume':
            return self.navigation.resume(body.get('task_id'), token)
        if path == '/api/navigation/stop':
            return self.navigation.stop(body.get('task_id', ''), token, body.get('reason', 'navigation_stop'))
        if path == '/api/navigation/release':
            return self.navigation.release(body.get('task_id', ''), token)
        raise ValueError('navigation_path_invalid')


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _trusted_host(self):
            return self.headers.get('Host') in ('127.0.0.1:'+str(app.port), 'localhost:'+str(app.port))

        def _send(self, code, payload, content_type='application/json; charset=utf-8'):
            if isinstance(payload, dict):
                payload = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if not self._trusted_host():
                self._send(403, {'error': 'Host rejected'})
                return
            path = urlsplit(self.path).path
            if path == '/favicon.ico':
                self._send(204, b'')
            elif path == '/api/state':
                self._send(200, app.state())
            elif path in ('/', '/app.js', '/state.js', '/style.css'):
                file = ROOT / {'/': 'index.html', '/app.js': 'app.js', '/state.js':'state.js', '/style.css': 'style.css'}[path]
                data = file.read_bytes().replace(b'__LOCAL_TOKEN__', app.token.encode('ascii'))
                kind = 'text/html' if path == '/' else 'text/javascript' if path.endswith('.js') else 'text/css'
                self._send(200, data, kind+'; charset=utf-8')
            else:
                self._send(404, {'error': 'Not found'})

        def do_POST(self):
            path = urlsplit(self.path).path
            if (path not in ('/api/control', '/api/navigation/authorize', '/api/navigation/command',
                             '/api/navigation/keepalive', '/api/navigation/pause', '/api/navigation/resume',
                             '/api/navigation/stop', '/api/navigation/release') or not self._trusted_host() or
                    self.headers.get('Origin') not in ('http://127.0.0.1:'+str(app.port), 'http://localhost:'+str(app.port)) or
                    not secrets.compare_digest(self.headers.get('X-Go1-Local-Token', ''), app.token)):
                self._send(403, {'error': 'Origin/token rejected'})
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 2048:
                    raise ValueError('请求长度超限')
                self.connection.settimeout(.5)
                body = json.loads(self.rfile.read(size), parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON')))
                if not isinstance(body, dict):
                    raise ValueError('请求必须是对象')
                if path == '/api/control':
                    app.control(body)
                    result = {'ok': True}
                else:
                    nav_token = self.headers.get('X-Go1-Navigation-Token', '')
                    result = app.navigation_control(path, body, nav_token)
                self._send(200, result)
            except (ValueError, TypeError, PermissionError, OSError) as error:
                self._send(400, {'error': str(error)})
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=pathlib.Path, required=True)
    parser.add_argument('--port', type=int, default=8099)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    app = App(config, args.port)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(app))
    print('PC_UI http://127.0.0.1:'+str(args.port)+' / '+('SIMULATION ONLY' if app.link.simulation else 'REAL ROBOT CONFIG'), flush=True)
    if not args.no_browser:
        webbrowser.open('http://127.0.0.1:'+str(args.port))
    try:
        server.serve_forever(poll_interval=.1)
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        server.server_close()


if __name__ == '__main__':
    main()
