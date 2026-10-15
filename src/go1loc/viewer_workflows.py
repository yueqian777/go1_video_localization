"""Human-reviewed offline route workflows, shared by the Viser application.

No nearest-node attachment, source manufacture, or robot execution lives here.
"""
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import numpy as np

from .evidence import digest_json, verify_binding
from .jobs import atomic_json, read_json, utc_now
from .planning import import_current_start, validate_start_connection, _valid_graph, _time_context
from .route_review import review_route_candidate
from .video import sha256_file
from .viewer_text import TIME_MODES, protocol_value


def verified_current_query(directory, model, target_photo=None):
    """Only load complete, content-bound localization exports; no JSON pose editing."""
    directory = Path(directory).resolve()
    result = read_json(directory/'localization_result.json')
    evidence = read_json(directory/'query_evidence.json')
    if digest_json({k: v for k, v in evidence.items() if k != 'evidence_sha256'}) != evidence.get('evidence_sha256'):
        raise ValueError('evidence_content_hash_mismatch')
    if evidence.get('localization_result_sha256') != digest_json(result):
        raise ValueError('current_result_not_bound_to_evidence: rerun localization with this version')
    for key in ('map_id', 'map_hash', 'component_id', 'pose_status', 'capture_time', 'localization_time'):
        if result.get(key) != evidence.get(key):
            raise ValueError('current_evidence_'+key+'_mismatch')
    for key in ('map_id', 'map_hash', 'component_id'):
        if result.get(key) != model.get(key) or not result.get(key):
            raise ValueError('current_'+key+'_mismatch')
    photo = Path(evidence['query_path']).resolve()
    verify_binding(evidence['binding'], photo, model['map_hash'])
    processed = evidence['binding'].get('files', {}).get('processed_query')
    query_key = Path(processed['path']).name if processed else photo.name
    if result.get('query_sha256') != evidence['binding']['query_sha256'] or result.get('query_id') != query_key:
        raise ValueError('current_query_identity_mismatch')
    if target_photo and sha256_file(photo) == sha256_file(Path(target_photo)):
        raise ValueError('current_photo_must_be_independent_of_target')
    if result.get('pose_status') != 'localized':
        raise ValueError('current_query_not_localized')
    return result, photo


