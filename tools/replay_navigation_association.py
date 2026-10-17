"""Read-only, ordered association regression on saved, hash-verified queries.

Legacy logs did not retain capture clocks. Their event-completion spacing is
used ONLY as an offline regression clock, not as a reconstructed exposure time
or a new live-freshness test. Previously stale records remain rejected.
"""
import argparse
from collections import Counter
from datetime import datetime
from pathlib import Path

from go1loc.jobs import atomic_json, read_json
from go1loc.evidence import digest_json
from go1loc.navigation import NavigationTask, plan_guidance
from go1loc.navigation_localizer import verified_localization


def replay(run, output):
    run=Path(run);output=Path(output);output.mkdir(parents=True,exist_ok=False)
    events=read_json(run/'guidance_log.json');graph=read_json(run/'navigation_index.json')
    if digest_json({k:v for k,v in graph.items() if k!='index_sha256'})!=graph.get('index_sha256'):
        raise ValueError('navigation_index_digest_mismatch')
    goals=read_json(run/'goals.json');task=NavigationTask(graph,goals,up=graph['up_estimate'])
    origin=datetime.fromisoformat(events[0]['at']).timestamp();rows=[]
    for i,event in enumerate(events):
        if event.get('error') or not event.get('query_directory'):
            task.clear_tracking();rows.append({'index':i,'reason':'recorded_error','guidance':None});continue
        obs,_=verified_localization(event['query_directory'],graph['map_hash'])
        metadata=event.get('observation_metadata') or {}
        has_clock=metadata.get('image_time_s') is not None
        stamp=metadata['image_time_s'] if has_clock else datetime.fromisoformat(event['at']).timestamp()-origin
        if event.get('reason')=='stale_result':
            task.clear_tracking();rows.append({'index':i,'reason':'recorded_stale_result','guidance':None});continue
        obs.update(sequence=i+1,session_id='offline-association-regression',camera_id='recorded-camera',
            time_mode='offline_replay',image_time_s=stamp,received_monotonic=stamp)
        task.observe(obs,now=stamp)
        rows.append({'index':i,**task.snapshot(),'clock_source':
            'recorded_observation_clock' if has_clock else 'legacy_completion_spacing_not_capture_time'})
    good=[r for r in rows if r.get('guidance')]
    closure=None
    if good:
        r=good[0];path=r['plan']['route_edge_ids']
        edge=next((e for e in path if e.startswith('gap:')),path[len(path)//2])
        closure={'closed_edge':edge,'result':plan_guidance(graph,r['association'],goals,{edge})}
    summary=dict(event_count=len(events),original_guidance_count=sum(bool(e.get('guidance')) for e in events),
        replay_guidance_count=len(good),original_reasons=dict(Counter(e['reason'] or e['state'] for e in events)),
        replay_reasons=dict(Counter(r['reason'] or r['state'] for r in rows)),
        bounded_tracking_count=sum((r.get('association') or {}).get('association_mode')=='bounded_tracking' for r in rows),
        automatic_closed_edges=sorted(task.closed),map_hash=graph['map_hash'],
        mode='offline_association_regression_not_new_localization_or_live_accuracy',
        uses_legacy_completion_spacing=any(not e.get('observation_metadata') for e in events),
        execution_permitted=False,closure_check=closure)
    atomic_json(output/'replay.json',rows);atomic_json(output/'summary.json',summary)
    print({k:v for k,v in summary.items() if k!='closure_check'})
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();replay(args.run,args.output)
