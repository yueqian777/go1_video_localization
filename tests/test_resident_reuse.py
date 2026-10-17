"""Exact same-observation reuse, not skipping candidates or weakening gates."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
try:
    import h5py
    import torch
    import hloc.localize_sfm
    VISION_AVAILABLE=True
except ImportError:
    VISION_AVAILABLE=False

from go1loc.evidence import digest_json, write_json
from go1loc.jobs import read_json
from go1loc.navigation_localizer import ResidentLocalizer


@unittest.skipUnless(VISION_AVAILABLE,'requires the optional pinned hloc/vision environment')
class ReuseTests(unittest.TestCase):
    def test_frozen_cluster_cache_preserves_order_and_returns_independent_lists(self):
        b=self.backend();b.reconstruction=object()
        with patch('hloc.localize_sfm.do_covisibility_clustering',side_effect=lambda ids,model:[ids]) as compute:
            a=b.covisibility_clusters([3,1]);a[0].append(9)
            self.assertEqual(b.covisibility_clusters([3,1]),[[3,1]])
            self.assertEqual(compute.call_count,1)
            self.assertEqual(b.covisibility_clusters([1,3]),[[1,3]])
            self.assertEqual(compute.call_count,2)

    def test_identical_pnp_input_still_invokes_estimator_without_sharing_refined_camera(self):
        from go1loc.localization import _CachedClusterSolver
        from test_localization_flow import FakeCamera
        point=lambda n:SimpleNamespace(point3D_id=n,has_point3D=lambda:True)
        model=SimpleNamespace(images={1:SimpleNamespace(name='a',num_points3D=2,points2D=[point(10),point(20)])})
        calls=[]
        def estimate(points,idxs,pids,camera):
            calls.append((idxs,pids));camera.params[0]=15
            return dict(num_inliers=2,inlier_mask=np.array([True,True]))
        localizer=SimpleNamespace(reconstruction=model,localize=estimate,config={'estimation':{'ransac':{'max_error':4}}})
        reader=lambda name:(np.array([[0,0],[1,1]]),np.ones(2))
        with patch('hloc.utils.io.get_keypoints',return_value=np.array([[1.,2.],[3.,4.]])):
            a=_CachedClusterSolver(localizer,'q',None,reader)
            b=_CachedClusterSolver(localizer,'q',None,reader)
        ca,cb=FakeCamera(),FakeCamera();ra,_=a(ca,[1]);rb,_=b(cb,[1])
        self.assertEqual(len(calls),2);self.assertEqual(cb.params[0],15)
        ra['inlier_mask'][0]=False;ca.params[0]=99
        self.assertTrue(rb['inlier_mask'][0]);self.assertEqual(cb.params[0],15)
        cc=FakeCamera();cc.params[0]=11;b(cc,[1]);self.assertEqual(len(calls),3)
        localizer.config={'estimation':{'ransac':{'max_error':3}}}
        b(FakeCamera(),[1]);self.assertEqual(len(calls),4)
    def test_cached_cluster_inputs_match_pinned_hloc_including_ambiguous_indices(self):
        from hloc.localize_sfm import pose_from_cluster
        from go1loc.localization import _CachedClusterSolver
        from unittest.mock import Mock
        point=lambda n:SimpleNamespace(point3D_id=n,has_point3D=lambda:n!=-1)
        images={1:SimpleNamespace(name='a',num_points3D=2,points2D=[point(10),point(20),point(-1)]),
                2:SimpleNamespace(name='b',num_points3D=2,points2D=[point(20),point(30)]),
                3:SimpleNamespace(name='empty',num_points3D=0)}
        model=SimpleNamespace(images=images)
        pairs={'a':(np.array([[0,0],[1,1],[2,2]]),np.ones(3)),
               'b':(np.array([[0,0],[1,0],[2,1]]),np.ones(3))}
        kp=np.array([[1.,2.],[3.,4.],[5.,6.]])
        captured=[]
        def solve(points,idxs,pids,camera):
            captured.append((points.copy(),idxs,pids));return {'num_inliers':3}
        localizer=SimpleNamespace(reconstruction=model,localize=solve)
        reader=Mock(side_effect=lambda name:pairs[name]);camera=object()
        with patch('hloc.utils.io.get_keypoints',return_value=kp.copy()) as read_kp:
            cached=_CachedClusterSolver(localizer,'q',Path('features'),reader)
            for ids in ([1,2,3],[2,1],[1],[1,2]):
                _,actual=cached(camera,list(ids))
                with patch('hloc.localize_sfm.get_keypoints',return_value=kp.copy()),patch(
                        'hloc.localize_sfm.get_matches',side_effect=lambda path,q,n:pairs[n]):
                    _,expected=pose_from_cluster(localizer,'q',camera,list(ids),None,None)
                for k in ('db','points3D_ids','num_matches','keypoint_index_to_db'):
                    self.assertEqual(actual[k],expected[k])
                np.testing.assert_equal(actual['keypoints_query'],expected['keypoints_query'])
                np.testing.assert_equal(captured[-2][0],captured[-1][0])
                self.assertEqual(captured[-2][1:],captured[-1][1:])
            self.assertEqual(read_kp.call_count,1)
            self.assertEqual(reader.call_count,2)

    def backend(self):
        b=object.__new__(ResidentLocalizer)
        b.device='cpu'; b.last_references=[]; b.reference_features={}
        b.calls=[]
        def model(data):
            b.calls.append(data)
            if 'image' in data:
                return dict(keypoints=torch.tensor([[[1.,2.],[3.,4.]]]),
                            descriptors=torch.ones(1,4,2),scores=torch.ones(1,2))
            n=data['keypoints0'].shape[1]
            return dict(matches0=torch.arange(n)[None], matching_scores0=torch.ones(1,n))
        b._model=lambda *args:model
        class Dataset:
            def __init__(self, image_dir, preprocessing, image_list):self.names=image_list
            def __getitem__(self,i):return dict(image=np.zeros((1,8,8),np.float32),original_size=[8,8])
        b.extract_module=SimpleNamespace(ImageDataset=Dataset)
        return b

    def test_raw_extraction_reused_but_different_input_or_config_not_reused(self):
        b=self.backend()
        with TemporaryDirectory() as temp, b.query_reuse():
            p=Path(temp);(p/'same.png').write_bytes(b'first original bytes')
            conf={'model':{'name':'superpoint'},'preprocessing':{}}
            def extract(name, config=conf):
                b.extract(config,p,p,image_list=['same.png'],feature_path=p/name,overwrite=True)
            extract('local.h5')
            # Filtering local features must not mutate the cached RAW extraction.
            with h5py.File(p/'local.h5','a') as f:f['same.png/keypoints'][0]=[99,99]
            extract('global.h5')
            self.assertEqual(len(b.calls),1)
            with h5py.File(p/'global.h5') as f:
                np.testing.assert_equal(f['same.png/keypoints'][0],[1,2])
                self.assertEqual(f['same.png/keypoints'].attrs['uncertainty'],1.)
            (p/'same.png').write_bytes(b'different original bytes');extract('different.h5')
            extract('config.h5',{**conf,'preprocessing':{'resize_max':4}})
            self.assertEqual(len(b.calls),3)
        self.assertIsNone(b._query_reuse)

    def test_match_overlap_reused_but_changed_filtered_features_not_reused(self):
        b=self.backend()
        source=dict(keypoints=np.ones((2,2),np.float16),descriptors=np.ones((4,2),np.float16),
                    scores=np.ones(2,np.float16),image_size=np.array([8,8]))
        b.reference_features={n:source for n in ['a','b','c']}
        with TemporaryDirectory() as temp, b.query_reuse():
            p=Path(temp);features=p/'features.h5';pairs=p/'pairs.txt'
            with h5py.File(features,'w') as f:
                for k,v in source.items():f.create_dataset('q/'+k,data=v)
            def match(name,refs):
                pairs.write_text(''.join('q '+r+'\n' for r in refs))
                b.match({'model':{'name':'lightglue'}},pairs,features=features,
                        features_ref=p/'unused',matches=p/name,overwrite=True)
            match('local.h5',['a','b']);match('global.h5',['a','b','c'])
            self.assertEqual(len(b.calls),3)
            with h5py.File(p/'local.h5') as a,h5py.File(p/'global.h5') as g:
                for ref in ['a','b']:
                    for k in ['matches0','matching_scores0']:
                        np.testing.assert_equal(a['q/'+ref+'/'+k][:],g['q/'+ref+'/'+k][:])
            with h5py.File(features,'a') as f:f['q/keypoints'][0]=[9,9]
            match('masked.h5',['a']);self.assertEqual(len(b.calls),4)
        with b.query_reuse():
            self.assertEqual(b._query_reuse,{'features':{},'matches':{}})

    def test_transaction_always_clears_on_error_and_disallows_overlap(self):
        b=self.backend()
        with self.assertRaisesRegex(RuntimeError,'test failure'):
            with b.query_reuse():
                with self.assertRaisesRegex(RuntimeError,'overlapping'):
                    with b.query_reuse():pass
                raise RuntimeError('test failure')
        self.assertIsNone(b._query_reuse)

    def test_local_and_global_attempt_share_only_one_transaction(self):
        b=self.backend();b.manifest={'manifest_sha256':'map'};b.map_dir=Path('map')
        b.last_references=['a'];seen=[]
        def localize(*args,**kwargs):
            seen.append(id(b._query_reuse))
            b._query_reuse['matches']['test']='value'
            return SimpleNamespace(pose_status='failed')
        with patch('go1loc.localization.localize_photo',side_effect=localize),patch(
                'go1loc.navigation_localizer.verified_localization',return_value=({'pose_status':'failed'},{ })):
            b.localize(Path('photo'),Path('output'))
        self.assertEqual(len(seen),2);self.assertEqual(seen[0],seen[1])
        self.assertIsNone(b._query_reuse)

    def test_compact_evidence_preserves_content_digest_and_atomic_replacement(self):
        payload={'rows':[{'xyz':[1.,2.,3.], 'text':'证据'}]*20}
        with TemporaryDirectory() as temp:
            p=Path(temp);write_json(p/'pretty.json',payload)
            write_json(p/'compact.json',payload,compact=True)
            self.assertEqual(digest_json(read_json(p/'compact.json')),digest_json(payload))
            self.assertLess((p/'compact.json').stat().st_size,(p/'pretty.json').stat().st_size)
            self.assertFalse((p/'compact.json.tmp').exists())
