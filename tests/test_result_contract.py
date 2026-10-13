import unittest

from go1loc.schemas import LocalizationStatus, LocalizationResult


class ResultContractTests(unittest.TestCase):
    def test_unlocalized_result_never_contains_nearest_pose(self):
        result = LocalizationResult.failure(
            query_id="q1", map_id="m1", reason="insufficient_2d3d"
        )
        self.assertEqual(result.pose_status, LocalizationStatus.INSUFFICIENT_EVIDENCE)
        self.assertIsNone(result.T_WC_query)
        self.assertIsNone(result.object_surface_position)
        self.assertIsNone(result.nav_goal)
        self.assertEqual(result.failure_reason, "insufficient_2d3d")

    def test_readonly_map_rejects_mutation(self):
        result = LocalizationResult.failure(
            query_id="q1", map_id="m1", reason="map_readonly"
        )
        self.assertEqual(result.pose_status, LocalizationStatus.PIPELINE_ERROR)


if __name__ == "__main__":
    unittest.main()
