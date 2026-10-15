"""Read-only real-map playback checks, not browser screenshots or route acceptance."""
import argparse
from pathlib import Path
import time
from go1loc.jobs import atomic_json
from go1loc.viser_viewer import ViewerApplication
from go1loc.video import sha256_file


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--map', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8786)
    args = parser.parse_args()
    before = sha256_file(args.map/'map_manifest.json')
    app = ViewerApplication(args.map, args.output, port=args.port)
    try:
        deadline = time.monotonic()+40
        while app.controller.model is None:
            if time.monotonic() > deadline:
                raise RuntimeError('map load timed out: '+app.status.value)
            time.sleep(.1)
        with app._state_lock:
            rows = app.playback_data['frames']
            assert rows and app.playback_data['available']
            checks = []
            for index in sorted({0, len(rows)//2, len(rows)-1}):
                app._playback_seek(index)
                assert app.reference_image.visible
                assert app.playback_clock.index == index
                checks.append({'index': index, 'source_frame': rows[index]['frame_index'],
                               'pts_s': rows[index]['pts_time_s'], 'image_readable': True})
            app._playback_restart(None)
        time.sleep(.8)
        with app._state_lock:
            app._pause_playback()
            moved = app.playback_clock.index
            assert moved > 0
        time.sleep(.6)
        with app._state_lock:
            assert app.playback_clock.index == moved
            assert app.current_start is None and app.plan_result is None
            assert before == sha256_file(args.map/'map_manifest.json')
            atomic_json(args.output/'validation.json', dict(
                scope='real_existing_map_server_controls_not_browser_render', frames=len(rows),
                source_kind=app.playback_data['source_kind'], time_source=app.playback_data['time_source'],
                samples=checks, timed_play_advanced_to=moved, pause_stable=True,
                original_manifest_unchanged=True, execution_permitted=False))
        print('Saved:', args.output/'validation.json')
    finally:
        app.close()


if __name__ == '__main__': main()
