"""Exercise real upload callbacks, workers and native snapshots (no browser automation).

Uses an explicitly supplied video and the user's previously reviewed screen config.
Writes all outputs to a new validation directory. No robot access or accuracy claim.
"""
import argparse
from pathlib import Path
import time
from types import SimpleNamespace

from go1loc.jobs import atomic_json, read_json
from go1loc.video import sha256_file
from go1loc.viser_viewer import ViewerApplication


def wait_for(test, seconds, detail):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        if test():
            return
        time.sleep(.15)
    raise RuntimeError('timeout: '+detail())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--video', type=Path, required=True)
    parser.add_argument('--screen-config', type=Path, required=True)
    parser.add_argument('--map', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8787)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Use a new validation directory')
    before = sha256_file(args.map/'map_manifest.json')
    video_hash = sha256_file(args.video)
    config = read_json(args.screen_config)
    if config.get('expected_source_sha256') != video_hash:
        raise ValueError('screen config does not belong to the supplied video')
    app = ViewerApplication(args.map, args.output/'viewer', port=args.port)
    records, last = [], None
    try:
        wait_for(lambda: app.controller.model is not None, 60, lambda: app.status.value)
        with app._state_lock:
            app._web_upload(SimpleNamespace(target=SimpleNamespace(value=SimpleNamespace(
                name=args.video.name, content=args.video.read_bytes()))))
        wait_for(lambda: app._web_preview_id is not None, 90, lambda: app.web_status.value)
        with app._state_lock:
            assert app.web_native.visible
            app.web_kind.value = 'Go1 界面录屏'
            assert app.web_native.image.shape[:2] == (config['source_size_wh'][1], config['source_size_wh'][0])
            app.web_crop_xy.value = tuple(config['crop_xywh'][:2])
            app.web_crop_wh.value = tuple(config['crop_xywh'][2:])
            app.web_window.value = config['selected_window']
            app.web_camera_label.value = config['camera_label']
            for rect in config['exclude_rectangles']:
                app.web_mask_xy.value, app.web_mask_wh.value = tuple(rect[:2]), tuple(rect[2:])
                app._web_add_mask(None)
            app.web_explore.value = True  # Explicit earlier user authorization: existing 30s exploratory map.
            app.web_confirm.value = True
            app._web_start(None)
            assert app.controller.model is None
            try:
                app._web_start(None)
            except ValueError:
                pass
            else:
                raise AssertionError('Duplicate start was not rejected')
            atomic_json(args.output/'before_final.json', dict(map_manifest_exists=(app.map_dir/'map_manifest.json').exists(),
                status=app.web_status.value, preview_shape=list(app.web_native.image.shape),
                execution_permitted=False))
        deadline = time.monotonic()+1200
        while time.monotonic() < deadline:
            with app._state_lock:
                state = app.web_build.state()
                model = app.controller.model
                key = (state['status'], state.get('build', {}).get('stage'),
                       model['mode'] if model else None, model['revision'] if model else None)
                if key != last:
                    record = dict(status=state['status'], stage=state.get('build', {}).get('stage'),
                        mode=model['mode'] if model else None, revision=model['revision'] if model else None,
                        map_manifest_exists=(app.map_dir/'map_manifest.json').exists(),
                        cameras=len(model['cameras']) if model else 0,
                        points=model['total_points'] if model else 0,
                        status_text=app.web_status.value, execution_permitted=False)
                    records.append(record)
                    atomic_json(args.output/'progress.json', records)
                    print({k: v for k, v in record.items() if k != 'status_text'}, flush=True)
                    last = key
                if state['status'] == 'failed':
                    raise RuntimeError(state.get('error'))
                if model and model['mode'] == 'final_map_replay' and model['map_id'].startswith('web-'):
                    break
            time.sleep(.15)
        else:
            raise RuntimeError('build did not reach final display: '+app.status.value)
        with app._state_lock:
            assert app.current_start is None and app.plan_result is None
            assert app.playback_data['available']
            app._playback_seek(0)
            assert app.reference_image.visible
            final = app.controller.model
            assert final['cameras']
            assert before == sha256_file(args.map/'map_manifest.json')
            assert video_hash == sha256_file(args.video)
            atomic_json(args.output/'validation.json', dict(
                scope='real_video_server_callbacks_and_workers_not_browser_render',
                exploratory=True, source_kind='go1_screen_recording', source_sha256=video_hash,
                original_manifest_unchanged=True, original_video_unchanged=True,
                map_dir=str(app.map_dir), build_job=app.web_build.record['build_job'],
                cameras=len(final['cameras']), points=final['total_points'], playback_frames=len(app.playback_data['frames']),
                automatic_snapshots=[r['revision'] for r in records if r['mode'] == 'build_snapshot'],
                snapshot_visible_before_final=any(r['mode'] == 'build_snapshot' and not r['map_manifest_exists'] for r in records),
                final_display_loaded=True, duplicate_start_rejected=True, execution_permitted=False))
        app.close()
        app = ViewerApplication(args.map, args.output/'viewer', port=args.port)
        wait_for(lambda: app.controller.model is not None and
                 app.controller.model['map_id'] == final['map_id'], 90, lambda: app.status.value)
        with app._state_lock:
            app.web_snapshot.value = '1'
            app._web_history(None)
        wait_for(lambda: app.controller.model is not None and app.controller.mode == 'build_snapshot'
                 and app.controller.model['revision'] == 1, 60, lambda: app.status.value)
        time.sleep(1.)
        with app._state_lock:
            assert not app.web_follow.value and app.controller.model['revision'] == 1
            app._web_final(None)
        wait_for(lambda: app.controller.mode == 'final_map_replay', 60, lambda: app.status.value)
        with app._state_lock:
            builds = [s for s in app.web_build.jobs.list_jobs() if s['kind'] == 'build_video']
            assert len(builds) == 1
            report = read_json(args.output/'validation.json')
            report.update(reopen_restores_final_without_rebuild=True,
                          manual_snapshot_pause_and_return_to_final=True)
            atomic_json(args.output/'validation.json', report)
        print('Saved:', args.output/'validation.json', flush=True)
    finally:
        app.close()


if __name__ == '__main__':
    main()
