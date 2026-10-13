"""Video probing and deterministic frame-selection metadata.

The module never fabricates a frame timestamp from ``index / fps`` when the
container reports VFR or incomplete PTS. Actual decoding is delegated to
ffprobe/ffmpeg or OpenCV when those tools are explicitly available.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from .events import BoundedEventWriter, outside_artifacts


class VideoPipelineError(RuntimeError):
    pass


@dataclass
class VideoProbe:
    source_path: str
    source_sha256: str
    decoder: str | None
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    frame_count: int | None = None
    fps_nominal: float | None = None
    time_base: str | None = None
    start_time_s: float | None = None
    rotation: int | None = None
    vfr_or_pts_uncertain: bool = False
    raw_ffprobe: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sha256_file(path: Path, chunk_bytes: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


def _ratio(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        a, b = value.split("/", 1)
        return float(a) / float(b) if float(b) else None
    except (ValueError, ZeroDivisionError):
        return None


def _ffprobe_json(path: Path) -> dict[str, Any]:
    executable = shutil.which("ffprobe")
    if not executable:
        raise VideoPipelineError("ffprobe_not_available")
    command = [
        executable,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    if completed.returncode != 0:
        raise VideoPipelineError(f"ffprobe_failed:{completed.stderr[-500:]}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise VideoPipelineError("ffprobe_invalid_json") from exc


def probe_video(path: Path) -> VideoProbe:
    path = path.resolve()
    if not path.is_file():
        raise VideoPipelineError("video_not_found")
    raw = _ffprobe_json(path)
    stream = next((s for s in raw.get("streams", []) if s.get("codec_type") == "video"), None)
    if stream is None:
        raise VideoPipelineError("no_video_stream")
    format_data = raw.get("format", {})
    fps = _ratio(stream.get("avg_frame_rate")) or _ratio(stream.get("r_frame_rate"))
    duration = stream.get("duration") or format_data.get("duration")
    duration_value = float(duration) if duration not in {None, "N/A"} else None
    frame_count = int(stream["nb_frames"]) if str(stream.get("nb_frames", "N/A")).isdigit() else None
    tags = {**format_data.get("tags", {}), **stream.get("tags", {})}
    rotation = None
    if "rotate" in tags:
        try:
            rotation = int(tags["rotate"])
        except ValueError:
            pass
    for side in stream.get('side_data_list', []):
        if 'rotation' in side:
            rotation = int(side['rotation'])
    warnings: list[str] = []
    if frame_count is None:
        warnings.append("frame_count_not_declared")
    if stream.get("avg_frame_rate") in {None, "0/0", "N/A"}:
        warnings.append("nominal_fps_unavailable")
    return VideoProbe(
        source_path=str(path),
        source_sha256=sha256_file(path),
        decoder="ffprobe",
        width=int(stream["width"]) if stream.get("width") else None,
        height=int(stream["height"]) if stream.get("height") else None,
        duration_s=duration_value,
        frame_count=frame_count,
        fps_nominal=fps,
        time_base=stream.get("time_base"),
        start_time_s=float(stream["start_time"]) if stream.get("start_time") not in {None, "N/A"} else None,
        rotation=rotation,
        vfr_or_pts_uncertain=frame_count is None or _ratio(stream.get("avg_frame_rate")) is None,
        raw_ffprobe=raw,
        warnings=warnings,
    )


def select_uniform_times(probe: VideoProbe, count: int) -> list[float]:
    """Select times only from known duration; never pretend they are frame indices."""

    if count < 1:
        raise ValueError("count must be positive")
    if probe.duration_s is None or probe.duration_s <= 0:
        raise VideoPipelineError("duration_unknown_cannot_select_times")
    if count == 1:
        return [0.0]
    return [probe.duration_s * i / (count - 1) for i in range(count)]


def write_probe(path: Path, probe: VideoProbe) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(probe.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


@dataclass
class FrameConfig:
    interval_s: float = 0.5
    max_frames: int = 80
    start_s: float = 0.0
    duration_s: float = 30.0
    min_blur_variance: float = 20.0
    max_overexposure_fraction: float = 0.4
    duplicate_mean_difference: float = 1.0
    max_gap_s: float = 2.0
    quality_filter: bool = True


def select_frame_records(frames: list[dict], config: FrameConfig) -> list[dict]:
    if config.interval_s <= 0 or not 1 <= config.max_frames <= 1000 or config.start_s < 0 or not 0 < config.duration_s <= 600:
        raise VideoPipelineError('invalid_frame_budget')
    selected = []
    previous = None
    origin = None
    next_time = config.start_s
    for i, frame in enumerate(frames):
        raw_time = frame.get('best_effort_timestamp_time', frame.get('pts_time'))
        if raw_time is None:
            raise VideoPipelineError('missing_frame_pts')
        t = float(raw_time)
        if not math.isfinite(t):
            raise VideoPipelineError('non_finite_frame_pts')
        if previous is not None and t <= previous:
            raise VideoPipelineError('non_monotonic_or_duplicate_pts')
        previous = t
        if origin is None:
            origin = t
        rel = t-origin
        if rel >= config.start_s+config.duration_s:
            break
        if rel+1e-9 >= next_time and len(selected)<config.max_frames:
            selected.append({'frame_index':i,'pts':frame.get('pts'),'pts_time_s':t,'relative_time_s':rel})
            next_time = rel+config.interval_s
    return selected


def frame_quality(image, config: FrameConfig, previous=None) -> dict:
    import cv2
    import numpy as np
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blur = float(cv2.Laplacian(gray,cv2.CV_64F).var())
    over = float(np.mean(gray>=250))
    reasons = []
    if config.quality_filter:
        if blur < config.min_blur_variance:
            reasons.append('blur')
        if over > config.max_overexposure_fraction:
            reasons.append('overexposure')
    diff = None
    if previous is not None:
        diff = float(np.abs(cv2.resize(gray,(64,64)).astype(float)-previous).mean())
    return {'laplacian_variance':blur,'overexposure_fraction':over,'mean_change_64px':diff,'rejection_reasons':reasons}


def prepare_video(path: Path, output_dir: Path, config: FrameConfig | None = None,
                  calibration_path: Path | None = None, *, events: BoundedEventWriter | None = None) -> dict:
    """Prepare frames; emit events outside the immutable cached frame directory."""
    output_dir = output_dir.resolve()
    if events:
        outside_artifacts(events.path, output_dir)
    else:
        sidecar = output_dir.with_name(output_dir.name + '.sidecar') / uuid4().hex
        events = BoundedEventWriter(sidecar / 'events.jsonl')
    with events.stage('video'):
        result = _prepare_video(path, output_dir, config, calibration_path, events)
    events.emit('final', stage='video', data={'frames_manifest': str(output_dir / 'frames_manifest.json'),
                'candidate_count': result['candidate_count'], 'kept_count': result['kept_count']})
    return result


def _prepare_video(path, output_dir, config, calibration_path, events) -> dict:
    """Bounded decode using real presentation times, followed by auditable filtering.

    Decoding always starts at stream origin. frame_index is the decoded display
    order, not index/fps. Raw selected PNGs are retained without recompression.
    """
    import cv2
    import numpy as np
    from .calibration import load_calibration, build_rectification
    config = config or FrameConfig()
    with events.stage('video_probe'):
        probe = probe_video(path)
    # Validate budget even if the clip has no frames.
    select_frame_records([],config)
    signature = {'video_sha256':probe.source_sha256,'config':asdict(config),'calibration_sha256':sha256_file(calibration_path) if calibration_path else None}
    output_dir = output_dir.resolve()
    manifest_file = output_dir/'frames_manifest.json'
    if manifest_file.exists():
        saved = json.loads(manifest_file.read_text(encoding='utf-8'))
        if saved.get('signature')!=signature:
            raise VideoPipelineError('output_cache_signature_mismatch_use_new_directory')
        if all((output_dir/r['image_path']).exists() for r in saved['frames'] if r['kept']):
            events.emit('cache_hit', stage='video', data={'source': str(manifest_file),
                        'note': 'no_new_decode_or_frame_events'})
            return saved
        raise VideoPipelineError('cached_frames_missing_use_new_directory')
    if output_dir.exists() and any(output_dir.iterdir()):
        raise VideoPipelineError('output_not_empty_use_new_directory')
    output_dir.mkdir(parents=True,exist_ok=True)
    for folder in ('raw','images'):
        (output_dir/folder).mkdir()
    raw = subprocess.run([shutil.which('ffprobe') or 'ffprobe','-v','error','-select_streams','v:0','-read_intervals',f'%+{config.start_s+config.duration_s}', '-show_frames','-show_entries','frame=pts,best_effort_timestamp_time,pts_time','-of','json',str(path.resolve())],capture_output=True,text=True,encoding='utf-8',timeout=180)
    if raw.returncode:
        raise VideoPipelineError('frame_probe_failed:'+raw.stderr[-500:])
    selected = select_frame_records(json.loads(raw.stdout).get('frames',[]),config)
    if not selected:
        raise VideoPipelineError('no_frames_in_requested_time_window')
    filt = 'select='+ '+'.join(f'eq(n\\,{r["frame_index"]})' for r in selected)
    command = [shutil.which('ffmpeg') or 'ffmpeg','-v','error','-nostdin','-noautorotate','-i',str(path.resolve()),'-map','0:v:0','-t',str(config.start_s+config.duration_s),'-vf',filt,'-fps_mode','vfr','-frames:v',str(len(selected)),str(output_dir/'raw'/'%06d.png')]
    with events.stage('video_decode'):
        decoded = subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=600)
        if decoded.returncode:
            raise VideoPipelineError('decode_failed:'+decoded.stderr[-500:])
    files = sorted((output_dir/'raw').glob('*.png'))
    if len(files)!=len(selected):
        raise VideoPipelineError('decoded_frame_count_does_not_match_pts')
    rect = None
    geometry = {'status':'uncalibrated','rotation_degrees':probe.rotation or 0,'coordinate_space':'processed_image_pixels'}
    if calibration_path:
        cal = load_calibration(calibration_path)
        if cal.image_size != (probe.width,probe.height):
            raise VideoPipelineError('calibration_resolution_mismatch')
        if probe.rotation not in (None,0):
            raise VideoPipelineError('calibration_with_rotation_requires_explicit_normalization')
        rect = build_rectification(cal)
        np.savez_compressed(output_dir/'rectification_maps.npz',map_x=rect['map_x'],map_y=rect['map_y'])
        cv2.imencode('.png',rect['valid_mask'])[1].tofile(str(output_dir/'valid_mask.png'))
        geometry.update({'status':'calibrated_rectified','calibration':cal.to_dict(),'crop_xywh':rect['crop_xywh'],'output_K':rect['output_K'].tolist(),'nonlinear_mapping':'rectification_maps.npz','valid_mask':'valid_mask.png'})
    prior = None
    last_kept = None
    events.emit('started', stage='frame_filter')
    for row,file in zip(selected,files):
        img = cv2.imdecode(np.fromfile(str(file),dtype=np.uint8),cv2.IMREAD_COLOR)
        if rect:
            img = cv2.remap(img,rect['map_x'],rect['map_y'],cv2.INTER_LINEAR)
            x,y,w,h = rect['crop_xywh']; img = img[y:y+h,x:x+w]
        else:
            rot = (probe.rotation or 0)%360
            if rot not in (0,90,180,270):
                raise VideoPipelineError('unsupported_non_right_angle_rotation')
            if rot:
                img = np.rot90(img,k=rot//90).copy()
        quality = frame_quality(img,config,prior)
        if config.quality_filter and quality['mean_change_64px'] is not None and quality['mean_change_64px']<config.duplicate_mean_difference and row['relative_time_s']-last_kept<config.max_gap_s:
            quality['rejection_reasons'].append('suspected_redundant_frame_not_proven_camera_drop')
        row.update({'raw_path':file.relative_to(output_dir).as_posix(),'quality':quality,'kept':not quality['rejection_reasons'],'image_path':None,'size_wh':[img.shape[1],img.shape[0]]})
        if row['kept']:
            dest = output_dir/'images'/f'frame_{row["frame_index"]:08d}.png'
            cv2.imencode('.png',img)[1].tofile(str(dest))
            row['image_path'] = dest.relative_to(output_dir).as_posix()
            prior = cv2.resize(cv2.cvtColor(img,cv2.COLOR_BGR2GRAY),(64,64)).astype(float)
            last_kept = row['relative_time_s']
        events.emit('frame_kept' if row['kept'] else 'frame_rejected', stage='frame_filter',
                    video_pts_s=row['pts_time_s'], data={'frame_index': row['frame_index'],
                    'pts': row['pts'], 'relative_time_s': row['relative_time_s'],
                    'image_path': row['image_path'], 'quality': quality,
                    'source_video_sha256': probe.source_sha256, 'exposure_time': None})
    events.emit('completed', stage='frame_filter', data={'candidate_count': len(selected),
                'kept_count': sum(r['kept'] for r in selected)})
    result = {'schema_version':'go1loc.frames.v1','signature':signature,'probe':probe.to_dict(),'geometry':geometry,'frames':selected,'candidate_count':len(selected),'kept_count':sum(r['kept'] for r in selected),'timing_source':'best_effort_pts_not_exposure_time','robot_access':False}
    manifest_file.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result
