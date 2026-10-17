"""Keep geometry visible without the two intrusive historical-view labels."""
import unittest

import test_viser_viewer as fixtures

from go1loc.viser_viewer import SceneController


class UnobstructedSceneTests(unittest.TestCase):
    def test_model_modes_have_points_and_frusta_without_center_text(self):
        for mode in ('final_map_replay', 'build_snapshot'):
            with self.subTest(mode=mode):
                server = fixtures.Server()
                controller = SceneController(server)
                payload = fixtures.model()
                payload['mode'] = mode
                controller.replace_model(payload)
                self.assertIn('/model/points', server.scene.active)
                self.assertIn('/model/camera/0', server.scene.active)
                self.assertNotIn('/model/label', server.scene.active)
                self.assertEqual(controller.model, payload)

    def test_other_pose_warnings_still_exist(self):
        server = fixtures.Server()
        controller = SceneController(server)
        controller.pose('query', fixtures.np.eye(4), color=(1, 2, 3), label='not current position')
        self.assertIn('/query/label', server.scene.active)


class UnobstructedReferenceTests(unittest.TestCase):
    setUp = fixtures.ApplicationTests.setUp

    def test_reference_selection_preserves_frustum_photo_and_sidebar_not_scene_label(self):
        self.app._show_frame(self.event)
        self.assertIn('/reference/camera', self.server.scene.active)
        self.assertNotIn('/reference/label', self.server.scene.active)
        self.assertTrue(self.app.reference_image.visible)
        self.assertTrue(self.app.reference_info.value)
        self.assertFalse(self.app.follow_reference.value)


if __name__ == '__main__':
    unittest.main()
