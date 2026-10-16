"""Selection gesture contracts, not a claim of browser-rendered QA."""
import asyncio
from types import SimpleNamespace
import unittest

import numpy as np
from PIL import Image

import test_viser_viewer as fixtures


class SelectionTests(unittest.TestCase):
    setUp = fixtures.ApplicationTests.setUp

    def open(self):
        self.app._connect(self.client)
        self.app._open_photo(self.event)

    def drag(self, handle, phase, start, end):
        # World coordinates of this 400x200 raw photo on a height-2 plane.
        world = lambda p: (p[0] / 100 - 2, 1 - p[1] / 100, 10000.)
        event = SimpleNamespace(client=self.client, client_id=1, target=handle,
            phase=phase, start_position=world(start), end_position=world(end))
        asyncio.run(handle.drag(event))

    def select(self):
        photo = self.client.scene.active['/roi/photo']
        self.drag(photo, 'start', (50, 30), (55, 35))
        self.drag(photo, 'update', (50, 30), (180, 120))
        self.drag(photo, 'end', (50, 30), (200, 130))

    def test_open_does_not_implicitly_select_whole_photo(self):
        self.open()
        self.assertTrue(self.app.roi_submit.disabled)
        self.assertIn('尚未框选', self.app.roi_status.value)
        with self.assertRaisesRegex(ValueError, '请先.*框选'):
            self.app._locate_object(self.event)

    def test_drag_draws_live_outline_and_keeps_it_on_release(self):
        self.open()
        photo = self.client.scene.active['/roi/photo']
        self.drag(photo, 'start', (50, 30), (60, 40))
        self.assertIn('/roi/outline', self.client.scene.active)
        self.assertTrue(self.app.roi_submit.disabled)
        self.drag(photo, 'end', (50, 30), (200, 130))
        self.assertFalse(self.app.roi_submit.disabled)
        self.assertIn('已选中', self.app.roi_status.value)
        self.assertTrue(self.app.roi_preview.visible)
        np.testing.assert_allclose(self.app._roi_selection['roi'], [50, 30, 200, 130])
        self.assertIn('/roi/handle-br', self.client.scene.active)

    def test_resize_and_move_clamp_to_raw_image_and_do_not_leak_handles(self):
        self.open(); self.select()
        corner = self.client.scene.active['/roi/handle-br']
        self.drag(corner, 'start', (200, 130), (220, 140))
        self.drag(corner, 'end', (200, 130), (250, 150))
        np.testing.assert_allclose(self.app._roi_selection['roi'], [50, 30, 250, 150])
        center = self.client.scene.active['/roi/handle-move']
        self.drag(center, 'start', (150, 90), (160, 100))
        self.drag(center, 'end', (150, 90), (500, 500))
        np.testing.assert_allclose(self.app._roi_selection['roi'], [200, 80, 400, 200])
        self.assertIs(corner, self.client.scene.active['/roi/handle-br'])
        self.app._open_photo(self.event)
        self.assertEqual(len(self.client.scene.active), 7)

    def test_reversed_drag_and_clipping(self):
        self.open()
        photo = self.client.scene.active['/roi/photo']
        self.drag(photo, 'start', (250, 150), (240, 140))
        self.drag(photo, 'end', (250, 150), (-10, -10))
        np.testing.assert_allclose(self.app._roi_selection['roi'], [0, 0, 250, 150])

    def test_zero_area_invalid_numbers_and_recovery(self):
        self.open()
        photo = self.client.scene.active['/roi/photo']
        self.drag(photo, 'start', (50, 30), (50, 30))
        self.drag(photo, 'end', (50, 30), (50, 30))
        self.assertTrue(self.app.roi_submit.disabled)
        self.select()
        self.app.roi_min.value = (float('nan'), 0)
        self.app.roi_min.update(self.event)
        self.assertTrue(self.app.roi_submit.disabled)
        self.assertIn('无效', self.app.roi_status.value)
        self.app.roi_min.value = (20., 20.)
        self.app.roi_min.update(self.event)
        self.assertFalse(self.app.roi_submit.disabled)

    def test_change_photo_or_same_path_bytes_refuses_old_selection(self):
        self.open(); self.select()
        self.app.photo.value = str(self.other)
        with self.assertRaisesRegex(ValueError, '照片.*变化'):
            self.app._locate_object(self.event)
        self.app._open_photo(self.event)
        self.assertFalse(self.app._roi_selection)
        self.select()
        Image.new('RGB', (400, 200), 'green').save(self.other)
        with self.assertRaisesRegex(ValueError, '照片.*变化'):
            self.app._locate_object(self.event)

    def test_submission_uses_visible_box_and_preserves_confirmation(self):
        self.open(); self.select()
        self.app._locate_object(self.event)
        kind, params = self.app.jobs.submitted[-1]
        self.assertEqual(kind, 'locate_object')
        np.testing.assert_allclose(params['roi'], [50, 30, 200, 130])
        self.assertIn('已提交', self.app.roi_status.value)
        self.assertIn('/roi/outline', self.client.scene.active)

    def test_edit_invalidates_old_object_route_and_late_worker(self):
        self.open(); self.select()
        self.app.target_result = {'status': 'supported'}
        self.app.plan_result = fixtures.plan_fixture()
        self.app._locate_object(self.event)
        revision = self.app._task_revision
        self.select()
        self.assertGreater(self.app._task_revision, revision)
        self.assertIsNone(self.app.target_result)
        self.assertIsNone(self.app.plan_result)

    def test_close_and_query_reset_remove_every_overlay(self):
        self.open(); self.select()
        handles = list(self.client.scene.active.values())
        self.app._invalidate_target(clear_query=True)
        self.assertTrue(all(h.removed for h in handles))
        self.assertFalse(self.client.scene.active)
        self.assertFalse(self.app._roi_selection)
        self.assertTrue(self.app.roi_submit.disabled)

    def test_clear_and_return_map(self):
        self.open(); self.select()
        self.app._clear_roi(self.event)
        self.assertEqual(list(self.client.scene.active), ['/roi/photo'])
        self.assertTrue(self.app.roi_submit.disabled)
        self.select()
        self.app._return_map(self.event)
        self.assertFalse(self.client.scene.active)

    def test_two_tabs_share_visible_box_and_supersede_unfinished_drag(self):
        self.open(); self.select()
        second = SimpleNamespace(client_id=2, scene=fixtures.Scene(), camera=SimpleNamespace(aspect=1.))
        second_event = SimpleNamespace(client=second, client_id=2)
        self.app._open_photo(second_event)
        self.assertIn('/roi/outline', second.scene.active)
        handle = self.client.scene.active['/roi/handle-br']
        self.drag(handle, 'start', (200, 130), (205, 135))
        self.app._select_roi(self.app._photo_states[2], [10, 20, 100, 110])
        self.drag(handle, 'end', (200, 130), (300, 160))
        np.testing.assert_allclose(self.app._roi_selection['roi'], [10, 20, 100, 110])
        self.assertFalse(self.app.roi_submit.disabled)
        self.app.photo.value = str(self.other)
        self.app._open_photo(second_event)
        self.assertFalse(self.client.scene.active)

    def test_retired_handle_and_tilted_photo_cannot_change_roi(self):
        self.open(); self.select()
        old = self.client.scene.active['/roi/photo']
        self.app._open_photo(self.event)
        self.drag(old, 'start', (20, 20), (50, 50))
        np.testing.assert_allclose(self.app._roi_selection['roi'], [50, 30, 200, 130])
        self.client.camera.wxyz = (1, 0, 0, 0)
        self.drag(self.client.scene.active['/roi/photo'], 'start', (20, 20), (50, 50))
        self.assertTrue(self.app.roi_submit.disabled)
        self.assertIn('倾斜', self.app.roi_status.value)

    def test_path_edit_and_tampered_numeric_input_are_not_silently_used(self):
        self.open(); self.select()
        self.app.roi_max.value = (300, 170)
        with self.assertRaisesRegex(ValueError, '不一致'):
            self.app._locate_object(self.event)
        self.app.photo.value = str(self.other)
        self.app.photo.update(self.event)
        self.assertFalse(self.app._roi_selection)
        self.assertFalse(self.client.scene.active)
        self.assertEqual(self.app.query_path.value, '')

    def test_reloading_saved_object_restores_roi_preview(self):
        fixtures.ApplicationTests.evidence_files(self)
        self.app.load_object(self.root/'object_result.json')
        self.assertIn('计算完成', self.app.roi_status.value)
        self.assertTrue(self.app.roi_preview.visible)
        self.app._open_photo(self.event)
        self.assertIn('/roi/outline', self.client.scene.active)
        np.testing.assert_allclose(self.app._roi_selection['roi'], [0, 0, 100, 100])

    def test_missing_file_disables_submit_and_hides_old_green_box(self):
        self.open(); self.select()
        self.photo.unlink()  # disposable test fixture, not user input
        with self.assertRaisesRegex(ValueError, '不可读'):
            self.app._locate_object(self.event)
        self.assertTrue(self.app.roi_submit.disabled)
        self.assertFalse(self.client.scene.active['/roi/outline'].visible)

    def test_cancel_or_late_end_without_start_does_nothing(self):
        self.open()
        photo = self.client.scene.active['/roi/photo']
        self.drag(photo, 'end', (1, 1), (30, 30))
        self.assertIsNone(self.app._roi_selection)
        with self.assertRaisesRegex(ValueError, 'browser_client'):
            self.app._open_photo(SimpleNamespace(client=None))

    def test_tampered_source_cannot_finish_gesture(self):
        self.open()
        photo = self.client.scene.active['/roi/photo']
        self.drag(photo, 'start', (50, 30), (55, 35))
        Image.new('RGB', (400, 200), 'yellow').save(self.photo)
        self.drag(photo, 'end', (50, 30), (200, 130))
        self.assertIn('照片内容已变化', self.app.roi_status.value)
        self.assertTrue(self.app.roi_submit.disabled)

    def test_wide_photo_is_fitted_and_raw_coordinates_are_not_thumbnail_coordinates(self):
        Image.new('RGB', (3200, 800), 'white').save(self.photo)
        self.client.camera.aspect = 1.
        self.open()
        state = self.app._photo_states[1]
        self.app._select_roi(state, [1000, 200, 2400, 600])
        self.assertEqual(state['width'], 3200)
        self.assertEqual(state['handle'].kwargs['image'].shape, (400, 1600, 3))
        self.assertGreater(self.client.camera.position[2] - 10000, 8.)
        self.assertEqual(self.app._validated_selection()[1], [1000, 200, 2400, 600])
        # A one-pixel ROI at the raw-image edge must survive thumbnail rounding.
        self.app._select_roi(state, [3199, 799, 3200, 800])
        self.assertTrue(self.app.roi_preview.visible)


