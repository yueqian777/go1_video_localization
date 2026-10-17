"""Operational, observation-driven route guidance. No robot transport.

Automatic traversal topology is a sidecar, NOT a strict-review route graph.
Distances are camera trajectory distances, never inferred robot clearances.
"""
from copy import deepcopy
import math
from pathlib import Path
import numpy as np

from .evidence import digest_json
from .planning import build_route_graph, _dijkstra, _rigid


def position(pose):
    return _rigid(pose)[:3, 3]


def unit(v):
    v = np.asarray(v, float)
    if v.shape != (3,) or not np.isfinite(v).all() or np.linalg.norm(v) < 1e-9:
        raise ValueError('invalid_direction')
    return v / np.linalg.norm(v)


def build_navigation_index(manifest, references, frames, declaration):
    if (declaration.get('map_hash') != manifest['manifest_sha256']
            or declaration.get('robot_traversed') is not True
            or declaration.get('unhandled_unedited') is not True):
        raise ValueError('source_declaration_required')
    graph = build_route_graph(manifest, references, frames, route_segments=[])
    graph['schema_version'] = 'go1loc.navigation_index.v1'
    graph.pop('graph_sha256', None)
    graph.update(source_declaration=deepcopy(declaration), exceptions=[], navigation_nodes=[],
                 topology_policy='recorded_direction_temporal_adjacency_not_free_space')
    rows = frames.get('frames', [])
    nodes = graph['nodes']
    names = [Path(r.get('image_path') or '').name for r in rows]
    for name,row in zip(names,rows):
        if name in nodes:
            nodes[name]['capture_source']={k:row.get(k,frames.get(k)) for k in ('camera_id','session_id')}
    traversal=0
    lengths = [float(np.linalg.norm(position(nodes[b]['T_MC'])-position(nodes[a]['T_MC'])))
               for a, b in zip(names, names[1:]) if a in nodes and b in nodes]
    step = float(np.median([x for x in lengths if x > 1e-5])) if any(x > 1e-5 for x in lengths) else 1.
    graph['typical_step'] = step
    graph['up_estimate'] = unit(np.median([-_rigid(n['T_MC'])[:3,1] for n in nodes.values()], axis=0)).tolist()
    graph['up_provenance'] = 'robust_camera_up_estimate_requires_upright_mount_confirmation'
    for i, (a, b) in enumerate(zip(names, names[1:])):
        ra, rb = rows[i:i+2]
        reasons = []
        if a not in nodes or b not in nodes:
            reasons.append('unregistered_or_rejected_frame_no_bridge')
        dt = None
        try:
            dt = float(rb['pts_time_s']) - float(ra['pts_time_s'])
            if not math.isfinite(dt) or not 0 < dt <= 2.:
                reasons.append('nonmonotonic_or_time_gap')
        except (KeyError, TypeError, ValueError):
            reasons.append('timing_missing')
        for key in ('camera_id', 'session_id', 'source_sha256'):
            if ra.get(key) != rb.get(key):
                reasons.append(key+'_changed')
        for r in (ra, rb):
            quality = r.get('quality') or {}
            flags = quality.get('rejection_reasons', []) + r.get('continuity_flags', [])
            if flags or r.get('kept') is False:
                reasons.append('quality_or_continuity_exception')
            if (quality.get('mean_change_64px') or 0) > 80:
                reasons.append('possible_visual_cut')
        if a in nodes and b in nodes:
            Ta, Tb = (_rigid(nodes[n]['T_MC']) for n in (a,b))
            length = float(np.linalg.norm(Tb[:3,3]-Ta[:3,3]))
            if length > 8*step:
                reasons.append('pose_translation_jump')
            angle = math.degrees(math.acos(float(np.clip((np.trace(Ta[:3,:3].T@Tb[:3,:3])-1)/2, -1,1))))
            if angle > 120:
                reasons.append('pose_rotation_jump')
        if reasons:
            graph['exceptions'].append(dict(from_frame=a, to_frame=b, reasons=sorted(set(reasons))))
            traversal+=1
            continue
        eid = f'auto:{i}'
        edge = dict(edge_id=eid, **{'from': a, 'to': b}, enabled=True,
            path=[Ta[:3,3].tolist(), Tb[:3,3].tolist()], length=length, cost=length,
            units=graph['units'], observation_only=length < step*.08,
            provenance=dict(kind='automatic_recorded_traversal', frame_names=[a,b],
                traversal_id=f'traversal:{traversal}',
                pts_interval_s=[ra['pts_time_s'],rb['pts_time_s']],
                source_sha256=frames.get('probe', {}).get('source_sha256'),
                source_kind=frames.get('source_kind', 'original_video_kind_unspecified'),
                reverse_visual_support=True))
        sharpness=min((r.get('quality') or {}).get('laplacian_variance',100.) for r in (ra,rb))
        edge['observation_quality']=float(np.clip(sharpness/100.,0,1))
        edge['navigation_cost']=length+step*.05*(1-edge['observation_quality'])
        graph['edges'][eid] = edge
        if declaration.get('reverse_allowed') is True and declaration.get('reversible_plane_confirmed') is True:
            graph['edges'][eid+':reverse'] = {**deepcopy(edge), 'edge_id':eid+':reverse',
                'from':b, 'to':a, 'path':list(reversed(edge['path'])),
                'provenance':{**edge['provenance'], 'kind':'declared_reversible_traversal',
                              'reverse_visual_support':False}}
    # Coarse visual waypoints retain all turns/endpoints. Edges still retain the
    # complete reference polyline; no shortcut is introduced by simplification.
    distance = 0.
    for i, name in enumerate(names):
        if name not in nodes:
            continue
        keep = i == 0 or i == len(names)-1
        if i and names[i-1] in nodes:
            distance += np.linalg.norm(position(nodes[name]['T_MC'])-position(nodes[names[i-1]]['T_MC']))
        if 0 < i < len(names)-1 and names[i-1] in nodes and names[i+1] in nodes:
            u = position(nodes[name]['T_MC'])-position(nodes[names[i-1]]['T_MC'])
            v = position(nodes[names[i+1]]['T_MC'])-position(nodes[name]['T_MC'])
            if min(np.linalg.norm(u),np.linalg.norm(v)) > step*.08:
                keep |= float(np.dot(unit(u),unit(v))) < math.cos(math.radians(25))
        else:
            keep = True
        if keep or distance >= 4*step:
            graph['navigation_nodes'].append(name); distance = 0.
    graph['index_sha256'] = digest_json(graph)
    return graph


