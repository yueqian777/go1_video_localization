import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from go1loc.capture_adapter import CaptureConfig, CaptureAdapter, OfflineVideoAdapter, Go1ReadonlyShmAdapter
from go1loc.cli import main
from go1loc.evaluation import evaluate_results
from go1loc.geometry import quaternion_wxyz_to_rotation, project_points_pinhole, transform_points
from go1loc.viewer import prepare_viewer


class OfflineSmokeTests(unittest.TestCase):
    def test_robot_capture_remains_disabled_for_every_mode(self):
        with self.assertRaises(NotImplementedError):
            CaptureAdapter().capture()
        with self.assertRaisesRegex(ValueError,'requires_input'):
            OfflineVideoAdapter(CaptureConfig())
        self.assertEqual(OfflineVideoAdapter(CaptureConfig(input_path=Path('x.mp4'))).capture(),Path('x.mp4'))
        with self.assertRaisesRegex(ValueError,'invalid_capture'):
            Go1ReadonlyShmAdapter(CaptureConfig())
        with self.assertRaisesRegex(RuntimeError,'disabled'):
            Go1ReadonlyShmAdapter(CaptureConfig(mode='go1_readonly_shm'))

    def test_geometry_invalid_inputs(self):
        for q in ([0,0,0,0],[1,2],[float('nan'),0,0,0]):
            with self.assertRaises(ValueError): quaternion_wxyz_to_rotation(q)
        for p,fx in [([[0,0,-1]],1),([[1,2]],1),([[0,0,1]],-1)]:
            with self.assertRaises(ValueError): project_points_pinhole(p,fx=fx,fy=1,cx=0,cy=0)
        np.testing.assert_allclose(transform_points(np.eye(3),[1,0,0],[[0,0,1]]),[[1,0,1]])
        for r,t,p in [([1],[0,0,0],[[1,1,1]]),(np.eye(3),[1,2],[[1,1,1]]),(np.eye(3),[0,0,0],[1,1])]:
            with self.assertRaises(ValueError):transform_points(r,t,p)

    def test_evaluation_retains_failed_and_successful_queries(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); inp=root/'input.json'; out=root/'report.json'
            inp.write_text(json.dumps([{'query_id':'ok','pose_status':'localized'},
                                      {'query_id':'bad','pose_status':'insufficient_evidence','failure_reason':'quality'}]))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['evaluate','--results',str(inp),'--output',str(out)]),0)
            payload=json.loads(out.read_text()); self.assertEqual(payload['num_queries'],2)
            self.assertIn('not computed',payload['position_error'])
            inp.write_text('17')
            with self.assertRaises(ValueError): evaluate_results(inp)

    def test_legacy_viewer_xss_and_map_guards(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); side=root/'side'; prepare_viewer(side,{'pose_status':'<script>evil</script>'})
            html=(side/'index.html').read_text(encoding='utf-8')
            self.assertNotIn('innerHTML',html)
            self.assertIn('textContent',html)
            (root/'map_manifest.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'frozen_map'):prepare_viewer(root)
            with self.assertRaisesRegex(ValueError,'frozen_map'):prepare_viewer(root/'child')

    def test_doctor_and_mapping_cli_propagate_errors(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with patch('go1loc.cli.collect_doctor',return_value={'robot_access':'disabled'}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['doctor','--output',str(root/'doctor.json')]),0)
            self.assertEqual(json.loads((root/'doctor.json').read_text())['robot_access'],'disabled')
            with contextlib.redirect_stdout(io.StringIO()), patch('go1loc.cli.build_map',side_effect=ValueError('invalid_input')):
                self.assertNotEqual(main(['build-map','--images',str(root),'--output',str(root/'map')]),0)

    def test_route_cli_frozen_and_existing_output_guards(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'map_manifest.json').write_text('{}')
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertNotEqual(main(['plan-route','--graph','none','--start','none','--target','none','--output',str(root/'p.json')]),0)
            self.assertIn('frozen_map',out.getvalue())
            self.assertFalse((root/'p.json').exists())
