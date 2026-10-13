import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from go1loc.cli import main


class CliGuardTests(unittest.TestCase):
    def test_rejected_output_cannot_write_into_frozen_map(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); marker=root/'map_manifest.json'
            marker.write_text('{"frozen": true}')
            with contextlib.redirect_stdout(io.StringIO()):
                code=main(['localize-photo','--photo',str(root/'q.jpg'),'--map',str(root),'--output',str(root)])
            self.assertNotEqual(code, 0)
            self.assertEqual([p.name for p in root.iterdir()], ['map_manifest.json'])

    def test_partial_explicit_intrinsics_rejected_before_backend(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code=main(['localize-photo','--photo',str(root/'q.jpg'),'--map',str(root/'map'),
                           '--output',str(root/'out'),'--camera-model','PINHOLE'])
            self.assertNotEqual(code,0)
            self.assertIn('required_together', out.getvalue())
            self.assertFalse((root/'out').exists())
