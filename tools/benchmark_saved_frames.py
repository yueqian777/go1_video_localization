"""Repeat selected saved observations, without capture or motion connections.

Restores the exact incoming local reference list from a previous saved-image
run. Re-extracts/matches/solves every observation, verifies source evidence and
compares ALL candidate evidence, not just the winning pose. No timing gates
are changed. Use separate processes/output directories for before and after.
"""
import argparse
import cProfile
from dataclasses import asdict
import importlib.metadata
import json
from pathlib import Path
import pstats
import subprocess
import sys
import time
import types
from contextlib import nullcontext
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from go1loc.evidence import digest_json, verify_map
from go1loc.jobs import atomic_json, read_json, utc_now
from go1loc.localization import LocalizationConfig, REFERENCE_CONSENSUS_POLICY
from go1loc.navigation_localizer import ResidentLocalizer, verified_localization
from go1loc.video import sha256_file


def signature(value, evidence):
    return digest_json(dict(status=value['pose_status'], reason=value.get('failure_reason'),
        pose=value.get('T_WC_query'), quality=value['quality'],
        references=value['matched_references'], selected=evidence['selected_cluster'],
        clusters=evidence['clusters']))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--map', type=Path, required=True)
    p.add_argument('--baseline', type=Path, required=True, help='Saved-image results.jsonl')
    p.add_argument('--task', required=True)
    p.add_argument('--events', type=int, nargs='+', required=True)
    p.add_argument('--repeat', type=int, default=3)
    p.add_argument('--compare-ref', help='Trusted git revision: alternate before/after in one process and share ONE model pair')
    p.add_argument('--profile', action='store_true', help='Diagnostic only, not wall-time acceptance')
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    if args.repeat < 1: p.error('--repeat must be positive')
    from go1loc.events import outside_artifacts
    outside_artifacts(args.output.resolve(), args.map.resolve())
    manifest = verify_map(args.map)
    sources = [json.loads(s) for s in args.baseline.read_text('utf-8').splitlines() if s.strip()]
    sources = [r for r in sources if r['task']==args.task and r['event_index'] in args.events]
    if sorted(r['event_index'] for r in sources) != sorted(set(args.events)):
        raise ValueError('requested_events_missing_or_duplicated')
    prepared = []
    for row in sources:
        old = Path(row['output'])
        value, evidence = verified_localization(old, manifest['manifest_sha256'])
        geom = evidence['binding']['geometry']
        request = geom.get('intrinsics_request', {})
        if request.get('calibration_sha256') or geom.get('screen_observation'):
            raise ValueError('benchmark_requires_native_unrectified_inputs')
        config = LocalizationConfig(query_camera_model=request.get('model'),
            query_camera_params=request.get('params'), capture_time=value.get('capture_time'))
        local = old.parent/'local'
        if old.name=='global' and (local/'pairs-query.txt').exists():
            verified_localization(local, manifest['manifest_sha256'])
            refs = [s.split()[1] for s in (local/'pairs-query.txt').read_text('utf-8').splitlines()]
        else:
            refs = [s.split()[1] for s in (old/'pairs-query.txt').read_text('utf-8').splitlines()] if old.name=='local' else []
        prepared.append((row, evidence, config, refs, signature(value, evidence)))
    args.output.mkdir(parents=True, exist_ok=False)
    backend = ResidentLocalizer(args.map)
    warmup = backend.warmup(args.output/'warmup', prepared[0][2])
    variants = [('current', backend, {})]
    if args.compare_ref:
        modules={}
        for name in ('evidence','localization','navigation_localizer'):
            key='go1loc.'+name
            module=types.ModuleType(key);module.__package__='go1loc'
            module.__file__=str(ROOT/'src/go1loc'/(name+'.py'))
            code=subprocess.check_output(['git','show',f'{args.compare_ref}:src/go1loc/{name}.py'],cwd=ROOT).decode('utf-8')
            modules[key]=module
            with patch.dict(sys.modules,modules):exec(compile(code,module.__file__,'exec'),module.__dict__)
        cls=modules['go1loc.navigation_localizer'].ResidentLocalizer
        baseline_backend=object.__new__(cls)
        baseline_backend.__dict__.update(backend.__dict__)
        baseline_backend._cluster_cache=dict(backend._cluster_cache)
        variants.insert(0,('baseline',baseline_backend,modules))
    atomic_json(args.output/'parameters.json', dict(runtime_at=utc_now(),
        git_head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        baseline_sha256=sha256_file(args.baseline), map_hash=manifest['manifest_sha256'],
        execution_permitted=False, time_mode='offline_replay', profile=args.profile,
        config=asdict(prepared[0][2]), consensus_policy=REFERENCE_CONSENSUS_POLICY,
        warmup=warmup, model_loads=backend.model_loads, compare_ref=args.compare_ref,
        versions={n:importlib.metadata.version(n) for n in ('torch','pycolmap','numpy','h5py')},
        source_sha256={n:sha256_file(ROOT/n) for n in ('src/go1loc/evidence.py',
            'src/go1loc/localization.py','src/go1loc/navigation_localizer.py',__file__)},
        inputs=[dict(event=r['event_index'], query_sha256=e['binding']['query_sha256'],
                     incoming_references=refs, signature=sig) for r,e,c,refs,sig in prepared]))
    import numpy as np
    import pycolmap
    rows = []
    for repeat in range(1,args.repeat+1):
        for old,evidence,config,refs,expected in prepared:
            for label,engine,modules in (variants if repeat%2 else variants[::-1]):
                engine.last_references = list(refs)
                pycolmap.set_random_seed(0)
                name=f"repeat-{repeat}-event-{old['event_index']}-{label}"
                prof=cProfile.Profile()
                with patch.dict(sys.modules,modules) if modules else nullcontext():
                    if args.profile: prof.enable()
                    started=time.perf_counter()
                    value,ev,directory=engine.localize(Path(evidence['query_path']),args.output/name,config)
                    elapsed=time.perf_counter()-started
                    if args.profile:
                        prof.disable()
                        prof.dump_stats(str(args.output/(name+'.prof')))
                        with (args.output/(name+'.txt')).open('w',encoding='utf-8') as f:
                            pstats.Stats(prof,stream=f).strip_dirs().sort_stats('cumulative').print_stats(100)
                actual=signature(value,ev)
                row=dict(repeat=repeat,event=old['event_index'],variant=label,elapsed_s=elapsed,
                    exact_candidate_evidence=actual==expected,signature=actual,
                    stages=engine.last_profile,output=str(directory),execution_permitted=False)
                rows.append(row)
                with (args.output/'results.jsonl').open('a',encoding='utf-8') as f:
                    f.write(json.dumps(row,ensure_ascii=False)+'\n')
                print(name,round(elapsed,4),'exact=',actual==expected,flush=True)
    summary=dict(scope='saved_real_observations_not_new_live_validation',
        all_exact=all(r['exact_candidate_evidence'] for r in rows),events={},
        profile=args.profile,execution_permitted=False)
    for event in args.events:
        summary['events'][event]={}
        for label,_,_ in variants:
            durations=[r['elapsed_s'] for r in rows if r['event']==event and r['variant']==label]
            summary['events'][event][label]=dict(count=len(durations),min=min(durations),
                p50=float(np.median(durations)),max=max(durations),over_3s=sum(t>3 for t in durations))
    verify_map(args.map)
    atomic_json(args.output/'summary.json',summary)
    print(json.dumps(summary,indent=2))
    if not summary['all_exact']: raise SystemExit('candidate_evidence_changed')


if __name__=='__main__': main()
