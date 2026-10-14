"""Real HDF5/PNG filtering with synthetic estimators; NOT query acceptance."""
import json
from pathlib import Path

import h5py
import numpy as np
from PIL import Image

from test_evidence_integration import EvidenceWorkspace
from test_localization_flow import FakeBackend
from go1loc.evidence import digest_json, write_json, verify_binding
from go1loc.localization import localize_photo, LocalizationConfig
from go1loc.video import sha256_file


class MaskBackend(FakeBackend):
    def extract(self, config, image_dir, output_dir, **kwargs):
        with h5py.File(kwargs['feature_path'], 'w') as f:
            g = f.create_group(kwargs['image_list'][0])
            g['keypoints'] = np.array([[4+4*i, 16] for i in range(6)]+[[50, 50]], np.float32)
            g['descriptors'] = np.arange(256*7, dtype=np.float32).reshape(256, 7)
            g['scores'] = np.arange(7, dtype=np.float32)
            g['image_size'] = [64, 64]

    def match(self, config, pairs, **kwargs):
        with h5py.File(kwargs['features']) as f:
            name = next(iter(f))
            assert f[name]['keypoints'].shape == (6, 2), 'matcher_received_unfiltered_points'
            assert f[name]['descriptors'].shape == (256, 6), 'descriptor_indices_not_filtered'
            assert f[name]['scores'][:].tolist() == list(range(6))
        return super().match(config, pairs, **kwargs)


class ScreenQueryFlowTests(EvidenceWorkspace):
    def setUp(self):
        super().setUp()
        imports = self.root/'query-import'; (imports/'images').mkdir(parents=True)
        self.screen_photo = imports/'images/frame_00000000.png'
        self.screen_photo.write_bytes(self.photo.read_bytes())
        mask = np.full((64, 64), 255, np.uint8); mask[45:55, 45:55] = 0
        Image.fromarray(mask).save(imports/'mask.png')
        self.screen_manifest = imports/'frames_manifest.json'
        write_json(self.screen_manifest, {'source_kind': 'go1_screen_recording', 'timing_source': 'screen_display',
            'probe': {'source_sha256': 'b'*64}, 'geometry': {'display_mode': 'unknown'},
            'feature_mask': {'path': 'mask.png', 'sha256': sha256_file(imports/'mask.png')},
            'frames': [{'image_path': 'images/frame_00000000.png', 'image_sha256': sha256_file(self.screen_photo),
                        'kept': True, 'captured_at': None, 'frame_index': 0, 'pts': 0, 'pts_time_s': 0}]})
        self.manifest['frames_manifest'] = {'source_kind': 'go1_screen_recording', 'probe': {'source_sha256': 'a'*64}}
        self.manifest['manifest_sha256'] = digest_json({k: v for k, v in self.manifest.items() if k != 'manifest_sha256'})
        write_json(self.map_dir/'map_manifest.json', self.manifest)

    def test_independent_query_filters_before_match_binds_source_and_never_invents_time(self):
        output = self.output(); before = self.snapshot_map()
        with MaskBackend().installed():
            result = localize_photo(self.screen_photo, self.map_dir, output)
        self.assertEqual(result.pose_status, 'localized')  # only a synthetic estimator
        self.assertIsNone(result.capture_time)
        self.assertEqual(result.query_sha256, sha256_file(self.screen_photo))
        self.assertNotEqual(result.query_id, self.screen_photo.name)
        evidence = json.loads((output/'query_evidence.json').read_text(encoding='utf-8'))
        verify_binding(evidence['binding'], self.screen_photo, self.manifest['manifest_sha256'])
        observation = evidence['binding']['geometry']['screen_observation']
        self.assertEqual(observation['source_sha256'], 'b'*64)
        self.assertEqual(observation['timing_source'], 'screen_display')
        self.assertEqual(before, self.snapshot_map())

    def test_missing_query_manifest_and_fake_capture_time_rejected_before_backend(self):
        with self.assertRaisesRegex(ValueError, 'screen_query_frames_manifest_required'):
            localize_photo(self.photo, self.map_dir, self.output())
        with self.assertRaisesRegex(ValueError, 'not_sensor_capture_time'):
            localize_photo(self.screen_photo, self.map_dir, self.output(),
                           LocalizationConfig(capture_time='2026-10-14T00:00:00Z'))

    def test_moved_query_explicit_manifest_keeps_same_origin(self):
        moved = self.root/'renamed.png'; moved.write_bytes(self.screen_photo.read_bytes())
        output = self.output()
        with MaskBackend().installed():
            result = localize_photo(moved, self.map_dir, output,
                                    LocalizationConfig(frames_manifest_path=self.screen_manifest))
        self.assertEqual(result.query_sha256, sha256_file(self.screen_photo))
        evidence = json.loads((output/'query_evidence.json').read_text(encoding='utf-8'))
        self.assertEqual(evidence['binding']['geometry']['screen_observation']['frame']['frame_index'], 0)
