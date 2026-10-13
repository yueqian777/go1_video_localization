"""Standalone first-build monitoring, independent of a frozen map manifest.

``run.json`` is atomic, hash-checked run metadata, not a map manifest. Events
describe processing stages; only atomically published READY snapshots are
parsed. Snapshots replace entire, unaligned build-attempt coordinate systems.
No live Reconstruction object or half-written native model is ever read.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
from uuid import uuid4

from .events import atomic_replace, outside_artifacts, read_events, utc_now
from .snapshots import list_ready_snapshots, read_ready_snapshot


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode('utf-8')).hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    """Single-writer publication with replace, including Windows sharing retry."""
    path = Path(path)
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode('utf-8')
    temporary = path.with_name(path.name + '.pending-' + uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        atomic_replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_run(run_dir: Path, metadata: dict) -> dict:
    """Publish/update a build-owned run.json; caller must own a fresh run_dir."""
    row = {k: v for k, v in metadata.items() if k != 'run_sha256'}
    row['updated_at'] = utc_now()
    row['run_sha256'] = _digest(row)
    atomic_json(Path(run_dir)/'run.json', row)
    return row


def _read_run(run_dir: Path) -> dict:
    path = run_dir/'run.json'
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError('invalid_run_metadata_file')
    row = json.loads(path.read_text(encoding='utf-8'))
    unsigned = {k: v for k, v in row.items() if k != 'run_sha256'}
    if row.get('run_sha256') != _digest(unsigned):
        raise ValueError('run_metadata_hash_mismatch')
    if (row.get('schema_version') != 'go1loc.build_run.v1' or row.get('run_id') != run_dir.name
            or not isinstance(row.get('map_id'), str) or not row['map_id']
            or row.get('status') not in {'running', 'completed', 'failed', 'cancelled'}
            or row.get('execution_permitted') is not False):
        raise ValueError('invalid_run_metadata_identity_or_status')
    for key in ('image_dir', 'output_dir', 'events_path'):
        if not isinstance(row.get(key), str) or not Path(row[key]).is_absolute():
            raise ValueError('invalid_run_metadata_path:' + key)
    outside_artifacts(run_dir, Path(row['image_dir']), Path(row['output_dir']))
    outside_artifacts(Path(row['events_path']), Path(row['image_dir']), Path(row['output_dir']))
    if not row.get('units') or not row.get('scale_status'):
        raise ValueError('run_scale_metadata_missing')
    return row


def _snapshots(run_dir: Path, run: dict) -> list[dict]:
    root = run_dir/'snapshots'
    if root.is_symlink() or root.resolve().parent != run_dir:
        raise ValueError('snapshot_directory_escape')
    rows = list_ready_snapshots(root)
    indices = set()
    for row in rows:
        if Path(row['snapshot_path']).resolve().parent != root.resolve():
            raise ValueError('snapshot_directory_escape')
        if row.get('schema_version') != 'go1loc.snapshot.v1' or not row.get('snapshot_sha256'):
            raise ValueError('snapshot_metadata_checksum_missing')
        if row.get('run_id') != run['run_id']:
            raise ValueError('snapshot_run_mismatch')
        if row.get('map_id') != run['map_id']:
            raise ValueError('snapshot_map_mismatch')
        if not re.fullmatch(re.escape(run['map_id']) + r':build-attempt:[1-9][0-9]*',
                            str(row.get('component_id', ''))):
            raise ValueError('snapshot_component_mismatch')
        index = row.get('snapshot_index')
        if (type(index) is not int or index < 1 or index in indices
                or Path(row['snapshot_path']).name != f'snapshot-{index:06d}'):
            raise ValueError('snapshot_index_mismatch')
        indices.add(index)
        if (row.get('source') != 'pycolmap_native_snapshot'
                or row.get('state_semantics') != 'replace_component'
                or row.get('alignment_to_final_map') != 'not_established'):
            raise ValueError('snapshot_state_semantics_mismatch')
    return rows


def read_build_state(run_dir: Path) -> dict:
    """Return run/events/snapshots/status/stage/failure_reason/final_map_dir.

    Before publication: waiting + run=None. A crash without a terminal event
    stays running/last-known (no guessed failure). Failure/cancel never erase
    snapshots. A claimed final result is independently hash/identity checked;
    an invalid result returns failed + final_map_dir=None, retaining snapshots.
    """
    run_dir = Path(run_dir).resolve()
    if not (run_dir/'run.json').exists():
        return dict(run=None, events=[], snapshots=[], status='waiting',
                    stage='waiting_for_run_metadata', failure_reason=None, final_map_dir=None)
    run = _read_run(run_dir)
    events = read_events(Path(run['events_path']))
    previous = 0
    for event in events:
        if event.get('run_id') != run['run_id']:
            raise ValueError('event_run_mismatch')
        if event.get('map_id') != run['map_id']:
            raise ValueError('event_map_mismatch')
        if type(event.get('sequence')) is not int or event['sequence'] <= previous:
            raise ValueError('event_sequence_invalid')
        previous = event['sequence']
    snapshots = _snapshots(run_dir, run)
    status = run['status']
    stage = run.get('stage') or 'initializing'
    reason = run.get('failure_reason')
    for event in events:
        if event.get('stage'):
            stage = event['stage']
        if status == 'running':
            if event.get('event') in ('failed', 'cancelled'):
                status = event['event']
                reason = event.get('data', {}).get('error') or event['event']
            elif event.get('event') == 'final':
                status = 'completed'
    final_map_dir = None
    if status == 'completed':
        try:
            from .evidence import verify_map
            final = verify_map(Path(run['output_dir']))
            if (final['map_id'] != run['map_id'] or final.get('build_run_id') != run['run_id']
                    or Path(final['image_dir']).resolve() != Path(run['image_dir']).resolve()):
                raise ValueError('run_identity_mismatch')
            if (run.get('final_manifest_sha256') is not None
                    and run['final_manifest_sha256'] != final['manifest_sha256']):
                raise ValueError('run_final_hash_mismatch')
            final_map_dir = str(Path(run['output_dir']).resolve())
        except (OSError, ValueError, KeyError, TypeError) as exc:
            status = 'failed'
            reason = 'final_map_invalid:' + str(exc)[:500]
    return dict(run=run, events=events, snapshots=snapshots, status=status,
                stage=stage, failure_reason=reason, final_map_dir=final_map_dir)


def snapshot_scene_payload(run_dir: Path, snapshot_index: int = -1,
                           point_budget: int = 50000) -> dict:
    """Worker-only native parse, same JSON scene schema as jobs.scene_payload.

    Index is a list index (0=first, -1=latest), not READY's one-based revision.
    map_hash intentionally remains null: final alignment is never established
    merely because the same run has subsequently produced a frozen map.
    """
    if type(point_budget) is not int or not 1 <= point_budget <= 50000:
        raise ValueError('invalid_point_budget')
    if type(snapshot_index) is not int:
        raise ValueError('invalid_snapshot_index')
    run_dir = Path(run_dir).resolve()
    # Deliberately do not call verify_map/read_build_state: snapshot parsing has
    # no dependency on a completed or even existent final map directory.
    run = _read_run(run_dir)
    rows = _snapshots(run_dir, run)
    if not rows:
        raise ValueError('no_ready_snapshot')
    try:
        snapshot = rows[snapshot_index]
    except IndexError as exc:
        raise ValueError('snapshot_index_out_of_range') from exc
    import numpy as np
    import pycolmap
    from scipy.spatial.transform import Rotation

    model_dir = Path(snapshot['snapshot_path'])
    model = pycolmap.Reconstruction(str(model_dir))
    if (not model.is_valid() or model.num_reg_images() < 2 or model.num_points3D() < 1
            or model.num_reg_images() != snapshot.get('registered_images')
            or model.num_points3D() != snapshot.get('points3D')):
        raise ValueError('snapshot_native_counts_or_validity_mismatch')
    frames = snapshot.get('frames', [])
    frame_names = [(r['image_id'], r['image_name']) for r in frames]
    native_names = [(int(i), im.name) for i, im in model.images.items()]
    if sorted(frame_names) != sorted(native_names):
        raise ValueError('snapshot_frame_identity_mismatch')
    times = {r['image_name']: r.get('pts_time_s') for r in frames}
    if any(t is not None and (type(t) not in (int, float) or not math.isfinite(t)) for t in times.values()):
        raise ValueError('snapshot_pts_invalid')
    image_dir = Path(run['image_dir']).resolve()
    cameras = []
    for image_id, img in sorted(model.images.items()):
        pose = img.cam_from_world().inverse()
        T = np.eye(4)
        T[:3] = pose.matrix()
        camera = model.cameras[img.camera_id]
        path = (image_dir/img.name).resolve()
        if not path.is_relative_to(image_dir):
            raise ValueError('reference_image_path_escape')
        cameras.append({'name': img.name, 'image_id': int(image_id), 'T_WC': T.tolist(),
                        'wxyz': Rotation.from_matrix(T[:3, :3]).as_quat(scalar_first=True).tolist(),
                        'image_path': str(path),
                        'fov': float(2*np.arctan(camera.height/(2*camera.focal_length_y))),
                        'aspect': camera.width/camera.height, 'pts_time_s': times.get(img.name)})
    image_ids = set(model.images)
    points = [(pid, p) for pid, p in sorted(model.points3D.items())
              if any(e.image_id in image_ids for e in p.track.elements)]
    total = len(points)
    if total > point_budget:
        points = [points[i] for i in np.linspace(0, total-1, point_budget, dtype=int)]
    cameras.sort(key=lambda c: (c['pts_time_s'] is None, c['pts_time_s'] or 0, c['image_id']))
    # Detect metadata/native mutation during loading, rather than display a
    # state that no longer matches the complete immutable READY publication.
    if snapshot != read_ready_snapshot(model_dir):
        raise ValueError('snapshot_changed_during_read')
    return {'schema_version': 'go1loc.viewer_scene.v1', 'map_id': run['map_id'],
            'map_hash': None, 'run_id': run['run_id'], 'component_id': snapshot['component_id'],
            'mode': 'build_snapshot', 'revision': snapshot['snapshot_index'],
            'units': run['units'], 'scale_status': run['scale_status'],
            'points': [p.xyz.tolist() for _, p in points], 'colors': [p.color.tolist() for _, p in points],
            'point_ids': [int(pid) for pid, _ in points], 'total_points': total, 'cameras': cameras,
            'pts_available': bool(cameras) and all(c['pts_time_s'] is not None for c in cameras),
            'warnings': ['historical_visualization_not_navigation', 'unaligned_components_are_not_combined'],
            'execution_permitted': False}
