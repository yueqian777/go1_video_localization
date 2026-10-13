"""Pure, bounded candidate/review workflow for historical camera routes.

Chronology proposes candidates, never approval. Only an explicit reviewer
decision emits verified route_segments. No file I/O, media synthesis, nearest
neighbor edges, or robot execution. See docs/REVIEW_TIME_CONTRACT.md.
"""
from __future__ import annotations

from copy import deepcopy

from .planning import (_digest, _edge_rejection, _frame_index, _frame_name,
                       _number, _position, _sha256, _time, build_route_graph)


MAX_FRAMES = 100_000
MAX_DECISIONS = 10_000


def _seal(review: dict) -> dict:
    review.pop("review_sha256", None)
    review["review_sha256"] = _digest(review)
    return review


def _candidate(review: dict, component_id: str, names: list, times: list,
               path: list, *, parent_id: str | None = None) -> dict:
    identity = {k: review[k] for k in ("map_id", "map_hash", "source_sha256",
                                      "source_kind", "input_sha256", "max_gap_s")}
    cid = "candidate-" + _digest({**identity, "component_id": component_id,
                                 "frame_names": names, "parent_id": parent_id})[:24]
    return {"candidate_id": cid, "component_id": component_id,
            "status": "PENDING", "verified_continuous": False,
            "frame_names": deepcopy(names), "pts_time_s": deepcopy(times),
            "camera_path": deepcopy(path), "parent_candidate_id": parent_id}


def generate_route_candidates(manifest: dict, reference_poses: dict,
                              frames_manifest: dict | None, max_gap_s: float = 2.0) -> dict:
    """Propose PENDING groups using registered poses and ALL sampled frame PTS.

    Missing/unregistered/quality-rejected records, gaps, declared cuts and
    component changes are barriers, not deletable shortcuts. Media source hash
    is mandatory. The caller must verify actual video and native map inputs;
    a hash or smoothly ordered PTS is not proof of genuine motion continuity.
    ``source_kind=synthetic_test`` is preserved, never relabeled as real video.
    Malformed configuration/frozen-map inputs raise ValueError; absent/invalid
    video timeline yields an empty candidate set with diagnostics.
    """
    if not isinstance(reference_poses, dict) or len(reference_poses) > MAX_FRAMES:
        raise ValueError("invalid_or_excessive_reference_poses")
    if frames_manifest is not None and not isinstance(frames_manifest, dict):
        raise ValueError("invalid_frames_manifest")
    frames = frames_manifest or {}
    raw = frames.get("frames", [])
    if not isinstance(raw, list) or len(raw) > MAX_FRAMES:
        raise ValueError("invalid_or_excessive_frame_count")
    if any(not isinstance(row, dict) for row in raw):
        raise ValueError("invalid_frame_record")
    # When embedded in a frozen map, a different sampling timeline is not the
    # same source merely because someone copied the video file hash.
    embedded = manifest.get("frames_manifest")
    if embedded is not None and _digest(embedded) != _digest(frames_manifest):
        raise ValueError("frames_manifest_binding_mismatch")
    components = manifest.get("components", [])
    if not components:
        raise ValueError("map_components_required")
    graphs = {c["component_id"]: build_route_graph(manifest, reference_poses, frames_manifest,
              component_id=c["component_id"], route_segments=[], max_gap_s=max_gap_s)
              for c in components}
    probe = frames.get("probe") or {}
    if not isinstance(probe, dict):
        raise ValueError("invalid_video_probe")
    review = {"schema_version": "go1loc.route_review.v1", "map_id": manifest["map_id"],
              "map_hash": manifest["manifest_sha256"], "source_sha256": probe.get("source_sha256"),
              "source_kind": frames.get("source_kind", "video" if frames_manifest is not None else "unordered_photos"),
              "capture_device_kind": frames.get("capture_device_kind", "unknown"),
              "source_path": probe.get("source_path"),
              "max_gap_s": _number(max_gap_s), "candidates": [], "timeline": [],
              "diagnostics": [], "route_segments": [], "decisions": [],
              "input_sha256": _digest({"manifest": manifest, "reference_poses": reference_poses,
                                        "frames_manifest": frames_manifest}),
              "execution_permitted": False,
              "warnings": ["pending_is_not_continuity_evidence",
                           "human_must_review_original_video_for_unmarked_cuts",
                           "camera_route_not_body_or_footstep_route"]}
    if not _sha256(review["source_sha256"]):
        review["diagnostics"].append({"reason": "video_source_hash_required"})
    if review["source_kind"] not in {"video", "synthetic_test"}:
        review["diagnostics"].append({"reason": "unsupported_source_kind"})
    rows, order = _frame_index(frames_manifest, review["diagnostics"])
    if not rows:
        if not review["diagnostics"]:
            review["diagnostics"].append({"reason": "frame_timeline_empty"})
        return _seal(review)
    ordered = sorted(raw, key=lambda row: _number(row["pts_time_s"]))
    nodes = {name: node for graph in graphs.values() for name, node in graph["nodes"].items()}
    for index, row in enumerate(ordered):
        name = _frame_name(row)
        node = nodes.get(name)
        reasons = []
        quality = row.get("quality")
        if name is None:
            reasons.append("frame_name_missing")
        if node is None:
            reasons.append("unregistered_frame")
        if (row.get("kept") is not True or not isinstance(quality, dict)
                or quality.get("rejection_reasons")):
            reasons.append("frame_quality_rejected")
        record = reference_poses.get(name, {})
        pose_quality = record.get("quality")
        if (record.get("registered") is False or (isinstance(pose_quality, dict)
                and (pose_quality.get("rejection_reasons") or pose_quality.get("valid") is False))):
            reasons.append("registration_quality_rejected")
        review["timeline"].append({"timeline_index": index, "frame_name": name,
            "image_path": row.get("image_path"), "frame_index": row.get("frame_index"),
            "pts_time_s": row["pts_time_s"], "captured_at": row.get("captured_at"),
            "registered": node is not None, "component_id": node["component_id"] if node else None,
            "kept": row.get("kept"), "quality": deepcopy(quality), "reasons": reasons,
            "discontinuity_before": bool(row.get("discontinuity_before")),
            "discontinuity_after": bool(row.get("discontinuity_after"))})
        for reason in reasons:
            review["diagnostics"].append({"timeline_index": index, "frame_name": name, "reason": reason})
    source_valid = (_sha256(review["source_sha256"])
                    and review["source_kind"] in {"video", "synthetic_test"})
    if not source_valid:
        return _seal(review)
    group = []

    def flush() -> None:
        if len(group) >= 2:
            names = [entry["frame_name"] for entry in group]
            review["candidates"].append(_candidate(review, group[0]["component_id"], names,
                [entry["pts_time_s"] for entry in group], [_position(nodes[n]["T_MC"]) for n in names]))
        elif group:
            review["diagnostics"].append({"frame_name": group[0]["frame_name"],
                                           "reason": "insufficient_contiguous_frames"})
        group.clear()

    for entry in review["timeline"]:
        if entry["reasons"]:
            flush()
            continue
        if group:
            previous = group[-1]
            reason = ("component_change" if previous["component_id"] != entry["component_id"] else
                _edge_rejection(previous["frame_name"], entry["frame_name"], rows, order,
                                graphs[entry["component_id"]], review["max_gap_s"]))
            if reason:
                review["diagnostics"].append({"from": previous["frame_name"],
                    "to": entry["frame_name"], "reason": reason})
                flush()
        group.append(entry)
    flush()
    return _seal(review)


