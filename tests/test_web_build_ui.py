from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

import test_viser_viewer as fixtures
from go1loc.jobs import atomic_json


class StateJobs(fixtures.Jobs):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.states = {}
    def submit(self, kind, params):
        row = super().submit(kind, params)
        self.states[row['job_id']] = row
        return row
    def status(self, key):
        return self.states[key]
    def list_jobs(self):
        return list(self.states.values())


class WebBuildUITests(unittest.TestCase):
    setUp = fixtures.ApplicationTests.setUp

    def test_native_recording_selector_and_calibration_controls(self):
        app = self.app
        rows = [dict(capture_id='rgb-clip', source_kind='orbbec_rgb_mjpeg', camera_id=6,
                     width=1280, height=720, frames=955)]
        with patch('go1loc.web_capture.list_captures', return_value=rows):
            app._web_refresh_captures(None)
        self.assertIn('rgb-clip', app.web_capture_choice.value)
        with patch.object(app.web_build, 'select_capture') as select:
            app._web_select_capture(None)
        select.assert_called_once_with('rgb-clip')
        app.web_kind.value = '录制台原始记录（PNG）'
        self.assertEqual(app._web_settings()['kind'], 'capture')
        with patch.object(app.web_build, 'set_calibration', return_value=dict(model='opencv', image_size=[1280,720])), \
             patch.object(app, '_web_preview'):
            app._web_calibration_upload(SimpleNamespace(target=SimpleNamespace(value=SimpleNamespace(
                name='cal.json', content=b'{}'))))
        self.assertIn('opencv', app.web_calibration_info.value)
        self.assertFalse(app.web_confirm.value)

    def test_native_preview_tick_reconnect_and_clear_calibration(self):
        app = self.app
        inspection = dict(input_kind='capture', source_kind='orbbec_rgb_mjpeg', source={'camera_id': 6},
            probe=dict(width=400, height=200, duration_s=63.5, source_sha256='a'*64), preview=str(self.photo))
        app.web_build.record = {'upload': {'upload_id': 'a'*32}}
        state = dict(job_id='b'*32, status='completed', kind='inspect_video', job_dir=str(self.root/'inspect'))
        with patch.object(app.web_build, 'state', return_value=state), \
             patch.object(app.web_build, 'inspection', return_value=inspection):
            app._web_tick()
            self.assertEqual(app.web_kind.value, '录制台原始记录（PNG）')
            self.assertEqual(app.web_camera.value, '6')
            self.assertEqual(app.web_native.image.shape, (200, 400, 3))
            self.assertIn('读取时间', app.web_status.value)
            app.web_build.record['settings'] = app._web_settings()
            app.web_build.record['settings']['start'] = 5.
            app._web_preview_id = None
            app._web_tick()
            self.assertEqual(app.web_start_s.value, 5.)
            with patch.object(app.web_build, 'clear_calibration') as clear:
                app._web_clear_calibration(None)
            clear.assert_called_once()
            self.assertIn('近似', app.web_calibration_info.value)

    def test_failed_initialization_has_actionable_chinese_explanation(self):
        from go1loc.web_build_ui import build_failure_help
        self.assertIn('平移', build_failure_help('PipelineInputError: sfm_did_not_produce_a_3d_model'))
        self.assertEqual(build_failure_help('other error'), 'other error')

    def test_route_controls_bind_disabled_edges_and_invalidate_old_plan(self):
        from test_planning import fixture, inputs
        from go1loc.planning import build_route_graph
        app = self.app
        graph = build_route_graph(*fixture())
        with patch.object(app, '_read_graph', return_value=graph):
            app._route_refresh_edges(None)
            edge = next(iter(graph['edges']))
            app.route_edge_choice.value = next(label for label, eid in app._route_edge_options.items() if eid == edge)
            app.plan_result = {'status': 'old'}
            app._route_disable_edge(None)
            self.assertIsNone(app.plan_result)
            app.current_start, app.target_result = inputs(graph)
            app.route_avoidance.value = True
            with patch.object(app, '_require_final'), patch.object(app, '_submit') as submit:
                app._plan_route(None)
            self.assertEqual(submit.call_args.args[1]['disabled_edge_ids'], [edge])
            self.assertTrue(submit.call_args.args[1]['local_avoidance_assumed'])
            app._route_restore_edges(None)
            self.assertEqual(app._route_disabled_edges, set())

    def test_upload_callback_submits_inspection_without_shell(self):
        with patch.object(self.app.web_build, 'upload') as upload:
            event = SimpleNamespace(target=SimpleNamespace(value=SimpleNamespace(name='中文.mp4', content=b'video')))
            self.app._web_upload(event)
        upload.assert_called_once_with('中文.mp4', b'video')

    def test_build_context_retires_target_route_and_old_job_results(self):
        state = {'job_id': 'a'*32, 'job_dir': str(self.app.web_build.root/'worker/jobs'/('a'*32))}
        self.app.target_result = {'status': 'supported'}
        self.app.plan_result = {'status': 'route_preview_only'}
        self.app.current_start = {'manual': True}
        self.app._pending_query = Path('old-query')
        with patch('go1loc.web_build_ui.JobManager', fixtures.Jobs):
            self.app._web_enter_build(state)
        self.assertIsNone(self.app.target_result)
        self.assertIsNone(self.app.plan_result)
        self.assertIsNone(self.app.current_start)
        self.assertIsNone(self.app.controller.model)
        self.assertIsNone(self.app._pending_query)
        with self.assertRaisesRegex(ValueError, 'final_map_required'):
            self.app._require_final()
        self.assertEqual(self.app.map_dir.name, 'map')

    def test_ready_snapshot_then_final_can_use_existing_completion_flow(self):
        payload = fixtures.model()
        payload.update(mode='build_snapshot', pts_available=True, total_points=1)
        path = self.root/'build_scene.json'
        atomic_json(path, payload)
        self.app._completed(dict(kind='inspect_build_snapshot', result_path=str(path)))
        self.assertEqual(self.app.controller.mode, 'build_snapshot')
        self.assertIsNone(self.app.target_result)
        payload['mode'] = 'final_map_replay'
        atomic_json(path, payload)
        self.app._completed(dict(kind='inspect_map', result_path=str(path)))
        self.app._require_final()

    def test_preview_uses_actual_dimensions_and_mask_never_changes_input(self):
        inspection = dict(probe=dict(width=400, height=200, duration_s=10., source_sha256='a'*64), preview=str(self.photo))
        before = self.photo.read_bytes()
        with patch.object(self.app.web_build, 'inspection', return_value=inspection):
            self.app.web_crop_xy.value = (0, 0)
            self.app.web_crop_wh.value = (400, 200)
            self.app.web_mask_xy.value = (20, 20)
            self.app.web_mask_wh.value = (20, 20)
            self.app._web_add_mask(None)
            self.assertEqual(self.app.web_mask_image.image.shape, (200, 400, 3))
            self.assertFalse(self.app.web_confirm.value)
            self.assertEqual(len(self.app._web_exclusions), 1)
            self.app._web_undo_mask(None)
            self.assertEqual(self.app._web_exclusions, [])
            self.app.web_mask_wh.value = (500, 500)
            with self.assertRaises(RuntimeError):
                self.app._web_add_mask(None)
            self.assertEqual(self.app._web_exclusions, [])
        self.assertEqual(before, self.photo.read_bytes())

    def test_monitor_follow_pause_history_failure_and_final_handoff(self):
        app = self.app
        directory = app.web_build.root/'worker/jobs'/('a'*32)
        state = dict(job_id='a'*32, kind='build_video', status='running', job_dir=str(directory),
                     run_dir=str(directory/'output/build_runs/run'), progress={},
                     build=dict(stage='reconstruction', snapshots=[{'snapshot_index': 1, 'component_id': 'c'}]))
        app.web_build.record = {'upload': {'upload_id': 'u'}}
        def complete():
            job_id = app._web_scene_job[0]
            payload = fixtures.model()
            kind = app.jobs.status(job_id)['kind']
            payload.update(mode='final_map_replay' if kind == 'inspect_map' else 'build_snapshot',
                           pts_available=True, total_points=1)
            path = self.root/(job_id+'.json')
            atomic_json(path, payload)
            row = dict(app.jobs.status(job_id), status='completed', result_path=str(path))
            app.jobs.states[job_id] = row
            app._completed(row)
            app._seen.add(job_id)
        with patch.object(app.web_build, 'state', return_value=state), \
             patch.object(app.web_build, 'inspection', return_value=None), \
             patch('go1loc.web_build_ui.JobManager', StateJobs):
            app._web_tick()
            self.assertIsNone(app.controller.model)
            self.assertTrue(app.web_upload.disabled)
            complete()
            app._web_tick()
            self.assertEqual(len(app.jobs.submitted), 1)  # No repeated load of unchanged READY.
            self.assertIn('真实过程快照', app.web_display_info.value)
            state['build']['snapshots'].append({'snapshot_index': 2, 'component_id': 'c'})
            app._web_tick()
            app.web_follow.value = False
            app._web_tick()
            complete()  # stale auto load is ignored after pause
            app._web_tick()
            self.assertIsNone(app._web_scene_job)
            app.web_snapshot.value = '1'
            app._web_history(None)
            app._web_tick()
            complete()
            app._web_tick()
            self.assertEqual(app._web_loaded_key, ('history', 1))
            state.update(status='failed', error='test failure')
            app._web_tick()
            self.assertIsNotNone(app.controller.model)
            self.assertIn('test failure', app.web_status.value)
            with self.assertRaises(ValueError):
                app._web_final(None)
            state.update(status='completed')
            state['build']['final_map_dir'] = str(app.map_dir)
            app._web_final(None)
            app._web_tick()
            complete()
            app._web_tick()
            self.assertEqual(app.controller.mode, 'final_map_replay')
            app._web_original(None)
            self.assertEqual(app.map_dir, self.map.resolve())
            self.assertFalse(app.web_follow.value)

    def test_stale_map_job_cannot_replace_new_build_context(self):
        app = self.app
        old = fixtures.model()
        path = self.root/'old_scene.json'
        atomic_json(path, old)
        app._job_revisions['old'] = app._task_revision - 1
        app.controller.model = None
        app._completed(dict(job_id='old', kind='inspect_map', result_path=str(path)))
        self.assertIsNone(app.controller.model)


if __name__ == '__main__': unittest.main()
