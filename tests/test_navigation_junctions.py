"""Synthetic route topology tests, NOT real robot or free-space acceptance."""
from copy import deepcopy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
import numpy as np

from test_planning import seal
from go1loc.navigation import build_navigation_index, plan_guidance, associate_route, NavigationTask
from go1loc.junctions import fuse_junctions
from go1loc.navigation_io import save_navigation_index
from go1loc.jobs import read_json


def junction_fixture(offset=(0., 0., 0.)):
    # Two separate traversals meet at J. A->J->E was never recorded as a run.
    names=['A.png','J1.png','C.png','D.png','J2.png','E.png']
    xyz=[(-1,0,0),(0,0,0),(1,0,0),(0,0,-1),offset,(0,0,1)]
    m=seal(dict(map_id='synthetic-junction',frozen=True,scale_status='arbitrary_monocular_scale',
        units='map_units',components=[dict(component_id='c0',image_ids=list(range(1,7)))],reference_images=names))
    refs={}; rows=[]; observations={}
    for i,(name,p) in enumerate(zip(names,xyz)):
        T=np.eye(4);T[:3,3]=p
        refs[name]=dict(image_id=i+1,T_CW=np.linalg.inv(T)[:3].tolist())
        rows.append(dict(image_path='images/'+name,pts_time_s=(i%3)*.5,frame_index=i,
            session_id='one' if i<3 else 'two',camera_id=6,kept=True,quality={}))
        observations[name]=dict(map_hash=m['manifest_sha256'],component_id='c0',
            T_MC=T.tolist(),points={str(j):dict(uv=[.15+(j%5)*.15,.2+(j//5)*.15],
                error_px=.3,depth=4.) for j in range(20)})
    frames=dict(frames=rows,source_kind='synthetic_test',probe={'source_sha256':'f'*64})
    g=build_navigation_index(m,refs,frames,dict(map_hash=m['manifest_sha256'],
        robot_traversed=True,unhandled_unedited=True))
    return g,observations


def start(g):
    return dict(status='associated',edge_id='auto:0',fraction=.5,projection=[-.5,0.,0.])


class JunctionTests(unittest.TestCase):
    def test_new_route_joins_two_traversals_without_rewriting_originals(self):
        g,obs=junction_fixture(); original=deepcopy(g)
        self.assertEqual(plan_guidance(g,start(g),[dict(node_id='E.png',quality=1.)])['status'],'no_path')
        fused=fuse_junctions(g,obs)
        p=plan_guidance(fused,start(g),[dict(node_id='E.png',quality=1.)])
        self.assertEqual(p['status'],'planned')
        self.assertTrue(p['recombined']);self.assertEqual(len(p['junction_transitions']),1)
        self.assertEqual(p['path'],[[-.5,0.,0.],[0.,0.,0.],[0.,0.,0.],[0.,0.,1.]])
        self.assertFalse(p['execution_permitted']);self.assertEqual(g,original)
        self.assertEqual(fused['nodes'],g['nodes'])
        for eid,e in g['edges'].items(): self.assertEqual(fused['edges'][eid],e)
        self.assertNotIn('verified_continuous',str(fused['junctions']))

    def test_nearby_different_tracks_and_shared_points_far_away_do_not_merge(self):
        for offset,different in [((.05,0,0),True),((.6,0,0),False),((0,.2,0),False)]:
            with self.subTest(offset=offset,different=different):
                g,obs=junction_fixture(offset)
                if different:
                    for n in ['D.png','J2.png','E.png']:
                        obs[n]['points']={str(int(k)+100):v for k,v in obs[n]['points'].items()}
                fused=fuse_junctions(g,obs)
                self.assertFalse(fused['junctions'])
                self.assertEqual(plan_guidance(fused,start(g),[dict(node_id='E.png',quality=1.)])['status'],'no_path')

    def test_missing_or_wrong_geometry_evidence_cannot_create_connection(self):
        for kind in ('missing','wrong_map','wrong_pose','narrow_patch','high_error','no_context'):
            with self.subTest(kind=kind):
                g,obs=junction_fixture()
                if kind=='missing': obs={}
                elif kind=='wrong_map': obs['J2.png']['map_hash']='another'
                elif kind=='wrong_pose': obs['J2.png']['T_MC'][0][3]=5.
                elif kind=='narrow_patch':
                    for pt in obs['J2.png']['points'].values():pt['uv']=[.5,.5]
                elif kind=='high_error':
                    for pt in obs['J2.png']['points'].values():pt['error_px']=9.
                else:
                    for n in ['D.png','E.png']:obs[n]['points']={}
                fused=fuse_junctions(g,obs)
                self.assertFalse(fused['junctions'])
                self.assertTrue(fused['junction_candidates'])

    def test_closing_junction_or_outgoing_edge_prevents_recombined_route(self):
        g,obs=junction_fixture();g=fuse_junctions(g,obs)
        goal=[dict(node_id='E.png',quality=1.)]
        jid=next(iter(g['junctions']));edges=g['junctions'][jid]['edge_ids']
        self.assertEqual(plan_guidance(g,start(g),goal,closed=edges)['status'],'no_path')
        self.assertEqual(plan_guidance(g,start(g),goal,closed=['auto:4'])['status'],'no_path')
        # A junction is not permission to drive all historical edges backwards.
        end=dict(status='associated',edge_id='auto:4',fraction=.5,projection=[0,0,.5])
        self.assertEqual(plan_guidance(g,end,[dict(node_id='A.png',quality=1.)])['status'],'no_path')

    def test_repeated_run_and_fusion_are_deterministic(self):
        g,obs=junction_fixture();a=fuse_junctions(g,obs)
        self.assertEqual(a,fuse_junctions(g,obs))
        self.assertEqual(a,fuse_junctions(a,obs))

    def test_index_update_archives_old_revision_and_stays_outside_map(self):
        g,obs=junction_fixture();new=fuse_junctions(g,obs)
        with TemporaryDirectory() as tmp:
            root=Path(tmp);root.joinpath('map').mkdir()
            save_navigation_index(root/'viewer',root/'map',g)
            save_navigation_index(root/'viewer',root/'map',new)
            self.assertEqual(read_json(root/'viewer'/'indexes'/(g['index_sha256']+'.json')),g)
            self.assertEqual(read_json(root/'viewer'/'navigation_index.json'),new)
            with self.assertRaises(ValueError):save_navigation_index(root/'map',root/'map',new)

    def test_context_binding_and_camera_changes_cannot_pass(self):
        for kind in ('context_map','camera'):
            g,obs=junction_fixture()
            if kind=='camera':g['nodes']['J2.png']['capture_source']['camera_id']=1
            else:
                for n in ('D.png','E.png'):obs[n]['map_hash']='wrong'
            self.assertFalse(fuse_junctions(g,obs)['junctions'])

    def test_junction_edges_are_not_current_pose_candidates(self):
        g,obs=junction_fixture();g=fuse_junctions(g,obs)
        o=dict(map_hash=g['map_hash'],component_id=g['component_id'],evidence_verified=True,
            pose_status='localized',T_WC_query=np.eye(4).tolist(),matched_references=['J1.png','J2.png'])
        a=associate_route(g,o)
        self.assertEqual(a['status'],'associated')
        self.assertTrue(all(not c['edge_id'].startswith('junction:') for c in a['candidates']))

    def test_no_long_range_transitive_position_merge(self):
        g,obs=junction_fixture((.25,0,0));fused=fuse_junctions(g,obs)
        self.assertEqual(fused['nodes']['J2.png']['T_MC'][0][3],.25)
        self.assertNotEqual(fused['nodes']['J1.png'],fused['nodes']['J2.png'])
        self.assertTrue(fused['junctions'])

    def test_lookahead_preserves_turn_at_recombined_junction(self):
        g,obs=junction_fixture();g=fuse_junctions(g,obs)
        t=NavigationTask(g,[dict(node_id='E.png',quality=1.)],up=[0,-1,0])
        # camera +z points down the first segment (+x).
        T=np.array([[0,0,1,-.5],[0,1,0,0],[-1,0,0,0],[0,0,0,1.]])
        o=dict(map_hash=g['map_hash'],component_id=g['component_id'],evidence_verified=True,
            pose_status='localized',T_WC_query=T.tolist(),matched_references=['A.png','J1.png'],
            query_sha256='a'*64,sequence=1,image_time_s=1.,received_monotonic=1.,
            session_id='query',camera_id='camera6',time_mode='offline_replay')
        # Reference headings must agree with incoming observation for association.
        for n in ['A.png','J1.png']:
            g['nodes'][n]['T_MC']=T.copy().tolist()
            g['nodes'][n]['T_MC'][0][3]=-1. if n=='A.png' else 0.
        t.observe(o,now=1.)
        self.assertIsNotNone(t.guidance)
        self.assertEqual(t.guidance['waypoint'],[0.,0.,0.])


if __name__=='__main__':unittest.main()
