"""Verified disk and localhost observation inputs for shadow guidance."""
import http.cookiejar
import hashlib
import json
import math
from pathlib import Path
import time
import urllib.request
from uuid import uuid4
import numpy as np

from .evidence import digest_json, verify_binding
from .jobs import atomic_json, read_json, utc_now
from .navigation import build_navigation_index
from .route_workflow import route_inputs
from .video import sha256_file


def prepare_index(map_dir, sidecar, declaration, *, publish=True):
    from .events import outside_artifacts
    sidecar=outside_artifacts(Path(sidecar).resolve(),Path(map_dir).resolve())
    manifest,refs,frames,model=route_inputs(map_dir,with_model=True)
    if not frames:
        raise ValueError('continuous_video_metadata_required')
    graph=build_navigation_index(manifest,refs,frames,declaration)
    from .junctions import fuse_junctions, native_junction_observations
    graph=fuse_junctions(graph,native_junction_observations(model,graph))
    from .navigation_gap import load_gap_recoveries
    graph=load_gap_recoveries(graph,manifest,frames,sidecar)
    from .navigation_trace import load_route_trace
    graph=load_route_trace(graph,sidecar)
    if publish:
        save_navigation_index(sidecar,map_dir,graph)
    return graph


def save_navigation_index(sidecar,map_dir,graph):
    """Retain old revisions; only the latest sidecar pointer is replaced."""
    from .events import outside_artifacts
    sidecar=outside_artifacts(Path(sidecar).resolve(),Path(map_dir).resolve())
    latest=sidecar/'navigation_index.json'
    versions=[read_json(latest),graph] if latest.is_file() else [graph]
    for version in versions:
        digest=version.get('index_sha256')
        if digest_json({k:v for k,v in version.items() if k!='index_sha256'})!=digest:
            raise ValueError('navigation_index_hash_mismatch')
        archived=sidecar/'indexes'/(digest+'.json')
        if archived.exists():
            if read_json(archived)!=version:
                raise ValueError('navigation_index_archive_conflict')
        else:
            atomic_json(archived,version)
    atomic_json(latest,graph)


def observation_goals(model, graph, target):
    """Score actual support tracks/pixel spread, NOT 3-D box volume."""
    if target.get('status')!='supported' or any(target.get(k)!=graph[k] for k in ('map_hash','component_id')):
        raise ValueError('supported_target_in_same_map_required')
    directory=Path(target['query_dir']); evidence=read_json(directory/'query_evidence.json')
    if digest_json({k:v for k,v in evidence.items() if k!='evidence_sha256'})!=evidence.get('evidence_sha256'):
        raise ValueError('target_evidence_hash_mismatch')
    verify_binding(evidence['binding'],Path(target['query_path']),graph['map_hash'])
    binding=target.get('roi_binding',{})
    if digest_json(binding)!=target.get('roi_sha256') or binding.get('evidence_sha256')!=evidence['evidence_sha256']:
        raise ValueError('target_roi_binding_mismatch')
    # Preserve supported masks; never silently ignore a mask during validation.
    from .photo_object import ground_evidence
    mask=None
    if binding.get('mask_sha256'):
        from PIL import Image
        file=Path(target.get('mask_path') or '')
        if not file.is_file() or sha256_file(file)!=binding['mask_sha256']:
            raise ValueError('旧目标缺少可核验的原掩膜文件，请用原掩膜重新计算目标，旧结果不会覆盖。')
        with Image.open(file) as im:
            if list(im.size)!=evidence['binding']['geometry']['source_size']:
                raise ValueError('target_mask_size_mismatch')
            mask=np.asarray(im.convert('L'))>0
    checked=ground_evidence(evidence,binding['roi'],mask=mask,cluster_radius=target['cluster_radius'])
    if (checked['status']!='supported' or set(checked['supporting_point3D_ids'])!=set(target['supporting_point3D_ids'])):
        raise ValueError('target_support_evidence_mismatch')
    ids=set(target['supporting_point3D_ids']); candidates=[]
    for im in model.images.values():
        if im.name not in graph['nodes']:
            continue
        camera=model.cameras[im.camera_id]
        uv=np.asarray([p.xy for p in im.points2D if p.has_point3D() and int(p.point3D_id) in ids])
        if len(uv)<3:
            continue
        spread=float(np.linalg.norm(np.ptp(uv,axis=0)/[camera.width,camera.height]))
        if spread<.007:
            continue
        score=.6*min(1.,len(uv)/max(3,len(ids)))+.4*min(1.,spread/.1)
        candidates.append(dict(node_id=im.name,quality=score,support_count=len(uv),
            target_query_sha256=evidence['binding']['query_sha256'],
            projected_support_span=spread, semantic='visible_surface_observation_not_object_size'))
    if not candidates:
        raise ValueError('target_has_no_sufficient_reference_observation')
    best=max(c['quality'] for c in candidates)
    return [c for c in candidates if c['quality']>=max(.5,best*.8)]


def count_target_matches(observation, target):
    ids=set(target['supporting_point3D_ids'])
    # Count distinct image features (one feature may have several 3-D candidates).
    return len({r['query_keypoint_index'] for r in observation.get('inlier_rows',[])
                if r['point3D_id'] in ids and r['pnp_inlier'] and r['positive_depth']
                and r['reprojection_px'] is not None and r['reprojection_px']<=4})


