import tempfile
import unittest
from pathlib import Path

from go1loc.dense_mvs import run_dense_mvs


class DenseMvsTests(unittest.TestCase):
    def test_missing_colmap_is_reported_without_fake_dense_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image_dir = root / "images"
            sparse_dir = root / "sparse"
            image_dir.mkdir()
            sparse_dir.mkdir()
            for name in ("cameras.bin", "images.bin", "points3D.bin"):
                (sparse_dir / name).write_bytes(b"fixture")
            report = run_dense_mvs(image_dir=image_dir, sparse_dir=sparse_dir,
                                   output_dir=root / "dense", colmap_binary="")
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["reason"], "colmap_not_installed")
            self.assertFalse(report["sparse_is_not_dense"] is False)
            self.assertFalse((root / "dense" / "fused.ply").exists())

    def test_default_mode_only_plans_when_colmap_is_available(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "images").mkdir()
            (root / "sparse").mkdir()
            for name in ("cameras.bin", "images.bin", "points3D.bin"):
                (root / "sparse" / name).write_bytes(b"fixture")
            report = run_dense_mvs(image_dir=root / "images", sparse_dir=root / "sparse",
                                   output_dir=root / "dense", colmap_binary="colmap.exe")
            self.assertEqual(report["status"], "ready")
            self.assertFalse(report["execution_permitted"])
            self.assertEqual(report["completed_steps"], [])


if __name__ == "__main__":
    unittest.main()
