"""Fixed JSON worker protocol for offline viewer jobs (no shell, no robot APIs).

Each job gets an exclusive directory, an immutable request, and atomically
replaced status/result JSON. Browser disconnects never own worker lifetime.
Only completed frozen models/ready snapshots are converted to viewer payloads.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from uuid import uuid4

KINDS = {
    'inspect_map': {'component_id', 'point_budget'},
    'inspect_snapshot': {'snapshot_dir', 'snapshot_index', 'point_budget'},
    'locate_object': {'photo', 'roi', 'mask_path', 'query_dir', 'cluster_radius', 'min_points', 'config'},
    'localize_current': {'photo', 'config'},
    'plan_route': {'graph_path', 'start', 'target'},
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def ensure_sidecar(map_dir, sidecar_dir):
    source, target = Path(map_dir).resolve(), Path(sidecar_dir).resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError('sidecar_overlaps_frozen_map')
    return target


def atomic_json(path, payload):
    path = Path(path)
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(data, encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    path = Path(path)
    if path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError('json_size_limit')
    return json.loads(path.read_text(encoding='utf-8'))


def _validate(kind, params):
    if kind not in KINDS:
        raise ValueError('unsupported_job')
    if not isinstance(params, dict) or params.keys() - KINDS[kind]:
        raise ValueError('unsupported_parameter')
    # Also rejects objects, NaN and inf before starting a process.
    json.dumps(params, allow_nan=False)


class JobManager:
    """Small bounded subprocess pool; persisted status supports viewer reconnects.

    close() intentionally does not cancel running work. An interrupted/killed
    OS worker is not restarted automatically; no job is silently resubmitted.
    """
    def __init__(self, map_dir, sidecar_dir, *, max_active=1):
        self.map_dir = Path(map_dir).resolve()
        self.sidecar_dir = ensure_sidecar(self.map_dir, sidecar_dir)
        self.root = self.sidecar_dir / 'jobs'
        self.root.mkdir(parents=True, exist_ok=True)
        if max_active < 1:
            raise ValueError('invalid_job_capacity')
        self.max_active = max_active
        self._processes = {}
        self._lock = threading.RLock()

    def submit(self, kind, params):
        _validate(kind, params)
        with self._lock:
            if sum(j['status'] in ('queued', 'running') for j in self.list_jobs()) >= self.max_active:
                raise RuntimeError('job_capacity_reached')
            job_id = uuid4().hex
            directory = ensure_sidecar(self.map_dir, self.root/job_id)
            directory.mkdir()
            request = {'schema_version': 'go1loc.job.v1', 'job_id': job_id, 'kind': kind,
                       'map_dir': str(self.map_dir), 'job_dir': str(directory), 'params': params}
            atomic_json(directory/'request.json', request)
            state = {'job_id': job_id, 'kind': kind, 'status': 'queued', 'created_at': utc_now(),
                     'sequence': 0, 'worker_pid': None, 'execution_permitted': False}
            atomic_json(directory/'status.json', state)
            env = os.environ.copy()
            temp = directory/'tmp'
            temp.mkdir()
            env.update(TEMP=str(temp), TMP=str(temp), PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
            # Preserve the existing D: model cache rather than downloading again.
            env.setdefault('TORCH_HOME', str(Path(__file__).resolve().parents[2]/'.models'/'torch'))
            try:
                with (directory/'worker.log').open('wb') as log:
                    process = subprocess.Popen(
                        [sys.executable, '-m', 'go1loc.jobs', '--request', str(directory/'request.json')],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                        shell=False, env=env, cwd=str(Path(__file__).resolve().parents[2]),
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self._processes[job_id] = process
            except Exception as exc:
                state.update(status='failed', error=type(exc).__name__ + ': ' + str(exc), sequence=1)
                atomic_json(directory/'status.json', state)
                raise
            return state

    def status(self, job_id):
        if not re.fullmatch(r'[a-f0-9]{32}', job_id):
            raise ValueError('invalid_job_id')
        state = read_json(self.root/job_id/'status.json')
        process = self._processes.get(job_id)
        if process and process.poll() is not None:
            if state['status'] in ('queued', 'running'):
                state.update(status='failed', error='worker_exited_without_result',
                             finished_at=utc_now(), sequence=state['sequence']+1)
                atomic_json(self.root/job_id/'status.json', state)
            self._processes.pop(job_id, None)
        return state

    def list_jobs(self):
        with self._lock:
            return [self.status(p.parent.name) for p in sorted(self.root.glob('*/status.json'))]

    def close(self):
        """No cancellation. Children finish and write durable status after disconnect."""

    def wait(self, job_id, timeout=None):
        """Optional CLI/test reaping; UI polls status and never waits on a client."""
        process = self._processes.get(job_id)
        if process:
            process.wait(timeout=timeout)
        return self.status(job_id)


def _resolve(base, value):
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base/path).resolve()


def scene_payload(map_dir, *, component_id=None, point_budget=50000, snapshot=None):
    """Called in a worker only. Returns JSON, not a live Reconstruction object."""
    from .evidence import verify_map

    map_dir = Path(map_dir).resolve()
    manifest = verify_map(map_dir)
    if not 1 <= int(point_budget) <= 50000:
        raise ValueError('invalid_point_budget')
    # Reject invalid inputs before cold-loading native visualization dependencies.
    # They can be slow on Windows; a missing manifest needs no reconstruction.
    import numpy as np
    import pycolmap
    from scipy.spatial.transform import Rotation

    components = manifest.get('components', [])
    mode = 'build_snapshot' if snapshot else 'final_map_replay'
    if snapshot:
        model_dir = Path(snapshot['snapshot_path'])
        component_id = snapshot['component_id']
        allowed = None
    else:
        if not components:
            raise ValueError('map_components_missing')
        component = next((c for c in components if c['component_id'] == (component_id or components[0]['component_id'])), None)
        if component is None:
            raise ValueError('unknown_component')
        component_id = component['component_id']
        allowed = set(component['image_ids'])
        model_dir = _resolve(map_dir, manifest['model_dir'])
    model = pycolmap.Reconstruction(str(model_dir))
    times = {r['image_name']: r.get('pts_time_s') for r in snapshot.get('frames', [])} if snapshot else {}
    image_dir = _resolve(map_dir, manifest['image_dir'])
    frame_manifest = manifest.get('frames_manifest')
    # Only use hash-linked, embedded timing. An adjacent file may belong to a
    # later extraction and must never silently change a frozen map's replay.
    if frame_manifest and not snapshot:
        for row in frame_manifest.get('frames', []):
            if row.get('kept'):
                times[Path(row['image_path']).name] = row.get('pts_time_s')
    cameras = []
    image_ids = set()
    for image_id, img in sorted(model.images.items()):
        if allowed is not None and image_id not in allowed:
            continue
        image_ids.add(image_id)
        pose = img.cam_from_world().inverse()
        T = np.eye(4)
        T[:3] = pose.matrix()
        camera = model.cameras[img.camera_id]
        path = _resolve(image_dir, img.name)
        if not path.is_relative_to(image_dir):
            raise ValueError('reference_image_path_escape')
        wxyz = Rotation.from_matrix(T[:3, :3]).as_quat(scalar_first=True)
        cameras.append({'name': img.name, 'image_id': int(image_id), 'T_WC': T.tolist(),
                        'wxyz': wxyz.tolist(), 'image_path': str(path),
                        'fov': float(2*np.arctan(camera.height/(2*camera.focal_length_y))),
                        'aspect': camera.width/camera.height, 'pts_time_s': times.get(img.name)})
    points = [(pid, p) for pid, p in sorted(model.points3D.items())
              if any(e.image_id in image_ids for e in p.track.elements)]
    total = len(points)
    if total > point_budget:
        points = [points[i] for i in np.linspace(0, total-1, int(point_budget), dtype=int)]
    # Frame list remains real PTS ordered only when timestamps exist.
    cameras.sort(key=lambda c: (c['pts_time_s'] is None, c['pts_time_s'] or 0, c['image_id']))
    return {'schema_version': 'go1loc.viewer_scene.v1', 'map_id': manifest['map_id'],
            'map_hash': manifest['manifest_sha256'], 'component_id': component_id, 'mode': mode,
            'revision': snapshot['snapshot_index'] if snapshot else manifest['manifest_sha256'],
            'units': manifest['units'], 'scale_status': manifest['scale_status'],
            'points': [p.xyz.tolist() for _, p in points], 'colors': [p.color.tolist() for _, p in points],
            'point_ids': [int(pid) for pid, _ in points], 'total_points': total, 'cameras': cameras,
            'pts_available': bool(cameras) and all(c['pts_time_s'] is not None for c in cameras),
            'warnings': ['historical_visualization_not_navigation', 'unaligned_components_are_not_combined'],
            'execution_permitted': False}


def _localization_config(value):
    from .localization import LocalizationConfig
    value = dict(value or {})
    if value.get('allow_map_update'):
        raise ValueError('map_updates_disabled')
    if value.get('calibration_path'):
        value['calibration_path'] = Path(value['calibration_path'])
    return LocalizationConfig(**value)


def execute_job(request):
    kind, params = request['kind'], request['params']
    map_dir = Path(request['map_dir'])
    output = Path(request['job_dir'])/'output'
    if kind == 'inspect_map':
        payload = scene_payload(map_dir, **params)
        path = output/'scene.json'
        atomic_json(path, payload)
    elif kind == 'inspect_snapshot':
        from .snapshots import list_ready_snapshots
        snapshots = list_ready_snapshots(Path(params['snapshot_dir']))
        if not snapshots:
            raise ValueError('no_ready_snapshot')
        index = int(params.get('snapshot_index', -1))
        snapshot = snapshots[index]
        # Snapshot run map identity, when supplied, must agree with frozen map.
        from .evidence import verify_map
        manifest = verify_map(map_dir)
        if snapshot.get('map_id') not in (None, manifest['map_id']):
            raise ValueError('snapshot_map_mismatch')
        payload = scene_payload(map_dir, snapshot=snapshot, point_budget=params.get('point_budget', 50000))
        path = output/'scene.json'
        atomic_json(path, payload)
    elif kind == 'locate_object':
        from .photo_object import locate_photo_object
        kwargs = {k: v for k, v in params.items() if k not in ('photo', 'config')}
        locate_photo_object(Path(params['photo']), map_dir, output,
                            config=_localization_config(params.get('config')), **kwargs)
        path = output/'object_result.json'
    elif kind == 'plan_route':
        from .planning import plan_route
        from .evidence import verify_map
        manifest = verify_map(map_dir)
        graph = read_json(params['graph_path'])
        if graph.get('map_id') != manifest['map_id'] or graph.get('map_hash') != manifest['manifest_sha256']:
            raise ValueError('planning_graph_map_mismatch')
        payload = plan_route(graph, params['start'], params['target'], now=utc_now())
        path = output/'planning_result.json'
        atomic_json(path, payload)
    else:  # fixed localize_current entry, no arbitrary import or command
        from .localization import localize_photo
        localize_photo(Path(params['photo']), map_dir, output, _localization_config(params.get('config')))
        path = output/'localization_result.json'
    return {'result_path': str(path), 'output_dir': str(output)}


def run_request(request_path):
    request = read_json(request_path)
    _validate(request['kind'], request['params'])
    directory = ensure_sidecar(request['map_dir'], request['job_dir'])
    if Path(request_path).resolve().parent != directory:
        raise ValueError('request_directory_mismatch')
    state_path = directory/'status.json'
    state = read_json(state_path)
    state.update(status='running', sequence=1, worker_pid=os.getpid(), started_at=utc_now())
    atomic_json(state_path, state)
    try:
        state.update(execute_job(request), status='completed')
    except Exception as exc:
        state.update(status='failed', error=f'{type(exc).__name__}: {str(exc)[:2000]}')
    state.update(sequence=2, finished_at=utc_now())
    atomic_json(state_path, state)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    args = parser.parse_args()
    state = run_request(args.request)
    return 0 if state['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
