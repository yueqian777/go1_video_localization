"""Evidence-bound 2.5D free-space maps for offline navigation previews.

The map is deliberately conservative: an unobserved cell is unknown, and
historical traversal only adds a thin corridor of supporting evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Iterable


class CellState(str, Enum):
    FREE = "free"
    OCCUPIED = "occupied"
    UNKNOWN = "unknown"
    UNCERTAIN = "uncertain"


@dataclass
class GridCell:
    x: int
    y: int
    state: CellState = CellState.UNKNOWN
    ground_height: float | None = None
    obstacle_height: float | None = None
    geometry_quality: float = 0.0
    evidence: set[str] = field(default_factory=set)


@dataclass
class FreeSpaceMap:
    resolution: float
    cells: dict[tuple[int, int], GridCell]
    origin_x: float = 0.0
    origin_y: float = 0.0
    schema_version: str = "go1loc.free_space_map.v1"

    def key(self, x: float, y: float) -> tuple[int, int]:
        return (math.floor((x - self.origin_x) / self.resolution),
                math.floor((y - self.origin_y) / self.resolution))

    def cell(self, x: int, y: int) -> GridCell:
        return self.cells.get((int(x), int(y)), GridCell(int(x), int(y)))

    def is_traversable(self, key: tuple[int, int]) -> bool:
        return self.cells.get(key, GridCell(*key)).state is CellState.FREE


def _cell_payload(cell: GridCell) -> dict:
    return {
        "x": int(cell.x),
        "y": int(cell.y),
        "state": cell.state.value,
        "ground_height": cell.ground_height,
        "obstacle_height": cell.obstacle_height,
        "geometry_quality": float(cell.geometry_quality),
        "evidence": sorted(str(value) for value in cell.evidence),
    }


def free_space_payload(grid: FreeSpaceMap, *, map_id: str, map_hash: str,
                       component_id: str, units: str,
                       scale_status: str,
                       geometry_source: str = "geometry_sidecar") -> dict:
    """Serialize a map sidecar with an explicit non-execution contract.

    The identity fields bind the sidecar to the frozen map that produced it.
    ``execution_permitted`` is always false here: a metric map and calibration
    gates are separate requirements handled by the closed-loop controller.
    """
    if not all(isinstance(value, str) and value for value in
               (map_id, map_hash, component_id, units, scale_status)):
        raise ValueError("free_space_identity_required")
    if not isinstance(grid, FreeSpaceMap):
        raise TypeError("free_space_map_required")
    cells = [_cell_payload(grid.cells[key]) for key in sorted(grid.cells)]
    counts = {state.value: 0 for state in CellState}
    for cell in grid.cells.values():
        counts[cell.state.value] += 1
    return {
        "schema_version": "go1loc.free_space_map.v1",
        "map_id": map_id,
        "map_hash": map_hash,
        "component_id": component_id,
        "resolution": float(grid.resolution),
        "origin": [float(grid.origin_x), float(grid.origin_y)],
        "units": units,
        "scale_status": scale_status,
        "geometry_source": geometry_source,
        "cells": cells,
        "cell_counts": counts,
        "execution_permitted": False,
        "warnings": [
            "missing_geometry_is_unknown",
            "historical_corridor_is_narrow_evidence_only",
            "map_requires_metric_scale_and_calibration_for_execution",
        ],
    }


def free_space_from_payload(payload: dict) -> FreeSpaceMap:
    """Load and validate a serialized sidecar without granting execution."""
    if not isinstance(payload, dict) or payload.get("schema_version") != "go1loc.free_space_map.v1":
        raise ValueError("free_space_schema_mismatch")
    if payload.get("execution_permitted") is not False:
        raise ValueError("free_space_execution_contract_invalid")
    try:
        resolution = float(payload["resolution"])
        origin = payload["origin"]
        origin_x, origin_y = (float(origin[0]), float(origin[1]))
    except (KeyError, TypeError, ValueError, IndexError):
        raise ValueError("free_space_geometry_header_invalid") from None
    if resolution <= 0 or not all(math.isfinite(v) for v in (resolution, origin_x, origin_y)):
        raise ValueError("free_space_geometry_header_invalid")
    cells = {}
    for row in payload.get("cells", []):
        if not isinstance(row, dict):
            raise ValueError("free_space_cell_invalid")
        try:
            x, y = int(row["x"]), int(row["y"])
            state = CellState(row["state"])
            ground = row.get("ground_height")
            obstacle = row.get("obstacle_height")
            quality = float(row.get("geometry_quality", 0.0))
            evidence = {str(value) for value in row.get("evidence", [])}
        except (KeyError, TypeError, ValueError):
            raise ValueError("free_space_cell_invalid") from None
        if not 0 <= quality <= 1 or any(value is not None and not math.isfinite(float(value))
                                        for value in (ground, obstacle)):
            raise ValueError("free_space_cell_invalid")
        cells[(x, y)] = GridCell(x, y, state=state,
                                 ground_height=None if ground is None else float(ground),
                                 obstacle_height=None if obstacle is None else float(obstacle),
                                 geometry_quality=quality, evidence=evidence)
    return FreeSpaceMap(resolution=resolution, cells=cells,
                        origin_x=origin_x, origin_y=origin_y)


def _point(value):
    if len(value) != 3:
        raise ValueError("point_must_be_xyz")
    x, y, z = (float(v) for v in value)
    if not all(math.isfinite(v) for v in (x, y, z)):
        raise ValueError("finite_point_required")
    return x, y, z


def _line_cells(a, b):
    x0, y0 = a
    x1, y1 = b
    length = max(abs(x1 - x0), abs(y1 - y0), 1)
    for i in range(int(length) + 1):
        t = i / length
        yield (round(x0 + (x1 - x0) * t), round(y0 + (y1 - y0) * t))


def build_free_space_map(points: Iterable[Iterable[float]], *, resolution: float = 0.1,
                         ground_height: float | None = None,
                         ground_tolerance: float = 0.08,
                         obstacle_height: float = 0.30,
                         robot_radius: float = 0.25,
                         historical_corridor=()) -> FreeSpaceMap:
    """Voxelize observed geometry into a conservative 2.5D map.

    ``points`` are metric XYZ samples. If scale is unknown, callers must keep
    the result in preview mode and must not convert cells into m/s commands.
    """
    resolution = float(resolution)
    if not math.isfinite(resolution) or resolution <= 0:
        raise ValueError("resolution_must_be_positive")
    ground_tolerance = float(ground_tolerance)
    obstacle_height = float(obstacle_height)
    if ground_tolerance < 0 or obstacle_height <= ground_tolerance:
        raise ValueError("invalid_height_thresholds")
    raw: dict[tuple[int, int], list[float]] = {}
    for point in points:
        x, y, z = _point(point)
        key = (math.floor(x / resolution), math.floor(y / resolution))
        raw.setdefault(key, []).append(z)
    cells = {}
    for key, heights in raw.items():
        low, high = min(heights), max(heights)
        cell = GridCell(*key, ground_height=low, obstacle_height=high,
                        geometry_quality=min(1.0, len(heights) / 3.0), evidence={"geometry"})
        if ground_height is None:
            cell.state = CellState.UNCERTAIN
        else:
            near_ground = any(abs(z - ground_height) <= ground_tolerance for z in heights)
            obstacle = any(z - ground_height >= obstacle_height for z in heights)
            if near_ground and obstacle:
                cell.state = CellState.UNCERTAIN
            elif obstacle:
                cell.state = CellState.OCCUPIED
            elif near_ground:
                cell.state = CellState.FREE
            else:
                cell.state = CellState.UNCERTAIN
        cells[key] = cell

    # A historical route is evidence only along its narrow measured centreline.
    # It cannot fill a broad region or override occupied/uncertain geometry.
    corridor_radius = max(0, int(math.floor(float(robot_radius) / resolution + 0.5)))
    for segment in historical_corridor or ():
        if len(segment) < 2:
            continue
        projected = []
        for point in segment:
            x, y = float(point[0]) / resolution, float(point[1]) / resolution
            projected.append((round(x), round(y)))
        for a, b in zip(projected, projected[1:]):
            for key in _line_cells(a, b):
                for dx in range(-corridor_radius, corridor_radius + 1):
                    for dy in range(-corridor_radius, corridor_radius + 1):
                        if dx * dx + dy * dy > corridor_radius * corridor_radius:
                            continue
                        cell = cells.setdefault((key[0] + dx, key[1] + dy), GridCell(key[0] + dx, key[1] + dy))
                        if cell.state is CellState.UNKNOWN:
                            cell.state = CellState.FREE
                            cell.geometry_quality = 0.25
                            cell.evidence.add("historical_corridor")
                        elif cell.state is CellState.FREE:
                            cell.evidence.add("historical_corridor")
    return FreeSpaceMap(resolution=resolution, cells=cells)
