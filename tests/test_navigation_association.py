"""Synthetic association/state regressions; not localization accuracy tests."""
from copy import deepcopy
import unittest
import numpy as np

from test_navigation import observation, setup_graph
from go1loc.navigation import NavigationTask, associate_route


def task(g):
    return NavigationTask(g, [{'node_id':'m.png','quality':1.}], up=[1,0,0])


def dwell_graph():
    g=setup_graph();g['typical_step']=1.;g['nodes']={};g['edges']={}
    for i,z in enumerate([0.,.001,.002,.003,.004,1.]):
        name=f'{i}.png';T=np.eye(4);T[2,3]=z
        g['nodes'][name]={'T_MC':T.tolist()}
        if i:
            eid=f'e{i-1}';a=f'{i-1}.png'
            g['edges'][eid]=dict(edge_id=eid,enabled=True,**{'from':a,'to':name},
                path=[np.asarray(g['nodes'][a]['T_MC'])[:3,3].tolist(),T[:3,3].tolist()],
                observation_only=i<5,length=z-[0.,.001,.002,.003,.004][i-1],
                provenance={'kind':'automatic_recorded_traversal','traversal_id':'t0'})
    return g


class AssociationTests(unittest.TestCase):
    def test_core_entry_then_small_noise_keeps_fresh_evidence_bound_track(self):
        g=setup_graph();s=g['typical_step']
        a=associate_route(g,observation(g,(0,1,1.98*s)))
        self.assertEqual(a['status'],'associated')
        o=observation(g,(0,1,2.2*s),seq=2)
        self.assertEqual(associate_route(g,o)['status'],'off_route')
        b=associate_route(g,o,a)
        self.assertEqual(b['status'],'associated')
        self.assertEqual(b['association_mode'],'bounded_tracking')
        self.assertGreater(b['distance'],2*s)
        # Keep the measured pose, never replace it with the route projection.
        self.assertEqual(o['T_WC_query'][2][3],2.2*s)

    def test_tracking_is_not_an_unbounded_snap_or_permission_across_sources(self):
        g=setup_graph();s=g['typical_step']
        a=associate_route(g,observation(g,(0,1,1.98*s)))
        for key,value in [('session_id','other'),('camera_id','camera1'),('map_hash','other'),
                ('component_id','other'),('time_mode','live_current'),('sequence',1),
                ('image_time_s',9.),('evidence_verified',False),('matched_references',['z.png'])]:
            o=observation(g,(0,1,2.2*s),seq=2);o[key]=value
            self.assertNotEqual(associate_route(g,o,a)['status'],'associated',key)
        self.assertEqual(associate_route(g,observation(g,(0,1,2.51*s),seq=2),a)['status'],'off_route')

    def test_contiguous_dwell_samples_are_not_different_branches(self):
        g=dwell_graph();o=observation(g,(.2,0,0),tuple(g['nodes']))
        self.assertEqual(associate_route(g,o)['status'],'associated')

    def test_repeat_place_transfer_does_not_split_original_dwell_chain(self):
        g=dwell_graph();e=deepcopy(g['edges']['e2'])
        e.update(edge_id='junction:stop',**{'from':'3.png','to':'0.png'})
        e['provenance']={'kind':'automatic_repeat_place'};g['edges'][e['edge_id']]=e
        self.assertEqual(associate_route(g,observation(g,(.2,0,0),tuple(g['nodes'])))['status'],'associated')

    def test_nearby_but_disconnected_or_moving_tracks_stay_ambiguous(self):
        for change in ('gap','moving','source','fork'):
            g=dwell_graph();o=observation(g,(.2,0,0),tuple(g['nodes']))
            if change=='gap':g['edges']['e2']['enabled']=False
            elif change=='moving':g['edges']['e2']['observation_only']=False
            elif change=='source':g['edges']['e2']['provenance']['traversal_id']='different'
            else:
                e=deepcopy(g['edges']['e2']);e['edge_id']='fork';e['to']='5.png';g['edges']['fork']=e
            self.assertEqual(associate_route(g,o)['status'],'ambiguous_branch',change)

    def test_stationary_shadow_guidance_never_infers_closed_edge(self):
        g=setup_graph();t=task(g)
        for seq in (1,30,60):
            t.observe(observation(g,(0,1,0),seq=seq),now=seq)
        self.assertFalse(t.closed)
        self.assertEqual(t.plan['status'],'planned')
        self.assertIsNotNone(t.guidance)
        self.assertEqual(t.snapshot()['progress_notice'],'stationary_or_no_observed_progress')
        t.close_edge(t.association['edge_id'])
        t.observe(observation(g,(0,1,0),seq=61),now=61)
        self.assertEqual(t.reason,'current_segment_closed')
        self.assertIsNone(t.guidance)

    def test_tracking_cannot_survive_stale_pause_invalid_pose_or_source(self):
        g=setup_graph();s=g['typical_step']
        for failure in ('stale','pause','failed_pose','source','duplicate'):
            t=task(g);o=observation(g,(0,1,1.98*s));o['time_mode']='live_current'
            t.observe(o,now=1)
            if failure=='stale':t.tick(5)
            elif failure=='pause':t.pause();t.resume()
            else:
                bad=observation(g,(0,1,1.98*s),seq=2);bad['time_mode']='live_current'
                if failure=='failed_pose':bad['pose_status']='failed'
                elif failure=='source':bad['camera_id']='camera1'
                else:bad['query_sha256']=o['query_sha256']
                t.observe(bad,now=2)
            new=observation(g,(0,1,2.2*s),seq=3);new['time_mode']='live_current'
            t.observe(new,now=3)
            self.assertEqual(t.reason,'off_route',failure)
            self.assertIsNone(t.guidance)

    def test_off_route_reports_nearest_rejection_not_empty_diagnostics(self):
        g=setup_graph();a=associate_route(g,observation(g,(20,20,20)))
        self.assertEqual(a['status'],'off_route')
        self.assertIn('distance',a['rejected_candidates'][0]['rejection_reasons'])


if __name__=='__main__':unittest.main()
