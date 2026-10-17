"""Backend-independent localization orchestration; never opens a GPU backend."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_evidence_integration import EvidenceWorkspace
from go1loc.evidence import digest_json, verify_binding, write_json
from go1loc.localization import LocalizationConfig, localize_photo
from go1loc.photo_object import locate_photo_object
from go1loc.pipeline_errors import PipelineInputError
from go1loc.video import sha256_file


class FakeCamera:
    def __init__(self, data=None, **kwargs):
        values = data if data is not None else kwargs
        self.model = NS(name=values.get("model", "PINHOLE"))
        self.width, self.height = values.get("width", 64), values.get("height", 64)
        self.params = np.asarray(values.get("params", [10, 10, 0, 0]), dtype=float).copy()

    def todict(self):
        return {"model": self.model.name, "width": self.width, "height": self.height,
                "params": self.params.tolist()}

    def verify_params(self):
        return len(self.params) == 4 and bool(np.isfinite(self.params).all()) and bool(np.all(self.params[:2] > 0))

    def img_from_cam(self, xyz):
        fx, fy, cx, cy = self.params
        return xyz[:, :2] / xyz[:, 2:] * [fx, fy] + [cx, cy]


class FakeBackend:
    def __init__(self, *, inliers=6, clusters=None, fail_stage=None):
        self.inliers = inliers
        self.clusters = clusters or [[42, 7]]
        self.fail_stage = fail_stage
        self.bad_mask = False
        self.camera_shift = 0
        self.cluster_cameras = []
        self.created_feature_confs = []
        point = lambda pid: NS(point3D_id=pid, has_point3D=lambda: True)
        self.images = {image_id: NS(image_id=image_id, name=name,
                                   points2D=[point(100 + i) for i in range(6)])
                       for image_id, name in [(42, "reference-a.png"), (7, "reference-b.png")]}
        self.xyz = {100 + i: NS(xyz=np.array([(4 + 4 * i + .5) / 10, 16.5 / 10, 1]))
                    for i in range(6)}
        self.model = NS(images=self.images, points3D=self.xyz,
                        find_image_with_name=lambda name: next((im for im in self.images.values() if im.name == name), None))
        self.reconstruction = Mock(return_value=self.model)
        self.pycolmap = NS(Reconstruction=self.reconstruction, Camera=FakeCamera,
                           infer_camera_from_image=Mock(return_value=FakeCamera()))
        self.extractor = NS(confs={"superpoint_aachen": {"output": "feats-superpoint-n4096-r1024",
                                        "model": {"max_keypoints": 4096}},
                                   "netvlad": {"output": "global-fixture"}}, main=self.extract)
        self.matcher = NS(confs={"superpoint+lightglue": {"output": "matches-fixture"}}, main=self.match)
        self.sfm = NS(parse_retrieval=self.parse_pairs,
                      do_covisibility_clustering=lambda ids, model: self.clusters,
                      QueryLocalizer=lambda model, config: NS(model=model, config=config),
                      pose_from_cluster=self.pose_from_cluster)
        self.retriever = NS(main=self.retrieve)

    def extract(self, config, image_dir, output_dir, **kwargs):
        self.created_feature_confs.append(config)
        if self.fail_stage == "extract":
            raise RuntimeError("synthetic_extraction_failure")
        kwargs["feature_path"].write_bytes(b"synthetic-query-features:" + str(config).encode())

    def match(self, config, pairs, **kwargs):
        if self.fail_stage == "match":
            raise RuntimeError("synthetic_matching_failure")
        kwargs["matches"].write_bytes(b"synthetic-query-matches")

    def retrieve(self, query_features, pairs_path, top_k, **kwargs):
        query = kwargs["query_list"][0]
        pairs_path.write_text(f"{query} reference-a.png\n", encoding="utf-8")

    def parse_pairs(self, pairs):
        result = {}
        for line in pairs.read_text(encoding="utf-8").splitlines():
            query, name = line.split()
            result.setdefault(query, []).append(name)
        return result

    def get_matches(self, path, query, name):
        # Reverse storage order to ensure the exporter really joins qidx/ridx.
        return np.array([[i, i] for i in reversed(range(6))]), np.array([.95 - .01 * i for i in reversed(range(6))])

    def pose_from_cluster(self, localizer, query, camera, cluster, features, matches):
        self.cluster_cameras.append(camera)
        camera.params[0] += self.camera_shift
        qidxs = [5, 0, 4, 1, 3, 2]
        log = {"db": cluster, "points3D_ids": [100 + q for q in qidxs],
               "keypoints_query": np.array([[4 + 4 * q + .5, 16.5] for q in qidxs]),
               "num_matches": 6 * len(cluster),
               "keypoint_index_to_db": (qidxs, [(100 + q, list(range(len(cluster)))) for q in qidxs])}
        if self.inliers is None:
            return None, log
        count = self.inliers if isinstance(self.inliers, int) else self.inliers[tuple(cluster)]
        pose = NS(rotation=NS(matrix=lambda: np.eye(3)), translation=np.zeros(3))
        ret = {"cam_from_world": pose, "inlier_mask": [i < count for i in range(6)], "num_inliers": count}
        if self.bad_mask:
            ret["inlier_mask"] = [True]
        return ret, log

    @contextmanager
    def installed(self):
        # Production imports this I/O entry during row export. Inject the module
        # only for this context and restore all module entries when finished.
        hloc, utils, io = ModuleType("hloc"), ModuleType("hloc.utils"), ModuleType("hloc.utils.io")
        hloc.__path__, utils.__path__ = [], []
        io.get_matches = self.get_matches
        backend = (self.pycolmap, self.extractor, self.sfm, self.matcher, self.retriever)
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"hloc": hloc, "hloc.utils": utils, "hloc.utils.io": io}))
            stack.enter_context(patch("go1loc.localization._require_backend", return_value=backend))
            stack.enter_context(patch("go1loc.mapping._ascii_stage", return_value=None))
            yield self


class LocalizationFlowTests(EvidenceWorkspace):
    def test_local_success_stops_speculative_gpu_before_serializing_evidence(self):
        from go1loc.evidence import make_binding
        for inliers in (6,2):
            with self.subTest(inliers=inliers):
                backend=FakeBackend(inliers=inliers);events=[]
                resident=NS(device='cuda',reconstruction=backend.model,
                    verified_manifest=lambda _:self.manifest,extract=backend.extract,match=backend.match,
                    prefetch_global_matches=lambda refs:events.append('prefetch'),
                    _finish_match_prefetch=lambda **kwargs:events.append(('cancel',kwargs['cancel'])))
                def bind(*args,**kwargs):
                    events.append('binding')
                    return make_binding(*args,**kwargs)
                with backend.installed(),patch('go1loc.localization.make_binding',side_effect=bind):
                    result=localize_photo(self.photo,self.map_dir,self.output(),resident=resident,
                        reference_names=[im.name for im in backend.images.values()])
                if result.pose_status=='localized':
                    self.assertEqual(events,['prefetch',('cancel',True),'binding'])
                else:
                    self.assertEqual(events,['prefetch','binding'])

    def localize(self, backend=None, *, config=None, output=None):
        backend = backend or FakeBackend()
        output = output or self.output()
        with backend.installed():
            result = localize_photo(self.photo, self.map_dir, output, config)
        return result, output, backend

    def assert_no_support(self, result):
        self.assertNotEqual(result["status"], "supported")
        self.assertFalse(result.get("supporting_point3D_ids"))
        self.assertFalse(result["execution_permitted"])

    def test_complete_localization_to_object_chain_real_serialization_and_hashes(self):
        before = self.snapshot_map()
        output = self.output()
        config = LocalizationConfig(capture_time="2026-10-13T09:59:55Z")
        with FakeBackend().installed():
            obj = locate_photo_object(self.photo, self.map_dir, output, (0, 0, 64, 64),
                                      cluster_radius=1, config=config)
        self.assertEqual(obj["status"], "supported")
        self.assertEqual(set(obj["supporting_point3D_ids"]), set(range(100, 106)))
        self.assertEqual(obj["supporting_frame_ids"], ["reference-a.png", "reference-b.png"])
        query_output = output / "query"
        result = json.loads((query_output / "localization_result.json").read_text(encoding="utf-8"))
        evidence = json.loads((query_output / "query_evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(result["pose_status"], "localized")
        self.assertEqual(result["map_hash"], self.manifest["manifest_sha256"])
        self.assertEqual(result["query_sha256"], sha256_file(self.photo))
        self.assertEqual(result["capture_time"], config.capture_time)
        self.assertTrue(result["localization_time"])
        self.assertIsNone(result["nav_goal"])
        self.assertEqual(result["quality"]["mean_inlier_reprojection_px"], 0)
        self.assertGreaterEqual(result["timing"]["total_s"], result["timing"]["pnp_and_evidence_s"])
        verify_binding(evidence["binding"], self.photo, self.manifest["manifest_sha256"])
        self.assertEqual(evidence["evidence_sha256"],
                         digest_json({k: v for k, v in evidence.items() if k != "evidence_sha256"}))
        self.assertEqual(evidence["clusters"][0]["rows"][0]["query_keypoint_index"], 5)
        self.assertEqual(evidence["clusters"][0]["rows"][0]["uv_original"], [24, 16])
        self.assertEqual(before, self.snapshot_map())

    def test_failed_pnp_persists_diagnostic_rows_but_never_supports_object(self):
        output = self.output()
        with FakeBackend(inliers=None).installed():
            result = locate_photo_object(self.photo, self.map_dir, output, (0, 0, 64, 64))
        self.assert_no_support(result)
        evidence = json.loads((output / "query/query_evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(evidence["pose_status"], "insufficient_evidence")
        self.assertIsNone(evidence["selected_cluster"])
        self.assertEqual(len(evidence["clusters"][0]["rows"]), 6)
        self.assertTrue(all(not r["pnp_inlier"] for r in evidence["clusters"][0]["rows"]))

    def test_quality_rejected_pose_with_selected_rows_cannot_support_object(self):
        output = self.output()
        with FakeBackend(inliers=3).installed():
            result = locate_photo_object(self.photo, self.map_dir, output, (0, 0, 64, 64))
        self.assert_no_support(result)
        evidence = json.loads((output / "query/query_evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(evidence["selected_cluster"], 0)
        self.assertEqual(evidence["failure_reason"], "insufficient_pnp_quality")

    def test_ambiguous_pose_with_two_valid_clusters_cannot_support_object(self):
        output = self.output()
        backend = FakeBackend(clusters=[[42], [7]])
        with backend.installed():
            result = locate_photo_object(self.photo, self.map_dir, output, (0, 0, 64, 64))
        self.assert_no_support(result)
        evidence = json.loads((output / "query/query_evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(evidence["pose_status"], "ambiguous")
        self.assertEqual(len(evidence["clusters"]), 2)

    def test_camera_refinement_isolated_per_cluster(self):
        backend = FakeBackend(clusters=[[42], [7]], inliers={(42,): 6, (7,): 3})
        backend.camera_shift = .01
        result, output, _ = self.localize(backend)
        self.assertEqual(result.pose_status, "localized")
        self.assertIsNot(backend.cluster_cameras[0], backend.cluster_cameras[1])
        self.assertEqual(backend.cluster_cameras[0].params[0], 10.01)
        self.assertEqual(backend.cluster_cameras[1].params[0], 10.01)
        self.assertEqual(backend.pycolmap.infer_camera_from_image.return_value.params[0], 10)

    def test_explicit_intrinsics_used_and_keypoint_budget_inherited_without_mutation(self):
        backend = FakeBackend()
        config = LocalizationConfig(query_camera_model="PINHOLE", query_camera_params=[10, 10, 0, 0])
        result, _, _ = self.localize(backend, config=config)
        self.assertEqual(result.quality["query_intrinsics"], "explicit")
        backend.pycolmap.infer_camera_from_image.assert_not_called()
        self.assertEqual(backend.created_feature_confs[0]["model"]["max_keypoints"], 2048)
        self.assertEqual(backend.extractor.confs["superpoint_aachen"]["model"]["max_keypoints"], 4096)

    def test_uncalibrated_query_estimates_focal_instead_of_fixing_initial_guess(self):
        backend = FakeBackend()
        backend.sfm.QueryLocalizer = Mock(side_effect=lambda model, config: NS(model=model, config=config))
        result, output, _ = self.localize(backend)
        options = backend.sfm.QueryLocalizer.call_args.args[1]
        self.assertTrue(options['estimation']['estimate_focal_length'])
        self.assertTrue(options['refinement']['refine_focal_length'])
        self.assertFalse(options['refinement']['refine_extra_params'])
        self.assertEqual(options['estimation']['ransac']['max_error'], 4.)
        self.assertEqual(result.quality['query_intrinsics'], 'self_estimated_not_calibrated')
        evidence = json.loads((output/'query_evidence.json').read_text(encoding='utf-8'))
        policy = evidence['binding']['geometry']['intrinsics_policy']
        self.assertEqual(policy['mode'], 'estimate_focal_from_2d3d')
        self.assertEqual(result.quality['query_camera']['params'], backend.cluster_cameras[0].params.tolist())
        self.assertEqual(result.quality['acceptance_thresholds']['min_inlier_ratio'], .25)

    def test_calibration_and_explicit_intrinsics_are_not_refined(self):
        for config in [LocalizationConfig(query_camera_model='PINHOLE', query_camera_params=[10, 10, 0, 0]),
                       LocalizationConfig(calibration_path=self.calibration)]:
            with self.subTest(config=config):
                backend = FakeBackend()
                backend.sfm.QueryLocalizer = Mock(side_effect=lambda model, config: NS(model=model, config=config))
                self.localize(backend, config=config)
                options = backend.sfm.QueryLocalizer.call_args.args[1]
                self.assertFalse(options['estimation']['estimate_focal_length'])
                self.assertFalse(options['refinement']['refine_focal_length'])
                self.assertFalse(options['refinement']['refine_extra_params'])

    def test_nonfinite_estimated_camera_never_emits_success_evidence(self):
        backend = FakeBackend()
        backend.camera_shift = float('nan')
        output = self.output()
        with self.assertRaisesRegex(PipelineInputError, 'invalid_estimated_query_camera_parameters'):
            self.localize(backend, output=output)
        self.assertFalse((output/'query_evidence.json').exists())

    def test_bad_camera_parameters_never_emit_success_evidence(self):
        config = LocalizationConfig(query_camera_model="PINHOLE", query_camera_params=[-1, 10, 0, 0])
        output = self.output()
        with FakeBackend().installed(), self.assertRaisesRegex(PipelineInputError, "invalid_query_camera_parameters"):
            localize_photo(self.photo, self.map_dir, output, config)
        self.assertFalse((output / "query_evidence.json").exists())
        self.assertFalse((output / "localization_result.json").exists())

    def test_partial_camera_config_and_map_update_rejected_before_backend(self):
        configs = [LocalizationConfig(query_camera_model="PINHOLE"),
                   LocalizationConfig(query_camera_params=[10, 10, 0, 0]),
                   LocalizationConfig(allow_map_update=True),
                   LocalizationConfig(query_camera_model="PINHOLE", query_camera_params=[10, 10, 0, 0],
                                      calibration_path=self.calibration)]
        with patch("go1loc.localization._require_backend") as backend:
            for config in configs:
                with self.assertRaises(PipelineInputError):
                    localize_photo(self.photo, self.map_dir, self.output(), config)
            backend.assert_not_called()

    def test_rejected_missing_photo_output_cannot_overwrite_map(self):
        before = self.snapshot_map()
        missing = self.root / "missing.png"
        with patch("go1loc.localization._require_backend") as backend:
            for output in (self.map_dir, self.map_dir / "rejected-query"):
                with self.assertRaisesRegex(PipelineInputError, "query_output_overlaps_frozen_map"):
                    localize_photo(missing, self.map_dir, output)
            backend.assert_not_called()
        self.assertEqual(before, self.snapshot_map())

    def test_missing_photo_and_manifest_fail_without_backend_and_keep_map_readonly(self):
        before = self.snapshot_map()
        with patch("go1loc.localization._require_backend") as backend:
            result = localize_photo(self.root / "missing.png", self.map_dir, self.output())
            self.assertEqual(result.pose_status, "invalid_input")
            result = localize_photo(self.photo, self.root / "missing-map", self.output())
            self.assertEqual(result.failure_reason, "invalid_map_manifest")
            backend.assert_not_called()
        self.assertEqual(before, self.snapshot_map())

    def test_existing_output_preserved_even_for_failed_query(self):
        output = self.output()
        output.mkdir()
        sentinel = output / "localization_result.json"
        sentinel.write_bytes(b"user-owned-old-result")
        with self.assertRaisesRegex(PipelineInputError, "query_output_not_empty"):
            localize_photo(self.root / "missing.png", self.map_dir, output)
        self.assertEqual(sentinel.read_bytes(), b"user-owned-old-result")

    def test_backend_exception_or_bad_row_alignment_never_produces_success(self):
        before = self.snapshot_map()
        for stage in ("extract", "match", "alignment"):
            with self.subTest(stage=stage):
                backend = FakeBackend(fail_stage=stage)
                backend.bad_mask = stage == "alignment"
                output = self.output()
                with backend.installed(), self.assertRaises((RuntimeError, ValueError)):
                    localize_photo(self.photo, self.map_dir, output)
                self.assertFalse((output / "query_evidence.json").exists())
                self.assertFalse((output / "localization_result.json").exists())
        self.assertEqual(before, self.snapshot_map())

    def test_no_reference_and_no_retrieval_do_not_create_supported_object(self):
        for mode in ("empty_model", "empty_retrieval"):
            backend = FakeBackend()
            if mode == "empty_model":
                backend.images.clear()
            else:
                backend.sfm.parse_retrieval = lambda _: {}
            with backend.installed():
                obj = locate_photo_object(self.photo, self.map_dir, self.output(), (0, 0, 64, 64))
            self.assert_no_support(obj)

    def test_retrieval_branch_uses_global_features_without_gpu(self):
        global_features = self.map_dir / "global.h5"
        global_features.write_bytes(b"synthetic-global")
        self.manifest["retrieval_features"] = str(global_features)
        self.manifest.pop("manifest_sha256")
        self.manifest["manifest_sha256"] = digest_json(self.manifest)
        write_json(self.map_dir / "map_manifest.json", self.manifest)
        result, output, backend = self.localize()
        self.assertEqual(result.pose_status, "localized")
        self.assertEqual(len(backend.created_feature_confs), 2)
        self.assertTrue((output / "query-global.h5").exists())

    def test_relative_native_model_path_resolved_like_verify_map(self):
        self.manifest["model_dir"] = "sfm"
        self.manifest.pop("manifest_sha256")
        self.manifest["manifest_sha256"] = digest_json(self.manifest)
        write_json(self.map_dir / "map_manifest.json", self.manifest)
        result, _, backend = self.localize()
        self.assertEqual(result.pose_status, "localized")
        actual_path = Path(backend.reconstruction.call_args.args[0]).resolve()
        self.assertEqual(actual_path, (self.map_dir / "sfm").resolve())


if __name__ == "__main__":
    unittest.main()
