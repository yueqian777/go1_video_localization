"""All hardware traffic is replaced by the native loopback-only simulator."""
import json
import os
import pathlib
import re
import secrets
import select
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import threading
from http.server import ThreadingHTTPServer
from contextlib import contextmanager

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pc'))
sys.path.insert(0, str(ROOT / 'tools'))
from transport import RobotLink
from server import App, make_handler
from run_demo import build_simulator

BINARY = pathlib.Path(os.environ.get('GO1_SIM_BINARY', ROOT / 'build' / ('go1_receiver_sim.exe' if os.name == 'nt' else 'go1_receiver_sim')))


@contextmanager
def receiver(*options):
    with tempfile.TemporaryDirectory(prefix='go1_test_') as directory:
        token = secrets.token_hex(32)
        token_file = pathlib.Path(directory) / 'robot.token'
        token_file.write_text(token, encoding='ascii')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen([str(BINARY), '--simulate', '--port', str(port), '--token-file', str(token_file), *options],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic()+3
            while time.monotonic() < deadline:
                try:
                    connection = socket.create_connection(('127.0.0.1', port), .1)
                    break
                except OSError:
                    if process.poll() is not None:
                        raise AssertionError(process.communicate())
                    time.sleep(.01)
            else:
                raise AssertionError('Simulator failed to listen')
            yield process, port, token, connection
        finally:
            if 'connection' in locals():
                connection.close()
            if process.poll() is None:
                process.terminate()  # Tests only ever start the simulation-only binary.
                process.wait(3)
            process.communicate()


class Client:
    def __init__(self, connection, token):
        self.socket = connection
        self.token = token
        self.seq = 0
        self.buffer = b''
        self.state = {}
        self.wait(lambda s: 'session' in s, heartbeat=False)
        self.send('AUTH')
        self.wait(lambda s: s.get('authenticated'), heartbeat=False)

    def send(self, op='PING', arg='-', active=True, tick=None, seq=None):
        self.seq += 1
        frame = 'G1R1|{}|{}|{}|{}|{}|{}|{}\n'.format(
            self.token, self.state['session'], self.seq if seq is None else seq,
            self.state['tick'] if tick is None else tick, op, arg, int(active))
        self.socket.sendall(frame.encode('ascii'))

    def read(self, timeout=.02):
        if select.select([self.socket], [], [], timeout)[0]:
            data = self.socket.recv(16384)
            if not data:
                raise EOFError('Receiver closed connection')
            self.buffer += data
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                self.state = json.loads(line)
        return self.state

    def pump(self, duration, key=None, active=True):
        deadline = time.monotonic()+duration
        next_send = 0
        while time.monotonic() < deadline:
            self.read(.01)
            if time.monotonic() >= next_send:
                self.send('MOVE' if key else 'PING', key or '-', active)
                next_send = time.monotonic()+.05
        self.read(.01)

    def wait(self, predicate, timeout=2, heartbeat=True):
        deadline = time.monotonic()+timeout
        next_send = 0
        while time.monotonic() < deadline:
            self.read(.01)
            if predicate(self.state):
                return self.state
            if heartbeat and self.state.get('authenticated') and time.monotonic() >= next_send:
                self.send()
                next_send = time.monotonic()+.05
        raise AssertionError(self.state)

    def take_and_arm(self, settings='STANDING,500'):
        self.send('TAKE', settings)
        self.wait(lambda s: s.get('control_active'))
        self.pump(.6)
        self.send('ARM')
        self.wait(lambda s: s.get('armed'))


@contextmanager
def status_gap_proxy(target_port):
    """Loopback-only fault injector: discard whole downlink JSON frames, never touch a robot."""
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    listener.settimeout(.1)
    pause, done = threading.Event(), threading.Event()

    def forward():
        client = upstream = None
        partial = b''
        try:
            while not done.is_set():
                try:
                    client, _ = listener.accept()
                    break
                except socket.timeout:
                    continue
            if client is None:
                return
            upstream = socket.create_connection(('127.0.0.1', target_port), 1)
            for connection in (client, upstream):
                connection.settimeout(.2)
                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            while not done.is_set():
                readable, _, _ = select.select([client, upstream], [], [], .01)
                for connection in readable:
                    data = connection.recv(8192)
                    if not data:
                        return
                    if connection is client:
                        upstream.sendall(data)
                    else:
                        partial += data
                        while b'\n' in partial:
                            line, partial = partial.split(b'\n', 1)
                            if not pause.is_set():
                                client.sendall(line+b'\n')
        except OSError:
            pass  # A test deliberately closes the PC transport or simulated receiver.
        finally:
            for connection in (client, upstream):
                if connection:
                    connection.close()

    worker = threading.Thread(target=forward, daemon=True)
    worker.start()
    try:
        yield listener.getsockname()[1], pause
    finally:
        done.set()
        worker.join(2)
        listener.close()


def wait_link(link, predicate, timeout=2, key=''):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        state = link.snapshot()
        if predicate(state):
            return state
        if state['connected']:
            try:
                link.submit('heartbeat', key)
            except ValueError:
                pass  # Intentional state gap: movement submission must be refused.
        time.sleep(.02)
    raise AssertionError(link.snapshot())


class ReceiverTests(unittest.TestCase):
    def test_cross_axis_gait_drift_does_not_interrupt_six_held_directions(self):
        with receiver('--mock-cross-drift') as (_, _, token, connection):
            c = Client(connection, token)
            c.take_and_arm()
            for key, axis, target in [('w','vx',.5),('s','vx',-.5),('j','vy',.2),
                                      ('l','vy',-.2),('z','yaw',.3),('c','yaw',-.3)]:
                with self.subTest(key=key):
                    before = c.state['motion_stop_count']
                    c.pump(1.2, key)
                    self.assertTrue(c.state['armed'])
                    self.assertFalse(c.state['fault'])
                    self.assertEqual(c.state['command_'+axis], target)
                    self.assertEqual(c.state['motion_stop_count'], before)
                    self.assertEqual(c.state['ttl_expirations'], 0)
                    self.assertGreater(c.state['motion_elapsed_s'], 1)
                    c.send('STOP')
                    c.wait(lambda s: s.get('command_'+axis) == 0)
                    self.assertEqual(c.state['last_stop_reason'], 'USER_STOP')
                    c.pump(.45)

    def test_motion_ttl_gap_is_distinguishable_from_feedback_drift(self):
        with receiver() as (_, _, token, connection):
            c = Client(connection, token)
            c.take_and_arm()
            c.pump(.2, 'w')
            c.pump(.8)  # Fresh PING/UI, but deliberately no MOVE renewal.
            self.assertTrue(c.state['armed'])
            self.assertEqual(c.state['command_vx'], 0)
            self.assertEqual(c.state['ttl_expirations'], 1)
            self.assertEqual(c.state['last_stop_reason'], 'COMMAND_TTL_EXPIRED')
            self.assertEqual(c.state['motion_stop_count'], 1)
    def test_configurable_lateral_and_turn_rates(self):
        with receiver() as (_, _, token, connection):
            c = Client(connection, token)
            self.assertTrue(c.state['configurable_speeds'])
            c.take_and_arm('STANDING,500,400,600')
            self.assertEqual((c.state['speed_vy'], c.state['speed_yaw']), (.4, .6))
            for key, axis, value in [('j','vy',.4), ('l','vy',-.4), ('z','yaw',.6), ('c','yaw',-.6)]:
                c.send('MOVE', key)
                c.wait(lambda s: abs(s['command_'+axis]) > 0)
                self.assertAlmostEqual(c.state['command_'+axis], value)
                c.send('STOP')
                c.wait(lambda s: s['command_'+axis] == 0)
                c.pump(.45)

    def test_out_of_range_profiles_never_open_control(self):
        for settings in ('STANDING,500,401,600', 'STANDING,500,400,601', 'STANDING,501,400,600'):
            with self.subTest(settings=settings), receiver() as (_, _, token, connection):
                c = Client(connection, token)
                c.send('TAKE', settings)
                with self.assertRaises((EOFError, ConnectionResetError)):
                    c.wait(lambda _: False, heartbeat=False)

    def test_stale_ping_keeps_network_not_motion(self):
        with receiver() as (_, _, token, connection):
            c = Client(connection, token)
            c.take_and_arm()
            c.pump(.1, 'w')
            deadline = time.monotonic()+3.35
            while time.monotonic() < deadline:
                c.send('PING', active=True, tick=0)
                c.read(.04)
            c.wait(lambda s: s.get('fault'), heartbeat=False)
            self.assertTrue(c.state['authenticated'])
            self.assertEqual(c.state['fault_reason'], 'LINK_TIMEOUT')
            self.assertFalse(c.state['armed'])
            self.assertEqual(c.state['command_vx'], 0)

    def test_pc_short_jitter_keeps_control_without_false_timeout(self):
        with receiver() as (_, port, token, connection):
            connection.close()
            time.sleep(.1)
            with status_gap_proxy(port) as (proxy_port, pause):
                link = RobotLink({'host':'127.0.0.1','port':proxy_port,'token':token,'simulation':True})
                try:
                    link.connect()
                    wait_link(link, lambda s: s['connected'])
                    link.submit('take', standing=True)
                    wait_link(link, lambda s: s['robot'].get('ready_ms', 0) >= 500)
                    link.submit('arm')
                    wait_link(link, lambda s: s['robot'].get('armed'))
                    wait_link(link, lambda s: s['robot'].get('command_vx') == .5, key='w')
                    pause.set()
                    deadline = time.monotonic()+.85
                    while time.monotonic() < deadline:
                        link.submit('heartbeat', 'w')
                        time.sleep(.03)
                    self.assertTrue(link.snapshot()['connected'])
                    self.assertFalse(link.snapshot()['recovery_required'])
                    self.assertFalse(link.snapshot()['degraded'])
                    pause.clear()
                    state = wait_link(link, lambda s: s['telemetry_age_ms'] < 150, key='w')
                    self.assertTrue(state['robot']['armed'])
                    self.assertFalse(state['robot']['fault'])
                    self.assertEqual(state['robot']['command_vx'], .5)
                    link.submit('heartbeat', '')
                    state = wait_link(link, lambda s: s['robot'].get('command_vx') == 0)
                    self.assertEqual(state['robot']['command_vx'], 0)
                    self.assertTrue(state['robot']['armed'])
                    self.assertFalse(state['recovery_required'])
                finally:
                    link.close()

    def test_two_point_five_second_status_gap_does_not_disarm_stationary_control(self):
        with receiver() as (_, port, token, connection):
            connection.close()
            time.sleep(.1)
            with status_gap_proxy(port) as (proxy_port, pause):
                link = RobotLink({'host':'127.0.0.1','port':proxy_port,'token':token,'simulation':True})
                try:
                    link.connect()
                    wait_link(link, lambda s: s['connected'])
                    link.submit('take', standing=True)
                    wait_link(link, lambda s: s['robot'].get('ready_ms', 0) >= 500)
                    link.submit('arm')
                    wait_link(link, lambda s: s['robot'].get('armed'))
                    pause.set()
                    deadline = time.monotonic()+2.5
                    while time.monotonic() < deadline:
                        link.submit('heartbeat')
                        time.sleep(.03)
                    state = link.snapshot()
                    self.assertTrue(state['robot_fresh'])
                    self.assertFalse(state['recovery_required'])
                    pause.clear()
                    state = wait_link(link, lambda s: s['telemetry_age_ms'] < 150)
                    self.assertTrue(state['robot']['armed'])
                    self.assertFalse(state['robot']['fault'])
                    self.assertEqual(state['robot']['command_vx'], 0)
                finally:
                    link.close()

    def test_echo_history_covers_full_three_second_window(self):
        with receiver() as (_, _, token, connection):
            c = Client(connection, token)
            c.take_and_arm()
            old_tick = c.state['tick']
            c.pump(2.4)
            self.assertEqual(c.state['control_timeout_ms'], 3000)
            self.assertEqual(c.state['command_echo_ms'], 3000)
            c.send('MOVE', 'j', tick=old_tick)
            c.wait(lambda s: s.get('command_vy') == .2)
            self.assertTrue(c.state['armed'])
            self.assertFalse(c.state['fault'])
            c.send('STOP', tick=0)  # Safe operations must not require fresh echoes.
            c.wait(lambda s: s.get('command_vy') == 0)
            c.pump(.8)
            c.send('MOVE', 'w', tick=old_tick)
            # Wait for this command's result, not a buffered, earlier disarm observation.
            c.wait(lambda s: s.get('message') == 'STALE_COMMAND_REJECTED')
            self.assertFalse(c.state['armed'])
            self.assertEqual(c.state['command_vx'], 0)
            self.assertEqual(c.state['message'], 'STALE_COMMAND_REJECTED')

    def test_pc_long_gap_disconnects_without_auto_reconnect(self):
        with receiver() as (_, port, token, connection):
            connection.close()
            time.sleep(.1)
            with status_gap_proxy(port) as (proxy_port, pause):
                link = RobotLink({'host':'127.0.0.1','port':proxy_port,'token':token,'simulation':True})
                try:
                    link.connect()
                    wait_link(link, lambda s: s['connected'])
                    pause.set()
                    state = wait_link(link, lambda s: not s['connected'], timeout=4)
                    self.assertIn('3 秒', state['disconnect_reason'])
                    self.assertFalse(state['connecting'])
                finally:
                    link.close()

    def test_six_directions_stop_and_ttl(self):
        with receiver() as (_, _, token, connection):
            c = Client(connection, token)
            self.assertFalse(c.state['control_active'])
            self.assertIsNone(c.state['soc'])
            c.send('MOVE', 'w')
            c.pump(.12)
            self.assertEqual(c.state['command_vx'], 0)
            c.take_and_arm()
            for key, expected in [('w', (.5, 0, 0)), ('s', (-.5, 0, 0)), ('j', (0, .2, 0)),
                                  ('l', (0, -.2, 0)), ('z', (0, 0, .3)), ('c', (0, 0, -.3))]:
                c.send('MOVE', key)
                c.wait(lambda s: any(s.get('command_'+axis, 0) for axis in ('vx', 'vy', 'yaw')))
                for axis, value in zip(('vx', 'vy', 'yaw'), expected):
                    self.assertAlmostEqual(c.state['command_'+axis], value)
                c.send('STOP')
                c.wait(lambda s: all(s.get('command_'+axis) == 0 for axis in ('vx', 'vy', 'yaw')))
                c.pump(.45)
            c.send('MOVE', 'j')
            c.pump(.8)
            self.assertEqual(c.state['command_vy'], 0)
            self.assertTrue(c.state['armed'])
            connection.close()

    def test_all_six_directions_hold_past_two_seconds_and_focus_lease(self):
        with receiver() as (_, _, token, connection):
            c = Client(connection, token)
            c.take_and_arm()
            self.assertTrue(c.state['hold_to_move'])
            self.assertIsNone(c.state['continuous_limit_s'])
            self.assertNotIn('burst_left_s', c.state)
            for key, axis, target in [('w','vx',.5),('s','vx',-.5),('j','vy',.2),
                                      ('l','vy',-.2),('z','yaw',.3),('c','yaw',-.3)]:
                with self.subTest(key=key):
                    c.pump(2.35, key)
                    self.assertTrue(c.state['armed'])
                    self.assertFalse(c.state['fault'])
                    self.assertAlmostEqual(c.state['command_'+axis], target)
                    self.assertGreater(c.state['motion_elapsed_s'], 2)
                    c.send('STOP')
                    c.wait(lambda s: s.get('command_'+axis) == 0)
                    self.assertTrue(c.state['armed'])
                    self.assertEqual(c.state['motion_elapsed_s'], 0)
                    c.pump(.45)
            c.pump(.15, 'z')
            c.send('PING', active=False)
            c.wait(lambda s: not s.get('armed'))
            self.assertEqual(c.state['command_yaw'], 0)
            c.pump(.2)
            self.assertFalse(c.state['armed'])
            connection.close()

    def test_link_timeout_and_no_auto_resume(self):
        with receiver() as (_, port, token, connection):
            c = Client(connection, token)
            c.take_and_arm()
            c.pump(.1, 'w')
            # Motion deadman stops quickly, but the idle TCP session is not expired before 3 s.
            time.sleep(1.1)
            c.read()
            c.wait(lambda s: not s.get('armed'), heartbeat=False)
            self.assertFalse(c.state['fault'])
            self.assertEqual(c.state['command_vx'], 0)
            with self.assertRaises((EOFError, ConnectionResetError)):
                c.wait(lambda _: False, timeout=3, heartbeat=False)
            connection.close()
            time.sleep(.1)
            c2 = Client(socket.create_connection(('127.0.0.1', port), 1), token)
            self.assertFalse(c2.state['armed'])
            self.assertFalse(c2.state['control_active'])
            c2.socket.close()

    def test_avoidance_and_stale_command(self):
        with receiver() as (_, _, token, connection):
            c = Client(connection, token)
            c.take_and_arm()
            c.send('AVOID')
            c.wait(lambda s: s.get('avoidance_selected'))
            self.assertFalse(c.state['armed'])
            c.send('ARM')
            c.wait(lambda s: s.get('armed'))
            c.pump(.15, 'w')
            self.assertEqual(c.state['feedback_gait'], 4)
            c.send('STOP')
            c.wait(lambda s: s.get('command_vx') == 0)
            self.assertNotEqual(c.state['feedback_gait'], 4)
            c.send('MOVE', 'w', tick=0)
            c.wait(lambda s: not s.get('armed'))
            self.assertEqual(c.state['command_vx'], 0)
            connection.close()

    def test_battery_and_acknowledgement(self):
        for soc in (10, 9):
            with self.subTest(soc=soc), receiver('--mock-soc', str(soc)) as (_, _, token, connection):
                c = Client(connection, token)
                c.send('TAKE', 'STANDING,500')
                c.wait(lambda s: s.get('control_active'))
                c.pump(.65)
                c.send('ARM')
                c.pump(.15)
                self.assertEqual(c.state['armed'], soc == 10)
                if soc == 9:
                    self.assertEqual(c.state['fault_reason'], 'BATTERY_LIMIT')
                connection.close()
        with receiver('--mock-no-ack') as (_, _, token, connection):
            c = Client(connection, token)
            c.take_and_arm()
            c.pump(1.7, 'j')
            self.assertEqual(c.state['fault_reason'], 'MOTION_START_TIMEOUT')
            self.assertEqual(c.state['command_vy'], 0)
            connection.close()

    def test_replay_and_bad_auth(self):
        with receiver() as (_, port, token, connection):
            c = Client(connection, token)
            c.take_and_arm()
            c.send('MOVE', 'w', seq=1)
            with self.assertRaises((EOFError, ConnectionResetError)):
                c.wait(lambda _: False, heartbeat=False)
            connection.close()
            bad = socket.create_connection(('127.0.0.1', port), 1)
            with self.assertRaises((EOFError, ConnectionResetError)):
                Client(bad, '0'*64)
            bad.close()

    def test_sdk_state_timeout_and_explicit_release(self):
        with receiver('--mock-stale-after', '1.0') as (_, _, token, connection):
            c = Client(connection, token)
            c.take_and_arm()
            c.pump(.6, 'w')
            self.assertEqual(c.state['fault_reason'], 'STALE_STATE')
            self.assertEqual(c.state['command_vx'], 0)
            c.send('RELEASE')
            c.wait(lambda s: not s.get('control_active'))
            self.assertFalse(c.state['armed'])
            c.pump(.15)
            self.assertTrue(c.state['authenticated'])
            connection.close()

    def test_pc_ui_lease_stops_without_ssh(self):
        with receiver() as (_, port, token, connection):
            connection.close()  # Let the PC transport own the next authenticated connection.
            time.sleep(.1)
            link = RobotLink({'host': '127.0.0.1', 'port': port, 'token': token, 'simulation': True})
            link.connect()
            try:
                def wait_for(predicate, timeout=2, key=''):
                    deadline = time.monotonic()+timeout
                    while time.monotonic() < deadline:
                        s = link.snapshot()
                        if predicate(s):
                            return s
                        if s['connected']:
                            link.submit('heartbeat', key)
                        time.sleep(.03)
                    raise AssertionError(link.snapshot())
                wait_for(lambda s: s['connected'])
                link.submit('take', standing=True)
                wait_for(lambda s: s['robot'].get('control_active'))
                deadline = time.monotonic()+.65
                while time.monotonic() < deadline:
                    link.submit('heartbeat')
                    time.sleep(.03)
                link.submit('arm')
                wait_for(lambda s: s['robot'].get('armed'))
                wait_for(lambda s: s['robot'].get('command_vx') == .5, key='w')
                deadline = time.monotonic()+3
                while time.monotonic() < deadline:
                    link.submit('heartbeat', 'w')
                    time.sleep(.03)
                self.assertTrue(link.snapshot()['robot']['armed'])
                self.assertEqual(link.snapshot()['robot']['command_vx'], .5)
                self.assertGreater(link.snapshot()['robot']['motion_elapsed_s'], 2)
                released_at = time.monotonic()
                link.submit('heartbeat', '')
                stopped = wait_for(lambda s: s['robot'].get('command_vx') == 0, timeout=.5)
                self.assertLess(time.monotonic()-released_at, .55)
                self.assertTrue(stopped['robot']['armed'])
                wait_for(lambda s: s['robot'].get('command_vx') == .5, key='w')
                # Stop refreshing the browser lease while the PC transport itself stays connected.
                time.sleep(.8)
                s = link.snapshot()
                self.assertTrue(s['connected'])
                self.assertFalse(s['robot']['armed'])
                self.assertEqual(s['robot']['command_vx'], 0)
            finally:
                link.close()


class StatusSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.link = RobotLink({'host':'127.0.0.1','port':8765,'token':'1'*64,'simulation':True})

    def accept(self, **overrides):
        state = dict(authenticated=True, session='a'*16, control_active=False, armed=False, stopping=False,
                     configurable_speeds=True, fault=False, fault_reason='', health='NO_SDK_CONTROL')
        state.update(overrides)
        self.link._accept_status(state)

    def test_disconnect_clears_current_state_preserves_labelled_history(self):
        self.accept(control_active=True, fault=True, fault_reason='LINK_TIMEOUT')
        self.link.recovery_required = self.link.degraded = True
        self.link.held_key = 'w'
        self.link.disconnect_reason = 'test disconnect'
        self.link._clear_connection()
        state = self.link.snapshot()
        self.assertEqual(state['robot'], {})
        self.assertEqual(state['connection_stage'], 'offline')
        self.assertFalse(state['robot_fresh'])
        self.assertFalse(state['degraded'])
        self.assertFalse(state['recovery_required'])
        self.assertIsNone(state['telemetry_age_ms'])
        self.assertEqual(state['history']['last_fault']['reason'], 'LINK_TIMEOUT')
        self.assertEqual(state['history']['last_disconnect']['reason'], 'test disconnect')
        with self.assertRaises(ValueError):
            self.link.submit('heartbeat', 'w')

    def test_idle_gap_does_not_latch_rearm_warning(self):
        self.accept()
        self.link.received_at = time.monotonic()-3.1
        self.link._expire_state(time.monotonic())
        self.assertTrue(self.link.snapshot()['degraded'])
        self.assertFalse(self.link.recovery_required)
        self.assertEqual(self.link.snapshot()['robot'], {})
        self.accept()
        self.link._expire_state(time.monotonic())
        state = self.link.snapshot()
        self.assertEqual(state['connection_stage'], 'online')
        self.assertFalse(state['degraded'])
        self.assertFalse(state['recovery_required'])
        self.assertIsNotNone(state['history']['last_network_gap'])

    def test_active_gap_keeps_manual_rearm_latch(self):
        self.accept(control_active=True, armed=True, health='OK')
        self.link.held_key = 'w'
        self.link.received_at = time.monotonic()-3.1
        self.link._expire_state(time.monotonic())
        self.assertTrue(self.link.recovery_required)
        self.assertEqual(self.link.held_key, '')
        self.accept(control_active=True, fault=True, fault_reason='LINK_TIMEOUT', health='OK')
        self.assertTrue(self.link.snapshot()['robot_fresh'])
        self.assertTrue(self.link.recovery_required)
        self.assertTrue(self.link.snapshot()['robot']['fault'])
        self.link.submit('heartbeat', 'w')
        self.assertEqual(self.link.held_key, '')

    def test_three_second_boundary_keeps_half_second_and_two_second_status_usable(self):
        for age in (.5, .65, 2.5, 2.99):
            self.accept(control_active=True, armed=True, health='OK')
            self.link.received_at = time.monotonic()-age
            self.link._expire_state(time.monotonic())
            state = self.link.snapshot()
            self.assertTrue(state['robot_fresh'], state)
            self.assertFalse(state['degraded'])
            self.assertFalse(state['recovery_required'])
            self.link.submit('heartbeat', 'w')
            self.assertEqual(self.link.held_key, 'w')
        self.link.received_at = time.monotonic()-3.01
        self.link._expire_state(time.monotonic())
        self.assertFalse(self.link.snapshot()['robot_fresh'])
        self.assertTrue(self.link.recovery_required)
        with self.assertRaises(ValueError):
            self.link.submit('heartbeat', 'w')

    def test_old_receiver_cannot_silently_use_quarter_second_echo_threshold(self):
        self.accept()
        with self.assertRaisesRegex(ValueError, '旧网络阈值'):
            self.link.submit('take', standing=True)

    def test_time_limited_receiver_cannot_pretend_to_offer_unlimited_held_motion(self):
        for capabilities in ({}, {'hold_to_move':True}, {'hold_to_move':True,'continuous_limit_s':2}):
            self.accept(control_timeout_ms=3000, command_echo_ms=3000, **capabilities)
            with self.assertRaisesRegex(ValueError, '连续运动时限'):
                self.link.submit('take', standing=True)

    def test_pre_drift_fix_receiver_requires_update(self):
        self.accept(control_timeout_ms=3000, command_echo_ms=3000, hold_to_move=True, continuous_limit_s=None)
        with self.assertRaisesRegex(ValueError, '轻微横摆'):
            self.link.submit('take', standing=True)

    def test_motion_stop_event_and_browser_heartbeat_diagnostics(self):
        self.accept(control_active=True, motion_stop_count=0)
        self.accept(control_active=True, motion_stop_count=1, last_stop_reason='FEEDBACK_NOT_SETTLED',
                    move_gap_ms=52, ttl_expirations=0)
        self.assertIn('FEEDBACK_NOT_SETTLED', self.link.snapshot()['events'][-1]['message'])
        self.link.submit('heartbeat', 'w')
        self.link.last_ui_heartbeat -= .2
        self.link.submit('heartbeat', 'w')
        self.assertGreaterEqual(self.link.snapshot()['input_diagnostics']['max_heartbeat_gap_ms'], 200)
        self.link._clear_connection()
        self.assertIsNone(self.link.snapshot()['input_diagnostics']['heartbeat_age_ms'])

    def test_confirmed_release_removes_rearm_prompt_not_fault_history(self):
        self.accept(control_active=True, fault=True, fault_reason='LINK_TIMEOUT')
        self.link.recovery_required = True
        self.accept(fault=True, fault_reason='LINK_TIMEOUT')
        self.assertFalse(self.link.recovery_required)
        self.assertEqual(self.link.snapshot()['history']['last_fault']['reason'], 'LINK_TIMEOUT')

    def test_refused_connection_is_offline_not_robot_fault(self):
        with patch('transport.socket.create_connection', side_effect=ConnectionRefusedError(10061, 'test refusal')) as attempt:
            self.link.connect()
            self.link.thread.join(3)
            state = self.link.snapshot()
            attempt.assert_called_once()
        self.assertFalse(state['connected'])
        self.assertFalse(state['connecting'])
        self.assertEqual(state['connection_stage'], 'offline')
        self.assertEqual(state['robot'], {})
        self.assertIsNone(state['history']['last_fault'])
        self.assertIn('拒绝连接', state['disconnect_reason'])

    def test_connect_timeout_does_not_invent_robot_fault(self):
        with patch('transport.socket.create_connection', side_effect=socket.timeout('test timeout')):
            self.link.connect()
            self.link.thread.join(3)
        state = self.link.snapshot()
        self.assertFalse(state['connected'])
        self.assertEqual(state['robot'], {})
        self.assertFalse(state['recovery_required'])
        self.assertIn('连接超时', state['disconnect_reason'])
        self.assertIsNone(state['history']['last_fault'])


class BrowserApiTests(unittest.TestCase):
    def test_profile_api_validation_and_forwarding(self):
        app = App({'host':'127.0.0.1','port':8765,'token':'1'*64,'simulation':True}, 8099)
        app.link.connected = True
        app.link.received_at = time.monotonic()
        app.link.robot = {'configurable_speeds':True, 'control_timeout_ms':3000, 'command_echo_ms':3000,
                          'hold_to_move':True, 'continuous_limit_s':None, 'steady_motion_renewal':True, 'startup_ack_ms':1500}
        tick = app.state()['browser_tick']
        self.assertEqual(app.state()['app_version'], '0.3.4-20261029')
        body = {'action':'take','browser':'c'*32,'seq':1,'tick':tick,
                'standing':True,'speed':.2,'lateral':.3,'turn':.45}
        app.control(body)
        self.assertEqual(app.link.queue[-1][1], 'STANDING,200,300,450')
        for i, overrides in enumerate(({'lateral':.5}, {'turn':.7}, {'turn':float('nan')}, {'lateral':True}), 2):
            with self.assertRaises(ValueError):
                app.control(dict(body, seq=i, **overrides))

    def test_http_origin_token_and_host(self):
        app = App({'host': '127.0.0.1', 'port': 8765, 'token': '1'*64, 'simulation': True}, 0)
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
        app.port = server.server_port
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = 'http://127.0.0.1:'+str(app.port)
        try:
            with urllib.request.urlopen(base+'/api/state') as response:
                tick = json.load(response)['browser_tick']
            body = json.dumps({'action': 'stop', 'browser': 'b'*32, 'seq': 1, 'tick': tick}).encode()
            headers = {'Origin': base, 'X-Go1-Local-Token': app.token, 'Content-Type': 'application/json'}
            for overrides in ({'Origin': 'http://untrusted.invalid'}, {'X-Go1-Local-Token': 'bad'}, {'Host': 'untrusted.invalid'}):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(urllib.request.Request(base+'/api/control', body, dict(headers, **overrides)))
                self.assertEqual(error.exception.code, 403)
            with urllib.request.urlopen(urllib.request.Request(base+'/api/control', body, headers)) as response:
                self.assertTrue(json.load(response)['ok'])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_sequence_and_freshness(self):
        app = App({'host': '127.0.0.1', 'port': 8765, 'token': '1'*64, 'simulation': True}, 8099)
        browser = 'a'*32
        tick = app.state()['browser_tick']
        body = {'action': 'stop', 'browser': browser, 'seq': 1, 'tick': tick}
        app.control(body)
        with self.assertRaises(ValueError):
            app.control(body)
        with self.assertRaisesRegex(ValueError, '过期'):
            app.control(dict(body, action='arm', seq=2, tick=0))
        with self.assertRaises(ValueError):
            app.control(dict(body, browser='invalid', seq=3))


if __name__ == '__main__':
    if not BINARY.exists():
        build_simulator()
    unittest.main(verbosity=2)
