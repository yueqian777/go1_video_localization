from pathlib import Path
import shutil
import tempfile
import unittest
import json
from go1loc.mapping import MappingConfig, _ascii_stage, _effective_feature_conf, build_map, temporal_pairs
from go1loc.localization import localize_photo
from go1loc.schemas import LocalizationResult


class GuardTests(unittest.TestCase):
    def test_feature_keypoint_budget_isolated_and_named(self):
        base = {
            "output": "feats-superpoint-n4096-r1024",
            "model": {"name": "superpoint", "max_keypoints": 4096},
        }
        actual = _effective_feature_conf(base, 2048)
        self.assertEqual(actual["model"]["max_keypoints"], 2048)
        self.assertEqual(actual["output"], "feats-superpoint-n2048-r1024")
        self.assertEqual(base["model"]["max_keypoints"], 4096)

    def test_mapping_config_records_default_keypoint_budget(self):
        self.assertEqual(MappingConfig().max_keypoints, 4096)

    def test_non_ascii_query_stage_is_ascii_and_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "中文"
            root.mkdir()
            source = root / "query.jpg"
            source.write_bytes(b"test-image-bytes")
            staged = _ascii_stage(root, ["query.jpg"])
            self.assertIsNotNone(staged)
            try:
                self.assertTrue(str(staged).isascii())
                self.assertEqual((staged / "query.jpg").read_bytes(), source.read_bytes())
            finally:
                shutil.rmtree(staged, ignore_errors=True)


    def test_temporal_pairs_are_bounded(self):
        pairs = temporal_pairs([str(i) for i in range(100)],3)
        self.assertEqual(len(pairs),294)
        self.assertTrue(all(0<int(b)-int(a)<=3 for a,b in pairs))

    def test_frozen_map_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            (p/'map_manifest.json').write_text('{"frozen":true}')
            with self.assertRaisesRegex(Exception,'frozen_map'):
                build_map(p/'missing',p)

    def test_failure_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            r=localize_photo(p/'absent.jpg',p/'no-map',p/'result')
            saved=json.loads((p/'result'/'localization_result.json').read_text())
            self.assertIsNone(saved['T_WC_query'])
            self.assertEqual(saved['pose_status'],'invalid_input')

    def test_map_output_overlap_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            (p/'map_manifest.json').write_text('{"frozen":true}')
            with self.assertRaisesRegex(Exception,'output_overlaps'):
                localize_photo(p/'q.jpg',p,p/'query')
