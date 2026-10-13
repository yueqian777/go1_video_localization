"""CPU-only evidence orchestration and immutable-map regression tests.

Temporary PNGs/model bytes are synthetic fixtures, not reconstruction evidence.
The optional upstream test executes the actual checked-out hloc function body,
with only its I/O and estimator replaced; it does not import torch/pycolmap.
"""
from __future__ import annotations

import ast
from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from go1loc.evidence import digest_json, export_rows, make_binding, verify_binding, write_json
from go1loc.localization import LocalizationConfig
from go1loc.photo_object import locate_photo_object
from go1loc.schemas import LocalizationResult
from go1loc.video import sha256_file


class EvidenceWorkspace(unittest.TestCase):
    """Reusable isolated workspace; does not replace any production fixture."""

    def setUp(self):
        cache = ROOT / ".cache"
        cache.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="evidence-integration-", dir=cache)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.photo = self.root / "current.png"
        Image.new("RGB", (64, 64), (20, 30, 40)).save(self.photo)
        self.map_dir = self.root / "frozen-map"
        self.map_dir.mkdir()
        model_dir = self.map_dir / "sfm"
        model_dir.mkdir()
        (model_dir / "images.bin").write_bytes(b"synthetic-model-not-colmap")
        self.reference_features = self.map_dir / "reference-features.h5"
        self.reference_features.write_bytes(b"synthetic-reference-features")
        self.manifest = {
            "map_id": "synthetic-integration", "frozen": True,
            "model_dir": str(model_dir), "local_features": str(self.reference_features),
            "native_model_sha256": {"images.bin": sha256_file(model_dir / "images.bin")},
            "local_features_sha256": sha256_file(self.reference_features),
            "units": "map_units", "scale_status": "arbitrary_monocular_scale",
            "components": [{"component_id": "c0", "image_ids": [42, 7]}],
            "map_observation_time": "2020-01-01T00:00:00Z",
            "config": {"max_keypoints": 2048}, "retrieval_features": None,
        }
        self.manifest["manifest_sha256"] = digest_json(self.manifest)
        write_json(self.map_dir / "map_manifest.json", self.manifest)
        self.query_dir = self.root / "cached-query"
        self.query_dir.mkdir()
        self.query_features = self.query_dir / "query-local.h5"
        self.query_features.write_bytes(b"synthetic-query-features")
        self.matches = self.query_dir / "matches.h5"
        self.matches.write_bytes(b"synthetic-matches")
        self.processed = self.query_dir / "processed.png"
        self.processed.write_bytes(self.photo.read_bytes())
        self.calibration = self.root / "calibration.json"
        write_json(self.calibration, {"source": "synthetic_unit_test", "model": "pinhole",
            "image_size": [64, 64], "output_size": [64, 64], "K": [[30, 0, 32], [0, 30, 32], [0, 0, 1]],
            "distortion": [0, 0, 0, 0, 0], "map_version": "fixture-v1"})
        self._output_count = 0
        self.write_evidence()

    def output(self):
        self._output_count += 1
        return self.root / f"object-{self._output_count}"

    def rows(self):
        return [{"point3D_id": i + 100, "uv_original": [float(i + 1), 2.0],
                 "uv": [float(i + 1), 2.0], "xyz": [i * .1, 0, 1],
                 "pnp_inlier": True, "positive_depth": True, "reprojection_px": .2,
                 "references": [{"image_name": "reference.png", "image_id": 42,
                                  "reference_keypoint_index": i, "match_score": .9}]}
                for i in range(6)]

    def write_evidence(self, *, rows=None, status="localized", calibrated=False, mutate=None):
        geometry = {"status": "calibrated_rectified" if calibrated else "original",
                    "source_size": [64, 64], "output_size": [64, 64],
                    "roi_coordinate_space": "original_zero_based_pixel_centers"}
        if calibrated:
            geometry["calibration_sha256"] = sha256_file(self.calibration)
        files = {"query_features": self.query_features, "matches": self.matches,
                 "reference_features": self.reference_features, "processed_query": self.processed,
                 "calibration": self.calibration if calibrated else None}
        binding = make_binding(self.photo, self.manifest, files, geometry)
        evidence = {"schema_version": "go1loc.evidence.v1", "query_path": str(self.photo),
                    "map_id": self.manifest["map_id"], "map_hash": self.manifest["manifest_sha256"],
                    "component_id": "c0", "binding": binding, "pose_status": status,
                    "selected_cluster": 0, "capture_time": "2026-10-13T10:00:00Z",
                    "localization_time": "2026-10-13T10:00:01Z",
                    "clusters": [{"cluster_index": 0, "reference_image_ids": [42],
                                  "rows": self.rows() if rows is None else rows,
                                  "camera": {"model": "PINHOLE", "width": 64, "height": 64,
                                             "params": [30, 30, 32, 32]}}]}
        if mutate:
            mutate(evidence)
        evidence["evidence_sha256"] = digest_json(evidence)
        write_json(self.query_dir / "query_evidence.json", evidence)
        return evidence

    def snapshot_map(self):
        return {str(path.relative_to(self.map_dir)): path.read_bytes()
                for path in self.map_dir.rglob("*") if path.is_file()}

    def locate(self, **kwargs):
        options = {"roi": (0, 0, 64, 64), "query_dir": self.query_dir,
                   "cluster_radius": 1.0}
        options.update(kwargs)
        return locate_photo_object(self.photo, self.map_dir, self.output(), **options)


