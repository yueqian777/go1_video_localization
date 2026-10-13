"""Dependency and upstream-version checks; no implicit installation."""

from __future__ import annotations

import importlib.util
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DependencyStatus:
    name: str
    available: bool
    detail: str


def _module(name: str) -> DependencyStatus:
    spec = importlib.util.find_spec(name)
    return DependencyStatus(name, spec is not None, "available" if spec else "missing")


def inspect_dependencies() -> list[DependencyStatus]:
    return [_module(name) for name in ("numpy", "cv2", "torch", "pycolmap", "hloc", "lightglue", "h5py")]


def git_revision(path: Path) -> str | None:
    if not (path / ".git").exists():
        return None
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None
