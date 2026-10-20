"""Map a user-selected image ROI to observed 3D support without inventing a center."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class ObjectSupport:
    status: str
    supporting_point3D_ids: list[int] = field(default_factory=list)
    # Keep the actual map coordinates beside the IDs so an exported target
    # result is self-contained for inspection and downstream route preview.
    # JSON object keys are strings even when the native point IDs are ints.
    supporting_points3D: dict[str, list[float]] = field(default_factory=dict)
    supporting_frame_ids: list[str] = field(default_factory=list)
    object_surface_position: list[float] | None = None
    object_extent: dict[str, list[float]] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    failure_reason: str | None = None


def _valid_roi(roi: tuple[float, float, float, float]) -> bool:
    x0, y0, x1, y1 = roi
    return all(np.isfinite(v) for v in roi) and x1 > x0 and y1 > y0


def _clusters(points: np.ndarray, radius: float) -> list[list[int]]:
    groups: list[list[int]] = []
    unseen = set(range(len(points)))
    while unseen:
        start = unseen.pop()
        group = [start]
        queue = [start]
        while queue:
            i = queue.pop()
            near = [j for j in unseen if np.linalg.norm(points[i] - points[j]) <= radius]
            for j in near:
                unseen.remove(j)
                queue.append(j)
                group.append(j)
        groups.append(group)
    return groups


def _candidate(ids: list[int], frame_ids: list[str], points: np.ndarray) -> dict[str, Any]:
    low = points.min(axis=0)
    high = points.max(axis=0)
    return {
        "supporting_point3D_ids": ids,
        "supporting_points3D": {
            str(point_id): points[index].tolist()
            for index, point_id in enumerate(ids)
        },
        "supporting_frame_ids": sorted(set(frame_ids)),
        "surface_position": points.mean(axis=0).tolist(),
        "extent": {"min": low.tolist(), "max": high.tolist()},
    }


def support_from_roi_matches(
    *,
    roi: tuple[float, float, float, float],
    observations: list[dict[str, Any]],
    points3d: dict[int, np.ndarray],
    min_points: int = 3,
    cluster_radius: float = 1.5,
) -> ObjectSupport:
    if not _valid_roi(roi):
        return ObjectSupport("invalid_input", failure_reason="invalid_roi")
    if min_points < 1 or not np.isfinite(cluster_radius) or cluster_radius <= 0:
        return ObjectSupport("invalid_input", failure_reason="invalid_clustering_parameters")
    x0, y0, x1, y1 = roi
    unique: dict[int, dict[str, Any]] = {}
    for observation in observations:
        try:
            u, v = float(observation["u"]), float(observation["v"])
            point_id = int(observation["point3D_id"])
            frame_id = str(observation["frame_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if x0 <= u <= x1 and y0 <= v <= y1 and point_id in points3d:
            point = np.asarray(points3d[point_id], dtype=float)
            if point.shape != (3,) or not np.isfinite(point).all():
                continue
            unique.setdefault(point_id, {"frame_ids": set()})["frame_ids"].add(frame_id)
    if len(unique) < min_points:
        return ObjectSupport("insufficient_evidence", failure_reason="insufficient_roi_3d_support")

    ids = list(unique)
    xyz = np.asarray([points3d[i] for i in ids], dtype=float)
    groups = [g for g in _clusters(xyz, cluster_radius) if len(g) >= min_points]
    candidates = [
        _candidate([ids[i] for i in group], [f for i in group for f in unique[ids[i]]["frame_ids"]], xyz[group])
        for group in groups
    ]
    if not candidates:
        return ObjectSupport("insufficient_evidence", failure_reason="insufficient_cluster_support")
    if len(candidates) > 1:
        return ObjectSupport("ambiguous", candidates=candidates, failure_reason="multiple_spatial_support_clusters")

    selected = candidates[0]
    return ObjectSupport(
        status="supported",
        supporting_point3D_ids=selected["supporting_point3D_ids"],
        supporting_points3D=selected["supporting_points3D"],
        supporting_frame_ids=selected["supporting_frame_ids"],
        object_surface_position=selected["surface_position"],
        object_extent=selected["extent"],
    )
