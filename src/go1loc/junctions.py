"""Repeat-place connections for shadow routing, not free-space traversability.

Keep original camera nodes and temporal edges. A junction is an explicit,
auditable pair of observations of the same small place, not a spatial snap or
union-find cluster whose transitive closure can silently span a wall.
"""
from copy import deepcopy
import math

import numpy as np

from .evidence import digest_json
from .planning import _rigid, _dijkstra

POLICY = dict(version='repeat-place-v1', radius_steps=.35, height_steps=.10,
              min_shared_tracks=15, min_shared_fraction=.2, min_context_tracks=8,
              max_reprojection_px=3., min_span=.08, min_grid_cells=4,
              max_camera_up_degrees=20., nearby_temporal_hops=3)


def _position(graph, name):
    return np.asarray(graph['nodes'][name]['T_MC'], float)[:3, 3]


def _valid_points(record):
    result={}
    for pid, point in (record or {}).get('points',{}).items():
        try:
            uv=np.asarray(point['uv'],float)
            error=float(point['error_px']);depth=float(point['depth'])
            if (uv.shape==(2,) and np.isfinite(uv).all() and (uv>=0).all() and (uv<=1).all()
                    and math.isfinite(error) and 0<=error<=POLICY['max_reprojection_px']
                    and math.isfinite(depth) and depth>0):
                result[str(pid)]=point
        except (KeyError,TypeError,ValueError):
            continue
    return result


def _binding_ok(record, graph, name):
    try:
        return (record['map_hash']==graph['map_hash'] and record['component_id']==graph['component_id']
                and np.allclose(_rigid(record['T_MC']),_rigid(graph['nodes'][name]['T_MC']),atol=1e-8,rtol=0))
    except (KeyError,ValueError,TypeError):
        return False


def _coverage(points, ids):
    if not ids:
        return {'span':[0.,0.], 'grid_cells':0}
    uv=np.asarray([points[x]['uv'] for x in sorted(ids)],float)
    cells=np.clip((uv*4).astype(int),0,3)
    return dict(span=np.ptp(uv,axis=0).tolist(),grid_cells=len({tuple(v) for v in cells}))


