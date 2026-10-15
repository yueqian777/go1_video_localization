"""Fake-scene contract checks. They do not claim browser-rendered UI QA."""
from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

import numpy as np

from go1loc.viser_viewer import (SceneController, ViewerApplication, rectangle_to_roi,
                               validate_planning_preview, framing, serve_viser)
from go1loc.jobs import atomic_json


class Handle:
    def __init__(self, name, scene, **kwargs):
        self.name, self.scene, self.kwargs = name, scene, kwargs
        self.removed = False
    def remove(self):
        self.removed = True
        self.scene.active.pop(self.name, None)
    def on_click(self, callback):
        return callback


class Scene:
    def __init__(self):
        self.active = {}
    def add(self, name, **kwargs):
        handle = Handle(name, self, **kwargs)
        self.active[name] = handle
        return handle
    add_point_cloud = add
    add_camera_frustum = add
    add_frame = add
    add_label = add
    add_line_segments = add
    def add_image(self, name, image, **kwargs):
        return self.add(name, image=image, **kwargs)
    def set_up_direction(self, value):
        self.up = value
    def on_rect_select(self, **kwargs):
        def register(callback):
            self.select = callback
            return callback
        return register


class Gui:
    def __init__(self):
        self.labels = []
        self.tabs = []

    def add_folder(self, label, **kwargs):
        self.labels.append(label)
        return nullcontext()

    def add_tab_group(self, **kwargs):
        def tab(label, **kwargs):
            self.tabs.append(label)
            return nullcontext()
        return SimpleNamespace(add_tab=tab)

    def __getattr__(self, method):
        def add(*args, **kwargs):
            if args:
                self.labels.append(args[0])
            value = kwargs.get('initial_value', args[1] if len(args) > 1 else None)
            options = args[1] if method == 'add_dropdown' else []
            if options:
                value = options[0]
            result = SimpleNamespace(value=value, options=options, visible=kwargs.get('visible', True))
            result.on_click = lambda cb: setattr(result, 'click', cb)
            result.on_update = lambda cb: setattr(result, 'update', cb)
            result.on_upload = lambda cb: setattr(result, 'upload', cb)
            return result
        return add


class Server:
    def __init__(self):
        self.scene = Scene()
        self.gui = Gui()
    def atomic(self):
        return nullcontext()
    def on_client_connect(self, callback):
        self.connect = callback
    def get_clients(self):
        return {}
    def stop(self):
        self.stopped = True


class Jobs:
    def __init__(self, *args, **kwargs):
        self.submitted = []
    def submit(self, kind, params):
        self.submitted.append((kind, params))
        return {'job_id': str(len(self.submitted)), 'kind': kind, 'status': 'queued'}
    def list_jobs(self):
        return []
    def close(self):
        pass


def model(version=1, component='c0'):
    return {'map_id': 'm', 'map_hash': 'h', 'component_id': component,
            'mode': 'final_map_replay', 'revision': version, 'units': 'map_units',
            'points': [[version, 0, 0]], 'colors': [[255, 0, 0]], 'point_ids': [1],
            'cameras': [{'name': 'a.jpg', 'image_id': 1, 'T_WC': np.eye(4).tolist(),
                         'wxyz': [1, 0, 0, 0], 'fov': .8, 'aspect': 1.5}]}


def plan_fixture():
    return {'map_id': 'm', 'map_hash': 'h', 'component_id': 'c0',
            'status': 'route_preview_only', 'execution_permitted': False,
            'path': [[0, 0, 0]], 'route_edge_ids': [], 'route_node_ids': ['a.jpg'],
            'connectivity_provenance': {'start_attachment': {'kind': 'manual_exact_node', 'manual': True}, 'edges': []},
            'geometry_provenance': {'source': 'frozen_reference_camera_poses'}, 'units': 'map_units',
            'start_camera_pose': np.eye(4).tolist(), 'goal_observation_pose': np.eye(4).tolist(),
            'start_source': 'explicit_pose'}


