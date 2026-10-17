"""Frozen-map query localization with strict quality gates."""

from __future__ import annotations

import json
import os
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from .input_safety import validate_image_path
from .pipeline_errors import PipelineInputError, PipelineUnavailable
from .schemas import LocalizationResult
from .evidence import verify_map, export_rows, make_binding, row_diagnostics, write_json, digest_json, resolve_map_path
from .query_geometry import prepare_query, original_coordinates


@dataclass
class LocalizationConfig:
    top_k: int = 20
    min_2d3d: int = 6
    min_inliers: int = 6
    min_inlier_ratio: float = 0.25
    max_reprojection_px: float = 4.0
    allow_map_update: bool = False
    query_camera_model: str | None = None
    query_camera_params: list[float] | None = None
    calibration_path: Path | None = None
    capture_time: str | None = None
    frames_manifest_path: Path | None = None


def _require_backend():
    try:
        import pycolmap
        from hloc import extract_features, localize_sfm, match_features, pairs_from_retrieval
    except ImportError as exc:
        raise PipelineUnavailable("localization_backend_missing") from exc
    from .mapping import _read_image_unicode
    extract_features.read_image = _read_image_unicode
    return pycolmap, extract_features, localize_sfm, match_features, pairs_from_retrieval


def _pose_matrix(cam_from_world) -> list[list[float]]:
    rotation = np.asarray(cam_from_world.rotation.matrix(), dtype=float)
    translation = np.asarray(cam_from_world.translation, dtype=float).reshape(3)
    r_wc = rotation.T
    t_wc = -r_wc @ translation
    matrix = np.eye(4, dtype=float)
    matrix[:3, :3] = r_wc
    matrix[:3, 3] = t_wc
    return matrix.tolist()


def _quality(ret: dict[str, Any], total_2d3d: int) -> dict[str, Any]:
    mask = np.asarray(ret.get("inlier_mask", []), dtype=bool)
    inliers = int(ret.get("num_inliers", int(mask.sum())))
    ratio = float(inliers / max(total_2d3d, 1))
    reproj = ret.get("inlier_errors")
    if reproj is not None:
        reproj = float(np.mean(np.asarray(reproj, dtype=float)))
    return {"num_2d3d": total_2d3d, "num_inliers": inliers, "inlier_ratio": ratio, "mean_inlier_reprojection_px": reproj}


def _passes_quality(quality, config):
    return (quality['num_2d3d'] >= config.min_2d3d
            and quality['num_inliers'] >= config.min_inliers
            and quality['inlier_ratio'] >= config.min_inlier_ratio)


# Additional agreement gates for recovery only. These do not change PnP,
# navigation entry radii, typical_step, or live-current freshness thresholds.
REFERENCE_CONSENSUS_POLICY = {
    'max_rotation_deg': 5.0, 'max_translation_depth_fraction': 0.05,
    'max_focal_ratio': 1.25, 'min_reciprocal_inlier_fraction': 0.5,
    'reference_budget': 'top_k', 'partners_per_seed': 1,
}


def _consistent_hypotheses(a, b, config):
    """Reciprocal image evidence AND pose agreement, not spatial nearest-neighbor.

    Translation is relative to observed scene depth, never an invented metric
    scale. The pose/focal checks reject planar focal-distance degeneracies that
    can agree in pixel space despite describing very different camera centers.
    """
    ta, tb = (np.asarray(_pose_matrix(c['ret']['cam_from_world'])) for c in (a, b))
    if not np.isfinite(ta).all() or not np.isfinite(tb).all():
        return False
    angle = np.degrees(np.arccos(np.clip((np.trace(ta[:3,:3].T @ tb[:3,:3])-1)/2, -1, 1)))
    if angle > REFERENCE_CONSENSUS_POLICY['max_rotation_deg']:
        return False
    # All hypotheses start from the same camera model and supplied intrinsics.
    # fx (or the single focal parameter) must remain mutually compatible too.
    focals = [float(c['camera'].params[0]) for c in (a, b)]
    if min(focals) <= 0 or max(focals)/min(focals) > REFERENCE_CONSENSUS_POLICY['max_focal_ratio']:
        return False
    for observer, source, transform, source_transform in ((a,b,ta,tb), (b,a,tb,ta)):
        # Count a query feature once, even if it has several map-point hypotheses.
        unique = {}
        for row in sorted(source['rows'], key=lambda r: r.get('reprojection_px') or 0):
            if (row['pnp_inlier'] and row['positive_depth'] and row['reprojection_px'] is not None
                    and row['reprojection_px'] <= config.max_reprojection_px):
                unique.setdefault(row['query_keypoint_index'], row)
        rows = list(unique.values())
        if len(rows) < config.min_inliers:
            return False
        xyz = np.asarray([r['xyz'] for r in rows])
        source_depth = ((xyz-source_transform[:3,3]) @ source_transform[:3,:3])[:,2]
        depth = float(np.median(source_depth))
        if depth <= 0 or np.linalg.norm(ta[:3,3]-tb[:3,3]) > depth*REFERENCE_CONSENSUS_POLICY['max_translation_depth_fraction']:
            return False
        camera_xyz = (xyz-transform[:3,3]) @ transform[:3,:3]
        pixels = np.asarray(observer['camera'].img_from_cam(camera_xyz))
        errors = np.linalg.norm(pixels-np.asarray([r['uv_colmap'] for r in rows]), axis=1)
        valid = np.isfinite(errors) & (camera_xyz[:,2] > 0) & (errors <= config.max_reprojection_px)
        if valid.sum() < config.min_inliers or valid.mean() < REFERENCE_CONSENSUS_POLICY['min_reciprocal_inlier_fraction']:
            return False
    return True


