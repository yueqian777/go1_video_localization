"""Resident hloc backend and latest-frame mailbox. No capture/control startup.

Uses the pinned hloc preprocessing, SuperPoint, LightGlue and PyCOLMAP evidence
export. Reference features and models stay resident; research CLI is unchanged.
"""
from copy import deepcopy
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

    def verified_manifest(self, map_dir):
        if Path(map_dir).resolve() != self.map_dir:
            raise ValueError('resident_map_mismatch')
        if any((p.stat().st_size,p.stat().st_mtime_ns) != s for p,s in self.stamps.items()):
            raise ValueError('frozen_map_changed_restart_required')
        return self.manifest

    def _model(self, namespace, conf):
        from hloc.utils.base_model import dynamic_load
        key = namespace.__name__+json.dumps(conf,sort_keys=True)
        if key not in self.models:
            self.models[key] = dynamic_load(namespace,conf['name'])(conf).eval().to(self.device)
            self.model_loads += 1
        return self.models[key]

    def extract(self, conf, image_dir, export_dir, *, image_list, feature_path, overwrite):
        import torch, h5py
        from hloc import extractors
        model = self._model(extractors,conf['model'])
        dataset = self.extract_module.ImageDataset(image_dir,conf['preprocessing'],image_list)
        with torch.inference_mode(), h5py.File(feature_path,'w') as f:
            for i,name in enumerate(dataset.names):
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
                group = f.create_group(name)
                for k,v in pred.items():
                    group.create_dataset(k,data=v.astype(np.float16) if v.dtype == np.float32 else v)
                if 'keypoints' in group:
                    group['keypoints'].attrs['uncertainty'] = uncertainty
        return feature_path

    def match(self, conf, pairs, *, features, features_ref, matches, overwrite):
        import torch, h5py
        from hloc import matchers
        from hloc.utils.parsers import parse_retrieval, names_to_pair
        model = self._model(matchers,conf['model'])
        with torch.inference_mode(), h5py.File(features,'r') as qf, h5py.File(matches,'w') as mf:
            for q, refs in parse_retrieval(pairs).items():
                qdata = {k:np.asarray(v) for k,v in qf[q].items()}
                for ref in refs:
                    data = {}
                    for suffix, source in [('0',qdata),('1',self.reference_features[ref])]:
                        for k,v in source.items():
                            data[k+suffix] = torch.from_numpy(v.copy()).float().unsqueeze(0).to(self.device)
                        data['image'+suffix] = torch.empty((1,1,*tuple(source['image_size'])[::-1]))
                    pred = model(data); group = mf.create_group(names_to_pair(q,ref))
                    group.create_dataset('matches0',data=pred['matches0'][0].cpu().short().numpy())
                    if 'matching_scores0' in pred:
                        group.create_dataset('matching_scores0',data=pred['matching_scores0'][0].cpu().half().numpy())
        return matches

    def localize(self, photo, output, config=None):
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
        value, evidence = verified_localization(directory,self.manifest['manifest_sha256'])
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
    def __init__(self, factory, output):
        self.factory=factory; self.output=Path(output); self.backend=None
        self.condition=threading.Condition(); self.pending=None; self.completed=None
        self.generation=0; self.closed=False; self.dropped=0; self.busy=False
        self.thread=threading.Thread(target=self._run,daemon=True,name='navigation-localizer')
        self.thread.start()

    def reset(self):
        with self.condition:
            self.generation+=1; self.pending=None; self.completed=None

    def submit(self, photo, metadata, config=None):
        with self.condition:
            if self.closed:
                return
            if self.pending:
                self.dropped+=1
            self.pending=(self.generation,Path(photo),deepcopy(metadata),config)
            self.condition.notify()

    def take(self):
        with self.condition:
            result,self.completed=self.completed,None
            return result

    def close(self):
        with self.condition:
            self.closed=True; self.pending=None; self.generation+=1; self.condition.notify()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda:self.closed or self.pending is not None)
                if self.closed:
                    return
                generation,photo,metadata,config=self.pending; self.pending=None; self.busy=True
            started=time.monotonic()
            try:
                if self.backend is None:
                    self.backend=self.factory()
                result,evidence,directory=self.backend.localize(photo,self.output/uuid4().hex,config)
                value={'observation':{**result,**metadata,'image_path':str(photo)},'directory':str(directory),
                       'latency_s':time.monotonic()-started,'error':None}
            except Exception as exc:
                value={'error':str(exc),'metadata':metadata,'latency_s':time.monotonic()-started}
            with self.condition:
                self.busy=False
                if generation==self.generation and not self.closed:
                    self.completed=value
