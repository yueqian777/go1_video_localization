"""Normal-cluster eligibility regression, with synthetic PnP and real evidence I/O.

These fixtures test selection, not image localization or field navigation accuracy.
Outliers remain in the correspondence denominator and in the exported evidence.
"""
from __future__ import annotations

import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import numpy as np

from test_evidence_integration import EvidenceWorkspace
import test_localization_flow as flow
from test_localization_flow import FakeBackend
from test_localization_reference_consensus import PollutedBackend
from go1loc.evidence import digest_json, verify_binding
from go1loc.localization import _reference_consensus_candidates


class CandidateBackend(FakeBackend):
    """Independent reference components: (correspondence count, inlier count).

    Each component has its own map points and a distinct camera center; query
    keypoint coordinates are shared. Non-inliers have actual projection errors.
    """
    def __init__(self, specs):
        super().__init__(clusters=[[image_id] for image_id in specs])
        self.specs = specs
        self.images.clear()
        self.xyz.clear()
        self.centers = {image_id: float(i) for i, image_id in enumerate(specs)}
        for image_id, (total, count) in specs.items():
            pids = [image_id * 1000 + q for q in range(total)]
            self.images[image_id] = NS(image_id=image_id, name=f'ref-{image_id}.png',
                points2D=[NS(point3D_id=pid, has_point3D=lambda: True) for pid in pids])
            for q, pid in enumerate(pids):
                uv = self.pixel(q)
                depth = 1 + .01 * q
                xyz = np.array([uv[0] * depth / 10 + self.centers[image_id],
                                uv[1] * depth / 10, depth])
                if count is None or q >= count:
                    xyz[0] += depth  # 10px mismatch, beyond the unchanged 4px gate.
                self.xyz[pid] = NS(xyz=xyz)

    @staticmethod
    def pixel(q):
        return np.array([4.5 + 3 * (q % 20), 4.5 + 3 * (q // 20)])

    def get_matches(self, path, query, name):
        image = self.model.find_image_with_name(name)
        n = len(image.points2D)
        return np.array([[q, q] for q in range(n)]), np.full(n, .9)

    def pose_from_cluster(self, localizer, query, camera, cluster, features, matches):
        self.cluster_cameras.append(camera)
        assert len(cluster) == 1
        image_id = cluster[0]
        total, count = self.specs[image_id]
        pids = [p.point3D_id for p in self.images[image_id].points2D]
        log = dict(db=cluster, points3D_ids=pids,
                   keypoints_query=np.array([self.pixel(q) for q in range(total)]),
                   num_matches=total,
                   keypoint_index_to_db=(list(range(total)), [(pid, [0]) for pid in pids]))
        if count is None:
            return None, log
        pose = NS(rotation=NS(matrix=lambda: np.eye(3)),
                  translation=np.array([-self.centers[image_id], 0, 0]))
        return dict(cam_from_world=pose, num_inliers=count,
                    inlier_mask=[q < count for q in range(total)]), log


class CandidateSelectionTests(EvidenceWorkspace):
    localize = flow.LocalizationFlowTests.localize

    def run_candidates(self, specs):
        backend = CandidateBackend(specs)
        before = self.snapshot_map()
        with patch('go1loc.localization._reference_consensus_candidates',
                   wraps=_reference_consensus_candidates) as recovery:
            result, out, _ = self.localize(backend)
        evidence = json.loads((out / 'query_evidence.json').read_text(encoding='utf-8'))
        verify_binding(evidence['binding'], self.photo, self.manifest['manifest_sha256'])
        self.assertEqual(evidence['localization_result_sha256'], digest_json(result.to_dict()))
        self.assertEqual(evidence['evidence_sha256'],
                         digest_json({k: v for k, v in evidence.items() if k != 'evidence_sha256'}))
        # Even rejected or unsolved candidates retain their original rows and indices.
        self.assertEqual(len(evidence['clusters']), len(specs))
        for index, (image_id, (total, count)) in enumerate(specs.items()):
            cluster = evidence['clusters'][index]
            self.assertEqual(cluster['cluster_index'], index)
            self.assertEqual(cluster['reference_image_ids'], [image_id])
            self.assertEqual(len(cluster['rows']), total)
            self.assertEqual(sum(r['pnp_inlier'] for r in cluster['rows']), count or 0)
        self.assertEqual(before, self.snapshot_map())
        return result, evidence, recovery

    def assert_selected(self, result, evidence, image_id, *, qualified=True):
        cluster = evidence['clusters'][evidence['selected_cluster']]
        quality = result.quality.get('best', result.quality)
        self.assertEqual(cluster['cluster_index'], quality['cluster_index'])
        self.assertEqual(cluster['reference_image_ids'], [image_id])
        self.assertEqual(quality['num_2d3d'], len(cluster['rows']))
        count = sum(r['pnp_inlier'] for r in cluster['rows'])
        self.assertEqual(quality['num_inliers'], count)
        self.assertEqual(quality['inlier_ratio'], count / len(cluster['rows']))
        self.assertEqual(quality['query_camera'], cluster['camera'])
        self.assertEqual(quality['acceptance_thresholds']['min_inlier_ratio'], .25)
        if qualified:
            self.assertGreaterEqual(quality['inlier_ratio'], .25)
            self.assertTrue(all(r['positive_depth'] and r['reprojection_px'] < 4
                                for r in cluster['rows'] if r['pnp_inlier']))
        if result.pose_status == 'localized':
            self.assertEqual(result.T_WC_query, cluster['T_WC_candidate'])
        else:
            self.assertIsNone(result.T_WC_query)

    def test_invalid_highest_count_does_not_hide_qualified_runner_up(self):
        for specs in ({42: (200, 40), 7: (60, 30)}, {7: (60, 30), 42: (200, 40)}):
            with self.subTest(order=list(specs)):
                result, evidence, recovery = self.run_candidates(specs)
                recovery.assert_not_called()
                self.assertEqual(result.pose_status, 'localized')
                self.assert_selected(result, evidence, 7)

    def test_all_invalid_invoke_existing_recovery_and_retain_failure_evidence(self):
        result, evidence, recovery = self.run_candidates({42: (200, 40), 7: (60, 10)})
        recovery.assert_called_once()
        self.assertEqual(result.failure_reason, 'insufficient_pnp_quality')
        self.assert_selected(result, evidence, 42, qualified=False)
        # Also exercise the real, existing two-reference recovery (not a stub).
        with patch('go1loc.localization._reference_consensus_candidates',
                   wraps=_reference_consensus_candidates) as recovery:
            result, out, _ = self.localize(PollutedBackend())
        recovery.assert_called_once()
        self.assertEqual(result.pose_status, 'localized')
        self.assertEqual(result.quality['reference_strategy'], 'query_reference_consensus')
        evidence = json.loads((out / 'query_evidence.json').read_text(encoding='utf-8'))
        self.assertEqual(len(evidence['clusters'][0]['rows']), 30)
        self.assertEqual(evidence['selected_cluster'], result.quality['cluster_index'])
        self.assertEqual(evidence['clusters'][evidence['selected_cluster']]['T_WC_candidate'], result.T_WC_query)

    def test_qualified_strong_competitors_still_rejected_in_either_order(self):
        for specs in ({42: (60, 30), 7: (60, 28)}, {7: (60, 28), 42: (60, 30)}):
            with self.subTest(order=list(specs)):
                result, evidence, recovery = self.run_candidates(specs)
                recovery.assert_not_called()
                self.assertEqual(result.pose_status, 'ambiguous')
                self.assertEqual(result.failure_reason, 'ambiguous_multiple_covisibility_clusters')
                self.assert_selected(result, evidence, 42)
                self.assertEqual(result.quality['alternatives'][0]['num_inliers'], 28)

    def test_normal_single_qualified_candidate_is_unchanged(self):
        result, evidence, recovery = self.run_candidates({42: (60, 30)})
        recovery.assert_not_called()
        self.assertEqual(result.pose_status, 'localized')
        self.assert_selected(result, evidence, 42)

    def test_invalid_near_count_is_not_a_qualified_ambiguity_competitor(self):
        result, evidence, recovery = self.run_candidates({42: (200, 29), 7: (60, 30)})
        recovery.assert_not_called()
        self.assertEqual(result.pose_status, 'localized')
        self.assert_selected(result, evidence, 7)

    def test_unsolved_candidate_does_not_renumber_selected_cluster(self):
        result, evidence, recovery = self.run_candidates({42: (200, None), 7: (60, 30)})
        recovery.assert_not_called()
        self.assertEqual(result.pose_status, 'localized')
        self.assertEqual(evidence['selected_cluster'], 1)
        self.assert_selected(result, evidence, 7)


if __name__ == '__main__':
    unittest.main()
