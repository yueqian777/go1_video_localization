import unittest

import numpy as np

from go1loc.object_grounding import support_from_roi_matches


class ObjectGroundingTests(unittest.TestCase):
    def test_deduplicates_point_ids_and_returns_extent(self):
        points = {
            10: np.array([1.0, 2.0, 3.0]),
            11: np.array([2.0, 2.0, 3.0]),
            12: np.array([1.0, 4.0, 3.5]),
        }
        support = support_from_roi_matches(
            roi=(0, 0, 100, 100),
            observations=[
                {"u": 10, "v": 20, "point3D_id": 10, "frame_id": "f1"},
                {"u": 11, "v": 21, "point3D_id": 10, "frame_id": "f2"},
                {"u": 12, "v": 22, "point3D_id": 11, "frame_id": "f1"},
            ],
            points3d=points,
            min_points=2,
        )
        self.assertEqual(support.status, "supported")
        self.assertEqual(support.supporting_point3D_ids, [10, 11])
        np.testing.assert_allclose(support.object_surface_position, [1.5, 2.0, 3.0])
        self.assertEqual(support.supporting_frame_ids, ["f1", "f2"])

    def test_multiple_spatial_clusters_are_not_averaged(self):
        points = {
            1: np.array([0.0, 0.0, 0.0]),
            2: np.array([0.1, 0.0, 0.0]),
            3: np.array([10.0, 0.0, 0.0]),
            4: np.array([10.1, 0.0, 0.0]),
        }
        support = support_from_roi_matches(
            roi=(0, 0, 100, 100),
            observations=[
                {"u": 1, "v": 1, "point3D_id": 1, "frame_id": "a"},
                {"u": 2, "v": 1, "point3D_id": 2, "frame_id": "a"},
                {"u": 3, "v": 1, "point3D_id": 3, "frame_id": "b"},
                {"u": 4, "v": 1, "point3D_id": 4, "frame_id": "b"},
            ],
            points3d=points,
            min_points=2,
            cluster_radius=0.5,
        )
        self.assertEqual(support.status, "ambiguous")
        self.assertIsNone(support.object_surface_position)
        self.assertEqual(len(support.candidates), 2)


if __name__ == "__main__":
    unittest.main()
