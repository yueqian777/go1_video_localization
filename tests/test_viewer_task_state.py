"""Task dependency regressions: no old target/route survives query switching."""
from copy import deepcopy
import unittest
import test_viser_viewer as fixtures
from go1loc.jobs import atomic_json


class TaskStateTests(unittest.TestCase):
    setUp = fixtures.ApplicationTests.setUp
    evidence_files = fixtures.ApplicationTests.evidence_files

    def ready_a(self):
        query, result, obj = self.evidence_files()
        self.app.load_object(self.root/'object_result.json')
        self.app._manual_start(None)
        self.app.plan_result = fixtures.plan_fixture()
        self.app.controller.load_plan(self.app.plan_result)
        return query, result, obj

    def assert_no_target(self):
        self.assertIsNone(self.app.target_result)
        self.assertIsNone(self.app.plan_result)
        self.assertIsNone(self.app.target_photo)
        for group in ('support', 'route', 'goal'):
            self.assertNotIn(group, self.app.controller.groups)
        self.assertFalse(self.app.evidence_image.visible)
        with self.assertRaisesRegex(ValueError, 'requires_explicit_start'):
            self.app._plan_route(None)

    def test_successful_b_invalidates_a_and_retains_explicit_start(self):
        _, result, _ = self.ready_a()
        self.app._open_photo(self.event)
        photo_handle = self.app._photo_states[1]['handle']
        current = deepcopy(self.app.current_start)
        atomic_json(self.root/'b'/'localization_result.json', dict(result, query_id='B'))
        self.app.load_query(self.root/'b')
        self.assert_no_target()
        self.assertEqual(self.app.current_start, current)
        self.assertIn('query', self.app.controller.groups)
        self.assertTrue(photo_handle.removed)
        self.assertFalse(self.app._photo_states)

    def test_failed_or_unreadable_b_also_invalidates_a(self):
        _, result, _ = self.ready_a()
        atomic_json(self.root/'b'/'localization_result.json', dict(result, pose_status='failed'))
        self.app.load_query(self.root/'b')
        self.assert_no_target()
        self.assertNotIn('query', self.app.controller.groups)
        self.ready_a()
        with self.assertRaises(OSError):
            self.app.load_query(self.root/'missing')
        self.assert_no_target()

    def test_complete_object_reload_commits_new_target_after_query_load(self):
        _, result, obj = self.ready_a()
        atomic_json(self.root/'b'/'localization_result.json', dict(result, pose_status='failed'))
        self.app.load_query(self.root/'b')
        self.app.load_object(self.root/'object_result.json')
        self.assertEqual(self.app.target_result, obj)
        self.assertIn('support', self.app.controller.groups)
        self.assertIsNone(self.app.plan_result)

    def test_late_worker_and_saved_plan_cannot_restore_a_route(self):
        _, result, _ = self.ready_a()
        job = self.app._submit('plan_route', {})
        path = self.root/'old-plan.json'
        atomic_json(path, fixtures.plan_fixture())
        atomic_json(self.root/'b'/'localization_result.json', dict(result, query_id='B'))
        self.app.load_query(self.root/'b')
        self.app._completed(dict(job, result_path=str(path)))
        self.assert_no_target()
        self.app.plan_path.value = str(path)
        with self.assertRaisesRegex(ValueError, 'current_target'):
            self.app._load_plan(None)


if __name__ == '__main__':
    unittest.main()
