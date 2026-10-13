"""Real hloc + LightGlue + pycolmap mapping adapter.

This wrapper deliberately contains no synthetic fallback. If the learned
backend is missing, the command fails with an actionable dependency status.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
import time
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .dependencies import git_revision
from .build_watch import atomic_json, publish_run
from .events import BoundedEventWriter, outside_artifacts, utc_now
from .pipeline_errors import PipelineInputError, PipelineUnavailable
from .snapshots import SnapshotPublisher, run_incremental_mapping
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
    snapshot_frames_freq: int = 0
    snapshot_max_count: int = 20
    snapshot_max_bytes: int = 512 * 1024 * 1024
    sidecar_dir: str | None = None


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


def _write_manifest(output_dir: Path, config: MappingConfig, image_dir: Path, model, refs: list[str], timings: dict[str, float], retrieval_features: Path | None, local_features: Path, retrieval_mode: str, *, run_id: str | None = None) -> dict[str, Any]:
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
        "build_run_id": run_id,
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
        "local_features_sha256": sha256_file(local_features),
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
    frame_manifest = image_dir.parent / 'frames_manifest.json'
    if frame_manifest.exists():
        frame_bytes = frame_manifest.read_bytes()
        manifest['frames_manifest'] = json.loads(frame_bytes)
        manifest['frames_manifest_sha256'] = hashlib.sha256(frame_bytes).hexdigest()
    else:
        manifest['frames_manifest'] = None
        manifest['frames_manifest_sha256'] = None
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    atomic_json(output_dir / 'map_manifest.json', manifest)
    return manifest


def _video_references(image_dir: Path, refs: list[str]) -> tuple[list[str], dict, dict | None]:
    """Order only validated manifest frames by real PTS, never by filename/id."""
    source = image_dir.parent / 'frames_manifest.json'
    if not source.exists():
        return refs, {}, None
    data = json.loads(source.read_text(encoding='utf-8'))
    times = {}
    for row in data.get('frames', []):
        if not row.get('kept'):
            continue
        path = (source.parent / row['image_path']).resolve()
        if image_dir not in path.parents:
            raise PipelineInputError('frame_manifest_image_outside_image_directory')
        name = path.relative_to(image_dir).as_posix()
        pts = row.get('pts_time_s')
        if not isinstance(pts, (int, float)) or not math.isfinite(pts):
            raise PipelineInputError('frame_manifest_missing_finite_pts')
        if name in times:
            raise PipelineInputError('frame_manifest_duplicate_image')
        times[name] = {key: row.get(key) for key in ('pts', 'pts_time_s', 'relative_time_s', 'frame_index')}
    if set(times) != set(refs) or len({row['pts_time_s'] for row in times.values()}) != len(times):
        raise PipelineInputError('frame_manifest_images_or_pts_mismatch')
    return sorted(refs, key=lambda name: times[name]['pts_time_s']), times, data


def _reconstruct_with_snapshots(backend, sfm_dir, work_image_dir, pairs_path, local_path,
                                match_path, refs, camera_mode, image_options, mapper_options,
                                publisher, config, frame_times, events):
    """Reuse pinned hloc imports/verification; only substitute its mapper driver."""
    import pycolmap
    sfm_dir.mkdir(parents=True, exist_ok=True)
    database = sfm_dir / 'database.db'
    pycolmap.logging.set_log_destination(pycolmap.logging.INFO, sfm_dir / 'colmap.LOG.')
    with events.stage('sfm_import_and_verification'):
        backend.create_empty_db(database)
        backend.import_images(work_image_dir, database, camera_mode, refs, image_options or None)
        image_ids = backend.get_image_ids(database)
        with pycolmap.Database.open(database) as db:
            backend.import_features(image_ids, db, local_path)
            backend.import_matches(image_ids, db, pairs_path, match_path, None, False)
        backend.estimation_and_geometric_verification(database, pairs_path, False)
    options = {'num_threads': min(os.cpu_count() or 1, 16), **mapper_options}
    models = run_incremental_mapping(database, work_image_dir, sfm_dir / 'models', options,
                                     publisher, frames_freq=config.snapshot_frames_freq,
                                     frame_times=frame_times)
    if not models:
        return None
    largest = max(models, key=lambda key: models[key].num_reg_images())
    # Preserve the other models separately, matching hloc's existing layout.
    for path in (sfm_dir / 'models' / str(largest)).glob('*.bin'):
        shutil.move(str(path), str(sfm_dir / path.name))
    return models[largest]


def build_map(image_dir: Path, output_dir: Path, config: MappingConfig | None = None,
              *, events: BoundedEventWriter | None = None) -> dict[str, Any]:
    """Build a new frozen map, with bounded diagnostics in an external sidecar.

    Snapshots are opt-in (frames_freq=0 by default). An injected event writer
    belongs to the caller and must also be outside map/image artifacts.
    """
    config = deepcopy(config or MappingConfig())
    image_dir = image_dir.resolve()
    output_dir = output_dir.resolve()
    sidecar = outside_artifacts(Path(config.sidecar_dir) if config.sidecar_dir else
                                output_dir.with_name(output_dir.name + '.sidecar'), image_dir, output_dir)
    if config.snapshot_frames_freq < 0:
        raise PipelineInputError('invalid_snapshot_frequency')
    if not 1 <= config.snapshot_max_count <= 1000 or config.snapshot_max_bytes < 1:
        raise PipelineInputError('invalid_snapshot_budget')
    if (output_dir/'map_manifest.json').exists():
        raise PipelineInputError('frozen_map_exists_use_new_output')
    if output_dir.exists() and any(output_dir.iterdir()):
        raise PipelineInputError('map_output_not_empty_use_new_output')
    if image_dir==output_dir or image_dir in output_dir.parents:
        raise PipelineInputError('map_output_inside_image_directory')
    if events:
        outside_artifacts(events.path, image_dir, output_dir)
        if events.map_id is not None and events.map_id != config.map_id:
            raise PipelineInputError('event_map_id_mismatch')
        events.map_id = config.map_id
    run_dir = sidecar / (events.run_id if events else uuid4().hex)
    # Never use caller-supplied run_id as a filesystem path.
    if run_dir.parent != sidecar or run_dir.name in {'.', '..'}:
        raise PipelineInputError('invalid_event_run_id')
    run_dir.mkdir(parents=True, exist_ok=False)
    events = events or BoundedEventWriter(run_dir / 'events.jsonl', run_id=run_dir.name, map_id=config.map_id)
    run = {'schema_version': 'go1loc.build_run.v1', 'run_id': events.run_id,
           'map_id': config.map_id, 'image_dir': str(image_dir), 'output_dir': str(output_dir),
           'events_path': str(events.path), 'scale_status': 'arbitrary_monocular_scale',
           'units': 'map_units', 'status': 'running', 'stage': 'initializing',
           'failure_reason': None, 'created_at': utc_now(), 'execution_permitted': False,
           'snapshot_frames_freq': config.snapshot_frames_freq,
           'source_kind': 'not_yet_inspected', 'map_observation_time': config.map_observation_time}
    # Publication precedes image preparation, native imports and feature work.
    # It is not a frozen map and does not authorize a final model read.
    publish_run(run_dir, run)
    stage_dir = None
    try:
        with events.stage('mapping'):
            refs = _images(image_dir)
            if len(refs) < 2:
                raise PipelineInputError('mapping_requires_at_least_two_images')
            refs, frame_times, frame_data = _video_references(image_dir, refs)
            run.update(source_kind='video_pts_extracted_frames' if frame_data is not None else 'unordered_images',
                       num_reference_images=len(refs), stage='mapping')
            publish_run(run_dir, run)
            stage_dir = _ascii_stage(image_dir, refs)
            manifest = _build_map(image_dir, output_dir, config, refs, frame_times, frame_data,
                                  stage_dir or image_dir, run_dir, events)
        events.emit('final', stage='mapping', registered_images=manifest.get('num_registered_images'),
                    points3D=manifest.get('num_points3D'), data={'map_manifest': str(output_dir / 'map_manifest.json'),
                    'mode': 'final_map', 'sidecar_dir': str(run_dir),
                    'build_snapshots_available': bool(manifest.get('timing_s', {}).get('snapshot_count', 0))})
        run.update(status='completed', stage='mapping', finished_at=utc_now(),
                   final_manifest_sha256=manifest.get('manifest_sha256'))
        publish_run(run_dir, run)
    except BaseException as exc:
        run.update(status='cancelled' if isinstance(exc, KeyboardInterrupt) else 'failed',
                   failure_reason=type(exc).__name__ + ': ' + str(exc)[:1000], finished_at=utc_now())
        try:
            publish_run(run_dir, run)
        except Exception as metadata_error:
            exc.add_note('Failed to persist run status: ' + str(metadata_error)[:256])
        raise
    finally:
        if stage_dir:
            shutil.rmtree(stage_dir, ignore_errors=True)
    return manifest


def _build_map(image_dir, output_dir, config, refs, frame_times, frame_data, work_image_dir, run_dir, events):
    project_root = Path(__file__).resolve().parents[2]
    os.environ.setdefault("TORCH_HOME", str(project_root / ".models" / "torch"))
    pycolmap, extract_features, match_features, pairs_from_exhaustive, pairs_from_retrieval, reconstruction = _require_backend()
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    timings: dict[str, float] = {}
    feature_conf = _effective_feature_conf(extract_features.confs[config.local_features], config.max_keypoints)
    matcher_conf = match_features.confs[config.matcher]
    global_path = None
    local_path = output_dir / f"{feature_conf['output']}.h5"
    match_path = output_dir / f"{feature_conf['output']}_{matcher_conf['output']}.h5"
    pairs_path = output_dir / "pairs-mapping.txt"

    retrieval_mode = "exhaustive"
    if frame_data is not None:
        geom = frame_data['geometry']
        if geom['status']=='calibrated_rectified' and not config.calibrated_camera_model:
            K=geom['output_K']
            config.calibrated_camera_model='PINHOLE'
            config.calibrated_camera_params=[K[0][0],K[1][1],K[0][2],K[1][2]]
        config.camera_mode='SINGLE'
        retrieval_mode = "temporal_window" if len(refs) <= config.exhaustive_limit else "netvlad_plus_temporal"
    if len(refs) > config.exhaustive_limit:
        retrieval_conf = extract_features.confs[config.retrieval]
        global_path = output_dir / f"{retrieval_conf['output']}.h5"
        with events.stage('global_features'):
            extract_features.main(retrieval_conf, work_image_dir, output_dir, image_list=refs, feature_path=global_path)
        timings["global_features"] = time.perf_counter() - started
        retrieval_mode = "netvlad_plus_temporal" if frame_data is not None else "netvlad"
        if frame_data is not None:
            pairs_from_retrieval.main(global_path, output_dir/'pairs-retrieval.txt', config.num_retrieved, query_list=refs, db_list=refs)
            pair_set = {tuple(sorted(pair)) for pair in temporal_pairs(refs,config.temporal_window)}
            pair_set.update(tuple(sorted(line.split())) for line in (output_dir/'pairs-retrieval.txt').read_text().splitlines() if line.strip())
            pairs_path.write_text(''.join(f'{a} {b}\n' for a,b in sorted(pair_set)),encoding='utf-8')
        else:
            pairs_from_retrieval.main(global_path, pairs_path, config.num_retrieved, query_list=refs, db_list=refs)
    elif frame_data is not None:
        pairs_path.write_text(''.join(f'{a} {b}\n' for a,b in temporal_pairs(refs,config.temporal_window)),encoding='utf-8')
    else:
        pairs_from_exhaustive.main(pairs_path, image_list=refs)
    with events.stage('local_features'):
        extract_features.main(feature_conf, work_image_dir, output_dir, image_list=refs, feature_path=local_path)
    with events.stage('matching'):
        pair_count = sum(bool(line.strip()) for line in pairs_path.read_text(encoding='utf-8').splitlines())
        events.emit('match_progress', stage='matching', data={'pairs_requested': pair_count,
                    'completed': False, 'granularity': 'whole_batch_no_per_pair_callback'})
        match_features.main(matcher_conf, pairs_path, features=local_path, matches=match_path)
        events.emit('match_progress', stage='matching', data={'pairs_requested': pair_count,
                    'completed': True, 'granularity': 'whole_batch_no_per_pair_callback'})
    timings["features_and_matches"] = time.perf_counter() - started

    sfm_dir = output_dir / "sfm"
    image_options: dict[str, Any] = {}
    if config.calibrated_camera_model:
        image_options["camera_model"] = config.calibrated_camera_model
        image_options["camera_params"] = ",".join(map(str, config.calibrated_camera_params or []))
    camera_mode = getattr(pycolmap.CameraMode, config.camera_mode)
    mapper_options = {'ba_refine_focal_length': not config.calibrated_camera_model or config.refine_calibration,
                      'ba_refine_extra_params': not config.calibrated_camera_model or config.refine_calibration}
    publisher = None
    with events.stage('reconstruction'):
        if config.snapshot_frames_freq:
            publisher = SnapshotPublisher(run_dir / 'snapshots', events=events,
                                          max_count=config.snapshot_max_count, max_bytes=config.snapshot_max_bytes)
            model = _reconstruct_with_snapshots(reconstruction, sfm_dir, work_image_dir, pairs_path,
                       local_path, match_path, refs, camera_mode, image_options, mapper_options,
                       publisher, config, frame_times, events)
        else:
            model = reconstruction.main(sfm_dir, work_image_dir, pairs_path, local_path, match_path,
                        image_list=refs, camera_mode=camera_mode, image_options=image_options or None,
                        mapper_options=mapper_options)
    timings["reconstruction"] = time.perf_counter() - started
    if model is None or int(model.num_reg_images()) < 2 or int(model.num_points3D()) == 0:
        raise PipelineInputError("sfm_did_not_produce_a_3d_model")
    timings['event_write_s'] = events.overhead_s
    timings['snapshot_publish_s'] = publisher.overhead_s if publisher else 0.0
    timings['snapshot_count'] = publisher.count if publisher else 0
    # Native snapshot write duration is unavailable from the verified 4.2.1 API.
    # This measures publication overhead only, not an invented total UI overhead.
    manifest = _write_manifest(output_dir, config, image_dir, model, refs, timings, global_path, local_path,
                               retrieval_mode, run_id=events.run_id)
    return manifest