class EvidenceIntegrationTests(EvidenceWorkspace):
    def test_cached_evidence_to_object_persists_all_identity_and_original_roi_binding(self):
        before = self.snapshot_map()
        out = self.output()
        with patch("go1loc.localization.localize_photo", side_effect=AssertionError("cached means no backend")):
            result = locate_photo_object(self.photo, self.map_dir, out, (1, 1, 4, 3),
                                         query_dir=self.query_dir, cluster_radius=1)
        self.assertEqual(result["status"], "supported")
        self.assertEqual(result["supporting_point3D_ids"], [100, 101, 102, 103])
        self.assertEqual(result["query_sha256"], sha256_file(self.photo))
        self.assertEqual(result["map_hash"], self.manifest["manifest_sha256"])
        self.assertEqual(result["component_id"], "c0")
        self.assertEqual(result["capture_time"], "2026-10-13T10:00:00Z")
        self.assertEqual(result["roi_sha256"], digest_json(result["roi_binding"]))
        self.assertFalse(result["execution_permitted"])
        self.assertEqual(json.loads((out / "object_result.json").read_text(encoding="utf-8")), result)
        self.assertEqual(before, self.snapshot_map())

    def test_stale_same_path_photo_bytes_rejected_before_output(self):
        Image.new("RGB", (64, 64), (90, 80, 70)).save(self.photo)
        out = self.output()
        with self.assertRaisesRegex(ValueError, "evidence_query_hash_mismatch"):
            locate_photo_object(self.photo, self.map_dir, out, (0, 0, 64, 64), query_dir=self.query_dir)
        self.assertFalse(out.exists())

    def test_each_stale_feature_match_or_processed_file_rejected(self):
        for name, path in [("query_features", self.query_features), ("matches", self.matches),
                           ("processed_query", self.processed)]:
            with self.subTest(file=name):
                original = path.read_bytes()
                path.write_bytes(original + b"changed")
                with self.assertRaisesRegex(ValueError, "evidence_file_hash_mismatch:" + name):
                    self.locate()
                path.write_bytes(original)

    def test_changed_reference_features_invalidates_frozen_map(self):
        self.reference_features.write_bytes(b"different-reference-features")
        with self.assertRaisesRegex(ValueError, "map_local_features_hash_mismatch"):
            self.locate()

    def test_changed_calibration_binding_rejected_without_current_config(self):
        self.write_evidence(calibrated=True)
        self.calibration.write_bytes(self.calibration.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "evidence_file_hash_mismatch:calibration"):
            self.locate()

    def test_new_calibration_file_cannot_reuse_old_geometry(self):
        self.write_evidence(calibrated=True)
        alternate = self.root / "new-calibration.json"
        alternate.write_bytes(self.calibration.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "evidence_calibration_mismatch"):
            self.locate(config=LocalizationConfig(calibration_path=alternate))

    def test_stale_map_binding_rejected_even_when_outer_evidence_checksum_valid(self):
        self.write_evidence(mutate=lambda e: e["binding"].update(map_hash="a" * 64))
        with self.assertRaisesRegex(ValueError, "evidence_map_hash_mismatch"):
            self.locate()

    def test_corrupt_evidence_payload_rejected_before_grounding(self):
        path = self.query_dir / "query_evidence.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["clusters"][0]["rows"][0]["xyz"] = [100, 100, 100]
        write_json(path, payload)  # Deliberately do not reseal the digest.
        with self.assertRaisesRegex(ValueError, "evidence_content_hash_mismatch"):
            self.locate()

    def test_failed_ambiguous_or_unselected_evidence_never_supports(self):
        for status in ("insufficient_evidence", "ambiguous", "pipeline_error", "invalid_input"):
            with self.subTest(status=status):
                self.write_evidence(status=status)
                result = self.locate()
                self.assertEqual(result["status"], "insufficient_evidence")
                self.assertEqual(result["failure_reason"], "query_not_localized")
                self.assertFalse(result["execution_permitted"])
                self.assertFalse(result.get("supporting_point3D_ids"))
        self.write_evidence(mutate=lambda e: e.update(selected_cluster=None))
        self.assertEqual(self.locate()["failure_reason"], "no_selected_evidence")

    def test_fresh_localization_failure_without_evidence_stays_rejected(self):
        failure = LocalizationResult.failure(query_id=self.photo.name, map_id=self.manifest["map_id"],
                                             reason="insufficient_pnp_evidence")
        before = self.snapshot_map()
        with patch("go1loc.localization.localize_photo", return_value=failure) as backend:
            result = self.locate(query_dir=None)
        self.assertEqual(backend.call_count, 1)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertFalse(result["execution_permitted"])
        self.assertEqual(before, self.snapshot_map())

    def test_map_output_guard_precedes_even_rejected_localization(self):
        before = self.snapshot_map()
        with patch("go1loc.localization.localize_photo") as backend:
            for output in (self.map_dir, self.map_dir / "new-rejected-output"):
                with self.assertRaisesRegex(ValueError, "object_output_overlaps_frozen_map"):
                    locate_photo_object(self.photo, self.map_dir, output, (0, 0, 64, 64))
            backend.assert_not_called()
        self.assertEqual(before, self.snapshot_map())

    def test_preexisting_object_output_never_overwritten(self):
        out = self.output()
        out.mkdir()
        prior = out / "object_result.json"
        prior.write_bytes(b"user-owned-result")
        with self.assertRaisesRegex(ValueError, "object_output_not_empty"):
            locate_photo_object(self.photo, self.map_dir, out, (0, 0, 64, 64), query_dir=self.query_dir)
        self.assertEqual(prior.read_bytes(), b"user-owned-result")

    def test_invalid_roi_bounds_do_not_create_output_or_call_backend(self):
        rois = [(-1, 0, 64, 64), (0, 0, 65, 64), (0, 0, 64, 65),
                (1, 1, 1, 2), (2, 2, 1, 1), (0, float("nan"), 5, 5), (0, 0, 2)]
        with patch("go1loc.localization.localize_photo") as backend:
            for roi in rois:
                out = self.output()
                with self.subTest(roi=roi), self.assertRaisesRegex(ValueError, "invalid_roi"):
                    locate_photo_object(self.photo, self.map_dir, out, roi)
                self.assertFalse(out.exists())
            backend.assert_not_called()

    def test_mask_size_check_and_original_pixel_centers(self):
        mask_path = self.root / "mask.png"
        Image.new("L", (63, 64), 255).save(mask_path)
        with self.assertRaisesRegex(ValueError, "mask_original_image_size_mismatch"):
            self.locate(mask_path=mask_path)
        mask = np.zeros((64, 64), np.uint8)
        mask[2, 1:4] = 255
        Image.fromarray(mask).save(mask_path)
        result = self.locate(mask_path=mask_path)
        self.assertEqual(result["supporting_point3D_ids"], [100, 101, 102])
        self.assertEqual(result["roi_binding"]["mask_sha256"], sha256_file(mask_path))
        Image.new("L", (64, 64), 0).save(mask_path)
        self.assertEqual(self.locate(mask_path=mask_path)["status"], "insufficient_evidence")

    def test_mask_nearest_pixel_rounding_and_valid_image_corners(self):
        rows = self.rows()[:3]
        for row in rows:
            row["uv_original"] = [0.49, 0.49]
        self.write_evidence(rows=rows)
        mask = np.zeros((64, 64), np.uint8)
        mask[0, 0] = 255
        mask_path = self.root / "corner-mask.png"
        Image.fromarray(mask).save(mask_path)
        self.assertEqual(self.locate(mask_path=mask_path)["status"], "supported")
        for row in rows:
            row["uv_original"] = [0.5, 0.5]
        self.write_evidence(rows=rows)
        self.assertEqual(self.locate(mask_path=mask_path)["status"], "insufficient_evidence")
        for row in rows:
            row["uv_original"] = [63.0, 63.0]
        self.write_evidence(rows=rows)
        mask[63, 63] = 255
        Image.fromarray(mask).save(mask_path)
        self.assertEqual(self.locate(mask_path=mask_path)["status"], "supported")

    def test_rows_need_inlier_positive_depth_low_reprojection_and_finite_xyz(self):
        for changes in ({"pnp_inlier": False}, {"positive_depth": False},
                        {"reprojection_px": None}, {"reprojection_px": 4.01},
                        {"xyz": [1, 2]}, {"references": []}):
            with self.subTest(changes=changes):
                rows = self.rows()
                for row in rows:
                    row.update(changes)
                self.write_evidence(rows=rows)
                self.assertNotEqual(self.locate()["status"], "supported")

    def test_duplicate_map_ids_cannot_inflate_support(self):
        self.write_evidence(rows=[self.rows()[0]] * 12)
        self.assertEqual(self.locate()["status"], "insufficient_evidence")

    def test_two_spatial_objects_are_ambiguous_not_one_supported_center(self):
        rows = self.rows()
        for row in rows[3:]:
            row["xyz"][0] += 100
        self.write_evidence(rows=rows)
        result = self.locate()
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertIsNone(result["object_surface_position"])

    def test_cached_query_must_reject_changed_explicit_intrinsics(self):
        # A current caller's requested camera is not the cached 30px-focal camera.
        # This is a safety regression, not an invitation to lower quality gates.
        config = LocalizationConfig(query_camera_model="PINHOLE", query_camera_params=[300, 300, 32, 32])
        with self.assertRaises(ValueError):
            self.locate(config=config)

    def test_geometry_hash_is_checked_by_binding_validator(self):
        evidence = self.write_evidence()
        binding = deepcopy(evidence["binding"])
        binding["geometry"]["source_size"] = [128, 128]
        with self.assertRaisesRegex(ValueError, "geometry"):
            verify_binding(binding, self.photo, self.manifest["manifest_sha256"])

    def test_roi_full_image_must_not_accept_pixel_centers_outside_image(self):
        rows = self.rows()[:3]
        for index, row in enumerate(rows):
            row["uv_original"] = [64.0, float(index + 1)]
        self.write_evidence(rows=rows)
        self.assertNotEqual(self.locate()["status"], "supported")


