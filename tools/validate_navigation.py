"""Real stored observations; no robot access and no synthetic pose substitution."""
import argparse
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from go1loc.jobs import atomic_json, read_json
from go1loc.navigation import NavigationTask
from go1loc.navigation_io import CaptureReplay, prepare_index, observation_goals, count_target_matches, reference_pixel_hashes
from go1loc.navigation_localizer import ResidentLocalizer


def main():
    parser=argparse.ArgumentParser()
    for name in ('map','target','capture','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--limit',type=int,default=12)
    parser.add_argument('--interval',type=float,default=3.)
    parser.add_argument('--declared-robot-traversal',action='store_true',required=True)
    args=parser.parse_args()
    if args.output.exists():
        raise ValueError('use_new_output')
    args.output.mkdir(parents=True)
    backend=ResidentLocalizer(args.map)
    graph=prepare_index(args.map,args.output,dict(map_hash=backend.manifest['manifest_sha256'],
        robot_traversed=True,unhandled_unedited=True,reverse_allowed=False,
        evidence='explicit_operator_declaration'))
    target=read_json(args.target)
    goals=observation_goals(backend.reconstruction,graph,target)
    atomic_json(args.output/'observation_goals.json',goals)
    replay=CaptureReplay(args.capture,interval_s=args.interval,
        map_image_hashes=backend.manifest['reference_image_sha256'].values(),
        map_pixel_hashes=reference_pixel_hashes(backend.manifest))
    task=NavigationTask(graph,goals,up=graph['up_estimate'])
    records=[]
    for i in range(args.limit):
        item=replay.next()
        if item is None:
            break
        photo,meta=item;start=time.monotonic()
        observation,_,directory=backend.localize(photo,args.output/f'query-{i:03}')
        observation.update(meta)
        observation['target_matches']=count_target_matches(observation,target)
        task.observe(observation,now=time.monotonic())
        task.confirm(now=time.monotonic())
        snapshot=task.snapshot()
        row=dict(index=i,source=photo.name,pose_status=observation['pose_status'],
            inliers=observation.get('quality',{}).get('num_inliers'),
            latency_s=time.monotonic()-start,target_matches=observation['target_matches'],
            **snapshot,query_directory=str(directory))
        records.append(row)
        atomic_json(args.output/'results.json',dict(source_kind='independent_real_capture_replay',
            source_recording=args.capture.name,topology_edges=len(graph['edges']),
            exceptions=graph['exceptions'],goals=goals,records=records,
            model_loads=backend.model_loads,execution_permitted=False,
            actual_robot_execution=False, actual_live_guidance=False))
        print(i,photo.name,observation['pose_status'],snapshot['state'],snapshot['reason'],
              round(row['latency_s'],2),flush=True)


if __name__=='__main__':
    main()
