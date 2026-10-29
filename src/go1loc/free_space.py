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

    def world_to_cell(self, x: float, y: float) -> tuple[int, int]:
        """Convert world/map coordinates to the unique lower-left cell."""
        x, y = float(x), float(y)
        if not all(math.isfinite(value) for value in (x, y)):
            raise ValueError("finite_world_coordinate_required")
        return (math.floor((x - self.origin_x) / self.resolution),
                math.floor((y - self.origin_y) / self.resolution))

    def cell_to_world(self, key: tuple[int, int], *, center: bool = True) -> tuple[float, float]:
        """Convert a cell index to its lower-left corner or center."""
        if not isinstance(key, (tuple, list)) or len(key) != 2:
            raise ValueError("cell_key_required")
        x, y = int(key[0]), int(key[1])
        offset = 0.5 if center else 0.0
        return (self.origin_x + (x + offset) * self.resolution,
                self.origin_y + (y + offset) * self.resolution)

    def cell_bounds(self, key: tuple[int, int]) -> tuple[float, float, float, float]:
        x, y = self.cell_to_world(key, center=False)
        return (x, y, x + self.resolution, y + self.resolution)

    def key(self, x: float, y: float) -> tuple[int, int]:
        return self.world_to_cell(x, y)

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
                       geometry_source: str = "geometry_sidecar",
                       footprint_radius: float | None = None) -> dict:
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
        "diagnostics": free_space_diagnostics(
            grid, units=units, scale_status=scale_status,
            footprint_radius=footprint_radius),
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
        try:
            finite_heights = all(value is None or math.isfinite(float(value))
                                 for value in (ground, obstacle))
        except (TypeError, ValueError):
            finite_heights = False
        if not 0 <= quality <= 1 or not finite_heights:
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
                         origin: tuple[float, float] = (0.0, 0.0),
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
    if not isinstance(origin, (tuple, list)) or len(origin) != 2:
        raise ValueError("origin_xy_required")
    origin_x, origin_y = (float(origin[0]), float(origin[1]))
    if not all(math.isfinite(value) for value in (origin_x, origin_y)):
        raise ValueError("finite_origin_required")
    coordinate_map = FreeSpaceMap(resolution=resolution, cells={},
                                  origin_x=origin_x, origin_y=origin_y)
    raw: dict[tuple[int, int], list[float]] = {}
    for point in points:
        x, y, z = _point(point)
        key = coordinate_map.world_to_cell(x, y)
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
            projected.append(coordinate_map.world_to_cell(float(point[0]), float(point[1])))
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
    return FreeSpaceMap(resolution=resolution, cells=cells,
                        origin_x=origin_x, origin_y=origin_y)


def inflate_obstacles(grid: FreeSpaceMap, *, radius: float) -> FreeSpaceMap:
    """Return a copy with occupied cells inflated by a measured footprint radius.

    The radius is deliberately required by the caller. It must come from the
    robot footprint or a safety policy; the map cannot infer it from sparse
    monocular geometry.
    """
    radius = float(radius)
    if not math.isfinite(radius) or radius < 0:
        raise ValueError("footprint_radius_must_be_nonnegative")
    radius_cells = int(math.ceil(radius / grid.resolution))
    cells = {key: GridCell(cell.x, cell.y, cell.state, cell.ground_height,
                           cell.obstacle_height, cell.geometry_quality,
                           set(cell.evidence)) for key, cell in grid.cells.items()}
    occupied = [key for key, cell in grid.cells.items()
                if cell.state is CellState.OCCUPIED]
    for ox, oy in occupied:
        for dx in range(-radius_cells, radius_cells + 1):
            for dy in range(-radius_cells, radius_cells + 1):
                if math.hypot(dx, dy) * grid.resolution > radius:
                    continue
                key = (ox + dx, oy + dy)
                cell = cells.setdefault(key, GridCell(*key))
                if cell.state is not CellState.OCCUPIED:
                    cell.state = CellState.OCCUPIED
                    cell.evidence.add("inflated_obstacle")
    return FreeSpaceMap(grid.resolution, cells, grid.origin_x, grid.origin_y,
                        grid.schema_version)


def free_space_diagnostics(grid: FreeSpaceMap, *, units: str,
                           scale_status: str, footprint_radius: float | None = None) -> dict:
    """Summarize whether a sidecar is suitable for preview or execution."""
    if not isinstance(grid, FreeSpaceMap):
        raise TypeError("free_space_map_required")
    counts = {state.value: 0 for state in CellState}
    for cell in grid.cells.values():
        counts[cell.state.value] += 1
    total = max(1, sum(counts.values()))
    free_keys = {key for key, cell in grid.cells.items() if cell.state is CellState.FREE}
    components = 0
    while free_keys:
        components += 1
        pending = [free_keys.pop()]
        while pending:
            x, y = pending.pop()
            for neighbour in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if neighbour in free_keys:
                    free_keys.remove(neighbour)
                    pending.append(neighbour)
    hole_components = 0
    hole_cells = 0
    if grid.cells:
        xs = [key[0] for key in grid.cells]
        ys = [key[1] for key in grid.cells]
        low_x, high_x, low_y, high_y = min(xs), max(xs), min(ys), max(ys)
        unknown = {(x, y) for x in range(low_x, high_x + 1)
                   for y in range(low_y, high_y + 1)
                   if grid.cells.get((x, y), GridCell(x, y)).state is CellState.UNKNOWN}
        while unknown:
            start = unknown.pop()
            pending = [start]
            component = {start}
            touches_boundary = start[0] in (low_x, high_x) or start[1] in (low_y, high_y)
            while pending:
                x, y = pending.pop()
                for neighbour in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if neighbour in unknown:
                        unknown.remove(neighbour)
                        component.add(neighbour)
                        pending.append(neighbour)
                        touches_boundary |= (neighbour[0] in (low_x, high_x)
                                             or neighbour[1] in (low_y, high_y))
            if not touches_boundary:
                hole_components += 1
                hole_cells += len(component)
    metric_ready = units == "m" and scale_status in {"metric", "verified_metric"}
    return {
        "resolution": grid.resolution,
        "origin": [grid.origin_x, grid.origin_y],
        "cell_counts": counts,
        "cell_ratios": {key: value / total for key, value in counts.items()},
        "free_components_4_connected": components,
        "unknown_hole_components": hole_components,
        "unknown_hole_cells": hole_cells,
        "units": units,
        "scale_status": scale_status,
        "footprint_radius": footprint_radius,
        "preview_only": not metric_ready,
        "execution_geometry_ready": metric_ready and counts[CellState.UNKNOWN.value] == 0
        and counts[CellState.UNCERTAIN.value] == 0,
        "warnings": (["metric_scale_required"] if not metric_ready else [])
        + (["unknown_or_uncertain_cells_present"]
           if counts[CellState.UNKNOWN.value] or counts[CellState.UNCERTAIN.value] else []),
    }
