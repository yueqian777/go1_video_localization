import unittest
import numpy as np
from go1loc.photo_object import ground_evidence


def evidence(scale=1):
    return {'pose_status': 'localized', 'selected_cluster': 0,
            'clusters': [{'cluster_index': 0, 'rows': [
                {'uv_original': [i+1, 2], 'point3D_id': i, 'xyz': [i*.1*scale, 0, scale],
                 'pnp_inlier': True, 'positive_depth': True, 'reprojection_px': .2,
                 'references': [{'image_name': 'ref.jpg'}]} for i in range(5)]}]}


class PhotoObjectTests(unittest.TestCase):
    def test_real_chain_input_filters_outliers(self):
        e = evidence(); e['clusters'][0]['rows'][0]['pnp_inlier'] = False
        result = ground_evidence(e, (0, 0, 10, 10))
        self.assertEqual(result['status'], 'supported')
        self.assertNotIn(0, result['supporting_point3D_ids'])
        self.assertEqual(result['supporting_frame_ids'], ['ref.jpg'])

    def test_scale_equivariance(self):
        a = ground_evidence(evidence(), (0, 0, 10, 10))
        b = ground_evidence(evidence(100), (0, 0, 10, 10))
        self.assertEqual(a['supporting_point3D_ids'], b['supporting_point3D_ids'])
        self.assertEqual(set(a['supporting_points3D']), set(b['supporting_points3D']))
        np.testing.assert_allclose(
            np.asarray(list(a['supporting_points3D'].values())) * 100,
            np.asarray(list(b['supporting_points3D'].values())),
        )
        np.testing.assert_allclose(np.array(a['object_surface_position'])*100, b['object_surface_position'])

    def test_failed_localization_rejected(self):
        e=evidence(); e['pose_status']='insufficient_evidence'
        self.assertEqual(ground_evidence(e, (0,0,10,10))['failure_reason'], 'query_not_localized')

    def test_mask_excludes_background(self):
        mask=np.zeros((10,10), bool); mask[2,1:4]=True
        result=ground_evidence(evidence(), (0,0,10,10), mask=mask)
        self.assertEqual(result['supporting_point3D_ids'], [0,1,2])

    def test_empty_roi_and_bad_radius(self):
        self.assertEqual(ground_evidence(evidence(), (8,8,9,9))['status'], 'insufficient_evidence')
        self.assertEqual(ground_evidence(evidence(), (0,0,10,10), cluster_radius=-1)['status'], 'invalid_input')