class RouteWorkflowControls:
    def _workflow_ui(self):
        gui = self.server.gui
        self._pending_attachment = None
        self.route_review = None
        self._review_revision = 0
        self._review_jobs = {}
        with self._plan_tab:
            with gui.add_folder('时间模式：当前 / 历史回放', expand_by_default=True):
                gui.add_markdown('真实当前照片检查 30 秒新鲜度；历史实验请选择离线回放，另填回放参考时间。不得把旧照片拍摄时间改为现在。两种模式都只是预览。')
                self.time_mode = gui.add_dropdown('时间核验方式', list(TIME_MODES))
                self.replay_time = gui.add_text('回放参考时间（含时区）', '', hint='仅离线回放使用，例如 2026-10-14T12:00:00+08:00；必须与素材时间语义一致。')
        self.time_mode.on_update(self._guard(self._workflow_context_changed))
        self.replay_time.on_update(self._guard(self._workflow_context_changed))
        self.graph_path.on_update(self._guard(self._workflow_context_changed))
        with self._start_tab:
            with gui.add_folder('进阶：复用独立照片定位结果', expand_by_default=False):
                self.current_query = gui.add_text('独立照片查询结果文件夹', '')
                self._button('校验并载入独立照片结果', self._load_current_query)
            with gui.add_folder('进阶：检查并采用照片接入证据', expand_by_default=False):
                gui.add_markdown('先在「⑤ 路线」选路线图与时间模式。**查看候选 → 导入证据 → 检查 → 明确采用**；定位成功不等于能够进入路线。')
                self.connection_info = gui.add_text('接入检查结果（含原始证据字段）', '尚未检查接入证据。', multiline=True, disabled=True)
                self.connection_node = gui.add_dropdown('候选接入节点（不会自动连接）', ['(none)'])
                self._button('检查路线图与当前接入状态', self._inspect_connection)
                self._button('仅显示所选候选节点', self._show_connection_node)
                self.attachment_path = gui.add_text('接入证据文件（current_start / attachment JSON）', '')
                self.attachment_source = gui.add_text('证据来源文件（核验 SHA256）', '')
                self._button('检查导入证据（暂不采用）', self._inspect_attachment)
                self._button('明确采用已检查证据（仅离线预览）', self._adopt_attachment)
                gui.add_markdown('采用前需核对原始来源。哈希只能证明文件一致，不能证明轨迹实际连续；单张照片本身不能提供接入路径证据。')
        with self._review_tab:
            gui.add_markdown('### 审核历史轨迹是否连续\n**生成候选 → 回看原视频与参考帧 → 填依据 → 确认 / 拆分 / 拒绝 → 保存。**\n\n紫色线仍是待审核候选。这里确认的是历史采集连续性，不是现在能通行，也不会让机器狗移动。')
            with gui.add_folder('候选生成参数', expand_by_default=False):
                self.review_gap = gui.add_number('候选最大时间间隔（秒）', 2., min=.01, step=.1)
            self._button('生成待审核候选路段', self._prepare_review)
            self.review_info = gui.add_text('审核进度', '尚未生成候选路段。', multiline=True, disabled=True)
            self.review_candidate = gui.add_dropdown('选择待审核路段', ['(none)'])
            self.review_candidate_info = gui.add_text('所选路段概况', '暂无待审核路段。', multiline=True, disabled=True)
            self.review_candidate.on_update(self._guard(self._select_review_candidate))
            self.review_frame = gui.add_dropdown('回看时间线帧序号（含未注册帧）', ['(none)'])
            self._button('查看该帧画面与时间', self._show_review_frame)
            self.review_image = gui.add_image(np.zeros((2, 2, 3), np.uint8), label='审核参考画面', visible=False)
            gui.add_markdown('**只确认一部分？** 先选下面的首尾帧并点击拆分，再选择拆出的路段进行确认。直接确认会确认当前整段。')
            self.review_first = gui.add_dropdown('拆分范围：首帧', ['(none)'])
            self.review_last = gui.add_dropdown('拆分范围：尾帧', ['(none)'])
            self.reviewer = gui.add_text('审核人（必填）', '')
            self.review_note = gui.add_text('审核依据 / 拆分或拒绝原因（必填）', '', multiline=True)
            self._button('按所选范围拆分（仍待审核）', lambda _: self._review_decide('split'))
            self._button('确认当前整段历史连续', lambda _: self._review_decide('confirm'))
            self._button('拒绝当前整段', lambda _: self._review_decide('reject'))
            self._button('保存审核结果并生成路线图', self._save_review)
            with gui.add_folder('原始审核诊断（时间缺口与来源）', expand_by_default=False):
                self.review_diagnostics = gui.add_text('来源与连续性风险记录', '', multiline=True, disabled=True)
                self.candidate_diagnostics = gui.add_text('候选原始记录', '', multiline=True, disabled=True)

    def _planning_time(self):
        mode = protocol_value(self.time_mode.value, TIME_MODES)
        reference = self.replay_time.value.strip() or None
        _time_context(utc_now(), mode, reference)
        return dict(time_mode=mode, replay_reference_time=reference)

    def _clear_connection_preview(self):
        self._pending_attachment = None
        self.controller.clear('attachment')
        self.controller.clear('connection_node')
        self.connection_info.value = '起点、路线图或时间变化后，需要重新检查接入证据。'

    def _workflow_context_changed(self, _):
        self._invalidate_plan()
        self._clear_connection_preview()

    def _reset_workflows(self):
        self._clear_connection_preview()
        self._review_revision += 1
        self.route_review = None
        self.controller.clear('review_candidate')
        self.review_info.value = '地图或分量已改变，请重新生成候选路段。'
        self.review_candidate_info.value = '暂无待审核路段。'
        self.review_diagnostics.value = self.candidate_diagnostics.value = ''
        self.review_image.visible = False
        self.review_candidate.options = ['(none)']
        self.review_candidate.value = '(none)'

    def _read_graph(self):
        self._require_final()
        if not self.graph_path.value.strip():
            raise ValueError('route_graph_required: generate and review historical segments first')
        graph = read_json(self.graph_path.value)
        if not _valid_graph(graph):
            raise ValueError('invalid_graph')
        if any(graph.get(k) != self.controller.model.get(k) for k in ('map_id', 'map_hash', 'component_id')):
            raise ValueError('graph_map_version_or_component_mismatch')
        self.connection_node.options = list(graph['nodes']) or ['(none)']
        if self.connection_node.value not in self.connection_node.options:
            self.connection_node.value = self.connection_node.options[0]
        return graph

    def _draw_preview(self, group, path, text, color):
        self.controller.clear(group)
        points = np.asarray(path, dtype=float).reshape(-1, 3)
        if not len(points) or not np.isfinite(points).all():
            return
        scene = self.server.scene
        if len(points) > 1:
            self.controller._keep(group, scene.add_line_segments('/'+group+'/path',
                points=np.stack((points[:-1], points[1:]), axis=1), colors=color,
                thickness=3., thickness_units='screen'))
        # Keep text explanations in the panel, never across the point cloud.

    def _connection_report(self, report, *, adopted=False):
        fields = {k: report.get(k) for k in ('valid', 'node_id', 'evidence', 'missing', 'reasons',
                                            'time_mode', 'replay_reference_time', 'run_at')}
        self.connection_info.value = ('已明确采用（仅离线预览）\n' if adopted else '仅检查，未自动采用\n') + json.dumps(fields, ensure_ascii=False, indent=2)
        self.controller.clear('attachment')
        if report['valid']:
            self._draw_preview('attachment', report['path'],
                ('已采用的相机接入' if adopted else '候选接入｜尚未采用')+' → '+str(report['node_id']), (250, 175, 70))

    def _inspect_connection(self, _):
        graph = self._read_graph()
        report = validate_start_connection(graph, self.current_start or {}, now=utc_now(),
                                            target=self.target_result, **self._planning_time())
        self._connection_report(report, adopted=bool((self.current_start or {}).get('attachment')))

    def _show_connection_node(self, _):
        graph = self._read_graph()
        node = graph['nodes'].get(self.connection_node.value)
        if not node:
            raise ValueError('select_graph_node')
        self.controller.pose('connection_node', node['T_MC'], color=(245, 175, 75))

    def _inspect_attachment(self, _):
        self._invalidate_plan()
        self._clear_connection_preview()
        if self.current_start:
            self.current_start.pop('attachment', None)
        graph = self._read_graph()
        imported = read_json(self.attachment_path.value)
        report = import_current_start(self.current_start, imported, graph, now=utc_now(), **self._planning_time())
        self._connection_report(report)
        if report['valid']:
            self._pending_attachment = dict(document=digest_json(imported), graph=graph['graph_sha256'],
                current=digest_json(self.current_start), time=self._planning_time())

    def _adopt_attachment(self, _):
        pending = self._pending_attachment
        if not pending:
            raise ValueError('inspect_valid_attachment_before_adoption')
        self._invalidate_plan()
        self._clear_connection_preview()
        graph = self._read_graph()
        imported = read_json(self.attachment_path.value)
        if pending != dict(document=digest_json(imported), graph=graph['graph_sha256'],
                           current=digest_json(self.current_start), time=self._planning_time()):
            raise ValueError('connection_inputs_changed_inspect_again')
        report = import_current_start(self.current_start, imported, graph, now=utc_now(), **self._planning_time())
        if report['valid']:
            source = self.attachment_source.value.strip()
            if not source or sha256_file(Path(source)) != report['evidence'].get('source_sha256'):
                raise ValueError('connection_source_file_required_or_hash_mismatch')
            check = validate_start_connection(graph, report['start'], now=utc_now(),
                                              target=self.target_result, **self._planning_time())
            if not check['valid']:
                self._connection_report(check)
                return
            self.current_start = report['start']
            atomic_json(self.sidecar/('current_start-'+uuid4().hex+'.json'), self.current_start)
            atomic_json(self.sidecar/('attachment_review-'+uuid4().hex+'.json'), {
                'reviewed_at': utc_now(), 'source_path': str(Path(source).resolve()),
                'source_sha256': sha256_file(Path(source)), 'binding': pending, 'execution_permitted': False})
        self._connection_report(report, adopted=report['valid'])

    def _apply_current(self, payload):
        self._invalidate_plan()
        self._clear_connection_preview()
        self.controller.load_query(payload, current=True)
        self.current_start = {**payload, 'source': 'current_photo',
            'captured_at': payload.get('capture_time'), 'localized_at': payload.get('localization_time'),
            'photo_sha256': payload.get('query_sha256'), 'T_MC': payload.get('T_WC_query')}
        atomic_json(self.sidecar/('current_start-'+uuid4().hex+'.json'), self.current_start)

    def _load_current_query(self, _):
        self._require_final()
        self.current_start = None
        self.controller.clear('start')
        self._invalidate_plan()
        self._clear_connection_preview()
        payload, photo = verified_current_query(self.current_query.value, self.controller.model, self.target_photo)
        self._apply_current(payload)
        self.current_photo.value = str(photo)
        self.capture_time.value = payload.get('capture_time') or ''
        self._message('Loaded verified independent photo; original capture/localization times retained. '
                      'Inspect/import connection evidence before planning.')

    def _prepare_review(self, _):
        self._require_final()
        self._review_revision += 1
        self.route_review = None
        self.controller.clear('review_candidate')
        self.review_candidate.options = ['(none)']
        self.review_candidate.value = '(none)'
        self.review_candidate_info.value = '正在从已校验的输入生成候选……'
        self.review_image.visible = False
        job = self._submit('prepare_route_review', {'max_gap_s': float(self.review_gap.value)})
        self._review_jobs[job['job_id']] = (self._review_revision, None)

    def _review_completion_current(self, state):
        job_id = state.get('job_id')
        if job_id is None:  # explicit synchronous result loading in tests/tools
            return True
        expected = (self._review_revision, (self.route_review or {}).get('review_sha256')
                    if state['kind'] == 'save_route_review' else None)
        return self._review_jobs.get(job_id) == expected

    def _set_review(self, review):
        self.route_review = deepcopy(review)
        self._refresh_review()

    def _refresh_review(self):
        review = self.route_review
        options = [c['candidate_id'] for c in review['candidates'] if c['status'] == 'PENDING'] or ['(none)']
        old = self.review_candidate.value
        self.review_candidate.options = options
        self.review_candidate.value = old if old in options else options[0]
        self.review_frame.options = [str(i) for i in range(len(review['timeline']))] or ['(none)']
        self.review_frame.value = self.review_frame.options[0]
        info = {k: review.get(k) for k in ('source_kind', 'capture_device_kind', 'source_path', 'source_sha256', 'diagnostics', 'warnings')}
        info.update(pending=len([c for c in review['candidates'] if c['status'] == 'PENDING']),
                    confirmed_segments=len(review['route_segments']), timeline_rows=len(review['timeline']))
        self.review_diagnostics.value = json.dumps(info, ensure_ascii=False, indent=2)[:16000]
        self.review_info.value = (f"待审核 {info['pending']} 段；已确认 {info['confirmed_segments']} 段；时间线 {info['timeline_rows']} 帧。\n"
            '请核对原始视频中的跳切、中断及未注册帧。时间连续本身不证明实际轨迹连续。')
        self._select_review_candidate(None)

    def _select_review_candidate(self, _):
        self.controller.clear('review_candidate')
        candidate = next((c for c in (self.route_review or {}).get('candidates', [])
                          if c['candidate_id'] == self.review_candidate.value and c['status'] == 'PENDING'), None)
        names = candidate['frame_names'] if candidate else ['(none)']
        self.review_first.options = self.review_last.options = names
        self.review_first.value, self.review_last.value = names[0], names[-1]
        self.candidate_diagnostics.value = json.dumps(candidate, ensure_ascii=False, indent=2)[:16000] if candidate else ''
        self.review_candidate_info.value = (f"本段 {len(names)} 帧\n首帧：{names[0]}\n尾帧：{names[-1]}\n状态：待审核，尚未作为路线边。"
                                            if candidate else '暂无待审核路段。')
        if candidate and candidate.get('pts_time_s'):
            times = candidate['pts_time_s']
            self.review_candidate_info.value += f'\n素材显示时间：{times[0]} — {times[-1]} 秒'
        if candidate and candidate['component_id'] == self.controller.model['component_id']:
            self._draw_preview('review_candidate', candidate['camera_path'],
                               '待审核历史路段｜尚未确认连续', (185, 100, 230))

    def _show_review_frame(self, _):
        if not self.route_review or self.review_frame.value == '(none)':
            raise ValueError('no_review_timeline')
        row = self.route_review['timeline'][int(self.review_frame.value)]
        self.review_diagnostics.value = json.dumps(row, ensure_ascii=False, indent=2)
        self.review_info.value = (f"正在回看第 {self.review_frame.value} 行；帧名：{row.get('frame_name', '未知')}\n"
            f"素材显示时间：{row.get('pts_time_s', '未知')} 秒；{'已注册' if row.get('registered') else '未注册'}\n"
            '风险与来源详见原始审核诊断；显示时间不等于传感器曝光时间。')
        name = row.get('frame_name')
        camera = next((c for c in self.controller.model['cameras'] if c['name'] == name), None)
        self.review_image.visible = False
        if camera and camera.get('image_path'):
            from .viser_viewer import _read_image
            self.review_image.image = _read_image(camera['image_path'])
            self.review_image.visible = True
        else:
            self.review_info.value += '\n当前分量没有该图像，请按记录的显示时间回看原视频。'

    def _review_decide(self, decision):
        self._require_final()
        if not self.route_review:
            raise ValueError('generate_candidates_first')
        self.route_review = review_route_candidate(self.route_review, self.review_candidate.value, decision,
            first_name=self.review_first.value, last_name=self.review_last.value,
            reviewer=self.reviewer.value, evidence_note=self.review_note.value, reviewed_at=utc_now())
        self._review_revision += 1
        self._refresh_review()

    def _save_review(self, _):
        self._require_final()
        if not self.route_review:
            raise ValueError('generate_candidates_first')
        review = self.route_review
        job = self._submit('save_route_review', {'input_sha256': review['input_sha256'],
            'decisions': deepcopy(review['decisions']), 'max_gap_s': review['max_gap_s'],
            'component_id': self.controller.model['component_id']})
        self._review_jobs[job['job_id']] = (self._review_revision, review['review_sha256'])
