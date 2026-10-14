"""Synthetic offline fixtures; these are not Go1 motion acceptance tests."""
from copy import deepcopy
import hashlib
import json
import unittest

import numpy as np

from go1loc.planning import build_route_graph, camera_to_base_pose, plan_route


NOW = "2026-10-13T10:00:00+00:00"
CAPTURED = "2026-10-13T09:59:55+00:00"
LOCALIZED = "2026-10-13T09:59:58+00:00"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def seal(manifest):
    manifest.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = digest(manifest)
    return manifest


def pose(x=0, y=0, z=0):
    value = np.eye(4)
    value[:3, 3] = [x, y, z]
    return value.tolist()


def fixture():
    names = ["z.png", "a.png", "m.png", "wall.png"]
    # The last camera is physically nearby but around a corner, NOT connected.
    positions = [(0, 0, 0), (0, 2, 0), (2, 2, 0), (0.01, 0, 0)]
    manifest = seal({"map_id": "synthetic-map", "frozen": True,
                     "scale_status": "arbitrary_monocular_scale", "units": "map_units",
                     "components": [{"component_id": "c0", "image_ids": [1, 2, 3, 4]}],
                     "reference_images": names, "map_observation_time": "2020-01-01T00:00:00Z"})
    refs = {name: {"image_id": i + 1, "T_CW": np.linalg.inv(pose(*xyz))[:3].tolist()}
            for i, (name, xyz) in enumerate(zip(names, positions))}
    frames = {"probe": {"source_sha256": "a" * 64}, "timing_source": "best_effort_pts_not_exposure_time",
              "frames": [{"image_path": "images/" + name, "kept": True,
                          "frame_index": i * 10, "pts_time_s": i * 0.5,
                          "quality": {"rejection_reasons": []}} for i, name in enumerate(names)]}
    frames["route_segments"] = [{"segment_id": "synthetic-segment", "source_kind": "synthetic_test",
        "source_sha256": "a" * 64, "map_id": manifest["map_id"],
        "map_hash": manifest["manifest_sha256"], "verified_continuous": True,
        "evidence_id": "synthetic-test-only:continuous-segment", "frame_names": names[:3], "enabled": True}]
    return manifest, refs, frames


def inputs(graph, goal="m.png", start="z.png"):
    identity = {key: graph[key] for key in ("map_id", "map_hash", "component_id")}
    return ({**identity, "source": "explicit_pose", "manual": True,
             "node_id": start, "selected_at": CAPTURED},
            {**identity, "status": "supported", "supporting_frame_ids": [goal],
             "object_surface_position": [123, 456, 789], "query_id": "target-photo",
             "query_sha256": "d" * 64})


