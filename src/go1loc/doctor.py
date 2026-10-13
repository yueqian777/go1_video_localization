"""Non-mutating environment inspection."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .dependencies import inspect_dependencies, git_revision


def _command_version(command: str, args: list[str]) -> str | None:
    exe = shutil.which(command)
    if not exe:
        return None
    try:
        result = subprocess.run([exe, *args], capture_output=True, text=True, timeout=10, check=False)
        return (result.stdout or result.stderr).strip().splitlines()[0][:300]
    except (OSError, subprocess.TimeoutExpired):
        return "unreadable"


def _torch_runtime() -> dict[str, Any]:
    try:
        import torch
    except ImportError:
        return {"available": False}
    result: dict[str, Any] = {
        "available": True,
        "version": torch.__version__,
        "torch_cuda_build": torch.version.cuda,
        "cuda_available": bool(torch.cuda.is_available()),
    }
    if result["cuda_available"]:
        result["device_name"] = torch.cuda.get_device_name(0)
        result["device_count"] = torch.cuda.device_count()
    return result


def collect_doctor(project_root: Path) -> dict[str, Any]:
    root = project_root.resolve()
    upstream = root / ".upstream"
    return {
        "platform": platform.platform(),
        "python": sys.version,
        "python_executable": sys.executable,
        "cwd": str(Path.cwd()),
        "project_root": str(root),
        "cpu_count": os.cpu_count(),
        "commands": {
            "git": _command_version("git", ["--version"]),
            "ffmpeg": _command_version("ffmpeg", ["-version"]),
            "ffprobe": _command_version("ffprobe", ["-version"]),
            "colmap": _command_version("colmap", ["--help"]),
            "nvidia_smi": _command_version("nvidia-smi", ["--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"]),
        },
        "python_dependencies": [status.__dict__ for status in inspect_dependencies()],
        "torch_runtime": _torch_runtime(),
        "upstream_revisions": {
            "hloc": git_revision(upstream / "Hierarchical-Localization"),
            "lightglue": git_revision(upstream / "LightGlue"),
        },
        "robot_access": "disabled_by_project_scope",
    }


def write_doctor(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
