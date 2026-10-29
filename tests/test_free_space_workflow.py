import unittest

from go1loc.free_space import build_free_space_map
from go1loc.free_space_workflow import plan_free_space_from_workbench


class FreeSpaceWorkflowTests(unittest.TestCase):
    def test_workbench_selects_goal_camera_without_manual_cell_coordinates(self):
        grid = build_free_space_map(
            [[float(x), 0.0, 0.0] for x in range(4)],
            resolution=1.0, ground_height=0.0,
        )
        model = {
            "map_id": "m", "map_hash": "h", "component_id": "c",
            "cameras": [
                {"name": "start.png", "T_WC": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]},
                {"name": "goal.png", "T_WC": [[1, 0, 0, 3], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]},
            ],
        }
        target = {"status": "supported", "supporting_frame_ids": ["goal.png"],
                  "object_surface_position": [3.0, 0.0, 0.0]}
        result = plan_free_space_from_workbench(
            grid=grid, model=model, target_result=target,
            start_pose=model["cameras"][0]["T_WC"], start_source="selected_reference_fallback")
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["start_cell"], [0, 0])
        self.assertEqual(result["goal_cell"], [3, 0])
        self.assertFalse(result["execution_permitted"])
        self.assertEqual(result["goal_observation_camera"], "goal.png")


if __name__ == "__main__":
    unittest.main()
