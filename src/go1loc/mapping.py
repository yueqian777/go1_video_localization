"""Real hloc + LightGlue + pycolmap mapping adapter.

This wrapper deliberately contains no synthetic fallback. If the learned
backend is missing, the command fails with an actionable dependency status.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .dependencies import git_revision
from .pipeline_errors import PipelineInputError, PipelineUnavailable
from .video import sha256_file


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".JPG", ".JPEG", ".PNG", ".WEBP"}


def _read_image_unicode(path, grayscale=False):
    """OpenCV's Windows narrow-path reader can fail on Chinese workspace paths."""
    import cv2
    import numpy as np
    flags = cv2.IMREAD_GRAYSCALE if grayscale else cv2.IMREAD_COLOR
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), flags | cv2.IMREAD_IGNORE_ORIENTATION)
    if image is None:
        raise ValueError(f"Cannot read image {path}.")
    if not grayscale and image.ndim == 3:
        image = image[:, :, ::-1]
    return image


@dataclass
class MappingConfig:
    retrieval: str = "netvlad"
    local_features: str = "superpoint_aachen"
    matcher: str = "superpoint+lightglue"
    num_retrieved: int = 20
    exhaustive_limit: int = 80
    camera_mode: str = "AUTO"
    map_id: str = "go1-map"
    map_observation_time: str | None = None
    calibrated_camera_model: str | None = None
    calibrated_camera_params: list[float] | None = None
    temporal_window: int = 5
    refine_calibration: bool = False
    max_keypoints: int = 4096


def _effective_feature_conf(base_conf: dict[str, Any], max_keypoints: int) -> dict[str, Any]:
    """Return an isolated hloc feature config with a bounded keypoint budget."""
    if max_keypoints < 256:
        raise PipelineInputError("max_keypoints_must_be_at_least_256")
    conf = deepcopy(base_conf)
    model = conf.setdefault("model", {})
    if "max_keypoints" in model:
        model["max_keypoints"] = int(max_keypoints)
        output = str(conf["output"])
        conf["output"] = output.replace("n4096", f"n{int(max_keypoints)}")
    return conf


def _images(image_dir: Path) -> list[str]:
    return sorted(
        p.relative_to(image_dir).as_posix()
        for p in image_dir.rglob("*")
        if p.is_file() and p.suffix in IMAGE_SUFFIXES
    )


def _ascii_stage(image_dir: Path, refs: list[str]) -> Path | None:
    """Stage only the current image set on an ASCII D: path for Windows C++/OpenCV."""
    if str(image_dir).isascii():
        return None
    stage = Path(tempfile.mkdtemp(prefix="go1loc-stage-", dir=image_dir.anchor or None))
    for name in refs:
        destination = stage / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(image_dir / name, destination)
    return stage


def temporal_pairs(refs: list[str], window: int) -> list[tuple[str,str]]:
    if window < 1:
        raise PipelineInputError('invalid_temporal_window')
    return [(a,refs[j]) for i,a in enumerate(refs) for j in range(i+1,min(i+window+1,len(refs)))]


def _require_backend():
    missing = []
    for name in ("torch", "h5py", "pycolmap"):
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    try:
        from hloc import extract_features, match_features, pairs_from_exhaustive, pairs_from_retrieval, reconstruction
    except ImportError as exc:
        raise PipelineUnavailable("hloc_or_dependencies_missing") from exc
    try:
        import pycolmap
    except ImportError as exc:
        raise PipelineUnavailable("pycolmap_missing") from exc
    if missing:
        raise PipelineUnavailable("missing_pipeline_dependencies:" + ",".join(missing))
    extract_features.read_image = _read_image_unicode
    return pycolmap, extract_features, match_features, pairs_from_exhaustive, pairs_from_retrieval, reconstruction


