"""Verify source PNG sessions; export VFR FFV1 or adapt to existing frame manifests."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import zlib
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import numpy as np
from PIL import Image

from .capture_session import safe_new_directory, write_json
from .capture_wire import validate_header
from .video import FrameConfig, frame_quality, select_frame_records, sha256_file


def load_recording(path, *, allow_interrupted=False):
    path = Path(path).resolve()
    if (path / 'INCOMPLETE').exists():
        raise ValueError('session_not_finalized_preserve_files_for_recovery')
    meta = json.loads((path / 'session.json').read_text(encoding='utf-8'))
    allowed = ('complete', 'interrupted') if allow_interrupted else ('complete',)
    if meta.get('schema_version') != 'go1loc.capture.v1' or meta.get('status') not in allowed:
        raise ValueError('capture_session_not_complete_explicit_interrupted_override_required')
    manifest = path / 'frames.jsonl'
    if manifest.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('capture_manifest_exceeds_budget')
    rows = [json.loads(line) for line in manifest.read_text(encoding='utf-8').splitlines() if line.strip()]
    if not 1 <= len(rows) <= 12000 or len(rows) != meta['written_frames']:
        raise ValueError('capture_frame_count_mismatch_or_empty')
    keys = ('session_id', 'camera_id', 'width', 'height', 'channels', 'pixel_format', 'stride',
            'projection', 'orientation', 'source_kind')
    identity = tuple(meta['source'].get(k) for k in keys)
    seq, stamp = -1, -1.
    for i, row in enumerate(rows):
        validate_header(row)
        if (tuple(row.get(k) for k in keys) != identity or row['saved_index'] != i
                or row['recording_id'] != meta['recording_id']
                or row['agent_seq'] <= seq or row['agent_read_monotonic'] <= stamp):
            raise ValueError('capture_identity_or_time_mismatch')
        seq, stamp = row['agent_seq'], row['agent_read_monotonic']
        file = (path / row['image_path']).resolve()
        if file.parent != path / 'frames' or file.suffix != '.png' or not file.is_file():
            raise ValueError('capture_frame_path_escape_or_missing')
        if sha256_file(file) != row['image_sha256']:
            raise ValueError('capture_png_hash_mismatch')
        with Image.open(file) as image:
            if image.mode != 'RGB' or image.size != (row['width'], row['height']):
                raise ValueError('capture_png_layout_mismatch')
            payload = np.asarray(image)[:, :, ::-1].tobytes()
            if (hashlib.sha256(payload).hexdigest() != row['raw_payload_sha256']
                    or zlib.crc32(payload) & 0xffffffff != row['payload_crc32']):
                raise ValueError('capture_source_pixels_mismatch')
    if meta['source_kind'] != rows[0]['source_kind']:
        raise ValueError('capture_source_kind_mismatch')
    return meta, rows


def capture_calibration(calibration_path, meta):
    """Bind calibration to this camera, without rewriting original source claims.

    Unknown display geometry needs explicit projection evidence. A pinhole
    calibration never authorizes a second rectification of a rectified image.
    """
    from .calibration import load_calibration
    binding = json.loads(Path(calibration_path).read_text(encoding='utf-8'))
    source = meta['source']
    projection = binding.get('capture_projection')
    if binding.get('capture_camera_id') != source['camera_id']:
        raise ValueError('calibration_camera_mismatch')
    if projection not in {'raw_fisheye', 'raw_pinhole', 'rectified_pinhole'}:
        raise ValueError('calibration_projection_required')
    original = source['projection']
    if original != projection and original != 'unknown':
        raise ValueError('calibration_projection_conflict')
    if original == 'unknown' and not str(binding.get('projection_evidence', '')).strip():
        raise ValueError('calibration_projection_evidence_required')
    # Keep existing verified fisheye files readable. New RGB calibrations must
    # identify the source and, when available, the physical camera serial.
    if projection != 'raw_fisheye' or source['source_kind'] == 'orbbec_rgb_mjpeg':
        if binding.get('capture_source_kind') != source['source_kind']:
            raise ValueError('calibration_source_kind_mismatch')
        if source.get('camera_serial') and binding.get('capture_camera_serial') != source['camera_serial']:
            raise ValueError('calibration_camera_serial_mismatch')
    cal = load_calibration(Path(calibration_path))
    models = {'fisheye', 'opencv_fisheye'} if projection == 'raw_fisheye' else {'pinhole', 'opencv'}
    if cal.image_size != (source['width'], source['height']) or cal.model.lower() not in models:
        raise ValueError('calibration_resolution_or_model_mismatch')
    if projection == 'rectified_pinhole' and any(v != 0 for v in cal.distortion):
        raise ValueError('already_rectified_requires_zero_distortion')
    return cal, projection, binding.get('projection_evidence')


def prepare_capture(path, output, config=None, calibration_path=None, *, allow_interrupted=False):
    """Use source-read timestamps in the legacy pts slot, explicitly labelled.

    They are neither original stream PTS nor sensor exposure timestamps. No route
    is approved here. Equal-content frames are retained, never auto-deduplicated.
    """
    import cv2

    from .calibration import build_rectification, load_calibration
    path, output = Path(path).resolve(), Path(output).resolve()
    meta, rows = load_recording(path, allow_interrupted=allow_interrupted)
    config = config or FrameConfig()
    selected = select_frame_records([{'pts_time': r['agent_read_monotonic'], 'pts': None} for r in rows], config)
    if not selected:
        raise ValueError('no_capture_frames_in_window')
    rect = None
    geometry = dict(status='uncalibrated', coordinate_space='processed_image_pixels',
                    camera_id=meta['source']['camera_id'], projection=meta['source']['projection'],
                    orientation=meta['source']['orientation'], warning='not_screen_intrinsics_requires_matching_calibration')
    if calibration_path:
        cal, projection, evidence = capture_calibration(calibration_path, meta)
        rect = build_rectification(cal)
        geometry.update(status='calibrated_rectified', calibration=cal.to_dict(), crop_xywh=rect['crop_xywh'],
                        output_K=rect['output_K'].tolist(), nonlinear_mapping='rectification_maps.npz', valid_mask='valid_mask.png',
                        source_projection=meta['source']['projection'], calibration_projection=projection,
                        projection_evidence=evidence)
    safe_new_directory(output)
    (output / 'raw').mkdir()
    (output / 'images').mkdir()
    if rect:
        np.savez_compressed(output / 'rectification_maps.npz', map_x=rect['map_x'], map_y=rect['map_y'])
        cv2.imencode('.png', rect['valid_mask'])[1].tofile(str(output / 'valid_mask.png'))
    previous_selected = 0
    for row in selected:
        source = rows[row['frame_index']]
        file = path / source['image_path']
        raw = output / 'raw' / file.name
        shutil.copyfile(file, raw)
        if sha256_file(raw) != source['image_sha256']:
            raise ValueError('source_changed_during_capture_import')
        rgb = np.asarray(Image.open(raw))
        bgr = rgb[:, :, ::-1].copy()
        if rect:
            bgr = cv2.remap(bgr, rect['map_x'], rect['map_y'], cv2.INTER_LINEAR)
            x, y, w, h = rect['crop_xywh']
            bgr = bgr[y:y+h, x:x+w]
        quality = frame_quality(bgr, config)  # no similarity-based deletion
        kept = not quality['rejection_reasons']
        dest = output / 'images' / ('frame_%08d.png' % row['frame_index'])
        if kept:
            if rect:
                cv2.imencode('.png', bgr)[1].tofile(str(dest))
            else:
                shutil.copyfile(raw, dest)
        row.update(raw_path=raw.relative_to(output).as_posix(), raw_sha256=source['image_sha256'],
                   image_path=dest.relative_to(output).as_posix() if kept else None,
                   kept=kept, size_wh=[bgr.shape[1], bgr.shape[0]], quality=quality,
                   source_capture=source, sensor_capture_time=None, original_stream_pts=None,
                   timing_source='agent_read_monotonic_not_exposure',
                   discontinuity_before=any(rows[j]['agent_seq'] > rows[j-1]['agent_seq'] + 1
                                            for j in range(max(1, previous_selected+1), row['frame_index']+1)))
        previous_selected = row['frame_index']
    source_hash = hashlib.sha256((sha256_file(path / 'session.json') + sha256_file(path / 'frames.jsonl')).encode()).hexdigest()
    result = dict(schema_version='go1loc.frames.v1', source_kind=meta['source_kind'],
                  timing_source='agent_read_monotonic_not_exposure', sensor_capture_time=None,
                  hardware_synchronized=False, execution_permitted=False, robot_access=False,
                  probe=dict(source_path=str(path), source_sha256=source_hash,
                             hash_meaning='sha256_of_session_and_frames_manifest_sha256_hex_concatenation',
                             width=rows[0]['width'], height=rows[0]['height']),
                  signature=dict(source_sha256=source_hash, config=asdict(config),
                                 calibration_sha256=sha256_file(Path(calibration_path)) if calibration_path else None),
                  geometry=geometry, frames=selected, candidate_count=len(selected),
                  kept_count=sum(r['kept'] for r in selected), route_segments=[],
                  source_session=meta, verified_continuous=False,
                  frame_selection=dict(input_saved_frames=len(rows), selected_frames=len(selected),
                                       interval_s=config.interval_s, reason='explicit_offline_map_frame_budget'),
                  quality_note='Laplacian threshold is an adjustable heuristic, not cross-resolution acceptance')
    write_json(output / 'frames_manifest.json', result)
    write_json(output / 'source_timeline.json', dict(frames=rows, source_session=meta))
    return result


def export_ffv1(path, *, allow_interrupted=False):
    """Lossless RGB/BGR pixels, VFR read timeline rounded to MKV milliseconds.

    No final-frame duplication; the last frame has no known exposure duration.
    Decode first/middle/last pixels and check ALL output PTS before publishing.
    """
    path = Path(path).resolve()
    meta, rows = load_recording(path, allow_interrupted=allow_interrupted)
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        raise RuntimeError('ffmpeg_and_ffprobe_required_source_PNGs_are_preserved')
    out = path / 'exports' / uuid4().hex
    out.mkdir(parents=True, exist_ok=False)
    times = [r['agent_read_monotonic'] - rows[0]['agent_read_monotonic'] for r in rows]
    if any(b-a < .001 for a, b in zip(times, times[1:])):
        raise ValueError('read_timestamps_too_close_for_MKV_millisecond_timebase')
    lines = ['ffconcat version 1.0']
    for i, row in enumerate(rows):
        # Validated local PNGs only. Paths are relative to the concat file.
        rel = '../../' + row['image_path']
        lines += ["file '" + rel.replace("'", "'\\''") + "'", 'option framerate 1000']
        if i + 1 < len(rows):
            lines += ['duration %.9f' % (times[i+1]-times[i])]
    concat = out / 'frames.ffconcat'
    concat.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    temporary, video = out / 'source.partial.mkv', out / 'source.mkv'
    result = subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-n', '-f', 'concat', '-safe', '0',
                             '-i', str(concat), '-map', '0:v:0', '-fps_mode', 'passthrough',
                             '-c:v', 'ffv1', '-level', '3', '-pix_fmt', 'bgr0', '-enc_time_base', '1:1000',
                             str(temporary)], capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError('ffv1_encode_failed_source_PNGs_preserved')
    probe = subprocess.run([ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_streams',
                            '-show_frames', '-show_entries',
                            'stream=codec_name,pix_fmt:frame=best_effort_timestamp_time', '-of', 'json',
                            str(temporary)], capture_output=True, timeout=60, check=True)
    info = json.loads(probe.stdout)
    actual = [float(f['best_effort_timestamp_time']) for f in info['frames']]
    if (len(actual) != len(times) or info['streams'][0]['codec_name'] != 'ffv1'
            or info['streams'][0]['pix_fmt'] != 'bgr0'):
        raise RuntimeError('ffv1_frame_count_or_pixel_format_verification_failed')
    error = max(abs(a-b) for a, b in zip(actual, times))
    if error > .0011:
        raise RuntimeError('ffv1_timestamp_verification_failed')
    indices = sorted({0, len(rows)//2, len(rows)-1})
    select = '+'.join('eq(n\\,%d)' % i for i in indices)
    decoded = subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-i', str(temporary), '-vf', 'select=' + select,
                              '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                             capture_output=True, timeout=60, check=True).stdout
    expected = b''.join(Image.open(path / rows[i]['image_path']).tobytes() for i in indices)
    if decoded != expected:
        raise RuntimeError('ffv1_pixel_roundtrip_verification_failed')
    temporary.rename(video)
    report = dict(video=str(video), schema_version='go1loc.capture_video.v1', codec='ffv1', pixel_format='bgr0',
                  frame_count=len(rows), verified_pixel_indices=indices, max_pts_error_s=error,
                  timing_source='agent_read_monotonic_not_exposure', time_quantization_s=.001,
                  last_frame_duration='unknown_container_default_1ms_not_exposure_duration',
                  sensor_capture_time=None, source_kind=meta['source_kind'],
                  source_session_sha256=sha256_file(path / 'session.json'), source_frames_sha256=sha256_file(path / 'frames.jsonl'),
                  video_sha256=sha256_file(video), execution_permitted=False)
    write_json(out / 'verification.json', report)
    return report