def associate_route(graph, observation, previous=None):
    """Associate a measured pose, not an unconditional nearest-node snap."""
    if (not observation.get('evidence_verified') or observation.get('pose_status') != 'localized'
            or any(observation.get(k) != graph[k] for k in ('map_hash','component_id'))):
        return {'status':'localization_required', 'candidates':[]}
    refs = set(observation.get('matched_references', []))
    if not refs:
        return {'status':'reference_evidence_missing', 'candidates':[]}
    pose = _rigid(observation['T_WC_query']); p = pose[:3,3]
    step = graph['typical_step']; candidates = []
    for eid, edge in graph['edges'].items():
        if not edge['enabled'] or edge.get('provenance',{}).get('kind')=='automatic_repeat_place':
            continue
        a, b = np.asarray(edge['path'], float)
        v = b-a; size = float(v@v)
        t = float(np.clip((p-a)@v/max(size,1e-12),0,1))
        q = a+t*v; d = float(np.linalg.norm(p-q))
        support = sum(n in refs or len(refs.intersection(
            graph['nodes'][n].get('localization_references',[]))) >= 2 for n in (edge['from'],edge['to']))
        heading = max(float(pose[:3,2] @ _rigid(graph['nodes'][n]['T_MC'])[:3,2]) for n in (edge['from'],edge['to']))
        if d > 2*step or not support or heading < -.5:
            continue
        score = d/step + .2*(1-heading) + .15*(2-support)
        if previous and previous.get('status') == 'associated':
            old = graph['edges'][previous['edge_id']]
            if eid == previous['edge_id'] or edge['from'] == old['to']:
                score -= .15
        candidates.append(dict(edge_id=eid, fraction=t, projection=q.tolist(), distance=d,
                               score=score, reference_support=support, heading_agreement=heading))
    candidates.sort(key=lambda x:x['score'])
    if not candidates:
        return {'status':'off_route', 'candidates':[]}
    best = candidates[0]
    for other in candidates[1:]:
        e1, e2 = (graph['edges'][c['edge_id']] for c in (best,other))
        adjacent = bool({e1['from'],e1['to']} & {e2['from'],e2['to']})
        if not adjacent:
            from .junctions import edges_meet_at_junction
            adjacent=edges_meet_at_junction(graph,e1,e2,p)
        if not adjacent and other['score']-best['score'] < .2:
            return {'status':'ambiguous_branch', 'candidates':candidates[:3]}
    return {**best, 'status':'associated', 'candidates':candidates[:3],
            'local_approach':'unverified_camera_route_association_only'}


