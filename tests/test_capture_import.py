import json
import tempfile
import unittest
from pathlib import Path

from go1loc.capture_import import export_ffv1, load_recording, prepare_capture
from go1loc.capture_session import Recorder
from go1loc.mapping import _video_references
from go1loc.video import FrameConfig
from tests.test_capture_session import frame


class ImportTests(unittest.TestCase):
    def make_clip(self, root):
        r = Recorder(root / 'clip', frame())
        for seq in (2, 3, 5):
            r.submit(frame(seq))
        r.finish()
        return r.path

    def test_source_order_and_unknown_exposure_are_preserved(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            clip = self.make_clip(root)
            result = prepare_capture(clip, root / 'prepared', FrameConfig(interval_s=.01, quality_filter=False))
            self.assertEqual(result['source_kind'], 'synthetic_test')
            self.assertEqual([r['pts_time_s'] for r in result['frames']], [.2, .3, .5])
            self.assertIsNone(result['sensor_capture_time'])
            self.assertEqual(result['route_segments'], [])
            self.assertNotIn('feature_mask', result)
            self.assertTrue(result['frames'][-1]['discontinuity_before'])
            refs = [Path(r['image_path']).name for r in result['frames']]
            ordered, _, _ = _video_references((root / 'prepared/images').resolve(), refs[::-1])
            self.assertEqual(ordered, refs)

    def test_new_source_types_do_not_bypass_route_review(self):
        from go1loc.planning import build_route_graph
        from tests.test_planning import fixture
        manifest, refs, frames = fixture()
        frames['source_kind'] = 'go1_shm_readonly'
        frames['timing_source'] = 'agent_read_monotonic_not_exposure'
        graph = build_route_graph(manifest, refs, frames, route_segments=[])
        self.assertEqual(graph['edges'], {})
        segment = frames['route_segments'][0]
        segment['source_kind'] = 'go1_shm_readonly'
        frames['frames'][1]['discontinuity_before'] = True
        graph = build_route_graph(manifest, refs, frames)
        self.assertIn('video_discontinuity', [d['reason'] for d in graph['diagnostics']])
        self.assertFalse(graph['execution_permitted'])

    def test_interrupted_hash_and_format_guards(self):
        with tempfile.TemporaryDirectory() as t:
            clip = self.make_clip(Path(t))
            p = clip / 'session.json'
            meta = json.loads(p.read_text())
            meta['status'] = 'interrupted'
            p.write_text(json.dumps(meta))
            with self.assertRaises(ValueError):
                load_recording(clip)
            self.assertEqual(len(load_recording(clip, allow_interrupted=True)[1]), 3)
            image = clip / 'frames/00000000.png'
            image.write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'hash_mismatch'):
                load_recording(clip, allow_interrupted=True)

    def test_tamper_and_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            clip = self.make_clip(Path(t))
            p = clip / 'frames.jsonl'
            lines = [json.loads(x) for x in p.read_text().splitlines()]
            lines[0]['image_path'] = '../outside.png'
            p.write_text('\n'.join(json.dumps(x) for x in lines))
            with self.assertRaises(ValueError):
                load_recording(clip)

    def test_lossless_variable_time_video_roundtrip(self):
        with tempfile.TemporaryDirectory() as t:
            clip = self.make_clip(Path(t))
            result = export_ffv1(clip)
            self.assertTrue(Path(result['video']).is_file())
            self.assertEqual(result['verified_pixel_indices'], [0, 1, 2])
            self.assertEqual(result['frame_count'], 3)
            self.assertLessEqual(result['max_pts_error_s'], .0011)


if __name__ == '__main__':
    unittest.main()
