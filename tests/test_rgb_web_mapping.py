"""Offline integration contracts; synthetic pixels are not camera accuracy tests."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from go1loc.jobs import atomic_json, read_json
from go1loc.web_build import WebBuildSession, _upload, execute_web_job, save_video_upload
from go1loc.capture_session import Recorder, SourceFrame
from go1loc.capture_wire import make_header, encode_frame
from go1loc.capture_import import prepare_capture, export_ffv1
from go1loc.video import FrameConfig
from go1loc.planning import build_route_graph, plan_route
from go1loc.route_review import generate_route_candidates
from test_planning import fixture, inputs, NOW


def clip(root):
    def frame(n):
        header = make_header('a'*32, 6, n, 40, 32, 10+n*.5, 100+n*.5,
                             source_kind='synthetic_test', camera_serial='test-camera')
        payload = bytes([50, 100, 150]) * (40*32)
        encode_frame(header, payload)
        return SourceFrame(header, payload, NOW, 1000+n*.5)
    recorder = Recorder(root/'clip', frame(0))
    for n in range(1, 5):
        recorder.submit(frame(n))
    recorder.finish()
    return recorder.path


class RGBWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.captures = self.root/'captures'
        self.captures.mkdir()
        self.source = clip(self.captures)
        self.patcher = patch('go1loc.web_capture.CAPTURE_ROOT', self.captures)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.settings = dict(kind='capture', confirmed=True, exploratory=True,
                             start=0., duration=1.5, interval=.5)

    def calibration(self, **changes):
        value = dict(source='synthetic-test-only-not-real-calibration', model='opencv',
            image_size=[40, 32], output_size=[40, 32], K=[[30, 0, 20], [0, 30, 16], [0, 0, 1]],
            distortion=[0, 0, 0, 0, 0], map_version='test',
            capture_camera_id=6, capture_source_kind='synthetic_test', capture_camera_serial='test-camera',
            capture_projection='raw_pinhole', projection_evidence='Synthetic pinhole test fixture')
        value.update(changes)
        path = self.root/'calibration.json'
        atomic_json(path, value)
        return path

    def test_mkv_keeps_bytes_extension_and_validates_hash(self):
        row = save_video_upload(self.root/'web', '../../rgb.MKV', b'test-container')
        path, meta = _upload(self.root/'web', row['upload_id'])
        self.assertEqual(path.name, 'source.mkv')
        self.assertEqual(path.read_bytes(), b'test-container')
        path.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'hash'):
            _upload(self.root/'web', row['upload_id'])

    def test_real_mkv_container_decodes_through_upload_worker(self):
        exported = export_ffv1(self.source)
        session = WebBuildSession(self.root/'web')
        with patch('go1loc.jobs.subprocess.Popen'):
            job = session.upload('camera.mkv', Path(exported['video']).read_bytes())
        request = read_json(session.jobs.root/job['job_id']/'request.json')
        output = execute_web_job(request)
        inspection = read_json(output['result_path'])
        self.assertEqual((inspection['probe']['width'], inspection['probe']['height']), (40, 32))
        self.assertTrue(Path(inspection['preview']).is_file())
        # Bare MKV is not falsely relabeled native RGB without its source session.
        self.assertNotEqual(inspection.get('source_kind'), 'orbbec_rgb_mjpeg')

    def select(self):
        session = WebBuildSession(self.root/'web')
        process = type('Process', (), {'poll': lambda self: None})()
        with patch('go1loc.jobs.subprocess.Popen', return_value=process):
            job = session.select_capture('clip')
        request = read_json(session.jobs.root/job['job_id']/'request.json')
        output = execute_web_job(request)
        atomic_json(session.jobs.root/job['job_id']/'status.json', dict(job, status='completed', **output))
        return session

    def test_native_capture_selection_preview_and_worker_preserve_provenance(self):
        from go1loc.web_capture import list_captures
        self.assertEqual([row['capture_id'] for row in list_captures()], ['clip'])
        session = self.select()
        inspection = session.inspection()
        self.assertEqual(inspection['input_kind'], 'capture')
        self.assertEqual(inspection['probe']['duration_s'], 1.5)
        self.assertEqual(inspection['source_kind'], 'synthetic_test')
        self.assertIsNone(inspection['sensor_capture_time'])
        with patch('go1loc.jobs.subprocess.Popen'):
            job = session.start(self.settings)
        request = read_json(session.jobs.root/job['job_id']/'request.json')
        with patch('go1loc.mapping.build_map', return_value={'map_id': 'synthetic'}) as build:
            execute_web_job(request)
        prepared = read_json(Path(request['job_dir'])/'output/prepared/frames_manifest.json')
        self.assertEqual(prepared['source_kind'], 'synthetic_test')
        self.assertEqual(prepared['timing_source'], 'agent_read_monotonic_not_exposure')
        self.assertIsNone(prepared['sensor_capture_time'])
        self.assertFalse(prepared['execution_permitted'])
        self.assertEqual(prepared['frames'][0]['source_capture']['agent_seq'], 1)
        self.assertEqual(prepared['frames'][0]['pts_time_s'], 10.5)
        self.assertEqual(build.call_args.args[2].matcher, 'superpoint+lightglue')
        self.assertEqual(prepared['route_segments'], [])

    def test_capture_escape_incomplete_and_changed_source_rejected(self):
        from go1loc.web_capture import list_captures
        session = WebBuildSession(self.root/'web')
        with self.assertRaises(ValueError):
            session.select_capture('../clip')
        (self.source/'INCOMPLETE').touch()
        self.assertEqual(list_captures(), [])
        with self.assertRaises(ValueError):
            session.select_capture('clip')
        (self.source/'INCOMPLETE').unlink()
        session = self.select()
        meta = read_json(self.source/'session.json')
        atomic_json(self.source/'session.json', dict(meta, note='changed'))
        with self.assertRaisesRegex(ValueError, 'changed|hash'):
            session.start(self.settings)

    def test_pinhole_calibration_binds_camera_source_serial_and_resolution(self):
        result = prepare_capture(self.source, self.root/'calibrated',
            FrameConfig(interval_s=.5, quality_filter=False), self.calibration())
        self.assertEqual(result['geometry']['status'], 'calibrated_rectified')
        self.assertEqual(result['geometry']['source_projection'], 'unknown')
        self.assertEqual(result['geometry']['calibration_projection'], 'raw_pinhole')
        self.assertEqual(result['source_session']['source']['projection'], 'unknown')
        for change in (dict(capture_camera_id=1), dict(capture_camera_serial='other'),
                       dict(capture_source_kind='video'), dict(projection_evidence=''),
                       dict(image_size=[80, 64]), dict(model='fisheye')):
            with self.subTest(change=change), self.assertRaises(ValueError):
                prepare_capture(self.source, self.root/'rejected', FrameConfig(), self.calibration(**change))
        self.assertFalse((self.root/'rejected').exists())

    def test_calibration_upload_is_bound_to_selected_input_and_never_silent(self):
        session = self.select()
        row = session.set_calibration('calibration.json', self.calibration().read_bytes())
        self.assertTrue(row['sha256'])
        self.assertEqual(session.record['calibration']['input_sha256'], session.record['upload']['sha256'])
        with patch('go1loc.jobs.subprocess.Popen'):
            state = session.start(self.settings)
        params = read_json(session.jobs.root/state['job_id']/'request.json')['params']
        self.assertEqual(params['settings']['calibration_id'], row['calibration_id'])

    def test_calibration_guards_clear_and_input_mode(self):
        from go1loc.web_capture import resolve_calibration
        session = self.select()
        for name, content in [('x.exe', b'{}'), ('x.json', b'{}' * 40000)]:
            with self.assertRaises(ValueError):
                session.set_calibration(name, content)
        for changes in (dict(valid_mask_path='../outside.png'), dict(output_size=[8192,8192])):
            with self.assertRaises(ValueError):
                session.set_calibration('x.json', self.calibration(**changes).read_bytes())
        row = session.set_calibration('x.json', self.calibration().read_bytes())
        folder = session.root/'uploads'/session.record['upload']['upload_id']/'calibrations'/row['calibration_id']
        (folder/'calibration.json').write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, 'hash_mismatch'):
            session.start(self.settings)
        with self.assertRaisesRegex(ValueError, 'invalid_calibration_id'):
            resolve_calibration(session.root, session.record['upload'], session.inspection(), '../x')
        session.clear_calibration()
        self.assertNotIn('calibration', session.record)
        with self.assertRaisesRegex(ValueError, '模式'):
            session.start(dict(self.settings, kind='video'))

    def test_projection_conflict_and_rectified_calibration(self):
        path = self.source/'session.json'
        meta = read_json(path)
        # Unit check binding only; never rewrite a real source session.
        from go1loc.capture_import import capture_calibration
        meta['source']['projection'] = 'rectified_pinhole'
        with self.assertRaisesRegex(ValueError, 'conflict'):
            capture_calibration(self.calibration(), meta)
        with self.assertRaisesRegex(ValueError, 'zero_distortion'):
            capture_calibration(self.calibration(capture_projection='rectified_pinhole', distortion=[.1,0,0,0]), meta)
        cal, projection, _ = capture_calibration(self.calibration(capture_projection='rectified_pinhole'), meta)
        self.assertEqual(projection, 'rectified_pinhole')

    def test_plain_video_calibration_is_validated_and_forwarded_to_pipeline(self):
        from go1loc.web_capture import save_calibration, resolve_calibration
        row = save_video_upload(self.root/'web', 'video.mp4', b'test-container')
        inspection = {'probe': {'width': 40, 'height': 32, 'source_sha256': row['sha256'], 'duration_s': 10.}}
        data = self.calibration(input_projection='raw_pinhole').read_bytes()
        saved = save_calibration(self.root/'web', row, inspection, 'cal.json', data)
        path = resolve_calibration(self.root/'web', row, inspection, saved['calibration_id'])
        directory = self.root/'web/worker/jobs'/('b'*32)
        directory.mkdir(parents=True)
        atomic_json(Path(row['path']).parent/'inspection.json', inspection)
        request = dict(kind='build_video', job_dir=str(directory), params=dict(upload_id=row['upload_id'],
            settings=dict(self.settings, kind='video', calibration_id=saved['calibration_id'])))
        with patch('go1loc.video.prepare_video', return_value={'candidate_count': 3, 'kept_count': 3}) as prepare, \
             patch('go1loc.mapping.build_map', return_value={'map_id': 'test'}):
            execute_web_job(request)
        self.assertEqual(prepare.call_args.kwargs['calibration_path'], path)
        for changes in (dict(input_projection='unknown'), dict(input_projection='raw_fisheye'),
                        dict(input_projection='rectified_pinhole', distortion=[.1,0,0,0]), dict(projection_evidence='')):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                save_calibration(self.root/'web', row, inspection, 'cal.json', self.calibration(**changes).read_bytes())

    def test_input_ids_and_status_cannot_be_forged(self):
        from go1loc.web_capture import capture_path, list_captures, register_capture
        for bad in ('../clip', 'missing', 'clip/frames', ''):
            with self.assertRaises(ValueError):
                capture_path(bad)
        meta_path = self.source/'session.json'
        meta = read_json(meta_path)
        atomic_json(meta_path, dict(meta, status='recording'))
        self.assertEqual(list_captures(), [])
        with self.assertRaises(ValueError):
            register_capture(self.root/'web', 'clip')
        atomic_json(meta_path, meta)
        row = save_video_upload(self.root/'web', 'valid.mp4', b'bytes')
        atomic_json(Path(row['path']).parent/'source.json', dict(row, upload_id='different'))
        with self.assertRaisesRegex(ValueError, 'identity_mismatch'):
            _upload(self.root/'web', row['upload_id'])


class RGBRouteTests(unittest.TestCase):
    def graph(self):
        manifest, refs, frames = fixture()
        frames['source_kind'] = 'orbbec_rgb_mjpeg'
        frames['route_segments'][0]['source_kind'] = 'orbbec_rgb_mjpeg'
        return manifest, refs, frames

    def test_rgb_source_can_be_reviewed_but_does_not_auto_approve(self):
        m, r, f = self.graph()
        review = generate_route_candidates(m, r, f)
        self.assertTrue(review['candidates'])
        self.assertTrue(all(c['status'] == 'PENDING' for c in review['candidates']))
        self.assertEqual(build_route_graph(m, r, f, route_segments=[])['edges'], {})
        self.assertTrue(build_route_graph(m, r, f)['edges'])

    def test_avoidance_assumption_adds_itinerary_not_edges_or_execution(self):
        graph = build_route_graph(*self.graph())
        start, target = inputs(graph)
        result = plan_route(graph, start, target, now=NOW, local_avoidance_assumed=True)
        self.assertEqual(result['status'], 'route_preview_only')
        self.assertFalse(result['execution_permitted'])
        self.assertEqual(result['avoidance_context']['state'], 'assumed_not_verified')
        self.assertEqual(len(result['itinerary']), 3)
        self.assertEqual(result['itinerary'][-1]['distance_from_start'], 4.)
        self.assertAlmostEqual(result['itinerary'][1]['turn_angle_deg'], 90.)
        self.assertEqual(result['units'], 'map_units')
        disabled = [next(iter(graph['edges']))]
        blocked = plan_route(graph, start, target, now=NOW, disabled_edge_ids=disabled,
                             local_avoidance_assumed=True)
        self.assertEqual(blocked['failure_reason'], 'no_path')
        self.assertEqual(blocked['disabled_edge_ids'], disabled)
        target['supporting_frame_ids'] = ['wall.png']
        self.assertEqual(plan_route(graph, start, target, now=NOW, local_avoidance_assumed=True)
                         ['failure_reason'], 'no_path')


if __name__ == '__main__':
    unittest.main()
