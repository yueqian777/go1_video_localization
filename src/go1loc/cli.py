from __future__ import annotations

import argparse
import json
from pathlib import Path

from .doctor import collect_doctor, write_doctor
from .evaluation import evaluate_results
from .mapping import MappingConfig, build_map
from .localization import LocalizationConfig, localize_photo
from .schemas import LocalizationResult
from .object_grounding import support_from_roi_matches
from .pipeline_errors import PipelineInputError, PipelineUnavailable
from .video import VideoPipelineError
from .video import FrameConfig, prepare_video
from .viewer import prepare_viewer, serve


def _path(value: str) -> Path:
    return Path(value).expanduser()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="go1loc")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--project-root", default=".")
    doctor.add_argument("--output", default="results/doctor.json")
    prep = sub.add_parser("prepare-video")
    prep.add_argument("video")
    prep.add_argument("--output", default="data/processed/clip")
    prep.add_argument("--config", help="JSON object with FrameConfig fields")
    prep.add_argument("--calibration", help="Source-attributed camera calibration JSON")
    prep.add_argument('--screen-config', help='Confirmed native-size screen crop/mask JSON; source stays screen_display')
    inspect = sub.add_parser('inspect-screen', help='Decode native-size preview before selecting a fixed single view')
    inspect.add_argument('video')
    inspect.add_argument('--output', required=True)
    diag = sub.add_parser('diagnose-screen', help='Bounded real masked SuperPoint/LightGlue pairs; no pose acceptance')
    diag.add_argument('--import-dir', required=True)
    diag.add_argument('--output', required=True)
    diag.add_argument('--pairs', type=int, default=3)
    mapping = sub.add_parser("build-map")
    mapping.add_argument("--images", required=True)
    mapping.add_argument("--output", required=True)
    mapping.add_argument("--map-id", default="go1-map")
    mapping.add_argument("--max-keypoints", type=int, default=4096)
    mapping.add_argument('--snapshot-frames-freq', type=int, default=0)
    mapping.add_argument('--snapshot-max-count', type=int, default=20)
    mapping.add_argument('--sidecar')
    localize = sub.add_parser("localize-photo")
    localize.add_argument("--photo", required=True)
    localize.add_argument("--map", required=True)
    localize.add_argument("--output", required=True)
    localize.add_argument('--camera-model')
    localize.add_argument('--camera-params', nargs='+', type=float)
    localize.add_argument('--calibration')
    localize.add_argument('--capture-time', help='Actual capture time in ISO8601, not file mtime')
    localize.add_argument('--frames-manifest', help='Original screen import manifest for a moved independent query PNG')
    photo_object = sub.add_parser('locate-photo-object')
    photo_object.add_argument('--photo', required=True)
    photo_object.add_argument('--map', required=True)
    photo_object.add_argument('--output', required=True)
    photo_object.add_argument('--roi', required=True, nargs=4, type=float)
    photo_object.add_argument('--mask')
    photo_object.add_argument('--query-dir', help='Reuse only hash-verified saved query evidence')
    photo_object.add_argument('--cluster-radius', type=float, help='Explicit radius in map units; default scale-adaptive')
    photo_object.add_argument('--min-points', type=int, default=3)
    photo_object.add_argument('--camera-model')
    photo_object.add_argument('--camera-params', nargs='+', type=float)
    photo_object.add_argument('--calibration')
    photo_object.add_argument('--capture-time')
    photo_object.add_argument('--frames-manifest')
    obj = sub.add_parser("locate-object")
    obj.add_argument("--observations", required=True, help="JSON list of ROI-to-3D observations")
    obj.add_argument("--points3d", required=True, help="JSON object mapping point3D_id to [x,y,z]")
    obj.add_argument("--roi", required=True, nargs=4, type=float, metavar=("X0", "Y0", "X1", "Y1"))
    obj.add_argument("--output", required=True)
    view = sub.add_parser("view")
    view.add_argument("--root", required=True)
    view.add_argument("--port", type=int, default=8765)
    view.add_argument('--sidecar', help='Output outside the frozen map; required for Viser')
    view.add_argument('--query-dir')
    view.add_argument('--snapshots')
    view.add_argument('--legacy', action='store_true', help='Static JSON debug view, not 3D')
    watch = sub.add_parser('watch-build', help='Monitor an active build run before the first frozen map exists')
    watch.add_argument('--run', required=True, help='Run directory containing atomic run metadata')
    watch.add_argument('--sidecar', required=True, help='Viewer output separate from build run and map')
    watch.add_argument('--port', type=int, default=8766)
    graph = sub.add_parser('build-route-graph')
    graph.add_argument('--map', required=True)
    graph.add_argument('--frames-manifest')
    graph.add_argument('--segments', help='Verified continuous segments JSON; no inferred proximity edges')
    graph.add_argument('--component')
    graph.add_argument('--max-gap-s', type=float, default=2.)
    graph.add_argument('--output', required=True)
    plan = sub.add_parser('plan-route')
    plan.add_argument('--graph', required=True)
    plan.add_argument('--start', required=True, help='Explicit manual/current_photo start JSON')
    plan.add_argument('--target', required=True, help='Supported object_result.json')
    plan.add_argument('--output', required=True)
    plan.add_argument('--max-start-age-s', type=float, default=30.)
    plan.add_argument('--disable-edge', action='append', default=[])
    plan.add_argument('--extrinsics', help='Metric T_BC JSON; arbitrary scale rejects it')
    plan.add_argument('--time-mode', choices=['live_current', 'offline_replay'], default='live_current')
    plan.add_argument('--replay-reference-time', help='Historical replay time; never replaces actual runtime or capture time')
    evaluation = sub.add_parser("evaluate")
    evaluation.add_argument("--results", required=True)
    evaluation.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.command == "doctor":
        report = collect_doctor(_path(args.project_root))
        write_doctor(_path(args.output), report)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    if args.command == 'inspect-screen':
        from .screen_import import inspect_screen
        try:
            print(json.dumps(inspect_screen(_path(args.video), _path(args.output)), indent=2, ensure_ascii=False))
            return 0
        except (VideoPipelineError, OSError, ValueError) as exc:
            print(json.dumps({'status': 'pipeline_error', 'failure_reason': str(exc)}, ensure_ascii=False))
            return 2
    if args.command == 'diagnose-screen':
        from .screen_diagnostics import diagnose_screen
        try:
            print(json.dumps(diagnose_screen(_path(args.import_dir), _path(args.output), args.pairs), indent=2))
            return 0
        except (ValueError, OSError, RuntimeError) as exc:
            print(json.dumps({'status': 'pipeline_error', 'failure_reason': str(exc)}))
            return 2
    if args.command == "prepare-video":
        try:
            config = FrameConfig(**json.loads(_path(args.config).read_text(encoding='utf-8'))) if args.config else FrameConfig()
            from .screen_import import ScreenConfig
            screen_config = ScreenConfig(**_read_json(args.screen_config)) if args.screen_config else None
            result = prepare_video(_path(args.video),_path(args.output),config,_path(args.calibration) if args.calibration else None,
                                   screen_config=screen_config)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        except (VideoPipelineError, OSError, ValueError, TypeError) as exc:
            print(json.dumps({"status": "pipeline_error", "failure_reason": str(exc)}, ensure_ascii=False))
            return 2
    if args.command == "build-map":
        try:
            manifest = build_map(_path(args.images), _path(args.output), MappingConfig(map_id=args.map_id,
                max_keypoints=args.max_keypoints, snapshot_frames_freq=args.snapshot_frames_freq,
                snapshot_max_count=args.snapshot_max_count, sidecar_dir=args.sidecar))
            print(json.dumps(manifest, indent=2, ensure_ascii=False))
            return 0
        except (PipelineInputError, PipelineUnavailable, OSError, ValueError, RuntimeError) as exc:
            print(json.dumps({"status": "pipeline_error", "failure_reason": str(exc)}, ensure_ascii=False))
            return 2
    if args.command == "localize-photo":
        try:
            result = localize_photo(_path(args.photo), _path(args.map), _path(args.output), _query_config(args))
        except (PipelineInputError, PipelineUnavailable, OSError, ValueError, KeyError) as exc:
            result = LocalizationResult.failure(query_id=_path(args.photo).name, map_id=_path(args.map).name, reason="pipeline_error_" + str(exc))
            # Validation may reject an output inside a frozen map or an existing run.
            # Never write there from the error handler.
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return 0 if result.pose_status == "localized" else 2
    if args.command == 'locate-photo-object':
        from .photo_object import locate_photo_object
        try:
            payload = locate_photo_object(_path(args.photo), _path(args.map), _path(args.output), args.roi,
                mask_path=_path(args.mask) if args.mask else None,
                query_dir=_path(args.query_dir) if args.query_dir else None,
                cluster_radius=args.cluster_radius, min_points=args.min_points, config=_query_config(args))
        except (PipelineInputError, PipelineUnavailable, OSError, ValueError, KeyError) as exc:
            payload = {'status': 'pipeline_error', 'failure_reason': str(exc), 'execution_permitted': False}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if payload['status'] == 'supported' else 2
    if args.command == "locate-object":
        observations = json.loads(_path(args.observations).read_text(encoding="utf-8"))
        points_raw = json.loads(_path(args.points3d).read_text(encoding="utf-8"))
        points = {int(k): v for k, v in points_raw.items()}
        support = support_from_roi_matches(roi=tuple(args.roi), observations=observations, points3d=points)
        payload = support.__dict__
        _path(args.output).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if support.status == "supported" else 2
    if args.command == 'watch-build':
        from .build_viewer import watch_build
        watch_build(_path(args.run), _path(args.sidecar), port=args.port)
        return 0
    if args.command == "view":
        root = _path(args.root).resolve()
        sidecar = _path(args.sidecar).resolve() if args.sidecar else root.with_name(root.name+'.viewer')
        from .events import outside_artifacts
        outside_artifacts(sidecar, root)
        if not args.legacy:
            from .viser_viewer import serve_viser
            serve_viser(root, sidecar, port=args.port,
                        query_dir=_path(args.query_dir) if args.query_dir else None,
                        snapshot_dir=_path(args.snapshots) if args.snapshots else None)
            return 0
        result_path = root / "localization_result.json"
        manifest_path = root / "map_manifest.json"
        result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        prepare_viewer(sidecar, result, manifest)
        serve(sidecar, port=args.port)
        return 0
    if args.command in ('build-route-graph', 'plan-route'):
        from datetime import datetime, timezone
        from .planning import build_route_graph, plan_route
        from .evidence import verify_map, write_json
        try:
            destination=_path(args.output).resolve()
            if destination.exists():
                raise ValueError('output_exists_use_new_path')
            if args.command == 'build-route-graph':
                from .events import outside_artifacts
                from .evidence import resolve_map_path
                map_dir=_path(args.map).resolve()
                outside_artifacts(destination,map_dir)
                manifest=verify_map(map_dir)
                # Read native verified camera poses rather than trusting an unbound sidecar.
                import pycolmap
                model=pycolmap.Reconstruction(resolve_map_path(map_dir,manifest['model_dir']))
                refs={im.name:{'image_id':int(im.image_id),'T_CW':im.cam_from_world().matrix().tolist()}
                      for im in model.images.values()}
                frames=_read_json(args.frames_manifest) if args.frames_manifest else manifest.get('frames_manifest')
                payload=build_route_graph(manifest,refs,frames,component_id=args.component,
                                         route_segments=_read_json(args.segments) if args.segments else None,
                                         max_gap_s=args.max_gap_s)
            else:
                _reject_frozen_destination(destination)
                payload=plan_route(_read_json(args.graph),_read_json(args.start),_read_json(args.target),
                    now=datetime.now(timezone.utc).isoformat(),max_start_age_s=args.max_start_age_s,
                    time_mode=args.time_mode, replay_reference_time=args.replay_reference_time,
                    disabled_edge_ids=args.disable_edge,T_BC=_read_json(args.extrinsics) if args.extrinsics else None)
            write_json(destination,payload)
        except (OSError, ValueError, KeyError) as exc:
            payload={'status':'rejected','failure_reason':str(exc),'execution_permitted':False}
        print(json.dumps(payload,indent=2,ensure_ascii=False))
        return 0 if payload.get('schema_version') == 'go1loc.route_graph.v1' or payload.get('status') == 'route_preview_only' else 2
    if args.command == "evaluate":
        report = evaluate_results(_path(args.results))
        _path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    return 1


def _query_config(args):
    return LocalizationConfig(query_camera_model=args.camera_model, query_camera_params=args.camera_params,
                              calibration_path=_path(args.calibration) if args.calibration else None,
                              capture_time=args.capture_time,
                              frames_manifest_path=_path(args.frames_manifest) if getattr(args, 'frames_manifest', None) else None)


def _read_json(path):
    return json.loads(_path(path).read_text(encoding='utf-8'))


def _reject_frozen_destination(destination):
    if any((p/'map_manifest.json').exists() for p in destination.parents):
        raise ValueError('output_overlaps_frozen_map')


if __name__ == '__main__':
    raise SystemExit(main())