class GeometryTests(unittest.TestCase):
    def test_all_four_corners_and_clipping_keep_opposite_corner_fixed(self):
        from go1loc.photo_selection import gesture_roi
        for key, expected in [('tl', [0, 0, 200, 150]), ('tr', [50, 0, 51, 150]),
                              ('bl', [0, 30, 200, 31]), ('br', [50, 30, 51, 31])]:
            with self.subTest(key=key):
                np.testing.assert_allclose(gesture_roi(key, [50, 30, 200, 150], np.array([100, 100]),
                    np.array([-1000, -1000]), 400, 200), expected)

    def test_nonfinite_bad_shape_and_out_of_bounds(self):
        from go1loc.photo_selection import checked_roi, world_pixel
        for roi in [[0, 0, float('nan'), 20], [-1, 0, 20, 20], [0, 0, 401, 200], [1, 2, 3]]:
            with self.assertRaises(ValueError):
                checked_roi(roi, 400, 200)
        for point in [(float('nan'), 1, 2), (1, 2)]:
            with self.assertRaises(ValueError):
                world_pixel(point, 400, 200)

    def test_real_viser_handles_accept_drag_and_overlay_updates(self):
        # Actual native API serialization; no browser automation or rendered QA.
        import viser
        from go1loc.photo_selection import PhotoSelectionControls
        server = viser.ViserServer(host='127.0.0.1', port=0, verbose=False)
        try:
            app = PhotoSelectionControls()
            state = {'width': 400, 'height': 200, 'client': SimpleNamespace(scene=server.scene), 'overlays': {}}
            app._draw_roi(state, [50, 30, 200, 130], handles=True)
            app._draw_roi(state, [60, 40, 250, 140], handles=True)
            self.assertEqual(len(state['overlays']), 6)
            self.assertEqual(state['overlays']['outline'].points.shape, (4, 2, 3))
            for handle in state['overlays'].values():
                handle.remove()
        finally:
            server.stop()


if __name__ == '__main__':
    unittest.main()
