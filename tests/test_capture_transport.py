import sys
import threading
import unittest
from unittest.mock import patch

from go1loc.capture_adapter import CaptureConfig, Go1ReadonlyShmAdapter
from go1loc.capture_transport import (
    ConnectionConfig,
    PipeSource,
    load_connection,
    ssh_command,
)


class TransportTests(unittest.TestCase):
    def test_no_connection_on_construction(self):
        with patch('subprocess.Popen') as proc:
            with self.assertRaises(RuntimeError):
                Go1ReadonlyShmAdapter(CaptureConfig(mode='go1_readonly_shm'))
            adapter = Go1ReadonlyShmAdapter(CaptureConfig(mode='go1_readonly_shm'),
                                            connection=ConnectionConfig('go1-front'), authorized=True)
            source = adapter.capture()
            self.assertIsNone(source.process)
            proc.assert_not_called()

    def test_config_and_shell_values(self):
        from pathlib import Path
        config = Path(__file__).resolve().parents[1] / 'configs/capture_go1.example.json'
        self.assertTrue(load_connection(config, 5).target.endswith('.15'))
        c = ConnectionConfig('go1-front', remote_agent="/tmp/a'; echo no; 'b.py", identity_file='x', ssh_config='y')
        command = ssh_command(c, 1, authorized=True)
        self.assertIn('-i', command)
        self.assertIn('-F', command)
        self.assertIn("'\"'\"'", command[-1])  # POSIX shell quote escaping
        for cfg in (ConnectionConfig('-oProxyCommand=x'), ConnectionConfig('go1', rate=100),
                    ConnectionConfig('go1', max_mbps=1), ConnectionConfig('go1', remote_python='python'),
                    ConnectionConfig('go1', projection='screen'), ConnectionConfig('go1', owner_uid=-1)):
            with self.assertRaises(ValueError):
                ssh_command(cfg, 1, authorized=True)

    def run_source(self, code, *, timeout=1.):
        received, reasons, end = [], [], threading.Event()
        source = PipeSource([sys.executable, '-u', '-c', code], 1, source_kind='synthetic_test', timeout=timeout)
        def ended(reason):
            reasons.append(reason)
            end.set()
        source.start(received.append, ended)
        self.assertTrue(end.wait(5.), 'source worker failed to stop')
        source.close()
        return source, received, reasons

    def test_real_subprocess_binary_channel_and_stderr_drain(self):
        code = ("import sys; from go1loc.capture_wire import make_header,encode_frame; "
                "sys.stderr.buffer.write(b'x'*100000); sys.stderr.flush(); "
                "sys.stdout.buffer.write(encode_frame(make_header('a'*32,1,1,2,2,1.,1.,source_kind='synthetic_test'), bytes(range(12)))); sys.stdout.flush()")
        source, frames, reasons = self.run_source(code)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].payload, bytes(range(12)))
        self.assertEqual(source.stderr_bytes, 100000)
        self.assertLessEqual(sum(map(len, source.stderr_tail)), 8192)
        self.assertEqual(reasons, ['stream_eof'])

    def test_stall_half_packet_and_wrong_camera(self):
        for code, expected in [
            ("import time; time.sleep(5)", 'frame_timeout'),
            ("import sys; sys.stdout.buffer.write(b'G1C1')", 'truncated'),
            ("import sys; from go1loc.capture_wire import *; sys.stdout.buffer.write(encode_frame(make_header('a'*32,2,1,2,2,1.,1.,source_kind='synthetic_test'),bytes(12)))", 'unexpected_camera')]:
            _, frames, reasons = self.run_source(code, timeout=.3)
            self.assertEqual(frames, [])
            self.assertIn(expected, reasons[0])

    def test_safe_remote_error_classification_without_echoing_stderr(self):
        _, _, reasons = self.run_source("import sys; sys.stderr.write('private diagnostic details: shm_owner_or_size_mismatch\\n'); sys.exit(2)")
        self.assertEqual(reasons, ['shm_owner_or_size_mismatch'])
