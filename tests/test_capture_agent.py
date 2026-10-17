import ctypes
import io
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from go1loc import capture_agent as agent
from go1loc.capture_wire import read_frame


class Function:
    def __init__(self, value):
        self.value, self.calls = value, []

    def __call__(self, *args):
        self.calls.append(args)
        return self.value


class AgentTests(unittest.TestCase):
    def test_sampler_hang_exits_without_detaching_inflight_memory(self):
        release, entered = threading.Event(), threading.Event()
        closed = []
        def read():
            entered.set()
            release.wait(5.)
        source = SimpleNamespace(read=read, close=lambda: closed.append(True), identity={})
        fake_fcntl = SimpleNamespace(F_GETFL=1, F_SETFL=2, fcntl=lambda *_: 0)
        fake_resource = SimpleNamespace(RUSAGE_SELF=0)
        start = time.monotonic()
        try:
            with patch.dict('sys.modules', {'fcntl': fake_fcntl, 'resource': fake_resource}), \
                    patch.object(agent, 'ReadonlyShm', return_value=source), \
                    patch.object(agent.signal, 'signal'), \
                    patch.object(agent.sys, 'stdout', SimpleNamespace(fileno=lambda: 9)), \
                    patch.object(agent.os, 'O_NONBLOCK', 2048, create=True), \
                    patch.object(agent.sys, 'stderr', io.StringIO()), \
                    self.assertRaisesRegex(RuntimeError, 'sampler_read_timeout'):
                agent.main(['--authorize-readonly', '--camera', '1', '--read-timeout', '.2'])
            self.assertTrue(entered.is_set())
            self.assertEqual(closed, [])
            self.assertLess(time.monotonic() - start, 1.5)
        finally:
            release.set()

    def test_proc_segment_checks(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / 'shm'
            p.write_text('key shmid uid size ctime\n42 17 7 16 123\n')
            self.assertEqual(agent.segment_info(42, 7, 16, p)['shmid'], '17')
            for key, uid, size in ((43, 7, 16), (42, 8, 16), (42, 7, 99)):
                with self.assertRaises(RuntimeError):
                    agent.segment_info(key, uid, size, p)

    def test_attach_readonly_double_copy_and_detach_only(self):
        buffer = ctypes.create_string_buffer(bytes([1, 0, 0, 0]) + bytes(range(12)))
        libc = SimpleNamespace(shmat=Function(ctypes.addressof(buffer)), shmdt=Function(0))
        def info(*_):
            return {'key': '42', 'shmid': '17', 'uid': '7', 'size': '16', 'ctime': '123'}
        s = agent.ReadonlyShm(42, 2, 2, 7, libc, info)
        self.assertEqual(libc.shmat.calls, [(17, None, 0o10000)])
        self.assertEqual(s.read(), (bytes(range(12)), 1))
        with patch.object(agent.ctypes, 'string_at', side_effect=[b'a', b'b']):
            self.assertIsNone(s.read())
        s.close()
        s.close()
        self.assertEqual(len(libc.shmdt.calls), 1)
        self.assertEqual(buffer.raw[:16], bytes([1, 0, 0, 0]) + bytes(range(12)))

    def test_attach_failure_and_identity_race(self):
        libc = SimpleNamespace(shmat=Function(ctypes.c_void_p(-1).value), shmdt=Function(0))
        def info(*_):
            return {'shmid': '17', 'uid': '7', 'size': '16', 'ctime': '123'}
        with self.assertRaises(OSError):
            agent.ReadonlyShm(42, 2, 2, 7, libc, info)
        libc.shmat.value = 100
        with patch.object(agent, 'segment_info'):
            calls = iter([info(), dict(info(), ctime='124')])
            with self.assertRaises(RuntimeError):
                agent.ReadonlyShm(42, 2, 2, 7, libc, lambda *_: next(calls))
        self.assertEqual(len(libc.shmdt.calls), 1)

    def test_replaced_producer_segment_is_not_reported_as_live(self):
        buffer = ctypes.create_string_buffer(bytes(16))
        libc = SimpleNamespace(shmat=Function(ctypes.addressof(buffer)), shmdt=Function(0))
        row = {'key': '42', 'shmid': '17', 'uid': '7', 'size': '16', 'ctime': '123'}
        source = agent.ReadonlyShm(42, 2, 2, 7, libc, lambda *_: row.copy())
        try:
            row['shmid'] = '18'
            with self.assertRaisesRegex(RuntimeError, 'replaced'):
                source.read()
        finally:
            source.close()

    def test_bounded_agent_queue_and_partial_transport_write(self):
        q = queue.Queue(1)
        self.assertTrue(agent.offer_latest(q, 1))
        self.assertFalse(agent.offer_latest(q, 2))
        stop = threading.Event()
        with patch.object(agent.select, 'select', return_value=([], [9], [])), \
                patch.object(agent.os, 'write', side_effect=[2, 2]) as write:
            agent.write_packet(9, b'abcd', stop)
            self.assertEqual(write.call_count, 2)
        with self.assertRaises(TimeoutError):
            agent.write_packet(9, b'a', stop, timeout=-1.)
        with patch.object(agent.select, 'select', return_value=([], [9], [])), \
                patch.object(agent.os, 'write', return_value=0), self.assertRaises(BrokenPipeError):
            agent.write_packet(9, b'a', stop)

    def test_agent_main_with_owned_fake_source_never_robot_shm(self):
        source = SimpleNamespace(read=lambda: (bytes(range(12)), 4), close=lambda: None, identity={'key': '42'})
        fake_fcntl = SimpleNamespace(F_GETFL=1, F_SETFL=2, fcntl=lambda *_: 0)
        fake_resource = SimpleNamespace(RUSAGE_SELF=0, getrusage=lambda *_: SimpleNamespace(ru_utime=.1, ru_stime=.1, ru_maxrss=1))
        stdout = SimpleNamespace(fileno=lambda: 9)
        packets = []
        def write(fd, packet, stop):
            packets.append(packet)
            stop.set()
        with patch.dict('sys.modules', {'fcntl': fake_fcntl, 'resource': fake_resource}), \
                patch.object(agent, 'ReadonlyShm', return_value=source), \
                patch.object(agent.signal, 'signal'), patch.object(agent.sys, 'stdout', stdout), \
                patch.object(agent.os, 'O_NONBLOCK', 2048, create=True), \
                patch.object(agent.sys, 'stderr', io.StringIO()), patch.object(agent, 'write_packet', side_effect=write):
            self.assertEqual(agent.main(['--authorize-readonly', '--camera', '1', '--width', '2', '--height', '2']), 0)
        self.assertEqual(read_frame(io.BytesIO(packets[0]))[1], bytes(range(12)))

    def test_agent_main_denies_unapproved_or_excessive_budget(self):
        with patch.object(agent.sys, 'stderr', io.StringIO()):
            for args in (['--camera', '1'], ['--camera', '1', '--authorize-readonly', '--rate', '100'],
                         ['--camera', '1', '--authorize-readonly', '--max-mbps', '1']):
                with self.assertRaises(SystemExit):
                    agent.main(args)
