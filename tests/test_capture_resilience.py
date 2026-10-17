"""Offline fault injection: no SSH, hardware, factory services or motion."""
import json
import math
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from go1loc.capture_control import CaptureController
from go1loc.capture_transport import PipeSource
from tests.test_capture_control import ManualSource
from tests.test_capture_session import frame


class HealthTests(unittest.TestCase):
    def test_packets_advancing_is_not_exposure_freshness_and_recovers(self):
        from go1loc.capture_health import FrameHealth
        health = FrameHealth(freeze_after=3.)
        original = frame()
        first = health.observe(original, now=10.)
        self.assertEqual(first['content_status'], 'unverified')
        self.assertEqual(health.observe(frame(2), now=12.)['content_status'], 'unverified')
        frozen = health.observe(frame(3), now=13.)
        self.assertEqual(frozen['content_status'], 'suspected_frozen')
        self.assertEqual(frozen['sensor_freshness'], 'unverified')
        changed = replace(frame(4), payload=b'x' + original.payload[1:])
        self.assertEqual(health.observe(changed, now=14.)['content_status'], 'changing')
        self.assertEqual(health.observe(changed, now=17.)['content_status'], 'suspected_frozen')
        health.reset()
        self.assertEqual(health.observe(changed, now=20.)['content_status'], 'unverified')
        self.assertNotIn('pc_health', original.header)

    def test_invalid_health_and_timeout_budgets(self):
        from go1loc.capture_health import FrameHealth
        for value in (0, -1, math.inf, math.nan):
            with self.assertRaises(ValueError):
                FrameHealth(freeze_after=value)
            with self.assertRaises(ValueError):
                PipeSource(['unused'], 1, source_kind='synthetic_test', timeout=value)

    def test_freeze_warning_is_saved_not_fatal_and_late_frames_ignored(self):
        with tempfile.TemporaryDirectory() as t:
            controller, source = CaptureController(freeze_after=.1), ManualSource()
            controller.connect(source)
            with patch('go1loc.capture_health.time.monotonic', return_value=10.):
                source.on_frame(frame())
                controller.start_recording(t, 'clip')
            with patch('go1loc.capture_health.time.monotonic', return_value=10.2):
                source.on_frame(frame(2))
                status, image = controller.view()
                self.assertTrue(status['transport_live'])
                self.assertFalse(status['live'])
                self.assertEqual(status['content_status'], 'suspected_frozen')
                self.assertIsNotNone(image)
                self.assertEqual(controller.state, 'recording')
            source.on_end('frame_timeout_no_live_preview')
            self.assertIsNone(controller.view()[1])
            count = controller.received
            source.on_frame(frame(3))
            self.assertEqual(controller.received, count)
            metadata = json.loads((Path(controller.last_output) / 'session.json').read_text())
            self.assertEqual(metadata['status'], 'interrupted')
            saved = (Path(controller.last_output) / 'frames.jsonl').read_text()
            self.assertIn('suspected_frozen', saved)
            controller.disconnect()
            controller.connect(ManualSource())
            self.assertEqual(controller.view()[0]['content_status'], 'waiting')
            controller.disconnect()


class PipeFaultTests(unittest.TestCase):
    def run_child(self, code, callback=None, timeout=.3):
        source = PipeSource([sys.executable, '-u', '-c', code], 1,
                            source_kind='synthetic_test', timeout=timeout)
        ended, reasons = threading.Event(), []
        def end(reason):
            reasons.append(reason)
            ended.set()
        started = time.monotonic()
        try:
            source.start(callback or (lambda _: None), end)
            self.assertTrue(ended.wait(3.), 'fault must complete without calling close()')
            self.assertIsNotNone(source.process.poll())
            self.assertLess(time.monotonic() - started, 2.5)
        finally:
            source.close()
        self.assertEqual(len(reasons), 1)
        return reasons[0]

    def test_nonzero_exit_is_not_clean_eof(self):
        self.assertEqual(self.run_child('raise SystemExit(7)'), 'source_exit_code_7')

    def test_partial_packet_blocks_then_exits_without_close(self):
        reason = self.run_child("import sys,time;sys.stdout.buffer.write(b'G1C1');sys.stdout.flush();time.sleep(20)")
        self.assertIn('frame_timeout', reason)

    def test_callback_fault_is_reported_and_owned_child_reaped(self):
        code = ("import sys,time;from go1loc.capture_wire import *;"
                "sys.stdout.buffer.write(encode_frame(make_header('a'*32,1,1,2,2,1.,1.,"
                "source_kind='synthetic_test'),bytes(12)));sys.stdout.flush();time.sleep(20)")
        def broken(_):
            raise RuntimeError('private callback detail')
        self.assertEqual(self.run_child(code, broken), 'frame_callback_failed')

    def test_owned_process_escalates_when_terminate_is_ignored(self):
        source = PipeSource(['unused'], 1, source_kind='synthetic_test')
        proc = Mock()
        proc.poll.return_value = None
        proc.wait.side_effect = [subprocess.TimeoutExpired('owned', .1), 0]
        source.process = proc
        source._shutdown()
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        self.assertEqual(proc.wait.call_count, 2)


if __name__ == '__main__':
    unittest.main()
