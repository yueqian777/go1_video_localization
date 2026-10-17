"""Resident hloc backend and latest-frame mailbox. No capture/control startup.

Uses the pinned hloc preprocessing, SuperPoint, LightGlue and PyCOLMAP evidence
export. Reference features and models stay resident; research CLI is unchanged.
"""
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import threading
import time
from uuid import uuid4
import numpy as np

from .evidence import verify_map, resolve_map_path, digest_json, verify_binding
from .jobs import read_json
from .video import sha256_file


class ResidentLocalizer:
    def __init__(self, map_dir):
        from .localization import _require_backend
        pycolmap, self.extract_module, _, self.match_module, _ = _require_backend()
        self.map_dir = Path(map_dir).resolve()
        self.manifest = verify_map(self.map_dir)
        self.reconstruction = pycolmap.Reconstruction(resolve_map_path(self.map_dir, self.manifest['model_dir']))
        paths = [self.map_dir/'map_manifest.json', resolve_map_path(self.map_dir, self.manifest['local_features'])]
        paths += [resolve_map_path(self.map_dir, self.manifest['model_dir'])/n for n in self.manifest['native_model_sha256']]
        self.stamps = {p:(p.stat().st_size,p.stat().st_mtime_ns) for p in paths}
        self.models = {}; self.reference_features = {}
        self.last_references = []
        self.model_loads = 0
        import torch
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        import h5py
        with h5py.File(resolve_map_path(self.map_dir, self.manifest['local_features']), 'r') as f:
            size = 0
            for im in self.reconstruction.images.values():
                data = {k:np.asarray(v) for k,v in f[im.name].items()}
                size += sum(v.nbytes for v in data.values())
                if size > 1024**3:
                    raise ValueError('resident_feature_cache_over_1GiB')
                self.reference_features[im.name] = data
        self._prime_covisibility()

    def verified_manifest(self, map_dir):
        if Path(map_dir).resolve() != self.map_dir:
            raise ValueError('resident_map_mismatch')
        if any((p.stat().st_size,p.stat().st_mtime_ns) != s for p,s in self.stamps.items()):
            raise ValueError('frozen_map_changed_restart_required')
        return self.manifest

    def warmup(self, output, config=None):
        """Load/run the SAME models using a reference, never a current frame.

        The warmup result is not an observation and cannot seed tracking.
        Subsequent tasks reuse this resident instance and its warmed models.
        """
        key=json.dumps(asdict(config) if config is not None else {},sort_keys=True,default=str)
        started=time.monotonic()
        cached=key in getattr(self,'_warmed_configs',set())
        if not cached:
            photo=Path(self.manifest['image_dir'])/next(iter(self.reference_features))
            try:
                self.localize(photo,Path(output),config)
            finally:
                self.last_references=[]
            self._warmed_configs=getattr(self,'_warmed_configs',set())|{key}
        return dict(ready=True,cached=cached,started_monotonic=started,
                    completed_monotonic=time.monotonic(),model_loads=self.model_loads,
                    input_kind='frozen_reference_warmup_not_current_observation')

    def _model(self, namespace, conf):
        from hloc.utils.base_model import dynamic_load
        key = namespace.__name__+json.dumps(conf,sort_keys=True)
        if key not in self.models:
            self.models[key] = dynamic_load(namespace,conf['name'])(conf).eval().to(self.device)
            self.model_loads += 1
        return self.models[key]

    @contextmanager
    def query_reuse(self):
        """Reuse exact algorithm inputs only within ONE local/global attempt.

        Never carry a current observation into the next frame, even on error.
        Raw extraction is cached before feature masks; matching keys include
        the final filtered arrays, not the basename or the source photo alone.
        """
        if getattr(self, '_query_reuse', None) is not None:
            raise RuntimeError('overlapping_resident_queries')
        self._query_reuse = {'features': {}, 'matches': {}}
        self._match_context = None
        self._match_prefetch = None
        self._initial_cameras = {}
        self.last_profile = dict(extract_s=0., match_s=0., extractions=0,
                                 extraction_reuses=0, match_pairs=0, match_reuses=0)
        try:
            yield
        finally:
            try:
                self._finish_match_prefetch(cancel=True)
            finally:
                self._query_reuse = None
                self._match_context = None
                self._initial_cameras = {}

    def _record_stage(self, stage, started, **counts):
        profile = getattr(self, 'last_profile', None)
        if profile is not None:
            profile[stage] = profile.get(stage, 0.) + time.perf_counter()-started
            for k, v in counts.items():
                profile[k] = profile.get(k, 0) + v

    def cluster_solver(self, localizer, query_name, features, read_matches):
        from .localization import _CachedClusterSolver
        return _CachedClusterSolver(localizer, query_name, features, read_matches)

    def infer_initial_camera(self, photo):
        """Reuse only the file-derived initial guess, never a PnP refinement."""
        import pycolmap
        started = time.perf_counter()
        cache = getattr(self,'_initial_cameras',{}) if getattr(self,'_query_reuse',None) is not None else {}
        key = sha256_file(photo)
        reused = key in cache
        if not reused: cache[key] = pycolmap.infer_camera_from_image(photo).todict()
        camera = pycolmap.Camera(deepcopy(cache[key]))
        self._record_stage('initial_camera_s',started,initial_camera_reuses=int(reused))
        return camera

    def _prime_covisibility(self):
        # Run once before taking current frames, like model warmup. Keep the
        # very same sets hloc constructs, without sorting/reordering their IDs.
        if not hasattr(self, '_covisibility_neighbors'):
            self._covisibility_neighbors = {
                image_id: {obs.image_id for point in image.points2D if point.has_point3D()
                           for obs in self.reconstruction.points3D[point.point3D_id].track.elements}
                for image_id, image in self.reconstruction.images.items()}

    def covisibility_clusters(self, image_ids):
        """Pinned hloc c13273b BFS, reusing only frozen-map adjacency sets.

        Preserve the upstream set operations, traversal and stable size sort:
        changing correspondence order would change the random PnP stream.
        Adapted from Apache-2.0 hloc; see THIRD_PARTY_NOTICES.md.
        """
        self._prime_covisibility()
        if not hasattr(self,'_cluster_cache'):self._cluster_cache={}
        key=tuple(image_ids)
        if key not in self._cluster_cache:
            if len(self._cluster_cache)>=32:self._cluster_cache.pop(next(iter(self._cluster_cache)))
            clusters, visited = [], set()
            selected = set(image_ids)
            for image_id in image_ids:
                if image_id in visited: continue
                clusters.append([])
                queue = {image_id}
                while queue:
                    current = queue.pop()
                    if current in visited: continue
                    visited.add(current)
                    clusters[-1].append(current)
                    connected = self._covisibility_neighbors[current] & selected
                    connected -= visited
                    queue |= connected
            self._cluster_cache[key]=sorted(clusters,key=len,reverse=True)
        return deepcopy(self._cluster_cache[key])

    def extract(self, conf, image_dir, export_dir, *, image_list, feature_path, overwrite):
        import torch, h5py
        from hloc import extractors
        self._finish_match_prefetch(cancel=False)
        model = self._model(extractors,conf['model'])
        dataset = self.extract_module.ImageDataset(image_dir,conf['preprocessing'],image_list)
        started = time.perf_counter(); computed = reused = 0
        cache = getattr(self, '_query_reuse', None)
        cache = cache['features'] if cache is not None else {}
        with torch.inference_mode(), h5py.File(feature_path,'w') as f:
            for i,name in enumerate(dataset.names):
                key = (sha256_file(Path(image_dir)/name), json.dumps(conf,sort_keys=True))
                if key in cache:
                    pred, uncertainty = cache[key]; reused += 1
                else:
                    data = dataset[i]
                    tensor = torch.from_numpy(data['image']).unsqueeze(0).to(self.device)
                    pred = {k:v[0].cpu().numpy() for k,v in model({'image':tensor}).items()}
                    pred['image_size'] = original = np.asarray(data['original_size'])
                    uncertainty = 1.
                    if 'keypoints' in pred:
                        scales = (original/np.asarray(tensor.shape[-2:][::-1])).astype(np.float32)
                        pred['keypoints'] = (pred['keypoints']+.5)*scales[None]-.5
                        if 'scales' in pred:
                            pred['scales'] *= scales.mean()
                        uncertainty = getattr(model,'detection_noise',1)*scales.mean()
                    cache[key] = (pred, uncertainty); computed += 1
                group = f.create_group(name)
                for k,v in pred.items():
                    group.create_dataset(k,data=v.astype(np.float16) if v.dtype == np.float32 else v)
                if 'keypoints' in group:
                    group['keypoints'].attrs['uncertainty'] = uncertainty
        self._record_stage('extract_s',started,extractions=computed,extraction_reuses=reused)
        return feature_path

    def match(self, conf, pairs, *, features, features_ref, matches, overwrite):
        import torch, h5py
        from hloc import matchers
        from hloc.utils.parsers import parse_retrieval, names_to_pair
        # Only one GPU model call at a time. Wait for the same-query speculative
        # matches before extraction/matching can use the model again.
        self._finish_match_prefetch(cancel=False)
        model = self._model(matchers,conf['model'])
        started = time.perf_counter(); computed = reused = 0
        cache = getattr(self, '_query_reuse', None)
        cache = cache['matches'] if cache is not None else {}
        def tensors(source, suffix):
            data = {k+suffix:torch.from_numpy(v.copy()).float().unsqueeze(0).to(self.device)
                    for k,v in source.items()}
            data['image'+suffix] = torch.empty((1,1,*tuple(source['image_size'])[::-1]))
            return data
        with torch.inference_mode(), h5py.File(features,'r') as qf, h5py.File(matches,'w') as mf:
            for q, refs in parse_retrieval(pairs).items():
                qdata = {k:np.asarray(v) for k,v in qf[q].items()}
                fingerprint = hashlib.sha256()
                for k,v in sorted(qdata.items()):
                    fingerprint.update(repr((k,v.dtype.str,v.shape)).encode())
                    fingerprint.update(v.tobytes())
                query_key = (fingerprint.hexdigest(),json.dumps(conf,sort_keys=True))
                query_tensors = tensors(qdata,'0')
                def predict(ref):
                    pred = model({**query_tensors,**tensors(self.reference_features[ref],'1')})
                    prediction = {'matches0':pred['matches0'][0].cpu().short().numpy()}
                    if 'matching_scores0' in pred:
                        prediction['matching_scores0'] = pred['matching_scores0'][0].cpu().half().numpy()
                    return prediction
                for ref in refs:
                    key = (query_key,ref)
                    if key in cache:
                        prediction = cache[key]; reused += 1
                    else:
                        prediction = predict(ref)
                        cache[key] = prediction; computed += 1
                    group = mf.create_group(names_to_pair(q,ref))
                    for k,v in prediction.items():group.create_dataset(k,data=v)
                if getattr(self, '_query_reuse', None) is not None:
                    self._match_context = (query_key, predict)
        self._record_stage('match_s',started,match_pairs=computed,match_reuses=reused)
        return matches

    def prefetch_global_matches(self, reference_names):
        """Overlap global GPU matches with local recovery on CPU, one query only.

        Called only during a local attempt on an exhaustive frozen map. Cancel
        remaining work if local recovery succeeds. No second model, no PnP
        thread, no speculative pose result.
        The same filtered-array fingerprint is checked by the later match().
        """
        context = getattr(self, '_match_context', None)
        cache = getattr(self, '_query_reuse', None)
        if context is None or cache is None or getattr(self,'_match_prefetch',None):
            return False
        query_key, predict = context
        cache = cache['matches']
        refs = [n for n in reference_names if (query_key,n) not in cache]
        if not refs: return False
        state = dict(cancel=threading.Event(), computed=0, error=None)
        def run():
            import torch
            started = time.perf_counter()
            try:
                with torch.inference_mode():
                    for ref in refs:
                        if state['cancel'].is_set(): break
                        cache[(query_key,ref)] = predict(ref)
                        state['computed'] += 1
            except Exception as exc:
                state['error'] = exc
            finally:
                state['elapsed_s'] = time.perf_counter()-started
        state['thread'] = threading.Thread(target=run,name='localization-match-prefetch',daemon=True)
        self._match_prefetch = state
        try:
            state['thread'].start()
        except Exception:
            self._match_prefetch = None
            raise
        return True

    def _finish_match_prefetch(self, *, cancel):
        state = getattr(self,'_match_prefetch',None)
        if state is None: return
        if cancel: state['cancel'].set()
        started = time.perf_counter()
        state['thread'].join()  # No detached work can leak into the next query.
        self._match_prefetch = None
        self._record_stage('prefetch_wait_s',started,
            prefetch_match_pairs=state['computed'],match_pairs=state['computed'],
            prefetch_s=state['elapsed_s'])
        if state['error'] is not None:
            raise RuntimeError('resident_match_prefetch_failed') from state['error']

    def localize(self, photo, output, config=None):
        with self.query_reuse():
            started = time.perf_counter()
            try:
                return self._localize(photo, output, config)
            finally:
                try:
                    self._finish_match_prefetch(cancel=True)
                finally:
                    self.last_profile['total_s'] = time.perf_counter()-started

    def _localize(self, photo, output, config=None):
        from .localization import localize_photo
        output = Path(output)
        # Local reference subgraph first; global retrieval/all-reference fallback.
        if self.last_references:
            result = localize_photo(Path(photo),self.map_dir,output/'local',config,
                                    resident=self,reference_names=self.last_references)
            directory = output/'local'
        else:
            result = None
        if result is None or result.pose_status != 'localized':
            directory = output/'global'
            result = localize_photo(Path(photo),self.map_dir,directory,config,resident=self)
        started = time.perf_counter()
        value, evidence = verified_localization(directory,self.manifest['manifest_sha256'])
        self._record_stage('verify_s', started)
        if value['pose_status'] == 'localized':
            refs = value['matched_references']
            ordered = list(self.reference_features)
            # Include temporal neighbours, not just repeatedly matched appearance.
            temporal = {Path(r.get('image_path') or '').name:i for i,r in enumerate(
                self.manifest.get('frames_manifest',{}).get('frames',[]))}
            ordered.sort(key=lambda n:temporal.get(n,float('inf')))
            selected = set(refs[:10])
            for name in list(selected):
                if name in ordered:
                    i=ordered.index(name); selected.update(ordered[max(0,i-3):i+4])
            self.last_references = [n for n in ordered if n in selected][:24]
        return value, evidence, directory


