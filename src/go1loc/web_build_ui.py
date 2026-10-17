"""Small browser controller for the existing offline reconstruction pipeline."""
from pathlib import Path

import numpy as np

from .jobs import JobManager, read_json
from .web_build import WebBuildSession, validate_settings

DISPLAY_MODES = {'未知（不额外去畸变）': 'unknown', '原始鱼眼（有证据）': 'raw_fisheye',
                 '已去畸变（有证据）': 'already_rectified', '显示重投影（有证据）': 'display_reprojected'}
STAGES = {'prepare_video': '解码、抽帧与质量检查', 'build_map': '准备视觉重建',
          'sfm_import_and_verification': '导入特征并验证几何',
          'reconstruction_component': '建立重建分量',
          'local_features': 'SuperPoint 特征提取与掩膜过滤', 'global_features': '图像检索特征提取',
          'reconstruction': '三维重建 / 优化',
          'mapping': '三维重建 / 优化', 'local_feature_extraction': 'SuperPoint 特征提取',
          'retrieval_feature_extraction': '图像检索特征提取', 'matching': 'LightGlue 图像匹配',
          'completed': '最终地图已生成', 'initializing': '初始化重建'}


def build_failure_help(error):
    if 'sfm_did_not_produce_a_3d_model' in str(error):
        return ('未能建立三维地图：没有找到满足几何条件的初始图像对。常见原因是相机位置几乎不变、'
                '仅原地转动，或匹配/内参不合适。请采集有缓慢平移、连续重叠视角的录像；'
                '仅提高画面清晰度不能替代视差。原素材和诊断结果已保留。')
    return str(error)


def _integers(values):
    if any(not np.isfinite(v) or int(v) != v for v in values):
        raise ValueError('坐标必须是原始解码帧中的整数像素。')
    return [int(v) for v in values]


