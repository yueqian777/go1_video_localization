"""First-build watcher UI protocol; fixtures are not real video acceptance."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import test_viser_viewer as f
from go1loc.jobs import atomic_json


class BuildViewerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.cache/tests')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = {'run': {'output_dir': str(self.root/'map'), 'image_dir': str(self.root/'images')}, 'events': [], 'snapshots': [],
                      'status': 'running', 'stage': 'features', 'failure_reason': None, 'final_map_dir': None}

    def app(self):
        from go1loc.build_viewer import BuildWatchApplication
        self.reader = patch('go1loc.build_viewer.read_build_state', side_effect=lambda _: self.state)
        self.reader.start(); self.addCleanup(self.reader.stop)
        with patch('go1loc.build_viewer.JobManager', f.Jobs), patch('go1loc.build_viewer.threading.Thread'):
            app = BuildWatchApplication(self.root/'run', self.root/'ui', server=f.Server())
        self.addCleanup(app.close)
        return app

    def test_starts_before_manifest_and_auto_updates_complete_snapshots(self):
        app = self.app()
        self.assertFalse((self.root/'map/map_manifest.json').exists())
        self.assertFalse(app.jobs.submitted)
        app.tick()
        self.assertIn('features', app.status.value)
        self.state['snapshots'] = [{'snapshot_index': 1, 'component_id': 'attempt1'}]
        app.tick()
        self.assertEqual(app.jobs.submitted[-1][0], 'inspect_build_snapshot')
        payload = f.model(); payload.update(mode='build_snapshot', total_points=1, pts_available=False)
        path = self.root/'scene.json'; atomic_json(path, payload)
        app.complete({'status': 'completed', 'result_path': str(path)})
        self.assertEqual(app.controller.mode, 'build_snapshot')
        self.state['snapshots'].append({'snapshot_index': 2, 'component_id': 'attempt2'})
        app.tick()
        self.assertEqual(len(app.jobs.submitted), 2)

    def test_pause_manual_history_failure_preserves_geometry_and_final_handoff(self):
        app = self.app()
        payload = f.model(); payload.update(mode='build_snapshot', total_points=1, pts_available=False)
        app.controller.replace_model(payload)
        app.follow.value = False
        self.state.update(snapshots=[{'snapshot_index': 1, 'component_id': 'attempt1'}], status='failed', failure_reason='backend_failed')
        app.tick()
        self.assertIn('backend_failed', app.status.value)
        self.assertFalse(app.jobs.submitted)
        self.assertEqual(app.controller.model, payload)
        app.snapshot.value = '1'
        app.load_history(None)
        self.assertEqual(app.jobs.submitted[-1][1]['snapshot_index'], 0)
        app.complete({'status': 'failed', 'error': 'corrupt snapshot'})
        app.follow.value = True
        self.state.update(status='completed', final_map_dir=str(self.root/'map'))
        app.tick()
        self.assertEqual(app.jobs.submitted[-1][0], 'inspect_map')

    def test_delayed_checkbox_callback_does_not_cancel_manual_history(self):
        app = self.app()
        self.state['snapshots'] = [{'snapshot_index': 1, 'component_id': 'attempt1'}]
        app.follow.value = False
        app.tick()
        app.snapshot.value = '1'
        app.load_history(None)
        generation = app._generation
        app._follow_changed(None)  # Viser may deliver programmatic update later
        self.assertEqual(app._generation, generation)
        payload = f.model(); payload.update(mode='build_snapshot', total_points=1)
        path = self.root/'scene.json'; atomic_json(path, payload)
        app.complete({'status': 'completed', 'result_path': str(path)})
        self.assertEqual(app.controller.mode, 'build_snapshot')

    def test_missing_run_has_actionable_error_not_frozen_manifest_requirement(self):
        from go1loc.build_viewer import BuildWatchApplication
        with self.assertRaisesRegex(ValueError, 'run_metadata_not_ready'):
            BuildWatchApplication(self.root/'not-started', self.root/'ui', server=f.Server())


if __name__ == '__main__':
    unittest.main()
