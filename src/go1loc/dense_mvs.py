"""Plan and optionally run a COLMAP dense MVS reconstruction.

The default is a read-only capability check and command plan. Missing COLMAP,
missing calibrated inputs, or a failed subprocess produces a blocked report;
the module never substitutes sparse points and calls them a dense result.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
from typing import Sequence


@dataclass(frozen=True)
class DenseMvsReport:
    status: str
    reason: str
    colmap: str | None
    commands: list[list[str]]
    output_dir: str
    execution_permitted: bool
    completed_steps: list[str]
    stderr_tail: str | None = None

    def as_dict(self) -> dict:
        return {
            "schema_version": "go1loc.dense_mvs.v1",
            "status": self.status,
            "reason": self.reason,
            "colmap": self.colmap,
            "commands": self.commands,
            "output_dir": self.output_dir,
            "execution_permitted": self.execution_permitted,
            "completed_steps": self.completed_steps,
            "stderr_tail": self.stderr_tail,
            "sparse_is_not_dense": True,
        }


def _commands(colmap: str, image_dir: Path, sparse_dir: Path, output_dir: Path) -> list[list[str]]:
    return [
        [colmap, "image_undistorter", "--image_path", str(image_dir),
         "--input_path", str(sparse_dir), "--output_path", str(output_dir),
         "--output_type", "COLMAP"],
        [colmap, "patch_match_stereo", "--workspace_path", str(output_dir),
         "--workspace_format", "COLMAP", "--PatchMatchStereo.geom_consistency", "true"],
        [colmap, "stereo_fusion", "--workspace_path", str(output_dir),
         "--workspace_format", "COLMAP", "--input_type", "geometric",
         "--output_path", str(output_dir / "fused.ply")],
    ]


def run_dense_mvs(*, image_dir: str | Path, sparse_dir: str | Path,
                  output_dir: str | Path, execute: bool = False,
                  colmap_binary: str | None = None) -> dict:
    image_dir = Path(image_dir)
    sparse_dir = Path(sparse_dir)
    output_dir = Path(output_dir)
    colmap = colmap_binary or shutil.which("colmap")
    commands = _commands(colmap or "colmap", image_dir, sparse_dir, output_dir)
    if not image_dir.is_dir():
        return DenseMvsReport("blocked", "image_dir_missing", colmap, commands,
                              str(output_dir), execute, []).as_dict()
    if not sparse_dir.is_dir():
        return DenseMvsReport("blocked", "sparse_model_missing", colmap, commands,
                              str(output_dir), execute, []).as_dict()
    required = {"cameras.bin", "images.bin", "points3D.bin"}
    if not required.issubset({p.name for p in sparse_dir.iterdir()}):
        return DenseMvsReport("blocked", "sparse_model_incomplete", colmap, commands,
                              str(output_dir), execute, []).as_dict()
    if not colmap:
        return DenseMvsReport("blocked", "colmap_not_installed", None, commands,
                              str(output_dir), execute, []).as_dict()
    if not execute:
        return DenseMvsReport("ready", "colmap_available_plan_only", colmap, commands,
                              str(output_dir), False, []).as_dict()
    output_dir.mkdir(parents=True, exist_ok=True)
    completed = []
    stderr_tail = None
    for label, command in zip(("image_undistorter", "patch_match_stereo", "stereo_fusion"), commands):
        try:
            result = subprocess.run(command, check=False, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace")
        except OSError as exc:
            return DenseMvsReport("blocked", f"{label}_launch_failed", colmap, commands,
                                  str(output_dir), True, completed, str(exc)[-2000:]).as_dict()
        if result.returncode != 0:
            return DenseMvsReport("blocked", f"{label}_failed_exit_{result.returncode}", colmap,
                                  commands, str(output_dir), True, completed,
                                  (result.stderr or result.stdout)[-2000:]).as_dict()
        completed.append(label)
    fused = output_dir / "fused.ply"
    if not fused.is_file() or fused.stat().st_size == 0:
        return DenseMvsReport("blocked", "dense_output_missing", colmap, commands,
                              str(output_dir), True, completed).as_dict()
    return DenseMvsReport("completed", "dense_mvs_ready", colmap, commands,
                          str(output_dir), True, completed).as_dict()


def write_dense_mvs_report(path: str | Path, **kwargs) -> dict:
    report = run_dense_mvs(**kwargs)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