def _reference_consensus_candidates(clusters, evidence, solve, config):
    """Bounded recovery from query-mismatched transitive covisibility clusters.

    Select reference hypotheses, not individual RANSAC inliers. Re-solve each
    compatible pair using ALL of its original correspondences and the unchanged
    quality gates. A single good reference is never sufficient for recovery.
    """
    support = {}
    for component, cluster in enumerate(clusters):
        if len(cluster) < 2:
            continue
        for row in evidence[component]['rows']:
            for ref in row['references']:
                support.setdefault((component, ref['image_id']), set()).add(row['query_keypoint_index'])
    ranked = sorted(support, key=lambda key: (-len(support[key]), key))[:max(0, config.top_k)]
    seeds = []
    for component, image_id in ranked:
        if len(support[(component,image_id)]) < config.min_2d3d:
            continue
        candidate = solve([image_id], 'reference_hypothesis')
        if candidate and _passes_quality(candidate['quality'], config):
            seeds.append((component, image_id, candidate))
    seeds.sort(key=lambda s: (-s[2]['quality']['num_inliers'], s[0], s[1]))
    pairs, seen = [], set()
    for component, image_id, seed in seeds:
        partner = next((s for s in seeds if s[0] == component and s[1] != image_id
                        and _consistent_hypotheses(seed, s[2], config)), None)
        if partner is None:
            continue
        ids = tuple(sorted((image_id, partner[1])))
        if ids in seen:
            continue
        seen.add(ids)
        joint = solve(list(ids), 'query_reference_consensus')
        if not joint:
            continue
        joint['quality']['covisibility_cluster_index'] = component
        joint['quality']['consensus_seed_clusters'] = [seed['cluster_index'], partner[2]['cluster_index']]
        if (_passes_quality(joint['quality'], config)
                and _consistent_hypotheses(joint, seed, config)
                and _consistent_hypotheses(joint, partner[2], config)):
            pairs.append(joint)
    return sorted(pairs, key=lambda c: (-c['quality']['num_inliers'], c['cluster_index']))


def _save_result(output_dir: Path, result: LocalizationResult) -> LocalizationResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "localization_result.json").write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result