def fuse_junctions(graph, observations):
    """Pure deterministic augmentation; callers supply verified native evidence.

    Spatial proximity only nominates a pair. Adoption also needs map/pose
    binding, common low-error 3-D tracks over a broad image area, compatible
    camera heights/up, and corroboration in temporal neighbors on BOTH runs.
    Output is explicitly an automatic camera-route hypothesis, never a robot
    clearance certificate. Separate maps/coordinate systems are not aligned here.
    """
    g=deepcopy(graph)
    g['edges']={k:v for k,v in g['edges'].items() if v.get('provenance',{}).get('kind')!='automatic_repeat_place'}
    g['junctions']={};g['junction_candidates']=[]
    g['schema_version']='go1loc.navigation_index.v2'
    g['topology_policy']='temporal_traversals_with_evidence_bound_repeat_places_not_free_space'
    g['junction_policy']=deepcopy(POLICY)
    g['junction_evidence_sha256']=digest_json(observations)
    temporal_graph={**g,'edges':deepcopy(g['edges'])}
    neighbors={n:set() for n in g['nodes']}
    lengths={n:[] for n in g['nodes']}
    for e in g['edges'].values():
        if not e['enabled']:
            continue
        a,b=e['from'],e['to']
        neighbors[a].add(b);neighbors[b].add(a)
        if e['length']>1e-8:
            lengths[a].append(e['length']);lengths[b].append(e['length'])
    spacing={n:min(g['typical_step'],float(np.median(v))) if v else g['typical_step'] for n,v in lengths.items()}
    valid={n:_valid_points(r) if _binding_ok(r,g,n) else {} for n,r in observations.items() if n in g['nodes']}
    up=np.asarray(g['up_estimate'],float)
    up/=np.linalg.norm(up)
    # Spatial hashing avoids all-pairs scanning of large maps. Thresholds are
    # scale-relative, not fabricated metres or robot footprint estimates.
    radius=POLICY['radius_steps']*g['typical_step']
    buckets={};pairs=[]
    for name in sorted(g['nodes']):
        if not neighbors[name]:
            continue
        cell=tuple(np.floor(_position(g,name)/radius).astype(int))
        for dx in (-1,0,1):
            for dy in (-1,0,1):
                for dz in (-1,0,1):
                    pairs.extend((other,name) for other in buckets.get((cell[0]+dx,cell[1]+dy,cell[2]+dz),[]))
        buckets.setdefault(cell,[]).append(name)
    near_temporal={}
    for name in neighbors:
        seen={name};front={name}
        for _ in range(POLICY['nearby_temporal_hops']):
            front={v for n in front for v in neighbors[n]}-seen;seen.update(front)
        near_temporal[name]=seen
    for a,b in sorted(pairs):
        if b in near_temporal[a]:
            continue
        pa,pb=_position(g,a),_position(g,b)
        scale=min(spacing[a],spacing[b]);distance=float(np.linalg.norm(pb-pa))
        if distance>POLICY['radius_steps']*scale:
            continue
        reasons=[]
        if abs(float((pb-pa)@up))>POLICY['height_steps']*scale:
            reasons.append('height_mismatch')
        Ta,Tb=(_rigid(g['nodes'][n]['T_MC']) for n in (a,b))
        if float(Ta[:3,1]@Tb[:3,1])<math.cos(math.radians(POLICY['max_camera_up_degrees'])):
            reasons.append('camera_up_mismatch')
        ca,cb=(g['nodes'][n].get('capture_source',{}).get('camera_id') for n in (a,b))
        if ca!=cb:
            reasons.append('physical_camera_changed')
        for n in (a,b):
            if not _binding_ok(observations.get(n,{}),g,n):
                reasons.append('missing_or_mismatched_native_evidence')
        va,vb=valid.get(a,{}),valid.get(b,{})
        shared=set(va)&set(vb)
        fraction=len(shared)/max(1,min(len(va),len(vb)))
        if len(shared)<POLICY['min_shared_tracks'] or fraction<POLICY['min_shared_fraction']:
            reasons.append('insufficient_shared_tracks')
        coverage=[_coverage(v,shared) for v in (va,vb)]
        if any(min(c['span'])<POLICY['min_span'] or c['grid_cells']<POLICY['min_grid_cells'] for c in coverage):
            reasons.append('shared_features_concentrated')
        contexts=[]
        for n in (a,b):
            witnesses=[dict(frame=k,shared_count=len(shared&set(valid.get(k,{}))))
                       for k in sorted(neighbors[n]) if len(shared&set(valid.get(k,{})))>=POLICY['min_context_tracks']]
            contexts.append(witnesses)
        if not all(contexts):
            reasons.append('temporal_context_missing')
        identity=dict(map_hash=g['map_hash'],component_id=g['component_id'],frames=[a,b])
        jid='j-'+digest_json(identity)[:16]
        candidate=dict(junction_id=jid,frame_names=[a,b],position=((pa+pb)/2).tolist(),
            distance=distance,radius=POLICY['radius_steps']*scale,shared_track_count=len(shared),
            shared_fraction=fraction,coverage=coverage,context=contexts,reasons=sorted(set(reasons)),
            status='rejected' if reasons else 'accepted',map_hash=g['map_hash'],component_id=g['component_id'],
            provenance='native_map_repeat_place_geometry',execution_permitted=False)
        g['junction_candidates'].append(candidate)
        if reasons:
            continue
        # Returning after travel differs from merely observing the same place
        # repeatedly while stationary. Retain that distinction in reports/UI.
        temporal_lengths=[]
        for src,dst in ((a,b),(b,a)):
            distances,_=_dijkstra(temporal_graph,src,set())
            if dst in distances:
                temporal_lengths.append(distances[dst])
        candidate['repeat_kind']=('stationary_observation' if temporal_lengths
            and min(temporal_lengths)<g['typical_step']*.5 else 'traversal_junction')
        candidate['recorded_between_length']=min(temporal_lengths) if temporal_lengths else None
        candidate['evidence_sha256']=digest_json(dict(identity=identity,policy=POLICY,
            observations={n:observations[n] for n in sorted({a,b}|neighbors[a]|neighbors[b]) if n in observations}))
        record=deepcopy(candidate);record['edge_ids']=[]
        for src,dst in ((a,b),(b,a)):
            eid=f'junction:{jid}:{len(record["edge_ids"])}'
            record['edge_ids'].append(eid)
            g['edges'][eid]=dict(edge_id=eid,**{'from':src,'to':dst},enabled=True,
                path=[_position(g,src).tolist(),_position(g,dst).tolist()],length=distance,
                navigation_cost=distance+scale*.15,cost=distance+scale*.15,units=g['units'],
                observation_only=True,execution_permitted=False,
                provenance=dict(kind='automatic_repeat_place',junction_id=jid,frame_names=[src,dst],
                    evidence_sha256=candidate['evidence_sha256'],map_hash=g['map_hash'],
                    component_id=g['component_id'],semantics='switch_recorded_traversal_at_same_place',
                    physical_clearance_verified=False))
        g['junctions'][jid]=record
    # Do not union positions or add an unverified A-C edge because A-B and B-C
    # passed. Every traversal change must use its own pair and evidence.
    g['navigation_nodes']=sorted(set(g['navigation_nodes'])|{n for j in g['junctions'].values() for n in j['frame_names']})
    g.pop('index_sha256',None)
    g['index_sha256']=digest_json(g)
    return g


