"""Fixed-worker I/O checks using explicit synthetic inputs."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import test_planning as f
from go1loc.jobs import atomic_json, read_json
from go1loc.route_review import generate_route_candidates, review_route_candidate
from go1loc.route_workflow import save_review, route_inputs
from go1loc.video import sha256_file
from go1loc.evidence import digest_json


class RouteWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.cache/tests')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.args = list(f.fixture())
        self.args[0]['image_dir'] = str(self.root/'input/images')
        self.args[0]['manifest_sha256'] = digest_json({k: v for k, v in self.args[0].items() if k != 'manifest_sha256'})
        self.args[2].pop('route_segments')
        self.args[2]['source_kind'] = 'synthetic_test'

    def test_worker_rederives_review_replays_decisions_and_refuses_changed_inputs(self):
        review = generate_route_candidates(*self.args)
        review = review_route_candidate(review, review['candidates'][0]['candidate_id'], 'confirm',
            reviewer='test', evidence_note='synthetic-only', reviewed_at=f.NOW)
        with patch('go1loc.route_workflow.route_inputs', return_value=self.args):
            out = save_review(self.root/'map', self.root/'out', input_sha256=review['input_sha256'],
                              decisions=review['decisions'])
            self.assertEqual(out['edges'], 3)
            self.assertEqual(read_json(out['segments_path']), review['route_segments'])
            self.assertFalse(out['execution_permitted'])
            with self.assertRaisesRegex(ValueError, 'review_inputs_changed'):
                save_review(self.root/'map', self.root/'wrong', input_sha256='wrong', decisions=[])
            self.assertFalse((self.root/'wrong').exists())
            with self.assertRaisesRegex(ValueError, 'output_exists'):
                save_review(self.root/'map', self.root/'out', input_sha256=review['input_sha256'], decisions=[])

    def test_original_frame_bytes_hash_not_canonical_json_and_tamper_rejected(self):
        path = self.root/'input/frames_manifest.json'
        atomic_json(path, self.args[2])
        manifest = dict(self.args[0], frames_manifest=self.args[2], model_dir='model',
                        frames_manifest_sha256=sha256_file(path))
        native = MagicMock(); native.images = {}
        with patch('go1loc.route_workflow.verify_map', return_value=manifest), \
             patch('pycolmap.Reconstruction', return_value=native):
            self.assertEqual(route_inputs(self.root/'map')[2], self.args[2])
            path.write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'frames_manifest_missing_or_hash_mismatch'):
                route_inputs(self.root/'map')


if __name__ == '__main__':
    unittest.main()
