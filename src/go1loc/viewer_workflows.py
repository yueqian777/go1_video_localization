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
        gui.add_markdown('### Time and independent current-photo connection\n'
            'live_current checks 30-second freshness. offline_replay keeps real capture time; '
            'both are non-executable previews. Imported evidence is never auto-approved.')
        self.time_mode = gui.add_dropdown('Planning time mode', ['live_current', 'offline_replay'])
        self.replay_time = gui.add_text('Replay reference time (ISO8601 + timezone)', '')
        self.time_mode.on_update(self._guard(self._workflow_context_changed))
        self.replay_time.on_update(self._guard(self._workflow_context_changed))
        self.graph_path.on_update(self._guard(self._workflow_context_changed))
        self.current_query = gui.add_text('Saved INDEPENDENT current query directory', '')
        self._button('Load verified current query (no automatic attachment)', self._load_current_query)
        self.connection_info = gui.add_text('Connection evidence / missing fields', 'No current connection inspected', multiline=True, disabled=True)
        self.connection_node = gui.add_dropdown('Graph nodes (candidates, NOT automatic attachment)', ['(none)'])
        self._button('Inspect graph and current connection', self._inspect_connection)
        self._button('Show selected candidate node only', self._show_connection_node)
        self.attachment_path = gui.add_text('Import current_start or attachment JSON path', '')
        self.attachment_source = gui.add_text('Connection source file (required to adopt; SHA256 checked)', '')
        self._button('Inspect imported connection (do not adopt)', self._inspect_attachment)
        self._button('Adopt inspected evidence for OFFLINE preview', self._adopt_attachment)
        gui.add_markdown('Review the original source before adopting. Hashes prove file consistency, '
            'not that its claimed trajectory is physically continuous. A single photo alone cannot supply this evidence.')
        gui.add_markdown('### Historical segment review\nCandidates start PENDING. '
            'Inspect the original video for unmarked cuts; PTS continuity alone is not proof.')
        self.review_gap = gui.add_number('Maximum candidate time gap (seconds)', 2., min=.01, step=.1)
        self._button('Generate PENDING candidates from frozen map inputs', self._prepare_review)
        self.review_info = gui.add_text('Review source / timeline / diagnostics', 'No review loaded', multiline=True, disabled=True)
        self.review_candidate = gui.add_dropdown('PENDING segment', ['(none)'])
        self.review_candidate_info = gui.add_text('Selected segment frames / real PTS', 'No pending segment', multiline=True, disabled=True)
        self.review_candidate.on_update(self._guard(self._select_review_candidate))
        self.review_first = gui.add_dropdown('Selected range first frame', ['(none)'])
        self.review_last = gui.add_dropdown('Selected range last frame', ['(none)'])
        self.review_frame = gui.add_dropdown('Inspect timeline row (including unregistered)', ['(none)'])
        self._button('Show review frame / PTS / registration', self._show_review_frame)
        self.reviewer = gui.add_text('Reviewer name (required)', '')
        self.review_note = gui.add_text('Continuity evidence / split / rejection note (required)', '', multiline=True)
        self._button('Split selected range; children stay PENDING', lambda _: self._review_decide('split'))
        self._button('Confirm entire reviewed segment', lambda _: self._review_decide('confirm'))
        self._button('Reject entire segment', lambda _: self._review_decide('reject'))
        self._button('Save reviewed segments and directed graph', self._save_review)

    def _planning_time(self):
        mode = self.time_mode.value
        reference = self.replay_time.value.strip() or None
        _time_context(utc_now(), mode, reference)
        return dict(time_mode=mode, replay_reference_time=reference)

    def _clear_connection_preview(self):
        self._pending_attachment = None
        self.controller.clear('attachment')
        self.controller.clear('connection_node')
        self.connection_info.value = 'Connection must be inspected for this start / graph / time.'

    def _workflow_context_changed(self, _):
        self._invalidate_plan()
        self._clear_connection_preview()

    def _reset_workflows(self):
        self._clear_connection_preview()
        self._review_revision += 1
        self.route_review = None
        self.controller.clear('review_candidate')
        self.review_info.value = 'Map/component changed; generate a new review.'
        self.review_candidate_info.value = 'No pending segment'
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
        self.controller._keep(group, scene.add_label('/'+group+'/label', text=text, position=points[0]))

    def _connection_report(self, report, *, adopted=False):
        fields = {k: report.get(k) for k in ('valid', 'node_id', 'evidence', 'missing', 'reasons',
                                            'time_mode', 'replay_reference_time', 'run_at')}
        self.connection_info.value = ('ADOPTED (offline only)\n' if adopted else 'INSPECTION ONLY\n') + json.dumps(fields, ensure_ascii=False, indent=2)
        self.controller.clear('attachment')
        if report['valid']:
            self._draw_preview('attachment', report['path'],
                ('ADOPTED camera connection' if adopted else 'CANDIDATE connection — not adopted')+' -> '+str(report['node_id']), (250, 175, 70))

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
        self.controller.pose('connection_node', node['T_MC'], color=(245, 175, 75),
                             label='Candidate node only — NO connection created')

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
        self.review_candidate_info.value = 'Generating candidates from verified inputs...'
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
        self.review_info.value = json.dumps(info, ensure_ascii=False, indent=2)[:16000]
        self._select_review_candidate(None)

    def _select_review_candidate(self, _):
        self.controller.clear('review_candidate')
        candidate = next((c for c in (self.route_review or {}).get('candidates', [])
                          if c['candidate_id'] == self.review_candidate.value and c['status'] == 'PENDING'), None)
        names = candidate['frame_names'] if candidate else ['(none)']
        self.review_first.options = self.review_last.options = names
        self.review_first.value, self.review_last.value = names[0], names[-1]
        self.review_candidate_info.value = (json.dumps(candidate, ensure_ascii=False, indent=2)[:16000]
                                            if candidate else 'No pending segment')
        if candidate and candidate['component_id'] == self.controller.model['component_id']:
            self._draw_preview('review_candidate', candidate['camera_path'],
                               'PENDING temporal candidate — NOT approved route', (185, 100, 230))

    def _show_review_frame(self, _):
        if not self.route_review or self.review_frame.value == '(none)':
            raise ValueError('no_review_timeline')
        row = self.route_review['timeline'][int(self.review_frame.value)]
        self.review_info.value = json.dumps(row, ensure_ascii=False, indent=2)
        name = row.get('frame_name')
        camera = next((c for c in self.controller.model['cameras'] if c['name'] == name), None)
        self.reference_image.visible = False
        if camera and camera.get('image_path'):
            from .viser_viewer import _read_image
            self.reference_image.image = _read_image(camera['image_path'])
            self.reference_image.visible = True
        else:
            self.review_info.value += '\nImage unavailable in this component; review original video at this PTS.'

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
