"""Browser workflow contracts; synthetic mocks are not reconstruction acceptance."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from go1loc.jobs import atomic_json, read_json
from go1loc.web_build import save_video_upload, validate_settings, execute_web_job, WebBuildSession


class WebBuildTests(unittest.TestCase):
    def setUp(self):
        cache = Path(__file__).resolve().parents[1]/'.cache/tests'
        cache.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.probe = dict(width=640, height=480, duration_s=42., source_sha256='a'*64)
        self.settings = dict(kind='screen', confirmed=True, duration=30., interval=.5,
                             start=0., exploratory=False, crop=[0, 0, 640, 480],
                             exclusions=[], window='front view', display_mode='unknown',
                             display_evidence='', camera_id='', camera_label='前方（用户描述）')

    def test_upload_has_unique_server_name_and_byte_identity(self):
        a = save_video_upload(self.root, '../../same.mp4', b'original')
        b = save_video_upload(self.root, '../../same.mp4', b'original')
        self.assertNotEqual(a['upload_id'], b['upload_id'])
        self.assertEqual(a['sha256'], b['sha256'])
        self.assertEqual(Path(a['path']).read_bytes(), b'original')
        self.assertTrue(Path(a['path']).is_relative_to(self.root/'uploads'))
        self.assertFalse((self.root.parent/'same.mp4').exists())

    def test_bad_upload_and_settings_rejected(self):
        for name, data in [('x.exe', b'xx'), ('x.mp4', b'')]:
            with self.assertRaises(ValueError):
                save_video_upload(self.root, name, data)
        with patch('go1loc.web_build.MAX_UPLOAD_BYTES', 2), self.assertRaises(ValueError):
            save_video_upload(self.root, 'x.mp4', b'123')
        for update in [dict(confirmed=False), dict(duration=float('nan')),
                       dict(start=-1), dict(interval=0), dict(duration=600, interval=.01),
                       dict(crop=[0, 0, 641, 480]), dict(exploratory='false'),
                       dict(display_mode='already_rectified', display_evidence='')]:
            with self.subTest(update=update), self.assertRaises((ValueError, RuntimeError)):
                validate_settings(dict(self.settings, **update), self.probe)

    def test_screen_preserves_unknown_geometry_and_explicit_quality_choice(self):
        frames, screen = validate_settings(self.settings, self.probe)
        self.assertTrue(frames.quality_filter)
        self.assertEqual(frames.max_frames, 60)
        self.assertEqual(screen.expected_source_sha256, 'a'*64)
        self.assertIsNone(screen.camera_id)
        self.assertEqual(screen.display_mode, 'unknown')
        frames, _ = validate_settings(dict(self.settings, exploratory=True), self.probe)
        self.assertFalse(frames.quality_filter)

    def request(self):
        upload = save_video_upload(self.root, 'video.mp4', b'original')
        self.probe['source_sha256'] = upload['sha256']
        atomic_json(Path(upload['path']).parent/'inspection.json', {'probe': self.probe})
        directory = self.root/'worker/jobs'/('1'*32)
        directory.mkdir(parents=True)
        return dict(kind='build_video', job_dir=str(directory),
                    params=dict(upload_id=upload['upload_id'], settings=self.settings)), upload

    def test_pipeline_uses_existing_algorithms_and_new_output_only(self):
        request, upload = self.request()
        with patch('go1loc.video.prepare_video', return_value={'candidate_count': 60, 'kept_count': 60}), \
             patch('go1loc.mapping.build_map', return_value={'map_id': 'test'}) as build:
            result = execute_web_job(request)
        args = build.call_args.args
        self.assertEqual(args[0].parts[-2:], ('prepared', 'images'))
        self.assertEqual(args[1].name, 'map')
        self.assertEqual(args[2].snapshot_frames_freq, 5)
        self.assertEqual(args[2].matcher, 'superpoint+lightglue')
        self.assertEqual(args[2].max_keypoints, 2048)
        self.assertFalse(read_json(result['result_path'])['execution_permitted'])
        self.assertEqual(Path(upload['path']).read_bytes(), b'original')

    def test_insufficient_frames_stop_before_mapping_and_keep_report(self):
        request, _ = self.request()
        with patch('go1loc.video.prepare_video', return_value={'candidate_count': 60, 'kept_count': 1}), \
             patch('go1loc.mapping.build_map') as build:
            with self.assertRaisesRegex(ValueError, 'quality_insufficient'):
                execute_web_job(request)
        build.assert_not_called()
        progress = read_json(Path(request['job_dir'])/'web_progress.json')
        self.assertEqual(progress['kept_count'], 1)

    def test_tampered_upload_refused_and_arbitrary_paths_not_accepted(self):
        request, upload = self.request()
        Path(upload['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'hash'):
            execute_web_job(request)
        request['params']['upload_id'] = '../../escape'
        with self.assertRaises(ValueError):
            execute_web_job(request)

    def test_session_persists_selection_and_refuses_duplicate_active_job(self):
        process = type('Process', (), {'poll': lambda self: None})()
        session = WebBuildSession(self.root)
        self.assertIsNone(session.state())
        with patch('go1loc.jobs.subprocess.Popen', return_value=process):
            job = session.upload('valid.mp4', b'original')
            with self.assertRaisesRegex(ValueError, '尚未结束'):
                session.upload('another.mp4', b'other')
            with self.assertRaises(ValueError):
                session.start(self.settings)
        recovered = WebBuildSession(self.root)
        self.assertEqual(recovered.record, session.record)
        self.assertEqual(recovered.state()['job_id'], job['job_id'])
        self.assertIsNone(recovered.inspection())
        inspection = self.root/'inspection.json'
        atomic_json(inspection, {'probe': self.probe})
        atomic_json(session.jobs.root/job['job_id']/'status.json', dict(job, status='completed', result_path=str(inspection)))
        self.assertEqual(recovered.inspection()['probe'], self.probe)
        with patch('go1loc.jobs.subprocess.Popen', return_value=process):
            build = recovered.start(self.settings)
        self.assertEqual(recovered.record['build_job'], build['job_id'])
        self.assertEqual(recovered.state()['kind'], 'build_video')
        self.assertEqual(WebBuildSession(self.root).record['settings'], self.settings)

    def test_inspection_decodes_mp4_and_checks_dimensions(self):
        from go1loc.video import VideoProbe
        request, upload = self.request()
        request['kind'] = 'inspect_video'
        result = {'probe': self.probe, 'preview': 'native.png'}
        with patch('go1loc.video.probe_video', return_value=VideoProbe('x', upload['sha256'], 'test', width=640, height=480)), \
             patch('go1loc.screen_import.inspect_screen', return_value=result) as inspect:
            output = execute_web_job(request)
        self.assertEqual(read_json(output['result_path']), result)
        inspect.assert_called_once()
        with patch('go1loc.video.probe_video', return_value=VideoProbe('x', '', 'test', width=99999, height=2)):
            with self.assertRaises(ValueError):
                execute_web_job(request)

    def test_wrong_inspection_or_existing_output_never_overwritten(self):
        request, upload = self.request()
        inspection = Path(upload['path']).parent/'inspection.json'
        atomic_json(inspection, {'probe': dict(self.probe, source_sha256='b'*64)})
        with self.assertRaisesRegex(ValueError, 'inspection_hash'):
            execute_web_job(request)
        atomic_json(inspection, {'probe': self.probe})
        output = Path(request['job_dir'])/'output'
        output.mkdir()
        with self.assertRaisesRegex(ValueError, 'output_exists'):
            execute_web_job(request)

    def test_video_mode_and_insufficient_scene_mask(self):
        _, screen = validate_settings(dict(self.settings, kind='video'), self.probe)
        self.assertIsNone(screen)
        for change in (dict(kind='fake'), dict(start=43), dict(exclusions=[[0, 0, 640, 480]]),
                       dict(exclusions=[[0, 0, 1, 1]]*33)):
            with self.assertRaises(ValueError):
                validate_settings(dict(self.settings, **change), self.probe)


if __name__ == '__main__':
    unittest.main()