class RoutePlanningTests(unittest.TestCase):
    def setUp(self):
        self.manifest, self.refs, self.frames = fixture()
        self.graph = build_route_graph(self.manifest, self.refs, self.frames)
        self.start, self.target = inputs(self.graph)

    def plan(self, **kwargs):
        return plan_route(self.graph, self.start, self.target, now=NOW, **kwargs)

    def assert_rejected(self, result, reason):
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["failure_reason"], reason)
        self.assertEqual(result["path"], [])
        self.assertEqual(result["route_edge_ids"], [])
        self.assertIsNone(result["length"])
        self.assertFalse(result["execution_permitted"])

    def test_forward_directed_route_uses_polyline_not_object_or_straight_line(self):
        result = self.plan()
        self.assertEqual(result["status"], "route_preview_only")
        self.assertEqual(result["route_node_ids"], ["z.png", "a.png", "m.png"])
        self.assertEqual(result["path"], [[0, 0, 0], [0, 2, 0], [2, 2, 0]])
        self.assertEqual(result["length"], 4)
        self.assertEqual(result["units"], "map_units")
        self.assertEqual(result["goal_observation_pose"], pose(2, 2))
        self.assertIsNone(result["start_base_pose"])
        self.assertFalse(result["execution_permitted"])
        self.assertEqual(result["route_kind"], "camera_route")
        self.assertIn("synthetic_test", result["provenance_kinds"])
        self.assertNotIn("nav_goal", result)
        json.dumps(result, allow_nan=False)

    def test_manifest_hash_is_verified_not_trusted_or_replaced_with_file_hash(self):
        self.assertEqual(self.graph["map_hash"], self.manifest["manifest_sha256"])
        self.manifest["map_id"] = "modified"
        with self.assertRaisesRegex(ValueError, "manifest_hash_mismatch"):
            build_route_graph(self.manifest, self.refs, self.frames)

    def test_frozen_map_required(self):
        self.manifest["frozen"] = False
        seal(self.manifest)
        with self.assertRaisesRegex(ValueError, "map_not_frozen"):
            build_route_graph(self.manifest, self.refs, self.frames)

    def test_no_video_provenance_does_not_create_filename_route(self):
        graph = build_route_graph(self.manifest, self.refs, None)
        self.assertEqual(graph["edges"], {})
        self.assertIn("video_provenance_missing", str(graph["diagnostics"]))
        self.assert_rejected(plan_route(graph, self.start, self.target, now=NOW), "no_path")

    def test_pts_alone_is_not_continuity_proof(self):
        self.frames.pop("route_segments")
        graph = build_route_graph(self.manifest, self.refs, self.frames)
        self.assertEqual(graph["edges"], {})

    def test_unordered_records_use_real_pts_not_name_or_image_id(self):
        self.frames["frames"].reverse()
        graph = build_route_graph(self.manifest, dict(reversed(list(self.refs.items()))), self.frames)
        result = plan_route(graph, self.start, self.target, now=NOW)
        self.assertEqual(result["route_node_ids"], ["z.png", "a.png", "m.png"])

    def test_time_gap_breaks_route(self):
        self.frames["frames"][2]["pts_time_s"] = 10
        graph = build_route_graph(self.manifest, self.refs, self.frames, max_gap_s=1)
        self.assert_rejected(plan_route(graph, self.start, self.target, now=NOW), "no_path")

    def test_missing_registration_is_not_bridged(self):
        self.refs.pop("a.png")
        graph = build_route_graph(self.manifest, self.refs, self.frames)
        self.assertEqual(graph["edges"], {})

    def test_cannot_skip_known_intermediate_frame(self):
        self.frames["route_segments"][0]["frame_names"] = ["z.png", "m.png"]
        graph = build_route_graph(self.manifest, self.refs, self.frames)
        self.assertEqual(graph["edges"], {})

    def test_jump_cut_rejected_even_with_small_gap(self):
        self.frames["frames"][1]["discontinuity_before"] = True
        graph = build_route_graph(self.manifest, self.refs, self.frames)
        self.assert_rejected(plan_route(graph, self.start, self.target, now=NOW), "no_path")

    def test_unverified_or_wrong_source_segment_rejected(self):
        for field, value in [("verified_continuous", False), ("source_sha256", "b" * 64),
                             ("map_hash", "b" * 64), ("evidence_id", ""),
                             ("source_kind", "covisibility")]:
            with self.subTest(field=field):
                frames = deepcopy(self.frames)
                frames["route_segments"][0][field] = value
                self.assertEqual(build_route_graph(self.manifest, self.refs, frames)["edges"], {})

    def test_nearby_across_wall_node_gets_no_shortcut(self):
        self.target["supporting_frame_ids"] = ["wall.png"]
        self.assert_rejected(self.plan(), "no_path")

    def test_reverse_not_assumed(self):
        self.start, self.target = inputs(self.graph, goal="z.png", start="m.png")
        self.assert_rejected(self.plan(), "no_path")

    def test_disabled_edge_excluded(self):
        edge = next(iter(self.graph["edges"]))
        self.assert_rejected(self.plan(disabled_edge_ids=[edge]), "no_path")
        frames = deepcopy(self.frames)
        frames["route_segments"][0]["enabled"] = False
        graph = build_route_graph(self.manifest, self.refs, frames)
        self.assert_rejected(plan_route(graph, self.start, self.target, now=NOW), "no_path")

    def test_multiple_goals_choose_reachable_then_shortest(self):
        self.target["supporting_frame_ids"] = ["wall.png", "m.png", "a.png"]
        self.assertEqual(self.plan()["goal_frame_id"], "a.png")
        self.target["supporting_frame_ids"] = ["wall.png", "m.png"]
        self.assertEqual(self.plan()["goal_frame_id"], "m.png")

    def test_target_insufficient_or_ambiguous_never_routes(self):
        for status in ["insufficient_evidence", "ambiguous", "localized"]:
            self.target["status"] = status
            self.assert_rejected(self.plan(), "target_not_supported")

    def test_unknown_target_frames_rejected(self):
        self.target["supporting_frame_ids"] = ["not-a-reference.png"]
        self.assert_rejected(self.plan(), "goal_not_supported")

    def test_map_and_component_mismatch(self):
        for owner in ("start", "target"):
            for field in ("map_id", "map_hash", "component_id"):
                with self.subTest(owner=owner, field=field):
                    self.start, self.target = inputs(self.graph)
                    getattr(self, owner)[field] = "different"
                    self.assert_rejected(self.plan(), field + "_mismatch")

    def test_component_boundary_cannot_be_crossed(self):
        self.manifest["components"] = [{"component_id": "c0", "image_ids": [1, 2]},
                                       {"component_id": "c1", "image_ids": [3, 4]}]
        seal(self.manifest)
        self.frames["route_segments"][0]["map_hash"] = self.manifest["manifest_sha256"]
        with self.assertRaisesRegex(ValueError, "component_selection_required"):
            build_route_graph(self.manifest, self.refs, self.frames)
        graph = build_route_graph(self.manifest, self.refs, self.frames, component_id="c0")
        self.assertNotIn("m.png", graph["nodes"])
        start, target = inputs(graph)
        self.assert_rejected(plan_route(graph, start, target, now=NOW), "goal_not_supported")

    def test_manual_node_requires_explicit_manual_label_and_exact_pose(self):
        self.start["manual"] = False
        self.assert_rejected(self.plan(), "manual_selection_required")
        self.start["manual"] = True
        self.start["T_MC"] = pose(0.0001)
        self.assert_rejected(self.plan(), "start_not_connected")

    def current(self, attached=False):
        start = {key: self.graph[key] for key in ("map_id", "map_hash", "component_id")}
        start.update(source="current_photo", query_id="current", photo_sha256="c" * 64,
                     pose_status="localized", T_MC=pose(-1), captured_at=CAPTURED,
                     localized_at=LOCALIZED, quality={"num_inliers": 20})
        if attached:
            start["attachment"] = {**{key: start[key] for key in
                ("map_id", "map_hash", "component_id", "query_id", "photo_sha256", "captured_at")},
                "kind": "verified_temporal_connection", "verified_continuous": True,
                "source_kind": "synthetic_test", "evidence_id": "synthetic-attachment",
                "source_sha256": "e" * 64, "pts_time_s": [0, .25, .5],
                "localized_at": LOCALIZED,
                "node_id": "z.png", "units": "map_units",
                "path": [[-1, 0, 0], [-1, 1, 0], [0, 0, 0]]}
        return start

    def test_current_pose_not_nearest_node_snapped(self):
        self.start = self.current()
        self.start["T_MC"] = pose(0)
        self.assert_rejected(self.plan(), "start_not_connected")

    def test_explicit_attachment_included_with_evidence_and_length(self):
        self.start = self.current(attached=True)
        result = self.plan()
        self.assertEqual(result["status"], "route_preview_only")
        self.assertEqual(result["path"][:3], [[-1, 0, 0], [-1, 1, 0], [0, 0, 0]])
        self.assertAlmostEqual(result["length"], 5 + 2 ** .5)
        self.assertEqual(result["start_times"]["captured_at"], CAPTURED)
        self.assertEqual(result["start_quality"], {"num_inliers": 20})
        self.assertEqual(result["connectivity_provenance"]["start_attachment"]["evidence_id"],
                         "synthetic-attachment")

    def test_attachment_cannot_reuse_another_query_or_invent_endpoints(self):
        for key, value in [("query_id", "old"), ("photo_sha256", "d" * 64),
                           ("captured_at", NOW), ("map_hash", "d" * 64),
                           ("path", [[-1, 0, 0], [0.01, 0, 0]]),
                           ("units", "m"), ("verified_continuous", False)]:
            with self.subTest(key=key):
                self.start = self.current(attached=True)
                self.start["attachment"][key] = value
                self.assert_rejected(self.plan(), "start_not_connected")

    def test_failed_current_localization_rejected_even_if_old_pose_present(self):
        self.start = self.current(True)
        self.start["pose_status"] = "insufficient_evidence"
        self.assert_rejected(self.plan(), "start_localization_failed")

    def test_stale_future_missing_and_reordered_timestamps_rejected(self):
        for key, value, reason in [
            ("captured_at", "2026-10-12T10:00:00Z", "stale_start"),
            ("captured_at", "2026-10-14T10:00:00Z", "invalid_start_time"),
            ("localized_at", "2026-10-13T09:59:54Z", "invalid_start_time"),
            ("captured_at", None, "invalid_start_time"),
            ("localized_at", "2026-10-13T09:59:58", "invalid_start_time")]:
            with self.subTest(key=key, value=value):
                self.start = self.current(True)
                self.start[key] = value
                self.assert_rejected(self.plan(), reason)

    def test_target_photo_cannot_be_current_photo(self):
        self.start = self.current(True)
        self.target["query_sha256"] = self.start["photo_sha256"]
        self.assert_rejected(self.plan(), "target_photo_is_not_current_pose")

    def test_target_roi_binding_hash_also_prevents_current_photo_reuse(self):
        self.start = self.current(True)
        self.target.pop("query_sha256")  # Legacy target with only its verified ROI binding.
        self.target["roi_binding"] = {"query_sha256": self.start["photo_sha256"]}
        self.assert_rejected(self.plan(), "target_photo_is_not_current_pose")

    def test_live_camera_disabled(self):
        self.start["source"] = "live_camera"
        self.assert_rejected(self.plan(), "unsupported_start_source")

    def test_arbitrary_scale_metric_extrinsics_rejected(self):
        self.assert_rejected(self.plan(T_BC=pose(.2)), "scale_mixing_not_permitted")

    def test_metric_conversion_and_missing_extrinsics_camera_only(self):
        self.manifest["scale_status"] = "metric"
        self.manifest["units"] = "m"
        seal(self.manifest)
        self.frames["route_segments"][0]["map_hash"] = self.manifest["manifest_sha256"]
        self.graph = build_route_graph(self.manifest, self.refs, self.frames)
        self.start, self.target = inputs(self.graph)
        result = self.plan(T_BC=pose(.2))
        self.assertEqual(result["start_base_pose"], pose(-.2))
        self.assertEqual(result["route_kind"], "camera_route")
        self.assertIsNone(self.plan()["start_base_pose"])

    def test_transform_order_with_rotation(self):
        mc, bc = np.asarray(pose(5, 2)), np.asarray(pose(.3, .4))
        mc[:2, :2] = [[0, -1], [1, 0]]
        bc[:2, :2] = [[0, 1], [-1, 0]]
        actual = camera_to_base_pose(mc, bc, scale_status="metric", units="m")
        np.testing.assert_allclose(actual, mc @ np.linalg.inv(bc), atol=1e-12)

    def test_bad_rigid_transforms_rejected(self):
        for bad in [np.zeros((4, 4)), np.diag([2, 1, 1, 1]),
                    np.diag([-1, 1, 1, 1]), np.full((4, 4), np.nan), [[1]]]:
            with self.subTest(bad=str(bad)):
                with self.assertRaisesRegex(ValueError, "invalid_rigid_transform"):
                    camera_to_base_pose(pose(), bad, scale_status="metric", units="m")

    def test_same_node_is_zero_length_not_missing_route(self):
        self.target["supporting_frame_ids"] = ["z.png"]
        result = self.plan()
        self.assertEqual(result["status"], "route_preview_only")
        self.assertEqual(result["path"], [[0, 0, 0]])
        self.assertEqual(result["length"], 0)

    def test_inputs_not_mutated(self):
        before = deepcopy((self.manifest, self.refs, self.frames, self.graph, self.start, self.target))
        result = self.plan()
        result["target_object_result"]["status"] = "modified"
        self.assertEqual(before, (self.manifest, self.refs, self.frames, self.graph, self.start, self.target))

    def test_attachment_requires_timed_samples_and_source_hash(self):
        for field, value in [("source_sha256", None), ("source_sha256", "x" * 64),
                             ("pts_time_s", None), ("pts_time_s", [0, 1]),
                             ("pts_time_s", [0, 1, 1]), ("pts_time_s", [0, 1, 10]),
                             ("localized_at", CAPTURED)]:
            with self.subTest(field=field, value=value):
                self.start = self.current(True)
                self.start["attachment"][field] = value
                self.assert_rejected(self.plan(), "start_not_connected")

    def test_null_attachment_is_rejected_not_crashed(self):
        self.start = self.current()
        self.start["attachment"] = None
        self.assert_rejected(self.plan(), "start_not_connected")

    def test_invalid_video_hash_not_provenance(self):
        self.frames["probe"]["source_sha256"] = "x" * 64
        self.frames["route_segments"][0]["source_sha256"] = "x" * 64
        self.assertEqual(build_route_graph(self.manifest, self.refs, self.frames)["edges"], {})

    def test_quality_rejection_breaks_path(self):
        for field, value in [("kept", False), ("quality", {"rejection_reasons": ["blur"]})]:
            frames = deepcopy(self.frames)
            frames["frames"][1][field] = value
            graph = build_route_graph(self.manifest, self.refs, frames)
            self.assert_rejected(plan_route(graph, self.start, self.target, now=NOW), "no_path")

    def test_missing_and_duplicate_pts_or_names_fail_closed(self):
        for change in ("missing", "duplicate_pts", "duplicate_name"):
            frames = deepcopy(self.frames)
            if change == "missing":
                frames["frames"][0].pop("pts_time_s")
            elif change == "duplicate_pts":
                frames["frames"][1]["pts_time_s"] = 0
            else:
                frames["frames"][1]["image_path"] = frames["frames"][0]["image_path"]
            graph = build_route_graph(self.manifest, self.refs, frames)
            self.assertEqual(graph["edges"], {})
            self.assertIn("invalid_frame_timeline", str(graph["diagnostics"]))

    def test_missing_frame_name_breaks_edge_not_coalesced(self):
        self.frames["frames"][1]["image_path"] = None
        graph = build_route_graph(self.manifest, self.refs, self.frames)
        self.assertEqual(graph["edges"], {})

    def test_same_length_goals_use_measured_observation_quality(self):
        self.refs["m.png"]["T_CW"] = self.refs["a.png"]["T_CW"]
        self.graph = build_route_graph(self.manifest, self.refs, self.frames)
        self.target["supporting_frame_ids"] = ["m.png", "a.png"]
        self.target["observation_quality"] = {"m.png": 40, "a.png": 5}
        result = self.plan()
        self.assertEqual(result["goal_frame_id"], "m.png")
        self.assertEqual(result["length"], 2)

    def test_graph_tamper_rejected_and_unknown_disabled_edge_reported(self):
        self.assert_rejected(self.plan(disabled_edge_ids=["missing"]), "unknown_disabled_edge")
        self.graph["edges"][next(iter(self.graph["edges"]))]["length"] = 0
        self.assert_rejected(self.plan(), "invalid_graph")

    def test_alias_localization_pose_and_object_status(self):
        self.start = self.current(True)
        self.start["T_WC_query"] = self.start.pop("T_MC")
        self.target["object_status"] = self.target.pop("status")
        self.assertEqual(self.plan()["status"], "route_preview_only")

    def test_current_identity_quality_and_pose_required(self):
        for key, value, reason in [("query_id", "", "current_photo_evidence_missing"),
                                   ("photo_sha256", None, "current_photo_evidence_missing"),
                                   ("quality", {}, "current_photo_evidence_missing"),
                                   ("T_MC", None, "invalid_start_pose")]:
            self.start = self.current(True)
            self.start[key] = value
            self.assert_rejected(self.plan(), reason)

    def test_invalid_configuration_rejected(self):
        for max_gap in [0, -1, float("nan"), True]:
            with self.assertRaises(ValueError):
                build_route_graph(self.manifest, self.refs, self.frames, max_gap_s=max_gap)
        with self.assertRaisesRegex(ValueError, "invalid_max_start_age_s"):
            self.plan(max_start_age_s=-1)
        with self.assertRaises(ValueError):
            plan_route(self.graph, self.start, self.target, now="no time")
        with self.assertRaisesRegex(ValueError, "unknown_component"):
            build_route_graph(self.manifest, self.refs, self.frames, component_id="missing")

    def test_invalid_manifest_component_and_units_rejected(self):
        for patch, message in [({"map_id": ""}, "map_id_required"),
            ({"units": "m"}, "inconsistent_scale_units"),
            ({"components": [{"component_id": "c0", "image_ids": [1]},
                              {"component_id": "c0", "image_ids": [2]}]}, "duplicate_component_id"),
            ({"components": [{"component_id": "c0", "image_ids": [1]},
                              {"component_id": "c1", "image_ids": [1]}]}, "image_in_multiple_components")]:
            manifest = seal({**self.manifest, **patch})
            with self.assertRaisesRegex(ValueError, message):
                build_route_graph(manifest, self.refs, self.frames, component_id="c0")

    def test_duplicate_segment_ids_and_empty_names(self):
        self.frames["route_segments"].append(deepcopy(self.frames["route_segments"][0]))
        with self.assertRaisesRegex(ValueError, "invalid_or_duplicate_segment_id"):
            build_route_graph(self.manifest, self.refs, self.frames)
        self.frames["route_segments"].pop()
        self.frames["route_segments"][0]["frame_names"] = []
        self.assertEqual(build_route_graph(self.manifest, self.refs, self.frames)["edges"], {})


if __name__ == "__main__":
    unittest.main()
