"""Real local recording -> UI callbacks -> workers -> READY/frozen map.

No robot connection or browser automation. This validates server UI handlers,
not browser rendering, camera calibration, localization accuracy or navigation.
"""
import argparse
from pathlib import Path
import time

from go1loc.jobs import atomic_json, read_json
from go1loc.video import sha256_file
from go1loc.web_capture import capture_path, capture_digest
from go1loc.viser_viewer import ViewerApplication


def wait_for(check, timeout, message):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.2)
    raise RuntimeError('timeout: '+message())


def motion_diagnostic(prepared):
    """Image-plane displacement only, not a metric parallax/accuracy estimate."""
    import cv2
    import numpy as np
    from PIL import Image
    manifest = read_json(Path(prepared)/'frames_manifest.json')
    rows = [r for r in manifest['frames'] if r['kept']]
    gray = lambda r: cv2.cvtColor(np.asarray(Image.open(Path(prepared)/r['image_path'])), cv2.COLOR_RGB2GRAY)
    base = gray(rows[0])
    corners = cv2.goodFeaturesToTrack(base, maxCorners=500, qualityLevel=.02, minDistance=10)
    records = []
    for i in sorted({0, len(rows)//2, len(rows)-1}):
        other = gray(rows[i])
        moved, status, _ = cv2.calcOpticalFlowPyrLK(base, other, corners, None)
        valid = status.ravel() == 1
        displacement = np.linalg.norm(moved[valid]-corners[valid], axis=2).ravel()
        records.append(dict(relative_time_s=rows[i]['relative_time_s'], tracked_points=int(valid.sum()),
            median_displacement_px=float(np.median(displacement)), p95_displacement_px=float(np.percentile(displacement, 95))))
    return dict(candidate_frames=manifest['candidate_count'], kept_frames=manifest['kept_count'],
        source_kind=manifest['source_kind'], timing_source=manifest['timing_source'], samples=records,
        meaning='2d_feature_displacement_not_metric_motion_not_exposure_or_accuracy_acceptance')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture-id', required=True)
    parser.add_argument('--map', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--duration', type=float, default=30.)
    parser.add_argument('--port', type=int, default=8791)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Use a new output directory')
    source = capture_path(args.capture_id)
    original = capture_digest(source)
    old_map_hash = sha256_file(args.map/'map_manifest.json')
    app = ViewerApplication(args.map, args.output/'viewer', port=args.port)
    records, last = [], None
    try:
        wait_for(lambda: app.controller.model is not None, 120, lambda: app.status.value)
        with app._state_lock:
            app._web_refresh_captures(None)
            app.web_capture_choice.value = next(k for k, v in app._web_capture_options.items() if v == args.capture_id)
            app._web_select_capture(None)
        wait_for(lambda: app._web_preview_id is not None, 180, lambda: app.web_status.value)
        with app._state_lock:
            assert app._web_settings()['kind'] == 'capture'
            assert app.web_native.image.shape[:2] == (720, 1280)
            app.web_duration.value = args.duration
            app.web_interval.value = .5
            app.web_explore.value = False
            app.web_confirm.value = True
            app._web_start(None)
            assert app.controller.model is None
        deadline = time.monotonic()+1800
        while time.monotonic() < deadline:
            with app._state_lock:
                state, model = app.web_build.state(), app.controller.model
                key = (state['status'], state.get('build', {}).get('stage'),
                       model['mode'] if model else None, model['revision'] if model else None)
                if key != last:
                    record = dict(status=state['status'], stage=state.get('build', {}).get('stage'),
                        mode=model['mode'] if model else None, revision=model['revision'] if model else None,
                        cameras=len(model['cameras']) if model else 0, points=model['total_points'] if model else 0,
                        final_manifest_exists=(app.map_dir/'map_manifest.json').exists(), execution_permitted=False)
                    records.append(record)
                    atomic_json(args.output/'progress.json', records)
                    print(record, flush=True)
                    last = key
                if state['status'] == 'failed':
                    raise RuntimeError(state.get('error', 'build failed'))
                if model and model['mode'] == 'final_map_replay' and model['map_id'].startswith('web-'):
                    break
            time.sleep(.2)
        else:
            raise RuntimeError('build timeout')
        from go1loc.route_workflow import prepare_review
        review = prepare_review(app.map_dir)
        atomic_json(args.output/'route_candidates.json', review)
        manifest = read_json(app.map_dir/'map_manifest.json')
        assert manifest['source_kind'] == 'orbbec_rgb_mjpeg'
        assert manifest['timing_source'] == 'agent_read_monotonic_not_exposure'
        assert original == capture_digest(source)
        assert old_map_hash == sha256_file(args.map/'map_manifest.json')
        assert 'unsupported_source_kind' not in [d['reason'] for d in review['diagnostics']]
        with app._state_lock:
            app._playback_seek(0)
            assert app.reference_image.visible
            report = dict(scope='real_recording_server_callbacks_workers_not_browser_render',
                capture_id=args.capture_id, source_sha256=original,
                source_kind=manifest['source_kind'], timing_source=manifest['timing_source'],
                calibrated=False, exploratory=False, map_dir=str(app.map_dir),
                build_job=app.web_build.record['build_job'],
                candidate_frames=manifest['frames_manifest']['candidate_count'],
                kept_frames=manifest['frames_manifest']['kept_count'],
                registered_frames=manifest['num_registered_images'], points=manifest['num_points3D'],
                units=manifest['units'], source_and_previous_map_unchanged=True,
                displayed_snapshots=[r['revision'] for r in records if r['mode'] == 'build_snapshot'],
                snapshot_visible_before_final=any(r['mode'] == 'build_snapshot' and not r['final_manifest_exists'] for r in records),
                final_display_loaded=True, playback_frames=len(app.playback_data['frames']),
                route_candidates=len(review['candidates']), route_segments_approved=0,
                accuracy_evaluated=False, real_route_acceptance=False, execution_permitted=False)
            atomic_json(args.output/'validation.json', report)
        print('VALIDATED', report, flush=True)
    except Exception as exc:
        report = dict(error=str(exc), execution_permitted=False,
            original_source_unchanged=original == capture_digest(source),
            previous_map_unchanged=old_map_hash == sha256_file(args.map/'map_manifest.json'))
        state = app.web_build.state() or {}
        prepared = Path(state.get('job_dir', ''))/'output/prepared'
        if (prepared/'frames_manifest.json').is_file():
            report['image_motion'] = motion_diagnostic(prepared)
        atomic_json(args.output/'failure.json', report)
        raise
    finally:
        app.close()


if __name__ == '__main__':
    main()
