import tempfile
import unittest
from pathlib import Path

from go1loc.input_safety import validate_image_path, validate_upload


class InputSafetyTests(unittest.TestCase):
    def test_rejects_missing_and_disallowed_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertFalse(validate_image_path(root / "missing.jpg", root).ok)
            bad = root / "payload.exe"
            bad.write_bytes(b"MZ")
            self.assertFalse(validate_upload(bad, root).ok)

    def test_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            outside = root.parent / "outside.jpg"
            outside.write_bytes(b"not-an-image")
            try:
                result = validate_image_path(outside, root)
                self.assertFalse(result.ok)
            finally:
                outside.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
