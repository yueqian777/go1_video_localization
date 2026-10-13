import json
import tempfile
import unittest
from pathlib import Path
import numpy as np
from PIL import Image
from go1loc.query_geometry import prepare_query, original_coordinates


class QueryGeometryTests(unittest.TestCase):
    def test_identity_and_calibrated_crop(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); photo = root/'photo.png'
            Image.new('RGB', (40, 30), 'white').save(photo)
            path, geometry, rect = prepare_query(photo, root/'raw', None)
            self.assertEqual(path.name, 'photo.png')
            self.assertIsNone(rect)
            calibration = root/'cal.json'
            calibration.write_text(json.dumps({'source': 'test_only', 'model': 'pinhole',
                'image_size': [40, 30], 'output_size': [40, 30], 'map_version': 'test',
                'K': [[30, 0, 20], [0, 30, 15], [0, 0, 1]], 'distortion': [0, 0, 0, 0]}))
            path, geometry, rect = prepare_query(photo, root/'rect', calibration)
            self.assertEqual(geometry['status'], 'calibrated_rectified')
            np.testing.assert_allclose(original_coordinates([[4., 5.]], rect), [[5, 6]], atol=1e-5)
            with Image.open(path) as image:
                self.assertEqual(image.size, (38, 28))

    def test_size_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); p=root/'p.png'; Image.new('RGB', (10, 10)).save(p)
            c=root/'c.json'; c.write_text(json.dumps({'source':'test', 'model':'pinhole',
                'image_size':[40, 30], 'output_size':[40, 30], 'map_version':'test',
                'K':[[30,0,20],[0,30,15],[0,0,1]],'distortion':[0,0,0,0]}))
            with self.assertRaisesRegex(ValueError, 'size'):
                prepare_query(p, root/'out', c)
