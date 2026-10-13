"""Explicit capture boundary. Offline video is default; no robot auto-connect."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CaptureConfig:
    mode: str = "offline_video"
    input_path: Path | None = None
    node_key: int | None = None
    duration_s: float = 0.0
    output_dir: Path | None = None
    bounded_queue: int = 8
    own_resources_only: bool = True


class CaptureAdapter:
    def capture(self):
        raise NotImplementedError


class OfflineVideoAdapter(CaptureAdapter):
    def __init__(self, config: CaptureConfig):
        if config.input_path is None:
            raise ValueError("offline_video_requires_input_path")
        self.config = config

    def capture(self):
        return self.config.input_path


class Go1ReadonlyShmAdapter(CaptureAdapter):
    """Placeholder boundary kept intentionally disabled until separately authorized."""

    def __init__(self, config: CaptureConfig):
        if config.mode != "go1_readonly_shm":
            raise ValueError("invalid_capture_mode")
        raise RuntimeError("robot_capture_disabled_in_offline_project")