class ActualHlocAlignmentTests(unittest.TestCase):
    def test_actual_pose_from_cluster_to_exporter_repeated_keypoint_and_db_offsets(self):
        upstream = ROOT / ".upstream/Hierarchical-Localization/hloc/localize_sfm.py"
        if not upstream.is_file():
            self.skipTest("Pinned hloc checkout absent; no source downloaded by this test")
        # Compile the real, unmodified function, not a hand-written log imitation.
        tree = ast.parse(upstream.read_text(encoding="utf-8"), filename=str(upstream))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "pose_from_cluster")
        header = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
        module = ast.fix_missing_locations(ast.Module(body=[header, function], type_ignores=[]))
        point = lambda pid: NS(point3D_id=pid, has_point3D=lambda: pid >= 0)
        model = NS(images={
            42: NS(name="first.png", num_points3D=2, points2D=[point(100), point(200), point(-1)]),
            7: NS(name="second.png", num_points3D=2, points2D=[point(200), point(300)])},
            points3D={100: NS(xyz=np.array([1.5, 2.5, 1.])),
                      200: NS(xyz=np.array([3.5, 4.5, 1.])),
                      300: NS(xyz=np.array([3.5, 4.5, 1.]))})
        kp = np.zeros((18, 2))
        kp[2], kp[17], kp[4] = [1, 2], [3, 4], [20, 20]
        pair_data = {
            "first.png": (np.array([[17, 1], [4, 2], [2, 0]]), np.array([.91, .01, .72])),
            "second.png": (np.array([[17, 1], [17, 0]]), np.array([.83, .64])),
        }
        pose = NS(rotation=NS(matrix=lambda: np.eye(3)), translation=np.zeros(3))
        ret = {"cam_from_world": pose, "inlier_mask": [True, False, True]}
        localize = Mock(return_value=ret)
        localizer = NS(reconstruction=model, localize=localize)
        env = {"np": np, "defaultdict": defaultdict, "logger": NS(debug=Mock()),
               "get_keypoints": lambda *_: kp.copy(),
               "get_matches": lambda _p, _q, name: pair_data[name]}
        exec(compile(module, str(upstream), "exec"), env)
        camera = NS(img_from_cam=lambda xyz: xyz[:, :2] / xyz[:, 2:])
        result, log = env["pose_from_cluster"](localizer, "query.png", camera, [42, 7], Path("features"), Path("matches"))
        self.assertEqual(log["points3D_ids"], [200, 300, 100])
        self.assertEqual(log["keypoint_index_to_db"][0], [17, 17, 2])
        self.assertEqual(log["num_matches"], 4)  # unmapped reference point is excluded
        rows = export_rows(model, log, result, camera, lambda name: pair_data[name])
        self.assertEqual([r["point3D_id"] for r in rows], [200, 300, 100])
        self.assertEqual([r["pnp_inlier"] for r in rows], [True, False, True])
        self.assertEqual(rows[0]["uv"], [3, 4])
        self.assertEqual(rows[0]["uv_colmap"], [3.5, 4.5])
        self.assertEqual([r["image_id"] for r in rows[0]["references"]], [42, 7])
        self.assertEqual([r["reference_keypoint_index"] for r in rows[0]["references"]], [1, 0])
        self.assertEqual([r["match_score"] for r in rows[0]["references"]], [.91, .64])
        self.assertEqual(rows[1]["references"][0]["match_score"], .83)
        self.assertEqual(rows[2]["references"][0]["match_score"], .72)
        self.assertTrue(all(row["reprojection_px"] == 0 for row in rows))


if __name__ == "__main__":
    unittest.main()
