"""Frozen-map query localization with strict quality gates."""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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


def _save_result(output_dir: Path, result: LocalizationResult) -> LocalizationResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "localization_result.json").write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result


def localize_photo(query_path: Path, map_dir: Path, output_dir: Path, config: LocalizationConfig | None = None) -> LocalizationResult:
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
    manifest = verify_map(map_dir)
    map_id = str(manifest.get("map_id", map_dir.name))
    if not bool(manifest.get("frozen")) and not config.allow_map_update:
        return _save_result(output_dir, LocalizationResult.failure(query_id=query_path.name, map_id=map_id, reason="map_readonly"))
    validation = validate_image_path(query_path, query_path.parent)
    if not validation.ok:
        return _save_result(output_dir, LocalizationResult.failure(query_id=query_path.name, map_id=map_id, reason="invalid_query_" + str(validation.reason)))
    project_root = Path(__file__).resolve().parents[2]
    os.environ.setdefault("TORCH_HOME", str(project_root / ".models" / "torch"))
    pycolmap, extract_features, localize_sfm, match_features, pairs_from_retrieval = _require_backend()
    model_dir = resolve_map_path(map_dir, manifest['model_dir'])
    model = pycolmap.Reconstruction(model_dir)
    ref_image_names = [image.name for image in model.images.values()]
    if not ref_image_names:
        return _save_result(output_dir, LocalizationResult.failure(query_id=query_path.name, map_id=map_id, reason="insufficient_empty_map"))
    output_dir.mkdir(parents=True, exist_ok=True)
    query_workspace = output_dir / "query_workspace"
    query_workspace.mkdir(parents=True, exist_ok=True)
    query_copy, geometry, rectification = prepare_query(query_path, query_workspace, config.calibration_path)
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
        if db_global:
            retrieval_conf = extract_features.confs["netvlad"]
            extract_features.main(retrieval_conf, query_image_dir, output_dir, image_list=[query_name], feature_path=global_query, overwrite=True)
            pairs_from_retrieval.main(global_query, query_pairs, config.top_k, query_list=[query_name], db_model=model_dir, db_descriptors=db_global)
        else:
            query_pairs.write_text("".join(f"{query_name} {name}\n" for name in ref_image_names if name != query_name), encoding="utf-8")
        extract_features.main(local_conf, query_image_dir, output_dir, image_list=[query_name], feature_path=local_query, overwrite=True)
    finally:
        if query_stage:
            shutil.rmtree(query_stage, ignore_errors=True)
    match_features.main(matcher_conf, query_pairs, features=local_query, features_ref=db_local, matches=query_matches, overwrite=True)
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
        query_intrinsics_status = "inferred_or_approximate"

    retrieval = localize_sfm.parse_retrieval(query_pairs)
    db_ids = []
    for name in retrieval.get(query_name, []):
        image = model.find_image_with_name(name)
        if image is not None:
            db_ids.append(image.image_id)
    if not db_ids:
        return _save_result(output_dir, LocalizationResult.failure(query_id=query_name, map_id=map_id, reason="insufficient_no_retrieved_reference"))
    clusters = localize_sfm.do_covisibility_clustering(db_ids, model)
    candidates = []
    all_evidence = []
    started = time.perf_counter()
    localizer = localize_sfm.QueryLocalizer(
        model, {"estimation": {"ransac": {"max_error": config.max_reprojection_px}}}
    )
    for index, cluster in enumerate(clusters):
        # Each estimator may refine its camera. Preserve the camera used by each pose.
        cluster_camera = pycolmap.Camera(camera.todict())
        ret, log = localize_sfm.pose_from_cluster(
            localizer,
            query_name,
            cluster_camera,
            cluster,
            local_query,
            query_matches,
        )
        from hloc.utils.io import get_matches
        rows = export_rows(model, log, ret, cluster_camera,
                           lambda name: get_matches(query_matches, query_name, name))
        raw_uv = original_coordinates([r['uv'] for r in rows], rectification)
        for row, uv in zip(rows, raw_uv):
            row['uv_original'] = uv.tolist()
        all_evidence.append({'cluster_index': index, 'reference_image_ids': [int(i) for i in cluster],
                             'num_matches': int(log['num_matches']), 'rows': rows,
                             'camera': {'model': cluster_camera.model.name, 'width': cluster_camera.width,
                                        'height': cluster_camera.height, 'params': cluster_camera.params.tolist()}})
        if ret is None:
            continue
        quality = _quality(ret, len(log.get("points3D_ids", [])))
        quality.update(row_diagnostics(rows, camera.width, camera.height))
        quality.update({"cluster_index": index, "retrieved_reference_count": len(cluster), "query_intrinsics": query_intrinsics_status})
        candidates.append({"ret": ret, "log": log, "quality": quality, "cluster_index": index})
    def finish(result, selected=None):
        result.map_hash = manifest['manifest_sha256']
        result.capture_time = config.capture_time
        result.localization_time = datetime.now(timezone.utc).isoformat()
        result.timing.update({'pnp_and_evidence_s': time.perf_counter() - started,
                              'total_s': time.perf_counter() - total_started})
        binding = make_binding(query_path, manifest,
            {'query_features': local_query, 'matches': query_matches, 'reference_features': db_local,
             'processed_query': query_copy, 'calibration': config.calibration_path}, geometry)
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
        write_json(output_dir/'query_evidence.json', evidence)
        return _save_result(output_dir, result)
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
    best_image_ids = {int(image_id) for image_id in clusters[best["cluster_index"]]}
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
