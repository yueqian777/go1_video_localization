"""First-build Viser monitor. Reads only run metadata and immutable READY models."""
from pathlib import Path
import threading
import time
from uuid import uuid4

from .build_watch import read_build_state
from .events import BoundedEventWriter, outside_artifacts
from .jobs import JobManager, read_json
from .viser_viewer import SceneController, framing


class BuildWatchApplication:
    def __init__(self, run_dir, sidecar_dir, *, port=8766, server=None):
        self.run_dir = Path(run_dir).resolve()
        initial = read_build_state(self.run_dir)
        if initial['run'] is None:
            raise ValueError('run_metadata_not_ready: start build-map and use its published run directory')
        self.map_dir = Path(initial['run']['output_dir']).resolve()
        self.sidecar = outside_artifacts(Path(sidecar_dir), self.run_dir, self.map_dir,
                                        Path(initial['run']['image_dir']))
        self.sidecar.mkdir(parents=True, exist_ok=True)
        if not 1 <= port <= 65535:
            raise ValueError('invalid_port')
        if server is None:
            import viser
            server = viser.ViserServer(host='127.0.0.1', port=port, label='Go1 首次建图监看')
        self.server = server
        self.controller = SceneController(server)
        self.jobs = JobManager(self.map_dir, self.sidecar)
        self.records = BoundedEventWriter(self.sidecar/('watch-'+uuid4().hex+'.jsonl'))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._active = None
        self._pending = None
        self._last_key = None
        self._generation = 0
        self._following = True
        self.state = initial
        gui = server.gui
        gui.configure_theme(control_width='large', show_share_button=False, brand_color=(31, 116, 151))
        gui.add_markdown('## 首次建图监看\n不需要最终地图。只读取已完整发布、通过校验的真实快照。\n\n'
                         '**勾选自动跟随**：跟随最新快照，完成后切到冻结地图。\n\n'
                         '**查看历史**：选择序号并暂停跟随。建图失败仍保留已有快照。\n\n'
                         '本窗口不连接机器狗，没有运动控制，也不把未对齐的查询或路线叠加到快照。')
        self.status = gui.add_text('建图阶段与事件（保留原始代码）', '等待完整快照。', multiline=True, disabled=True)
        self.display = gui.add_text('当前显示的模型', '尚无完整快照。', multiline=True, disabled=True)
        self.follow = gui.add_checkbox('自动跟随最新快照与最终地图', True)
        self.follow.on_update(self._guard(self._follow_changed))
        self.snapshot = gui.add_dropdown('选择历史快照序号', ['(none)'])
        self._button('暂停跟随并查看所选快照', self.load_history)
        self._button('加载已完成的冻结地图', self.load_final)
        self._button('保存三维画布截图', self.screenshot)
        server.on_client_connect(self._connect)
        self.records.emit('watch_started', data={'frozen_manifest_exists': (self.map_dir/'map_manifest.json').exists(),
                                                'run_dir': str(self.run_dir)})
        self._thread = threading.Thread(target=self._poll, daemon=True, name='go1loc-build-watch')
        self._thread.start()

    def _guard(self, callback):
        def guarded(event):
            with self._lock:
                try:
                    callback(event)
                except Exception as exc:
                    self.display.value = f'{type(exc).__name__}: {exc}'
        return guarded

    def _button(self, label, callback):
        handle = self.server.gui.add_button(label)
        handle.on_click(self._guard(callback))

    def _follow_changed(self, _):
        if self._following == bool(self.follow.value):
            return  # programmatic checkbox update may emit its callback later
        self._following = bool(self.follow.value)
        self._generation += 1
        self._pending = None
        self._last_key = None

    def _connect(self, client):
        self.controller.on_client_connect(client)
        if self.controller.model:
            center, position, up = framing(self.controller.model)
            client.camera.position = position
            client.camera.up_direction = up
            client.camera.look_at = center

    def _queue(self, kind, params, key, *, manual=False):
        self._pending = (kind, params, key, self._generation, manual)
        self._dispatch()

    def _dispatch(self):
        if self._active is not None or self._pending is None:
            return
        kind, params, key, generation, manual = self._pending
        job = self.jobs.submit(kind, params)
        self._active = (job['job_id'], key, generation, manual)
        self._pending = None

    def tick(self):
        with self._lock:
            self.state = read_build_state(self.run_dir)
            state = self.state
            events = state.get('events', [])
            stage = state.get('stage')
            phase = {'features': '特征提取', 'extraction': '特征提取', 'matching': '图像匹配', 'mapping': '三维重建', 'prepare': '准备输入'}.get(stage, '阶段详见原始代码')
            status = {'running': '进行中', 'completed': '已完成', 'failed': '失败（保留已有快照）', 'cancelled': '已取消'}.get(state['status'], '等待')
            self.status.value = (f"{status} | {phase}（{stage}） | "
                f"完整快照 {len(state['snapshots'])} 份 | 最近事件 {events[-1].get('sequence') if events else '无'}\n"
                f"{state.get('failure_reason') or ''}\nrun={self.run_dir}")
            options = [str(s['snapshot_index']) for s in state['snapshots']] or ['(none)']
            if list(self.snapshot.options) != options:
                old = self.snapshot.value
                self.snapshot.options = options
                self.snapshot.value = old if old in options else options[-1]
            if self.follow.value and self._active is None:
                if state.get('final_map_dir'):
                    key = ('final', str(state['final_map_dir']))
                    if key != self._last_key:
                        self._queue('inspect_map', {}, key)
                elif state['snapshots']:
                    latest = state['snapshots'][-1]
                    key = ('snapshot', latest['component_id'], latest['snapshot_index'])
                    if key != self._last_key:
                        self._queue('inspect_build_snapshot', {'run_dir': str(self.run_dir),
                                    'snapshot_index': len(state['snapshots'])-1}, key)
            if self._active:
                job = self.jobs.status(self._active[0]) if hasattr(self.jobs, 'status') else None
                if job and job['status'] in ('completed', 'failed'):
                    self.complete(job)
            self._dispatch()

    def complete(self, state):
        active, self._active = self._active, None
        if active is None:
            return
        _, key, generation, manual = active
        if generation != self._generation or (not manual and not self.follow.value):
            return
        self._last_key = key
        if state['status'] == 'failed':
            self.display.value = '模型读取失败；上一份完整模型保留。原始原因：'+state.get('error', '')
            self.records.emit('display_failed', data={'error': state.get('error'), 'selection': key})
            return
        payload = read_json(state['result_path'])
        self.controller.replace_model(payload)
        self.display.value = (f"{'最终地图' if payload['mode'] == 'final_map_replay' else '真实过程快照'} | {len(payload['cameras'])} 个相机位姿 | {payload['total_points']} 个点\n"
                              f"分量：{payload['component_id']} | 不假定快照与最终地图已对齐")
        self.records.emit('display_loaded', component_id=payload['component_id'],
                          registered_images=len(payload['cameras']), points3D=payload['total_points'],
                          data={'mode': payload['mode'], 'revision': payload['revision'],
                                'automatic': not manual, 'frozen_manifest_exists': (self.map_dir/'map_manifest.json').exists()})
        for client in self.server.get_clients().values():
            self._connect(client)

    def load_history(self, _):
        self.follow.value = False
        self._follow_changed(None)
        self._generation += 1
        self._pending = None
        rows = self.state['snapshots']
        index = next((i for i, row in enumerate(rows) if str(row['snapshot_index']) == self.snapshot.value), None)
        if index is None:
            raise ValueError('no_ready_snapshot_selected')
        self._queue('inspect_build_snapshot', {'run_dir': str(self.run_dir), 'snapshot_index': index},
                    ('manual', rows[index]['component_id'], rows[index]['snapshot_index']), manual=True)

    def load_final(self, _):
        if not self.state.get('final_map_dir'):
            raise ValueError('final_map_not_ready')
        self.follow.value = False
        self._follow_changed(None)
        self._generation += 1
        self._pending = None
        self._queue('inspect_map', {}, ('final', str(self.state['final_map_dir'])), manual=True)

    def screenshot(self, event):
        if not event.client:
            raise ValueError('screenshot_requires_browser_client')
        from PIL import Image
        path = self.sidecar/('screenshot-'+uuid4().hex+'.png')
        Image.fromarray(event.client.get_render(height=720, width=1280, timeout=15.)).save(path)
        self.display.value = '三维画布截图已保存：'+str(path)

    def _poll(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                self.status.value = f'{type(exc).__name__}: {exc}; last complete model retained'
            self._stop.wait(.5)

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.jobs.close()
        self.server.stop()


def watch_build(run_dir, sidecar_dir, *, port=8766):
    app = BuildWatchApplication(run_dir, sidecar_dir, port=port)
    try:
        while True:
            time.sleep(.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
