"""Bounded, lossless single-view import from a real Go1 display recording.

No sensor clock inference, rectification, image synthesis, or route approval.
Only selected PNGs are retained; every decoded display frame is quality-checked
and retains its source index/PTS in the separate full timeline.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .video import FrameConfig, VideoPipelineError, probe_video, select_frame_records, sha256_file


@dataclass
class ScreenConfig:
    source_size_wh: list[int]
    crop_xywh: list[int]
    selected_window: str
    output_size_wh: list[int] | None = None
    camera_id: str | None = None
    camera_label: str = 'unknown'
    display_mode: str = 'unknown'
    display_evidence: str = 'unknown; no additional undistortion performed'
    calibration_status: str = 'unknown'
    expected_source_sha256: str | None = None
    exclude_rectangles: list[list[int]] = field(default_factory=list)
    margin_px: int = 8
    border_px: int = 4

    def validate(self, width, height):
        if (not isinstance(self.source_size_wh, list) or len(self.source_size_wh) != 2
                or any(type(v) is not int or not 1 <= v <= 8192 for v in self.source_size_wh)):
            raise VideoPipelineError('invalid_screen_native_dimensions')
        if self.source_size_wh != [width, height]:
            raise VideoPipelineError('screen_selection_resolution_mismatch_reinspect_native_frame')
        if not isinstance(self.selected_window, str) or not self.selected_window.strip() or self.display_mode not in {'unknown', 'raw_fisheye', 'already_rectified', 'display_reprojected'}:
            raise VideoPipelineError('invalid_screen_window_or_display_mode')
        if self.display_mode != 'unknown' and (not self.display_evidence or self.display_evidence.startswith('unknown')):
            raise VideoPipelineError('display_mode_requires_evidence')
        if self.calibration_status != 'unknown':
            raise VideoPipelineError('screen_calibration_not_implemented_no_invented_intrinsics')
        for rect in [self.crop_xywh, *self.exclude_rectangles]:
            if (len(rect) != 4 or any(type(v) is not int for v in rect)
                    or min(rect[:2]) < 0 or min(rect[2:]) <= 0
                    or rect[0]+rect[2] > width or rect[1]+rect[3] > height):
                raise VideoPipelineError('screen_rectangle_out_of_native_bounds')
        if (type(self.margin_px) is not int or not 0 <= self.margin_px <= 128
                or type(self.border_px) is not int or not 0 <= self.border_px <= 128):
            raise VideoPipelineError('invalid_screen_mask_margin')
        if self.output_size_wh is not None and (len(self.output_size_wh) != 2
                or any(type(v) is not int or not 1 <= v <= 8192 for v in self.output_size_wh)):
            raise VideoPipelineError('invalid_screen_output_size')


def crop_image(image, config: ScreenConfig):
    import cv2
    x, y, w, h = config.crop_xywh
    image = image[y:y+h, x:x+w].copy()
    size = config.output_size_wh or [w, h]
    if size != [w, h]:
        image = cv2.resize(image, tuple(size), interpolation=cv2.INTER_AREA)
    return image


def effective_mask(config: ScreenConfig):
    import cv2
    width, height = config.source_size_wh
    mask = np.full((height, width), 255, np.uint8)
    margin = config.margin_px
    for x, y, w, h in config.exclude_rectangles:
        mask[max(0, y-margin):min(height, y+h+margin), max(0, x-margin):min(width, x+w+margin)] = 0
    x, y, w, h = config.crop_xywh
    mask = mask[y:y+h, x:x+w].copy()
    b = config.border_px
    if b:
        mask[:b] = 0; mask[-b:] = 0; mask[:, :b] = 0; mask[:, -b:] = 0
    size = config.output_size_wh or [w, h]
    if size != [w, h]:
        mask = cv2.resize(mask, tuple(size), interpolation=cv2.INTER_AREA)
    return np.where(mask == 255, 255, 0).astype(np.uint8)


def screen_geometry(config: ScreenConfig):
    x, y, w, h = config.crop_xywh
    ow, oh = config.output_size_wh or [w, h]
    # OpenCV pixel-center resize relation (not the bounding-box edge relation).
    matrix = [[w/ow, 0, x+(w/ow-1)/2], [0, h/oh, y+(h/oh-1)/2], [0, 0, 1]]
    return {'status': 'uncalibrated_screen_crop', 'source_size_wh': config.source_size_wh,
            'crop_xywh': config.crop_xywh, 'output_size_wh': [ow, oh],
            'output_to_source_pixel_centers': matrix,
            'source_to_output_pixel_centers': np.linalg.inv(matrix).tolist(),
            'roi_coordinate_space': 'derived_image_zero_based_pixel_centers',
            'source_roi_coordinate_space': 'native_decoded_screen_pixels',
            'display_mode': config.display_mode, 'display_evidence': config.display_evidence,
            'calibration_status': config.calibration_status, 'camera_id': config.camera_id,
            'camera_label': config.camera_label, 'selected_window': config.selected_window,
            'additional_undistortion': False,
            'intrinsics_policy': 'no_measured_intrinsics; downstream_SfM_estimates_are_approximate'}


def screen_quality(image, mask, config: FrameConfig, previous=None):
    """Scene-only heuristics, not a camera-drop or semantic-occlusion detector."""
    import cv2
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # Erode before derivatives so UI/mask-boundary edges cannot contribute.
    valid = cv2.erode(mask, np.ones((5, 5), np.uint8)) > 0
    fraction = float(valid.mean())
    if fraction < .2:
        return {'valid_area_fraction': fraction, 'rejection_reasons': ['insufficient_valid_area'],
                'warnings': ['insufficient_valid_area'], 'similar_scene': False, 'discontinuity_suspected': False}
    values = gray[valid]
    blur = float(cv2.Laplacian(gray, cv2.CV_32F)[valid].var())
    over = float(np.mean(values >= 250)); dark = float(np.mean(values <= 5))
    change = None if previous is None else float(np.abs(gray.astype(float)-cv2.cvtColor(previous, cv2.COLOR_BGR2GRAY))[valid].mean())
    histogram = np.histogram(values, bins=16, range=(0, 256))[0].astype(float); histogram /= histogram.sum()
    distance = None
    if previous is not None:
        old = np.histogram(cv2.cvtColor(previous, cv2.COLOR_BGR2GRAY)[valid], bins=16, range=(0, 256))[0].astype(float)
        old /= old.sum(); distance = float(np.abs(histogram-old).sum()/2)
    warnings = []
    if blur < config.min_blur_variance:
        warnings.append('blur_or_low_texture')
    if over > config.max_overexposure_fraction or dark > .7 or float(values.std()) < 4:
        warnings.append('possible_occlusion_or_low_scene_information')
    # A jump can be camera motion, a cut, or a moved/resized UI window. Human
    # review is required; do not classify any of these as a verified event.
    jump = change is not None and change > 40 and distance > .35
    if jump:
        warnings.append('possible_scene_jump_or_layout_change')
    similar = change is not None and change < config.duplicate_mean_difference
    return {'valid_area_fraction': fraction, 'laplacian_variance': blur,
            'overexposure_fraction': over, 'dark_fraction': dark,
            'scene_mean_change': change, 'scene_histogram_distance': distance,
            'similar_scene': similar, 'discontinuity_suspected': jump,
            'warnings': warnings,
            'rejection_reasons': [w for w in warnings if w != 'possible_scene_jump_or_layout_change'] if config.quality_filter else []}


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def inspect_screen(path: Path, output: Path) -> dict:
    """Native-resolution preview; config must be confirmed before import."""
    probe = probe_video(path)
    if output.exists() and any(output.iterdir()):
        raise VideoPipelineError('output_not_empty_use_new_directory')
    output.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([shutil.which('ffmpeg') or 'ffmpeg', '-v', 'error', '-nostdin', '-n', '-noautorotate',
        '-i', str(path.resolve()), '-map', '0:v:0', '-frames:v', '1', str(output/'native_frame.png')],
        capture_output=True, timeout=60)
    if result.returncode:
        raise VideoPipelineError('screen_preview_decode_failed')
    _write_json(output/'probe.json', probe.to_dict())
    return {'probe': probe.to_dict(), 'preview': str((output/'native_frame.png').resolve()),
            'selection_required': True, 'coordinate_space': 'native_decoded_pixels'}


def prepare_screen(path: Path, output: Path, frames: FrameConfig, config: ScreenConfig, events) -> dict:
    import cv2
    from .feature_masks import SCREEN_SOURCE
    probe = probe_video(path)
    config.validate(probe.width, probe.height)
    select_frame_records([], frames)
    if probe.rotation not in (None, 0):
        raise VideoPipelineError('rotated_screen_requires_new_explicit_native_selection')
    if config.expected_source_sha256 and config.expected_source_sha256 != probe.source_sha256:
        raise VideoPipelineError('screen_selection_source_hash_mismatch')
    if output.exists() and any(output.iterdir()):
        raise VideoPipelineError('output_not_empty_use_new_directory')
    output.mkdir(parents=True, exist_ok=True)
    for name in ('images', 'raw', 'diagnostics'):
        (output/name).mkdir()
    mask = effective_mask(config)
    if np.mean(mask > 0) < .2:
        raise VideoPipelineError('insufficient_valid_area')
    cv2.imencode('.png', mask)[1].tofile(output/'feature_mask.png')
    _write_json(output/'screen_config.json', asdict(config))
    end = frames.start_s+frames.duration_s
    p = subprocess.run([shutil.which('ffprobe') or 'ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-read_intervals', f'%+{end}', '-show_frames', '-show_entries',
        'frame=pts,best_effort_timestamp_time,pts_time', '-of', 'json', str(path.resolve())],
        capture_output=True, text=True, encoding='utf-8', timeout=180)
    if p.returncode:
        raise VideoPipelineError('screen_frame_probe_failed')
    all_frames = json.loads(p.stdout).get('frames', [])
    selected = select_frame_records(all_frames, frames)
    if not selected or len(all_frames) > 100_000:
        raise VideoPipelineError('empty_or_excessive_screen_timeline')
    origin = float(all_frames[0].get('best_effort_timestamp_time', all_frames[0].get('pts_time')))
    records = []
    previous_t = None
    for index, row in enumerate(all_frames):
        t = float(row.get('best_effort_timestamp_time', row.get('pts_time')))
        if not math.isfinite(t) or (previous_t is not None and t <= previous_t):
            raise VideoPipelineError('invalid_screen_frame_pts')
        previous_t = t
        if t-origin >= end:
            break
        if row.get('pts') is None:
            raise VideoPipelineError('screen_original_pts_required')
        records.append({'frame_index': index, 'pts': row['pts'], 'pts_time_s': t,
                        'relative_time_s': t-origin, 'captured_at': None})
    chosen = {r['frame_index']: r for r in selected}
    previous = None; previous_pts = None; similar_since = None; pending_review = []
    timeline = []; warnings = []; decoded_count = 0
    command = [shutil.which('ffmpeg') or 'ffmpeg', '-v', 'error', '-nostdin', '-noautorotate',
        '-i', str(path.resolve()), '-map', '0:v:0', '-frames:v', str(len(records)),
        '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1']
    with (output/'decoder.log').open('wb') as err:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=err)
        try:
            for row in records:
                raw_bytes = proc.stdout.read(probe.width*probe.height*3)
                if len(raw_bytes) != probe.width*probe.height*3:
                    raise VideoPipelineError('screen_decode_count_or_dimensions_mismatch')
                decoded_count += 1
                if row['relative_time_s'] < frames.start_s:
                    continue
                image = np.frombuffer(raw_bytes, np.uint8).reshape(probe.height, probe.width, 3)
                cropped = crop_image(image, config)
                q = screen_quality(cropped, mask, frames, previous)
                if q['similar_scene']:
                    similar_since = similar_since if similar_since is not None else previous_pts
                else:
                    similar_since = None
                q['similar_duration_s'] = 0 if similar_since is None else row['pts_time_s']-similar_since
                if q['similar_duration_s'] >= .8:
                    q['warnings'].append('stationary_or_suspected_display_freeze_not_proven_drop')
                if previous_pts is not None and row['pts_time_s']-previous_pts > frames.max_gap_s:
                    q['warnings'].append('screen_pts_gap'); q['discontinuity_suspected'] = True
                if q['discontinuity_suspected'] or q['rejection_reasons']:
                    pending_review.append({'frame_index': row['frame_index'], 'pts_time_s': row['pts_time_s'],
                                           'reasons': q['warnings']})
                row.update(quality=q, selected=row['frame_index'] in chosen, image_path=None)
                if row['selected']:
                    item = chosen[row['frame_index']]
                    raw_path = output/'raw'/f'screen_{row["frame_index"]:08d}.png'
                    cv2.imencode('.png', image)[1].tofile(raw_path)
                    kept = not q['rejection_reasons']
                    dest = output/'images'/f'frame_{row["frame_index"]:08d}.png'
                    item.update(raw_path=raw_path.relative_to(output).as_posix(), raw_sha256=sha256_file(raw_path),
                        quality=q, kept=kept, image_path=None, image_sha256=None, captured_at=None,
                        size_wh=[cropped.shape[1], cropped.shape[0]],
                        discontinuity_before=bool(pending_review), interval_review=list(pending_review))
                    pending_review.clear()
                    if kept:
                        cv2.imencode('.png', cropped)[1].tofile(dest)
                        item.update(image_path=dest.relative_to(output).as_posix(), image_sha256=sha256_file(dest))
                        row['image_path'] = item['image_path']
                    if len(timeline) == 0 or row['frame_index'] in list(chosen)[::max(1, len(chosen)//8)]:
                        display = cropped.copy()
                        display[mask == 0] = (display[mask == 0]*.35 + np.array([0, 0, 160])).clip(0, 255)
                        cv2.imencode('.png', display)[1].tofile(output/'diagnostics'/f'valid_area_{row["frame_index"]:08d}.png')
                    events.emit('frame_kept' if kept else 'frame_rejected', stage='screen_decode',
                        video_pts_s=row['pts_time_s'], data={'frame_index': row['frame_index'],
                        'source_video_sha256': probe.source_sha256, 'exposure_time': None, 'quality': q})
                if q['warnings']:
                    warnings.append({'frame_index': row['frame_index'], 'pts_time_s': row['pts_time_s'], 'warnings': q['warnings']})
                timeline.append(row)
                previous = cropped; previous_pts = row['pts_time_s']
            proc.stdout.close()
            if proc.wait(timeout=30) != 0:
                raise VideoPipelineError('screen_decoder_failed_see_decoder_log')
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait(timeout=10)
            if not proc.stdout.closed:
                proc.stdout.close()
    if sha256_file(path) != probe.source_sha256:
        raise VideoPipelineError('source_changed_during_screen_import')
    _write_json(output/'source_timeline.json', {'timing_source': 'screen_display',
        'time_base': probe.time_base, 'source_sha256': probe.source_sha256, 'frames': timeline})
    from collections import Counter
    report = {'schema_version': 'go1loc.screen_quality.v1', 'analysed_display_frames': len(timeline),
        'decoded_from_origin': decoded_count, 'selected_count': len(selected),
        'kept_count': sum(r['kept'] for r in selected), 'warning_counts': dict(Counter(w for r in warnings for w in r['warnings'])),
        'quality_filter_enabled': frames.quality_filter,
        'kept_means': 'candidate_selected_for_features_not_quality_acceptance',
        'scene_laplacian_variance': {'min': min(r['quality'].get('laplacian_variance', 0) for r in timeline),
            'median': float(np.median([r['quality'].get('laplacian_variance', 0) for r in timeline])),
            'max': max(r['quality'].get('laplacian_variance', 0) for r in timeline)},
        'valid_area_fraction': float(np.mean(mask > 0)), 'warnings': warnings,
        'manual_checks_required': ['moving_mouse_or_body_occlusion', 'window_layout_stayed_fixed',
            'distinguish_stationary_camera_from_stream_freeze', 'review_all_scene_jump_candidates'],
        'limitations': ['heuristics_not_semantic_occlusion_or_layout_detection',
            'screen_updates_not_sensor_exposures', 'similarity_not_dropped_frame_evidence'],
        'route_segments': [], 'verified_continuous': False, 'execution_permitted': False}
    _write_json(output/'quality_report.json', report)
    result = {'schema_version': 'go1loc.frames.v1', 'source_kind': SCREEN_SOURCE,
        'capture_device_kind': 'go1_camera_via_screen_recording', 'probe': probe.to_dict(),
        'signature': {'video_sha256': probe.source_sha256, 'config': asdict(frames), 'screen_config': asdict(config)},
        'geometry': screen_geometry(config), 'frames': selected, 'candidate_count': len(selected),
        'kept_count': report['kept_count'], 'timing_source': 'screen_display',
        'hardware_synchronized': False, 'sensor_capture_time': None,
        'imported_at': datetime.now(timezone.utc).isoformat(),
        'feature_mask': {'path': 'feature_mask.png', 'sha256': sha256_file(output/'feature_mask.png'),
            'method': 'post_superpoint_pre_lightglue', 'margin_source_px': config.margin_px,
            'pixel_values': '255=allowed,0=excluded', 'images_blackened': False},
        'source_timeline': {'path': 'source_timeline.json', 'sha256': sha256_file(output/'source_timeline.json')},
        'quality_report': {'path': 'quality_report.json', 'sha256': sha256_file(output/'quality_report.json')},
        'route_segments': [], 'robot_access': False, 'execution_permitted': False}
    _write_json(output/'frames_manifest.json', result)
    return result
