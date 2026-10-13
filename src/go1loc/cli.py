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
    mapping = sub.add_parser("build-map")
    mapping.add_argument("--images", required=True)
    mapping.add_argument("--output", required=True)
    mapping.add_argument("--map-id", default="go1-map")
    mapping.add_argument("--max-keypoints", type=int, default=4096)
    localize = sub.add_parser("localize-photo")
    localize.add_argument("--photo", required=True)
    localize.add_argument("--map", required=True)
    localize.add_argument("--output", required=True)
    obj = sub.add_parser("locate-object")
    obj.add_argument("--observations", required=True, help="JSON list of ROI-to-3D observations")
    obj.add_argument("--points3d", required=True, help="JSON object mapping point3D_id to [x,y,z]")
    obj.add_argument("--roi", required=True, nargs=4, type=float, metavar=("X0", "Y0", "X1", "Y1"))
    obj.add_argument("--output", required=True)
    view = sub.add_parser("view")
    view.add_argument("--root", required=True)
    view.add_argument("--port", type=int, default=8765)
    evaluation = sub.add_parser("evaluate")
    evaluation.add_argument("--results", required=True)
    evaluation.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.command == "doctor":
        report = collect_doctor(_path(args.project_root))
        write_doctor(_path(args.output), report)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    if args.command == "prepare-video":
        try:
            config = FrameConfig(**json.loads(_path(args.config).read_text(encoding='utf-8'))) if args.config else FrameConfig()
            result = prepare_video(_path(args.video),_path(args.output),config,_path(args.calibration) if args.calibration else None)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        except VideoPipelineError as exc:
            print(json.dumps({"status": "pipeline_error", "failure_reason": str(exc)}, ensure_ascii=False))
            return 2
    if args.command == "build-map":
        try:
            manifest = build_map(_path(args.images), _path(args.output), MappingConfig(map_id=args.map_id, max_keypoints=args.max_keypoints))
            print(json.dumps(manifest, indent=2, ensure_ascii=False))
            return 0
        except (PipelineInputError, PipelineUnavailable, OSError, ValueError, RuntimeError) as exc:
            print(json.dumps({"status": "pipeline_error", "failure_reason": str(exc)}, ensure_ascii=False))
            return 2
    if args.command == "localize-photo":
        try:
            result = localize_photo(_path(args.photo), _path(args.map), _path(args.output), LocalizationConfig())
        except (PipelineInputError, PipelineUnavailable, OSError, ValueError, KeyError) as exc:
            result = LocalizationResult.failure(query_id=_path(args.photo).name, map_id=_path(args.map).name, reason="pipeline_error_" + str(exc))
            _path(args.output).mkdir(parents=True, exist_ok=True)
            (_path(args.output) / "localization_result.json").write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return 0 if result.pose_status == "localized" else 2
    if args.command == "locate-object":
        observations = json.loads(_path(args.observations).read_text(encoding="utf-8"))
        points_raw = json.loads(_path(args.points3d).read_text(encoding="utf-8"))
        points = {int(k): v for k, v in points_raw.items()}
        support = support_from_roi_matches(roi=tuple(args.roi), observations=observations, points3d=points)
        payload = support.__dict__
        _path(args.output).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if support.status == "supported" else 2
    if args.command == "view":
        root = _path(args.root)
        result_path = root / "localization_result.json"
        manifest_path = root / "map_manifest.json"
        result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        prepare_viewer(root, result, manifest)
        serve(root, port=args.port)
        return 0
    if args.command == "evaluate":
        report = evaluate_results(_path(args.results))
        _path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    return 1
