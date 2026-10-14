"""Filter external hloc features BEFORE matching; never modify scene pixels.

Indices in the report refer to the pre-filter arrays. Matchers and COLMAP see
only the new, consistently indexed arrays. This is not a COLMAP mask option.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import numpy as np

from .video import sha256_file


SCREEN_SOURCE = 'go1_screen_recording'


def read_mask(path: Path):
    import cv2
    mask = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if mask is None or mask.ndim != 2 or not np.isin(mask, [0, 255]).all():
        raise ValueError('feature_mask_must_be_binary_png')
    return mask


def screen_frame_binding(photo: Path, manifest_path: Path | None = None) -> dict | None:
    """Bind a derived PNG to source recording, PTS, geometry and exact mask.

    A moved query can explicitly supply its original frames manifest. Filenames
    alone are never identity. Identical ambiguous frames require the original
    recorded path, rather than guessing which PTS belongs to a copied PNG.
    """
    photo = photo.resolve()
    source = manifest_path or photo.parent.parent/'frames_manifest.json'
    if not source.is_file():
        if manifest_path:
            raise ValueError('screen_frames_manifest_missing')
        return None
    data = json.loads(source.read_text(encoding='utf-8'))
    if data.get('source_kind') != SCREEN_SOURCE:
        if manifest_path:
            raise ValueError('not_a_screen_frames_manifest')
        return None
    if data.get('timing_source') != 'screen_display':
        raise ValueError('screen_timing_source_invalid')
    digest = sha256_file(photo)
    rows = [r for r in data['frames'] if r.get('kept') and r.get('image_sha256') == digest]
    exact = [r for r in rows if (source.parent/r['image_path']).resolve() == photo]
    if len(exact or rows) != 1:
        raise ValueError('screen_frame_hash_missing_or_ambiguous')
    row = (exact or rows)[0]
    if row.get('captured_at') is not None:
        raise ValueError('screen_frame_cannot_claim_sensor_capture_time')
    mask_info = data['feature_mask']
    mask_path = (source.parent/mask_info['path']).resolve()
    if source.parent.resolve() not in mask_path.parents:
        raise ValueError('screen_mask_outside_import')
    if not mask_path.is_file() or sha256_file(mask_path) != mask_info['sha256']:
        raise ValueError('screen_mask_hash_mismatch')
    mask = read_mask(mask_path)
    from PIL import Image
    with Image.open(photo) as im:
        if mask.shape != (im.height, im.width):
            raise ValueError('screen_mask_image_size_mismatch')
    source_sha = data.get('probe', {}).get('source_sha256', '')
    if len(source_sha) != 64 or any(c not in '0123456789abcdef' for c in source_sha):
        raise ValueError('screen_source_hash_invalid')
    return {'source_kind': SCREEN_SOURCE, 'source_sha256': source_sha,
            'timing_source': 'screen_display', 'sensor_capture_time': None,
            'manifest_path': str(source.resolve()), 'manifest_sha256': sha256_file(source),
            'frame': row, 'geometry': data['geometry'],
            'mask_path': str(mask_path), 'mask_sha256': mask_info['sha256']}


def filter_features(feature_path: Path, masks: dict, *, image_paths: dict | None = None,
                    diagnostics_dir: Path | None = None) -> dict:
    """Atomic all-or-nothing filtering of known SuperPoint aligned datasets.

    Reject unknown arrays rather than leaving stale per-keypoint indices behind.
    Caller must own a fresh feature file with no matches generated from it yet.
    """
    import h5py
    import cv2
    tmp = feature_path.with_name(feature_path.name+'.masking.partial')
    if tmp.exists():
        raise ValueError('feature_mask_partial_exists_use_new_output')
    reports = {}
    shutil.copyfile(feature_path, tmp)
    try:
        with h5py.File(tmp, 'r+') as f:
            for name, mask in masks.items():
                group = f[name]
                if group.attrs.get('go1loc_mask_applied'):
                    raise ValueError('features_already_filtered')
                if not {'keypoints', 'descriptors', 'scores', 'image_size'} <= set(group):
                    raise ValueError('superpoint_feature_fields_missing')
                if set(group) - {'keypoints', 'descriptors', 'scores', 'image_size', 'scales', 'oris'}:
                    raise ValueError('unknown_feature_array_alignment')
                points = group['keypoints'][:]
                size = group['image_size'][:]
                if mask.shape != (int(size[1]), int(size[0])) or points.ndim != 2 or points.shape[1] != 2:
                    raise ValueError('feature_mask_size_or_keypoints_invalid')
                n = len(points)
                valid = np.isfinite(points).all(axis=1)
                xy = np.floor(np.where(np.isfinite(points), points, -1)+0.5).astype(np.int64)
                valid &= (xy[:, 0] >= 0) & (xy[:, 0] < size[0]) & (xy[:, 1] >= 0) & (xy[:, 1] < size[1])
                inside = np.flatnonzero(valid)
                valid[inside] &= mask[xy[inside, 1], xy[inside, 0]] > 0
                retained = np.flatnonzero(valid)
                for key in list(group):
                    if key == 'image_size':
                        continue
                    ds = group[key]; array = ds[:]; attrs = dict(ds.attrs)
                    axis = 1 if key == 'descriptors' else 0
                    if array.ndim <= axis or array.shape[axis] != n:
                        raise ValueError('inconsistent_feature_array_'+key)
                    filtered = np.take(array, retained, axis=axis)
                    del group[key]
                    group.create_dataset(key, data=filtered).attrs.update(attrs)
                group.attrs['go1loc_mask_applied'] = True
                reports[name] = {'before': n, 'after': len(retained), 'removed': n-len(retained),
                                 'retained_original_indices': retained.tolist()}
                if diagnostics_dir and image_paths and name in image_paths:
                    diagnostics_dir.mkdir(parents=True, exist_ok=True)
                    img = cv2.imdecode(np.fromfile(image_paths[name], np.uint8), cv2.IMREAD_COLOR)
                    if img is None:
                        raise ValueError('diagnostic_image_unreadable')
                    for i, (x, y) in enumerate(xy):
                        if 0 <= x < img.shape[1] and 0 <= y < img.shape[0]:
                            cv2.circle(img, (int(x), int(y)), 2, (0, 200, 0) if valid[i] else (0, 0, 255), 1)
                    cv2.imencode('.png', img)[1].tofile(diagnostics_dir/(name.replace('/', '_')+'.keypoints.png'))
            f.flush()
        os.replace(tmp, feature_path)
    finally:
        if tmp.exists():
            tmp.unlink()
    if diagnostics_dir:
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        (diagnostics_dir/'feature_filter.json').write_text(json.dumps(reports, indent=2), encoding='utf-8')
    return reports


def filter_screen_map_features(feature_path: Path, image_dir: Path, names: list[str], frames: dict | None,
                               diagnostics_dir: Path) -> None:
    if not frames or frames.get('source_kind') != SCREEN_SOURCE:
        return
    masks = {}
    for name in names:
        binding = screen_frame_binding(image_dir/name)
        if binding is None:
            raise ValueError('screen_map_frame_binding_required')
        masks[name] = read_mask(Path(binding['mask_path']))
    filter_features(feature_path, masks, image_paths={n: image_dir/n for n in names}, diagnostics_dir=diagnostics_dir)


def query_screen_binding(photo: Path, manifest: dict, frames_path: Path | None,
                         calibration: Path | None, capture_time: str | None) -> dict | None:
    binding = screen_frame_binding(photo, frames_path)
    map_frames = manifest.get('frames_manifest') or {}
    if map_frames.get('source_kind') == SCREEN_SOURCE and binding is None:
        raise ValueError('screen_query_frames_manifest_required_for_mask_and_provenance')
    if binding:
        if calibration:
            raise ValueError('screen_rectification_not_supported_without_verified_display_geometry')
        if capture_time is not None:
            raise ValueError('screen_display_pts_are_not_sensor_capture_time')
        if map_frames.get('probe', {}).get('source_sha256') == binding['source_sha256']:
            raise ValueError('screen_query_requires_independent_recording')
    return binding
