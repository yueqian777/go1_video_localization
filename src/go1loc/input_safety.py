"""Bounded local-file validation for CLI inputs and the local viewer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass(frozen=True)
class Validation:
    ok: bool
    reason: str | None = None
    path: Path | None = None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_image_path(path: Path, root: Path, max_bytes: int = 250 * 1024 * 1024) -> Validation:
    root = root.resolve()
    candidate = path.resolve()
    if not _inside(candidate, root):
        return Validation(False, "path_outside_allowed_root")
    if not candidate.is_file():
        return Validation(False, "file_not_found")
    if candidate.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
        return Validation(False, "unsupported_image_extension")
    if candidate.stat().st_size > max_bytes:
        return Validation(False, "file_too_large")
    return Validation(True, path=candidate)


def validate_upload(path: Path, root: Path, max_bytes: int = 250 * 1024 * 1024) -> Validation:
    return validate_image_path(path, root, max_bytes=max_bytes)
