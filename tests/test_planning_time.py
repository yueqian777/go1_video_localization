"""Synthetic contract tests; no video/robot acceptance is implied."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from PIL import Image

import test_planning as fixtures
from go1loc.planning import build_route_graph, plan_route
from go1loc.planning import import_current_start, validate_start_connection
from go1loc.evidence import make_binding, verify_binding
from go1loc.query_geometry import prepare_query


RUN = "2026-10-13T10:00:00Z"
CAPTURE = "2020-01-01T10:00:00Z"
REPLAY = "2020-01-01T10:00:05Z"
LOCALIZED = "2026-10-13T09:59:58Z"


def current_photo(graph):
    start = {k: graph[k] for k in ("map_id", "map_hash", "component_id")}
    start.update(source="current_photo", pose_status="localized", query_id="independent.jpg",
                 photo_sha256="c" * 64, T_MC=fixtures.pose(-1), captured_at=CAPTURE,
                 localized_at=LOCALIZED, quality={"num_inliers": 42})
    start["attachment"] = {**{k: start[k] for k in
        ("map_id", "map_hash", "component_id", "query_id", "photo_sha256", "captured_at", "localized_at")},
        "kind": "verified_temporal_connection", "verified_continuous": True,
        "source_kind": "synthetic_test", "source_sha256": "e" * 64,
        "evidence_id": "synthetic-only", "node_id": "z.png", "units": "map_units",
        "T_MC": deepcopy(start["T_MC"]), "path": [[-1, 0, 0], [0, 0, 0]], "pts_time_s": [0, .5]}
    return start


class PlanningTimeTests(unittest.TestCase):
    def setUp(self):
        self.graph = build_route_graph(*fixtures.fixture())
        self.manual, self.target = fixtures.inputs(self.graph)
        self.start = current_photo(self.graph)

    def replay(self, **kwargs):
        return plan_route(self.graph, self.start, self.target, now=RUN,
                          time_mode="offline_replay", replay_reference_time=REPLAY, **kwargs)

    def test_replay_preserves_capture_computation_and_actual_runtime(self):
        before = deepcopy(self.start)
        result = self.replay()
        self.assertEqual(result["status"], "route_preview_only")
        self.assertEqual(result["time_mode"], "offline_replay")
        self.assertEqual(result["replay_reference_time"], REPLAY)
        self.assertEqual(result["planned_at"], RUN)
        self.assertEqual(result["run_at"], RUN)
        self.assertEqual(result["start_times"]["captured_at"], CAPTURE)
        self.assertEqual(result["start_times"]["localized_at"], LOCALIZED)
        self.assertEqual(before, self.start)
        self.assertFalse(result["execution_permitted"])

    def test_live_default_still_rejects_historical_photo(self):
        result = plan_route(self.graph, self.start, self.target, now=RUN)
        self.assertEqual(result["failure_reason"], "stale_start")
        self.assertEqual(result["time_mode"], "live_current")
        self.assertIsNone(result["replay_reference_time"])

    def test_replay_freshness_and_capture_order(self):
        self.assertEqual(self.replay(max_start_age_s=4)["failure_reason"], "stale_start")
        self.assertEqual(self.replay(max_start_age_s=5)["status"], "route_preview_only")
        self.start["captured_at"] = "2020-01-01T10:00:06Z"
        self.assertEqual(self.replay()["failure_reason"], "invalid_start_time")

    def test_localization_may_follow_replay_but_not_runtime_or_precede_capture(self):
        for timestamp in ("2026-10-13T10:00:01Z", "2019-12-31T10:00:00Z"):
            with self.subTest(timestamp=timestamp):
                self.start["localized_at"] = timestamp
                self.assertEqual(self.replay()["failure_reason"], "invalid_start_time")

    def test_bad_time_modes_and_reference_rejected(self):
        for mode, reference in [("other", None), ("offline_replay", None),
                                ("offline_replay", "2026-10-14T00:00:00Z"),
                                ("offline_replay", "2020-01-01T10:00:05"),
                                ("live_current", REPLAY)]:
            with self.subTest(mode=mode, reference=reference), self.assertRaises(ValueError):
                plan_route(self.graph, self.start, self.target, now=RUN,
                           time_mode=mode, replay_reference_time=reference)

    def test_manual_node_selection_uses_actual_selection_time_not_photo_time(self):
        result = plan_route(self.graph, self.manual, self.target, now=RUN,
                            time_mode="offline_replay", replay_reference_time=REPLAY)
        self.assertEqual(result["status"], "route_preview_only")
        self.manual["selected_at"] = REPLAY
        self.assertEqual(plan_route(self.graph, self.manual, self.target, now=RUN,
            time_mode="offline_replay", replay_reference_time=REPLAY)["failure_reason"], "stale_start")

    def test_replay_does_not_supply_missing_connection_or_target_independence(self):
        self.start.pop("attachment")
        self.assertEqual(self.replay()["failure_reason"], "start_not_connected")
        self.start = current_photo(self.graph)
        self.target["query_sha256"] = self.start["photo_sha256"]
        self.assertEqual(self.replay()["failure_reason"], "target_photo_is_not_current_pose")


class StartImportTests(unittest.TestCase):
    def setUp(self):
        self.graph = build_route_graph(*fixtures.fixture())
        self.full = current_photo(self.graph)
        self.current = deepcopy(self.full)
        self.current.pop("attachment")
        self.clock = dict(now=RUN, time_mode="offline_replay", replay_reference_time=REPLAY)

    def test_preview_is_same_gate_as_planning_and_has_specific_missing_fields(self):
        missing = validate_start_connection(self.graph, self.current, **self.clock)
        self.assertFalse(missing["valid"])
        self.assertIn("attachment", missing["missing"])
        self.assertEqual(missing["path"], [])
        result = validate_start_connection(self.graph, self.full, **self.clock)
        self.assertTrue(result["valid"])
        self.assertEqual(result["node_id"], "z.png")
        self.assertEqual(result["evidence"]["evidence_id"], "synthetic-only")
        self.assertFalse(result["execution_permitted"])

    def test_full_and_attachment_only_import_return_copy_without_mutation(self):
        before = deepcopy((self.current, self.full, self.graph))
        for document in (self.full, self.full["attachment"], {"attachment": self.full["attachment"]}):
            result = import_current_start(self.current, document, self.graph, **self.clock)
            self.assertTrue(result["valid"], result)
            self.assertEqual(result["start"], self.full)
            result["start"]["quality"]["num_inliers"] = -1
        self.assertEqual(before, (self.current, self.full, self.graph))

    def test_import_binding_mismatch_never_overwrites_current(self):
        changes = {"map_id": "other", "map_hash": "b" * 64, "component_id": "other",
                   "query_id": "other", "photo_sha256": "b" * 64,
                   "captured_at": REPLAY, "localized_at": RUN, "T_MC": fixtures.pose(9)}
        for key, value in changes.items():
            for attachment_only in (True, False):
                with self.subTest(key=key, attachment_only=attachment_only):
                    document = deepcopy(self.full["attachment"] if attachment_only else self.full)
                    document[key] = value
                    before = deepcopy(self.current)
                    result = import_current_start(self.current, document, self.graph, **self.clock)
                    self.assertFalse(result["valid"])
                    self.assertIsNone(result["start"])
                    self.assertEqual(self.current, before)

    def test_attachment_only_requires_full_pose_binding_including_rotation(self):
        document = deepcopy(self.full["attachment"])
        document.pop("T_MC")
        result = import_current_start(self.current, document, self.graph, **self.clock)
        self.assertFalse(result["valid"])
        self.assertIn("T_MC", result["missing"])
        document["T_MC"] = fixtures.pose(-1)
        document["T_MC"][0][0] = document["T_MC"][1][1] = -1
        self.assertFalse(import_current_start(self.current, document, self.graph, **self.clock)["valid"])

    def test_full_start_pose_can_bind_legacy_attachment_without_pose(self):
        document = deepcopy(self.full)
        document["attachment"].pop("T_MC")
        self.assertTrue(import_current_start(self.current, document, self.graph, **self.clock)["valid"])

    def test_unverified_current_cannot_be_replaced_by_import(self):
        self.current["pose_status"] = "failed"
        result = import_current_start(self.current, self.full, self.graph, **self.clock)
        self.assertFalse(result["valid"])
        self.assertIn("start_localization_failed", result["reasons"])

    def test_full_import_cannot_replace_quality_or_conflict_with_legacy_pose_alias(self):
        for field, value in [("quality", {"num_inliers": 999}), ("source", "explicit_pose"),
                             ("pose_status", "failed"), ("T_WC_query", fixtures.pose(9))]:
            with self.subTest(field=field):
                imported = deepcopy(self.full)
                imported[field] = value
                self.assertFalse(import_current_start(self.current, imported, self.graph, **self.clock)["valid"])

    def test_manual_start_cannot_be_import_authority(self):
        manual = fixtures.inputs(self.graph)[0]
        result = import_current_start(manual, self.full, self.graph, **self.clock)
        self.assertFalse(result["valid"])
        self.assertIsNone(result["start"])

    def test_attachment_missing_fields_and_invalid_flags_are_explained(self):
        broken = deepcopy(self.full)
        broken["attachment"].pop("source_sha256")
        broken["attachment"]["verified_continuous"] = False
        result = validate_start_connection(self.graph, broken, **self.clock)
        self.assertIn("attachment.source_sha256", result["missing"])
        self.assertIn("attachment_continuity_not_verified", result["reasons"])

    def test_bad_graph_target_and_geometry_fail_closed(self):
        bad_graph = deepcopy(self.graph)
        bad_graph["map_hash"] = "b" * 64
        self.assertFalse(validate_start_connection(bad_graph, self.full, **self.clock)["valid"])
        target = fixtures.inputs(self.graph)[1]
        target["query_sha256"] = self.current["photo_sha256"]
        self.assertFalse(validate_start_connection(self.graph, self.full, target=target, **self.clock)["valid"])
        for key, value in [("path", [[-1, 0, 0], [9, 9, 9]]), ("pts_time_s", [0, 9]),
                           ("verified_continuous", False), ("source_sha256", "bad")]:
            with self.subTest(key=key):
                document = deepcopy(self.full)
                document["attachment"][key] = value
                self.assertFalse(import_current_start(self.current, document, self.graph, **self.clock)["valid"])


class OriginalPhotoIdentityTests(unittest.TestCase):
    """Real file hashes/rectification, synthetic poses and route: NOT route acceptance."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.photos = []
        for source, color in (("target_capture", "red"), ("current_capture", "blue")):
            directory = self.root/source
            directory.mkdir()
            photo = directory/"image.jpg"
            Image.new("RGB", (40, 30), color).save(photo)
            self.photos.append(photo)
        self.calibration = self.root/"calibration.json"
        self.calibration.write_text(json.dumps({"source": "synthetic_identity_test_only",
            "model": "pinhole", "image_size": [40, 30], "output_size": [40, 30],
            "map_version": "test", "K": [[30, 0, 20], [0, 30, 15], [0, 0, 1]],
            "distortion": [0, 0, 0, 0]}))
        self.graph = build_route_graph(*fixtures.fixture())
        self.clock = dict(now=RUN, time_mode="offline_replay", replay_reference_time=REPLAY)

    def binding(self, photo, folder, calibrated=False):
        processed, geometry, _ = prepare_query(photo, self.root/folder,
                                               self.calibration if calibrated else None)
        binding = make_binding(photo, {"manifest_sha256": self.graph["map_hash"]},
                               {"processed_query": processed}, geometry)
        verify_binding(binding, photo, self.graph["map_hash"])
        return processed.name, binding

    def documents(self, *, current_calibrated=False, target_calibrated=False):
        target_key, target_binding = self.binding(self.photos[0], "target_work", target_calibrated)
        current_key, current_binding = self.binding(self.photos[1], "current_work", current_calibrated)
        start = current_photo(self.graph)
        start.update(query_id=current_key, query_sha256=current_binding["query_sha256"],
                     photo_sha256=current_binding["query_sha256"], query_path=str(self.photos[1]))
        start["attachment"].update(query_id=current_key, photo_sha256=start["photo_sha256"])
        target = fixtures.inputs(self.graph)[1]
        target.update(query_id=self.photos[0].name, query_path=str(self.photos[0]),
                      query_sha256=target_binding["query_sha256"],
                      roi_binding={"query_sha256": target_binding["query_sha256"]})
        return start, target, target_key

    def check(self, start, target, reason=None):
        before = deepcopy((start, target))
        result = plan_route(self.graph, start, target, **self.clock)
        self.assertFalse(result["execution_permitted"])
        self.assertEqual((start, target), before)
        self.assertEqual(result["failure_reason"], reason)
        self.assertEqual(result["status"], "rejected" if reason else "route_preview_only")
        if reason:
            self.assertEqual(result["path"], [])
        else:
            self.assertEqual(result["goal_frame_id"], "m.png")
        preview = validate_start_connection(self.graph, start, target=target, **self.clock)
        self.assertEqual(preview["valid"], reason is None)
        if reason:
            self.assertIn(reason, preview["reasons"])

    def test_same_basename_different_raw_hashes_are_independent(self):
        start, target, _ = self.documents()
        self.assertEqual(start["query_id"], target["query_id"])
        self.assertNotEqual(start["photo_sha256"], target["query_sha256"])
        self.check(start, target)

    def test_renamed_copy_same_raw_content_is_rejected(self):
        copy = self.photos[1].with_name("renamed.jpg")
        shutil.copyfile(self.photos[0], copy)
        self.photos[1] = copy
        start, target, _ = self.documents()
        self.assertNotEqual(start["query_id"], target["query_id"])
        self.check(start, target, "target_photo_is_not_current_pose")

    def test_different_processing_retains_common_original_identity(self):
        shutil.copyfile(self.photos[0], self.photos[1])
        start, target, _ = self.documents(current_calibrated=True)
        self.assertEqual(start["query_id"], "query_rectified.png")
        self.assertEqual(start["photo_sha256"], target["query_sha256"])
        self.check(start, target, "target_photo_is_not_current_pose")

    def test_distinct_originals_share_rectified_algorithm_key(self):
        # An original input can legitimately use the fixed processing basename.
        target_photo = self.photos[0].with_name("query_rectified.png")
        Image.new("RGB", (40, 30), "red").save(target_photo)
        self.photos[0] = target_photo
        start, target, target_key = self.documents(current_calibrated=True, target_calibrated=True)
        self.assertEqual(start["query_id"], target_key)
        self.assertEqual(start["query_id"], target["query_id"])
        self.check(start, target)

    def test_missing_invalid_or_conflicting_target_identity_fails_closed(self):
        start, target, _ = self.documents()
        start["query_id"] = start["attachment"]["query_id"] = "different-name.jpg"
        for update, reason in [
            ({}, "target_photo_evidence_missing"),
            ({"query_sha256": None}, "target_photo_identity_invalid"),
            ({"query_sha256": "not-a-hash"}, "target_photo_identity_invalid"),
            ({"query_sha256": "a"*64, "photo_sha256": "b"*64}, "target_photo_identity_conflict"),
            ({"query_sha256": "a"*64, "roi_binding": {"query_sha256": "b"*64}}, "target_photo_identity_conflict"),
            ({"query_sha256": "a"*64, "roi_binding": {}}, "target_photo_identity_invalid"),
            ({"query_sha256": "a"*64, "roi_binding": []}, "target_photo_identity_invalid"),
        ]:
            with self.subTest(update=update):
                candidate = {k: v for k, v in target.items() if k not in ("query_sha256", "roi_binding")}
                candidate.update(update)
                self.check(start, candidate, reason)

    def test_conflicting_current_original_aliases_are_not_independent(self):
        start, target, _ = self.documents()
        start["query_id"] = start["attachment"]["query_id"] = "different-name.jpg"
        start["query_sha256"] = target["query_sha256"]
        self.check(start, target, "current_photo_identity_conflict")

    def test_missing_or_invalid_current_hash_cannot_establish_independence(self):
        start, target, _ = self.documents()
        for field, value, reason in [
            ("photo_sha256", None, "current_photo_evidence_missing"),
            ("photo_sha256", "invalid", "current_photo_evidence_missing"),
            ("query_sha256", None, "current_photo_identity_invalid"),
            ("query_sha256", "invalid", "current_photo_identity_invalid"),
        ]:
            with self.subTest(field=field, value=value):
                candidate = deepcopy(start)
                candidate[field] = value
                self.check(candidate, target, reason)

    def test_legacy_roi_only_identity_is_checked_not_guessed(self):
        start, target, _ = self.documents()
        target.pop("query_sha256")
        self.check(start, target)
        target["roi_binding"]["query_sha256"] = start["photo_sha256"]
        self.check(start, target, "target_photo_is_not_current_pose")

    def test_same_basename_does_not_bypass_other_binding_gates(self):
        start, target, _ = self.documents()
        for field, value, reason in [
            ("map_hash", "f"*64, "map_hash_mismatch"),
            ("captured_at", "2019-01-01T00:00:00Z", "stale_start"),
            ("T_MC", fixtures.pose(5), "start_not_connected"),
            ("attachment", {}, "start_not_connected"),
        ]:
            with self.subTest(field=field):
                candidate = deepcopy(start)
                candidate[field] = value
                self.check(candidate, target, reason)


if __name__ == "__main__":
    unittest.main()
