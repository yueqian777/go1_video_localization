"""Read-only native-map audit of route recombination. Never connects to Go1."""
import argparse
from pathlib import Path
from time import perf_counter

from go1loc.evidence import verify_map
from go1loc.events import outside_artifacts
from go1loc.jobs import atomic_json, read_json
from go1loc.junctions import fuse_junctions, native_junction_observations
from go1loc.navigation import build_navigation_index, plan_guidance
from go1loc.navigation_io import observation_goals, save_navigation_index
from go1loc.route_workflow import route_inputs


def audit(map_dir, setup, output, target=None):
    output=outside_artifacts(Path(output).resolve(),Path(map_dir).resolve())
    if output.exists():
        raise ValueError('output_exists_use_new_directory')
    started=perf_counter()
    m,refs,frames,model=route_inputs(map_dir,with_model=True)
    outside_artifacts(output,Path(m['image_dir']))
    graph=build_navigation_index(m,refs,frames,read_json(Path(setup)))
    fused=fuse_junctions(graph,native_junction_observations(model,graph))
    summary=dict(kind='native_map_historical_node_routing_not_independent_navigation_acceptance',
        map_hash=m['manifest_sha256'],registered_references=len(graph['nodes']),
        temporal_edges=len(graph['edges']),candidate_pairs=len(fused['junction_candidates']),
        adopted_junctions=len(fused['junctions']),new_reachable_pairs=0,shorter_pairs=0,
        moving_junctions=sum(j['repeat_kind']=='traversal_junction' for j in fused['junctions'].values()),
        stationary_repeat_connections=sum(j['repeat_kind']=='stationary_observation' for j in fused['junctions'].values()),
        execution_permitted=False)
    novel=None;shorter=None
    for eid,edge in graph['edges'].items():
        start=dict(status='associated',edge_id=eid,fraction=0.,projection=edge['path'][0])
        for node in sorted(graph['nodes']):
            if node==edge['from']:
                continue  # Staying where we already are is not a new route.
            goal=[dict(node_id=node,quality=1.)]
            old=plan_guidance(graph,start,goal);new=plan_guidance(fused,start,goal)
            if new['status']!='planned' or not new.get('recombined'):
                continue
            record=dict(historical_start=edge['from'],observation_goal=node,before=old,after=new)
            if old['status']!='planned':
                summary['new_reachable_pairs']+=1
                novel=novel or record
            elif new['length']<old['length']-1e-8:
                summary['shorter_pairs']+=1
                if shorter is None or old['length']-new['length']>shorter['before']['length']-shorter['after']['length']:
                    shorter=record
    example=novel or shorter
    if example:
        start_edge=example['after']['route_edge_ids'][0]
        edge=graph['edges'][start_edge]
        start=dict(status='associated',edge_id=start_edge,fraction=0.,projection=edge['path'][0])
        closed=[k for j in fused['junctions'].values() for k in j['edge_ids']]
        example['junctions_disabled']=plan_guidance(fused,start,[dict(node_id=example['observation_goal'],quality=1.)],closed)
        atomic_json(output/'recombination_example.json',example)
    if target:
        goals=observation_goals(model,fused,read_json(Path(target)))
        plans=[]
        for eid,edge in graph['edges'].items():
            plan=plan_guidance(fused,dict(status='associated',edge_id=eid,fraction=0.,projection=edge['path'][0]),goals)
            if plan['status']=='planned':
                plans.append(plan)
        summary['target_observation_goals']=len(goals)
        summary['target_historical_starts_with_path']=len(plans)
        summary['target_paths_using_junction']=sum(p['recombined'] for p in plans)
    save_navigation_index(output/'index',map_dir,fused)
    atomic_json(output/'temporal_index.json',graph)
    summary['frozen_map_unchanged']=verify_map(Path(map_dir))['manifest_sha256']==m['manifest_sha256']
    summary['elapsed_s']=perf_counter()-started
    atomic_json(output/'summary.json',summary)
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--map',type=Path,required=True)
    parser.add_argument('--setup',type=Path,required=True,help='Existing map-bound navigation/setup.json')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--target',type=Path)
    args=parser.parse_args()
    print(audit(args.map,args.setup,args.output,args.target))
