"""Artificial timelines for review logic, NOT real continuous-video evidence."""
from copy import deepcopy
import unittest

import test_planning as fixtures
from go1loc.planning import build_route_graph
from go1loc.route_review import generate_route_candidates, review_route_candidate


class RouteReviewTests(unittest.TestCase):
    def setUp(self):
        self.manifest, self.refs, self.frames = fixtures.fixture()
        self.frames.pop("route_segments")
        # Explicit synthetic label must survive generation and review.
        self.frames["source_kind"] = "synthetic_test"
        self.args = dict(reviewer="test reviewer", evidence_note="synthetic fixture review only",
                         reviewed_at=fixtures.NOW)

    def generate(self):
        return generate_route_candidates(self.manifest, self.refs, self.frames)

    def test_candidates_are_pending_no_segments_or_automatic_approval(self):
        review = self.generate()
        self.assertEqual(len(review["candidates"]), 1)
        candidate = review["candidates"][0]
        self.assertEqual(candidate["status"], "PENDING")
        self.assertFalse(candidate["verified_continuous"])
        self.assertEqual(candidate["frame_names"], ["z.png", "a.png", "m.png", "wall.png"])
        self.assertEqual(review["route_segments"], [])
        self.assertFalse(review["execution_permitted"])

    def test_explicit_confirm_emits_compatible_segments_and_preserves_source(self):
        review = self.generate()
        before = deepcopy(review)
        approved = review_route_candidate(review, review["candidates"][0]["candidate_id"], "confirm", **self.args)
        self.assertEqual(review, before)
        segment = approved["route_segments"][0]
        self.assertTrue(segment["verified_continuous"])
        self.assertEqual(segment["source_kind"], "synthetic_test")
        self.assertEqual(segment["review"]["reviewer"], "test reviewer")
        graph = build_route_graph(self.manifest, self.refs, self.frames, route_segments=approved["route_segments"])
        self.assertEqual(len(graph["edges"]), 3)

    def test_gaps_cuts_quality_and_missing_registration_are_hard_barriers(self):
        for kind in ("gap", "cut", "quality", "unregistered", "missing_name"):
            with self.subTest(kind=kind):
                self.setUp()
                if kind == "gap":
                    for row in self.frames["frames"][2:]:
                        row["pts_time_s"] += 10
                elif kind == "cut":
                    self.frames["frames"][2]["discontinuity_before"] = True
                elif kind == "quality":
                    self.frames["frames"][1]["quality"]["rejection_reasons"] = ["blur"]
                elif kind == "unregistered":
                    self.refs.pop("a.png")
                else:
                    self.frames["frames"][1]["image_path"] = None
                review = self.generate()
                self.assertTrue(review["diagnostics"])
                self.assertFalse(any("z.png" in c["frame_names"] and "m.png" in c["frame_names"]
                                     for c in review["candidates"]))

    def test_rejected_frame_without_name_remains_visible_diagnostic(self):
        self.frames["frames"][1].update(image_path=None, kept=False)
        self.frames["frames"][1]["quality"]["rejection_reasons"] = ["blur"]
        review = self.generate()
        self.assertEqual(len(review["timeline"]), 4)
        self.assertIn("frame_quality_rejected", review["timeline"][1]["reasons"])

    def test_missing_video_hash_and_invalid_pts_produce_no_candidates(self):
        for change in ("hash", "pts", "duplicate"):
            with self.subTest(change=change):
                self.setUp()
                if change == "hash":
                    self.frames["probe"].pop("source_sha256")
                elif change == "pts":
                    self.frames["frames"][1].pop("pts_time_s")
                else:
                    self.frames["frames"][1]["pts_time_s"] = 0
                review = self.generate()
                self.assertEqual(review["candidates"], [])
                self.assertTrue(review["diagnostics"])

    def test_split_children_all_pending_and_no_cross_boundary_segment(self):
        review = self.generate()
        candidate = review["candidates"][0]
        split = review_route_candidate(review, candidate["candidate_id"], "split",
            first_name="m.png", last_name="wall.png", **self.args)
        self.assertEqual(split["route_segments"], [])
        children = [c for c in split["candidates"] if c["status"] == "PENDING"]
        self.assertEqual([c["frame_names"] for c in children], [["z.png", "a.png"], ["m.png", "wall.png"]])
        for child in children:
            split = review_route_candidate(split, child["candidate_id"], "confirm", **self.args)
        graph = build_route_graph(self.manifest, self.refs, self.frames, route_segments=split["route_segments"])
        self.assertEqual({(e["from"], e["to"]) for e in graph["edges"].values()},
                         {("z.png", "a.png"), ("m.png", "wall.png")})

    def test_reject_does_not_approve_and_cannot_silently_reapprove(self):
        review = self.generate()
        cid = review["candidates"][0]["candidate_id"]
        rejected = review_route_candidate(review, cid, "reject", **self.args)
        self.assertEqual(rejected["route_segments"], [])
        self.assertEqual(rejected["candidates"][0]["status"], "REJECTED")
        with self.assertRaisesRegex(ValueError, "candidate_not_pending"):
            review_route_candidate(rejected, cid, "confirm", **self.args)

    def test_partial_confirmation_unknown_range_and_empty_review_are_rejected(self):
        review = self.generate()
        cid = review["candidates"][0]["candidate_id"]
        for decision, kwargs in [("confirm", {"first_name": "a.png", "last_name": "wall.png"}),
                                 ("split", {"first_name": "m.png", "last_name": "z.png"}),
                                 ("split", {"first_name": "invented", "last_name": "wall.png"}),
                                 ("confirm", {"reviewer": " "}), ("confirm", {"evidence_note": ""}),
                                 ("confirm", {"reviewed_at": "2026-10-13"})]:
            with self.subTest(decision=decision, kwargs=kwargs), self.assertRaises(ValueError):
                review_route_candidate(review, cid, decision, **(self.args | kwargs))

    def test_review_document_changes_detected_and_multiple_components_not_joined(self):
        review = self.generate()
        review["candidates"][0]["frame_names"] = ["z.png", "wall.png"]
        with self.assertRaisesRegex(ValueError, "review_hash_mismatch"):
            review_route_candidate(review, review["candidates"][0]["candidate_id"], "confirm", **self.args)
        self.manifest["components"] = [{"component_id": "c0", "image_ids": [1, 2]},
                                       {"component_id": "c1", "image_ids": [3, 4]}]
        fixtures.seal(self.manifest)
        review = self.generate()
        self.assertEqual({c["component_id"] for c in review["candidates"]}, {"c0", "c1"})
        self.assertTrue(all(len(c["frame_names"]) == 2 for c in review["candidates"]))

    def test_decision_replay_reproduces_ids_and_full_document(self):
        review = self.generate()
        review = review_route_candidate(review, review["candidates"][0]["candidate_id"], "split",
            first_name="m.png", last_name="wall.png", **self.args)
        children = [c for c in review["candidates"] if c["status"] == "PENDING"]
        review = review_route_candidate(review, children[0]["candidate_id"], "confirm", **self.args)
        review = review_route_candidate(review, children[1]["candidate_id"], "reject", **self.args)
        restored = self.generate()
        for decision in review["decisions"]:
            restored = review_route_candidate(restored, **decision)
        self.assertEqual(restored, review)

    def test_embedded_frames_cannot_be_replaced_with_same_video_hash(self):
        self.manifest["frames_manifest"] = deepcopy(self.frames)
        fixtures.seal(self.manifest)
        self.frames["frames"][1]["pts_time_s"] += .01
        with self.assertRaisesRegex(ValueError, "frames_manifest_binding_mismatch"):
            self.generate()

    def test_bad_frame_shapes_fail_as_value_error_not_unhandled_attribute_error(self):
        self.frames["frames"][1] = None
        with self.assertRaises(ValueError):
            self.generate()


if __name__ == "__main__":
    unittest.main()
