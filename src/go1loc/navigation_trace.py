"""Evidence-bound continuous camera-route traces.

An imported trace is a sidecar extension to a frozen navigation index.  It may
add only poses that were actually localized from a newer continuous recording.
It never turns a spatially nearby pose into a route edge, never marks an edge
``verified_continuous`` and never enables robot execution.
"""
from copy import deepcopy
import math
from pathlib import Path

import numpy as np

from .evidence import digest_json
from .planning import _rigid


_TRACE_KIND = "verified_route_trace_observation"
_ANCHOR_KIND = "verified_route_trace_anchor_projection"


def _sha(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _position(pose):
    return _rigid(pose)[:3, 3]


def _pose_angle(a, b):
    A, B = _rigid(a), _rigid(b)
    value = (np.trace(A[:3, :3].T @ B[:3, :3]) - 1.) / 2.
    return math.degrees(math.acos(float(np.clip(value, -1., 1.))))


def _quality_ok(row):
    quality = row.get("quality") or {}
    flags = quality.get("rejection_reasons") or row.get("continuity_flags") or []
    return (row.get("pose_status", "localized") == "localized"
            and not flags
            # Consume the normal localizer acceptance contract.  The trace
            # importer does not silently invent a second, different quality
            # threshold; evidence verification and temporal continuity are
            # the additional route-specific gates.
            and int(quality.get("num_inliers", 0)) >= 6
            and float(quality.get("inlier_ratio", 0.)) >= .25)


def _check_observation(row, graph, source):
    if not isinstance(row.get("name"), str) or not row["name"]:
        raise ValueError("trace_name_required")
    if not _sha(row.get("evidence_sha256")) or not _sha(row.get("query_sha256")):
        raise ValueError("trace_evidence_required")
    if len(set(row.get("references") or [])) < 2:
        raise ValueError("trace_reference_evidence_required")
    if not _quality_ok(row):
        raise ValueError("trace_quality_gate_not_met")
    try:
        _rigid(row["T_MC"])
        pts = float(row["pts_time_s"])
        sequence = int(row["source_sequence"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("trace_timing_or_pose_required") from exc
    if not math.isfinite(pts):
        raise ValueError("trace_timing_or_pose_required")
    for key in ("source_sha256", "session_id", "camera_id"):
        if row.get(key) != source[key]:
            raise ValueError("trace_source_identity_conflict")
    for key in ("map_hash", "component_id"):
        if key in row and row[key] != graph[key]:
            raise ValueError("trace_map_identity_conflict")
    return pts, sequence


def _edge_projection(edge, fraction):
    if not isinstance(fraction, (int, float)) or isinstance(fraction, bool):
        raise ValueError("trace_anchor_fraction_required")
    fraction = float(fraction)
    if not 0. <= fraction <= 1.:
        raise ValueError("trace_anchor_fraction_required")
    a, b = np.asarray(edge["path"][0], float), np.asarray(edge["path"][-1], float)
    return fraction, a + fraction * (b - a)


def _edge_record(graph, edge_id, from_id, to_id, path, *, trace_id, role, provenance):
    path = [np.asarray(p, float).tolist() for p in path]
    length = float(sum(np.linalg.norm(np.subtract(b, a)) for a, b in zip(path, path[1:])))
    edge_provenance = dict(provenance)
    edge_provenance.update(kind=_ANCHOR_KIND if role == "anchor" else _TRACE_KIND,
                           trace_id=trace_id)
    return dict(edge_id=edge_id, **{"from": from_id, "to": to_id}, path=path,
                enabled=True, length=length, cost=length,
                navigation_cost=length, units=graph["units"],
                observation_only=length < graph["typical_step"] * .08,
                provenance=edge_provenance,
                execution_permitted=False)


def _validate_anchor(graph, anchor, source, *, role):
    if anchor.get("role") != role or anchor.get("edge_id") not in graph["edges"]:
        raise ValueError("trace_anchor_edge_required")
    edge = graph["edges"][anchor["edge_id"]]
    if not edge.get("enabled"):
        raise ValueError("trace_anchor_edge_disabled")
    fraction, projection = _edge_projection(edge, anchor.get("fraction"))
    supplied = anchor.get("projection")
    if supplied is not None and not np.allclose(projection, np.asarray(supplied, float), atol=1e-7, rtol=0):
        raise ValueError("trace_anchor_projection_mismatch")
    obs = anchor.get("observation") or {}
    pts, sequence = _check_observation(obs, graph, source)
    if np.linalg.norm(_position(obs["T_MC"]) - projection) > 2. * graph["typical_step"]:
        raise ValueError("trace_anchor_distance_exceeded")
    return dict(edge=edge, fraction=fraction, projection=projection, observation=obs,
                pts_time_s=pts, source_sequence=sequence)


def add_verified_route_trace(graph, samples, *, trace_id, anchors=(), provenance=None,
                             max_interval_s=.5):
    """Add a bounded, directed trace from actual localized observations.

    ``anchors`` optionally contains ``entry`` and ``exit`` records.  Each anchor
    is a measured observation immediately before/after the trace, associated to
    an existing edge by the caller using the normal route-association rules.
    The projection is recomputed from the existing edge; a supplied XYZ is only
    accepted as a consistency check, never as a new pose source.
    """
    if not isinstance(trace_id, str) or not trace_id:
        raise ValueError("trace_id_required")
    if not isinstance(samples, list) or len(samples) < 2:
        raise ValueError("trace_needs_two_observations")
    if any("verified_continuous" in row for row in samples) or any("verified_continuous" in (a or {}) for a in anchors):
        raise ValueError("trace_verified_continuous_field_forbidden")
    source = {key: samples[0].get(key) for key in ("source_sha256", "session_id", "camera_id")}
    if not _sha(source["source_sha256"]) or not source["session_id"] or not source["camera_id"]:
        raise ValueError("trace_source_identity_required")
    if graph.get("execution_permitted") is not False:
        raise ValueError("trace_execution_must_remain_disabled")
    seen = set(); times = []; sequences = []
    for row in samples:
        if row["name"] in seen or row["name"] in graph["nodes"]:
            raise ValueError("trace_duplicate_or_existing_node")
        seen.add(row["name"])
        pts, sequence = _check_observation(row, graph, source)
        times.append(pts); sequences.append(sequence)
    for a, b in zip(samples, samples[1:]):
        dt = float(b["pts_time_s"]) - float(a["pts_time_s"])
        if not 0. < dt <= float(max_interval_s):
            raise ValueError("trace_interval_exceeded")
        if int(b["source_sequence"]) <= int(a["source_sequence"]):
            raise ValueError("trace_sequence_not_monotonic")
        A, B = _rigid(a["T_MC"]), _rigid(b["T_MC"])
        if np.linalg.norm(A[:3, 3] - B[:3, 3]) > 8. * graph["typical_step"]:
            raise ValueError("trace_pose_discontinuity")
        if _pose_angle(A, B) > 120.:
            raise ValueError("trace_pose_discontinuity")

    anchors = list(anchors)
    if len(anchors) > 2 or any(a.get("role") not in {"entry", "exit"} for a in anchors):
        raise ValueError("trace_anchor_roles_invalid")
    roles = {a.get("role") for a in anchors}
    if len(roles) != len(anchors):
        raise ValueError("trace_anchor_roles_invalid")
    checked = {}
    for role in roles:
        checked[role] = _validate_anchor(graph, next(a for a in anchors if a.get("role") == role),
                                         source, role=role)
    if "entry" in checked:
        if not 0. < times[0] - checked["entry"]["pts_time_s"] <= max_interval_s:
            raise ValueError("trace_anchor_interval_exceeded")
        if sequences[0] <= checked["entry"]["source_sequence"]:
            raise ValueError("trace_anchor_sequence_invalid")
    if "exit" in checked:
        if not 0. < checked["exit"]["pts_time_s"] - times[-1] <= max_interval_s:
            raise ValueError("trace_anchor_interval_exceeded")
        if checked["exit"]["source_sequence"] <= sequences[-1]:
            raise ValueError("trace_anchor_sequence_invalid")

    g = deepcopy(graph)
    g.setdefault("route_traces", [])
    g.setdefault("navigation_nodes", [])
    common = dict(trace_id=trace_id, source_sha256=source["source_sha256"],
                  session_id=source["session_id"], camera_id=source["camera_id"],
                  time_source=(provenance or {}).get("time_source", "recorded_source_time"),
                  execution_permitted=False)
    anchor_ids = {}
    for role, item in checked.items():
        anchor_id = f"{trace_id}:anchor:{role}"
        if anchor_id in g["nodes"]:
            raise ValueError("trace_id_conflict")
        anchor_ids[role] = anchor_id
        pose = _rigid(item["observation"]["T_MC"]).copy()
        pose[:3, 3] = item["projection"]
        g["nodes"][anchor_id] = dict(node_id=anchor_id, frame_id=anchor_id,
            image_id=None, component_id=g["component_id"], T_MC=pose.tolist(),
            pts_time_s=item["pts_time_s"], image_path=None, native_registered=False,
            geometry_source="verified_route_association_projection",
            localization_references=list(item["observation"]["references"]),
            evidence_sha256=item["observation"]["evidence_sha256"],
            query_sha256=item["observation"]["query_sha256"],
            source_sequence=item["source_sequence"], source_sha256=source["source_sha256"])
        g["navigation_nodes"].append(anchor_id)
        edge = item["edge"]
        before = [edge["path"][0], item["projection"]]
        after = [item["projection"], edge["path"][-1]]
        # Keep zero-length endpoint links as explicit topology.  They are not
        # shortcuts; they connect the derived projection node to the exact
        # original endpoint when the anchor is at fraction 0 or 1.
        eid = f"{trace_id}:anchor:{role}:from"
        g["edges"][eid] = _edge_record(g, eid, edge["from"], anchor_id, before,
            trace_id=trace_id, role="anchor", provenance={**common, "anchor_role": role,
                "original_edge_id": item["edge"].get("edge_id"), "fraction": item["fraction"]})
        eid = f"{trace_id}:anchor:{role}:to"
        g["edges"][eid] = _edge_record(g, eid, anchor_id, edge["to"], after,
            trace_id=trace_id, role="anchor", provenance={**common, "anchor_role": role,
                "original_edge_id": item["edge"].get("edge_id"), "fraction": item["fraction"]})

    for row in samples:
        g["nodes"][row["name"]] = dict(node_id=row["name"], frame_id=row["name"],
            image_id=None, component_id=g["component_id"], T_MC=_rigid(row["T_MC"]).tolist(),
            pts_time_s=float(row["pts_time_s"]), image_path=row.get("image_path"),
            native_registered=False, geometry_source="verified_historical_route_trace",
            localization_references=list(row["references"]), evidence_sha256=row["evidence_sha256"],
            query_sha256=row["query_sha256"], source_sequence=int(row["source_sequence"]),
            source_sha256=source["source_sha256"])
        g["navigation_nodes"].append(row["name"])

    edge_rows = []
    if "entry" in checked:
        edge_rows.append((anchor_ids["entry"], samples[0]["name"], "entry"))
    edge_rows.extend((a["name"], b["name"], "sample") for a, b in zip(samples, samples[1:]))
    if "exit" in checked:
        edge_rows.append((samples[-1]["name"], anchor_ids["exit"], "exit"))
    for index, (from_id, to_id, role) in enumerate(edge_rows):
        path = [_position(g["nodes"][from_id]["T_MC"]).tolist(),
                _position(g["nodes"][to_id]["T_MC"]).tolist()]
        eid = f"{trace_id}:trace:{index}"
        g["edges"][eid] = _edge_record(g, eid, from_id, to_id, path,
            trace_id=trace_id, role="sample", provenance={**common, "trace_role": role,
                "from": from_id, "to": to_id})

    trace_record = dict(common, sample_names=[row["name"] for row in samples],
                        anchor_nodes=anchor_ids, edge_ids=[f"{trace_id}:trace:{i}" for i in range(len(edge_rows))],
                        provenance=deepcopy(provenance or {}))
    g["route_traces"].append(trace_record)
    g["provenance_kinds"] = sorted(set(g.get("provenance_kinds", [])) | {_TRACE_KIND, _ANCHOR_KIND})
    g["index_sha256"] = digest_json({k: v for k, v in g.items() if k != "index_sha256"})
    return g


def load_route_trace(graph, sidecar):
    """Load an optional route_trace_import.json without trusting hand-filled poses.

    The sidecar stores query directories and source timing only.  Pose, map and
    evidence hashes are read from the sealed localization results and verified
    again before ``add_verified_route_trace`` is called.
    """
    path = Path(sidecar) / "route_trace_import.json"
    if not path.is_file():
        return graph
    from .navigation_localizer import verified_localization
    from .jobs import read_json
    spec = read_json(path)
    if spec.get("schema_version") != "go1loc.route_trace_import.v1":
        raise ValueError("trace_import_schema_invalid")
    if spec.get("map_hash") != graph.get("map_hash") or spec.get("component_id") != graph.get("component_id"):
        raise ValueError("trace_import_map_mismatch")
    samples = []
    for row in spec.get("samples", []):
        value, evidence = verified_localization(row["query_dir"], graph["map_hash"])
        if value.get("pose_status") != "localized" or value.get("component_id") != graph["component_id"]:
            raise ValueError("trace_import_localization_not_usable")
        samples.append({**row, "T_MC": value["T_WC_query"],
                        "pose_status": value["pose_status"],
                        "references": value["matched_references"],
                        "evidence_sha256": evidence["evidence_sha256"],
                        "query_sha256": evidence["binding"]["query_sha256"],
                        "map_hash": value["map_hash"], "component_id": value["component_id"],
                        "quality": value.get("quality", {})})
    anchors = []
    for anchor in spec.get("anchors", []):
        value, evidence = verified_localization(anchor["query_dir"], graph["map_hash"])
        anchors.append({**anchor, "observation": {**anchor.get("observation", {}),
            "T_MC": value["T_WC_query"], "pose_status": value["pose_status"],
            "references": value["matched_references"],
            "evidence_sha256": evidence["evidence_sha256"],
            "query_sha256": evidence["binding"]["query_sha256"],
            "map_hash": value["map_hash"], "component_id": value["component_id"],
            "quality": value.get("quality", {})}})
    return add_verified_route_trace(graph, samples, trace_id=spec["trace_id"], anchors=anchors,
        provenance={k: v for k, v in spec.items() if k in {"source_kind", "time_source", "source_sha256"}})
