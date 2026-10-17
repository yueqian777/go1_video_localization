"""Known planar synthetic regressions, never real movement acceptance."""
from copy import deepcopy
import unittest
import numpy as np
from test_planning import fixture
from test_navigation import observation
from go1loc.navigation import build_navigation_index, associate_route, plan_guidance, NavigationTask


def straight(reverse=True):
    m, refs, frames = fixture()
    refs.pop('wall.png'); frames['frames'] = frames['frames'][:3]
    for i, ref in enumerate(refs.values()):
        T = np.eye(4); T[2, 3] = i
        ref['T_CW'] = np.linalg.inv(T)[:3].tolist()
    return build_navigation_index(m, refs, frames, dict(map_hash=m['manifest_sha256'],
        robot_traversed=True, unhandled_unedited=True,
        reverse_allowed=reverse, reversible_plane_confirmed=reverse))


def measured(g, z, seq=1, reverse=False, matches=4):
    o = observation(g, (0, 0, z), tuple(g['nodes']), seq)
    if reverse:
        o['T_WC_query'][0][0] = -1.; o['T_WC_query'][2][2] = -1.
    o['target_matches'] = matches
    return o


def goals(node='z.png'):
    return [dict(node_id=node, quality=1., support_count=4, target_query_sha256='e'*64)]


class BoundaryTests(unittest.TestCase):
    def test_opposite_visual_axis_still_associates_with_real_reference_evidence(self):
        g = straight(); a = associate_route(g, measured(g, 1.6, reverse=True))
        self.assertEqual(a['status'], 'associated')
        p = plan_guidance(g, a, goals())
        self.assertEqual(p['status'], 'planned')
        self.assertEqual(p['route_edge_ids'], ['auto:1:reverse', 'auto:0:reverse'])
        self.assertFalse(g['edges']['auto:1:reverse']['provenance']['reverse_visual_support'])

    def test_goal_selects_reverse_not_insertion_order_even_when_facing_forward(self):
        g = straight()
        for invert in (False, True):
            if invert: g['edges'] = dict(reversed(list(g['edges'].items())))
            p = plan_guidance(g, associate_route(g, measured(g, 1.6)), goals())
            self.assertEqual(p['status'], 'planned')
            self.assertEqual(p['route_edge_ids'], ['auto:1:reverse', 'auto:0:reverse'])

    def test_goal_behind_requires_turn_not_a_fabricated_reverse_view(self):
        g = straight(); t = NavigationTask(g, goals(), up=[0,-1,0])
        t.observe(measured(g, 1.6), now=1)
        self.assertEqual(t.state, 'turning')
        self.assertEqual(t.guidance['direction'], '请先转身')
        self.assertFalse(t.guidance['reverse_visual_support'])

    def test_no_reverse_declaration_reports_direction_limit_not_off_route(self):
        g = straight(False); t = NavigationTask(g, goals(), up=[0,-1,0])
        t.observe(measured(g, 1.6, reverse=True), now=1)
        self.assertEqual(t.reason, 'reverse_travel_not_enabled')
        self.assertTrue(t.current_pose_available(1))
        self.assertIsNone(t.guidance)

    def test_opposite_view_needs_both_endpoint_evidence_and_upright_pose(self):
        g = straight(); o = measured(g, 1.6, reverse=True)
        o['matched_references'] = ['m.png']
        self.assertNotEqual(associate_route(g,o)['status'], 'associated')
        o = measured(g, 1.6, reverse=True)
        o['T_WC_query'][0][0]=1.;o['T_WC_query'][1][1]=-1.
        self.assertNotEqual(associate_route(g,o)['status'], 'associated')

    def test_closed_physical_segment_does_not_escape_via_its_reverse(self):
        g = straight(); t = NavigationTask(g, goals(), up=[0,-1,0])
        t.close_edge('auto:1')
        t.observe(measured(g, 1.6, reverse=True), now=1)
        self.assertIsNone(t.guidance)
        self.assertIn('auto:1:reverse', t.closed)

    def test_at_first_goal_no_return_path_needed_and_new_observation_confirms(self):
        g = straight(False); t = NavigationTask(g, goals(), up=[0,-1,0])
        t.observe(measured(g, 0), now=1)
        self.assertEqual(t.state, 'observation_region_reached')
        self.assertEqual(t.plan['route_edge_ids'], [])
        self.assertFalse(t.confirm(now=1))
        t.observe(measured(g, .01, seq=2), now=2)
        self.assertTrue(t.confirm(now=2))

    def test_near_target_needs_geometry_heading_and_verified_independent_input(self):
        for change in ('far', 'backward', 'no_target', 'bad_pose', 'map', 'old_target'):
            g=straight(False); t=NavigationTask(g, goals(), up=[0,-1,0])
            o=measured(g, 0)
            if change=='far': o=measured(g, 1.8)
            elif change=='backward': o=measured(g, 0, reverse=True)
            elif change=='no_target': o['target_matches']=0
            elif change=='bad_pose': o['pose_status']='failed'
            elif change=='map': o['map_hash']='other'
            else: o['query_sha256']='e'*64
            t.observe(o, now=1)
            self.assertNotEqual(t.state, 'observation_region_reached', change)
            self.assertFalse(t.confirm(now=1), change)

    def test_confirmation_cannot_survive_leave_pause_stale_or_another_goal(self):
        for change in ('leave','pause','stale','identity'):
            g=straight(False);t=NavigationTask(g, goals(), up=[0,-1,0])
            o=measured(g,0);o['time_mode']='live_current';t.observe(o,now=1)
            self.assertEqual(t.state,'observation_region_reached')
            if change=='leave':
                o=measured(g,2,seq=2);o['time_mode']='live_current';t.observe(o,now=2)
            elif change=='pause':t.pause();t.resume()
            elif change=='stale':t.tick(5)
            else:
                o=measured(g,0,seq=2);o['query_sha256']='e'*64;o['time_mode']='live_current';t.observe(o,now=2)
            o=measured(g,0,seq=3);o['time_mode']='live_current';t.observe(o,now=3)
            self.assertFalse(t.confirm(now=3),change)


if __name__=='__main__': unittest.main()
