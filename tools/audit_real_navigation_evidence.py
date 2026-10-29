"""Audit an existing Go1 map against free-space, dense-MVS and target evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from go1loc.dense_mvs import run_dense_mvs  # noqa: E402
from go1loc.free_space import free_space_from_payload  # noqa: E402


def audit(scene_path: Path, free_space_path: Path, image_dir: Path,
          sparse_dir: Path, dense_output: Path, object_root: Path) -> dict:
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    sidecar = json.loads(free_space_path.read_text(encoding="utf-8"))
    grid = free_space_from_payload(sidecar)
    matching = []
    scanned = 0
    for path in object_root.rglob("object_result.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        scanned += 1
        if (payload.get("map_id"), payload.get("map_hash")) == (scene.get("map_id"), scene.get("map_hash")):
            matching.append(str(path.resolve()))
    dense = run_dense_mvs(image_dir=image_dir, sparse_dir=sparse_dir,
                          output_dir=dense_output, execute=False)
    diagnostics = sidecar.get("diagnostics", {})
    return {
        "schema_version": "go1loc.real_navigation_evidence_audit.v1",
        "scene": str(scene_path.resolve()),
        "map_identity": {key: scene.get(key) for key in ("map_id", "map_hash", "component_id", "units", "scale_status")},
        "free_space": {
            "sidecar": str(free_space_path.resolve()),
            "cells": len(grid.cells),
            "diagnostics": diagnostics,
            "execution_permitted": sidecar.get("execution_permitted"),
        },
        "dense_mvs": dense,
        "target_evidence": {
            "object_results_scanned": scanned,
            "matching_map_object_results": matching,
            "status": "ready" if matching else "blocked_target_map_binding_missing",
        },
        "conclusion": ("offline_preview_only_target_binding_missing"
                       if not matching else "offline_preview_only"),
        "robot_motion_commands_sent": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--free-space", type=Path, required=True)
    parser.add_argument("--image-dir", type=Path, required=True)
    parser.add_argument("--sparse-dir", type=Path, required=True)
    parser.add_argument("--dense-output", type=Path, required=True)
    parser.add_argument("--object-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = audit(args.scene, args.free_space, args.image_dir, args.sparse_dir,
                   args.dense_output, args.object_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "conclusion": report["conclusion"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
