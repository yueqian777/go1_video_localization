import tempfile
import threading
import unittest
from pathlib import Path

from go1loc.capture_control import CaptureController
from tests.test_capture_session import frame


class ManualSource:
    def start(self, on_frame, on_end):
        self.on_frame, self.on_end = on_frame, on_end

    def close(self):
        self.closed = True
        self.on_end('manual_closed')


class ControlTests(unittest.TestCase):
    def test_manual_start_stop_disconnect_and_reconnect(self):
        with tempfile.TemporaryDirectory() as t:
            c, s = CaptureController(), ManualSource()
            c.connect(s)
            self.assertEqual(c.state, 'connecting')
            with self.assertRaises(RuntimeError):
                c.start_recording(t, 'clip')
            s.on_frame(frame())
            self.assertEqual(c.state, 'previewing')
            c.start_recording(t, 'clip')
            with self.assertRaises(RuntimeError):
                c.start_recording(t, 'again')
            s.on_frame(frame(2))
            c.stop_recording()
            self.assertEqual(c.state, 'previewing')
            c.disconnect()
            self.assertTrue(s.closed)
            self.assertIsNone(c.view()[1])
            c.connect(ManualSource())
            self.assertIsNone(c.recorder)
            c.disconnect()

    def test_eof_marks_interrupted_and_clears_preview(self):
        with tempfile.TemporaryDirectory() as t:
            c, s = CaptureController(), ManualSource()
            c.connect(s)
            s.on_frame(frame())
            c.start_recording(t, 'clip')
            s.on_frame(frame(2))
            s.on_end('EOF')
            self.assertEqual(c.state, 'error')
            self.assertFalse(c.view()[0]['live'])
            self.assertIn('interrupted', (Path(c.last_output) / 'session.json').read_text())
            c.disconnect()

    def test_snapshot_requires_next_frame_and_never_uses_stale(self):
        with tempfile.TemporaryDirectory() as t:
            c, s = CaptureController(), ManualSource()
            c.connect(s)
            s.on_frame(frame())
            with self.assertRaises(TimeoutError):
                c.snapshot(t, 'photo', timeout=.02)
            timer = threading.Timer(.03, lambda: s.on_frame(frame(2)))
            timer.start()
            saved = c.snapshot(t, 'photo', timeout=1.)
            timer.join()
            self.assertTrue((saved / 'source.png').exists())
            c.disconnect()