def verified_localization(directory, map_hash):
    directory=Path(directory); result=read_json(directory/'localization_result.json')
    evidence=read_json(directory/'query_evidence.json')
    if (evidence.get('evidence_sha256') != digest_json({k:v for k,v in evidence.items() if k!='evidence_sha256'})
            or digest_json(result) != evidence.get('localization_result_sha256')):
        raise ValueError('localization_evidence_tampered')
    verify_binding(evidence['binding'],Path(evidence['query_path']),map_hash)
    selected=evidence.get('selected_cluster')
    rows=next((c['rows'] for c in evidence['clusters'] if c['cluster_index']==selected),[])
    good=[r for r in rows if r['pnp_inlier'] and r['positive_depth'] and (r['reprojection_px'] or 0)<=4]
    counts={}
    for r in good:
        for ref in r['references']:
            n=ref['image_name']; counts[n]=counts.get(n,0)+1
    result['matched_references']=sorted(counts,key=counts.get,reverse=True)
    result['evidence_verified']=True
    result['inlier_rows']=good
    return result,evidence


class LatestFrameWorker:
    """One in-flight inference and one replaceable pending frame; generation fence."""
    def __init__(self, factory, output, *, not_before_monotonic=0.):
        self.factory=factory; self.output=Path(output); self.backend=None
        self.output.mkdir(parents=True,exist_ok=True)
        self.not_before_monotonic=not_before_monotonic
        self.condition=threading.Condition(); self.pending=None; self.completed=None
        self.generation=0; self.closed=False; self.dropped=0; self.busy=False
        self.thread=threading.Thread(target=self._run,daemon=True,name='navigation-localizer')
        self.thread.start()

    def _record(self, kind, metadata=None, **fields):
        row=dict(event=kind,at_monotonic=time.monotonic(),generation=self.generation,
                 metadata=metadata or {},**fields)
        with (self.output/'worker_events.jsonl').open('a',encoding='utf-8') as f:
            f.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')

    def reset(self):
        with self.condition:
            if self.pending:self._record('rejected',self.pending[2],reason='generation_reset_pending')
            if self.completed:self._record('rejected',self.completed.get('metadata'),reason='generation_reset_completed')
            self.generation+=1; self.pending=None; self.completed=None

    def submit(self, photo, metadata, config=None):
        with self.condition:
            if self.closed:
                return False
            if (metadata.get('time_mode')=='live_current'
                    and metadata.get('received_monotonic',0)<self.not_before_monotonic):
                self._record('rejected',metadata,reason='source_predates_warmup')
                return False
            if self.pending:
                self.dropped+=1
                self._record('replaced',self.pending[2],reason='newer_pending_frame')
            submitted=time.monotonic()
            self.pending=(self.generation,Path(photo),deepcopy(metadata),config,submitted)
            self._record('submitted',metadata,submitted_monotonic=submitted)
            self.condition.notify()
            return True

    def take(self):
        with self.condition:
            result,self.completed=self.completed,None
            if result:result['timing']['result_taken_monotonic']=time.monotonic()
            return result

    def close(self):
        with self.condition:
            if self.pending:self._record('rejected',self.pending[2],reason='worker_closed_pending')
            if self.completed:self._record('rejected',self.completed.get('metadata'),reason='worker_closed_completed')
            self.completed=None
            self.closed=True; self.pending=None; self.generation+=1; self.condition.notify()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda:self.closed or self.pending is not None)
                if self.closed:
                    return
                generation,photo,metadata,config,submitted=self.pending; self.pending=None; self.busy=True
            started=None
            try:
                if self.backend is None:
                    self.backend=self.factory()
                started=time.monotonic()
                result,evidence,directory=self.backend.localize(photo,self.output/uuid4().hex,config)
                value={'observation':{**result,**metadata,'image_path':str(photo)},'directory':str(directory),
                       'latency_s':time.monotonic()-started,'error':None,
                       'processing_stages':deepcopy(getattr(self.backend,'last_profile',None))}
            except Exception as exc:
                value={'error':str(exc),'metadata':metadata,'latency_s':time.monotonic()-(started or submitted)}
            finished=time.monotonic()
            value['metadata']=metadata
            value['timing']=dict(source_received_monotonic=metadata.get('received_monotonic'),
                submitted_monotonic=submitted,inference_started_monotonic=started,
                inference_finished_monotonic=finished,
                clock_semantics='PC_monotonic_not_sensor_exposure')
            with self.condition:
                self.busy=False
                if generation==self.generation and not self.closed:
                    if self.completed:self._record('replaced',self.completed.get('metadata'),reason='newer_completed_result')
                    self.completed=value
                    self._record('completed',metadata,timing=value['timing'],error=value['error'],
                                 processing_stages=value.get('processing_stages'))
                else:
                    self._record('rejected',metadata,reason='late_generation_result',timing=value['timing'])
