"""Versioned, JSON-only evidence. hloc indices are explicitly joined, never zipped blindly."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from .video import sha256_file


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode('utf-8')).hexdigest()


def resolve_map_path(map_dir: Path, value):
    path = Path(value)
    return path.resolve() if path.is_absolute() else (map_dir/path).resolve()


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def verify_map(map_dir: Path):
    manifest = json.loads((map_dir/'map_manifest.json').read_text(encoding='utf-8'))
    expected = manifest.get('manifest_sha256')
    unsigned = {k: v for k, v in manifest.items() if k != 'manifest_sha256'}
    if not manifest.get('frozen') or not expected or digest_json(unsigned) != expected:
        raise ValueError('invalid_map_manifest_hash_or_not_frozen')
    model_dir = resolve_map_path(map_dir, manifest['model_dir'])
    for name, digest in manifest.get('native_model_sha256', {}).items():
        if Path(name).name != name or sha256_file(model_dir/name) != digest:
            raise ValueError('map_native_model_hash_mismatch')
    if not manifest.get('native_model_sha256'):
        raise ValueError('map_native_hash_missing')
    if manifest.get('local_features_sha256'):
        if sha256_file(resolve_map_path(map_dir, manifest['local_features'])) != manifest['local_features_sha256']:
            raise ValueError('map_local_features_hash_mismatch')
    return manifest


def verify_binding(binding, photo: Path, map_hash: str):
    if binding.get('map_hash') != map_hash:
        raise ValueError('evidence_map_hash_mismatch')
    if binding.get('query_sha256') != sha256_file(photo):
        raise ValueError('evidence_query_hash_mismatch')
    if 'geometry' in binding or 'geometry_sha256' in binding:
        if digest_json(binding.get('geometry')) != binding.get('geometry_sha256'):
            raise ValueError('evidence_geometry_hash_mismatch')
    for name, record in binding.get('files', {}).items():
        if sha256_file(Path(record['path'])) != record['sha256']:
            raise ValueError('evidence_file_hash_mismatch:' + name)


def make_binding(photo, manifest, files, geometry):
    return {'query_sha256': sha256_file(photo), 'map_hash': manifest['manifest_sha256'],
            'geometry': geometry, 'geometry_sha256': digest_json(geometry),
            'files': {name: {'path': str(path.resolve()), 'sha256': sha256_file(path)}
                      for name, path in files.items() if path is not None}}


def export_rows(model, log, ret, camera, match_provider):
    """Pinned hloc c13273b: log DB indices are local offsets into log['db'].

    Stored uv is zero-based image pixel-center coordinates (OpenCV/hloc).
    uv_colmap includes hloc's +0.5 shift. inlier_mask indexes flattened 2D-3D
    correspondences, NOT unique query keypoints or unique map points.
    """
    ids = log['points3D_ids']
    uv_colmap = np.asarray(log['keypoints_query'], dtype=float).reshape(-1, 2)
    query_indices, associations = log['keypoint_index_to_db']
    if not (len(ids) == len(uv_colmap) == len(query_indices) == len(associations)):
        raise ValueError('evidence_log_length_mismatch')
    mask = np.asarray(ret.get('inlier_mask', []), dtype=bool) if ret else np.zeros(len(ids), bool)
    if mask.shape != (len(ids),):
        raise ValueError('evidence_inlier_mask_length_mismatch')
    xyz = np.asarray([model.points3D[int(i)].xyz for i in ids], dtype=float).reshape(-1, 3)
    errors = np.full(len(ids), np.nan)
    positive_depth = np.zeros(len(ids), bool)
    if ret and len(ids):
        pose = ret['cam_from_world']
        camera_xyz = xyz @ np.asarray(pose.rotation.matrix()).T + np.asarray(pose.translation)
        positive_depth = camera_xyz[:, 2] > 0
        projected = np.asarray(camera.img_from_cam(camera_xyz))
        errors = np.linalg.norm(projected - uv_colmap, axis=1)
    # Match scores belong to explicit (query index, reference index) pairs.
    lookup = {}
    for db_id in log['db']:
        ref = model.images[int(db_id)]
        matches, scores = match_provider(ref.name)
        if len(matches) != len(scores):
            raise ValueError('evidence_match_score_length_mismatch')
        table = {}
        for (qidx, ridx), score in zip(matches, scores):
            p = ref.points2D[int(ridx)]
            if p.has_point3D():
                table[(int(qidx), int(p.point3D_id))] = (int(ridx), float(score))
        lookup[int(db_id)] = table
    rows = []
    for i, (pid, qidx, association) in enumerate(zip(ids, query_indices, associations)):
        if int(association[0]) != int(pid):
            raise ValueError('evidence_point_id_alignment_mismatch')
        references = []
        for offset in association[1]:
            if not 0 <= int(offset) < len(log['db']):
                raise ValueError('evidence_db_offset_out_of_bounds')
            db_id = int(log['db'][int(offset)])
            key = (int(qidx), int(pid))
            if key not in lookup[db_id]:
                raise ValueError('evidence_match_pair_not_found')
            ridx, score = lookup[db_id][key]
            references.append({'image_id': db_id, 'image_name': model.images[db_id].name,
                               'reference_keypoint_index': ridx,
                               'match_score': score if np.isfinite(score) else None})
        rows.append({'correspondence_index': i, 'query_keypoint_index': int(qidx),
                     'uv_colmap': uv_colmap[i].tolist(), 'uv': (uv_colmap[i] - .5).tolist(),
                     'point3D_id': int(pid), 'xyz': xyz[i].tolist(), 'references': references,
                     'pnp_inlier': bool(mask[i]), 'positive_depth': bool(positive_depth[i]),
                     'reprojection_px': float(errors[i]) if np.isfinite(errors[i]) else None})
    return rows


def row_diagnostics(rows, width, height):
    good = [r for r in rows if r['pnp_inlier'] and r['positive_depth'] and r['reprojection_px'] is not None]
    errors = [r['reprojection_px'] for r in good]
    occupied = {(min(3, max(0, int(r['uv'][0]*4/width))),
                 min(3, max(0, int(r['uv'][1]*4/height)))) for r in good}
    return {'mean_inlier_reprojection_px': float(np.mean(errors)) if errors else None,
            'median_inlier_reprojection_px': float(np.median(errors)) if errors else None,
            'p95_inlier_reprojection_px': float(np.percentile(errors, 95)) if errors else None,
            'unique_inlier_keypoints': len({r['query_keypoint_index'] for r in good}),
            'unique_inlier_map_points': len({r['point3D_id'] for r in good}),
            'inlier_4x4_grid_coverage': len(occupied)/16}