def _connected_components(model) -> list[list[int]]:
    adjacency: dict[int, set[int]] = {int(i): set() for i in model.images}
    for point in model.points3D.values():
        ids = [int(track.image_id) for track in point.track.elements]
        for a in ids:
            adjacency.setdefault(a, set()).update(b for b in ids if b != a)
    components: list[list[int]] = []
    unseen = set(adjacency)
    while unseen:
        seed = unseen.pop()
        component = [seed]
        queue = [seed]
        while queue:
            current = queue.pop()
            for nxt in adjacency.get(current, ()) & unseen:
                unseen.remove(nxt)
                queue.append(nxt)
                component.append(nxt)
        components.append(sorted(component))
    return sorted(components, key=len, reverse=True)


def _write_manifest(output_dir: Path, config: MappingConfig, image_dir: Path, model, refs: list[str], timings: dict[str, float], retrieval_features: Path | None, local_features: Path, retrieval_mode: str) -> dict[str, Any]:
    components = _connected_components(model)
    component_records = [
        {"component_id": f"{config.map_id}:component:{i}", "image_ids": ids, "num_images": len(ids)}
        for i, ids in enumerate(components)
    ]
    upstream_dir = Path(__file__).resolve().parents[2] / ".upstream"
    registered = {image.name for image in model.images.values()}
    model.export_PLY(output_dir/'points3D.ply')
    model.write_text(output_dir/'sfm')
    poses = {image.name:{'image_id':int(image.image_id),'camera_id':int(image.camera_id),'T_CW':image.cam_from_world().matrix().tolist()} for image in model.images.values()}
    (output_dir/'reference_poses.json').write_text(json.dumps(poses,indent=2),encoding='utf-8')
    other_models = [str(p.relative_to(output_dir)) for p in (output_dir/'sfm'/'models').iterdir() if (p/'images.bin').exists()]
    manifest = {
        "schema_version": "go1loc.map.v1",
        "map_id": config.map_id,
        "frozen": True,
        "image_dir": str(image_dir.resolve()),
        "reference_images": refs,
        "unregistered_images": sorted(set(refs)-registered),
        "other_unaligned_models": other_models,
        "other_models_policy": "preserved_not_merged_or_queried_in_main_coordinate_system",
        "native_model_sha256": {p.name:sha256_file(p) for p in (output_dir/'sfm').glob('*.bin')},
        "reference_image_sha256": {n:sha256_file(image_dir/n) for n in refs},
        "model_dir": str((output_dir / "sfm").resolve()),
        "retrieval_features": str(retrieval_features.resolve()) if retrieval_features else None,
        "retrieval_mode": retrieval_mode,
        "local_features": str(local_features.resolve()),
        "components": component_records,
        "num_registered_images": int(model.num_reg_images()),
        "num_points3D": int(model.num_points3D()),
        "reconstruction_summary": str(model.summary()),
        "map_observation_time": config.map_observation_time,
        "scale_status": "arbitrary_monocular_scale",
        "units": "map_units",
        "config": asdict(config),
        "timing_s": timings,
        "upstream": {
            "hloc": git_revision(upstream_dir / "Hierarchical-Localization"),
            "lightglue": git_revision(upstream_dir / "LightGlue"),
        },
    }
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    (output_dir / "map_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return manifest


def build_map(image_dir: Path, output_dir: Path, config: MappingConfig | None = None) -> dict[str, Any]:
    config = config or MappingConfig()
    image_dir = image_dir.resolve()
    output_dir = output_dir.resolve()
    if (output_dir/'map_manifest.json').exists():
        raise PipelineInputError('frozen_map_exists_use_new_output')
    if output_dir.exists() and any(output_dir.iterdir()):
        raise PipelineInputError('map_output_not_empty_use_new_output')
    if image_dir==output_dir or image_dir in output_dir.parents:
        raise PipelineInputError('map_output_inside_image_directory')
    project_root = Path(__file__).resolve().parents[2]
    os.environ.setdefault("TORCH_HOME", str(project_root / ".models" / "torch"))
    refs = _images(image_dir)
    if len(refs) < 2:
        raise PipelineInputError("mapping_requires_at_least_two_images")
    pycolmap, extract_features, match_features, pairs_from_exhaustive, pairs_from_retrieval, reconstruction = _require_backend()
    output_dir.mkdir(parents=True, exist_ok=True)
    stage_dir = _ascii_stage(image_dir, refs)
    work_image_dir = stage_dir or image_dir
    started = time.perf_counter()
    timings: dict[str, float] = {}
    feature_conf = _effective_feature_conf(extract_features.confs[config.local_features], config.max_keypoints)
    matcher_conf = match_features.confs[config.matcher]
    global_path = None
    local_path = output_dir / f"{feature_conf['output']}.h5"
    match_path = output_dir / f"{feature_conf['output']}_{matcher_conf['output']}.h5"
    pairs_path = output_dir / "pairs-mapping.txt"

    retrieval_mode = "exhaustive"
    frame_manifest = image_dir.parent/'frames_manifest.json'
    if frame_manifest.exists():
        frame_data = json.loads(frame_manifest.read_text(encoding='utf-8'))
        geom = frame_data['geometry']
        if geom['status']=='calibrated_rectified' and not config.calibrated_camera_model:
            K=geom['output_K']
            config.calibrated_camera_model='PINHOLE'
            config.calibrated_camera_params=[K[0][0],K[1][1],K[0][2],K[1][2]]
        config.camera_mode='SINGLE'
        retrieval_mode = "temporal_plus_exhaustive" if len(refs) <= config.exhaustive_limit else "netvlad_plus_temporal"
    if len(refs) > config.exhaustive_limit:
        retrieval_conf = extract_features.confs[config.retrieval]
        global_path = output_dir / f"{retrieval_conf['output']}.h5"
        extract_features.main(retrieval_conf, work_image_dir, output_dir, image_list=refs, feature_path=global_path)
        timings["global_features"] = time.perf_counter() - started
        retrieval_mode = "netvlad_plus_temporal" if frame_manifest.exists() else "netvlad"
        if frame_manifest.exists():
            pairs_from_retrieval.main(global_path, output_dir/'pairs-retrieval.txt', config.num_retrieved, query_list=refs, db_list=refs)
            pair_set = {tuple(sorted(pair)) for pair in temporal_pairs(refs,config.temporal_window)}
            pair_set.update(tuple(sorted(line.split())) for line in (output_dir/'pairs-retrieval.txt').read_text().splitlines() if line.strip())
            pairs_path.write_text(''.join(f'{a} {b}\n' for a,b in sorted(pair_set)),encoding='utf-8')
        else:
            pairs_from_retrieval.main(global_path, pairs_path, config.num_retrieved, query_list=refs, db_list=refs)
    elif frame_manifest.exists():
        pairs_path.write_text(''.join(f'{a} {b}\n' for a,b in temporal_pairs(refs,config.temporal_window)),encoding='utf-8')
    else:
        pairs_from_exhaustive.main(pairs_path, image_list=refs)
    extract_features.main(feature_conf, work_image_dir, output_dir, image_list=refs, feature_path=local_path)
    match_features.main(matcher_conf, pairs_path, features=local_path, matches=match_path)
    timings["features_and_matches"] = time.perf_counter() - started

    sfm_dir = output_dir / "sfm"
    image_options: dict[str, Any] = {}
    if config.calibrated_camera_model:
        image_options["camera_model"] = config.calibrated_camera_model
        image_options["camera_params"] = ",".join(map(str, config.calibrated_camera_params or []))
    camera_mode = getattr(pycolmap.CameraMode, config.camera_mode)
    model = reconstruction.main(
        sfm_dir,
        work_image_dir,
        pairs_path,
        local_path,
        match_path,
        image_list=refs,
        camera_mode=camera_mode,
        image_options=image_options or None,
        mapper_options={'ba_refine_focal_length':not config.calibrated_camera_model or config.refine_calibration,'ba_refine_extra_params':not config.calibrated_camera_model or config.refine_calibration},
    )
    timings["reconstruction"] = time.perf_counter() - started
    if model is None or int(model.num_reg_images()) < 2 or int(model.num_points3D()) == 0:
        raise PipelineInputError("sfm_did_not_produce_a_3d_model")
    manifest = _write_manifest(output_dir, config, image_dir, model, refs, timings, global_path, local_path, retrieval_mode)
    if stage_dir:
        shutil.rmtree(stage_dir, ignore_errors=True)
    return manifest
