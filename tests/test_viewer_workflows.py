"""Synthetic GUI workflow contracts, not actual video/robot acceptance."""
from copy import deepcopy
from unittest.mock import patch
import unittest

import test_viser_viewer as ui
import test_planning as planning
import test_planning_time as clocks
from go1loc.jobs import atomic_json
from go1loc.planning import build_route_graph
from go1loc.route_review import generate_route_candidates


class WorkflowTests(unittest.TestCase):
    setUp = ui.ApplicationTests.setUp

    def connected_fixture(self):
        graph = build_route_graph(*planning.fixture())
        model = ui.model()
        model.update({k: graph[k] for k in ('map_id', 'map_hash', 'component_id')})
        model['cameras'] = [dict(name=n, T_WC=v['T_MC'], image_id=v['image_id'], fov=.8, aspect=1.5)
                            for n, v in graph['nodes'].items()]
        self.app.controller.replace_model(model)
        atomic_json(self.root/'graph.json', graph)
        self.app.graph_path.value = str(self.root/'graph.json')
        full = clocks.current_photo(graph)
        source = self.root/'synthetic-evidence.txt'
        source.write_text('synthetic fixture only', encoding='utf-8')
        from go1loc.video import sha256_file
        full['attachment']['source_sha256'] = sha256_file(source)
        self.app.attachment_source.value = str(source)
        self.app.current_start = deepcopy(full)
        self.app.current_start.pop('attachment')
        atomic_json(self.root/'import.json', full)
        self.app.attachment_path.value = str(self.root/'import.json')
        self.app.time_mode.value = 'offline_replay'
        self.app.replay_time.value = clocks.REPLAY
        return graph, full

    def test_inspect_then_adopt_binding_and_invalid_reimport_clears_old_route(self):
        _, full = self.connected_fixture()
        with patch('go1loc.viewer_workflows.utc_now', return_value=clocks.RUN):
            self.app._inspect_attachment(None)
            self.assertNotIn('attachment', self.app.current_start)
            self.assertIn('attachment', self.app.controller.groups)
            self.assertIn('synthetic-only', self.app.connection_info.value)
            self.app._adopt_attachment(None)
            self.assertEqual(self.app.current_start, full)
            self.app.plan_result = {'obsolete': True}
            full['attachment']['query_id'] = 'another-query'
            atomic_json(self.root/'import.json', full)
            self.app._inspect_attachment(None)
            self.assertIsNone(self.app.plan_result)
            self.assertNotIn('attachment', self.app.current_start)
            self.assertNotIn('attachment', self.app.controller.groups)
            with self.assertRaisesRegex(ValueError, 'inspect_valid'):
                self.app._adopt_attachment(None)

    def test_replay_submits_separate_time_and_reports_missing_evidence(self):
        graph, full = self.connected_fixture()
        self.app.target_result = planning.inputs(graph)[1]
        with patch('go1loc.viewer_workflows.utc_now', return_value=clocks.RUN):
            self.app._inspect_connection(None)
            self.assertIn('attachment', self.app.connection_info.value)
            self.assertNotIn('attachment', self.app.controller.groups)
            self.app._inspect_attachment(None)
            self.app._adopt_attachment(None)
            self.app._plan_route(None)
        request = self.app.jobs.submitted[-1][1]
        self.assertEqual(request['time_mode'], 'offline_replay')
        self.assertEqual(request['replay_reference_time'], clocks.REPLAY)
        self.assertEqual(request['start']['captured_at'], clocks.CAPTURE)
        self.assertEqual(request['start']['localized_at'], clocks.LOCALIZED)
        self.app._workflow_context_changed(None)
        self.assertIsNone(self.app._pending_attachment)
        self.assertNotIn('attachment', self.app.controller.groups)

    def test_review_buttons_split_confirm_reject_and_save_decisions_only(self):
        manifest, refs, frames = planning.fixture()
        frames.pop('route_segments')
        frames['source_kind'] = 'synthetic_test'
        review = generate_route_candidates(manifest, refs, frames)
        self.app._set_review(review)
        self.assertFalse(self.app.route_review['route_segments'])
        self.app.reviewer.value = 'fixture reviewer'
        self.app.review_note.value = 'synthetic fixture only'
        self.app.review_first.value = 'm.png'
        self.app.review_last.value = 'wall.png'
        self.app._review_decide('split')
        self.assertEqual(len(self.app.review_candidate.options), 2)
        self.app._review_decide('confirm')
        self.app._review_decide('reject')
        self.assertEqual(len(self.app.route_review['route_segments']), 1)
        self.app._save_review(None)
        kind, params = self.app.jobs.submitted[-1]
        self.assertEqual(kind, 'save_route_review')
        self.assertEqual(len(params['decisions']), 3)
        self.assertNotIn('route_segments', params)

    def test_unreadable_saved_current_removes_old_start_and_attachment(self):
        self.connected_fixture()
        self.app.current_query.value = str(self.root/'missing')
        self.app.plan_result = {'old': True}
        with self.assertRaises(OSError):
            self.app._load_current_query(None)
        self.assertIsNone(self.app.current_start)
        self.assertIsNone(self.app.plan_result)

    def test_changed_import_or_wrong_source_cannot_be_adopted_after_preview(self):
        _, full = self.connected_fixture()
        with patch('go1loc.viewer_workflows.utc_now', return_value=clocks.RUN):
            self.app._inspect_attachment(None)
            full['attachment']['evidence_id'] = 'replaced-after-inspection'
            atomic_json(self.root/'import.json', full)
            with self.assertRaisesRegex(ValueError, 'inputs_changed'):
                self.app._adopt_attachment(None)
            self.assertNotIn('attachment', self.app.current_start)
            self.app._inspect_attachment(None)
            self.app.attachment_source.value = str(self.photo)
            with self.assertRaisesRegex(ValueError, 'source_file_required_or_hash_mismatch'):
                self.app._adopt_attachment(None)
            self.assertNotIn('attachment', self.app.current_start)

    def test_saved_current_result_hash_times_pose_and_photo_are_bound(self):
        from go1loc.evidence import digest_json
        from go1loc.video import sha256_file
        graph, full = self.connected_fixture()
        photo = self.other
        result = {k: full[k] for k in ('map_id', 'map_hash', 'component_id', 'pose_status', 'quality')}
        result.update(query_id=photo.name, query_sha256=sha256_file(photo), T_WC_query=full['T_MC'],
                      capture_time=full['captured_at'], localization_time=full['localized_at'])
        evidence = {k: result[k] for k in ('map_id', 'map_hash', 'component_id', 'pose_status', 'capture_time', 'localization_time')}
        evidence.update(query_path=str(photo), localization_result_sha256=digest_json(result),
                        binding={'query_sha256': sha256_file(photo), 'map_hash': graph['map_hash'], 'files': {}})
        evidence['evidence_sha256'] = digest_json(evidence)
        directory = self.root/'saved-current'
        atomic_json(directory/'localization_result.json', result)
        atomic_json(directory/'query_evidence.json', evidence)
        self.app.current_query.value = str(directory)
        self.app._load_current_query(None)
        self.assertEqual(self.app.current_start['captured_at'], clocks.CAPTURE)
        self.assertNotIn('attachment', self.app.current_start)
        # Calibration legitimately changes the hloc key to a processed name.
        processed = directory/'query_rectified.png'
        processed.write_bytes(photo.read_bytes())
        result['query_id'] = processed.name
        evidence['binding']['files']['processed_query'] = {'path': str(processed), 'sha256': sha256_file(processed)}
        evidence['localization_result_sha256'] = digest_json(result)
        evidence['evidence_sha256'] = digest_json({k: v for k, v in evidence.items() if k != 'evidence_sha256'})
        atomic_json(directory/'localization_result.json', result)
        atomic_json(directory/'query_evidence.json', evidence)
        self.app._load_current_query(None)
        self.assertEqual(self.app.current_start['query_id'], 'query_rectified.png')
        result['T_WC_query'][0][3] = 999
        atomic_json(directory/'localization_result.json', result)
        with self.assertRaisesRegex(ValueError, 'not_bound_to_evidence'):
            self.app._load_current_query(None)
        self.assertIsNone(self.app.current_start)

    def test_obsolete_review_generation_and_save_do_not_overwrite_new_decisions(self):
        manifest, refs, frames = planning.fixture()
        frames.pop('route_segments')
        frames['source_kind'] = 'synthetic_test'
        old = generate_route_candidates(manifest, refs, frames)
        self.app._prepare_review(None)
        self.app.review_gap.value = .75
        self.app._prepare_review(None)
        path = self.root/'old-review.json'
        atomic_json(path, old)
        self.app._completed({'job_id': '2', 'kind': 'prepare_route_review', 'result_path': str(path)})
        self.assertIsNone(self.app.route_review)
        self.app._set_review(old)
        self.app.reviewer.value = 'test'
        self.app.review_note.value = 'synthetic-only'
        self.app.review_first.value = 'm.png'
        self.app.review_last.value = 'wall.png'
        self.app._review_decide('split')
        self.app._review_decide('confirm')
        self.app._save_review(None)
        save_id = str(len(self.app.jobs.submitted))
        self.app._review_decide('confirm')
        atomic_json(path, {'graph_path': 'obsolete-graph.json', 'confirmed_segments': 1, 'edges': 1})
        self.app._completed({'job_id': save_id, 'kind': 'save_route_review', 'result_path': str(path)})
        self.assertNotEqual(self.app.graph_path.value, 'obsolete-graph.json')


if __name__ == '__main__':
    unittest.main()
