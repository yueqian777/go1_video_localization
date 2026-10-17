"""Query-conditioned reference recovery, without GPU or robot connections."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_localization_flow as flow
from test_localization_flow import FakeBackend
from test_evidence_integration import EvidenceWorkspace
from go1loc.evidence import verify_binding
from go1loc.localization import LocalizationConfig, _consistent_hypotheses


class PollutedBackend(FakeBackend):
    """Three references share a component, but only two explain this query.

    All correspondences (including mismatches) must survive into evidence;
    reducing the denominator to RANSAC's inliers would hide this regression.
    """
    def __init__(self, *, second_valid=True, ambiguous=False, joint_polluted=False):
        super().__init__()
        self.second_valid = second_valid
        self.ambiguous = ambiguous
        self.joint_polluted = joint_polluted
        self.clusters = [[42, 7, 99] + ([100, 101] if ambiguous else [])]
        for image_id in self.clusters[0]:
            self.images[image_id] = NS(image_id=image_id, name=f'ref-{image_id}.png',
                points2D=[NS(point3D_id=100+i, has_point3D=lambda: True) for i in range(30)])
        for i in range(30):
            self.xyz[100+i] = NS(xyz=np.array([.4+(i%6)*.1, .4+(i//6)*.1, 2.+i*.03]))

    def get_matches(self, path, query, name):
        return np.array([[i,i] for i in range(30)]), np.ones(30)*.8

    def pose_from_cluster(self, localizer, query, camera, cluster, features, matches):
        self.cluster_cameras.append(camera)
        polluted = len(cluster)>2 or 99 in cluster or (self.joint_polluted and len(cluster)==2)
        n = 30 if polluted else 8
        count = 6 if polluted else 8
        if cluster == [99] or (7 in cluster and not self.second_valid): count = 0
        shifted = all(i in (100,101) for i in cluster)
        translation = np.array([1.,0,0]) if shifted else np.zeros(3)
        xyz = np.array([self.xyz[100+i].xyz for i in range(n)])
        uv = camera.img_from_cam(xyz+translation)
        log = dict(db=cluster,points3D_ids=list(range(100,100+n)),keypoints_query=uv,
                   num_matches=n*len(cluster),
                   keypoint_index_to_db=(list(range(n)),[(100+i,list(range(len(cluster)))) for i in range(n)]))
        ret = dict(cam_from_world=NS(rotation=NS(matrix=lambda:np.eye(3)),translation=translation),
                   num_inliers=count,inlier_mask=[i<count for i in range(n)])
        return ret,log


class ReferenceConsensusTests(EvidenceWorkspace):
    localize = flow.LocalizationFlowTests.localize

    def test_polluted_connected_component_recovers_with_two_consistent_references(self):
        before=self.snapshot_map()
        result,out,_=self.localize(PollutedBackend())
        self.assertEqual(result.pose_status,'localized')
        self.assertEqual(result.quality['reference_strategy'],'query_reference_consensus')
        self.assertEqual(result.quality['acceptance_thresholds']['min_inlier_ratio'],.25)
        evidence=json.loads((out/'query_evidence.json').read_text(encoding='utf-8'))
        selected=evidence['clusters'][evidence['selected_cluster']]
        self.assertEqual(set(selected['reference_image_ids']),{42,7})
        self.assertEqual(len(evidence['clusters'][0]['rows']),30)
        self.assertEqual(len(selected['rows']),8)
        self.assertEqual(result.quality['num_2d3d'],8)
        verify_binding(evidence['binding'],self.photo,self.manifest['manifest_sha256'])
        self.assertEqual(before,self.snapshot_map())

    def test_single_reference_candidate_cannot_rescue_a_failed_component(self):
        result,_,_=self.localize(PollutedBackend(second_valid=False))
        self.assertNotEqual(result.pose_status,'localized')

    def test_competing_reference_pairs_are_ambiguous_not_first_inserted(self):
        result,_,_=self.localize(PollutedBackend(ambiguous=True))
        self.assertEqual(result.pose_status,'ambiguous')
        self.assertEqual(result.failure_reason,'ambiguous_reference_consensus')

    def test_valid_normal_solution_does_not_trigger_extra_estimators(self):
        result,_,backend=self.localize(FakeBackend())
        self.assertEqual(result.pose_status,'localized')
        self.assertEqual(len(backend.cluster_cameras),1)

    def test_joint_quality_still_counts_outliers_not_just_agreeing_inliers(self):
        result,_,_=self.localize(PollutedBackend(joint_polluted=True))
        self.assertNotEqual(result.pose_status,'localized')
        self.assertEqual(result.failure_reason,'insufficient_pnp_quality')

    def test_recovery_budget_cannot_accept_one_reference(self):
        result,_,_=self.localize(PollutedBackend(),config=LocalizationConfig(top_k=1))
        self.assertNotEqual(result.pose_status,'localized')

    def test_disconnected_components_are_not_deduplicated_by_similar_xyz(self):
        backend=PollutedBackend(ambiguous=True)
        backend.images[102]=deepcopy(backend.images[99])
        backend.images[102].image_id=102
        backend.images[102].name='ref-102.png'
        backend.clusters=[[42,7,99],[100,101,102]]
        original=backend.pose_from_cluster
        def same_coordinate_frames(*args):
            ret,log=original(*args)
            if ret:
                ret['cam_from_world'].translation[:]=0
                xyz=np.array([backend.xyz[i].xyz for i in log['points3D_ids']])
                log['keypoints_query']=args[2].img_from_cam(xyz)
            return ret,log
        backend.sfm.pose_from_cluster=same_coordinate_frames
        result,_,_=self.localize(backend)
        self.assertEqual(result.pose_status,'ambiguous')


class HypothesisAgreementTests(unittest.TestCase):
    def candidate(self):
        camera=flow.FakeCamera(params=[100,100,32,32])
        xyz=np.array([[.1*(i%3),.1*(i//3),2.+i*.1] for i in range(9)])
        uv=camera.img_from_cam(xyz)
        return dict(camera=camera,
            ret=dict(cam_from_world=NS(rotation=NS(matrix=lambda:np.eye(3)),translation=np.zeros(3))),
            rows=[dict(query_keypoint_index=i,xyz=p.tolist(),uv_colmap=u.tolist(),
                       pnp_inlier=True,positive_depth=True,reprojection_px=0.) for i,(p,u) in enumerate(zip(xyz,uv))])

    def test_identical_evidence_agrees(self):
        a=self.candidate()
        self.assertTrue(_consistent_hypotheses(a,deepcopy(a),LocalizationConfig()))

    def test_planar_focal_distance_degeneracy_not_accepted_as_same_pose(self):
        a=self.candidate();b=deepcopy(a)
        b['camera'].params[:2]*=4
        b['ret']['cam_from_world'].translation[2]=6
        self.assertFalse(_consistent_hypotheses(a,b,LocalizationConfig()))

    def test_translation_and_rotation_must_agree_even_with_claimed_inliers(self):
        for change in ('translation','rotation','nan'):
            with self.subTest(change=change):
                a=self.candidate();b=deepcopy(a)
                if change=='translation':b['ret']['cam_from_world'].translation[0]=1
                elif change=='nan':b['ret']['cam_from_world'].translation[0]=float('nan')
                else:b['ret']['cam_from_world'].rotation.matrix=lambda:np.diag([-1,-1,1])
                self.assertFalse(_consistent_hypotheses(a,b,LocalizationConfig()))

    def test_reciprocal_pixels_and_unique_query_features_required(self):
        for change in ('pixels','duplicate','negative_depth','bad_reprojection','missing_reprojection'):
            with self.subTest(change=change):
                a=self.candidate();b=deepcopy(a)
                for row in b['rows']:
                    if change=='pixels':row['uv_colmap'][0]+=30
                    elif change=='duplicate':row['query_keypoint_index']=0
                    elif change=='negative_depth':row['xyz'][2]=-2
                    elif change=='missing_reprojection':row['reprojection_px']=None
                    else:row['reprojection_px']=10
                self.assertFalse(_consistent_hypotheses(a,b,LocalizationConfig()))


if __name__=='__main__': unittest.main()
