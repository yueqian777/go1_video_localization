"""Stable, explicit result contracts. Unknown geometry is represented by null."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class LocalizationStatus(str, Enum):
    LOCALIZED = "localized"
    AMBIGUOUS = "ambiguous"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    INVALID_INPUT = "invalid_input"
    PIPELINE_ERROR = "pipeline_error"


class ObjectStatus(str, Enum):
    SUPPORTED = "supported"
    AMBIGUOUS = "ambiguous"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    INVALID_INPUT = "invalid_input"


@dataclass
class LocalizationResult:
    query_id: str
    map_id: str
    component_id: str | None
    mode: str
    pose_status: str
    object_status: str
    T_WC_query: list[list[float]] | None = None
    object_surface_position: list[float] | None = None
    object_extent: dict[str, Any] | None = None
    supporting_frame_ids: list[str] = field(default_factory=list)
    supporting_point3D_ids: list[int] = field(default_factory=list)
    quality: dict[str, Any] = field(default_factory=dict)
    units: str = "map_units"
    scale_status: str = "arbitrary_monocular_scale"
    map_observation_time: str | None = None
    failure_reason: str | None = None
    timing: dict[str, float] = field(default_factory=dict)
    nav_goal: None = None

    @classmethod
    def failure(cls, *, query_id: str, map_id: str, reason: str, mode: str = "photo"):
        if reason.startswith("invalid"):
            status = LocalizationStatus.INVALID_INPUT.value
        elif reason.startswith("insufficient"):
            status = LocalizationStatus.INSUFFICIENT_EVIDENCE.value
        elif reason.startswith("ambiguous"):
            status = LocalizationStatus.AMBIGUOUS.value
        else:
            status = LocalizationStatus.PIPELINE_ERROR.value
        return cls(
            query_id=query_id,
            map_id=map_id,
            component_id=None,
            mode=mode,
            pose_status=status,
            object_status=ObjectStatus.INSUFFICIENT_EVIDENCE.value,
            failure_reason=reason,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