def plan_guidance(graph, association, goals, closed=()):
    result = dict(status='no_path', path=[], route_edge_ids=[], execution_permitted=False,
                  units=graph['units'], route_kind='camera_route', goal_semantics='target_observation_pose')
    if association.get('status') != 'associated':
        return {**result, 'status':'start_ambiguous_or_off_route'}
    eid = association['edge_id']; edge = graph['edges'][eid]
    if eid in closed:
        return {**result, 'reason':'current_segment_closed'}
    search_graph={**graph,'edges':{k:{**v,'length':v.get('navigation_cost',v['length'])}
                                  for k,v in graph['edges'].items()}}
    distance, previous = _dijkstra(search_graph, edge['to'], set(closed))
    eligible = [g for g in goals if g['node_id'] in distance and g.get('quality',0) >= .5]
    if not eligible:
        return {**result, 'reason':'no_reachable_good_observation'}
    # Filter first, then optimize both route length and observation quality.
    goal = min(eligible, key=lambda g:distance[g['node_id']] + 3*graph['typical_step']*(1-g['quality']))
    n = goal['node_id']; edges = []
    while n != edge['to']:
        prior, peid = previous[n]; edges.append(peid); n = prior
    edges.reverse()
    path = [association['projection'], edge['path'][-1]]
    for peid in edges:
        path.extend(graph['edges'][peid]['path'][1:])
    steps=[dict(edge_id=k,**{'from':graph['edges'][k]['from'],'to':graph['edges'][k]['to']},
                provenance=deepcopy(graph['edges'][k]['provenance'])) for k in [eid]+edges]
    transitions=[s for s in steps if s['provenance']['kind']=='automatic_repeat_place']
    return {**result, 'status':'planned', 'path':path, 'route_edge_ids':[eid]+edges,
            'index_sha256':graph.get('index_sha256'),'route_steps':steps,
            'recombined':bool(transitions),'junction_transitions':transitions,
            'route_node_ids':[edge['to']]+[graph['edges'][x]['to'] for x in edges],
            'goal_node':goal['node_id'], 'goal_quality':goal['quality'],
            'goal_observation_pose':graph['nodes'][goal['node_id']]['T_MC'],
            'length':sum(float(np.linalg.norm(np.subtract(b,a))) for a,b in zip(path,path[1:]))}


