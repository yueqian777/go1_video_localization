"""Evidence-bound integration from the existing target workbench to grid preview."""
from __future__ import annotations

from copy import deepcopy
import math

from .free_space import FreeSpaceMap, free_space_payload
from .grid_planner import GridRoute, plan_grid


def _translation(pose):
    if not isinstance(pose, (list, tuple)) or len(pose) < 3:
        raise ValueError("camera_pose_invalid")
    if len(pose) == 4 and len(pose[0]) == 4:
        x, y, z = float(pose[0][3]), float(pose[1][3]), float(pose[2][3])
    elif len(pose) >= 3 and all(isinstance(row, (list, tuple)) for row in pose[:3]):
        x, y, z = float(pose[0][3]), float(pose[1][3]), float(pose[2][3])
    else:
        x, y, z = float(pose[0]), float(pose[1]), float(pose[2])
    if not all(math.isfinite(value) for value in (x, y, z)):
        raise ValueError("camera_pose_not_finite")
    return (x, y, z)


def choose_goal_observation(model: dict, target_result: dict) -> dict:
    if target_result.get("status") != "supported":
        raise ValueError("target_support_required")
    support = set(target_result.get("supporting_frame_ids") or ())
    if not support or target_result.get("object_surface_position") is None:
        raise ValueError("target_observation_support_required")
    surface = _translation(target_result["object_surface_position"])
    candidates = [camera for camera in model.get("cameras", []) if camera.get("name") in support]
    if not candidates:
        raise ValueError("target_support_cameras_missing")
    camera = min(candidates, key=lambda row: math.dist(_translation(row["T_WC"]), surface))
    return {"camera": camera, "world_position": list(_translation(camera["T_WC"])),
            "selection_reason": "nearest_supported_observation_camera"}


def plan_free_space_from_workbench(*, grid: FreeSpaceMap, model: dict,
                                   target_result: dict, start_pose, start_source: str,
                                   start_heading: str = "E") -> dict:
    """Plan from a pose and supported target without exposing cell inputs to UI."""
    start_world = _translation(start_pose)
    goal = choose_goal_observation(model, target_result)
    goal_world = tuple(goal["world_position"])
    start_cell = grid.world_to_cell(start_world[0], start_world[1])
    goal_cell = grid.world_to_cell(goal_world[0], goal_world[1])
    route = plan_grid(grid, start_cell, goal_cell, start_heading=start_heading)
    return {
        "schema_version": "go1loc.free_space_route_preview.v1",
        "map_id": model.get("map_id"),
        "map_hash": model.get("map_hash"),
        "component_id": model.get("component_id"),
        "status": route.status,
        "execution_permitted": False,
        "source": "free_space_grid",
        "start_source": start_source,
        "start_world": list(start_world),
        "start_cell": list(start_cell),
        "goal_world": list(goal_world),
        "goal_cell": list(goal_cell),
        "goal_observation_camera": goal["camera"].get("name"),
        "goal_selection_reason": goal["selection_reason"],
        "path": [list(row) for row in route.path],
        "actions": [{"primitive": action.primitive,
                     "cell": list(action.cell) if action.cell is not None else None,
                     "target_heading": action.target_heading}
                    for action in route.actions],
        "reason": route.reason,
        "requires_exploration": route.requires_exploration,
        "warnings": ["preview_only", "goal_is_observation_camera_not_object_center"],
    }
