import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from go1loc.capture_session import Recorder, SourceFrame
from go1loc.capture_transport import ConnectionConfig, ssh_command
from go1loc.capture_wire import encode_frame
from tests.test_capture_wire import sample


def frame(seq=1, camera=1, session='a' * 32):
    h, p = sample(seq, camera, session)
    encode_frame(h, p)
    return SourceFrame(h, p, '2026-10-15T00:00:00+00:00', time.monotonic())


class SessionTests(unittest.TestCase):
    def test_source_colors_and_start_stop_boundary(self):
        with tempfile.TemporaryDirectory() as t:
            recorder = Recorder(Path(t) / 'clip', frame())
            self.assertFalse(recorder.submit(frame()))  # preview frame is not recorded again
            self.assertTrue(recorder.submit(frame(2)))
            recorder.finish()
            self.assertFalse(recorder.submit(frame(3)))
            rows = [json.loads(x) for x in (recorder.path / 'frames.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            im = np.asarray(Image.open(recorder.path / rows[0]['image_path']))
            self.assertEqual(im[0, 0].tolist(), [255, 10, 0])
            self.assertIsNone(rows[0]['sensor_capture_time'])
            self.assertEqual(json.loads((recorder.path / 'session.json').read_text())['status'], 'complete')

    def test_camera_switch_and_reconnect_interrupt(self):
        for changed in (frame(2, camera=2), frame(2, session='b' * 32)):
            with tempfile.TemporaryDirectory() as t:
                r = Recorder(Path(t) / 'clip', frame())
                with self.assertRaises(ValueError):
                    r.submit(changed)
                r.finish('interrupted')
                self.assertEqual(r.written, 0)

    def test_queue_overflow_is_bounded(self):
        with tempfile.TemporaryDirectory() as t:
            r = Recorder(Path(t) / 'clip', frame(), queue_size=1, start_worker=False)
            self.assertTrue(r.submit(frame(2)))
            self.assertFalse(r.submit(frame(3)))
            self.assertEqual(r.dropped, 1)
            r.start_worker()
            r.finish()
            self.assertEqual(r.written, 1)

    def test_disk_failure_preserves_written(self):
        with tempfile.TemporaryDirectory() as t:
            r = Recorder(Path(t) / 'clip', frame())
            r.submit(frame(2))
            r.queue.join()
            with patch('go1loc.capture_session.save_png', side_effect=OSError('disk full')):
                r.submit(frame(3))
                r.queue.join()
            r.finish()
            self.assertEqual(r.written, 1)
            self.assertEqual(json.loads((r.path / 'session.json').read_text())['status'], 'interrupted')
            self.assertTrue((r.path / 'frames/00000000.png').exists())

    def test_ssh_requires_explicit_authorization_and_safe_argv(self):
        c = ConnectionConfig(target='unitree@192.168.123.13', jump='pi@192.168.12.1')
        with self.assertRaises(PermissionError):
            ssh_command(c, 1, authorized=False)
        argv = ssh_command(c, 1, authorized=True)
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertIn('BatchMode=yes', argv)
        self.assertIn('-T', argv)
        with self.assertRaises(ValueError):
            ssh_command(ConnectionConfig(target='x; touch /tmp/x'), 1, authorized=True)


if __name__ == '__main__':
    unittest.main()