class NavigationTask:
    """Only actual, newer observation records advance a task. No timer animation."""
    def __init__(self, graph, goals, *, up, max_age_s=3., no_progress_s=20., lookahead_steps=3.):
        self.graph, self.goals, self.up = graph, goals, unit(up)
        if not 1 <= float(lookahead_steps) <= 10:
            raise ValueError('lookahead_steps_out_of_range')
        self.lookahead_steps=float(lookahead_steps)
        self.max_age_s, self.no_progress_s = max_age_s, no_progress_s
        self.state = 'locating'; self.guidance = None; self.plan = None
        self.last = None; self.association = None; self.closed = set()
        self.best_remaining = math.inf; self.progress_time = None
        self.arrival_sequence = None; self.reason = None
        self.stable_association=None; self.recovery_candidate=None

    def pause(self):
        self.state = 'paused'; self.guidance = None

    def cancel(self):
        self.state = 'cancelled'; self.guidance = None; self.plan = None

    def resume(self):
        self.state = 'locating'; self.guidance = None; self.progress_time = None

    def tick(self, now):
        if self.state in ('paused','cancelled','failed','target_confirmed'):
            return
        if self.last and self.last['time_mode'] == 'live_current' and now-self.last['received_monotonic'] > self.max_age_s:
            self.state = 'relocalizing'; self.guidance = None; self.reason = 'frame_stale_or_disconnected'
            self.plan = None

    def current_pose_available(self, now):
        """A verified fresh camera pose is not contingent on finding a route."""
        if self.state in ('locating','paused','cancelled','failed') or not self.last:
            return False
        if self.reason not in (None, 'off_route', 'ambiguous_branch',
                'branch_change_needs_second_observation', 'no_reachable_good_observation',
                'no_observed_progress', 'current_segment_closed', 'heading_undefined'):
            return False
        o = self.last
        if (o.get('pose_status') != 'localized' or o.get('evidence_verified') is not True
                or any(o.get(k) != self.graph[k] for k in ('map_hash','component_id'))):
            return False
        if o.get('time_mode') not in ('live_current', 'offline_replay'):
            return False
        try:
            _rigid(o['T_WC_query'])
            return o['time_mode'] != 'live_current' or 0 <= now-float(o['received_monotonic']) <= self.max_age_s
        except (ValueError, TypeError, KeyError):
            return False

    def close_edge(self, edge_id):
        if edge_id not in self.graph['edges']:
            raise ValueError('unknown_edge')
        self.closed.add(edge_id); self.guidance = None; self.plan = None; self.state = 'blocked'

    def observe(self, observation, *, now):
        if self.state in ('paused','cancelled','failed','target_confirmed'):
            return
        if self.last:
            if any(observation.get(k) != self.last.get(k) for k in ('session_id','camera_id','time_mode')):
                self.state = 'relocalizing'; self.guidance = None; self.plan = None; self.reason = 'source_changed_restart_required'; return
            if observation['sequence'] <= self.last['sequence']:
                return
        if observation['time_mode'] == 'live_current' and not 0 <= now-observation['received_monotonic'] <= self.max_age_s:
            self.state='relocalizing'; self.guidance=None; self.plan=None; self.reason='stale_result'; return
        if self.last and observation['query_sha256'] == self.last['query_sha256']:
            self.state='relocalizing'; self.guidance=None; self.plan=None; self.reason='unchanged_pixels_static_or_frozen'; return
        self.last = deepcopy(observation)
        a = associate_route(self.graph, observation, self.association)
        self.association = a
        if a['status'] != 'associated':
            self.state='relocalizing'; self.guidance=None; self.plan=None; self.reason=a['status']; return
        # A distant route-branch change needs another measured observation,
        # even if one globally retrieved PnP hypothesis is internally strong.
        if self.stable_association:
            old=self.graph['edges'][self.stable_association['edge_id']]
            new=self.graph['edges'][a['edge_id']]
            distance=float(np.linalg.norm(np.subtract(a['projection'],self.stable_association['projection'])))
            if not {old['from'],old['to']} & {new['from'],new['to']} and distance>6*self.graph['typical_step']:
                consistent=self.recovery_candidate and np.linalg.norm(np.subtract(
                    a['projection'],self.recovery_candidate['projection']))<4*self.graph['typical_step']
                if not consistent:
                    self.recovery_candidate=a
                    self.state='relocalizing';self.plan=None;self.guidance=None;self.reason='branch_change_needs_second_observation';return
        self.recovery_candidate=None;self.stable_association=a
        self.plan = plan_guidance(self.graph, a, self.goals, self.closed)
        if self.plan['status'] != 'planned':
            self.state='blocked'; self.guidance=None; self.reason=self.plan.get('reason'); return
        self.reason=None
        remaining = self.plan['length']; step = self.graph['typical_step']
        image_time = float(observation['image_time_s'])
        if remaining < self.best_remaining-step*.15 or self.progress_time is None:
            self.best_remaining=remaining; self.progress_time=image_time
        elif image_time-self.progress_time > self.no_progress_s:
            self.closed.add(a['edge_id'])
            self.plan=plan_guidance(self.graph,a,self.goals,self.closed)
            self.state='blocked'; self.guidance=None; self.reason='no_observed_progress'; return
        goal_distance = float(np.linalg.norm(position(observation['T_WC_query'])-position(self.plan['goal_observation_pose'])))
        # A region spans one measured reference sampling interval, not a
        # fabricated metric radius or an exact historic camera pixel pose.
        goal_heading=float(_rigid(observation['T_WC_query'])[:3,2] @ _rigid(self.plan['goal_observation_pose'])[:3,2])
        if remaining <= step and goal_distance <= step and goal_heading >= .5:
            self.state='observation_region_reached'; self.guidance=None
            if self.arrival_sequence is None:
                self.arrival_sequence=observation['sequence']
            return
        self.arrival_sequence=None
        path = np.asarray(self.plan['path']); waypoint = 1
        accumulated = 0.
        for i in range(1,len(path)-1):
            accumulated += np.linalg.norm(path[i]-path[i-1]); waypoint=i
            # Repeated observations at a fused junction can have identical
            # centres. Look through zero-length switches, not through turns.
            before=next((j for j in range(i-1,-1,-1) if np.linalg.norm(path[i]-path[j])>1e-9),None)
            after=next((j for j in range(i+1,len(path)) if np.linalg.norm(path[j]-path[i])>1e-9),None)
            u=path[i]-path[before] if before is not None else np.zeros(3)
            v=path[after]-path[i] if after is not None else np.zeros(3)
            if np.linalg.norm(path[i]-position(observation['T_WC_query']))<step*.02:
                waypoint=i+1
                continue
            if accumulated >= self.lookahead_steps*step or (min(np.linalg.norm(u),np.linalg.norm(v)) > 1e-9
                    and unit(u)@unit(v) < math.cos(math.radians(25))):
                break
            waypoint=i+1
        T = _rigid(observation['T_WC_query']); forward=T[:3,2]
        delta=path[waypoint]-T[:3,3]
        forward=forward-self.up*(forward@self.up); delta=delta-self.up*(delta@self.up)
        if min(np.linalg.norm(forward),np.linalg.norm(delta)) < 1e-8:
            self.state='relocalizing'; self.guidance=None; self.reason='heading_undefined'; return
        forward, delta = unit(forward),unit(delta)
        angle=math.atan2(float(self.up@np.cross(forward,delta)),float(forward@delta))
        direction='左转' if angle > .25 else '右转' if angle < -.25 else '向前'
        self.state='turning' if abs(angle) > .4 else 'following'; self.reason=None
        self.guidance=dict(direction=direction, yaw_error_degrees=math.degrees(angle),
            waypoint=path[waypoint].tolist(), reference=self.plan['route_node_ids'][waypoint-1],
            remaining=remaining, units=self.graph['units'], image_sequence=observation['sequence'],
            execution_permitted=False, mode='shadow_guidance')

    def confirm(self, *, now):
        self.tick(now)
        if (self.state != 'observation_region_reached' or self.last is None
                or self.arrival_sequence is None or self.last['sequence'] <= self.arrival_sequence
                or self.last.get('target_matches',0) < 3):
            return False
        self.state='target_confirmed'; self.guidance=None; self.reason=None
        return True

    def snapshot(self):
        return dict(state=self.state, reason=self.reason, guidance=self.guidance, plan=self.plan,
            association=self.association, closed_edges=sorted(self.closed),
            sequence=self.last.get('sequence') if self.last else None,
            execution_permitted=False, mode='shadow_guidance')
