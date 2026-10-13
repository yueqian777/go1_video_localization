import json
from pathlib import Path
import tempfile
import unittest
import numpy as np

from go1loc.video import FrameConfig, select_frame_records, frame_quality
from go1loc.calibration import Calibration, build_rectification


class FrameTests(unittest.TestCase):
    def test_pts_not_nominal_fps(self):
        rows = [{'best_effort_timestamp_time': str(t), 'pts': i} for i,t in enumerate([0,.04,.3,.34,.9])]
        selected = select_frame_records(rows, FrameConfig(interval_s=.25, max_frames=3))
        self.assertEqual([r['frame_index'] for r in selected], [0,2,4])
        self.assertEqual([r['pts_time_s'] for r in selected], [0,.3,.9])

    def test_bad_pts_rejected(self):
        with self.assertRaisesRegex(Exception, 'non_monotonic'):
            select_frame_records([{'best_effort_timestamp_time':'1'},{'best_effort_timestamp_time':'.5'}], FrameConfig())

    def test_flat_and_overexposed_rejected(self):
        q = frame_quality(np.full((40,40,3),255,np.uint8), FrameConfig())
        self.assertIn('blur', q['rejection_reasons'])
        self.assertIn('overexposure', q['rejection_reasons'])

    def test_identity_rectification(self):
        c = Calibration('synthetic unit test','pinhole',(80,60),[[60,0,40],[0,60,30],[0,0,1]],[0,0,0,0],(80,60),None,'test')
        r = build_rectification(c)
        self.assertEqual(r['map_x'].shape,(60,80))
        self.assertEqual(len(r['crop_xywh']),4)
        self.assertTrue(np.isfinite(r['output_K']).all())
