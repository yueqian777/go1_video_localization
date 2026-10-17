"""Read-only geometry audit of hash-bound saved localization and route evidence.

This reports a rejected connection; it never snaps the pose, adds a route edge,
changes a threshold, connects a camera, or supplies an execution instruction.
"""
import argparse
import json
from pathlib import Path
import sys
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from go1loc.evidence import verify_map, resolve_map_path, digest_json
from go1loc.jobs import read_json, atomic_json
from go1loc.localization import _pose_matrix
from go1loc.navigation import associate_route
from go1loc.navigation_localizer import verified_localization


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--map',type=Path,required=True)
    p.add_argument('--task',type=Path,required=True)
    p.add_argument('--replay',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    from go1loc.events import outside_artifacts
    outside_artifacts(a.output.resolve(),a.map.resolve())
    if a.output.exists():raise ValueError('use_new_output')
    import pycolmap
    manifest=verify_map(a.map)
    graph=read_json(a.task/'navigation_index.json')
    if graph['map_hash']!=manifest['manifest_sha256'] or graph['index_sha256']!=digest_json(
            {k:v for k,v in graph.items() if k!='index_sha256'}):
        raise ValueError('map_or_graph_binding_mismatch')
    model=pycolmap.Reconstruction(resolve_map_path(a.map,manifest['model_dir']))
    nodes=[]
    for name,node in graph['nodes'].items():
        im=model.find_image_with_name(name)
        if im is not None:
            nodes.append(float(np.abs(np.asarray(node['T_MC'])-_pose_matrix(im.cam_from_world())).max()))
    endpoint_errors=[float(np.linalg.norm(np.asarray(e['path'][i])-
        np.asarray(graph['nodes'][e[n]]['T_MC'])[:3,3]))
        for e in graph['edges'].values() for i,n in [(0,'from'),(-1,'to')]]
    rows=[]
    for line in (a.replay/'results.jsonl').read_text('utf8').splitlines():
        row=json.loads(line)
        if row['task']!=a.task.name:continue
        observation,evidence=verified_localization(Path(row['output']),graph['map_hash'])
        if observation['query_sha256']!=row['input_sha256']:raise ValueError('replay_input_mismatch')
        association=associate_route(graph,observation)
        if observation['pose_status']!='localized':
            rows.append(dict(event_index=row['event_index'],status=observation['pose_status']));continue
        T=np.asarray(observation['T_WC_query']);p0=T[:3,3];refs=set(observation['matched_references'])
        distances=[]
        for eid,e in graph['edges'].items():
            if not e['enabled'] or e['provenance']['kind']=='automatic_repeat_place':continue
            start,end=np.asarray(e['path']);delta=end-start
            t=np.clip((p0-start)@delta/max(delta@delta,1e-12),0,1);q=start+t*delta
            support=sum(n in refs or len(refs.intersection(graph['nodes'][n].get('localization_references',[])))>=2
                        for n in (e['from'],e['to']))
            distances.append(dict(edge_id=eid,reference_support=support,distance=float(np.linalg.norm(p0-q)),
                                  projection=q.tolist(),endpoints=[e['from'],e['to']]))
        distances.sort(key=lambda d:d['distance']);nearest=distances[0]
        supported=next((d for d in distances if d['reference_support']),None)
        displacement=p0-np.asarray(nearest['projection']);up=np.asarray(graph['up_estimate']);up/=np.linalg.norm(up)
        good=observation['inlier_rows'];camera=observation['quality']['query_camera']
        cam=pycolmap.Camera(**camera);unique={}
        for r in sorted(good,key=lambda r:r['reprojection_px']):unique.setdefault(r['query_keypoint_index'],r)
        selected=list(unique.values());xyz=np.asarray([r['xyz'] for r in selected]);uv=np.asarray([r['uv_colmap'] for r in selected])
        # Counterfactual diagnostic ONLY: same intrinsics/rotation, camera center
        # moved to the nearest historical edge. This is NOT a refined pose.
        errors=np.linalg.norm(cam.img_from_cam((xyz-np.asarray(nearest['projection']))@T[:3,:3])-uv,axis=1)
        rows.append(dict(event_index=row['event_index'],query_sha256=observation['query_sha256'],
            association=association,nearest=nearest,nearest_visually_supported=supported,
            matched_references=observation['matched_references'],camera_position=p0.tolist(),
            height_component=abs(float(displacement@up)),
            horizontal_component=float(np.linalg.norm(displacement-(displacement@up)*up)),
            actual_median_reprojection_px=float(np.median([r['reprojection_px'] for r in selected])),
            counterfactual_snap_fixed_rotation_intrinsics_median_px=float(np.median(errors)),
            counterfactual_snap_not_refined=True))
    if not rows:raise ValueError('no_rows_for_task')
    atomic_json(a.output,dict(map_hash=manifest['manifest_sha256'],index_sha256=graph['index_sha256'],
        native_pose_max_abs_difference=max(nodes),edge_endpoint_max_difference=max(endpoint_errors),
        units=graph['units'],entry_limit=2*graph['typical_step'],tracking_limit=2.5*graph['typical_step'],
        rows=rows,execution_permitted=False,scope='offline_geometry_diagnosis_not_pose_ground_truth'))
    print('wrote',a.output,'rows',len(rows))


if __name__=='__main__':main()
