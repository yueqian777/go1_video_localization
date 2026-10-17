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
        from hloc.localize_sfm import do_covisibility_clustering
        from itertools import permutations
        observed=[]
        class Points(list):
            def __iter__(self):
                observed.append(1)
                return super().__iter__()
        tracks={10:[1,3],20:[3,9],30:[21]}
        def reconstruction(count):
            return SimpleNamespace(images={n:SimpleNamespace(points2D=(Points if count else list)(
                [SimpleNamespace(point3D_id=p,has_point3D=lambda:True)
                 for p, ids in tracks.items() if n in ids])) for n in [1,3,9,21]},
                points3D={p:SimpleNamespace(track=SimpleNamespace(elements=[SimpleNamespace(image_id=n)
                    for n in ids])) for p,ids in tracks.items()})
        b=self.backend();b.reconstruction=reconstruction(True)
        reference=reconstruction(False)
        for ids in ([3,1],[1,3],[9,1],[21],*permutations([1,3,9,21])):
            expected=do_covisibility_clustering(list(ids),reference)
            actual=b.covisibility_clusters(list(ids))
            self.assertEqual(actual,expected)
            actual[0].append(-999)
            self.assertEqual(b.covisibility_clusters(list(ids)),expected)
        # New subsets and permutations must not walk frozen tracks repeatedly.
        self.assertEqual(len(observed),4)

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

    def test_initial_camera_reuse_is_per_original_bytes_and_never_refined_pose(self):
        import pycolmap
        b=self.backend()
        camera=pycolmap.Camera(model='PINHOLE',width=8,height=8,params=[10,10,4,4])
        with TemporaryDirectory() as temp, patch('pycolmap.infer_camera_from_image',return_value=camera) as infer:
            p=Path(temp);a=p/'a.png';c=p/'c.png';a.write_bytes(b'same');c.write_bytes(b'same')
            with b.query_reuse():
                first=b.infer_initial_camera(a);first.params[0]=99
                second=b.infer_initial_camera(c)
                self.assertEqual(second.params[0],10)
                self.assertEqual(infer.call_count,1)
                c.write_bytes(b'changed');b.infer_initial_camera(c)
                self.assertEqual(infer.call_count,2)
            with b.query_reuse():b.infer_initial_camera(a)
            self.assertEqual(infer.call_count,3)
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

    def test_prefetch_joins_before_global_matching_and_does_not_cross_queries(self):
        import threading
        b=self.backend()
        data=dict(keypoints=np.ones((2,2),np.float16),descriptors=np.ones((4,2),np.float16),
                  scores=np.ones(2,np.float16),image_size=np.array([8,8]))
        b.reference_features={n:data for n in ['a','b','c']}
        entered,release=threading.Event(),threading.Event()
        original=b._model(None,None)
        def gated(inputs):
            if threading.current_thread().name=='localization-match-prefetch':
                entered.set()
                if not release.wait(5): raise RuntimeError('test_release_timeout')
            return original(inputs)
        b._model=lambda *args:gated
        with TemporaryDirectory() as temp:
            p=Path(temp);features=p/'features.h5';pairs=p/'pairs.txt'
            with h5py.File(features,'w') as f:
                for k,v in data.items():f.create_dataset('q/'+k,data=v)
            def match(name, refs):
                pairs.write_text(''.join('q '+r+'\n' for r in refs))
                b.match({'model':{'name':'lightglue'}},pairs,features=features,
                        features_ref=p/'unused',matches=p/name,overwrite=True)
            with b.query_reuse():
                match('local.h5',['a'])
                self.assertTrue(b.prefetch_global_matches(['a','b','c']))
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertEqual(len(b.calls),1)  # main thread can continue PnP work
                finally:release.set()
                match('global.h5',['a','b','c'])
                self.assertIsNone(b._match_prefetch)
                self.assertEqual(len(b.calls),3)
                self.assertEqual(b.last_profile['prefetch_match_pairs'],2)
                with h5py.File(p/'global.h5') as f:
                    np.testing.assert_equal(f['q/b/matches0'][:],[0,1])
                # Even if prefetch completed, changed filtered query arrays miss the cache.
                with h5py.File(features,'a') as f:f['q/keypoints'][0]=[9,9]
                match('changed.h5',['b']);self.assertEqual(len(b.calls),4)
            with b.query_reuse():
                self.assertFalse(b.prefetch_global_matches(['b']))
                match('new_query.h5',['b']);self.assertEqual(len(b.calls),5)

    def test_prefetch_error_cleans_up_and_is_not_a_silent_match_success(self):
        import threading
        b=self.backend()
        def broken(ref): raise ValueError('matcher_failed')
        with self.assertRaisesRegex(RuntimeError,'prefetch_failed'):
            with b.query_reuse():
                b._match_context=(('fingerprint','configuration'),broken)
                self.assertTrue(b.prefetch_global_matches(['a']))
                b._finish_match_prefetch(cancel=False)
        self.assertIsNone(b._match_prefetch)
        self.assertIsNone(b._query_reuse)
        self.assertFalse(any(t.name=='localization-match-prefetch' for t in threading.enumerate()))

    def test_local_success_cancels_and_joins_prefetch_inside_recorded_total(self):
        import threading
        b=self.backend();entered=threading.Event();release=threading.Event();computed=[]
        def predict(ref):
            entered.set()
            if not release.wait(5):raise RuntimeError('test_release_timeout')
            computed.append(ref)
            return {}
        def localize(*args):
            b._match_context=(('q','conf'),predict)
            b.prefetch_global_matches(['a','b','c'])
            self.assertTrue(entered.wait(5))
            cancel=b._match_prefetch['cancel']
            def release_after_cancel():
                cancel.wait(5);release.set()
            helper=threading.Thread(target=release_after_cancel)
            helper.start()
            b.helper=helper
            return 'localized'
        b._localize=localize
        self.assertEqual(b.localize(Path('photo'),Path('out')),'localized')
        b.helper.join(5)
        self.assertEqual(computed,['a'])
        self.assertIsNone(b._match_prefetch)
        self.assertIsNone(b._query_reuse)
        self.assertGreaterEqual(b.last_profile['total_s'],b.last_profile['prefetch_wait_s'])
        self.assertEqual(b.last_profile['match_pairs'],1)

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
