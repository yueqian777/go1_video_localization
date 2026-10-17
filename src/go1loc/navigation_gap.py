"""Bounded, evidence-bound resampling of an isolated historical video gap.

Never register a missing image by interpolation or alter the frozen model.
Only additional, actually localized frames create sidecar camera-route nodes.
This is recorded-history preview, not a free-space or robot-clearance proof.
"""
from copy import deepcopy
from pathlib import Path
import math
import tempfile
import numpy as np

from .evidence import digest_json
from .jobs import read_json
from .planning import _rigid
from .video import sha256_file


def _verify_dense_source(source_path, directory, dense, sidecar):
    """Re-decode bounded native PTS and pixels; a JSON claim is not provenance."""
    from .video import prepare_video, FrameConfig
    config=FrameConfig(**dense['signature']['config'])
    if not 0<config.duration_s<=2 or not 3<=config.max_frames<=120:
        raise ValueError('gap_decode_budget_exceeded')
    Path(sidecar).mkdir(parents=True,exist_ok=True)
    # The temporary directory is beside the sidecar (D: in this installation),
    # never in the immutable map or the default system temporary directory.
    with tempfile.TemporaryDirectory(prefix='verify-gap-',dir=sidecar) as temp:
        output=Path(temp)/'frames'
        actual=prepare_video(Path(source_path),output,config)
        if actual['frames']!=dense['frames']:
            raise ValueError('gap_decoded_timeline_mismatch')
        for row in actual['frames']:
            if sha256_file(output/row['image_path'])!=sha256_file(directory/row['image_path']):
                raise ValueError('gap_decoded_pixels_mismatch')


def add_verified_gap(graph, samples, *, repair_id, provenance):
    """Pure geometry stage; disk caller must first verify ALL query bindings."""
    if len(samples)<3 or samples[0]['name'] not in graph['nodes'] or samples[-1]['name'] not in graph['nodes']:
        raise ValueError('gap_needs_native_anchors_and_additional_observations')
    for row in (samples[0],samples[-1]):
        if not np.allclose(_rigid(row['T_MC']),_rigid(graph['nodes'][row['name']]['T_MC']),atol=1e-8,rtol=0):
            raise ValueError('gap_anchor_pose_mismatch')
    if not 0 < samples[-1]['pts_time_s']-samples[0]['pts_time_s'] <= 2:
        raise ValueError('gap_interval_too_large')
    for row in samples[1:-1]:
        if row['name'] in graph['nodes'] or not row.get('evidence_sha256') or len(row.get('references',[]))<2:
            raise ValueError('gap_additional_localization_evidence_required')
        _rigid(row['T_MC'])
    for a,b in zip(samples,samples[1:]):
        if not 0 < b['pts_time_s']-a['pts_time_s'] <= .5:
            raise ValueError('gap_unobserved_interval_over_half_second')
        A,B=_rigid(a['T_MC']),_rigid(b['T_MC'])
        angle=math.degrees(math.acos(float(np.clip((np.trace(A[:3,:3].T@B[:3,:3])-1)/2,-1,1))))
        if np.linalg.norm(A[:3,3]-B[:3,3]) > 8*graph['typical_step'] or angle>120:
            raise ValueError('gap_pose_discontinuity')
    g=deepcopy(graph)
    for row in samples[1:-1]:
        g['nodes'][row['name']]=dict(node_id=row['name'],frame_id=row['name'],image_id=None,
            component_id=g['component_id'],T_MC=row['T_MC'],pts_time_s=row['pts_time_s'],
            image_path=row.get('image_path'),native_registered=False,
            geometry_source='verified_historical_frame_relocalization',
            localization_references=row['references'],evidence_sha256=row['evidence_sha256'])
        g['navigation_nodes'].append(row['name'])
    for i,(a,b) in enumerate(zip(samples,samples[1:])):
        eid=f'gap:{repair_id}:{i}'
        path=[_rigid(r['T_MC'])[:3,3].tolist() for r in (a,b)]
        length=float(np.linalg.norm(np.subtract(path[1],path[0])))
        g['edges'][eid]=dict(edge_id=eid,**{'from':a['name'],'to':b['name']},path=path,
            enabled=True,length=length,cost=length,navigation_cost=length,units=g['units'],
            provenance=dict(kind='verified_dense_video_relocalization',repair_id=repair_id,
                frame_names=[a['name'],b['name']],pts_interval_s=[a['pts_time_s'],b['pts_time_s']],
                **provenance))
    for exception in g['exceptions']:
        if exception['from_frame']==samples[0]['name'] or exception['to_frame']==samples[-1]['name']:
            exception['resolved_by']=repair_id
    g.setdefault('gap_recoveries',[]).append(dict(repair_id=repair_id,**provenance))
    g.pop('index_sha256',None);g['index_sha256']=digest_json(g)
    return g