def review_route_candidate(review: dict, candidate_id: str, decision: str, *,
                           first_name: str | None = None, last_name: str | None = None,
                           reviewer: str, evidence_note: str, reviewed_at: str) -> dict:
    """Return a new reviewed document. Only explicit confirm approves a segment.

    Confirm/reject apply to a complete PENDING candidate. To choose a subrange,
    split first: selected range and disjoint prefix/suffix become new PENDING
    candidates (singletons have no edge and are reported). No boundary edge is
    silently retained. Decisions are serialized call kwargs for deterministic
    replay against freshly verified inputs by the save worker.
    """
    if (not isinstance(review, dict) or review.get("schema_version") != "go1loc.route_review.v1"
            or review.get("review_sha256") != _digest({k: v for k, v in review.items()
                                                      if k != "review_sha256"})):
        raise ValueError("review_hash_mismatch")
    if len(review["decisions"]) >= MAX_DECISIONS:
        raise ValueError("review_decision_limit")
    if decision not in {"confirm", "reject", "split"}:
        raise ValueError("invalid_review_decision")
    for name, text in (("reviewer", reviewer), ("evidence_note", evidence_note)):
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError(name + "_required_or_too_long")
    _time(reviewed_at)
    result = deepcopy(review)
    candidate = next((c for c in result["candidates"] if c["candidate_id"] == candidate_id), None)
    if candidate is None:
        raise ValueError("unknown_candidate")
    if candidate["status"] != "PENDING":
        raise ValueError("candidate_not_pending")
    names = candidate["frame_names"]
    if (first_name is None) != (last_name is None):
        raise ValueError("both_range_endpoints_required")
    first_name = names[0] if first_name is None else first_name
    last_name = names[-1] if last_name is None else last_name
    if first_name not in names or last_name not in names:
        raise ValueError("unknown_range_endpoint")
    first, last = names.index(first_name), names.index(last_name)
    if first > last:
        raise ValueError("reversed_range")
    full_range = first == 0 and last == len(names) - 1
    if decision != "split" and not full_range:
        raise ValueError("partial_decision_requires_split")
    if decision == "split" and full_range:
        raise ValueError("split_must_change_range")
    record = {"candidate_id": candidate_id, "decision": decision,
              "first_name": first_name, "last_name": last_name,
              "reviewer": reviewer, "evidence_note": evidence_note, "reviewed_at": reviewed_at}
    if decision == "confirm":
        if len(names) < 2:
            raise ValueError("segment_requires_two_frames")
        candidate.update(status="CONFIRMED", verified_continuous=True)
        result["route_segments"].append({
            "segment_id": "reviewed-" + candidate_id,
            **{k: review[k] for k in ("map_id", "map_hash", "source_kind", "source_sha256")},
            "component_id": candidate["component_id"], "verified_continuous": True,
            "evidence_id": "review:" + _digest(record), "frame_names": deepcopy(names),
            "enabled": True, "review": deepcopy(record), "input_sha256": review["input_sha256"]})
    elif decision == "reject":
        candidate["status"] = "REJECTED"
    else:
        candidate["status"] = "SPLIT"
        for begin, end in ((0, first), (first, last + 1), (last + 1, len(names))):
            if end - begin >= 2:
                result["candidates"].append(_candidate(review, candidate["component_id"], names[begin:end],
                    candidate["pts_time_s"][begin:end], candidate["camera_path"][begin:end], parent_id=candidate_id))
            elif end - begin == 1:
                result["diagnostics"].append({"frame_name": names[begin], "parent_candidate_id": candidate_id,
                                               "reason": "split_singleton_has_no_route"})
    result["decisions"].append(record)
    return _seal(result)
