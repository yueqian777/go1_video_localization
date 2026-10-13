import json
import tempfile
import unittest
from pathlib import Path

from go1loc.calibration import CalibrationError, load_calibration


class CalibrationContractTests(unittest.TestCase):
    def test_unknown_source_is_not_accepted_as_go1_calibration(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "calibration.json"
            path.write_text(json.dumps({"source": "unknown", "model": "fisheye", "image_size": [464, 400], "K": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "distortion": [0, 0, 0, 0], "output_size": [464, 400], "map_version": "x"}), encoding="utf-8")
            with self.assertRaises(CalibrationError):
                load_calibration(path)


if __name__ == "__main__":
    unittest.main()
