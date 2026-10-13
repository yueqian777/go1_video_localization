import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from go1loc.events import BoundedEventWriter, read_events


class EventTests(unittest.TestCase):
    def test_envelope_real_time_and_pts_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = BoundedEventWriter(Path(directory) / 'events.jsonl', run_id='run', map_id='map')
            event = writer.emit('frame_kept', stage='video', video_pts_s=12.75,
                                component_id='component', data={'frame_index': 17})
            self.assertEqual(event['sequence'], 1)
            self.assertEqual(event['run_id'], 'run')
            self.assertEqual(event['video_pts_s'], 12.75)
            self.assertIsNotNone(datetime.fromisoformat(event['processing_time']).tzinfo)
            self.assertIsNone(event['registered_images'])
            self.assertEqual(read_events(writer.path), [event])

    def test_count_and_bytes_bounded_final_survives_and_reconnect_has_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = BoundedEventWriter(Path(directory) / 'events.jsonl', max_events=3,
                                       max_bytes=2200, max_event_bytes=1000)
            for index in range(40):
                writer.emit('progress', data={'index': index, 'text': 'x' * 250})
            writer.emit('final')
            rows = read_events(writer.path)
            self.assertLessEqual(len(rows), 3)
            self.assertLessEqual(writer.path.stat().st_size, 2200)
            self.assertGreater(rows[0]['sequence'], 1)
            self.assertEqual(rows[-1]['event'], 'final')
            self.assertEqual(rows[-1]['sequence'], 41)
            self.assertEqual(read_events(writer.path, after_sequence=40), [rows[-1]])

    def test_reject_invalid_numbers_oversize_and_existing_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            writer = BoundedEventWriter(path, max_event_bytes=1024)
            with self.assertRaises(ValueError):
                writer.emit('bad', video_pts_s=float('nan'))
            with self.assertRaisesRegex(ValueError, 'event_too_large'):
                writer.emit('bad', data={'text': 'x' * 2048})
            with self.assertRaises(FileExistsError):
                BoundedEventWriter(path)
            self.assertEqual(writer.emit('final')['sequence'], 1)

    def test_reader_ignores_only_incomplete_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            writer = BoundedEventWriter(path)
            writer.emit('started')
            with path.open('ab') as stream:
                stream.write(b'{"unfinished":')
            self.assertEqual(len(read_events(path)), 1)
            with path.open('ab') as stream:
                stream.write(b'\n')
            with self.assertRaises(json.JSONDecodeError):
                read_events(path)

    def test_stage_failure_and_cancellation_are_truthful(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = BoundedEventWriter(Path(directory) / 'events.jsonl')
            with writer.stage('ok'):
                pass
            with self.assertRaisesRegex(RuntimeError, 'oops'):
                with writer.stage('broken'):
                    raise RuntimeError('oops')
            with self.assertRaises(KeyboardInterrupt):
                with writer.stage('cancel'):
                    raise KeyboardInterrupt()
            self.assertEqual([r['event'] for r in read_events(writer.path)],
                             ['started', 'completed', 'started', 'failed', 'started', 'cancelled'])

    def test_atomic_rotation_retries_transient_windows_sharing_violation(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            writer = BoundedEventWriter(Path(directory) / 'events.jsonl', max_events=1)
            writer.emit('started')
            real_replace = os.replace
            calls = []
            def transient(source, destination):
                calls.append(1)
                if len(calls) == 1:
                    raise PermissionError('simulated Windows reader sharing violation')
                real_replace(source, destination)
            with patch('go1loc.events.os.replace', side_effect=transient):
                writer.emit('final')
            self.assertEqual(read_events(writer.path)[0]['event'], 'final')
            self.assertEqual(len(calls), 2)

    def test_diagnostic_disk_failure_does_not_mask_original_stage_error(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = BoundedEventWriter(Path(directory) / 'events.jsonl')
            real_emit = writer.emit
            def failing(event, **kwargs):
                if event == 'failed':
                    raise OSError('disk unavailable')
                return real_emit(event, **kwargs)
            with patch.object(writer, 'emit', side_effect=failing):
                with self.assertRaisesRegex(RuntimeError, 'original worker error'):
                    with writer.stage('worker'):
                        raise RuntimeError('original worker error')

    def test_video_invalid_input_produces_failed_event(self):
        from go1loc.video import prepare_video
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = BoundedEventWriter(root / 'events.jsonl')
            with self.assertRaisesRegex(Exception, 'video_not_found'):
                prepare_video(root / 'missing.mp4', root / 'frames', events=writer)
            rows = read_events(writer.path)
            self.assertEqual(rows[-1]['event'], 'failed')
            self.assertNotIn('final', [r['event'] for r in rows])

    def test_nonfinite_pts_rejected_instead_of_silently_skipping_frames(self):
        from go1loc.video import select_frame_records, FrameConfig
        for pts in ('nan', 'inf', '-inf'):
            with self.assertRaisesRegex(Exception, 'non_finite_frame_pts'):
                select_frame_records([{'pts_time': pts}], FrameConfig())

    def test_probe_reads_reported_metadata_not_invented_frame_clock(self):
        from go1loc.video import probe_video, _ratio, _ffprobe_json
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'synthetic-video.bin'
            path.write_bytes(b'not actual video; ffprobe is explicitly mocked')
            raw = {'streams': [{'codec_type': 'video', 'width': 64, 'height': 48,
                    'avg_frame_rate': '0/0', 'r_frame_rate': '30/1', 'start_time': '4.5',
                    'time_base': '1/1000', 'tags': {'rotate': 'invalid'},
                    'side_data_list': [{'rotation': 90}]}], 'format': {'duration': '3.5'}}
            with patch('go1loc.video._ffprobe_json', return_value=raw):
                result = probe_video(path)
            self.assertEqual(result.rotation, 90)
            self.assertTrue(result.vfr_or_pts_uncertain)
            self.assertEqual(result.start_time_s, 4.5)
            self.assertEqual(result.duration_s, 3.5)
            self.assertIsNone(_ratio('bad'))
            with patch('go1loc.video.shutil.which', return_value=None):
                with self.assertRaisesRegex(Exception, 'ffprobe_not_available'):
                    _ffprobe_json(path)
            for response, error in [(SimpleNamespace(returncode=1, stderr='error'), 'ffprobe_failed'),
                                    (SimpleNamespace(returncode=0, stdout='broken'), 'ffprobe_invalid_json')]:
                with patch('go1loc.video.shutil.which', return_value='ffprobe'), \
                     patch('go1loc.video.subprocess.run', return_value=response):
                    with self.assertRaisesRegex(Exception, error):
                        _ffprobe_json(path)
            with patch('go1loc.video._ffprobe_json', return_value={'streams': []}):
                with self.assertRaisesRegex(Exception, 'no_video_stream'):
                    probe_video(path)

    def test_video_cache_does_not_claim_new_decode_or_modify_cached_artifact(self):
        from dataclasses import asdict
        from go1loc.video import FrameConfig, VideoProbe, prepare_video
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frames = root / 'frames'
            frames.mkdir()
            frame = frames / 'frame.png'
            frame.write_bytes(b'fixture')
            config = FrameConfig()
            manifest = {'signature': {'video_sha256': 'fixture', 'config': asdict(config),
                                     'calibration_sha256': None},
                        'frames': [{'kept': True, 'image_path': 'frame.png'}],
                        'candidate_count': 1, 'kept_count': 1}
            saved = frames / 'frames_manifest.json'
            saved.write_text(json.dumps(manifest), encoding='utf-8')
            before = saved.read_bytes()
            probe = VideoProbe('fixture', 'fixture', 'mock')
            with patch('go1loc.video.probe_video', return_value=probe):
                result = prepare_video(root / 'synthetic.mp4', frames)
                frame.unlink()
                with self.assertRaisesRegex(Exception, 'cached_frames_missing'):
                    prepare_video(root / 'synthetic.mp4', frames)
                with self.assertRaisesRegex(Exception, 'signature_mismatch'):
                    prepare_video(root / 'synthetic.mp4', frames, FrameConfig(interval_s=0.25))
            self.assertEqual(result, manifest)
            self.assertEqual(saved.read_bytes(), before)
            logs = list(root.glob('frames.sidecar/*/events.jsonl'))
            cached = next(read_events(log) for log in logs if any(r['event'] == 'cache_hit' for r in read_events(log)))
            self.assertNotIn('video_decode', [r['stage'] for r in cached])

    def test_video_decode_and_probe_failures_are_logged(self):
        from go1loc.video import prepare_video, VideoProbe
        from types import SimpleNamespace
        cases = [([SimpleNamespace(returncode=1, stderr='probe error')], 'frame_probe_failed'),
                 ([SimpleNamespace(returncode=0, stdout='{"frames":[]}')], 'no_frames'),
                 ([SimpleNamespace(returncode=0, stdout='{"frames":[{"pts_time":"0.0"}]}'),
                   SimpleNamespace(returncode=1, stderr='decoder error')], 'decode_failed'),
                 ([SimpleNamespace(returncode=0, stdout='{"frames":[{"pts_time":"0.0"}]}'),
                   SimpleNamespace(returncode=0)], 'decoded_frame_count')]
        for responses, message in cases:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                events = BoundedEventWriter(root / 'events.jsonl')
                with patch('go1loc.video.probe_video', return_value=VideoProbe('fixture', 'hash', 'mock')), \
                     patch('go1loc.video.subprocess.run', side_effect=responses):
                    with self.assertRaisesRegex(Exception, message):
                        prepare_video(root / 'fixture.mp4', root / 'frames', events=events)
                self.assertEqual(read_events(events.path)[-1]['event'], 'failed')

    def test_video_kept_and_rejected_events_preserve_actual_pts(self):
        import numpy as np
        from types import SimpleNamespace
        from go1loc.video import FrameConfig, VideoProbe, prepare_video
        import cv2
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = BoundedEventWriter(root / 'events.jsonl')
            probe = VideoProbe('synthetic fixture', 'fixture-sha', 'mock', width=32, height=32)
            rows = [{'pts': 71, 'best_effort_timestamp_time': '2.3'},
                    {'pts': 103, 'best_effort_timestamp_time': '3.1'}]
            def run(command, **kwargs):
                if '-show_frames' in command:
                    return SimpleNamespace(returncode=0, stdout=json.dumps({'frames': rows}), stderr='')
                for number in (1, 2):
                    cv2.imencode('.png', np.zeros((32, 32, 3), dtype=np.uint8))[1].tofile(
                        str(root / 'frames' / 'raw' / f'{number:06d}.png'))
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            with patch('go1loc.video.probe_video', return_value=probe), \
                 patch('go1loc.video.subprocess.run', side_effect=run), \
                 patch('go1loc.video.frame_quality', side_effect=[
                     {'rejection_reasons': [], 'mean_change_64px': None},
                     {'rejection_reasons': ['blur'], 'mean_change_64px': 5.0}]):
                result = prepare_video(root / 'fixture.mp4', root / 'frames', FrameConfig(), events=writer)
            frames = [r for r in read_events(writer.path) if r['event'].startswith('frame_')]
            self.assertEqual([r['video_pts_s'] for r in frames], [2.3, 3.1])
            self.assertEqual([r['event'] for r in frames], ['frame_kept', 'frame_rejected'])
            self.assertEqual(result['kept_count'], 1)
            self.assertEqual(read_events(writer.path)[-1]['event'], 'final')


if __name__ == '__main__':
    unittest.main()