def edges_meet_at_junction(graph, first, second, observation_position):
    """Disambiguate equivalent local arrivals only inside the accepted place."""
    for j in graph.get('junctions',{}).values():
        a,b=j['frame_names']
        if ((a in (first['from'],first['to']) and b in (second['from'],second['to'])) or
                (b in (first['from'],first['to']) and a in (second['from'],second['to']))):
            if np.linalg.norm(np.asarray(observation_position)-j['position'])<=j['radius']:
                return True
    return False


def native_junction_observations(model, graph):
    """Extract actual registered observations, including per-image reprojection.

    Model must have been opened through verify_map. The pure fusion layer also
    checks poses/map/component against the temporal graph. No image matching
    edge by itself becomes a route edge.
    """
    observations={}
    for image in model.images.values():
        if image.name not in graph['nodes']:
            continue
        camera=model.cameras[image.camera_id]
        T=np.eye(4);T[:3]=image.cam_from_world().matrix();points={}
        entries=[p for p in image.points2D if p.has_point3D() and int(p.point3D_id) in model.points3D]
        if entries:
            xyz=np.asarray([model.points3D[int(p.point3D_id)].xyz for p in entries])
            xyz_camera=xyz@T[:3,:3].T+T[:3,3]
            projected=camera.img_from_cam(xyz_camera)
            for p,pc,uv in zip(entries,xyz_camera,projected):
                point=model.points3D[int(p.point3D_id)]
                if len(point.track.elements)<3 or not np.isfinite(uv).all() or pc[2]<=0:
                    continue
                error=float(np.linalg.norm(np.asarray(p.xy)-uv))
                xy=(np.asarray(p.xy)/[camera.width,camera.height]).tolist()
                if math.isfinite(error):
                    points[str(int(p.point3D_id))]=dict(uv=xy,error_px=error,depth=float(pc[2]))
        observations[image.name]=dict(map_hash=graph['map_hash'],component_id=graph['component_id'],
            T_MC=np.linalg.inv(T).tolist(),points=points)
    return observations
