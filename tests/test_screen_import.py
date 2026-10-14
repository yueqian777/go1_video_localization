"""Synthetic unit fixtures, NOT real video route acceptance."""
import json
import tempfile
import unittest
import shutil
import subprocess
from unittest.mock import patch
from pathlib import Path

import cv2
import h5py
import numpy as np

from go1loc.screen_import import ScreenConfig, effective_mask, screen_quality, crop_image, screen_geometry
from go1loc.feature_masks import filter_features, screen_frame_binding, query_screen_binding
from go1loc.video import FrameConfig, VideoPipelineError, sha256_file, prepare_video


class ScreenImportTests(unittest.TestCase):
    def config(self, **kw):
        return ScreenConfig(source_size_wh=[100, 80], crop_xywh=[10, 10, 80, 60],
                            selected_window='single front view', **kw)

    def test_crop_bounds_and_unknown_projection(self):
        c = self.config()
        c.validate(100, 80)
        self.assertEqual(crop_image(np.zeros((80, 100, 3), np.uint8), c).shape, (60, 80, 3))
        g = screen_geometry(c)
        self.assertEqual(g['display_mode'], 'unknown')
        self.assertIsNone(g['camera_id'])
        self.assertNotIn('output_K', g)
        for size in [(99, 80), (100, 81)]:
            with self.assertRaises(VideoPipelineError):
                c.validate(*size)
        with self.assertRaises(VideoPipelineError):
            self.config(output_size_wh=[0, 20]).validate(100, 80)

    def test_resize_pixel_center_mapping_and_margin(self):
        c = self.config(output_size_wh=[40, 30], exclude_rectangles=[[40, 10, 2, 60]], margin_px=4)
        c.validate(100, 80)
        T = np.asarray(screen_geometry(c)['output_to_source_pixel_centers'])
        np.testing.assert_allclose(T @ [0, 0, 1], [10.5, 10.5, 1])
        mask = effective_mask(c)
        self.assertEqual(mask.shape, (30, 40))
        self.assertEqual(mask[15, 15], 0)
        self.assertEqual(mask[15, 14], 0)  # exclusion expanded before sampling
        self.assertEqual(mask[15, 7], 255)

    def test_ui_animation_ignored_static_scene_not_rejected(self):
        rng = np.random.default_rng(42)
        a = rng.integers(20, 220, (60, 80, 3), dtype=np.uint8)
        b = a.copy(); b[:, 30:35] = 255
        mask = np.full((60, 80), 255, np.uint8); mask[:, 25:40] = 0
        q = screen_quality(b, mask, FrameConfig(), a)
        self.assertEqual(q['scene_mean_change'], 0)
        self.assertTrue(q['similar_scene'])
        self.assertEqual(q['rejection_reasons'], [])
        self.assertFalse(q['discontinuity_suspected'])
        self.assertEqual(screen_quality(a, np.zeros_like(mask), FrameConfig())['rejection_reasons'], ['insufficient_valid_area'])

    def test_feature_filter_synchronizes_arrays_and_keeps_attrs(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'features.h5'
            points = np.array([[2, 2], [5, 5], [8, 8], [11, 1]], np.float32)
            with h5py.File(path, 'w') as f:
                g = f.create_group('image.png')
                g.create_dataset('keypoints', data=points).attrs['uncertainty'] = 1.5
                g.create_dataset('descriptors', data=np.arange(12).reshape(3, 4))
                g.create_dataset('scores', data=[.1, .2, .3, .4])
                g.create_dataset('image_size', data=[10, 10])
            mask = np.full((10, 10), 255, np.uint8); mask[4:7, 4:7] = 0
            report = filter_features(path, {'image.png': mask})
            self.assertEqual(report['image.png']['retained_original_indices'], [0, 2])
            with h5py.File(path) as f:
                np.testing.assert_array_equal(f['image.png/descriptors'][:], np.arange(12).reshape(3, 4)[:, [0, 2]])
                np.testing.assert_allclose(f['image.png/scores'][:], [.1, .3])
                self.assertEqual(f['image.png/keypoints'].attrs['uncertainty'], 1.5)
                self.assertEqual(f['image.png/image_size'][:].tolist(), [10, 10])
            with self.assertRaisesRegex(ValueError, 'already_filtered'):
                filter_features(path, {'image.png': mask})

    def test_derived_frame_hash_and_mask_binding(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root/'images').mkdir()
            p = root/'images/a.png'; m = root/'feature_mask.png'
            cv2.imencode('.png', np.zeros((10, 10, 3), np.uint8))[1].tofile(p)
            cv2.imencode('.png', np.full((10, 10), 255, np.uint8))[1].tofile(m)
            payload = {'source_kind': 'go1_screen_recording', 'timing_source': 'screen_display',
                       'probe': {'source_sha256': 'a'*64}, 'geometry': {'display_mode': 'unknown'},
                       'feature_mask': {'path': m.name, 'sha256': sha256_file(m)},
                       'frames': [{'kept': True, 'image_path': 'images/a.png', 'image_sha256': sha256_file(p),
                                   'frame_index': 30, 'pts': 1000, 'pts_time_s': 1.0}]}
            mf = root/'frames_manifest.json'; mf.write_text(json.dumps(payload))
            binding = screen_frame_binding(p)
            self.assertEqual(binding['frame']['frame_index'], 30)
            self.assertEqual(binding['source_kind'], 'go1_screen_recording')
            with self.assertRaisesRegex(ValueError, 'independent_recording'):
                query_screen_binding(p, {'frames_manifest': payload}, None, None, None)
            with self.assertRaisesRegex(ValueError, 'not_sensor_capture_time'):
                query_screen_binding(p, {}, None, None, '2026-10-14T00:00:00Z')
            with self.assertRaisesRegex(ValueError, 'rectification_not_supported'):
                query_screen_binding(p, {}, None, Path('not_actual_calibration'), None)
            self.assertIsNotNone(query_screen_binding(p, {}, None, None, None))
            p.write_bytes(b'wrong photo')
            with self.assertRaisesRegex(ValueError, 'frame_hash'):
                screen_frame_binding(p)
            cv2.imencode('.png', np.zeros((10, 10, 3), np.uint8))[1].tofile(p)
            m.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'mask_hash'):
                screen_frame_binding(p)

    def test_bad_aligned_array_is_atomic_and_never_reaches_matcher(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'features.h5'
            with h5py.File(path, 'w') as f:
                g = f.create_group('x.png')
                g['keypoints'] = [[2., 2.], [3., 3.]]
                g['scores'] = [.1]  # malformed length
                g['descriptors'] = np.zeros((256, 2))
                g['image_size'] = [10, 10]
            before = sha256_file(path)
            with self.assertRaisesRegex(ValueError, 'inconsistent_feature_array'):
                filter_features(path, {'x.png': np.ones((10, 10), np.uint8)*255})
            self.assertEqual(sha256_file(path), before)
            self.assertFalse(path.with_name(path.name+'.masking.partial').exists())

    def test_scene_jump_not_masked_animation_sets_review_barrier(self):
        a = np.full((80, 100, 3), 30, np.uint8)
        b = np.full_like(a, 190)
        q = screen_quality(b, np.full((80, 100), 255, np.uint8), FrameConfig(quality_filter=False), a)
        self.assertTrue(q['discontinuity_suspected'])
        self.assertIn('possible_scene_jump_or_layout_change', q['warnings'])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg required')
    def test_real_decoder_vfr_source_indices_png_hashes_and_no_route_edges(self):
        # This generated fixture tests decoder bookkeeping, NOT real Go1 data.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); video = root/'synthetic_decoder_fixture.mp4'
            subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
                'testsrc2=size=100x80:rate=5:duration=1', '-vf', "select='not(eq(n,2))'",
                '-fps_mode', 'vfr', '-c:v', 'libx264', str(video)], check=True, capture_output=True)
            before = sha256_file(video)
            cfg = self.config(output_size_wh=[40, 30], exclude_rectangles=[[40, 10, 2, 60]])
            out = root/'out'
            result = prepare_video(video, out, FrameConfig(interval_s=.19, duration_s=1, quality_filter=False), screen_config=cfg)
            self.assertEqual(sha256_file(video), before)
            self.assertEqual(result['timing_source'], 'screen_display')
            self.assertFalse(result['hardware_synchronized'])
            self.assertFalse(result['execution_permitted'])
            self.assertEqual(result['route_segments'], [])
            rows = result['frames']
            self.assertEqual([r['frame_index'] for r in rows], [0, 1, 2, 3])
            np.testing.assert_allclose([r['pts_time_s'] for r in rows], [0, .2, .6, .8])
            self.assertEqual(result['source_kind'], 'go1_screen_recording')
            for row in rows:
                self.assertIsNone(row['captured_at'])
                self.assertEqual(sha256_file(out/row['image_path']), row['image_sha256'])
                self.assertEqual(sha256_file(out/row['raw_path']), row['raw_sha256'])
                self.assertEqual(screen_frame_binding(out/row['image_path'])['source_sha256'], before)
            timeline = json.loads((out/'source_timeline.json').read_text())
            self.assertEqual(len(timeline['frames']), 4)
            # An actual decoded-but-not-selected jump still blocks crossing it.
            def jump_at_second(image, mask, config, previous=None):
                jump_at_second.count += 1
                return {'similar_scene': False, 'discontinuity_suspected': jump_at_second.count == 2,
                        'warnings': ['jump'] if jump_at_second.count == 2 else [], 'rejection_reasons': []}
            jump_at_second.count = 0
            with patch('go1loc.screen_import.screen_quality', side_effect=jump_at_second):
                interrupted = prepare_video(video, root/'jump', FrameConfig(interval_s=.5, duration_s=1), screen_config=cfg)
            self.assertTrue(interrupted['frames'][1]['discontinuity_before'])
            self.assertEqual(interrupted['frames'][1]['interval_review'][0]['frame_index'], 1)
            with self.assertRaisesRegex(VideoPipelineError, 'not_empty'):
                prepare_video(video, out, screen_config=cfg)

    def test_screen_route_source_requires_explicit_review(self):
        import test_planning as fixtures
        from go1loc.route_review import generate_route_candidates
        manifest, poses, frames = fixtures.fixture()
        frames['source_kind'] = 'go1_screen_recording'
        frames.pop('route_segments', None)
        review = generate_route_candidates(manifest, poses, frames)
        self.assertEqual(review['source_kind'], 'go1_screen_recording')
        self.assertEqual(review['route_segments'], [])
        self.assertTrue(all(c['verified_continuous'] is False for c in review['candidates']))

    def test_screen_route_cannot_be_relabeled_raw_video(self):
        import test_planning as fixtures
        from go1loc.planning import build_route_graph
        manifest, poses, frames = fixtures.fixture()
        frames['source_kind'] = 'go1_screen_recording'
        for segment in frames['route_segments']:
            segment['source_kind'] = 'video'
        graph = build_route_graph(manifest, poses, frames)
        self.assertEqual(graph['edges'], {})
        self.assertTrue(any(d['reason'] == 'segment_source_kind_mismatch' for d in graph['diagnostics']))

    def test_native_snapshot_retains_screen_display_provenance(self):
        from test_snapshots import fixture_model
        from go1loc.snapshots import SnapshotPublisher
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); model = fixture_model(root/'native')
            pub = SnapshotPublisher(root/'published', model_loader=lambda _: model,
                timing_source='screen_display', source_kind='go1_screen_recording')
            row = pub.publish(root/'native', 'component')
            self.assertEqual(row['timing_source'], 'screen_display')
            self.assertEqual(row['source_kind'], 'go1_screen_recording')
            self.assertIsNone(row['exposure_time'])


if __name__ == '__main__':
    unittest.main()
