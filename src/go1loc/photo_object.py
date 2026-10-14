"""Photo + map + original-image ROI/mask -> verified visible 3D surface support."""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
from .evidence import verify_map, verify_binding, digest_json, write_json
from .object_grounding import support_from_roi_matches
from .video import sha256_file


def ground_evidence(evidence, roi, *, mask=None, cluster_radius=None, min_points=3,
                    max_reprojection_px=4.0):
    if evidence.get('pose_status') != 'localized':
        return {'status': 'insufficient_evidence', 'failure_reason': 'query_not_localized'}
    cluster = next((c for c in evidence['clusters'] if c['cluster_index'] == evidence['selected_cluster']), None)
    if cluster is None:
        return {'status': 'insufficient_evidence', 'failure_reason': 'no_selected_evidence'}
    observations, points = [], {}
    for r in cluster['rows']:
        if not r['pnp_inlier'] or not r['positive_depth'] or r['reprojection_px'] is None:
            continue
        if not np.isfinite(r['reprojection_px']) or r['reprojection_px'] > max_reprojection_px:
            continue
        u, v = r['uv_original']
        size = evidence.get('binding', {}).get('geometry', {}).get('source_size')
        if size and not (0 <= u < size[0] and 0 <= v < size[1]):
            continue
        if not np.isfinite([u, v]).all() or not (roi[0] <= u <= roi[2] and roi[1] <= v <= roi[3]):
            continue
        if mask is not None:
            x, y = int(np.floor(u+.5)), int(np.floor(v+.5))
            if not (0 <= y < mask.shape[0] and 0 <= x < mask.shape[1] and mask[y, x]):
                continue
        xyz = np.asarray(r['xyz'], dtype=float)
        if xyz.shape != (3,) or not np.isfinite(xyz).all():
            continue
        pid = r['point3D_id']; points[pid] = xyz
        observations.extend({'u': u, 'v': v, 'point3D_id': pid, 'frame_id': ref['image_name']}
                            for ref in r['references'])
    automatic = cluster_radius is None
    if automatic:
        if len(points) < min_points:
            return {'status': 'insufficient_evidence', 'failure_reason': 'insufficient_roi_3d_support'}
        xyz = np.array(list(points.values()))
        # Nearest-neighbor spacing scales with the SfM model; it is not meters.
        distances = []
        for p in xyz:
            d = np.linalg.norm(xyz-p, axis=1)
            positive = d[d > 0]
            if len(positive):
                distances.append(float(positive.min()))
        if not distances:
            return {'status': 'insufficient_evidence', 'failure_reason': 'degenerate_coincident_support'}
        cluster_radius = 2.5*float(np.median(distances))
    support = support_from_roi_matches(roi=tuple(roi), observations=observations, points3d=points,
                                      min_points=min_points, cluster_radius=cluster_radius)
    return {**support.__dict__, 'cluster_radius': cluster_radius,
            'clustering_policy': '2.5_median_nearest_neighbor_map_units' if automatic else 'explicit_map_units',
            'selection_semantics': 'visible_surface_support_not_full_object_center',
            'warnings': ['ROI can include background; supported geometry is not semantic object recognition.',
                         'Historical observations do not prove the object is still present.']}


def locate_photo_object(photo: Path, map_dir: Path, output_dir: Path, roi,
                        mask_path=None, query_dir=None, cluster_radius=None, min_points=3, config=None):
    from PIL import Image
    from .localization import localize_photo
    photo, map_dir, output_dir = Path(photo).resolve(), Path(map_dir).resolve(), Path(output_dir).resolve()
    if output_dir == map_dir or map_dir in output_dir.parents:
        raise ValueError('object_output_overlaps_frozen_map')
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError('object_output_not_empty_use_new_output')
    with Image.open(photo) as image:
        width, height = image.size
    if len(roi) != 4 or not np.isfinite(roi).all() or not (0 <= roi[0] < roi[2] <= width and 0 <= roi[1] < roi[3] <= height):
        raise ValueError('invalid_roi_original_image_bounds')
    manifest = verify_map(map_dir)
    mask = None
    if mask_path:
        with Image.open(mask_path) as image:
            if image.size != (width, height):
                raise ValueError('mask_original_image_size_mismatch')
            mask = np.asarray(image.convert('L')) > 0
    if query_dir is None:
        query_dir = output_dir/'query'
        result = localize_photo(photo, map_dir, query_dir, config)
        if result.pose_status != 'localized' and not (query_dir/'query_evidence.json').exists():
            payload = {'status': 'insufficient_evidence', 'failure_reason': result.failure_reason,
                       'execution_permitted': False}
            write_json(output_dir/'object_result.json', payload)
            return payload
    else:
        query_dir = Path(query_dir).resolve()
    evidence = json.loads((query_dir/'query_evidence.json').read_text(encoding='utf-8'))
    unsigned = {k: v for k, v in evidence.items() if k != 'evidence_sha256'}
    if digest_json(unsigned) != evidence.get('evidence_sha256'):
        raise ValueError('evidence_content_hash_mismatch')
    verify_binding(evidence['binding'], photo, manifest['manifest_sha256'])
    if evidence['binding']['geometry'].get('source_size') != [width, height]:
        raise ValueError('evidence_source_image_size_mismatch')
    if config and (config.query_camera_model or config.query_camera_params):
        requested = {'model': config.query_camera_model, 'params': config.query_camera_params,
                     'calibration_sha256': None}
        if evidence['binding']['geometry'].get('intrinsics_request') != requested:
            raise ValueError('evidence_query_intrinsics_mismatch')
    if config and config.calibration_path:
        if evidence['binding']['geometry'].get('calibration_sha256') != sha256_file(config.calibration_path):
            raise ValueError('evidence_calibration_mismatch')
    payload = ground_evidence(evidence, roi, mask=mask, cluster_radius=cluster_radius, min_points=min_points)
    roi_binding = {'roi': list(roi), 'mask_sha256': sha256_file(Path(mask_path)) if mask_path else None,
                   'query_sha256': evidence['binding']['query_sha256'], 'evidence_sha256': evidence['evidence_sha256']}
    # query_id here is a display basename, unlike localization's processed key.
    # Both exports retain the SAME verified raw-image hash for identity checks.
    payload.update({'schema_version': 'go1loc.object.v1', 'query_path': str(photo),
                    'query_id': photo.name, 'query_sha256': evidence['binding']['query_sha256'],
                    'query_dir': str(query_dir), 'roi_binding': roi_binding, 'roi_sha256': digest_json(roi_binding),
                    'map_id': manifest['map_id'], 'map_hash': manifest['manifest_sha256'],
                    'component_id': evidence.get('component_id'), 'units': manifest['units'],
                    'capture_time': evidence.get('capture_time'), 'localization_time': evidence.get('localization_time'),
                    'map_observation_time': manifest.get('map_observation_time'), 'execution_permitted': False})
    write_json(output_dir/'object_result.json', payload)
    return payload