class ViewerTests(unittest.TestCase):
    def test_replace_entire_model_clears_stale_handles_and_components(self):
        server = Server()
        view = SceneController(server)
        view.replace_model(model())
        previous = list(server.scene.active.values())
        view.replace_model(model(2, 'unaligned-other'))
        self.assertTrue(all(h.removed for h in previous))
        self.assertEqual(view.model['component_id'], 'unaligned-other')
        self.assertEqual(len(server.scene.active), len(previous))

    def test_reconnect_preserves_scene_and_does_not_restart_work(self):
        server = Server()
        view = SceneController(server)
        view.replace_model(model())
        handles = list(server.scene.active.values())
        state = view.on_client_connect(object())
        self.assertEqual(state['map_id'], 'm')
        self.assertEqual(handles, list(server.scene.active.values()))
        self.assertFalse(any(h.removed for h in handles))

    def test_build_mode_without_snapshot_refused_without_final_fallback(self):
        view = SceneController(Server())
        view.replace_model(model())
        with self.assertRaisesRegex(ValueError, 'no_ready_snapshot'):
            view.set_mode('build_snapshot', [])
        self.assertEqual(view.mode, 'final_map_replay')

    def test_point_budget_and_invalid_model_preserves_previous(self):
        view = SceneController(Server(), point_budget=2)
        payload = model()
        payload.update(points=[[i, 0, 0] for i in range(10)], colors=[[0, 0, 0]]*10,
                       point_ids=list(range(10)))
        view.replace_model(payload)
        self.assertEqual(view.displayed_points, 2)
        bad = deepcopy(payload)
        bad['points'][0] = [float('nan'), 0, 0]
        with self.assertRaises(ValueError):
            view.replace_model(bad)
        self.assertEqual(view.displayed_points, 2)

    def test_rectangle_maps_to_raw_pixels_with_letterboxing_and_zoom(self):
        roi = rectangle_to_roi((.25, .25), (.75, .75), width=400, height=200,
                               camera_position=(0, 0, 2), camera_wxyz=(0, 1, 0, 0),
                               fov=np.pi/2, aspect=2, plane_z=0, render_height=2)
        self.assertTrue(np.allclose(roi, [0, 0, 400, 200]))
        with self.assertRaisesRegex(ValueError, 'photo_view_not_front_parallel'):
            rectangle_to_roi((0, 0), (1, 1), width=400, height=200,
                             camera_position=(0, 0, 2), camera_wxyz=(1, 0, 0, 0),
                             fov=1, aspect=2, plane_z=0, render_height=2)

    def test_route_refuses_unverified_or_cross_map_preview(self):
        payload = plan_fixture()
        self.assertEqual(len(validate_planning_preview(payload, model())), 1)
        for key, value in [('execution_permitted', True), ('map_hash', 'other'),
                           ('connectivity_provenance', {}), ('status', 'rejected')]:
            bad = dict(payload, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_planning_preview(bad, model())

    def test_snapshot_disallows_all_final_overlays(self):
        view = SceneController(Server())
        payload = model()
        payload['mode'] = 'build_snapshot'
        view.replace_model(payload)
        for method, args in [(view.load_query, ({},)), (view.load_object, ({}, {})),
                             (view.load_plan, (plan_fixture(),))]:
            with self.assertRaisesRegex(ValueError, 'alignment'):
                method(*args)

    def test_no_invented_route_vertices_even_in_matching_map(self):
        payload = plan_fixture()
        payload['path'] = [[999, 0, 0]]
        with self.assertRaisesRegex(ValueError, 'geometry_not_supported'):
            validate_planning_preview(payload, model())

    def test_verified_multinode_route_drawn_and_unverified_edge_rejected(self):
        payload, geometry = plan_fixture(), model()
        camera = deepcopy(geometry['cameras'][0])
        camera['name'] = 'b.jpg'
        camera['image_id'] = 2
        camera['T_WC'][0][3] = 2
        geometry['cameras'].append(camera)
        payload.update(path=[[0, 0, 0], [2, 0, 0]], route_node_ids=['a.jpg', 'b.jpg'],
                       route_edge_ids=['edge1'], goal_observation_pose=camera['T_WC'])
        payload['connectivity_provenance']['edges'] = [{'kind': 'verified_temporal_segment',
            'evidence_id': 'fixture-only', 'frame_names': ['a.jpg', 'b.jpg']}]
        view = SceneController(Server())
        view.replace_model(geometry)
        view.load_plan(payload)
        self.assertIn('/route/path', view.server.scene.active)
        payload['connectivity_provenance']['edges'][0]['evidence_id'] = ''
        with self.assertRaisesRegex(ValueError, 'unverified_route_edge'):
            view.load_plan(payload)
        self.assertNotIn('/route/path', view.server.scene.active)

    def test_framing_ignores_outlier_only_for_camera_not_geometry(self):
        payload = model()
        payload['points'] = [[i/100., i/100., 1] for i in range(100)]+[[1000000, 0, 0]]
        before = deepcopy(payload)
        center, position, up = framing(payload)
        self.assertLess(np.linalg.norm(position), 10)
        self.assertTrue(np.allclose(up, [0, -1, 0]))
        self.assertEqual(payload, before)

    def test_roi_top_left_and_bottom_right_raw_coordinates(self):
        common = dict(width=400, height=200, camera_position=(0, 0, 1), camera_wxyz=(0, 1, 0, 0),
                      fov=np.pi/2, aspect=2, plane_z=0, render_height=2)
        self.assertTrue(np.allclose(rectangle_to_roi((0, 0), (.25, .25), **common), [0, 0, 100, 50]))
        self.assertTrue(np.allclose(rectangle_to_roi((.75, .75), (1, 1), **common), [300, 150, 400, 200]))
        with self.assertRaisesRegex(ValueError, 'empty_photo_roi'):
            rectangle_to_roi((0, 0), (0, 0), **common)

    def test_concurrent_pose_updates_do_not_remove_inflight_handles(self):
        server = Server()
        view = SceneController(server)
        active, peak = 0, 0
        lock = threading.Lock()
        def slow_add(name, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(.05)
            handle = server.scene.add(name, **kwargs)
            with lock:
                active -= 1
            return handle
        server.scene.add_camera_frustum = slow_add
        threads = [threading.Thread(target=lambda: view.pose('query', np.eye(4), color=(1,2,3), label='test'))
                   for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(peak, 1)


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        cache = Path(__file__).resolve().parents[1]/'.cache'/'tests'
        cache.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.map = self.root/'map'
        self.map.mkdir()
        self.photo = self.root/'raw.png'
        Image.new('RGB', (400, 200), 'red').save(self.photo)
        self.other = self.root/'current.png'
        Image.new('RGB', (400, 200), 'blue').save(self.other)
        self.jobs_patch = patch('go1loc.viser_viewer.JobManager', Jobs)
        self.thread_patch = patch('go1loc.viser_viewer.threading.Thread')
        self.jobs_patch.start(); self.thread_patch.start()
        self.addCleanup(self.jobs_patch.stop); self.addCleanup(self.thread_patch.stop)
        self.server = Server()
        self.app = ViewerApplication(self.map, self.root/'sidecar', server=self.server)
        self.addCleanup(self.app.close)
        payload = model()
        payload['cameras'][0]['image_path'] = str(self.photo)
        payload.update(total_points=1, pts_available=False)
        self.app.controller.replace_model(payload)
        self.app.frame.value = 'a.jpg'
        self.app.photo.value = str(self.photo)
        camera = SimpleNamespace(position=(0, 0, 2), wxyz=(0, 1, 0, 0), fov=np.pi/2, aspect=2)
        self.client = SimpleNamespace(client_id=1, scene=Scene(), camera=camera,
                                      get_render=Mock(return_value=np.zeros((20, 20, 3), np.uint8)))
        self.event = SimpleNamespace(client=self.client, client_id=1)

    def evidence_files(self):
        from go1loc.evidence import digest_json
        from go1loc.video import sha256_file
        query = self.root/'query'
        evidence = {'query_path': str(self.photo), 'selected_cluster': 0,
            'binding': {'query_sha256': sha256_file(self.photo), 'map_hash': 'h', 'files': {}},
            'clusters': [{'cluster_index': 0, 'rows': [
                {'point3D_id': 1, 'xyz': [1, 0, 0], 'pnp_inlier': True, 'uv_original': [12, 25]}]}]}
        evidence['evidence_sha256'] = digest_json(evidence)
        identity = {'map_id': 'm', 'map_hash': 'h', 'component_id': 'c0'}
        result = {**identity, 'pose_status': 'localized', 'T_WC_query': np.eye(4).tolist(),
                  'query_sha256': sha256_file(self.photo), 'capture_time': '2026-10-13T11:00:00Z',
                  'localization_time': '2026-10-13T11:00:01Z', 'quality': {'num_inliers': 10}}
        binding = {'roi': [0, 0, 100, 100], 'evidence_sha256': evidence['evidence_sha256']}
        obj = {**identity, 'status': 'supported', 'query_path': str(self.photo), 'query_dir': str(query),
               'roi_binding': binding, 'roi_sha256': digest_json(binding), 'supporting_point3D_ids': [1],
               'supporting_frame_ids': ['a.jpg'], 'object_surface_position': [1, 0, 0]}
        atomic_json(query/'query_evidence.json', evidence)
        atomic_json(query/'localization_result.json', result)
        atomic_json(self.root/'object_result.json', obj)
        return query, result, obj

    def test_ui_connect_photo_selection_return_and_screenshot(self):
        self.app._connect(self.client)
        self.app._open_photo(self.event)
        self.assertTrue(self.client.scene.active['/roi/photo'].kwargs['image'].shape[1] <= 1600)
        selection = SimpleNamespace(client=self.client, client_id=1, screen_min=(.4, .4), screen_max=(.6, .6))
        self.client.scene.select(selection)
        self.assertLess(self.app.roi_min.value[0], self.app.roi_max.value[0])
        self.app._open_photo(self.event)  # replacing a selection plane, not leaking handles
        self.assertEqual(len(self.client.scene.active), 1)
        self.app._show_frame(self.event)
        self.assertTrue(self.app.reference_image.visible)
        self.app._return_map(self.event)
        self.assertFalse(self.client.scene.active)
        self.app._screenshot(self.event)
        self.assertEqual(len(list(self.app.sidecar.glob('screenshot-*.png'))), 1)
        self.client.scene.select(selection)
        self.assertIn('open_raw_photo', self.app.status.value)

    def test_query_object_and_current_completed(self):
        query, result, obj = self.evidence_files()
        self.app.load_query(query)
        self.assertIn('query', self.app.controller.groups)
        self.app.load_object(self.root/'object_result.json')
        self.assertIn('support', self.app.controller.groups)
        self.assertEqual(self.app.target_result, obj)
        self.app._completed({'kind': 'localize_current', 'result_path': str(query/'localization_result.json')})
        self.assertEqual(self.app.current_start['photo_sha256'], result['query_sha256'])
        self.assertTrue(list(self.app.sidecar.glob('current_start-*.json')))
        self.app._completed({'kind': 'locate_object', 'result_path': str(self.root/'object_result.json')})
        obj['roi_sha256'] = 'tampered'
        atomic_json(self.root/'object_result.json', obj)
        with self.assertRaisesRegex(ValueError, 'object_evidence_binding'):
            self.app.load_object(self.root/'object_result.json')

    def test_submit_validated_object_current_and_planning(self):
        self.app.camera_model.value = 'PINHOLE'
        self.app.camera_params.value = '[100,100,200,100]'
        self.app.calibration.value = '/calibration.json'
        self.app._locate_object(None)
        self.assertEqual(self.app.jobs.submitted[-1][0], 'locate_object')
        self.app.current_photo.value = str(self.photo)
        self.app.capture_time.value = '2026-10-13T11:00:00Z'
        with self.assertRaisesRegex(ValueError, 'independent'):
            self.app._localize_current(None)
        self.app.current_photo.value = str(self.other)
        self.app._localize_current(None)
        self.assertEqual(self.app.jobs.submitted[-1][1]['config']['capture_time'], '2026-10-13T11:00:00Z')
        self.app._manual_start(None)
        self.assertTrue(self.app.current_start['manual'])
        with self.assertRaisesRegex(ValueError, 'requires_explicit_start'):
            self.app._plan_route(None)
        self.app.target_result = {'status': 'insufficient_evidence'}
        self.app.graph_path.value = str(self.root/'graph.json')
        self.app._plan_route(None)  # worker planning gate handles target rejection
        self.assertEqual(self.app.jobs.submitted[-1][0], 'plan_route')

    def test_loaded_and_computed_plans_failure_clears_route(self):
        path = self.root/'plan.json'
        payload = plan_fixture()
        atomic_json(path, payload)
        self.app.plan_path.value = str(path)
        self.app._load_plan(None)
        self.app._save_plan(None)
        self.assertTrue(list(self.app.sidecar.glob('planning_result-*.json')))
        state = {'kind': 'plan_route', 'result_path': str(path)}
        self.app._completed(state)
        self.assertEqual(self.app.plan_result, payload)
        atomic_json(path, {'status': 'rejected', 'failure_reason': 'no_path'})
        self.app._completed(state)
        self.assertIsNone(self.app.plan_result)
        self.assertNotIn('route', self.app.controller.groups)
        self.assertIn('no_path', self.app.status.value)
        with self.assertRaisesRegex(ValueError, 'no_displayed'):
            self.app._save_plan(None)

    def test_mode_completed_reconnect_no_snapshot_and_failure_ui(self):
        path = self.root/'scene.json'
        payload = deepcopy(self.app.controller.model)
        atomic_json(path, payload)
        self.server.get_clients = lambda: {1: self.client}
        self.app._completed({'kind': 'inspect_map', 'result_path': str(path)})
        self.assertIn('unordered', self.app.status.value)
        self.app._load_mode(None)
        self.app.mode.value = 'build_snapshot'
        with self.assertRaisesRegex(ValueError, 'no_ready_snapshot'):
            self.app._load_mode(None)
        self.app.snapshot_dir = self.root/'snapshots'
        self.app._load_mode(None)
        self.assertEqual(self.app.jobs.submitted[-1][0], 'inspect_snapshot')
        query, result, _ = self.evidence_files()
        result.update(pose_status='failed', failure_reason='insufficient_pnp_quality')
        atomic_json(query/'localization_result.json', result)
        self.app.load_query(query)
        self.assertIn('insufficient_pnp_quality', self.app.status.value)
        self.assertNotIn('query', self.app.controller.groups)
        self.app._guard(lambda _: 1/0)(None)
        self.assertIn('ZeroDivisionError', self.app.status.value)

    def test_poll_loop_reports_running_failure_and_completed_without_browser(self):
        def states():
            self.app._stop.set()
            return [{'job_id': '1', 'status': 'running', 'kind': 'inspect_map'},
                    {'job_id': '2', 'status': 'failed', 'error': 'safe failure'}]
        self.app.jobs.list_jobs = states
        self.app._poll()
        self.assertIn('safe failure', self.app.status.value)

    def test_serve_entry_cleanup_on_interrupt(self):
        with patch('go1loc.viser_viewer.ViewerApplication') as factory, \
             patch('go1loc.viser_viewer.time.sleep', side_effect=KeyboardInterrupt):
            serve_viser(self.map, self.root/'server-sidecar', port=8765)
            factory.return_value.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