def load_gap_recoveries(graph, manifest, frames, sidecar):
    """No poses are read from repair JSON: derive them from verified PnP outputs."""
    from .navigation_localizer import verified_localization
    path=Path(sidecar)/'gap_recoveries.json'
    if not path.is_file():
        return graph
    config=read_json(path)
    if config.get('map_hash')!=graph['map_hash']:
        raise ValueError('gap_recovery_map_mismatch')
    source=frames['probe']
    if sha256_file(Path(source['source_path']))!=source['source_sha256']:
        raise ValueError('gap_original_video_changed')
    names=[Path(r.get('image_path') or '').name for r in frames['frames']]
    result=graph
    for patch in config.get('repairs',[]):
        start,end=patch['from_frame'],patch['to_frame']
        ia,ib=names.index(start),names.index(end)
        # Only a single missing sampled frame, not an arbitrary route shortcut.
        if ib!=ia+2 or names[ia+1] in result['nodes']:
            raise ValueError('gap_must_be_single_missing_sample')
        if any(r.get('continuity_flags') or r.get('discontinuity_before') or r.get('discontinuity_after')
               or (r.get('quality') or {}).get('rejection_reasons')
               for r in frames['frames'][ia:ib+1]):
            raise ValueError('gap_original_continuity_or_quality_exception')
        directory=Path(patch['prepared_dir']);dense=read_json(directory/'frames_manifest.json')
        if (dense['probe']['source_sha256']!=source['source_sha256']
                or dense['signature']['video_sha256']!=source['source_sha256']
                or dense['signature'].get('calibration_sha256') is not None
                or dense['geometry'].get('rotation_degrees',0)!=0
                or dense['geometry'].get('status')!='uncalibrated'):
            raise ValueError('gap_source_or_geometry_mismatch')
        rows=dense['frames']
        if len(rows)<3 or len(rows)>120:
            raise ValueError('gap_dense_sample_budget')
        for actual,expected in zip((rows[0],rows[-1]),(frames['frames'][ia],frames['frames'][ib])):
            if any(actual[k]!=expected[k] for k in ('frame_index','pts_time_s','size_wh')):
                raise ValueError('gap_anchor_time_mismatch')
        _verify_dense_source(source['source_path'],directory,dense,sidecar)
        # Dense decoder must include every source frame and preserve its clock.
        for a,b in zip(rows,rows[1:]):
            if b['frame_index']!=a['frame_index']+1 or not 0<b['pts_time_s']-a['pts_time_s']<=.2:
                raise ValueError('gap_dense_decode_incomplete')
            if ((b.get('quality') or {}).get('mean_change_64px') or 0)>80:
                raise ValueError('gap_possible_visual_cut')
        samples=[];rejected=[];evidence_hashes=[]
        for i,row in enumerate(rows):
            name=Path(row['image_path']).name;photo=directory/row['image_path']
            if i in (0,len(rows)-1):
                if name not in (start,end) or sha256_file(photo)!=manifest['reference_image_sha256'][name]:
                    raise ValueError('gap_anchor_pixels_mismatch')
                samples.append(dict(name=name,pts_time_s=row['pts_time_s'],T_MC=result['nodes'][name]['T_MC']))
                continue
            query=patch['queries'].get(str(row['frame_index']))
            if not query:
                raise ValueError('gap_query_evidence_missing')
            value,evidence=verified_localization(query,graph['map_hash'])
            if (sha256_file(photo)!=sha256_file(Path(evidence['query_path']))
                    or value.get('map_hash')!=graph['map_hash']):
                raise ValueError('gap_query_photo_or_map_mismatch')
            evidence_hashes.append(evidence['evidence_sha256'])
            q=value['quality']
            good=(value['pose_status']=='localized' and value.get('component_id')==graph['component_id']
                  and q.get('unique_inlier_keypoints',0)>=30 and q.get('inlier_ratio',0)>=.25
                  and q.get('inlier_4x4_grid_coverage',0)>=.25
                  and q.get('p95_inlier_reprojection_px',float('inf'))<=4)
            if not good:
                rejected.append(dict(frame=name,pts_time_s=row['pts_time_s'],status=value['pose_status'],
                    reason='historical_recovery_quality_gate_not_met',quality=q))
                continue
            samples.append(dict(name=name,pts_time_s=row['pts_time_s'],T_MC=value['T_WC_query'],
                image_path=str(photo.resolve()),references=value['matched_references'],
                evidence_sha256=evidence['evidence_sha256']))
        provenance=dict(source_sha256=source['source_sha256'],source_kind=frames.get('source_kind','video'),
            time_source=frames.get('timing_source'),rejected_samples=rejected,
            query_evidence_sha256=evidence_hashes,prepared_manifest_sha256=sha256_file(directory/'frames_manifest.json'),
            frozen_map_unchanged=True,execution_permitted=False)
        repair_id=digest_json(dict(start=start,end=end,**provenance))[:16]
        result=add_verified_gap(result,samples,repair_id=repair_id,provenance=provenance)
    return result