class _CachedClusterSolver:
    """Pinned hloc pose_from_cluster input assembly with per-query IO reuse.

    Adapted from Apache-2.0 hloc; see THIRD_PARTY_NOTICES.md.

    Retains hloc's +0.5 coordinates, insertion order, conflicting 3D hypotheses
    and db-offset lists. The upstream QueryLocalizer/PyCOLMAP estimator is called
    for EVERY candidate with unchanged options and order; estimated poses are
    deliberately NOT cached because that would change the random-solver stream.
    """
    def __init__(self, localizer, query_name, features_path, read_matches):
        from hloc.utils.io import get_keypoints
        self.localizer = localizer
        self.keypoints = get_keypoints(features_path, query_name)+0.5
        self.read_matches = read_matches
        self.references = {}

    def __call__(self, camera, db_ids):
        ids_by_keypoint = defaultdict(list)
        db_by_keypoint = defaultdict(lambda: defaultdict(list))
        num_matches = 0
        for offset, db_id in enumerate(db_ids):
            if db_id not in self.references:
                image = self.localizer.reconstruction.images[db_id]
                if image.num_points3D == 0:
                    self.references[db_id] = []
                else:
                    pids = np.array([p.point3D_id if p.has_point3D() else -1 for p in image.points2D])
                    matches, _ = self.read_matches(image.name)
                    matches = matches[pids[matches[:,1]] != -1]
                    self.references[db_id] = [(idx,pids[m]) for idx,m in matches]
            matches = self.references[db_id]; num_matches += len(matches)
            for idx, pid in matches:
                db_by_keypoint[idx][pid].append(offset)
                if pid not in ids_by_keypoint[idx]:
                    ids_by_keypoint[idx].append(pid)
        idxs = list(ids_by_keypoint)
        indices = [i for i in idxs for _ in ids_by_keypoint[i]]
        pids = [pid for i in idxs for pid in ids_by_keypoint[i]]
        ret = self.localizer.localize(self.keypoints, indices, pids, camera)
        if ret is not None:ret['camera'] = camera
        return ret, dict(db=db_ids, PnP_ret=ret, keypoints_query=self.keypoints[indices],
            points3D_ids=pids, points3D_xyz=None, num_matches=num_matches,
            keypoint_index_to_db=(indices,[(pid,db_by_keypoint[i][pid])
                                          for i in idxs for pid in ids_by_keypoint[i]]))