def reference_pixel_hashes(manifest):
    """PNG re-encoding changes file hashes; capture stores a BGR pixel hash."""
    from PIL import Image
    hashes=set()
    for name,expected in manifest.get('reference_image_sha256',{}).items():
        path=Path(manifest['image_dir'])/name
        if sha256_file(path)!=expected:
            raise ValueError('reference_image_changed')
        with Image.open(path) as image:
            hashes.add(hashlib.sha256(np.asarray(image.convert('RGB'))[:,:,::-1].tobytes()).hexdigest())
    return hashes


class CaptureReplay:
    def __init__(self, directory, *, interval_s=1., map_image_hashes=(), map_pixel_hashes=(), strict_independent=True):
        from .web_capture import capture_metadata
        self.directory=Path(directory).resolve()
        checked,meta=capture_metadata(self.directory.name)
        if checked!=self.directory:
            raise ValueError('replay_outside_capture_catalog')
        if meta.get('source_kind')=='synthetic_test':
            raise ValueError('synthetic_capture_not_real_navigation_input')
        rows=[json.loads(s) for s in (self.directory/'frames.jsonl').read_text('utf-8').splitlines() if s.strip()]
        self.rows=[]; last=-float('inf'); seq=-1
        forbidden=set(map_image_hashes)
        forbidden_pixels=set(map_pixel_hashes)
        for row in rows:
            stamp=float(row['agent_read_monotonic'])
            camera=meta.get('source',{}).get('camera_id')
            if (not math.isfinite(stamp) or stamp<=last or row['agent_seq']<=seq
                    or row['session_id']!=meta['stream_session_id']
                    or (camera is not None and row.get('camera_id')!=camera)):
                raise ValueError('replay_nonmonotonic_or_mixed_source')
            last=stamp;seq=row['agent_seq']
            if strict_independent and (row['image_sha256'] in forbidden or row.get('raw_payload_sha256') in forbidden_pixels):
                raise ValueError('该录像包含建图原帧（包括重新编码的同像素帧），请选另一采集会话。')
            if not self.rows or stamp-self.rows[-1]['agent_read_monotonic']>=interval_s:
                self.rows.append(row)
        if not self.rows:
            raise ValueError('empty_replay')
        self.index=0;self.session=uuid4().hex;self.started_at=utc_now()
        self.reference_time=self.rows[0]['agent_read_monotonic']

    def next(self):
        if self.index>=len(self.rows):
            return None
        row=self.rows[self.index]; self.index+=1
        file=(self.directory/row['image_path']).resolve()
        if file.parent!=self.directory/'frames' or file.suffix!='.png' or sha256_file(file)!=row['image_sha256']:
            raise ValueError('replay_image_path_or_hash_invalid')
        from PIL import Image
        with Image.open(file) as image:
            pixels=hashlib.sha256(np.asarray(image.convert('RGB'))[:,:,::-1].tobytes()).hexdigest()
        if pixels!=row.get('raw_payload_sha256'):
            raise ValueError('replay_decoded_pixels_hash_mismatch')
        return file,dict(sequence=self.index,session_id=self.session,camera_id='camera'+str(row['camera_id']),
            query_sha256=row['image_sha256'],time_mode='offline_replay',
            image_time_s=row['agent_read_monotonic'],sensor_capture_time=row.get('sensor_capture_time'),
            source_read_time=row['agent_read_monotonic'],recorded_receive_time=row.get('pc_receive_time'),
            replay_reference_time=self.reference_time, replay_started_at=self.started_at,
            received_monotonic=time.monotonic(),runtime_at=utc_now(),source_recording=str(self.directory.name),
            time_source='recorded_agent_read_not_sensor_exposure',execution_permitted=False)


class LocalCaptureReader:
    """GET-only subscription to an already-running PC capture app. No SSH/start."""
    def __init__(self, output):
        self.origin='http://127.0.0.1:8772';self.output=Path(output)
        self.output.mkdir(parents=True,exist_ok=True)
        self.opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.key=None;self.sequence=0
        self._get('/api/v1/session',{'X-Go1-Session':'renew'})

    def _get(self,path,headers=None):
        request=urllib.request.Request(self.origin+path,headers=headers or {},method='GET')
        with self.opener.open(request,timeout=1.) as response:
            data=response.read(32*1024*1024+1)
            if len(data)>32*1024*1024:
                raise ValueError('capture_response_too_large')
            return data,response.headers

    def next(self):
        raw,_=self._get('/api/v1/state');state=json.loads(raw)['data']
        if state.get('mode')!='robot':
            raise ValueError('synthetic_preview_not_live_robot_input')
        if not state.get('live'):
            raise ValueError('capture_disconnected_or_content_unchanged')
        data,headers=self._get('/api/v1/frame.png');key=headers.get('X-Go1-Frame-Key')
        if not key or key==self.key or not data:
            return None
        if key.split(':')[0]!=(state.get('frame_key') or '').split(':')[0]:
            raise ValueError('camera_session_changed_during_read')
        from .viewer_text import save_uploaded_image
        path=save_uploaded_image(self.output,'current.jpg',data)
        self.key=key;self.sequence+=1;now=time.monotonic()
        return path,dict(sequence=self.sequence,session_id=key.split(':')[0],camera_id='camera'+str(state['camera']),
            source_frame_key=key,time_mode='live_current',image_time_s=now,received_monotonic=now,
            runtime_at=utc_now(),sensor_capture_time=None,source_read_time=None,
            time_source='PC_preview_delivery_not_sensor_exposure',sensor_freshness='unverified',
            query_sha256=sha256_file(path),execution_permitted=False)
