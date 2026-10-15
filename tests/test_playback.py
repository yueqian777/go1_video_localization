"""Ordered image playback is visualization, never a traversability claim."""
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
import test_viser_viewer as ui
from go1loc.playback import playback_timeline, PlaybackClock


class TimelineTests(unittest.TestCase):
    def test_pts_order_keeps_missing_unregistered_and_gaps(self):
        rows = [{'image_path': 'z.png', 'pts_time_s': 20, 'frame_index': 30},
                {'image_path': 'a.png', 'pts_time_s': 10, 'frame_index': 0},
                {'pts_time_s': 11, 'frame_index': 3, 'kept': False}]
        cameras = [dict(name='a.png', pts_time_s=10)]
        data = playback_timeline({'frames': rows, 'source_kind': 'go1_screen_recording'}, cameras, Path.cwd())
        self.assertEqual([r['pts_time_s'] for r in data['frames']], [10, 11, 20])
        self.assertIsNone(data['frames'][1]['image_path'])
        self.assertIsNone(data['frames'][2]['camera_name'])
        self.assertEqual(data['frames'][0]['camera_name'], 'a.png')
        self.assertEqual(data['time_source'], 'screen_display')

    def test_unordered_or_invalid_times_are_not_invented(self):
        self.assertFalse(playback_timeline(None, [dict(name='a', pts_time_s=None)], Path.cwd())['available'])
        for value in (None, float('nan'), True):
            self.assertFalse(playback_timeline({'frames': [dict(pts_time_s=value)]}, [], Path.cwd())['available'])
        self.assertFalse(playback_timeline({'frames': [dict(pts_time_s=1), dict(pts_time_s=1)]}, [], Path.cwd())['available'])

    def test_clock_preserves_source_gaps_pause_resume_seek_and_end(self):
        clock = PlaybackClock([10, 10.5, 20])
        clock.play(0)
        self.assertEqual(clock.tick(1), 1)
        clock.pause(2)
        self.assertEqual(clock.tick(99), 1)
        clock.play(100)
        self.assertEqual(clock.tick(107), 1)
        self.assertEqual(clock.tick(108), 2)
        self.assertFalse(clock.playing)
        clock.play(200)
        self.assertEqual(clock.index, 0)
        clock.seek(1, 200)
        clock.set_speed(2, 200)
        self.assertEqual(clock.tick(205), 2)
        with self.assertRaises(ValueError): clock.seek(-1, 200)
        with self.assertRaises(ValueError): clock.set_speed(0, 200)


class PlaybackUITests(unittest.TestCase):
    setUp = ui.ApplicationTests.setUp

    def test_build_guide_button_only_generates_commands_and_clears_stale_on_error(self):
        video = self.root/'fixture.mp4'
        video.write_bytes(b'command generation only')
        self.app.build_video.value = str(video)
        self.app.build_output.value = str(self.root/'new-map')
        self.app.build_kind.value = '原始单路视频'
        count = len(self.app.jobs.submitted)
        self.app._generate_build_recipe(None)
        self.assertIn('build-map', self.app.build_commands.value)
        self.assertEqual(len(self.app.jobs.submitted), count)
        self.app.build_video.value = 'missing.mp4'
        self.app._guard(self.app._generate_build_recipe)(None)
        self.assertNotIn('build-map', self.app.build_commands.value)

    def payload(self):
        model = ui.model()
        model['cameras'][0]['image_path'] = str(self.photo)
        model['cameras'][0]['pts_time_s'] = 0.
        model['playback'] = playback_timeline({'source_kind': 'video', 'frames': [
            dict(image_path=str(self.photo), pts_time_s=0, frame_index=0),
            dict(image_path=str(self.other), pts_time_s=2, frame_index=4)]},
            [dict(name=self.photo.name, pts_time_s=0)], self.root)
        model['playback']['frames'][0]['camera_name'] = 'a.jpg'
        return model

    def test_playback_never_sets_start_or_builds_route_and_unregistered_clears_pose(self):
        model = self.payload()
        self.app.controller.replace_model(model)
        self.app._reset_playback(model)
        count = len(self.app.jobs.submitted)
        self.app._playback_seek(0)
        self.assertTrue(self.app.reference_image.visible)
        self.assertIn('/reference/camera', self.server.scene.active)
        self.app._playback_seek(1)
        self.assertTrue(self.app.reference_image.visible)
        self.assertNotIn('/reference/camera', self.server.scene.active)
        self.assertIn('未在当前分量注册', self.app.playback_info.value)
        self.assertIsNone(self.app.current_start)
        self.assertIsNone(self.app.plan_result)
        self.assertEqual(len(self.app.jobs.submitted), count)

    def test_map_switch_pauses_and_missing_image_hides_old_frame(self):
        model = self.payload()
        self.app.controller.replace_model(model)
        self.app._reset_playback(model)
        self.app._playback_seek(0)
        self.app.playback_clock.play(0)
        self.app._reset_playback(ui.model())
        self.assertFalse(self.app.playback_clock.playing)
        self.assertFalse(self.app.reference_image.visible)
        model['playback']['frames'][1]['image_path'] = str(self.root/'missing.png')
        self.app._reset_playback(model)
        self.app._playback_seek(1)
        self.assertFalse(self.app.reference_image.visible)
        self.assertIn('图片缺失', self.app.playback_info.value)

    def test_programmatic_slider_update_does_not_pause_playback(self):
        self.app._reset_playback(self.payload())
        self.app.playback_clock.play(0)
        self.app._playback_slider_changed(SimpleNamespace(client=None))
        self.assertTrue(self.app.playback_clock.playing)

    def test_pause_between_ticks_keeps_photo_and_index_in_sync(self):
        model = self.payload()
        self.app.controller.replace_model(model)
        self.app._reset_playback(model)
        self.app._playback_seek(0)
        self.app.playback_clock.play(0)
        with patch('go1loc.playback.time.monotonic', return_value=2):
            self.app._pause_playback()
        self.assertEqual(self.app._playback_rendered, 1)
        self.assertNotIn('/reference/camera', self.server.scene.active)

    def test_play_pause_speed_slider_and_follow_callbacks(self):
        self.app.controller.replace_model(self.payload())
        self.app._reset_playback(self.payload())
        self.app.follow_reference.value = True
        with patch('go1loc.playback.time.monotonic', return_value=10):
            self.app._playback_play(None)
            self.app.playback_speed.value = '2×'
            self.app._playback_speed_changed(None)
            self.app._playback_tick(11)
            self.assertFalse(self.app.playback_clock.playing)
            self.app._playback_restart(None)
            self.app._playback_slider_changed(SimpleNamespace(client=self.client, target=SimpleNamespace(value=1)))
            self.assertFalse(self.app.playback_clock.playing)
            self.app._playback_step(-1)
            self.assertEqual(self.app.playback_clock.index, 0)


if __name__ == '__main__': unittest.main()
