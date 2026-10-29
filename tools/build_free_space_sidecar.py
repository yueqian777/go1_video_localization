"""Build an evidence-bound free-space sidecar from an existing scene JSON.

This tool consumes the scene payload already produced by ``inspect_map``. It
does not run COLMAP, invent missing geometry, or connect to a robot. Without a
verified ground height every observed cell remains uncertain by design.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from go1loc.free_space import build_free_space_map, free_space_payload  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scene", type=Path, help="inspect_map scene.json")
    parser.add_argument("output", type=Path, help="free_space.json output")
    parser.add_argument("--resolution", type=float, default=0.10,
                        help="grid resolution in the scene units (default: 0.10)")
    parser.add_argument("--ground-height", type=float, default=None,
                        help="verified ground height; omit to keep geometry uncertain")
    parser.add_argument("--ground-tolerance", type=float, default=0.08)
    parser.add_argument("--obstacle-height", type=float, default=0.30)
    parser.add_argument("--robot-radius", type=float, default=0.25)
    args = parser.parse_args(argv)

    scene_path = args.scene.resolve()
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    required = ("map_id", "map_hash", "component_id", "units", "scale_status", "points")
    missing = [key for key in required if key not in scene]
    if missing:
        parser.error("scene identity/geometry fields missing: " + ", ".join(missing))
    grid = build_free_space_map(
        scene["points"], resolution=args.resolution,
        ground_height=args.ground_height,
        ground_tolerance=args.ground_tolerance,
        obstacle_height=args.obstacle_height,
        robot_radius=args.robot_radius,
    )
    payload = free_space_payload(
        grid, map_id=scene["map_id"], map_hash=scene["map_hash"],
        component_id=scene["component_id"], units=scene["units"],
        scale_status=scene["scale_status"],
        geometry_source=f"scene_points:{scene_path.name}",
    )
    payload["source_scene"] = str(scene_path)
    payload["source_scene_schema"] = scene.get("schema_version")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    counts = payload["cell_counts"]
    print(json.dumps({"output": str(args.output.resolve()), "cells": len(payload["cells"]),
                      "cell_counts": counts, "execution_permitted": False},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