class WebBuildControls:
    def _web_build_ui(self):
        self.web_build = WebBuildSession(self.sidecar/'web_build')
        self._web_initial = (self.map_dir, self.sidecar, self.snapshot_dir)
        self._web_preview_id = None
        self._web_context = None
        self._web_scene_job = None
        self._web_loaded_key = None
        self._web_pending = None
        self._web_retired_jobs = []
        self._web_exclusions = []
        self._web_last_follow = True
        self._web_applied_job = None
        gui = self.server.gui
        self.web_panel = gui.add_folder('新建地图：选视频 → 开始 → 看进度', expand_by_default=False)
        with self.web_panel:
            gui.add_markdown('**不用打开 PowerShell。** 可直接选择录制台保存的原始记录，或上传 MP4 / MKV。检查首帧后开始。每次自动新建目录，旧地图和原视频不改动。\n\n'
                             '仅本机处理、不上传云端、不连接机器狗。建议先试 30 秒；网页单文件 ≤512 MiB、单次 ≤240 帧。')
            self.web_capture_choice = gui.add_dropdown('① 录制台已保存记录（推荐，无需导出视频）', ['（暂无完整记录）'])
            self._button('刷新本机录制记录', self._web_refresh_captures)
            self.web_capture_select = self._button('使用选中记录建图', self._web_select_capture)
            gui.add_markdown('只读取本项目 data/captures 下已结束的录像；不启动采集、不连接相机。PNG 和读取时间直接进入建图。')
            self._web_capture_options = {}
            self._web_refresh_captures(None)
            self.web_upload = gui.add_upload_button('或选择视频 MP4 / MKV', mime_type='.mp4,.mkv')
            self.web_upload.on_upload(self._guard(self._web_upload))
            self.web_status = gui.add_text('视频任务进度', '请选择视频。下方已有地图仍可查看。', multiline=True, disabled=True)
            self.web_native = gui.add_image(np.zeros((2, 2, 3), np.uint8), label='所选视频首帧（完整解码尺寸）', visible=False)
            self.web_kind = gui.add_dropdown('素材来源', ['Go1 界面录屏', '原始单路视频', '录制台原始记录（PNG）'])
            with gui.add_folder('② 核对单路画面与排除区域', expand_by_default=False):
                gui.add_markdown('坐标以首帧实际像素为准。录屏中的十字线、图标、边框和固定遮挡可添加为排除矩形；'
                    '红色仅为诊断叠加，不涂黑输入。实际在 SuperPoint 后、LightGlue 前排除特征。\n\n'
                    '运动鼠标或变化的布局不能靠固定掩膜解决，请重新录制。原始视频模式保留整幅图，不应用此处录屏裁剪。')
                self.web_crop_xy = gui.add_vector2('保留区域左上角 x / y', (0, 0), step=1)
                self.web_crop_wh = gui.add_vector2('保留区域宽 / 高', (1, 1), step=1)
                self.web_mask_xy = gui.add_vector2('排除区域左上角 x / y', (0, 0), step=1)
                self.web_mask_wh = gui.add_vector2('排除区域宽 / 高', (1, 1), step=1)
                self._button('添加这块排除区域', self._web_add_mask)
                self._button('撤销最后一块排除区域', self._web_undo_mask)
                self.web_masks = gui.add_text('已添加排除区域', '无', multiline=True, disabled=True)
                self.web_window = gui.add_text('选中窗口描述', '单路视图（请核实）')
                self.web_camera = gui.add_text('物理 camera_id（不清楚请留空）', '')
                self.web_camera_label = gui.add_text('相机方向描述（可选）', '')
                self.web_display = gui.add_dropdown('显示几何', list(DISPLAY_MODES))
                self.web_evidence = gui.add_text('几何判断依据（未知时留空）', '')
                self._button('更新裁剪与有效区域预览', self._web_preview)
                self.web_mask_image = gui.add_image(np.zeros((2, 2, 3), np.uint8), label='裁剪后有效区域：红色不参与特征匹配', visible=False)
                gui.add_markdown('录屏裁剪不能套用原相机内参。原始视频或原始记录可在下面导入对应相机的实测标定；未导入时不额外去畸变，内参近似估计。')
            with gui.add_folder('可选：相机实测标定（不沿用旧鱼眼参数）', expand_by_default=False):
                gui.add_markdown('先选素材、等待首帧，再选标定 JSON。支持普通 OpenCV / pinhole 和原始 fisheye；'
                    '核验分辨率、投影模式以及原始记录的相机身份。未知几何需提供标定中的投影依据。没有标定可以先探索建图，长度仍为 map_units。')
                self.web_calibration_upload = gui.add_upload_button('选择该相机的标定 JSON', mime_type='.json')
                self.web_calibration_upload.on_upload(self._guard(self._web_calibration_upload))
                self.web_calibration_clear = self._button('移除本次标定，使用近似内参', self._web_clear_calibration)
                self.web_calibration_info = gui.add_text('当前标定', '未选择：不额外去畸变。', multiline=True, disabled=True)
            self.web_start_s = gui.add_number('从视频第几秒开始', 0., min=0., step=1.)
            self.web_duration = gui.add_number('处理时长（秒）', 30., min=.1, max=600., step=1.)
            self.web_interval = gui.add_number('抽帧间隔（秒）', .5, min=.05, max=10., step=.05)
            self.web_explore = gui.add_checkbox('允许低质量探索（放宽模糊/过曝过滤，不算正式验收）', False)
            self.web_confirm = gui.add_checkbox('已检查首帧：单路固定视图、裁剪和遮挡设置正确', False)
            self.web_start_button = self._button('③ 开始建图', self._web_start)
            self.web_follow = gui.add_checkbox('自动显示最新完整快照，完成后切到新地图', True)
            self.web_snapshot = gui.add_dropdown('本次建图历史快照', ['（尚无快照）'])
            self._button('暂停跟随并查看选中快照', self._web_history)
            self._button('显示本次已完成地图', self._web_final)
            self._button('返回启动时的地图（不停止建图）', self._web_original)
            self.web_display_info = gui.add_text('建图显示说明', '没有快照时只显示阶段；不读取半写入模型。', multiline=True, disabled=True)
        for field in (self.web_kind, self.web_crop_xy, self.web_crop_wh, self.web_window,
                      self.web_camera, self.web_camera_label, self.web_display, self.web_evidence):
            field.on_update(self._guard(lambda event: self._web_unconfirm() if event.client is not None else None))
        self._web_config_fields = (self.web_kind, self.web_crop_xy, self.web_crop_wh, self.web_mask_xy,
            self.web_mask_wh, self.web_window, self.web_camera, self.web_camera_label, self.web_display,
            self.web_evidence, self.web_start_s, self.web_duration, self.web_interval, self.web_explore,
            self.web_confirm)

    def _web_refresh_captures(self, _):
        from .web_capture import list_captures
        rows = list_captures()
        self._web_capture_options = {
            f"{r['capture_id']} | {r['source_kind']} · 相机{r['camera_id']} · {r['width']}×{r['height']} · {r['frames']}帧": r['capture_id']
            for r in rows}
        old = self.web_capture_choice.value
        options = list(self._web_capture_options) or ['（暂无完整记录）']
        self.web_capture_choice.options = options
        self.web_capture_choice.value = old if old in options else options[0]

    def _web_select_capture(self, _):
        capture_id = self._web_capture_options.get(self.web_capture_choice.value)
        if capture_id is None:
            raise ValueError('请选择已完成的录像记录；单张照片和未结束录像不用于建图。')
        self.select_capture_for_build(capture_id)

    def select_capture_for_build(self, capture_id):
        """Shared recording-to-map handoff; selection never starts reconstruction."""
        self.web_build.select_capture(capture_id)
        self.web_panel.expand_by_default = True
        self._web_unconfirm()
        self.web_native.visible = False
        self.web_start_button.disabled = True
        self.web_status.value = '正在核验原始PNG、帧序号和时间记录；不会连接相机。'

    def _web_calibration_upload(self, event):
        upload = event.target.value
        if upload is None:
            return
        if self.web_kind.value == 'Go1 界面录屏':
            raise ValueError('请先选择原始单路视频或录制台原始记录；录屏不能套用原相机标定。')
        row = self.web_build.set_calibration(upload.name, upload.content)
        self.web_calibration_info.value = f"已采用 {upload.name} · {row['model']} · {row['image_size']}；仅绑定当前素材。"
        self._web_unconfirm()
        self._web_preview(None)

    def _web_clear_calibration(self, _):
        self.web_build.clear_calibration()
        self.web_calibration_info.value = '未选择：不额外去畸变，内参近似估计。'
        self._web_unconfirm()
        if self.web_build.inspection():
            self._web_preview(None)

    def _web_unconfirm(self):
        self.web_confirm.value = False
        self.web_mask_image.visible = False

    def _web_upload(self, event):
        upload = event.target.value
        if upload is None:
            return
        self.web_build.upload(upload.name, upload.content)
        self.web_kind.value = '原始单路视频'
        self._web_unconfirm()
        self.web_native.visible = False
        self.web_status.value = '正在检查视频并解码原尺寸首帧；默认按普通视频处理，若是界面录屏请切换素材来源。'
        self.web_start_button.disabled = True

    def _web_settings(self):
        return dict(kind={'Go1 界面录屏': 'screen', '原始单路视频': 'video', '录制台原始记录（PNG）': 'capture'}[self.web_kind.value],
            confirmed=bool(self.web_confirm.value), start=float(self.web_start_s.value),
            duration=float(self.web_duration.value), interval=float(self.web_interval.value),
            exploratory=bool(self.web_explore.value),
            crop=_integers([*self.web_crop_xy.value, *self.web_crop_wh.value]),
            exclusions=list(self._web_exclusions), window=self.web_window.value.strip(),
            camera_id=self.web_camera.value.strip(), camera_label=self.web_camera_label.value.strip(),
            display_mode=DISPLAY_MODES[self.web_display.value], display_evidence=self.web_evidence.value.strip())

    def _web_add_mask(self, _):
        if self.web_build.busy():
            raise ValueError('任务执行中不能修改本次掩膜。')
        rect = _integers([*self.web_mask_xy.value, *self.web_mask_wh.value])
        self._web_exclusions.append(rect)
        try:
            self._web_preview(None)
        except Exception:
            self._web_exclusions.pop()
            raise
        self._web_unconfirm()
        self._web_preview(None)

    def _web_undo_mask(self, _):
        if self.web_build.busy():
            raise ValueError('任务执行中不能修改本次掩膜。')
        if self._web_exclusions:
            self._web_exclusions.pop()
        self._web_unconfirm()
        self._web_preview(None)

    def _web_preview(self, _):
        inspection = self.web_build.inspection()
        if not inspection:
            raise ValueError('请先选择视频并等待首帧。')
        from PIL import Image
        from .screen_import import crop_image, effective_mask
        settings = self._web_settings()
        # Preview validation does not certify the user's review.
        _, screen = validate_settings(dict(settings, confirmed=True), inspection['probe'])
        with Image.open(inspection['preview']) as image:
            rgb = np.array(image.convert('RGB'))
        if screen:
            rgb = crop_image(rgb, screen)
            excluded = effective_mask(screen) == 0
            rgb[excluded] = (rgb[excluded].astype(float)*.3 + np.array([255, 30, 30])*.7).astype(np.uint8)
        calibration = self.web_build.record.get('calibration')
        if calibration:
            if screen:
                raise ValueError('请移除原相机标定后再使用录屏模式。')
            import cv2
            from .web_capture import resolve_calibration
            from .calibration import load_calibration, build_rectification
            path = resolve_calibration(self.web_build.root, self.web_build.record['upload'], inspection,
                                       calibration['calibration_id'])
            rect = build_rectification(load_calibration(path))
            rgb = cv2.remap(rgb, rect['map_x'], rect['map_y'], cv2.INTER_LINEAR)
            x, y, w, h = rect['crop_xywh']
            rgb = rgb[y:y+h, x:x+w]
        self.web_mask_image.image = rgb
        self.web_mask_image.visible = True
        self.web_masks.value = '\n'.join(f'{i+1}. x={r[0]}, y={r[1]}, 宽={r[2]}, 高={r[3]}'
                                      for i, r in enumerate(self._web_exclusions)) or '无（录屏仍排除裁剪边缘）'

    def _web_start(self, _):
        if any(j['status'] in ('queued', 'running') and not j['kind'].startswith('inspect_')
               for j in self.jobs.list_jobs()):
            raise ValueError('照片定位或路线任务尚未结束，请等它完成后开始新建图。')
        self._web_preview(None)
        state = self.web_build.start(self._web_settings())
        state['job_dir'] = str(self.web_build.jobs.root/state['job_id'])
        self.web_follow.value = True
        self._web_enter_build(state)
        self.web_start_button.disabled = True
        self.web_status.value = '任务已启动：将自动抽帧、检查质量、建图和发布快照。'

    def _web_switch_context(self, map_dir, sidecar, snapshot_dir=None):
        self._pause_playback()
        self._invalidate_target(clear_query=True)
        self._query_selected = False
        self._pending_query = None
        self.current_start = self.plan_result = None
        self._reset_workflows()
        self.graph_path.value = self.query_path.value = self.object_path.value = ''
        self.current_photo.value = self.photo.value = ''
        for state in self._photo_states.values():
            state['handle'].remove()
        self._photo_states.clear()
        for group in list(self.controller.groups):
            self.controller.clear(group)
        self.controller.model = None
        self.controller.mode = 'build_snapshot'
        self.reference_image.visible = False
        self._reset_playback({'cameras': []})
        self._web_retired_jobs.append(self.jobs)
        self.map_dir, self.sidecar = Path(map_dir).resolve(), Path(sidecar).resolve()
        self.snapshot_dir = Path(snapshot_dir) if snapshot_dir else None
        self.jobs = JobManager(self.map_dir, self.sidecar)
        self._seen = {j['job_id'] for j in self.jobs.list_jobs()}
        self._job_roles.clear()
        self._job_revisions.clear()
        self._web_scene_job = self._web_pending = self._web_loaded_key = None
        self._web_applied_job = None
        self.component.value = ''
        self.map_directory.value = str(self.map_dir)
        self.results_directory.value = str(self.sidecar)
        self._refresh_overview()

    def _web_enter_build(self, state):
        if self._web_context == state['job_id']:
            return
        directory = Path(state['job_dir'])
        self._web_switch_context(directory/'output/map', directory/'viewer')
        self._web_context = state['job_id']
        self.web_display_info.value = '本次建图尚无完整模型；旧地图、查询和路线已退出当前画布。'

    def _web_history(self, _):
        state = self.web_build.state() or {}
        rows = state.get('build', {}).get('snapshots', [])
        index = next((i for i, row in enumerate(rows) if str(row['snapshot_index']) == self.web_snapshot.value), None)
        if index is None:
            raise ValueError('尚无可读的完整快照。')
        self._web_enter_build(state)
        self.web_follow.value = False
        self._web_last_follow = False
        self._task_revision += 1  # In-flight automatic selection may not replace a manual selection.
        self._web_pending = ('inspect_build_snapshot', {'run_dir': state['run_dir'], 'snapshot_index': index},
                             ('history', rows[index]['snapshot_index']))

    def _web_final(self, _):
        state = self.web_build.state() or {}
        if not state.get('build', {}).get('final_map_dir'):
            raise ValueError('本次建图尚无通过校验的最终地图。')
        self._web_enter_build(state)
        self.web_follow.value = False
        self._web_last_follow = False
        self._task_revision += 1
        self._web_pending = ('inspect_map', {}, ('final', state['job_id']))

    def _web_original(self, _):
        self.web_follow.value = False
        self._web_last_follow = False
        self._web_switch_context(*self._web_initial)
        self._web_context = None
        self._submit('inspect_map', {})
        self.web_display_info.value = '正在返回启动时地图；后台建图继续。'

    def _web_tick(self):
        # Reap only our own retired subprocess handles; do not stop any worker.
        for manager in self._web_retired_jobs:
            manager.list_jobs()
        state = self.web_build.state()
        if state is None:
            self.web_start_button.disabled = True
            return
        busy = state['status'] in ('queued', 'running')
        self.web_upload.disabled = busy
        self.web_capture_select.disabled = busy
        self.web_capture_choice.disabled = busy
        self.web_calibration_upload.disabled = self.web_calibration_clear.disabled = busy
        self.web_start_button.disabled = busy
        for field in self._web_config_fields:
            field.disabled = busy
        upload_id = self.web_build.record['upload']['upload_id']
        inspection = self.web_build.inspection()
        if inspection and upload_id != self._web_preview_id:
            from PIL import Image
            self._web_preview_id = upload_id
            with Image.open(inspection['preview']) as image:
                self.web_native.image = np.array(image.convert('RGB'))
            self.web_native.visible = True
            p = inspection['probe']
            self.web_crop_xy.value = (0, 0)
            self.web_crop_wh.value = (p['width'], p['height'])
            self._web_exclusions = []
            self.web_camera.value = self.web_camera_label.value = self.web_evidence.value = ''
            self.web_display.value = next(iter(DISPLAY_MODES))
            self.web_window.value = '单路视图（请核实）'
            self.web_confirm.value = False
            self.web_explore.value = False
            native = inspection.get('input_kind') == 'capture'
            if native:
                self.web_kind.value = '录制台原始记录（PNG）'
                self.web_camera.value = str(inspection['source']['camera_id'])
                self.web_camera_label.value = inspection['source_kind']
            elif self.web_kind.value == '录制台原始记录（PNG）':
                self.web_kind.value = '原始单路视频'
            calibration = self.web_build.record.get('calibration')
            self.web_calibration_info.value = (f"已采用 {calibration['original_name']} · {calibration['model']}；绑定当前素材。"
                                               if calibration else '未选择：不额外去畸变，内参近似估计。')
            saved = self.web_build.record.get('settings')
            if saved:
                self.web_crop_xy.value, self.web_crop_wh.value = tuple(saved['crop'][:2]), tuple(saved['crop'][2:])
                self._web_exclusions = list(saved['exclusions'])
                self.web_camera.value, self.web_camera_label.value = saved['camera_id'], saved['camera_label']
                self.web_evidence.value, self.web_window.value = saved['display_evidence'], saved['window']
                self.web_display.value = next(k for k, v in DISPLAY_MODES.items() if v == saved['display_mode'])
                self.web_kind.value = {'screen': 'Go1 界面录屏', 'video': '原始单路视频', 'capture': '录制台原始记录（PNG）'}[saved['kind']]
                self.web_start_s.value, self.web_duration.value, self.web_interval.value = saved['start'], saved['duration'], saved['interval']
                self.web_explore.value = saved['exploratory']
            self._web_preview(None)
        progress = state.get('progress', {})
        build = state.get('build', {})
        phase = build.get('stage', progress.get('stage', '检查视频 / 等待解码'))
        display_status = 'failed' if build.get('status') == 'failed' else state['status']
        status = {'queued': '已排队', 'running': '进行中', 'completed': '已完成', 'failed': '失败（数据保留）'}[display_status]
        error = build_failure_help(state.get('error') or build.get('failure_reason') or '')
        probe_text = ''
        if inspection:
            p = inspection['probe']
            probe_text = f"原尺寸 {p['width']}×{p['height']}；记录时长 {p.get('duration_s'):.3f} 秒。\n" if p.get('duration_s') is not None else f"原尺寸 {p['width']}×{p['height']}；时长未知。\n"
            if inspection.get('input_kind') == 'capture':
                probe_text += f"来源 {inspection['source_kind']}；时间为读取时间，不是曝光时间。\n"
        self.web_status.value = (probe_text + f"{status} · {STAGES.get(phase, phase)}\n"
            f"候选帧 {progress.get('candidate_count', '待统计')}；保留 {progress.get('kept_count', '待统计')}；完整快照 {len(build.get('snapshots', []))} 份。\n"
            f"{'低质量探索，不算正式验收。' if progress.get('exploratory') else ''}\n{error}\n保存位置：{state['job_dir']}")
        if state['kind'] == 'inspect_video':
            if state['status'] == 'completed':
                self.web_status.value = probe_text+'首帧已就绪：检查裁剪与排除区域、勾选确认，然后点击「③ 开始建图」。'
            else:
                self.web_start_button.disabled = True
            return
        if bool(self.web_follow.value) != self._web_last_follow:
            self._web_last_follow = bool(self.web_follow.value)
            self._task_revision += 1
            self._web_pending = None
            self._web_loaded_key = None
        if self.web_follow.value:
            self._web_enter_build(state)
        rows = build.get('snapshots', [])
        options = [str(row['snapshot_index']) for row in rows] or ['（尚无快照）']
        if list(self.web_snapshot.options) != options:
            old = self.web_snapshot.value
            self.web_snapshot.options = options
            self.web_snapshot.value = old if old in options else options[-1]
        if self._web_context != state['job_id']:
            return
        if state.get('run_dir'):
            self.snapshot_dir = Path(state['run_dir'])/'snapshots'
        if self._web_scene_job:
            job_id, key, revision = self._web_scene_job
            if job_id in self._seen:
                result = self.jobs.status(job_id)
                if result['status'] == 'completed' and self._web_applied_job == job_id:
                    self._web_loaded_key = key
                    payload = self.controller.model
                    self.web_display_info.value = (f"{'最终地图' if payload['mode'] == 'final_map_replay' else '真实过程快照'}："
                        f"{len(payload['cameras'])} 个相机位姿，{payload['total_points']} 个点。\n"
                        '快照之间未假定对齐；所有路线仍是离线预览。')
                elif result['status'] == 'failed':
                    self._web_loaded_key = key  # Do not repeatedly retry corrupt publications.
                    self.web_display_info.value = '模型读取失败，上一份完整模型保留：'+result.get('error', '')
                self._web_scene_job = None
        if self.web_follow.value and self._web_scene_job is None:
            if build.get('final_map_dir'):
                choice = ('inspect_map', {}, ('final', state['job_id']))
            elif rows:
                choice = ('inspect_build_snapshot', {'run_dir': state['run_dir'], 'snapshot_index': len(rows)-1},
                          ('snapshot', rows[-1]['component_id'], rows[-1]['snapshot_index']))
            else:
                choice = None
            if choice and choice[2] != self._web_loaded_key:
                self._web_pending = choice
        if self._web_pending and self._web_scene_job is None:
            if any(j['status'] in ('queued', 'running') for j in self.jobs.list_jobs()):
                return
            kind, params, key = self._web_pending
            job = self._submit(kind, params)
            self._web_scene_job = (job['job_id'], key, self._task_revision)
            self._web_pending = None

    def _web_close(self):
        self.web_build.jobs.close()
        for jobs in self._web_retired_jobs:
            jobs.close()
