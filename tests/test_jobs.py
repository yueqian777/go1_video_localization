"""Worker protocol tests; no robot, network or GPU required."""
import json
import builtins
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from go1loc.jobs import (JobManager, ensure_sidecar, run_request, atomic_json, execute_job,
                        scene_payload, _localization_config, read_json)


class JobTests(unittest.TestCase):
    def setUp(self):
        cache = Path(__file__).resolve().parents[1]/'.cache'/'tests'
        cache.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.map = self.root / 'map'
        self.map.mkdir()

    def test_output_rejects_both_overlap_directions(self):
        for output in (self.map, self.map/'out', self.root):
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, 'overlap'):
                ensure_sidecar(self.map, output)
        self.assertEqual(ensure_sidecar(self.map, self.root/'out'), (self.root/'out').resolve())

    def test_atomic_json_replaces_complete_json(self):
        path = self.root/'out'/'state.json'
        atomic_json(path, {'status': 'queued'})
        atomic_json(path, {'status': 'completed'})
        self.assertEqual(json.loads(path.read_text())['status'], 'completed')
        self.assertFalse(list(path.parent.glob('*.tmp')))

    def test_no_shell_or_arbitrary_callable_and_bounded_jobs(self):
        manager = JobManager(self.map, self.root/'out', max_active=1)
        with self.assertRaisesRegex(ValueError, 'unsupported_job'):
            manager.submit('shell', {'command': 'echo unsafe'})
        process = type('Process', (), {'pid': 1234, 'poll': lambda self: None})()
        with patch('go1loc.jobs.subprocess.Popen', return_value=process) as popen:
            job = manager.submit('inspect_map', {})
            self.assertFalse(popen.call_args.kwargs.get('shell', False))
            args = popen.call_args.args[0]
            self.assertIn('go1loc.jobs', args)
            with self.assertRaisesRegex(RuntimeError, 'job_capacity'):
                manager.submit('inspect_map', {})
        self.assertEqual(manager.status(job['job_id'])['status'], 'queued')

    def test_real_worker_failure_survives_manager_disconnect_and_is_durable(self):
        # No valid map exists. A real child must report failure without hanging.
        manager = JobManager(self.map, self.root/'out')
        job = manager.submit('inspect_map', {})
        manager.close()  # Browser/server ownership release must not terminate work.
        recovered = JobManager(self.map, self.root/'out')
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                state = recovered.status(job['job_id'])
                if state['status'] in ('failed', 'completed'):
                    break
                time.sleep(.05)
        finally:
            # Reap even after a failed assertion/timeout, before deleting its files.
            manager.wait(job['job_id'], timeout=10)
        self.assertEqual(state['status'], 'failed')
        self.assertNotEqual(state['worker_pid'], os.getpid())
        self.assertIn('error', state)
        self.assertFalse(list(self.map.iterdir()))

    def test_invalid_map_rejected_before_loading_native_scene_dependencies(self):
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == 'pycolmap' or name.startswith('scipy'):
                raise AssertionError('invalid map loaded native scene dependencies')
            return original_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=guarded_import):
            with self.assertRaises(FileNotFoundError):
                scene_payload(self.map)

    def test_worker_revalidates_malicious_request(self):
        request = self.root/'request.json'
        atomic_json(request, {'kind': 'inspect_map', 'job_id': 'x',
                             'map_dir': str(self.map), 'job_dir': str(self.map/'bad'),
                             'params': {}})
        with self.assertRaisesRegex(ValueError, 'overlap'):
            run_request(request)
        self.assertFalse((self.map/'bad').exists())

    def test_unknown_params_fail_before_launch(self):
        manager = JobManager(self.map, self.root/'out')
        with self.assertRaisesRegex(ValueError, 'unsupported_parameter'):
            manager.submit('inspect_map', {'output_dir': str(self.map)})
        with self.assertRaisesRegex(ValueError, 'invalid_job_id'):
            manager.status('../escape')

    def test_worker_success_and_error_states_written_atomically(self):
        directory = self.root/'out'/'job'
        request = {'kind': 'inspect_map', 'params': {}, 'map_dir': str(self.map), 'job_dir': str(directory)}
        atomic_json(directory/'request.json', request)
        atomic_json(directory/'status.json', {'sequence': 0, 'status': 'queued'})
        with patch('go1loc.jobs.execute_job', return_value={'result_path': 'scene.json'}):
            state = run_request(directory/'request.json')
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['sequence'], 2)
        with patch('go1loc.jobs.execute_job', side_effect=ValueError('bad source')):
            state = run_request(directory/'request.json')
        self.assertEqual(state['status'], 'failed')
        self.assertIn('bad source', state['error'])

    def test_launch_error_and_abnormal_worker_exit_reported(self):
        manager = JobManager(self.map, self.root/'out')
        with patch('go1loc.jobs.subprocess.Popen', side_effect=OSError('no interpreter')):
            with self.assertRaises(OSError):
                manager.submit('inspect_map', {})
        self.assertEqual(manager.list_jobs()[0]['status'], 'failed')
        process = SimpleNamespace(pid=2, poll=lambda: 1)
        with patch('go1loc.jobs.subprocess.Popen', return_value=process):
            job = manager.submit('inspect_map', {})
        self.assertEqual(manager.status(job['job_id'])['error'], 'worker_exited_without_result')

    def test_config_and_fixed_dispatch_functions(self):
        with self.assertRaisesRegex(ValueError, 'updates_disabled'):
            _localization_config({'allow_map_update': True})
        self.assertEqual(_localization_config({'calibration_path': 'cal.json'}).calibration_path, Path('cal.json'))
        base = {'map_dir': str(self.map), 'job_dir': str(self.root/'out'), 'params': {}}
        with patch('go1loc.jobs.scene_payload', return_value={'points': []}):
            result = execute_job(dict(base, kind='inspect_map'))
        self.assertEqual(read_json(result['result_path']), {'points': []})
        with patch('go1loc.photo_object.locate_photo_object') as locate:
            result = execute_job(dict(base, kind='locate_object', params={'photo': 'a.png', 'roi': [1,2,3,4]}))
            self.assertEqual(locate.call_args.kwargs['roi'], [1,2,3,4])
            self.assertTrue(result['result_path'].endswith('object_result.json'))
        with patch('go1loc.localization.localize_photo') as localize:
            execute_job(dict(base, kind='localize_current', params={'photo': 'current.png'}))
            self.assertEqual(localize.call_args.args[0], Path('current.png'))
        with patch('go1loc.snapshots.list_ready_snapshots', return_value=[]):
            with self.assertRaisesRegex(ValueError, 'no_ready_snapshot'):
                execute_job(dict(base, kind='inspect_snapshot', params={'snapshot_dir': 'snapshots'}))
        with patch('go1loc.snapshots.list_ready_snapshots', return_value=[{'map_id': 'other'}]), \
             patch('go1loc.evidence.verify_map', return_value={'map_id': 'm'}):
            with self.assertRaisesRegex(ValueError, 'snapshot_map_mismatch'):
                execute_job(dict(base, kind='inspect_snapshot', params={'snapshot_dir': 'snapshots'}))
        with patch('go1loc.snapshots.list_ready_snapshots', return_value=[{'map_id': 'm'}]), \
             patch('go1loc.evidence.verify_map', return_value={'map_id': 'm'}), \
             patch('go1loc.jobs.scene_payload', return_value={'mode': 'build_snapshot'}):
            result = execute_job(dict(base, kind='inspect_snapshot', params={'snapshot_dir': 'snapshots'}))
            self.assertEqual(read_json(result['result_path'])['mode'], 'build_snapshot')
        graph_path = self.root/'graph.json'
        atomic_json(graph_path, {'map_id': 'm', 'map_hash': 'h'})
        with patch('go1loc.planning.plan_route', return_value={'status': 'rejected', 'execution_permitted': False}), \
             patch('go1loc.evidence.verify_map', return_value={'map_id': 'm', 'manifest_sha256': 'h'}):
            result = execute_job(dict(base, kind='plan_route', params={'graph_path': str(graph_path), 'start': {}, 'target': {}}))
            self.assertEqual(read_json(result['result_path'])['status'], 'rejected')

    def test_scene_export_filters_component_caps_points_embedded_pts_only(self):
        import numpy as np
        pose = SimpleNamespace(matrix=lambda: np.eye(4)[:3])
        camera = SimpleNamespace(width=400, height=200, focal_length_y=100)
        images = {i: SimpleNamespace(cam_from_world=lambda: SimpleNamespace(inverse=lambda: pose),
                                    camera_id=1, name=f'{i}.jpg') for i in (1, 2)}
        points = {i: SimpleNamespace(xyz=np.array([i, 0., 0.]), color=np.array([1, 2, 3]),
                    track=SimpleNamespace(elements=[SimpleNamespace(image_id=1)])) for i in range(9)}
        native = SimpleNamespace(images=images, cameras={1: camera}, points3D=points)
        manifest = {'map_id': 'm', 'manifest_sha256': 'h', 'image_dir': str(self.root/'images'),
                    'model_dir': 'sfm', 'units': 'map_units', 'scale_status': 'arbitrary_monocular_scale',
                    'components': [{'component_id': 'c0', 'image_ids': [1]}],
                    'frames_manifest': {'frames': [{'kept': True, 'image_path': 'images/1.jpg', 'pts_time_s': 1.2}]}}
        with patch('go1loc.evidence.verify_map', return_value=manifest), \
             patch('pycolmap.Reconstruction', return_value=native):
            payload = scene_payload(self.map, point_budget=3)
            self.assertEqual(len(payload['points']), 3)
            self.assertEqual(len(payload['cameras']), 1)
            self.assertTrue(payload['pts_available'])
            snapshot = {'component_id': 'build0', 'snapshot_path': 'snap', 'snapshot_index': 4,
                        'frames': [{'image_name': '2.jpg', 'pts_time_s': 0.2}]}
            payload = scene_payload(self.map, snapshot=snapshot)
            self.assertEqual(payload['revision'], 4)
            self.assertEqual(payload['mode'], 'build_snapshot')
            self.assertEqual(payload['cameras'][0]['name'], '2.jpg')
            with self.assertRaisesRegex(ValueError, 'point_budget'):
                scene_payload(self.map, point_budget=50001)
            with self.assertRaisesRegex(ValueError, 'unknown_component'):
                scene_payload(self.map, component_id='missing')
            manifest['frames_manifest'] = None
            self.assertFalse(scene_payload(self.map)['pts_available'])


if __name__ == '__main__':
    unittest.main()
