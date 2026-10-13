import tempfile
import unittest
from pathlib import Path
from PIL import Image
from go1loc.diagnostics import render_query_evidence


class DiagnosticTests(unittest.TestCase):
    def test_renders_real_rows_and_roi(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); photo=root/'p.png'; Image.new('RGB',(100,80),'black').save(photo)
            data={'selected_cluster':0,'pose_status':'localized','clusters':[{'cluster_index':0,'rows':[
                {'uv_original':[50,40],'pnp_inlier':True},
                {'uv_original':[20,20],'pnp_inlier':False}]}]}
            out=render_query_evidence(photo,data,root/'diagnostic.png',roi=[10,10,70,60])
            with Image.open(out) as image:
                self.assertEqual(image.size,(100,80))
                self.assertNotEqual(image.getpixel((50,40)),(0,0,0))

    def test_failed_query_has_no_fake_features(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); p=root/'p.png'; Image.new('RGB',(100,80),'black').save(p)
            render_query_evidence(p,{'selected_cluster':None,'clusters':[]},root/'out.png')
            with Image.open(root/'out.png') as image:
                self.assertEqual(image.getpixel((90,70)),(0,0,0))
