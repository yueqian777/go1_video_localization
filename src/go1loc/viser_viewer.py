"""Optional Viser 1.1.1 localhost UI. Offline evidence and route PREVIEW only.

The browser owns only its photo-selection plane and camera view. Global scene
state and bounded worker jobs live on the server and survive browser reconnects.
No active Reconstruction object is ever read by this module.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from functools import wraps
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

import numpy as np

from .jobs import JobManager, atomic_json, ensure_sidecar, read_json, utc_now

POINT_BUDGET = 50000
PHOTO_PLANE_Z = 10000.0


def _rotation(wxyz):
    from scipy.spatial.transform import Rotation
    return Rotation.from_quat(np.asarray(wxyz), scalar_first=True).as_matrix()


def _pose(value):
    T = np.asarray(value, dtype=float)
    if (T.shape != (4, 4) or not np.isfinite(T).all()
            or not np.allclose(T[3], [0, 0, 0, 1])
            or not np.allclose(T[:3, :3].T @ T[:3, :3], np.eye(3), atol=1e-5)
            or not np.isclose(np.linalg.det(T[:3, :3]), 1)):
        raise ValueError('invalid_camera_pose')
    from scipy.spatial.transform import Rotation
    return T, Rotation.from_matrix(T[:3, :3]).as_quat(scalar_first=True)


def _points(value):
    result = np.asarray(value, dtype=float).reshape(-1, 3)
    if not np.isfinite(result).all():
        raise ValueError('nonfinite_geometry')
    return result


def rectangle_to_roi(screen_min, screen_max, *, width, height, camera_position,
                     camera_wxyz, fov, aspect, plane_z=PHOTO_PLANE_Z, render_height=2.):
    """Intersect front-parallel viewport rays with the actual raw-photo plane.

    Handles viewport letterboxing, zoom and panning. If orbiting tilts the photo,
    reject instead of turning a skewed quadrilateral into an inaccurate ROI.
    Coordinates are original (not thumbnail or rectified) image coordinates.
    """
    R = _rotation(camera_wxyz)
    if not np.allclose(R, np.diag([1., -1., -1.]), atol=1e-4):
        raise ValueError('photo_view_not_front_parallel: reopen photo to reset view')
    if width <= 0 or height <= 0 or not 0 < fov < np.pi or aspect <= 0:
        raise ValueError('invalid_photo_projection')
    corners = np.asarray([screen_min, screen_max], dtype=float)
    if corners.shape != (2, 2) or not np.isfinite(corners).all():
        raise ValueError('invalid_rectangle')
    position = np.asarray(camera_position, dtype=float)
    uv = []
    render_width = render_height*width/height
    for sx, sy in corners:
        ray = R @ np.array([(2*sx-1)*aspect*np.tan(fov/2), (2*sy-1)*np.tan(fov/2), 1.])
        distance = (plane_z-position[2])/ray[2]
        if not np.isfinite(distance) or distance <= 0:
            raise ValueError('photo_plane_behind_camera')
        x, y, _ = position + distance*ray
        uv.append([(x/render_width+.5)*width, (.5-y/render_height)*height])
    uv = np.asarray(uv)
    low, high = uv.min(axis=0), uv.max(axis=0)
    low = np.clip(low, [0, 0], [width, height])
    high = np.clip(high, [0, 0], [width, height])
    if np.any(high-low < 1):
        raise ValueError('empty_photo_roi')
    return [float(low[0]), float(low[1]), float(high[0]), float(high[1])]


def _identity(payload, model):
    for key in ('map_id', 'map_hash', 'component_id'):
        if payload.get(key) != model.get(key) or not payload.get(key):
            raise ValueError('result_' + key + '_mismatch')


def validate_planning_preview(payload, model):
    """Validate saved preview contract, not proof of current traversability.

    Trust is limited to provenance carried by the local planning result. This
    function neither invents missing edges nor certifies hand-edited JSON.
    """
    if model.get('mode') != 'final_map_replay':
        raise ValueError('snapshot_alignment_not_established')
    _identity(payload, model)
    if payload.get('status') != 'route_preview_only' or payload.get('execution_permitted') is not False:
        raise ValueError('not_a_nonexecutable_route_preview')
    if not payload.get('connectivity_provenance') or not payload.get('geometry_provenance'):
        raise ValueError('route_provenance_missing')
    if payload.get('units') != model.get('units'):
        raise ValueError('route_units_mismatch')
    path = _points(payload.get('path', []))
    if not len(path) or (len(path) > 1 and not payload.get('route_edge_ids')
                        and not payload['connectivity_provenance'].get('start_attachment')):
        raise ValueError('route_path_or_edges_missing')
    nodes = payload.get('route_node_ids', [])
    edges = payload['connectivity_provenance'].get('edges', [])
    cameras = {c['name']: _pose(c['T_WC'])[0][:3, 3] for c in model['cameras']}
    if (not nodes or any(n not in cameras for n in nodes)
            or len(payload['route_edge_ids']) != len(nodes)-1 or len(edges) != len(nodes)-1):
        raise ValueError('route_nodes_or_provenance_mismatch')
    for i, edge in enumerate(edges):
        if (edge.get('kind') != 'verified_temporal_segment' or not edge.get('evidence_id')
                or edge.get('frame_names') != nodes[i:i+2]):
            raise ValueError('unverified_route_edge')
    attachment = payload['connectivity_provenance'].get('start_attachment', {})
    if attachment.get('kind') == 'manual_exact_node' and attachment.get('manual') is True:
        expected = [cameras[nodes[0]]]
    elif attachment.get('kind') == 'verified_temporal_connection' and attachment.get('verified_continuous') is True:
        expected = list(_points(attachment.get('path', [])))
        if not expected or not np.allclose(expected[-1], cameras[nodes[0]]):
            raise ValueError('route_attachment_endpoint_mismatch')
    else:
        raise ValueError('route_start_attachment_missing')
    expected.extend(cameras[n] for n in nodes[1:])
    if np.asarray(expected).shape != path.shape or not np.allclose(expected, path, atol=1e-6):
        raise ValueError('route_geometry_not_supported_by_provenance')
    return path


def _read_image(path, max_size=1000):
    from PIL import Image
    with Image.open(path) as image:
        image = image.convert('RGB')
        image.thumbnail((max_size, max_size))
        return np.array(image)


def framing(model):
    """Robust display framing only: never discard or alter map geometry."""
    points = _points(model['points'])
    poses = [_pose(c['T_WC'])[0] for c in model['cameras']]
    up = -np.mean([T[:3, 1] for T in poses], axis=0) if poses else np.array([0., 0., 1.])
    up = up / np.linalg.norm(up) if np.linalg.norm(up) > 1e-6 else np.array([0., 0., 1.])
    centers = np.array([T[:3, 3] for T in poses]) if poses else np.zeros((1, 3))
    if len(points):
        low, high = np.percentile(points, [2, 98], axis=0)
        center = (low+high)/2
        extent = max(float(np.linalg.norm(high-low)), 1.)
    else:
        center, extent = np.median(centers, axis=0), 3.
    direction = center-np.median(centers, axis=0)
    if np.linalg.norm(direction) < 1e-6:
        direction = np.mean([T[:3, 2] for T in poses], axis=0) if poses else np.array([0., 1., 0.])
    if np.linalg.norm(direction) < 1e-6:
        direction = np.array([0., 1., 0.])
    direction /= np.linalg.norm(direction)
    return center, center-direction*extent*1.1+up*extent*.15, up


def _scene_locked(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


class SceneController:
    """One replaceable component, persistent across client connections."""
    def __init__(self, server, *, point_budget=POINT_BUDGET):
        self.server = server
        self.point_budget = min(POINT_BUDGET, max(1, point_budget))
        self.groups = {}
        self.model = None
        self.mode = 'final_map_replay'
        self.displayed_points = 0
        self._lock = threading.RLock()
        self.on_reference_click = None

    @_scene_locked
    def clear(self, group):
        for handle in reversed(self.groups.pop(group, [])):
            handle.remove()

    def _keep(self, group, handle):
        self.groups.setdefault(group, []).append(handle)
        return handle

    @_scene_locked
    def set_mode(self, mode, ready_snapshots=()):
        if mode == 'build_snapshot' and not ready_snapshots:
            raise ValueError('no_ready_snapshot')
        if mode not in ('final_map_replay', 'build_snapshot'):
            raise ValueError('invalid_view_mode')
        self.mode = mode

    @_scene_locked
    def replace_model(self, payload):
        points = _points(payload['points'])
        colors = np.asarray(payload['colors'], dtype=np.uint8).reshape(-1, 3)
        if len(points) != len(colors) or len(points) != len(payload['point_ids']):
            raise ValueError('scene_point_arrays_mismatch')
        for camera in payload['cameras']:
            _pose(camera['T_WC'])
        if payload.get('mode') not in ('final_map_replay', 'build_snapshot'):
            raise ValueError('invalid_view_mode')
        indices = np.linspace(0, len(points)-1, min(len(points), self.point_budget), dtype=int)
        with self._lock, self.server.atomic():
            for group in list(self.groups):
                self.clear(group)  # all old BA geometry and final-map overlays retired
            self.model = deepcopy(payload)
            self.mode = payload['mode']
            self.displayed_points = len(indices)
            scene = self.server.scene
            if hasattr(scene, 'set_up_direction'):
                scene.set_up_direction(framing(payload)[2])
            if len(indices):
                self._keep('model', scene.add_point_cloud('/model/points', points=points[indices],
                           colors=colors[indices], point_size=.025))
            for i, camera in enumerate(payload['cameras']):
                T, q = _pose(camera['T_WC'])
                h = self._keep('model', scene.add_camera_frustum('/model/camera/'+str(i),
                    fov=camera['fov'], aspect=camera['aspect'], wxyz=q, position=T[:3, 3],
                    scale=.16, color=(85, 155, 245)))
                h.on_click(lambda event, c=camera: self.on_reference_click(event, c)
                           if self.on_reference_click else None)
            self._keep('model', scene.add_label('/model/label', text=(
                'BUILD SNAPSHOT — final-map alignment unknown' if self.mode == 'build_snapshot'
                else 'FINAL MAP REPLAY — not live reconstruction'), position=(0, 0, 0)))

    @_scene_locked
    def on_client_connect(self, client):
        # Viser's persistent server message buffer performs the actual replay.
        # Re-adding global geometry here would duplicate handles on reconnect.
        return deepcopy(self.model) if self.model else {}

    @_scene_locked
    def pose(self, group, value, *, color, label):
        T, q = _pose(value)
        self.clear(group)
        self._keep(group, self.server.scene.add_camera_frustum('/'+group+'/camera', fov=.8,
                   aspect=1.5, scale=.25, wxyz=q, position=T[:3, 3], color=color))
        self._keep(group, self.server.scene.add_label('/'+group+'/label', text=label, position=T[:3, 3]))

    @_scene_locked
    def load_query(self, payload, *, current=False):
        if not self.model or self.mode != 'final_map_replay':
            raise ValueError('snapshot_alignment_not_established')
        group = 'start' if current else 'query'
        self.clear(group)
        _identity(payload, self.model)
        if payload.get('pose_status') != 'localized' or payload.get('T_WC_query') is None:
            raise ValueError('query_not_localized')
        self.pose(group, payload['T_WC_query'], color=(255, 165, 0) if current else (230, 80, 240),
                  label='CURRENT CAMERA (capture time required)' if current else 'TARGET PHOTO CAMERA (not current)')

    @_scene_locked
    def load_object(self, payload, evidence):
        if not self.model or self.mode != 'final_map_replay':
            raise ValueError('snapshot_alignment_not_established')
        self.clear('support')
        _identity(payload, self.model)
        if payload.get('status') != 'supported':
            return
        ids = set(payload.get('supporting_point3D_ids', []))
        rows = next((c['rows'] for c in evidence['clusters']
                     if c['cluster_index'] == evidence.get('selected_cluster')), [])
        support = {r['point3D_id']: r['xyz'] for r in rows
                   if r['point3D_id'] in ids and r.get('pnp_inlier')}
        if support:
            self._keep('support', self.server.scene.add_point_cloud('/support/points',
                       points=_points(list(support.values())), colors=(255, 210, 40), point_size=.065))
        if payload.get('object_surface_position') is not None:
            self._keep('support', self.server.scene.add_label('/support/label',
                text='Visible surface support — NOT full object center', position=payload['object_surface_position']))
        # Observing reference cameras are candidate goals, never the object centroid.
        names = set(payload.get('supporting_frame_ids', []))
        for i, c in enumerate(self.model['cameras']):
            if c['name'] in names:
                T, q = _pose(c['T_WC'])
                self._keep('support', self.server.scene.add_camera_frustum('/support/observer/'+str(i),
                    fov=c['fov'], aspect=c['aspect'], wxyz=q, position=T[:3, 3], scale=.2, color=(80, 230, 110)))

    @_scene_locked
    def load_plan(self, payload):
        self.clear('route')
        self.clear('goal')
        path = validate_planning_preview(payload, self.model or {})
        if len(path) > 1:
            self._keep('route', self.server.scene.add_line_segments('/route/path',
                points=np.stack((path[:-1], path[1:]), axis=1), colors=(30, 225, 220),
                thickness=3., thickness_units='screen'))
        self._keep('route', self.server.scene.add_label('/route/label',
            text='CAMERA ROUTE PREVIEW ONLY — execution_permitted=false; '+payload['units'], position=path[0]))
        if payload.get('start_camera_pose') is not None:
            self.pose('start', payload['start_camera_pose'], color=(255, 165, 0), label='Plan start: '+str(payload.get('start_source')))
        if payload.get('goal_observation_pose') is not None:
            self.pose('goal', payload['goal_observation_pose'], color=(80, 230, 110), label='Goal observation camera')


class ViewerApplication:
    def __init__(self, map_dir, sidecar_dir, *, port=8765, query_dir=None, snapshot_dir=None, server=None):
        self.map_dir = Path(map_dir).resolve()
        self.sidecar = ensure_sidecar(self.map_dir, sidecar_dir)
        self.sidecar.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir = Path(snapshot_dir).resolve() if snapshot_dir else None
        if not 1 <= port <= 65535:
            raise ValueError('invalid_port')
        if server is None:
            try:
                import viser
            except ImportError as exc:
                raise RuntimeError('Optional UI missing: install viser==1.1.1 in the project runtime') from exc
            server = viser.ViserServer(host='127.0.0.1', port=port, label='Go1 offline evidence viewer')
        self.server = server
        self.jobs = JobManager(self.map_dir, self.sidecar)
        self.controller = SceneController(server)
        self.controller.on_reference_click = self._reference_clicked
        self._stop = threading.Event()
        self._seen = {j['job_id'] for j in self.jobs.list_jobs() if j['status'] in ('completed', 'failed')}
        self._job_roles = {}
        self._photo_states = {}
        self._pending_query = Path(query_dir) if query_dir else None
        self.plan_result = None
        self.target_result = None
        self.target_photo = None
        self.current_start = None
        self._ui()
        server.on_client_connect(self._connect)
        self._submit('inspect_map', {})
        self._thread = threading.Thread(target=self._poll, name='go1loc-viewer-poll', daemon=True)
        self._thread.start()

    def _message(self, text):
        self.status.value = str(text)[:8000]

    def _guard(self, callback):
        def guarded(event):
            try:
                callback(event)
            except Exception as exc:
                self._message(f'{type(exc).__name__}: {exc}')
        return guarded

    def _button(self, label, callback):
        handle = self.server.gui.add_button(label)
        handle.on_click(self._guard(callback))
        return handle

    def _ui(self):
        gui = self.server.gui
        gui.add_markdown('## Go1 offline evidence viewer\nLocalhost only. No robot controls.\n'
                         'Historical camera routes are previews, not clearance or walking commands.')
        self.status = gui.add_text('Status / counts / errors', 'Loading frozen map in worker...', multiline=True, disabled=True)
        self.mode = gui.add_dropdown('View mode', ['final_map_replay', 'build_snapshot'])
        self.component = gui.add_text('Component ID (blank = first)', '')
        self.snapshot_index = gui.add_number('Snapshot list index (-1 = latest)', -1, step=1, min=-1)
        self._button('Load selected mode / component', self._load_mode)
        self.frame = gui.add_dropdown('Reference frame / real PTS', ['(loading)'])
        self._button('Show reference photo and pose', self._show_frame)
        self.reference_image = gui.add_image(np.zeros((2, 2, 3), np.uint8), label='Reference photo', visible=False)
        gui.add_markdown('Final replay shows the FINAL cloud at all times; selecting keyframes does not pretend to reconstruct it.')
        self.photo = gui.add_text('Target raw photo path', '')
        self._button('Open raw photo for visual ROI (Shift + drag)', self._open_photo)
        self.roi_min = gui.add_vector2('ROI top-left raw pixels', (0., 0.))
        self.roi_max = gui.add_vector2('ROI bottom-right raw pixels', (1., 1.))
        self.query_path = gui.add_text('Saved query directory (optional reuse)', str(self._pending_query or ''))
        self._button('Load saved query pose / evidence', lambda _: self.load_query(Path(self.query_path.value)))
        self.mask_path = gui.add_text('Original-size mask path (optional)', '')
        self.cluster_radius = gui.add_number('Cluster radius map_units (0 = automatic)', 0., min=0., step=.05)
        self.minimum_points = gui.add_number('Minimum support points', 3, min=3, max=1000, step=1)
        self.camera_model = gui.add_text('Query camera model (optional)', '')
        self.camera_params = gui.add_text('Query camera params JSON (optional)', '')
        self.calibration = gui.add_text('Query calibration path (optional)', '')
        self._button('Compute photo ROI -> 3D support', self._locate_object)
        self.object_path = gui.add_text('Saved object_result JSON path', '')
        self._button('Load saved object support', lambda _: self.load_object(Path(self.object_path.value)))
        self.evidence_image = gui.add_image(np.zeros((2, 2, 3), np.uint8), label='Measured matches: green=inlier, red=rejected', visible=False)
        self._button('Return to 3D map', self._return_map)
        self.current_photo = gui.add_text('INDEPENDENT current photo path', '')
        self.capture_time = gui.add_text('Actual capture time ISO8601 + timezone', '',
            hint='Required; never auto-filled with localization time. Old photos are not current.')
        self._button('Localize independent current photo', self._localize_current)
        self._button('Use selected reference as MANUAL start', self._manual_start)
        self.graph_path = gui.add_text('Verified route_graph JSON path', '')
        self._button('Compute route preview from start + target', self._plan_route)
        self.plan_path = gui.add_text('Saved planning_result JSON path', '')
        self._button('Load route preview JSON', self._load_plan)
        self._button('Save displayed planning JSON to sidecar', self._save_plan)
        self._button('Save screenshot to sidecar', self._screenshot)

    def _submit(self, kind, params, role=None):
        job = self.jobs.submit(kind, params)
        self._job_roles[job['job_id']] = role or kind
        self._message(f"Queued {kind}; job={job['job_id']}. Browser disconnect does not cancel it.")
        return job

    def _poll(self):
        while not self._stop.is_set():
            try:
                for state in self.jobs.list_jobs():
                    job_id = state['job_id']
                    if job_id in self._seen:
                        continue
                    if state['status'] == 'completed':
                        self._seen.add(job_id)
                        self._completed(state)
                    elif state['status'] == 'failed':
                        self._seen.add(job_id)
                        self._message('Job failed: '+state.get('error', 'unknown'))
                    elif state['status'] == 'running':
                        self._message(f"Running {state['kind']}; pid={state.get('worker_pid')}; job={job_id}")
            except Exception as exc:
                self._message(f'{type(exc).__name__}: {exc}')
            self._stop.wait(.5)

    def _completed(self, state):
        result_path = Path(state['result_path'])
        payload = read_json(result_path)
        kind = state['kind']
        if kind in ('inspect_map', 'inspect_snapshot'):
            self.controller.replace_model(payload)
            self.plan_result = self.current_start = None
            self.target_result = None
            self.frame.options = [c['name'] for c in payload['cameras']] or ['(no frames)']
            self.frame.value = self.frame.options[0]
            for client in self.server.get_clients().values():
                self._focus_map(client)
            self._message(f"{payload['mode']} | {len(payload['cameras'])} cameras | "
                          f"{self.controller.displayed_points}/{payload['total_points']} points | "
                          f"PTS {'available' if payload['pts_available'] else 'unavailable (unordered photos)'} | "
                          f"component {payload['component_id']} | {payload['units']}")
            if self._pending_query and self.controller.mode == 'final_map_replay':
                pending, self._pending_query = self._pending_query, None
                self.load_query(pending)
        elif kind == 'locate_object':
            self.load_object(result_path)
        elif kind == 'plan_route':
            self.plan_result = None
            self.controller.clear('route')
            self.controller.clear('goal')
            self.plan_path.value = str(result_path)
            if payload.get('status') == 'route_preview_only':
                self.controller.load_plan(payload)
                self.plan_result = payload
                self._message('Computed camera route preview; execution_permitted=false.\n'+str(result_path))
            else:
                self._message('Route rejected: '+str(payload.get('failure_reason'))+'\n'+str(result_path))
        else:
            self.controller.load_query(payload, current=True)
            self.current_start = {**payload, 'source': 'current_photo',
                'captured_at': payload.get('capture_time'), 'localized_at': payload.get('localization_time'),
                'photo_sha256': payload.get('query_sha256'),
                'T_MC': payload.get('T_WC_query')}
            atomic_json(self.sidecar/('current_start-'+uuid4().hex+'.json'), self.current_start)
            self._message('Independent current camera localized. No automatic route attachment.\n'+str(result_path))

    def _connect(self, client):
        self.controller.on_client_connect(client)
        self._focus_map(client)
        @client.scene.on_rect_select(modifier='shift')
        def selected(event):
            try:
                state = self._photo_states.get(event.client_id)
                if not state:
                    raise ValueError('open_raw_photo_before_selecting')
                c = event.client.camera
                roi = rectangle_to_roi(event.screen_min, event.screen_max, width=state['width'], height=state['height'],
                    camera_position=c.position, camera_wxyz=c.wxyz, fov=c.fov, aspect=c.aspect)
                self.roi_min.value, self.roi_max.value = tuple(roi[:2]), tuple(roi[2:])
                self._message('Selected raw-photo ROI: '+str([round(x, 2) for x in roi]))
            except Exception as exc:
                self._message(str(exc))

    def _focus_map(self, client):
        model = self.controller.model
        if not model:
            return
        center, position, up = framing(model)
        client.camera.position = position
        client.camera.up_direction = up
        client.camera.look_at = center
        client.camera.fov = .9

    def _reference(self):
        if not self.controller.model:
            raise ValueError('map_not_loaded')
        return next(c for c in self.controller.model['cameras'] if c['name'] == self.frame.value)

    def _reference_clicked(self, event, camera):
        self.frame.value = camera['name']
        self._show_frame(event)

    def _show_frame(self, event):
        camera = self._reference()
        if camera.get('image_path') and Path(camera['image_path']).is_file():
            self.reference_image.image = _read_image(camera['image_path'])
            self.reference_image.visible = True
        if event.client:
            T, q = _pose(camera['T_WC'])
            event.client.camera.position = T[:3, 3]
            event.client.camera.wxyz = q
            event.client.camera.fov = camera['fov']
        self._message(f"Reference {camera['name']}; video PTS={camera.get('pts_time_s')} s; "
                      f"mode={self.controller.mode}. Reference camera is NOT current pose.")

    def _load_mode(self, _):
        if self.mode.value == 'build_snapshot':
            if not self.snapshot_dir:
                raise ValueError('no_ready_snapshot: snapshot directory not supplied')
            # Worker validates READY and all native hashes, never a live model.
            self._submit('inspect_snapshot', {'snapshot_dir': str(self.snapshot_dir),
                'snapshot_index': int(self.snapshot_index.value), 'point_budget': POINT_BUDGET})
        else:
            self._submit('inspect_map', {'component_id': self.component.value.strip() or None})

    def _open_photo(self, event):
        from PIL import Image
        if event.client is None:
            raise ValueError('photo_selection_requires_browser_client')
        path = Path(self.photo.value).resolve()
        with Image.open(path) as image:
            width, height = image.size
        client = event.client
        old = self._photo_states.pop(client.client_id, None)
        if old:
            old['handle'].remove()
        handle = client.scene.add_image('/roi/photo', _read_image(path, 1600),
            render_width=2.*width/height, render_height=2., position=(0., 0., PHOTO_PLANE_Z),
            wxyz=(0., 1., 0., 0.),  # Viser image local axes use OpenCV y-down.
            cast_shadow=False, receive_shadow=False)
        self._photo_states[client.client_id] = {'path': path, 'width': width, 'height': height, 'handle': handle}
        client.camera.position = (0., 0., PHOTO_PLANE_Z+3.5)
        client.camera.up_direction = (0., 1., 0.)
        client.camera.look_at = (0., 0., PHOTO_PLANE_Z)
        client.camera.wxyz = (0., 1., 0., 0.)
        client.camera.fov = .9
        self.roi_min.value, self.roi_max.value = (0., 0.), (float(width), float(height))
        self._message('RAW PHOTO: Shift + drag a rectangle. Do not orbit; reopen photo to reset orientation.')

    def _return_map(self, event):
        if event.client:
            state = self._photo_states.pop(event.client_id, None)
            if state:
                state['handle'].remove()
            self._focus_map(event.client)

    def _config(self, capture_time=None):
        config = {}
        if self.camera_model.value.strip():
            config['query_camera_model'] = self.camera_model.value.strip()
            config['query_camera_params'] = json.loads(self.camera_params.value)
        if self.calibration.value.strip():
            config['calibration_path'] = self.calibration.value.strip()
        if capture_time:
            config['capture_time'] = capture_time
        return config

    def _require_final(self):
        if self.controller.mode != 'final_map_replay' or not self.controller.model:
            raise ValueError('final_map_required: snapshot_alignment_not_established')

    def _locate_object(self, _):
        self._require_final()
        self.controller.clear('support')
        self.controller.clear('route')
        self.controller.clear('goal')
        self.plan_result = None
        self.target_result = None
        self._submit('locate_object', {'photo': str(Path(self.photo.value).resolve()),
            'roi': [*self.roi_min.value, *self.roi_max.value],
            'query_dir': self.query_path.value.strip() or None, 'mask_path': self.mask_path.value.strip() or None,
            'cluster_radius': float(self.cluster_radius.value) or None,
            'min_points': int(self.minimum_points.value), 'config': self._config()})

    def load_query(self, directory):
        self._require_final()
        payload = read_json(Path(directory)/'localization_result.json')
        if any(payload.get(k) != self.controller.model[k] for k in ('map_id', 'map_hash')):
            raise ValueError('query_map_mismatch')
        evidence_path = Path(directory)/'query_evidence.json'
        if evidence_path.exists():
            evidence = read_json(evidence_path)
            if evidence.get('query_path'):
                self.photo.value = evidence['query_path']
                self.target_photo = Path(evidence['query_path']).resolve()
        self.query_path.value = str(directory)
        self._show_evidence(Path(directory))
        if payload.get('pose_status') != 'localized':
            self.controller.clear('query')
            self.controller.clear('support')
            self._message('Query failed: '+str(payload.get('failure_reason'))+'\n'+json.dumps(payload.get('quality', {})))
            return
        self.controller.load_query(payload)
        self._message('Query camera (NOT current pose): '+str(payload.get('pose_status'))+'\n'+json.dumps(payload.get('quality', {})))

    def _show_evidence(self, directory, roi=None):
        evidence_path = directory/'query_evidence.json'
        if not evidence_path.exists() or not self.photo.value.strip():
            return
        from .evidence import digest_json, verify_binding
        evidence = read_json(evidence_path)
        if digest_json({k: v for k, v in evidence.items() if k != 'evidence_sha256'}) != evidence.get('evidence_sha256'):
            raise ValueError('evidence_content_hash_mismatch')
        verify_binding(evidence['binding'], Path(self.photo.value), self.controller.model['map_hash'])
        from .diagnostics import render_query_evidence
        destination = self.sidecar/('evidence-'+uuid4().hex+'.png')
        render_query_evidence(Path(self.photo.value), evidence, destination, roi=roi)
        self.evidence_image.image = _read_image(destination, 1600)
        self.evidence_image.visible = True

    def load_object(self, path):
        self._require_final()
        self.target_result = None
        self.controller.clear('support')
        self.controller.clear('route')
        self.controller.clear('goal')
        self.plan_result = None
        payload = read_json(path)
        if payload.get('query_dir'):
            self.target_photo = Path(payload['query_path']).resolve()
            self.photo.value = str(self.target_photo)
            query_dir = Path(payload['query_dir'])
            self.load_query(query_dir)  # verifies evidence bindings before rendering
            evidence = read_json(query_dir/'query_evidence.json')
            from .evidence import digest_json
            binding = payload.get('roi_binding', {})
            if (binding.get('evidence_sha256') != evidence['evidence_sha256']
                    or digest_json(binding) != payload.get('roi_sha256')):
                raise ValueError('object_evidence_binding_mismatch')
            self.controller.load_object(payload, evidence)
            self._show_evidence(query_dir, roi=binding.get('roi'))
            self.target_result = payload
        self.object_path.value = str(path)
        self._message('Object result: '+payload['status']+'; '+str(payload.get('failure_reason'))+
                      '\n'+str(path)+'\nVisible surface support only; historical observations.')

    def _plan_route(self, _):
        self._require_final()
        if self.current_start is None or self.target_result is None:
            raise ValueError('route_requires_explicit_start_and_object_evidence')
        self.controller.clear('route')
        self.controller.clear('goal')
        self.plan_result = None
        self._submit('plan_route', {'graph_path': str(Path(self.graph_path.value).resolve()),
                                   'start': self.current_start, 'target': self.target_result})

    def _localize_current(self, _):
        self._require_final()
        photo = Path(self.current_photo.value).resolve()
        target = self.target_photo or (Path(self.photo.value).resolve() if self.photo.value.strip() else None)
        from .video import sha256_file
        if target and (photo == target or sha256_file(photo) == sha256_file(target)):
            raise ValueError('current_photo_must_be_independent_of_target')
        capture = self.capture_time.value.strip()
        stamp = datetime.fromisoformat(capture.replace('Z', '+00:00'))
        if not stamp.tzinfo:
            raise ValueError('capture_time_requires_timezone')
        self.current_start = None
        self.controller.clear('start')
        self.controller.clear('route')
        self.controller.clear('goal')
        self.plan_result = None
        self._submit('localize_current', {'photo': str(photo), 'config': self._config(capture)})

    def _manual_start(self, _):
        self._require_final()
        camera = self._reference()
        self.controller.clear('route')
        self.controller.clear('goal')
        self.plan_result = None
        self.current_start = {k: self.controller.model[k] for k in ('map_id', 'map_hash', 'component_id')}
        self.current_start.update(source='explicit_pose', manual=True, node_id=camera['name'],
                                  T_MC=camera['T_WC'], selected_at=utc_now())
        self.controller.pose('start', camera['T_WC'], color=(255, 165, 0), label='MANUAL camera start — not measured current pose')
        path = self.sidecar/('manual_start-'+uuid4().hex+'.json')
        atomic_json(path, self.current_start)
        self._message('Saved explicitly MANUAL start: '+str(path))

    def _load_plan(self, _):
        payload = read_json(self.plan_path.value)
        self.plan_result = None
        self.controller.load_plan(payload)
        self.plan_result = payload
        self._message('Loaded saved CAMERA ROUTE PREVIEW. execution_permitted=false.\n'
                      'Source provenance is retained; historical route does not prove current clearance.')

    def _save_plan(self, _):
        if not self.plan_result:
            raise ValueError('no_displayed_planning_result')
        path = self.sidecar/('planning_result-'+uuid4().hex+'.json')
        atomic_json(path, self.plan_result)
        self._message('Saved '+str(path))

    def _screenshot(self, event):
        if not event.client:
            raise ValueError('screenshot_requires_browser_client')
        from PIL import Image
        image = event.client.get_render(height=720, width=1280, timeout=15.)
        path = self.sidecar/('screenshot-'+uuid4().hex+'.png')
        Image.fromarray(image).save(path)
        self._message('Saved 3D canvas screenshot (not sidebar): '+str(path))

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.jobs.close()  # deliberately do not kill compute when UI stops
        self.server.stop()


def serve_viser(map_dir, sidecar_dir, port=8765, query_dir=None, snapshot_dir=None):
    """Blocking CLI entry. Bind only 127.0.0.1; Ctrl+C closes UI, not workers."""
    app = ViewerApplication(map_dir, sidecar_dir, port=port, query_dir=query_dir, snapshot_dir=snapshot_dir)
    try:
        while True:
            time.sleep(.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
