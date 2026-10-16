"""A completed negative result is not a crashed or still-running task."""
import unittest

import test_viser_viewer as f
from go1loc.evidence import digest_json
from go1loc.jobs import atomic_json, read_json


class FailureFeedbackTests(unittest.TestCase):
    setUp = f.ApplicationTests.setUp

    def result(self, *, localized=False, supported=False):
        query, loc, obj = f.ApplicationTests.evidence_files(self)
        loc.update(pose_status='localized' if localized else 'insufficient_evidence',
                   failure_reason=None if localized else 'insufficient_pnp_quality',
                   quality={'num_2d3d': 1568, 'num_inliers': 77, 'inlier_ratio': 77/1568,
                            'query_intrinsics': 'self_estimated_not_calibrated',
                            'query_camera': {'model': 'SIMPLE_RADIAL', 'params': [617., 640, 360, 0]},
                            'acceptance_thresholds': {'min_inliers': 6, 'min_inlier_ratio': .25}})
        atomic_json(query/'localization_result.json', loc)
        evidence = read_json(query/'query_evidence.json')
        evidence['pose_status'] = loc['pose_status']
        evidence['failure_reason'] = loc['failure_reason']
        evidence.pop('evidence_sha256')
        evidence['evidence_sha256'] = digest_json(evidence)
        atomic_json(query/'query_evidence.json', evidence)
        obj['status'] = 'supported' if supported else 'insufficient_evidence'
        obj['failure_reason'] = None if supported else ('insufficient_roi_3d_support' if localized else 'query_not_localized')
        obj['roi_binding']['evidence_sha256'] = evidence['evidence_sha256']
        obj['roi_sha256'] = digest_json(obj['roi_binding'])
        atomic_json(self.root/'object_result.json', obj)
        return query, obj

    def test_completed_failed_pose_shows_reason_and_preserves_roi_without_target(self):
        query, _ = self.result()
        self.app._completed({'kind': 'locate_object', 'result_path': str(self.root/'object_result.json')})
        self.assertIn('照片定位未通过', self.app.roi_status.value)
        self.assertIn('4.91%', self.app.roi_status.value)
        self.assertIn('25.00%', self.app.roi_status.value)
        self.assertIn('照片定位未通过', self.app.notice.value)
        self.assertTrue(self.app.roi_preview.visible)
        self.assertFalse(self.app.roi_submit.disabled)
        self.assertIsNone(self.app.target_result)
        self.assertNotIn('support', self.app.controller.groups)
        self.assertNotIn('query', self.app.controller.groups)
        self.assertEqual(self.app._roi_selection['roi'], [0, 0, 100, 100])

    def test_localized_photo_but_unsupported_object_is_distinct(self):
        self.result(localized=True)
        self.app.load_object(self.root/'object_result.json')
        self.assertIn('照片已定位', self.app.roi_status.value)
        self.assertIn('框内三维证据不足', self.app.roi_status.value)
        self.assertIn('query', self.app.controller.groups)
        self.assertNotIn('support', self.app.controller.groups)
        self.assertIsNone(self.app.target_result)

    def test_negative_result_binding_still_verified(self):
        self.result()
        obj = read_json(self.root/'object_result.json')
        obj['roi_sha256'] = 'tampered'
        atomic_json(self.root/'object_result.json', obj)
        with self.assertRaisesRegex(ValueError, 'object_evidence_binding'):
            self.app.load_object(self.root/'object_result.json')

    def test_failed_query_default_retry_runs_new_localization(self):
        query, _ = self.result()
        self.app.load_object(self.root/'object_result.json')
        self.app._locate_object(None)
        self.assertIsNone(self.app.jobs.submitted[-1][1]['query_dir'])

    def test_success_result_also_requires_explicit_opt_in_for_reuse(self):
        query, obj = self.result(localized=True, supported=True)
        self.app.load_object(self.root/'object_result.json')
        self.assertNotIn('诊断：None', self.app.notice.value)
        self.assertNotIn(str(self.root), self.app.notice.value)
        self.app._locate_object(None)
        self.assertIsNone(self.app.jobs.submitted[-1][1]['query_dir'])
        self.app.reuse_query.value = True
        self.app._locate_object(None)
        self.assertEqual(self.app.jobs.submitted[-1][1]['query_dir'], str(query))

    def test_failed_query_cannot_be_explicitly_reused(self):
        self.result()
        self.app.load_object(self.root/'object_result.json')
        self.app.reuse_query.value = True
        before = len(self.app.jobs.submitted)
        with self.assertRaisesRegex(ValueError, '没有定位成功的证据可复用'):
            self.app._locate_object(None)
        self.assertEqual(len(self.app.jobs.submitted), before)

    def test_contradictory_supported_result_and_wrong_map_are_rejected(self):
        self.result(supported=True)
        with self.assertRaisesRegex(ValueError, 'object_query_not_localized'):
            self.app.load_object(self.root/'object_result.json')
        _, obj = self.result()
        obj['map_hash'] = 'another-map'
        atomic_json(self.root/'object_result.json', obj)
        with self.assertRaisesRegex(ValueError, 'object_map_hash_mismatch'):
            self.app.load_object(self.root/'object_result.json')
        self.assertIsNone(self.app.target_result)
        self.assertNotIn('support', self.app.controller.groups)

    def test_early_failure_without_evidence_is_explained(self):
        atomic_json(self.root/'early.json', {'status':'insufficient_evidence', 'failure_reason':'invalid_query_file_missing'})
        self.app.load_object(self.root/'early.json')
        self.assertIn('计算结束', self.app.roi_status.value)
        self.assertIn('invalid_query_file_missing', self.app.status.value)
        self.assertIsNone(self.app.target_result)

    def test_multiple_object_clusters_are_not_described_as_missing_features(self):
        self.result(localized=True)
        path = self.root/'object_result.json'
        obj = read_json(path)
        obj.update(status='ambiguous', failure_reason='multiple_spatial_support_clusters', candidates=[{}, {}])
        atomic_json(path, obj)
        self.app.load_object(path)
        self.assertIn('2 组', self.app.roi_status.value)
        self.assertIn('多个三维候选', self.app.notice.value)
        self.assertIsNone(self.app.target_result)

    def test_query_notice_keeps_numeric_reason_even_with_raw_error_code(self):
        query, _ = self.result()
        self.app.load_query(query)
        self.assertIn('4.91%', self.app.notice.value)
        self.assertIn('25.00%', self.app.notice.value)
        self.assertNotIn('选框已保留', self.app.notice.value)


if __name__ == '__main__':
    unittest.main()
