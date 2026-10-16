"""Algorithm fixtures, not a hardware or localization accuracy benchmark."""
from copy import deepcopy
import unittest
import numpy as np

from test_planning import fixture
from go1loc.navigation import build_navigation_index, associate_route, plan_guidance, NavigationTask


def setup_graph():
    m, r, f = fixture()
    # No hand-reviewed segments are supplied to operational topology.
    f.pop('route_segments')
    f['frames'] = f['frames'][:3]
    r.pop('wall.png')
    return build_navigation_index(m, r, f, {'robot_traversed': True, 'unhandled_unedited': True,
        'map_hash': m['manifest_sha256'], 'reverse_allowed': False})


def observation(g, xyz=(0, 1, 0), refs=('z.png', 'a.png'), seq=1):
    T = np.eye(4); T[:3, 3] = xyz
    return dict(map_hash=g['map_hash'], component_id=g['component_id'], pose_status='localized',
        T_WC_query=T.tolist(), matched_references=list(refs), query_sha256=str(seq)*64,
        sequence=seq, image_time_s=float(seq), received_monotonic=float(seq),
        camera_id='camera6', session_id='test', time_mode='offline_replay',
        evidence_verified=True, target_matches=0)


class NavigationTests(unittest.TestCase):
    def test_auto_edges_separate_from_strict_review_and_no_shortcut(self):
        g = setup_graph()
        self.assertEqual(len(g['edges']), 2)
        self.assertTrue(all(e['provenance']['kind'] == 'automatic_recorded_traversal' for e in g['edges'].values()))
        self.assertNotIn('verified_continuous', str(g))
        self.assertNotIn(('z.png', 'm.png'), [(e['from'], e['to']) for e in g['edges'].values()])

    def test_missing_registration_never_bridged(self):
        m, r, f = fixture(); r.pop('a.png')
        g = build_navigation_index(m, r, f, dict(robot_traversed=True, unhandled_unedited=True, map_hash=m['manifest_sha256']))
        self.assertFalse(any(e['from'] == 'z.png' for e in g['edges'].values()))
        self.assertTrue(g['exceptions'])

    def test_source_declaration_bound_to_map(self):
        m, r, f = fixture()
        with self.assertRaisesRegex(ValueError, 'source_declaration'):
            build_navigation_index(m, r, f, {'robot_traversed': True, 'map_hash': 'wrong'})

    def test_mid_edge_real_pose_and_evidence_not_nearest_node(self):
        g = setup_graph(); a = associate_route(g, observation(g))
        self.assertEqual(a['status'], 'associated'); self.assertAlmostEqual(a['fraction'], .5)
        o = observation(g); o['matched_references'] = []
        self.assertNotEqual(associate_route(g, o)['status'], 'associated')
        o = observation(g, (20, 20, 20))
        self.assertEqual(associate_route(g, o)['status'], 'off_route')

    def test_path_keeps_bend_goal_not_object_and_closed_edge_no_path(self):
        g = setup_graph(); a = associate_route(g, observation(g))
        goals = [{'node_id': 'm.png', 'quality': 1., 'support_count': 5}]
        p = plan_guidance(g, a, goals)
        self.assertEqual(p['status'], 'planned')
        self.assertEqual(p['path'], [[0., 1., 0.], [0., 2., 0.], [2., 2., 0.]])
        self.assertFalse(p['execution_permitted'])
        self.assertEqual(plan_guidance(g, a, goals, closed={'auto:1'})['status'], 'no_path')

    def test_no_reverse_without_declaration(self):
        g = setup_graph(); a = associate_route(g, observation(g, (1, 2, 0), ('a.png', 'm.png')))
        self.assertEqual(plan_guidance(g, a, [{'node_id': 'z.png', 'quality': 1.}])['status'], 'no_path')

    def test_no_completion_from_elapsed_time_or_old_target(self):
        g = setup_graph(); t = NavigationTask(g, [{'node_id': 'm.png', 'quality': 1.}], up=[0,0,1])
        t.observe(observation(g), now=1)
        self.assertNotEqual(t.state, 'target_confirmed')
        t.tick(now=100)
        self.assertNotEqual(t.state, 'target_confirmed')
        t.observe(observation(g, (2,2,0), ('a.png','m.png'), seq=2), now=2)
        self.assertEqual(t.state, 'observation_region_reached')
        self.assertFalse(t.confirm(now=2))
        o = observation(g, (2,2,0), ('a.png','m.png'), seq=3); o['target_matches'] = 4
        t.observe(o, now=3)
        self.assertTrue(t.confirm(now=3)); self.assertEqual(t.state, 'target_confirmed')

    def test_stale_live_clears_guidance_and_camera_change_rejected(self):
        g = setup_graph(); t = NavigationTask(g, [{'node_id':'m.png', 'quality':1.}], up=[0,0,1])
        o = observation(g); o['time_mode'] = 'live_current'; t.observe(o, now=1)
        t.tick(now=10); self.assertEqual(t.state, 'relocalizing'); self.assertIsNone(t.guidance)
        o = observation(g, seq=2); o['camera_id'] = 'camera1'
        t.observe(o, now=2); self.assertIsNone(t.guidance)

    def test_late_result_does_not_move_task_back(self):
        g = setup_graph(); t = NavigationTask(g, [{'node_id':'m.png','quality':1.}], up=[0,0,1])
        t.observe(observation(g, seq=3), now=3)
        t.observe(observation(g, (0,0,0), seq=1), now=4)
        self.assertEqual(t.last['sequence'], 3)

    def test_failure_retires_old_path_and_success_retires_old_failure_reason(self):
        g=setup_graph();t=NavigationTask(g,[{'node_id':'m.png','quality':1.}],up=[0,0,1])
        t.observe(observation(g),now=1)
        t.observe(observation(g,(30,30,30),seq=2),now=2)
        self.assertIsNone(t.plan);self.assertIsNone(t.guidance)
        t.observe(observation(g,(2,2,0),('a.png','m.png'),seq=3),now=3)
        self.assertIsNone(t.reason)

    def test_turn_clips_lookahead_and_reverse_is_explicit(self):
        g=setup_graph();t=NavigationTask(g,[{'node_id':'m.png','quality':1.}],up=[0,0,1])
        o=observation(g);o['T_WC_query']=[[1,0,0,0],[0,0,1,1],[0,-1,0,0],[0,0,0,1]]
        t.observe(o,now=1)
        self.assertEqual(t.guidance['waypoint'],[0.,2.,0.])
        m,r,f=fixture()
        reverse=build_navigation_index(m,r,f,dict(map_hash=m['manifest_sha256'],robot_traversed=True,
            unhandled_unedited=True,reverse_allowed=True,reversible_plane_confirmed=True))
        self.assertTrue(any(e.endswith(':reverse') for e in reverse['edges']))
        self.assertTrue(all(not e['provenance']['reverse_visual_support'] for k,e in reverse['edges'].items() if k.endswith(':reverse')))

    def test_target_visible_far_away_does_not_complete(self):
        g=setup_graph();t=NavigationTask(g,[{'node_id':'m.png','quality':1.}],up=[0,0,1])
        o=observation(g);o['target_matches']=12
        t.observe(o,now=1)
        self.assertFalse(t.confirm(now=1))

    def test_duplicate_pixels_do_not_count_as_progress(self):
        g = setup_graph(); t = NavigationTask(g, [{'node_id':'m.png','quality':1.}], up=[0,0,1])
        o = observation(g); t.observe(o, now=1)
        o2 = deepcopy(o); o2.update(sequence=2, image_time_s=30., received_monotonic=30.)
        t.observe(o2, now=30)
        self.assertNotEqual(t.state, 'target_confirmed')
        self.assertIn(t.state, ('blocked', 'relocalizing'))


if __name__ == '__main__':
    unittest.main()