def localize_photo(query_path: Path, map_dir: Path, output_dir: Path, config: LocalizationConfig | None = None,
                   *, resident=None, reference_names=None) -> LocalizationResult:
    total_started = time.perf_counter()
    config = config or LocalizationConfig()
    query_path = query_path.resolve()
    map_dir = map_dir.resolve()
    output_dir = output_dir.resolve()
    if output_dir == map_dir or map_dir in output_dir.parents:
        raise PipelineInputError('query_output_overlaps_frozen_map')
    if config.allow_map_update:
        raise PipelineInputError('map_updates_not_supported_in_query')
    if bool(config.query_camera_model) != bool(config.query_camera_params):
        raise PipelineInputError('query_camera_model_and_params_required_together')
    if config.calibration_path and config.query_camera_model:
        raise PipelineInputError('choose_calibration_or_explicit_query_intrinsics')
    if output_dir.exists() and any(output_dir.iterdir()):
        raise PipelineInputError('query_output_not_empty_use_new_output')
    manifest_path = map_dir / "map_manifest.json"
    if not manifest_path.exists():
        return _save_result(output_dir, LocalizationResult.failure(query_id=query_path.name, map_id=map_dir.name, reason="invalid_map_manifest"))
    manifest = resident.verified_manifest(map_dir) if resident else verify_map(map_dir)
    map_id = str(manifest.get("map_id", map_dir.name))
    if not bool(manifest.get("frozen")) and not config.allow_map_update:
        return _save_result(output_dir, LocalizationResult.failure(query_id=query_path.name, map_id=map_id, reason="map_readonly"))
    validation = validate_image_path(query_path, query_path.parent)
    if not validation.ok:
        return _save_result(output_dir, LocalizationResult.failure(query_id=query_path.name, map_id=map_id, reason="invalid_query_" + str(validation.reason)))
    from .feature_masks import query_screen_binding, read_mask, filter_features
    screen_binding = query_screen_binding(query_path, manifest, config.frames_manifest_path,
                                          config.calibration_path, config.capture_time)
    project_root = Path(__file__).resolve().parents[2]
    os.environ.setdefault("TORCH_HOME", str(project_root / ".models" / "torch"))
    pycolmap, extract_features, localize_sfm, match_features, pairs_from_retrieval = _require_backend()
    model_dir = resolve_map_path(map_dir, manifest['model_dir'])
    model = resident.reconstruction if resident else pycolmap.Reconstruction(model_dir)
    ref_image_names = [image.name for image in model.images.values()]
    if not ref_image_names:
        return _save_result(output_dir, LocalizationResult.failure(query_id=query_path.name, map_id=map_id, reason="insufficient_empty_map"))
    output_dir.mkdir(parents=True, exist_ok=True)
    query_workspace = output_dir / "query_workspace"
    query_workspace.mkdir(parents=True, exist_ok=True)
    query_copy, geometry, rectification = prepare_query(query_path, query_workspace, config.calibration_path)
    if screen_binding or resident:
        if screen_binding:
            geometry['screen_observation'] = screen_binding
        # Independent imports often both start at frame_00000000.png. Give hloc
        # a unique internal key without changing the original observation hash.
        from .video import sha256_file
        unique_copy = query_copy.with_name('observation_'+sha256_file(query_path)[:24]+query_copy.suffix)
        query_copy.rename(unique_copy)
        query_copy = unique_copy
    geometry['intrinsics_request'] = {'model': config.query_camera_model,
                                     'params': config.query_camera_params,
                                     'calibration_sha256': geometry.get('calibration_sha256')}
    # hloc's per-workspace image key; rectified queries may all have this name.
    # Original observation identity is binding['query_sha256'], not this key.
    query_name = query_copy.name
    from .mapping import _ascii_stage, _effective_feature_conf
    max_keypoints = int(manifest.get("config", {}).get("max_keypoints", 4096))
    local_conf = _effective_feature_conf(extract_features.confs["superpoint_aachen"], max_keypoints)
    matcher_conf = match_features.confs["superpoint+lightglue"]
    global_query = output_dir / "query-global.h5"
    local_query = output_dir / "query-local.h5"
    query_pairs = output_dir / "pairs-query.txt"
    query_matches = output_dir / "matches-query.h5"
    db_global = resolve_map_path(map_dir, manifest['retrieval_features']) if manifest.get('retrieval_features') else None
    db_local = resolve_map_path(map_dir, manifest['local_features'])
    query_stage = _ascii_stage(query_workspace, [query_name])
    query_image_dir = query_stage or query_workspace
    try:
        if reference_names is not None:
            selected = [n for n in reference_names if n in ref_image_names and n != query_name]
            query_pairs.write_text(''.join(f'{query_name} {n}\n' for n in selected), encoding='utf-8')
        elif db_global:
            retrieval_conf = extract_features.confs["netvlad"]
            (resident.extract if resident else extract_features.main)(retrieval_conf, query_image_dir, output_dir, image_list=[query_name], feature_path=global_query, overwrite=True)
            pairs_from_retrieval.main(global_query, query_pairs, config.top_k, query_list=[query_name], db_model=model_dir, db_descriptors=db_global)
        else:
            query_pairs.write_text("".join(f"{query_name} {name}\n" for name in ref_image_names if name != query_name), encoding="utf-8")
        (resident.extract if resident else extract_features.main)(local_conf, query_image_dir, output_dir, image_list=[query_name], feature_path=local_query, overwrite=True)
    finally:
        if query_stage:
            shutil.rmtree(query_stage, ignore_errors=True)
    if screen_binding:
        filter_features(local_query, {query_name: read_mask(Path(screen_binding['mask_path']))},
                        image_paths={query_name: query_copy}, diagnostics_dir=output_dir/'mask_diagnostics')
    (resident.match if resident else match_features.main)(matcher_conf, query_pairs, features=local_query, features_ref=db_local, matches=query_matches, overwrite=True)
    if config.query_camera_model or rectification is not None:
        try:
            from PIL import Image
            with Image.open(query_copy) as query_image:
                query_width, query_height = query_image.size
        except Exception as exc:
            raise PipelineInputError("query_image_size_unavailable") from exc
        if rectification is not None:
            K = rectification['output_K']
            params = [K[0, 0], K[1, 1], K[0, 2], K[1, 2]]
        else:
            params = config.query_camera_params
        camera = pycolmap.Camera(model='PINHOLE' if rectification is not None else config.query_camera_model,
                                width=query_width, height=query_height, params=np.asarray(params, dtype=float))
        if not camera.verify_params() or not np.isfinite(camera.params).all():
            raise PipelineInputError('invalid_query_camera_parameters')
        query_intrinsics_status = 'calibrated_rectified' if rectification is not None else 'explicit'
    else:
        camera = pycolmap.infer_camera_from_image(query_copy)
        query_intrinsics_status = "self_estimated_not_calibrated"

    # infer_camera_from_image supplies an initialization, not a calibration.
    # PyCOLMAP otherwise fixes that guess during both RANSAC and refinement.
    # Never copy a map camera just because the image dimensions happen to match.
    estimate_focal = not (config.query_camera_model or rectification is not None)
    pose_options = {
        'estimation': {'estimate_focal_length': estimate_focal,
                       'ransac': {'max_error': config.max_reprojection_px}},
        'refinement': {'refine_focal_length': estimate_focal, 'refine_extra_params': False},
    }
    geometry['intrinsics_policy'] = {
        'mode': 'estimate_focal_from_2d3d' if estimate_focal else 'fixed_supplied_intrinsics',
        'initial_camera': {'model': camera.model.name, 'width': camera.width,
                           'height': camera.height, 'params': camera.params.tolist()},
        'pose_options': pose_options,
    }

    retrieval = localize_sfm.parse_retrieval(query_pairs)
    db_ids = []
    for name in retrieval.get(query_name, []):
        image = model.find_image_with_name(name)
        if image is not None:
            db_ids.append(image.image_id)
    if not db_ids:
        return _save_result(output_dir, LocalizationResult.failure(query_id=query_name, map_id=map_id, reason="insufficient_no_retrieved_reference"))
    cluster_started = time.perf_counter()
    clusters = (resident.covisibility_clusters(db_ids) if hasattr(resident,'covisibility_clusters')
                else localize_sfm.do_covisibility_clustering(db_ids, model))
    if hasattr(resident,'_record_stage'):
        resident._record_stage('covisibility_s',cluster_started)
    candidates = []
    all_evidence = []
    started = time.perf_counter()
    localizer = localize_sfm.QueryLocalizer(model, pose_options)
    from hloc.utils.io import get_matches

    @lru_cache(maxsize=None)
    def read_matches(name):
        return get_matches(query_matches, query_name, name)

    cached_solver = (resident.cluster_solver(localizer, query_name, local_query, read_matches)
                     if hasattr(resident, 'cluster_solver') else None)

    def solve(cluster, strategy='covisibility_component'):
        solve_started = time.perf_counter()
        index = len(all_evidence)
        # Each estimator may refine its camera. Preserve the camera used by each pose.
        cluster_camera = pycolmap.Camera(camera.todict())
        if cached_solver is not None:
            ret, log = cached_solver(cluster_camera, cluster)
        else:
            ret, log = localize_sfm.pose_from_cluster(
                localizer, query_name, cluster_camera, cluster, local_query, query_matches)
        if not cluster_camera.verify_params() or not np.isfinite(cluster_camera.params).all():
            raise PipelineInputError('invalid_estimated_query_camera_parameters')
        if hasattr(resident, '_record_stage'):
            resident._record_stage('pnp_inputs_and_solve_s',solve_started,pose_candidates=1)
        export_started = time.perf_counter()
        rows = export_rows(model, log, ret, cluster_camera, read_matches)
        raw_uv = original_coordinates([r['uv'] for r in rows], rectification)
        for row, uv in zip(rows, raw_uv):
            row['uv_original'] = uv.tolist()
        all_evidence.append({'cluster_index': index, 'reference_image_ids': [int(i) for i in cluster],
                             'reference_strategy': strategy,
                             'T_WC_candidate': _pose_matrix(ret['cam_from_world']) if ret is not None else None,
                             'num_matches': int(log['num_matches']), 'rows': rows,
                             'camera': {'model': cluster_camera.model.name, 'width': cluster_camera.width,
                                        'height': cluster_camera.height, 'params': cluster_camera.params.tolist()}})
        if hasattr(resident, '_record_stage'):
            resident._record_stage('export_rows_s',export_started)
        if ret is None:
            return None
        quality = _quality(ret, len(log.get("points3D_ids", [])))
        quality.update(row_diagnostics(rows, camera.width, camera.height))
        quality.update({"cluster_index": index, "retrieved_reference_count": len(cluster), "query_intrinsics": query_intrinsics_status})
        quality['query_camera'] = all_evidence[-1]['camera']
        quality['reference_strategy'] = strategy
        quality['acceptance_thresholds'] = {'min_2d3d': config.min_2d3d, 'min_inliers': config.min_inliers,
            'min_inlier_ratio': config.min_inlier_ratio, 'max_reprojection_px': config.max_reprojection_px}
        return {"ret": ret, "log": log, "quality": quality, "cluster_index": index,
                'camera': cluster_camera, 'rows': rows}

    for cluster in clusters:
        candidate = solve(cluster)
        if candidate:
            candidates.append(candidate)
    recovery = []
    if not any(_passes_quality(c['quality'], config) for c in candidates):
        recovery_started = time.perf_counter()
        recovery = _reference_consensus_candidates(clusters, all_evidence, solve, config)
        if hasattr(resident, '_record_stage'):
            resident._record_stage('consensus_including_solves_s',recovery_started)
        geometry['reference_consensus_policy'] = {**REFERENCE_CONSENSUS_POLICY, 'top_k': config.top_k}
    def finish(result, selected=None):
        finish_started = time.perf_counter()
        result.map_hash = manifest['manifest_sha256']
        result.capture_time = config.capture_time
        result.localization_time = datetime.now(timezone.utc).isoformat()
        result.timing.update({'pnp_and_evidence_s': time.perf_counter() - started,
                              'total_s': time.perf_counter() - total_started})
        binding = make_binding(query_path, manifest,
            {'query_features': local_query, 'matches': query_matches, 'reference_features': db_local,
             'processed_query': query_copy, 'calibration': config.calibration_path,
             'screen_frames_manifest': Path(screen_binding['manifest_path']) if screen_binding else None,
             'feature_exclusion_mask': Path(screen_binding['mask_path']) if screen_binding else None}, geometry)
        result.query_sha256 = binding['query_sha256']  # Original bytes, before rectification.
        result.evidence_path = str(output_dir/'query_evidence.json')
        evidence = {'schema_version': 'go1loc.evidence.v1', 'query_path': str(query_path),
                    'map_id': map_id, 'map_hash': result.map_hash, 'component_id': result.component_id,
                    'binding': binding, 'pose_status': result.pose_status, 'selected_cluster': selected,
                    'capture_time': result.capture_time, 'localization_time': result.localization_time,
                    'map_observation_time': manifest.get('map_observation_time'),
                    'failure_reason': result.failure_reason, 'clusters': all_evidence}
        # Bind exported pose/quality/times as well as image and feature files.
        # Saved current-start import must not accept a hand-edited XYZ result.
        evidence['localization_result_sha256'] = digest_json(result.to_dict())
        evidence['evidence_sha256'] = digest_json(evidence)
        # The full audit evidence is retained; indentation alone is expensive
        # on the resident critical path and does not participate in JSON digests.
        write_json(output_dir/'query_evidence.json', evidence, compact=resident is not None)
        if hasattr(resident, '_record_stage'):
            resident._record_stage('seal_and_write_evidence_s',finish_started)
        return _save_result(output_dir, result)
    if recovery:
        best = recovery[0]
        alternatives = [c for c in recovery[1:]
                        if c['quality']['num_inliers'] >= 0.9*best['quality']['num_inliers']
                        and (c['quality']['covisibility_cluster_index'] != best['quality']['covisibility_cluster_index']
                             or not _consistent_hypotheses(best, c, config))]
        if alternatives:
            result = LocalizationResult.failure(query_id=query_name, map_id=map_id, reason='ambiguous_reference_consensus')
            result.quality = {'best': best['quality'], 'alternatives': [c['quality'] for c in alternatives]}
            return finish(result, best['cluster_index'])
        candidates = [best]
    if not candidates:
        result = LocalizationResult.failure(query_id=query_name, map_id=map_id, reason="insufficient_pnp_evidence")
        result.quality = {"retrieved_reference_count": len(db_ids), "candidate_clusters": len(clusters)}
        return finish(result)
    candidates.sort(key=lambda c: c["quality"]["num_inliers"], reverse=True)
    best = candidates[0]
    quality = best["quality"]
    if quality["num_2d3d"] < config.min_2d3d or quality["num_inliers"] < config.min_inliers or quality["inlier_ratio"] < config.min_inlier_ratio:
        result = LocalizationResult.failure(query_id=query_name, map_id=map_id, reason="insufficient_pnp_quality")
        result.quality = quality
        return finish(result, best['cluster_index'])
    if len(candidates) > 1 and candidates[1]["quality"]["num_inliers"] >= 0.9 * quality["num_inliers"]:
        result = LocalizationResult.failure(query_id=query_name, map_id=map_id, reason="ambiguous_multiple_covisibility_clusters")
        result.quality = {"best": quality, "alternatives": [c["quality"] for c in candidates[1:]]}
        return finish(result, best['cluster_index'])
    component_id = None
    best_image_ids = set(all_evidence[best['cluster_index']]['reference_image_ids'])
    for component in manifest.get("components", []):
        component_image_ids = {int(image_id) for image_id in component.get("image_ids", [])}
        if best_image_ids & component_image_ids:
            component_id = component.get("component_id")
            break
    result = LocalizationResult(
        query_id=query_name,
        map_id=map_id,
        component_id=component_id,
        mode="photo",
        pose_status="localized",
        object_status="insufficient_evidence",
        T_WC_query=_pose_matrix(best["ret"]["cam_from_world"]),
        quality=quality,
        map_observation_time=manifest.get("map_observation_time"),
        units=manifest.get("units", "map_units"),
        scale_status=manifest.get("scale_status", "arbitrary_monocular_scale"),
        timing={"localization_s": time.perf_counter() - started},
    )
    return finish(result, best['cluster_index'])
