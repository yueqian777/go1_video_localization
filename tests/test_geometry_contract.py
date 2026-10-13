import math
import unittest

import numpy as np

from go1loc.geometry import (
    camera_center_from_colmap,
    colmap_world_to_camera_to_camera_to_world,
    project_points_pinhole,
    quaternion_wxyz_to_rotation,
)


class GeometryContractTests(unittest.TestCase):
    def test_colmap_pose_is_inverted_to_t_wc(self):
        rotation_cw = np.eye(3)
        translation_cw = np.array([1.0, 2.0, 3.0])
        rotation_wc, translation_wc = colmap_world_to_camera_to_camera_to_world(
            rotation_cw, translation_cw
        )
        np.testing.assert_allclose(rotation_wc, np.eye(3))
        np.testing.assert_allclose(translation_wc, [-1.0, -2.0, -3.0])
        np.testing.assert_allclose(
            camera_center_from_colmap(rotation_cw, translation_cw), [-1.0, -2.0, -3.0]
        )

    def test_quaternion_order_is_wxyz(self):
        q = np.array([math.cos(math.pi / 4), 0.0, 0.0, math.sin(math.pi / 4)])
        r = quaternion_wxyz_to_rotation(q)
        np.testing.assert_allclose(r @ np.array([1.0, 0.0, 0.0]), [0.0, 1.0, 0.0], atol=1e-7)

    def test_projection_uses_pixel_center_contract(self):
        xyz = np.array([[0.0, 0.0, 2.0], [1.0, 0.0, 2.0]])
        uv = project_points_pinhole(xyz, fx=100.0, fy=100.0, cx=50.0, cy=40.0)
        np.testing.assert_allclose(uv, [[50.0, 40.0], [100.0, 40.0]])


if __name__ == "__main__":
    unittest.main()
