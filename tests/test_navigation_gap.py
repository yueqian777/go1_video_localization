"""Additional real observations may repair a gap; invented poses may not."""
import unittest
from copy import deepcopy
from pathlib import Path
import tempfile
from unittest.mock import patch
import numpy as np
from test_navigation import setup_graph, observation
from go1loc.navigation_gap import add_verified_gap, load_gap_recoveries, _verify_dense_source
from go1loc.navigation import associate_route, plan_guidance
from go1loc.jobs import atomic_json, read_json
from go1loc.video import sha256_file


def gap_fixture():
    g=setup_graph()
    old=g['edges'].pop('auto:0')
    g['exceptions']=[dict(from_frame='z.png',to_frame='lost.png',reasons=['unregistered_or_rejected_frame_no_bridge'])]
    def sample(name,t,xyz,registered=False):
        T=np.eye(4);T[:3,3]=xyz
        return dict(name=name,pts_time_s=t,T_MC=T.tolist(),registered=registered,
                    references=['z.png','a.png'],evidence_sha256='a'*64)
    samples=[sample('z.png',0,[0,0,0],True),sample('extra.png',.25,[0,1,0]),
             sample('a.png',.5,[0,2,0],True)]
    return g,samples


class GapTests(unittest.TestCase):
    def test_actual_observation_chain_restores_directed_path_not_missing_pose(self):
        g,rows=gap_fixture(); repaired=add_verified_gap(g,rows,repair_id='test',provenance={'verified_input':True})
        self.assertNotIn('lost.png',repaired['nodes'])
        self.assertIsNone(repaired['nodes']['extra.png']['image_id'])
        self.assertIn('resolved_by',repaired['exceptions'][0])
        o=observation(g,xyz=(0,1.5,0));a=associate_route(repaired,o)
        self.assertEqual(a['status'],'associated')
        p=plan_guidance(repaired,a,[dict(node_id='m.png',quality=1.)])
        self.assertEqual(p['status'],'planned')
        self.assertFalse(p['execution_permitted'])
        self.assertNotIn('extra.png',g['nodes'])

    def test_no_evidence_time_gap_jump_or_anchor_change_no_repair(self):
        g,rows=gap_fixture()
        for mutation in ('no_evidence','time_gap','jump','wrong_anchor','one_only'):
            bad=deepcopy(rows)
            if mutation=='no_evidence':bad[1]['evidence_sha256']=None
            if mutation=='time_gap':bad[-1]['pts_time_s']=1.1
            if mutation=='jump':bad[1]['T_MC'][0][3]=100
            if mutation=='wrong_anchor':bad[0]['T_MC'][0][3]=.1
            if mutation=='one_only':bad=[bad[0],bad[-1]]
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                add_verified_gap(g,bad,repair_id='test',provenance={})

    def test_repair_does_not_create_reverse_or_bypass_closed_edges(self):
        g,rows=gap_fixture();r=add_verified_gap(g,rows,repair_id='test',provenance={})
        a=associate_route(r,observation(g,xyz=(0,.5,0)))
        path=plan_guidance(r,a,[dict(node_id='m.png',quality=1.)])
        closed={path['route_edge_ids'][1]}
        self.assertEqual(plan_guidance(r,a,[dict(node_id='m.png',quality=1.)],closed)['status'],'no_path')


class GapBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.dense=self.root/'dense';self.dense.mkdir()
        self.source=self.root/'source.mkv';self.source.write_bytes(b'unit-test-only-not-a-video')
        self.g,_=gap_fixture();self.rows=[];self.results={}
        names=['z.png','extra1.png','lost.png','extra3.png','a.png']
        for i,name in enumerate(names):
            photo=self.dense/name;photo.write_bytes(name.encode())
            self.rows.append(dict(frame_index=i,pts_time_s=i*.125,image_path=name,size_wh=[20,10],
                quality={'mean_change_64px':1.,'rejection_reasons':[]}))
            T=np.eye(4);T[1,3]=i*.5
            value=dict(map_hash=self.g['map_hash'],component_id=self.g['component_id'],
                pose_status='insufficient_evidence' if i==2 else 'localized',T_WC_query=T.tolist(),
                matched_references=['z.png','a.png'],quality=dict(unique_inlier_keypoints=50,
                    inlier_ratio=.5,inlier_4x4_grid_coverage=.5,p95_inlier_reprojection_px=2))
            self.results[str(i)]=(value,dict(query_path=str(photo),evidence_sha256=str(i)*64))
        source=dict(source_path=str(self.source),source_sha256=sha256_file(self.source))
        self.frames=dict(probe=source,frames=[self.rows[i] for i in (0,2,4)])
        self.manifest={'reference_image_sha256':{n:sha256_file(self.dense/n) for n in ('z.png','a.png')}}
        self.meta=dict(frames=self.rows,probe=source,signature=dict(video_sha256=source['source_sha256'],
            calibration_sha256=None,config=dict(duration_s=.6,max_frames=5)),
            geometry=dict(status='uncalibrated',rotation_degrees=0))
        self.config=dict(map_hash=self.g['map_hash'],repairs=[dict(from_frame='z.png',to_frame='a.png',
            prepared_dir=str(self.dense),queries={str(i):str(i) for i in (1,2,3)})])
        self.save()

    def save(self):
        atomic_json(self.root/'gap_recoveries.json',self.config)
        atomic_json(self.dense/'frames_manifest.json',self.meta)

    def load(self):
        with patch('go1loc.navigation_gap._verify_dense_source') as decode, patch(
                'go1loc.navigation_localizer.verified_localization',side_effect=lambda p,h:self.results[p]):
            result=load_gap_recoveries(self.g,self.manifest,self.frames,self.root)
            decode.assert_called_once()
            return result

    def test_load_derives_pose_from_evidence_and_retains_failed_frame(self):
        result=self.load()
        self.assertNotIn('lost.png',result['nodes'])
        self.assertIn('extra1.png',result['nodes'])
        self.assertEqual(result['gap_recoveries'][0]['rejected_samples'][0]['frame'],'lost.png')
        # Missing sidecar is exactly the original no-bridge graph.
        self.assertIs(load_gap_recoveries(self.g,self.manifest,self.frames,self.root/'absent'),self.g)

    def test_wrong_photo_map_component_or_missing_query_never_fills_gap(self):
        self.config['map_hash']='wrong';self.save()
        with self.assertRaisesRegex(ValueError,'map_mismatch'):self.load()
        self.config['map_hash']=self.g['map_hash'];self.save()
        self.results['1'][1]['query_path']=str(self.dense/'z.png')
        with self.assertRaisesRegex(ValueError,'photo_or_map'):self.load()
        self.results['1'][1]['query_path']=str(self.dense/'extra1.png')
        del self.config['repairs'][0]['queries']['2'];self.save()
        with self.assertRaisesRegex(ValueError,'evidence_missing'):self.load()
        self.config['repairs'][0]['queries']['2']='2';self.save()
        self.results['1'][0]['component_id']='another'
        self.results['3'][0]['component_id']='another'
        with self.assertRaisesRegex(ValueError,'additional_observations'):self.load()

    def test_source_timing_and_discontinuities_fail_closed(self):
        original=deepcopy(self.meta)
        for mutate,reason in [
            (lambda:self.meta['probe'].update(source_sha256='other'),'source_or_geometry'),
            (lambda:self.meta['geometry'].update(status='rectified'),'source_or_geometry'),
            (lambda:self.meta['frames'][0].update(pts_time_s=-1),'anchor_time'),
            (lambda:self.meta['frames'][1].update(frame_index=99),'decode_incomplete'),
            (lambda:self.meta['frames'][1]['quality'].update(mean_change_64px=81),'visual_cut')]:
            self.meta=deepcopy(original);mutate();self.save()
            with self.subTest(reason=reason),self.assertRaisesRegex(ValueError,reason):self.load()
        self.meta=original;self.save();self.source.write_bytes(b'changed-source')
        with self.assertRaisesRegex(ValueError,'original_video_changed'):self.load()

    def test_native_redecode_checks_pixels_and_pts_not_just_json_hash(self):
        def decode(path,out,config):
            out.mkdir(parents=True)
            for row in self.rows:
                (out/row['image_path']).write_bytes(row['image_path'].encode())
            return deepcopy(self.meta)
        with patch('go1loc.video.prepare_video',side_effect=decode):
            _verify_dense_source(self.source,self.dense,self.meta,self.root)
            (self.dense/'extra1.png').write_bytes(b'substituted-photo')
            with self.assertRaisesRegex(ValueError,'pixels_mismatch'):
                _verify_dense_source(self.source,self.dense,self.meta,self.root)
        with patch('go1loc.video.prepare_video',return_value={'frames':[]}):
            with self.assertRaisesRegex(ValueError,'timeline_mismatch'):
                _verify_dense_source(self.source,self.dense,self.meta,self.root)
