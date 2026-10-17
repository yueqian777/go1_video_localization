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
from .viewer_workflows import RouteWorkflowControls
from .viewer_text import (VIEW_MODES, human_status, protocol_value, save_uploaded_image,
                          query_failure_notice, query_camera_notice, object_failure_notice)
from .playback import PlaybackControls
from .build_guide import BuildGuideControls
from .web_build_ui import WebBuildControls
from .photo_selection import PhotoSelectionControls, PHOTO_PLANE_Z
from .navigation_ui import NavigationControls

POINT_BUDGET = 50000


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

    @_scene_locked
    def on_client_connect(self, client):
        # Viser's persistent server message buffer performs the actual replay.
        # Re-adding global geometry here would duplicate handles on reconnect.
        return deepcopy(self.model) if self.model else {}

    @_scene_locked
    def pose(self, group, value, *, color, label=None):
        # The workbench omits labels; explicit API callers can still request one.
        T, q = _pose(value)
        self.clear(group)
        self._keep(group, self.server.scene.add_camera_frustum('/'+group+'/camera', fov=.8,
                   aspect=1.5, scale=.25, wxyz=q, position=T[:3, 3], color=color))
        if label:
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
        self.pose(group, payload['T_WC_query'], color=(255, 165, 0) if current else (230, 80, 240))

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
        if payload.get('start_camera_pose') is not None:
            self.pose('start', payload['start_camera_pose'], color=(255, 165, 0))
        if payload.get('goal_observation_pose') is not None:
            self.pose('goal', payload['goal_observation_pose'], color=(80, 230, 110))


