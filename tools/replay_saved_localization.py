"""Offline-only regression of saved task observations; never opens capture.

cached_matches isolates PnP/reference selection. saved_images uses ONE resident
SuperPoint/LightGlue backend and re-extracts the original PNGs. Neither mode is
a live-navigation acceptance test, and neither rewrites original observations.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import json
import importlib.metadata
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))

from go1loc.evidence import verify_map, verify_binding, resolve_map_path, digest_json
from go1loc.jobs import read_json, atomic_json, utc_now
from go1loc.localization import LocalizationConfig, localize_photo, REFERENCE_CONSENSUS_POLICY
from go1loc.navigation import NavigationTask, associate_route, plan_guidance
from go1loc.navigation_localizer import ResidentLocalizer, verified_localization
from go1loc.video import sha256_file


class SavedMatchBackend:
    """Only copies hash-verified algorithm inputs, not any saved pose result."""
    def __init__(self, map_dir, manifest):
        import pycolmap
        self.manifest = manifest
        self.reconstruction = pycolmap.Reconstruction(resolve_map_path(map_dir, manifest['model_dir']))

    def verified_manifest(self, map_dir):
        return self.manifest

    def extract(self, conf, image_dir, output, *, image_list, feature_path, overwrite):
        import h5py
        source = self.directory/'query-local.h5'
        with h5py.File(source,'r') as f:
            if set(image_list) != set(f):
                raise ValueError('saved_internal_query_key_mismatch')
        shutil.copy2(source, feature_path)

    def match(self, conf, pairs, *, features, features_ref, matches, overwrite):
        original = (self.directory/'pairs-query.txt').read_text(encoding='utf-8').splitlines()
        if set(pairs.read_text(encoding='utf-8').splitlines()) != set(original):
            raise ValueError('saved_reference_pairs_mismatch')
        shutil.copy2(self.directory/'matches-query.h5', matches)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--map', type=Path, required=True)
    parser.add_argument('--task', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=['cached_matches','saved_images'], default='cached_matches')
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    from go1loc.events import outside_artifacts
    outside_artifacts(args.output.resolve(), args.map.resolve())
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = verify_map(args.map)
    config = LocalizationConfig()
    backend = (ResidentLocalizer(args.map) if args.mode=='saved_images'
               else SavedMatchBackend(args.map, manifest))
    warmup = backend.warmup(args.output/'warmup', config) if args.mode=='saved_images' else None
    atomic_json(args.output/'parameters.json', dict(mode=args.mode, map_hash=manifest['manifest_sha256'],
        config=asdict(config),consensus_policy=REFERENCE_CONSENSUS_POLICY,warmup=warmup,
        source_sha256={name:sha256_file(ROOT/name) for name in (
            'src/go1loc/localization.py','src/go1loc/navigation_localizer.py',
            'src/go1loc/navigation.py','tools/replay_saved_localization.py')},
        versions={name:importlib.metadata.version(name) for name in ('pycolmap','torch','h5py','numpy')},
        runtime_at=utc_now(),time_mode='offline_replay',execution_permitted=False))
    rows=[]
    for task in args.task:
        graph,goals = read_json(task/'navigation_index.json'),read_json(task/'goals.json')
        if digest_json({k:v for k,v in graph.items() if k!='index_sha256'}) != graph['index_sha256']:
            raise ValueError('saved_graph_hash_mismatch')
        if graph['map_hash'] != manifest['manifest_sha256']:
            raise ValueError('saved_task_map_mismatch')
        if args.mode=='saved_images': backend.last_references=[]
        events=[json.loads(s) for s in (task/'guidance_events.jsonl').read_text('utf-8').splitlines() if s.strip()]
        events=[e for e in events if e.get('query_directory')]
        if args.limit is not None: events=events[:args.limit]
        if not events: raise ValueError('no_saved_observations')
        previous=None
        navigation=NavigationTask(graph,goals,up=graph['up_estimate'])
        for event_index,event in enumerate(events,1):
            old_dir=Path(event['query_directory']);old=read_json(old_dir/'localization_result.json')
            evidence=read_json(old_dir/'query_evidence.json');photo=Path(evidence['query_path'])
            if (digest_json(old)!=evidence['localization_result_sha256'] or
                digest_json({k:v for k,v in evidence.items() if k!='evidence_sha256'})!=evidence['evidence_sha256']):
                raise ValueError('saved_evidence_tampered')
            verify_binding(evidence['binding'],photo,manifest['manifest_sha256'])
            intrinsics=evidence['binding']['geometry'].get('intrinsics_request',{})
            if intrinsics.get('calibration_sha256') or evidence['binding']['geometry'].get('screen_observation'):
                raise ValueError('this_replay_requires_native_unrectified_inputs')
            config.query_camera_model=intrinsics.get('model');config.query_camera_params=intrinsics.get('params')
            config.capture_time=old.get('capture_time')
            # Task state.sequence can remain at the last accepted observation
            # when new frames are stale. It is NOT a unique event/file ID.
            output=args.output/task.name/f"{event_index:06d}"
            import pycolmap
            pycolmap.set_random_seed(0)
            started=time.perf_counter()
            if args.mode=='saved_images':
                value,new_evidence,directory=backend.localize(photo,output,config)
            else:
                backend.directory=old_dir
                refs=[s.split()[1] for s in (old_dir/'pairs-query.txt').read_text('utf-8').splitlines()]
                localize_photo(photo,args.map,output,config,resident=backend,reference_names=refs)
                value,new_evidence=verified_localization(output,manifest['manifest_sha256']);directory=output
            elapsed=time.perf_counter()-started
            metadata=event.get('observation_metadata',{})
            observation={**value,**metadata,'time_mode':'offline_replay','execution_permitted':False}
            association=associate_route(graph,observation,previous)
            if association['status']=='associated':previous=association
            plan=plan_guidance(graph,association,goals)
            # Recorded receive-clock spacing is retained; unknown exposure time
            # stays unknown. This checks only offline state/route behavior.
            if 'image_time_s' in metadata:
                navigation.observe(observation,now=metadata['image_time_s'])
                shadow=navigation.snapshot()
            else:
                shadow=None
            row=dict(task=task.name,event_index=event_index,
                sequence=metadata.get('sequence'),state_sequence=event.get('sequence'),input_sha256=sha256_file(photo),
                original_query_directory=str(old_dir),output=str(directory),
                source_observation_metadata=metadata,runtime_at=utc_now(),time_mode='offline_replay',
                sensor_capture_time=metadata.get('sensor_capture_time'),
                before=dict(status=old['pose_status'],quality=old['quality'],state=event.get('state'),reason=event.get('reason')),
                after=dict(status=value['pose_status'],reason=value.get('failure_reason'),quality=value['quality'],
                           T_WC_query=value.get('T_WC_query'),matched_references=value['matched_references']),
                processing_s=elapsed,processing_stages=getattr(backend,'last_profile',None),
                association=association,plan=plan,shadow=shadow,execution_permitted=False)
            rows.append(row)
            with (args.output/'results.jsonl').open('a',encoding='utf-8') as f:
                f.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
            print(task.name,event_index,old['pose_status'],'->',value['pose_status'],
                  value['quality'].get('reference_strategy'),association['status'],round(elapsed,3),flush=True)
    import numpy as np
    summary=dict(mode=args.mode,scope='saved_observation_reprocessing_not_new_live_acceptance',
        execution_permitted=False,map_hash=verify_map(args.map)['manifest_sha256'],
        model_loads=getattr(backend,'model_loads',0),warmup=warmup,groups={})
    for task in args.task:
        group=[r for r in rows if r['task']==task.name];durations=[r['processing_s'] for r in group]
        summary['groups'][task.name]=dict(count=len(group),
            before=dict(Counter(r['before']['status'] for r in group)),
            after=dict(Counter(r['after']['status'] for r in group)),
            association=dict(Counter(r['association']['status'] for r in group)),
            plan=dict(Counter(r['plan']['status'] for r in group)),
            offline_directions=dict(Counter(r['shadow']['guidance']['direction'] for r in group
                                            if r.get('shadow') and r['shadow'].get('guidance'))),
            processing_s=dict(p50=float(np.percentile(durations,50)),p95=float(np.percentile(durations,95)),max=max(durations)))
    atomic_json(args.output/'summary.json',summary);print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
