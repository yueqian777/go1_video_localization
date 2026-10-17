import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np

from go1loc.evidence import export_rows, verify_map, verify_binding, digest_json
from go1loc.object_grounding import support_from_roi_matches


class EvidenceTests(unittest.TestCase):
    def fixture(self):
        point = lambda pid: NS(point3D_id=pid, has_point3D=lambda: True)
        model = NS(images={42: NS(name='ref.jpg', points2D=[point(8), point(9)])},
                   points3D={8: NS(xyz=np.array([1., 2., 1.])), 9: NS(xyz=np.array([3., 4., 1.]))})
        log = {'db': [42], 'points3D_ids': [9, 8], 'keypoints_query': [[3.5, 4.5], [1.5, 2.5]],
               'keypoint_index_to_db': ([17, 2], [(9, [0]), (8, [0])])}
        pose = NS(rotation=NS(matrix=lambda: np.eye(3)), translation=np.zeros(3))
        camera = NS(img_from_cam=lambda p: p[:, :2] / p[:, 2:])
        ret = {'inlier_mask': [True, False], 'cam_from_world': pose}
        matches = lambda name: (np.array([[2, 0], [17, 1]]), np.array([.8, .9]))
        return model, log, ret, camera, matches

    def test_explicit_keypoint_and_db_index_join(self):
        rows = export_rows(*self.fixture())
        self.assertEqual(rows[0]['query_keypoint_index'], 17)
        self.assertEqual(rows[0]['references'][0]['image_id'], 42)
        self.assertEqual(rows[0]['references'][0]['reference_keypoint_index'], 1)
        self.assertAlmostEqual(rows[0]['references'][0]['match_score'], .9)
        self.assertEqual(rows[0]['uv'], [3., 4.])
        self.assertTrue(rows[0]['pnp_inlier'])
        self.assertFalse(rows[1]['pnp_inlier'])
        self.assertAlmostEqual(rows[0]['reprojection_px'], np.sqrt(.5))

    def test_misaligned_log_is_rejected(self):
        args = list(self.fixture())
        args[1]['keypoint_index_to_db'][1][0] = (8, [0])
        with self.assertRaisesRegex(ValueError, 'point_id'):
            export_rows(*args)

    def test_missing_mask_does_not_imply_inlier(self):
        args = list(self.fixture())
        args[2].pop('inlier_mask')
        with self.assertRaisesRegex(ValueError, 'mask'):
            export_rows(*args)

    def test_failed_pnp_rows_remain_diagnostic(self):
        args = list(self.fixture()); args[2] = None
        self.assertFalse(any(r['pnp_inlier'] for r in export_rows(*args)))

    def test_query_bound_row_index_reuses_only_lookup_not_pose_or_rows(self):
        from unittest.mock import Mock
        from go1loc.evidence import QueryRowIndex
        model, log, ret, camera, matches = self.fixture()
        reader = Mock(side_effect=matches)
        index = QueryRowIndex(model, reader)
        expected = export_rows(model, log, ret, camera, matches)
        first = export_rows(model, log, ret, camera, reader, row_index=index)
        self.assertEqual(first, expected)
        first[0]['references'][0]['match_score'] = -999
        ret2 = copy.deepcopy(ret); ret2['inlier_mask'] = [False, True]
        for pose in (ret2, None, ret):
            self.assertEqual(export_rows(model, log, pose, camera, reader, row_index=index),
                             export_rows(model, log, pose, camera, matches))
        self.assertEqual(reader.call_count, 1)
        wrong = copy.deepcopy(log); wrong['keypoint_index_to_db'][1][0] = (8, [0])
        with self.assertRaisesRegex(ValueError, 'point_id_alignment'):
            export_rows(model, wrong, ret, camera, reader, row_index=index)
        different = lambda name: (matches(name)[0], np.array([.1, .2]))
        with self.assertRaisesRegex(ValueError, 'row_index_binding'):
            export_rows(model, log, ret, camera, different, row_index=index)
        new_index = QueryRowIndex(model, different)
        self.assertEqual(export_rows(model, log, ret, camera, different, row_index=new_index),
                         export_rows(model, log, ret, camera, different))
        with self.assertRaisesRegex(ValueError, 'row_index_binding'):
            export_rows(copy.copy(model), log, ret, camera, reader, row_index=index)

    def test_row_index_keeps_duplicate_match_and_score_validation(self):
        from go1loc.evidence import QueryRowIndex
        model, log, ret, camera, matches = self.fixture()
        pairs, scores = matches('ref.jpg')
        duplicate = lambda name: (np.concatenate([pairs, pairs[1:]]), np.r_[scores, .4])
        index = QueryRowIndex(model, duplicate)
        self.assertEqual(export_rows(model, log, ret, camera, duplicate, row_index=index),
                         export_rows(model, log, ret, camera, duplicate))
        self.assertEqual(index.lookup(42)[(17, 9)], (1, .4))
        invalid = lambda name: (pairs, scores[:1])
        with self.assertRaisesRegex(ValueError, 'match_score_length'):
            export_rows(model, log, ret, camera, invalid, row_index=QueryRowIndex(model, invalid))

    def test_map_tampering_detected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); sfm = root/'sfm'; sfm.mkdir()
            (sfm/'images.bin').write_bytes(b'model')
            manifest = {'frozen': True, 'model_dir': str(sfm), 'map_id': 'm',
                        'native_model_sha256': {'images.bin': hashlib.sha256(b'model').hexdigest()}}
            manifest['manifest_sha256'] = digest_json(manifest)
            (root/'map_manifest.json').write_text(json.dumps(manifest))
            self.assertEqual(verify_map(root)['map_id'], 'm')
            (sfm/'images.bin').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'hash'):
                verify_map(root)

    def test_stale_query_and_file_binding(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'photo'; p.write_bytes(b'first')
            binding = {'query_sha256': hashlib.sha256(b'first').hexdigest(), 'map_hash': 'm', 'files': {}}
            verify_binding(binding, p, 'm')
            p.write_bytes(b'second')
            with self.assertRaisesRegex(ValueError, 'query'):
                verify_binding(binding, p, 'm')
            with self.assertRaisesRegex(ValueError, 'map'):
                verify_binding(binding, p, 'other')

    def test_zero_clusters_is_insufficient_not_ambiguous(self):
        result = support_from_roi_matches(roi=(0, 0, 100, 100), min_points=3, cluster_radius=.1,
            observations=[{'u': 1, 'v': 1, 'point3D_id': i, 'frame_id': 'f'} for i in range(3)],
            points3d={i: [i*10, 0, 1] for i in range(3)})
        self.assertEqual(result.status, 'insufficient_evidence')

    def test_nonfinite_points_and_invalid_radius(self):
        result = support_from_roi_matches(roi=(0, 0, 10, 10), min_points=1, cluster_radius=0,
                                         observations=[], points3d={})
        self.assertEqual(result.status, 'invalid_input')
        result = support_from_roi_matches(roi=(0, 0, 10, 10), min_points=1,
            observations=[{'u': 1, 'v': 1, 'point3D_id': 1, 'frame_id': 'f'}],
            points3d={1: [float('nan'), 0, 1]})
        self.assertEqual(result.status, 'insufficient_evidence')