def _app_locked(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._state_lock:
            return method(self, *args, **kwargs)
    return locked


class ViewerApplication(NavigationControls, PhotoSelectionControls, RouteWorkflowControls, PlaybackControls, BuildGuideControls, WebBuildControls):
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
            server = viser.ViserServer(host='127.0.0.1', port=port, label='Go1 地图与照片定位工作台')
        self.server = server
        self._state_lock = threading.RLock()
        self._task_revision = 0
        self._job_revisions = {}
        self.query_result = None
        self._query_selected = False
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
        self._start_playback_thread()

    def _message(self, text):
        self.status.value = str(text)[:8000]
        self.notice.value = human_status(text)
        self._refresh_overview()

    def _guard(self, callback):
        def guarded(event):
            try:
                with self._state_lock:
                    callback(event)
                    self._refresh_overview()
            except Exception as exc:
                self._message(f'{type(exc).__name__}: {exc}')
        return guarded

    def _button(self, label, callback):
        handle = self.server.gui.add_button(label)
        handle.on_click(self._guard(callback))
        return handle

    def _ui(self):
        gui = self.server.gui
        gui.configure_theme(control_width='large', show_share_button=False, brand_color=(31, 116, 151))
        gui.add_markdown('## Go1 地图与照片定位\n**视觉方向引导 · 不会控制机器狗**')
        self.notice = gui.add_text('当前提示', '正在读取地图，请稍候。', multiline=True, disabled=True)
        self.overview = gui.add_markdown('地图载入后显示任务进度。')
        self._button('恢复地图总览', self._return_map)
        self._web_build_ui()
        self.navigation_panel = gui.add_folder('开始找物与方向引导', expand_by_default=True)
        self.research_mode = gui.add_checkbox('显示研究模式与旧流程（人工起点、逐段审核、证据导入）', False)
        with gui.add_folder('新手先看：图中是什么？', expand_by_default=False):
            gui.add_markdown('**散点**：从多张照片估计的三维特征，不是完整墙面；没有点不代表没有障碍。\n\n'
                '**蓝色线框**：历史相机位置和朝向；**黄色点**：目标表面证据；**绿色相机**：目标观察位置。\n\n'
                '**紫色线**：待审核历史路段；**青色线**：通过证据检查的离线路线预览。\n\n'
                '鼠标拖动旋转，滚轮缩放。视角迷路时点击「恢复地图总览」。没有米制尺度时使用地图单位，不当作米。')
        tabs = gui.add_tab_group()
        tabs.visible = False
        self.research_mode.on_update(self._guard(lambda _: setattr(tabs, 'visible', self.research_mode.value)))
        self._map_tab, self._object_tab, self._start_tab, self._review_tab, self._plan_tab = [
            tabs.add_tab(name) for name in ['① 地图', '② 找物', '③ 起点', '④ 路段', '⑤ 路线']]
        with self._map_tab:
            gui.add_markdown('### 回看环境与拍摄位置\n先选模式再加载。最终地图回看不是实时建图；过程快照是建图时保存的完整阶段结果，与最终地图的对齐关系未知。')
            self.mode = gui.add_dropdown('查看内容', list(VIEW_MODES))
            self.snapshot_index = gui.add_number('快照序号（0 起；-1 为最新）', -1, step=1, min=-1,
                hint='仅在「建图过程快照」模式使用；需要启动时已指定快照目录。')
            with gui.add_folder('高级：地图分量', expand_by_default=False):
                self.component = gui.add_text('分量编号（留空使用第一个）', '')
            self._button('加载所选地图或快照', self._load_mode)
            self.frame = gui.add_dropdown('选择历史参考帧', ['（正在载入）'])
            self.follow_reference = gui.add_checkbox('同时进入该相机视角（可选）', False)
            self._button('查看参考照片与拍摄位置', self._show_frame)
            self.reference_image = gui.add_image(np.zeros((2, 2, 3), np.uint8), label='历史参考照片（不是实时画面）', visible=False)
            self.reference_info = gui.add_text('所选参考帧信息', '请选择参考帧并点击查看。', multiline=True, disabled=True)
            gui.add_markdown('参考帧的时间是素材记录时间。屏幕录制的显示时间不等于传感器曝光时间。')
            self._playback_ui()
            self._build_guide_ui()
        with self._object_tab:
            gui.add_markdown('### 用独立照片寻找物体\n**选照片 → 打开原图 → 鼠标左键拖出绿框 → 计算。**\n\n'
                '正式验证请使用未参与建图的独立采集照片。输出是可见表面的三维支持点，不是自动识别出的完整物体。')
            self.photo = gui.add_text('目标原图路径', '')
            self._photo_upload('选择目标照片', self.photo)
            self._button('打开原图并框选目标', self._open_photo)
            self._selection_ui()
            self.evidence_image = gui.add_image(np.zeros((2, 2, 3), np.uint8), label='匹配证据：绿色通过，红色排除', visible=False)
            with gui.add_folder('复用已有查询或目标结果', expand_by_default=False):
                self.query_path = gui.add_text('查询结果文件夹', str(self._pending_query or ''))
                self.reuse_query = gui.add_checkbox('复用已载入且定位成功的证据（不重新求解照片）', False)
                self._button('载入已有照片定位证据', lambda _: self.load_query(Path(self.query_path.value)))
                self.object_path = gui.add_text('目标结果文件（object_result.json）', '')
                self._button('载入已有目标支持点', lambda _: self.load_object(Path(self.object_path.value)))
            with gui.add_folder('高级：掩膜、标定与支持点参数', expand_by_default=False):
                gui.add_markdown('没有可靠标定时留空：将从图像与地图匹配估计并优化焦距，不把默认猜测值固定为标定。'
                    '不会仅凭相同分辨率自动套用地图相机参数。已提供的标定/内参保持固定，不重复去畸变。'
                    '此处标定参数也供独立当前照片使用；修改后应重新计算照片。')
                self.query_camera_info = gui.add_text('本次查询采用的相机参数', '未求解。无标定时自动估计焦距。', multiline=True, disabled=True)
                self.mask_path = gui.add_text('原尺寸特征排除掩膜（可选）', '')
                self.cluster_radius = gui.add_number('聚类半径（地图单位；0 自动）', 0., min=0., step=.05)
                self.minimum_points = gui.add_number('最少支持点数', 3, min=3, max=1000, step=1)
                self.camera_model = gui.add_text('相机模型（可选，如 PINHOLE）', '')
                self.camera_params = gui.add_text('相机内参 JSON 数组（可选）', '')
                self.calibration = gui.add_text('标定文件路径（可选）', '')
        with self._start_tab:
            gui.add_markdown('### 路线从哪里出发？\n**入门方法：人工历史起点。** 在「① 地图」选择参考帧，再点下面按钮。它不是机器狗的实时位置。')
            self._button('将选中参考帧设为人工起点', self._manual_start)
            with gui.add_folder('进阶：定位独立当前照片', expand_by_default=False):
                gui.add_markdown('目标照片不能冒充当前位置。照片定位后仍需合法接入证据，不会自动吸附最近节点。')
                self.current_photo = gui.add_text('独立当前照片路径', '')
                self._photo_upload('选择独立当前照片', self.current_photo)
                self.capture_time = gui.add_text('真实拍摄时间（含时区）', '',
                    hint='格式如 2026-10-14T12:00:00+08:00。示例不是你的采集时间；未知时不能编造。')
                self._button('定位独立当前照片', self._localize_current)
        with self._plan_tab:
            gui.add_markdown('### 检查准备情况并预览路线\n需要：已支持的目标、明确起点、已审核路线图。独立照片起点还需要合法接入证据。\n\n'
                '**终点是能观察目标的参考相机位置，不是物体中心。所有结果禁止直接用于机器人执行。**')
            self.graph_path = gui.add_text('已审核路线图文件', '', hint='在「④ 路段」保存后自动填入；也可填已有 route_graph.json。')
            self.route_avoidance = gui.add_checkbox('按机载局部避障可用的前提预览（未读取实机状态）', False)
            self.route_avoidance.on_update(self._guard(lambda _: self._invalidate_plan()))
            gui.add_markdown('全局路线沿已审核历史路段选择最短路；局部避障处理执行时的近处障碍。'
                             '无需预先证明每处当前空旷，但避障不能把断路或隔墙连线变成通路。本页不控制机器狗。')
            self._route_disabled_edges = set()
            self._route_edge_options = {}
            self._route_edges_graph = None
            with gui.add_folder('路段封闭与重新规划', expand_by_default=False):
                self._button('读取可选路段', self._route_refresh_edges)
                self.route_edge_choice = gui.add_dropdown('选择要排除的有向路段', ['（先读取路线图）'])
                self._button('本次避开这条路段', self._route_disable_edge)
                self._button('取消本次所有路段排除', self._route_restore_edges)
                self.route_blocked_info = gui.add_text('本次排除', '无', multiline=True, disabled=True)
            self.route_summary = gui.add_text('路线摘要与途经点', '计算后显示距离、途经点和转角。', multiline=True, disabled=True)
            self._button('计算离线路线预览', self._plan_route)
            self._button('导出当前路线结果', self._save_plan)
            with gui.add_folder('载入已有路线结果', expand_by_default=False):
                self.plan_path = gui.add_text('路线结果文件（planning_result.json）', '')
                self._button('校验并载入已有路线', self._load_plan)
        self._workflow_ui()
        with gui.add_folder('导出与诊断详情', expand_by_default=False):
            self._button('保存三维画布截图', self._screenshot)
            gui.add_markdown('截图仅包含三维画布，不包含面板。下方保留原始英文错误码和文件位置，便于复核。')
            self.status = gui.add_text('原始诊断记录', '', multiline=True, disabled=True)
            self.map_directory = gui.add_text('当前地图目录', str(self.map_dir), disabled=True)
            self.results_directory = gui.add_text('结果保存目录', str(self.sidecar), disabled=True)
        self._navigation_ui()

    def _photo_upload(self, label, field):
        button = self.server.gui.add_upload_button(label, mime_type='image/png,image/jpeg',
            hint='仅复制到本机工作目录；原文件不改动，不上传云端。最大 32 MiB。')
        def received(event):
            upload = event.target.value
            if upload is None:
                return
            path = save_uploaded_image(self.sidecar, upload.name, upload.content)
            if field is self.photo:
                self._invalidate_target(clear_query=True)
                self._query_selected = True
                self.query_path.value = ''
            elif field is self.current_photo:
                self._task_revision += 1
                self.current_start = None
                self.controller.clear('start')
                self._invalidate_plan()
                self._clear_connection_preview()
            field.value = str(path)
            self._message('照片已复制到本地工作目录，原始字节保持不变。请继续打开原图或填写真实拍摄时间。')
        button.on_upload(self._guard(received))

    def _refresh_overview(self):
        model = self.controller.model
        map_state = ('最终地图已加载' if self.controller.mode == 'final_map_replay' else '过程快照（仅查看）') if model else '地图待加载'
        target = '目标已有支持点' if (self.target_result or {}).get('status') == 'supported' else '目标待求解'
        if hasattr(self, 'research_mode') and not self.research_mode.value:
            self.overview.content = f'{map_state} · {target}\n\n默认使用自动路线关联，不要求人工选起点或逐段审核。'
            return
        start = ('人工历史起点' if self.current_start.get('manual') else '独立照片起点') if self.current_start else '起点待设置'
        review = getattr(self, 'route_review', None) or {}
        segments = len(review.get('route_segments', []))
        self.overview.content = f'**{map_state}** · {target} · {start}\n\n已确认历史路段：{segments} 段（不等于当前可通行）'

    def _submit(self, kind, params, role=None):
        job = self.jobs.submit(kind, params)
        self._job_revisions[job['job_id']] = self._task_revision
        self._job_roles[job['job_id']] = role or kind
        self._message(f"Queued {kind}; job={job['job_id']}. Browser disconnect does not cancel it.")
        return job

    def _poll(self):
        while not self._stop.is_set():
            try:
                with self._state_lock:
                    for state in self.jobs.list_jobs():
                        job_id = state['job_id']
                        if job_id in self._seen:
                            continue
                        if state['status'] == 'completed':
                            self._seen.add(job_id)
                            self._completed(state)
                        elif state['status'] == 'failed':
                            self._seen.add(job_id)
                            if (state.get('kind') == 'locate_object'
                                    and self._job_revisions.get(job_id) == self._task_revision):
                                self.roi_status.value = '计算失败，选框仍保留；请查看诊断后重试。'
                                self.roi_submit.disabled = self._roi_selection is None
                            self._message('Job failed: '+state.get('error', 'unknown'))
                        elif state['status'] == 'running':
                            self._message(f"Running {state['kind']}; pid={state.get('worker_pid')}; job={job_id}")
                    self._web_tick()
                    self._nav_tick()
            except Exception as exc:
                self._message(f'{type(exc).__name__}: {exc}')
            self._stop.wait(.5)

    @_app_locked
    def _completed(self, state):
        if (state.get('job_id') is not None
                and self._job_revisions.get(state['job_id']) != self._task_revision):
            self._message('Ignored completed job from an obsolete task; saved files retained: '+state['job_id'])
            return
        if state['kind'] in ('prepare_route_review', 'save_route_review') and not self._review_completion_current(state):
            self._message('Ignored obsolete review generation/save; current decisions kept, saved files retained: '+state['job_id'])
            return
        result_path = Path(state['result_path'])
        payload = read_json(result_path)
        kind = state['kind']
        if kind in ('inspect_map', 'inspect_snapshot', 'inspect_build_snapshot'):
            self._invalidate_target(clear_query=True)
            self._query_selected = False
            self.controller.replace_model(payload)
            self._web_applied_job = state.get('job_id')
            self.reference_image.visible = False
            self.reference_info.value = '地图已切换，请重新选择参考帧。'
            self._reset_workflows()
            self.plan_result = self.current_start = None
            self.target_result = None
            self.frame.options = [c['name'] for c in payload['cameras']] or ['(no frames)']
            self.frame.value = self.frame.options[0]
            self._reset_playback(payload)
            self._nav_map_loaded()
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
        elif kind == 'prepare_route_review':
            self._set_review(payload)
            self._message('PENDING candidates generated. Review source / PTS / registration before confirming.\n'+str(result_path))
        elif kind == 'save_route_review':
            self._workflow_context_changed(None)
            self.graph_path.value = payload['graph_path']
            self._message(f"Saved {payload['confirmed_segments']} reviewed segments, {payload['edges']} directed edges.\n"+
                          json.dumps(payload, ensure_ascii=False))
        elif kind == 'plan_route':
            self.plan_result = None
            self.controller.clear('route')
            self.controller.clear('goal')
            self.plan_path.value = str(result_path)
            if payload.get('status') == 'route_preview_only':
                self.controller.load_plan(payload)
                self.plan_result = payload
                self._route_show_summary(payload)
                self._message('Computed camera route preview; execution_permitted=false.\n'+str(result_path))
            else:
                self.route_summary.value = '没有生成路线：'+str(payload.get('failure_reason'))+'。可检查起点、已审核路段和排除列表。'
                self._message('Route rejected: '+str(payload.get('failure_reason'))+'\n'+str(result_path))
        elif kind == 'localize_current':
            self._apply_current(payload)
            self.current_query.value = str(result_path.parent)
            self._message('Independent current camera localized. No automatic route attachment.\n'+str(result_path))
        else:
            raise ValueError('unknown_completed_job_kind')

    def _connect(self, client):
        self.controller.on_client_connect(client)
        self._focus_map(client)
        @client.scene.on_rect_select(modifier='shift')
        def selected(event):
            try:
                with self._state_lock:
                    state = self._photo_states.get(event.client_id)
                    if not state:
                        raise ValueError('open_raw_photo_before_selecting')
                    c = event.client.camera
                    roi = rectangle_to_roi(event.screen_min, event.screen_max, width=state['width'], height=state['height'],
                        camera_position=c.position, camera_wxyz=c.wxyz, fov=c.fov, aspect=c.aspect)
                    self._select_roi(state, roi)
                    self._message('已选中目标：绿框已保留，可拖四角或中心蓝块调整。')
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
        self._pause_playback()
        camera = self._reference()
        self.controller.pose('reference', camera['T_WC'], color=(240, 125, 235))
        pts = camera.get('pts_time_s')
        self.reference_info.value = f"帧：{camera['name']}\n素材显示时间：{pts if pts is not None else '未知'} 秒\n该位置是历史拍摄位置，不是当前实测位置。"
        self.reference_image.visible = False
        if camera.get('image_path') and Path(camera['image_path']).is_file():
            self.reference_image.image = _read_image(camera['image_path'])
            self.reference_image.visible = True
        if event.client and self.follow_reference.value:
            T, q = _pose(camera['T_WC'])
            event.client.camera.position = T[:3, 3]
            event.client.camera.wxyz = q
            event.client.camera.fov = camera['fov']
        self._message(f"Reference {camera['name']}; video PTS={camera.get('pts_time_s')} s; "
                      f"mode={self.controller.mode}. Reference camera is NOT current pose.")

    def _load_mode(self, _):
        self._pause_playback()
        if protocol_value(self.mode.value, VIEW_MODES) == 'build_snapshot':
            if not self.snapshot_dir:
                raise ValueError('no_ready_snapshot: snapshot directory not supplied')
            # Worker validates READY and all native hashes, never a live model.
            self._submit('inspect_snapshot', {'snapshot_dir': str(self.snapshot_dir),
                'snapshot_index': int(self.snapshot_index.value), 'point_budget': POINT_BUDGET})
        else:
            self._submit('inspect_map', {'component_id': self.component.value.strip() or None})

    def _return_map(self, event):
        self._pause_playback()
        if event.client:
            self._remove_photo_state(event.client_id)
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

    def _invalidate_plan(self):
        self._task_revision += 1
        self.plan_result = None
        self.controller.clear('route')
        self.controller.clear('goal')
        self.plan_path.value = ''
        if hasattr(self, 'route_summary'):
            self.route_summary.value = '输入已变化，请重新计算路线。'

    def _invalidate_target(self, *, clear_query=False):
        self._nav_invalidate()
        self._invalidate_plan()
        self._clear_connection_preview()
        self.target_result = None
        self.target_photo = None
        self.object_path.value = ''
        self.controller.clear('support')
        if clear_query:
            self.query_result = None
            self.controller.clear('query')
            self.evidence_image.visible = False
            self.query_camera_info.value = '未求解。无标定时自动估计焦距。'
            self.reuse_query.value = False
            self._clear_photo_selection()
            for client in self.server.get_clients().values():
                self._focus_map(client)

    def _locate_object(self, _):
        self._require_final()
        photo, roi = self._validated_selection()
        reuse = self.reuse_query.value
        if reuse and (not self.query_path.value.strip() or (self.query_result or {}).get('pose_status') != 'localized'):
            raise ValueError('当前照片没有定位成功的证据可复用，请取消「复用已载入」后重新计算。')
        self._invalidate_target(clear_query=False)
        self.evidence_image.visible = False
        self._submit('locate_object', {'photo': photo,
            'roi': roi,
            'query_dir': self.query_path.value.strip() if reuse else None, 'mask_path': self.mask_path.value.strip() or None,
            'cluster_radius': float(self.cluster_radius.value) or None,
            'min_points': int(self.minimum_points.value), 'config': self._config()})
        self.roi_status.value = '已提交计算：当前绿框就是本次目标区域。调整选框将使本次计算不再作为当前结果。'
        self.roi_submit.disabled = True

    @_app_locked
    def load_query(self, directory):
        self._require_final()
        # Clear dependants BEFORE I/O: an unreadable/failed B must not leave A active.
        self._invalidate_target(clear_query=True)
        self._query_selected = True
        self.photo.value = ''
        self.query_path.value = str(directory)
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
        self.query_result = payload
        self.query_camera_info.value = query_camera_notice(payload)
        if payload.get('pose_status') != 'localized':
            self.controller.clear('query')
            self.controller.clear('support')
            self.roi_status.value = query_failure_notice(payload, roi_retained=False)
            self._message(self.roi_status.value+'\n诊断：'+str(payload.get('failure_reason')))
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

    @_app_locked
    def load_object(self, path):
        self._require_final()
        self._invalidate_target(clear_query=True)
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
            for key in ('map_id', 'map_hash'):
                if payload.get(key) != self.controller.model[key]:
                    raise ValueError('object_' + key + '_mismatch')
            if self.query_result.get('pose_status') != 'localized' and payload.get('status') == 'supported':
                raise ValueError('object_query_not_localized')
            from PIL import Image
            from .video import sha256_file
            source = Path(self.photo.value).resolve()
            with Image.open(source) as image:
                width, height = image.size
            self._select_roi({'path': source, 'sha256': sha256_file(source),
                              'width': width, 'height': height}, binding['roi'])
            self._show_evidence(query_dir, roi=binding.get('roi'))
            if self.query_result.get('pose_status') != 'localized':
                self.roi_status.value = query_failure_notice(self.query_result)
            else:
                self.controller.load_object(payload, evidence)
                self.target_result = payload if payload['status'] == 'supported' else None
                self.roi_status.value = ('计算完成：照片已定位，框内目标已有三维支持。'
                    '黄色点是可见表面证据，不是完整物体中心。' if self.target_result else
                    object_failure_notice(payload))
        else:
            self.roi_status.value = '计算结束：未生成可用目标结果。原因：'+str(payload.get('failure_reason'))+'。请检查输入后重新计算。'
        self.object_path.value = str(path)
        details = ('\n诊断：'+str(payload['failure_reason'])) if payload.get('failure_reason') else ''
        self._message(self.roi_status.value+details+'\n结果文件：'+str(path))

    def _plan_route(self, _):
        self._require_final()
        if self.current_start is None or self.target_result is None:
            raise ValueError('route_requires_explicit_start_and_object_evidence')
        self.controller.clear('route')
        self.controller.clear('goal')
        self.plan_result = None
        if self._route_disabled_edges:
            graph = self._read_graph()
            if graph['graph_sha256'] != self._route_edges_graph:
                raise ValueError('路线图已变化，请重新读取并选择要排除的路段。')
        self._submit('plan_route', {'graph_path': str(Path(self.graph_path.value).resolve()),
                                   'start': self.current_start, 'target': self.target_result,
                                   'disabled_edge_ids': sorted(self._route_disabled_edges),
                                   'local_avoidance_assumed': bool(self.route_avoidance.value),
                                   **self._planning_time()})

    def _route_refresh_edges(self, _):
        graph = self._read_graph()
        if self._route_edges_graph != graph['graph_sha256']:
            self._route_restore_edges(None)
        self._route_edges_graph = graph['graph_sha256']
        self._route_edge_options = {
            f"{edge['from']} → {edge['to']} | {eid}": eid
            for eid, edge in graph['edges'].items() if edge.get('enabled') is True}
        self.route_edge_choice.options = list(self._route_edge_options) or ['（没有已审核路段）']
        self.route_edge_choice.value = self.route_edge_choice.options[0]

    def _route_disable_edge(self, _):
        graph = self._read_graph()
        if graph['graph_sha256'] != self._route_edges_graph:
            raise ValueError('路线图已变化，请先重新读取路段。')
        edge_id = self._route_edge_options.get(self.route_edge_choice.value)
        if edge_id is None:
            raise ValueError('请选择已审核路段。')
        self._route_disabled_edges.add(edge_id)
        self.route_blocked_info.value = '\n'.join(sorted(self._route_disabled_edges))
        self._invalidate_plan()

    def _route_restore_edges(self, _):
        self._route_disabled_edges.clear()
        self.route_blocked_info.value = '无'
        self._invalidate_plan()

    def _route_show_summary(self, payload):
        lines = [f"终点观察帧：{payload.get('goal_frame_id')}；路径长度：{payload.get('length', 0):.3f} {payload.get('units')}"]
        if payload.get('avoidance_context', {}).get('state') == 'assumed_not_verified':
            lines.append('使用假设：机载局部避障可用；未核验实机。')
        for row in payload.get('itinerary', []):
            turn = f"；三维折线转角 {row['turn_angle_deg']:.1f}°" if row.get('turn_angle_deg') is not None else ''
            lines.append(f"{row['index']+1}. {row['frame_id']} · 累计 {row['distance_from_start']:.3f} {row['units']}{turn}")
        lines.append('相机历史路线，不是机身控制指令；不自动执行。')
        self.route_summary.value = '\n'.join(lines)

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
        self._invalidate_plan()
        self._clear_connection_preview()
        self.controller.clear('start')
        self.controller.clear('route')
        self.controller.clear('goal')
        self.plan_result = None
        self._submit('localize_current', {'photo': str(photo), 'config': self._config(capture)})

    def _manual_start(self, _):
        self._require_final()
        camera = self._reference()
        self._invalidate_plan()
        self._clear_connection_preview()
        self.controller.clear('route')
        self.controller.clear('goal')
        self.plan_result = None
        self.current_start = {k: self.controller.model[k] for k in ('map_id', 'map_hash', 'component_id')}
        self.current_start.update(source='explicit_pose', manual=True, node_id=camera['name'],
                                  T_MC=camera['T_WC'], selected_at=utc_now())
        self.controller.pose('start', camera['T_WC'], color=(255, 165, 0))
        path = self.sidecar/('manual_start-'+uuid4().hex+'.json')
        atomic_json(path, self.current_start)
        self._message('Saved explicitly MANUAL start: '+str(path))

    def _load_plan(self, _):
        payload = read_json(self.plan_path.value)
        if self._query_selected:
            if self.target_result is None or payload.get('target_object_result') != self.target_result:
                raise ValueError('saved_plan_not_bound_to_current_target')
        self.plan_result = None
        self.controller.load_plan(payload)
        self.plan_result = payload
        self._route_show_summary(payload)
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
        self._nav_invalidate()
        self._stop.set()
        self._thread.join(timeout=2)
        self._playback_thread.join(timeout=2)
        self.jobs.close()  # deliberately do not kill compute when UI stops
        self._web_close()
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
