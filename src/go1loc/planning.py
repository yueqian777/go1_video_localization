"""Pure, offline historical camera-route previews. No I/O or robot commands.

T_AB maps coordinates in B to A. Map frame M is the legacy world frame W.
Only caller-verified temporal evidence creates directed graph edges. Poses,
co-visibility and Euclidean proximity never establish traversability.
See docs/PLANNING_CONTRACT.md for the JSON-compatible input contract.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import heapq
import json
import math
from typing import Any

import numpy as np


IDENTITY = ("map_id", "map_hash", "component_id")
SOURCE_KINDS = {"video", "synthetic_test"}
POSE_ATOL = 1e-7  # Numerical consistency, NOT a physical snapping radius.


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _rigid(value: Any) -> np.ndarray:
    try:
        matrix = np.asarray(value, dtype=float)
        if matrix.shape == (3, 4):
            matrix = np.vstack((matrix, [0, 0, 0, 1]))
        if (matrix.shape != (4, 4) or not np.isfinite(matrix).all()
                or not np.allclose(matrix[3], [0, 0, 0, 1], atol=POSE_ATOL, rtol=0)
                or not np.allclose(matrix[:3, :3].T @ matrix[:3, :3], np.eye(3),
                                   atol=POSE_ATOL, rtol=0)
                or not math.isclose(float(np.linalg.det(matrix[:3, :3])), 1,
                                    abs_tol=POSE_ATOL, rel_tol=0)):
            raise ValueError("invalid_rigid_transform")
        return matrix
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_rigid_transform") from exc


def _inverse(matrix: np.ndarray) -> np.ndarray:
    inverse = np.eye(4)
    inverse[:3, :3] = matrix[:3, :3].T
    inverse[:3, 3] = -inverse[:3, :3] @ matrix[:3, 3]
    return inverse


def camera_to_base_pose(T_MC: Any, T_BC: Any, *, scale_status: str,
                        units: str) -> list[list[float]]:
    """Return T_MB = T_MC @ inverse(T_BC); reject non-metric map translations.

    T_BC is an already calibrated, metre-valued rigid transform. This function
    does not estimate scale, rescale the map, or establish a body route.
    """
    if scale_status != "metric" or units != "m":
        raise ValueError("scale_mixing_not_permitted")
    return (_rigid(T_MC) @ _inverse(_rigid(T_BC))).tolist()


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("finite_number_required")
    return float(value)


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _position(pose: Any) -> list[float]:
    return _rigid(pose)[:3, 3].tolist()


def _length(path: list[list[float]]) -> float:
    return sum(math.dist(a, b) for a, b in zip(path, path[1:]))


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timezone_aware_iso_time_required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone_aware_iso_time_required")
    return parsed


def _frame_name(row: dict) -> str | None:
    name = row.get("reference_name")
    if name is None:
        name = row.get("image_path")
        if isinstance(name, str) and name.startswith("images/"):
            name = name[len("images/"):]
    return name if isinstance(name, str) and name else None


def _frame_index(frames_manifest: dict | None, diagnostics: list) -> tuple[dict, dict]:
    """Keep *all* sampled records in time order so missing poses are barriers."""
    if not frames_manifest:
        diagnostics.append({"reason": "video_provenance_missing"})
        return {}, {}
    try:
        rows = sorted(frames_manifest["frames"], key=lambda row: _number(row["pts_time_s"]))
        times = [_number(row["pts_time_s"]) for row in rows]
        if any(b <= a for a, b in zip(times, times[1:])):
            raise ValueError("non_monotonic_pts")
        by_name, order = {}, {}
        for index, row in enumerate(rows):
            name = _frame_name(row)
            if name is not None:
                if name in by_name:
                    raise ValueError("duplicate_frame_name")
                by_name[name], order[name] = row, index
        return by_name, order
    except (KeyError, TypeError, ValueError) as exc:
        diagnostics.append({"reason": "invalid_frame_timeline", "detail": str(exc)})
        return {}, {}


def build_route_graph(manifest: dict, reference_poses: dict,
                      frames_manifest: dict | None = None, *,
                      component_id: str | None = None,
                      route_segments: list[dict] | None = None,
                      max_gap_s: float = 2.0) -> dict:
    """Build a JSON-compatible directed graph without inferring connections.

    Existing T_CW (3x4) reference poses and frames.v1 PTS records are accepted.
    ``route_segments`` is a separate, explicitly verified evidence sidecar;
    it may instead be placed in ``frames_manifest['route_segments']`` in memory.
    The original frozen manifest is never modified. Its canonical self-hash
    (excluding manifest_sha256) is checked using mapping.py's serialization.
    """
    if manifest.get("frozen") is not True:
        raise ValueError("map_not_frozen")
    content = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    map_hash = _digest(content)
    if manifest.get("manifest_sha256") != map_hash:
        raise ValueError("manifest_hash_mismatch")
    if not isinstance(manifest.get("map_id"), str) or not manifest["map_id"]:
        raise ValueError("map_id_required")
    max_gap_s = _number(max_gap_s)
    if max_gap_s <= 0:
        raise ValueError("invalid_max_gap_s")
    scale, units = manifest.get("scale_status"), manifest.get("units")
    if (scale, units) not in {("metric", "m"), ("arbitrary_monocular_scale", "map_units")}:
        raise ValueError("inconsistent_scale_units")
    components = manifest.get("components", [])
    ids = [c["component_id"] for c in components]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_component_id")
    if component_id is None:
        if len(ids) != 1:
            raise ValueError("component_selection_required")
        component_id = ids[0]
    if component_id not in ids:
        raise ValueError("unknown_component")
    image_components = {}
    for component in components:
        for image_id in component["image_ids"]:
            if image_id in image_components:
                raise ValueError("image_in_multiple_components")
            image_components[image_id] = component["component_id"]

    graph = {"schema_version": "go1loc.route_graph.v1", "map_id": manifest["map_id"],
             "map_hash": map_hash, "component_id": component_id,
             "scale_status": scale, "units": units, "nodes": {}, "edges": {},
             "max_gap_s": max_gap_s, "diagnostics": [], "provenance_kinds": [],
             "map_observation_time": manifest.get("map_observation_time"),
             "geometry_provenance": {"source": "frozen_reference_camera_poses",
                 "reference_poses_sha256": _digest(reference_poses),
                 "frames_manifest_sha256": _digest(frames_manifest)},
             "execution_permitted": False, "route_preview_only": True}
    rows, order = _frame_index(frames_manifest, graph["diagnostics"])
    source_sha = (frames_manifest or {}).get("probe", {}).get("source_sha256")
    for name, record in reference_poses.items():
        if image_components.get(record["image_id"]) != component_id:
            continue
        T_MC = _inverse(_rigid(record["T_CW"])).tolist()
        row = rows.get(name, {})
        graph["nodes"][name] = {
            "node_id": name, "frame_id": name, "image_id": record["image_id"],
            "component_id": component_id, "T_MC": T_MC,
            "pts_time_s": row.get("pts_time_s"), "captured_at": row.get("captured_at"),
            "frame_index": row.get("frame_index"), "source_sha256": source_sha,
            "quality": deepcopy({"reconstruction": record.get("quality"),
                                  "frame": row.get("quality")})}
    segments = route_segments if route_segments is not None else (frames_manifest or {}).get("route_segments", [])
    graph["geometry_provenance"]["route_segments_sha256"] = _digest(segments)
    used_segment_ids = set()
    for segment in segments:
        sid = segment.get("segment_id")
        reason = None
        if not isinstance(sid, str) or not sid or sid in used_segment_ids:
            raise ValueError("invalid_or_duplicate_segment_id")
        used_segment_ids.add(sid)
        if (segment.get("verified_continuous") is not True
                or not isinstance(segment.get("evidence_id"), str) or not segment["evidence_id"]
                or segment.get("source_kind") not in SOURCE_KINDS):
            reason = "continuous_segment_evidence_required"
        elif (not _sha256(source_sha)
              or segment.get("source_sha256") != source_sha):
            reason = "video_source_hash_mismatch"
        elif any(segment.get(k) != graph[k] for k in ("map_id", "map_hash")):
            reason = "segment_map_mismatch"
        names = segment.get("frame_names")
        if not isinstance(names, list) or len(names) < 2 or any(not isinstance(n, str) for n in names):
            reason = "segment_frame_names_required"
        if reason:
            graph["diagnostics"].append({"segment_id": sid, "reason": reason})
            continue
        graph["provenance_kinds"].append(segment["source_kind"])
        for index, (a, b) in enumerate(zip(names, names[1:])):
            reason = _edge_rejection(a, b, rows, order, graph, max_gap_s)
            if reason:
                graph["diagnostics"].append({"segment_id": sid, "from": a, "to": b, "reason": reason})
                continue
            # Every intermediate registered camera is retained as a graph node.
            path = [_position(graph["nodes"][n]["T_MC"]) for n in (a, b)]
            eid = f"{sid}:{index}"
            graph["edges"][eid] = {
                "edge_id": eid, "from": a, "to": b, "enabled": segment.get("enabled", True) is True,
                "path": path, "length": _length(path), "units": units,
                "provenance": {"kind": "verified_temporal_segment", "segment_id": sid,
                    "source_kind": segment["source_kind"], "source_sha256": source_sha,
                    "evidence_id": segment["evidence_id"], "frame_names": [a, b],
                    "pts_interval_s": [rows[a]["pts_time_s"], rows[b]["pts_time_s"]]}}
    graph["provenance_kinds"] = sorted(set(graph["provenance_kinds"]))
    graph["graph_sha256"] = _digest(graph)
    return graph


def _edge_rejection(a: str, b: str, rows: dict, order: dict, graph: dict,
                    max_gap_s: float) -> str | None:
    if a not in rows or b not in rows:
        return "frame_timing_missing"
    if a not in graph["nodes"] or b not in graph["nodes"]:
        return "unregistered_or_other_component"
    delta = rows[b]["pts_time_s"] - rows[a]["pts_time_s"]
    if not 0 < delta <= max_gap_s:
        return "temporal_gap_or_wrong_direction"
    if order[b] != order[a] + 1:
        return "missing_intermediate_frames"
    if rows[b].get("discontinuity_before") or rows[a].get("discontinuity_after"):
        return "video_discontinuity"
    if any(rows[n].get("kept") is not True or rows[n].get("quality", {}).get("rejection_reasons")
           for n in (a, b)):
        return "frame_quality_rejected"
    return None


def _identity_failure(value: dict, graph: dict) -> str | None:
    return next((key + "_mismatch" for key in IDENTITY if value.get(key) != graph[key]), None)


def _start_connection(graph: dict, start: dict, target: dict, now: datetime,
                      max_age: float) -> tuple[str | None, list, dict | None, list | None, str | None]:
    """Return node, prefix path, provenance, camera pose, rejection reason."""
    source = start.get("source")
    if source not in {"explicit_pose", "current_photo"}:
        return None, [], None, None, "unsupported_start_source"
    if source == "current_photo" and start.get("pose_status") != "localized":
        return None, [], None, None, "start_localization_failed"
    try:
        captured = _time(start.get("selected_at") if source == "explicit_pose" else start.get("captured_at"))
        localized = captured if source == "explicit_pose" else _time(start.get("localized_at"))
        if captured > localized or localized > now:
            raise ValueError("unordered_times")
        if (now - captured).total_seconds() > max_age:
            return None, [], None, None, "stale_start"
    except (TypeError, ValueError):
        return None, [], None, None, "invalid_start_time"
    if source == "explicit_pose":
        if start.get("manual") is not True:
            return None, [], None, None, "manual_selection_required"
        node = graph["nodes"].get(start.get("node_id"))
        if node is None:
            return None, [], None, None, "start_not_connected"
        try:
            supplied = _rigid(start.get("T_MC", node["T_MC"]))
        except ValueError:
            return None, [], None, None, "invalid_start_pose"
        if not np.allclose(supplied, node["T_MC"], atol=POSE_ATOL, rtol=0):
            return None, [], None, supplied.tolist(), "start_not_connected"
        evidence = {"kind": "manual_exact_node", "manual": True,
                    "node_id": node["node_id"], "selected_at": start["selected_at"]}
        return node["node_id"], [_position(node["T_MC"])], evidence, node["T_MC"], None
    try:
        camera = _rigid(start.get("T_MC", start.get("T_WC_query"))).tolist()
    except ValueError:
        return None, [], None, None, "invalid_start_pose"
    if (not isinstance(start.get("query_id"), str) or not start["query_id"]
            or not _sha256(start.get("photo_sha256"))
            or not isinstance(start.get("quality"), dict) or not start["quality"]):
        return None, [], None, camera, "current_photo_evidence_missing"
    roi_binding = target.get("roi_binding")
    roi_query_hash = roi_binding.get("query_sha256") if isinstance(roi_binding, dict) else None
    if (start["query_id"] == target.get("query_id")
            or start["photo_sha256"] == target.get("query_sha256")
            or start["photo_sha256"] == target.get("photo_sha256")
            or start["photo_sha256"] == roi_query_hash):
        return None, [], None, camera, "target_photo_is_not_current_pose"
    connection = start.get("attachment", {})
    if not isinstance(connection, dict):
        return None, [], None, camera, "start_not_connected"
    node = graph["nodes"].get(connection.get("node_id"))
    if (node is None or connection.get("kind") != "verified_temporal_connection"
            or connection.get("verified_continuous") is not True
            or connection.get("source_kind") not in SOURCE_KINDS
            or not _sha256(connection.get("source_sha256"))
            or not isinstance(connection.get("evidence_id"), str) or not connection["evidence_id"]
            or connection.get("units") != graph["units"]
            or any(connection.get(k) != start.get(k) for k in
                   (*IDENTITY, "query_id", "photo_sha256", "captured_at", "localized_at"))):
        return None, [], None, camera, "start_not_connected"
    try:
        path = np.asarray(connection["path"], dtype=float)
        times = [_number(t) for t in connection["pts_time_s"]]
        if (path.ndim != 2 or path.shape[1] != 3 or len(path) < 2 or not np.isfinite(path).all()
                or len(times) != len(path)
                or any(not 0 < b - a <= graph["max_gap_s"] for a, b in zip(times, times[1:]))
                or not np.allclose(path[0], _position(camera), atol=POSE_ATOL, rtol=0)
                or not np.allclose(path[-1], _position(node["T_MC"]), atol=POSE_ATOL, rtol=0)):
            raise ValueError("invalid_attachment_geometry")
    except (KeyError, TypeError, ValueError):
        return None, [], None, camera, "start_not_connected"
    return node["node_id"], path.tolist(), connection, camera, None


def _dijkstra(graph: dict, start: str, disabled: set[str]) -> tuple[dict, dict]:
    adjacency: dict[str, list] = {n: [] for n in graph["nodes"]}
    for eid, edge in sorted(graph["edges"].items()):
        if edge["enabled"] and eid not in disabled:
            adjacency[edge["from"]].append((edge["to"], edge["length"], eid))
    distance, previous, queue = {start: 0.0}, {}, [(0.0, start)]
    while queue:
        cost, current = heapq.heappop(queue)
        if cost != distance[current]:
            continue
        for neighbor, length, eid in adjacency[current]:
            candidate = cost + length
            if candidate < distance.get(neighbor, math.inf):
                distance[neighbor] = candidate
                previous[neighbor] = (current, eid)
                heapq.heappush(queue, (candidate, neighbor))
    return distance, previous


def plan_route(graph: dict, start: dict, target: dict, *, now: str,
               max_start_age_s: float = 30.0, disabled_edge_ids: tuple | list = (),
               T_BC: Any = None) -> dict:
    """Dijkstra preview to an actually supported reference observation camera.

    `now` must be a timezone-aware ISO time, supplied explicitly for purity.
    Known edges can be disabled per call without modifying the graph. Supplying
    T_BC requests metric start-body conversion; the route remains camera-only.
    Missing connectivity never produces a straight-line fallback.
    """
    now_value = _time(now)
    max_age = _number(max_start_age_s)
    if max_age < 0:
        raise ValueError("invalid_max_start_age_s")
    result = {
        "schema_version": "go1loc.planning_result.v1",
        **{key: graph.get(key) for key in IDENTITY},
        "start_source": start.get("source"),
        "start_times": {key: start.get(key) for key in ("captured_at", "localized_at", "selected_at")},
        "planned_at": now, "max_start_age_s": max_age,
        "start_quality": deepcopy(start.get("quality")),
        "start_camera_pose": None, "start_base_pose": None,
        "target_object_result": deepcopy(target), "goal_observation_pose": None,
        "goal_frame_id": None, "goal_candidates": [], "route_node_ids": [],
        "route_edge_ids": [], "path": [], "length": None,
        "units": graph.get("units"), "scale_status": graph.get("scale_status"),
        "status": "rejected", "failure_reason": None,
        "route_kind": "camera_route", "route_preview_only": True,
        "execution_permitted": False, "graph_sha256": graph.get("graph_sha256"),
        "geometry_provenance": deepcopy(graph.get("geometry_provenance")),
        "connectivity_provenance": {"algorithm": "dijkstra", "kind": "historical_temporal_graph",
                                    "start_attachment": None, "edges": []},
        "map_observation_time": graph.get("map_observation_time"),
        "provenance_kinds": list(graph.get("provenance_kinds", [])),
        "warnings": ["historical_route_does_not_prove_current_clearance",
                     "target_observation_does_not_prove_object_is_still_present",
                     "camera_route_not_body_or_footstep_route"],
    }

    def reject(reason: str) -> dict:
        result["failure_reason"] = reason
        return result

    if (graph.get("schema_version") != "go1loc.route_graph.v1"
            or graph.get("graph_sha256") != _digest({k: v for k, v in graph.items() if k != "graph_sha256"})):
        return reject("invalid_graph")
    for value in (start, target):
        reason = _identity_failure(value, graph)
        if reason:
            return reject(reason)
    if target.get("status", target.get("object_status")) != "supported":
        return reject("target_not_supported")
    support = target.get("supporting_frame_ids")
    if not isinstance(support, list) or any(not isinstance(n, str) for n in support):
        return reject("goal_not_supported")
    goals = sorted(set(support) & graph["nodes"].keys())
    if not goals:
        return reject("goal_not_supported")
    node, prefix, evidence, camera, reason = _start_connection(graph, start, target, now_value, max_age)
    result["start_camera_pose"] = deepcopy(camera)
    if reason:
        return reject(reason)
    result["connectivity_provenance"]["start_attachment"] = deepcopy(evidence)
    if evidence.get("source_kind"):
        result["provenance_kinds"] = sorted(set(result["provenance_kinds"] + [evidence["source_kind"]]))
    if T_BC is not None:
        try:
            result["start_base_pose"] = camera_to_base_pose(camera, T_BC, scale_status=graph["scale_status"],
                                                            units=graph["units"])
        except ValueError as exc:
            return reject(str(exc))
    disabled = set(disabled_edge_ids)
    if not disabled <= graph["edges"].keys():
        return reject("unknown_disabled_edge")
    distances, previous = _dijkstra(graph, node, disabled)
    quality = target.get("observation_quality", {})
    for goal in goals:
        try:
            score = _number(quality.get(goal))
        except (AttributeError, ValueError):
            score = None
        result["goal_candidates"].append({"frame_id": goal, "reachable": goal in distances,
            "route_length": distances.get(goal), "observation_quality": score})
    reachable = [c for c in result["goal_candidates"] if c["reachable"]]
    if not reachable:
        return reject("no_path")
    # Quality is caller-provided ranking/count, never a fabricated probability.
    chosen = min(reachable, key=lambda c: (c["route_length"],
                    -(c["observation_quality"] if c["observation_quality"] is not None else -math.inf), c["frame_id"]))
    goal = chosen["frame_id"]
    nodes, edges, cursor = [goal], [], goal
    while cursor != node:
        cursor, eid = previous[cursor]
        nodes.append(cursor)
        edges.append(eid)
    nodes.reverse()
    edges.reverse()
    path = deepcopy(prefix)
    for eid in edges:
        path.extend(deepcopy(graph["edges"][eid]["path"][1:]))
    result.update(status="route_preview_only", goal_frame_id=goal,
                  goal_observation_pose=deepcopy(graph["nodes"][goal]["T_MC"]),
                  route_node_ids=nodes, route_edge_ids=edges, path=path, length=_length(path))
    result["connectivity_provenance"]["edges"] = [deepcopy(graph["edges"][e]["provenance"]) for e in edges]
    return result
