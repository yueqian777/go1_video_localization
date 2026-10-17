"""Standalone loopback-only Viser UI. Never connects on startup."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import numpy as np

from .capture_control import CaptureController
from .capture_transport import PipeSource, load_connection


def health_message(status):
    if status['state'] == 'error':
        return '采集已故障退出，旧画面已隐藏；录像不会自动续录。'
    if not status['transport_live']:
        return '尚无新数据或接收已超时；不显示旧画面为实时预览。'
    if status['content_status'] == 'suspected_frozen':
        return ('警告：画面疑似冻结，连续 {:.1f} 秒像素完全相同。数据包仍到达；'
                '也可能是静止场景。源曝光时间未知，请勿把旧图当作实时观察依据。'
                .format(status['unchanged_s']))
    return '数据正在接收；' + ('已观察到画面内容变化。' if status['content_status'] == 'changing' else
                              '尚未观察到画面变化。') + '源曝光新鲜度未验证。'


class CaptureUI:
    def __init__(self, config_path, *, port=8767, synthetic=False, output='data/captures', server=None):
        if server is None:
            import viser
            server = viser.ViserServer(host='127.0.0.1', port=port, label='Go1 manual camera capture')
        self.server, self.config_path, self.synthetic = server, Path(config_path), synthetic
        self.controller = CaptureController()
        self.stop = threading.Event()
        self.busy = threading.Lock()
        self.action_error = None
        self.action_thread = None
        gui = server.gui
        gui.configure_theme(control_width='large', show_share_button=False)
        gui.add_markdown('## Go1 源图预览与手动录像\n'
                         '仅采集图像；停止录像不是机器人急停。断线不会自动续录。\n'
                         + ('**合成测试模式：不是机器狗图像。**' if synthetic else
                            '首次部署、SSH 主机指纹和布局须先按手册确认。'))
        self.camera = gui.add_dropdown('Camera / 相机', ['1 front', '2 chin', '3 left', '4 right', '5 rear-down'])
        self.authorize = gui.add_checkbox('确认只读连接所选板卡（不会自动部署）', False)
        self.folder = gui.add_text('保存目录', str(output))
        self.name = gui.add_text('会话名称', 'go1_camera')
        self.preview = gui.add_image(np.zeros((2, 2, 3), np.uint8), label='源图显示副本 / 无 UI 烧录', format='png', visible=False)
        self.status = gui.add_text('状态 / 计数 / 错误', 'disconnected', multiline=True, disabled=True)
        self.health_status = gui.add_text('画面健康提示（不是曝光时间证明）', '等待连接', multiline=True, disabled=True)
        self.buttons = {}
        for label, callback in [('连接所选相机', self.connect), ('断开 / 换相机前先断开', self.controller.disconnect),
                                ('开始录制', lambda: self.controller.start_recording(self.folder.value, self.name.value)),
                                ('停止录制（不停止机器人）', self.controller.stop_recording),
                                ('保存下一张源图', lambda: self.controller.snapshot(self.folder.value, self.name.value)),
                                ('导出最近录像 FFV1 MKV', self.export)]:
            button = gui.add_button(label)
            button.on_click(lambda event, cb=callback: self.submit(cb))
            self.buttons[label] = button
        self.poller = threading.Thread(target=self._poll, daemon=True, name='preview-only')
        self.poller.start()

    def submit(self, callback):
        if not self.busy.acquire(blocking=False):
            return False
        def run():
            try:
                self.action_error = None
                callback()
            except Exception as exc:  # noqa: BLE001 - GUI action boundary displays the failure
                self.action_error = type(exc).__name__ + ': ' + str(exc)
            finally:
                self.busy.release()
        self.action_thread = threading.Thread(target=run, daemon=True, name='capture-ui-action')
        self.action_thread.start()
        return True

    def connect(self):
        camera = int(self.camera.value.split()[0])
        if not self.authorize.value:
            raise PermissionError('请先勾选手动只读连接确认')
        if self.synthetic:
            source = PipeSource([sys.executable, '-m', 'go1loc.capture_synthetic',
                                 '--camera', str(camera)], camera, source_kind='synthetic_test')
        else:
            from .capture_adapter import CaptureConfig, Go1ReadonlyShmAdapter
            source = Go1ReadonlyShmAdapter(CaptureConfig(mode='go1_readonly_shm'),
                        connection=load_connection(self.config_path, camera), camera_id=camera, authorized=True).capture()
        self.controller.connect(source)

    def export(self):
        from .capture_import import export_ffv1
        status, _ = self.controller.view()
        if status['state'] in ('recording', 'finalizing') or not status['output']:
            raise RuntimeError('stop_recording_before_export')
        export_ffv1(Path(status['output']))

    def _poll(self):
        last = None
        while not self.stop.wait(.2):
            status, frame = self.controller.view()
            status['ui_action_error'] = self.action_error
            status['mode'] = 'SYNTHETIC TEST' if self.synthetic else 'GO1 READONLY (field validation required)'
            self.health_status.value = health_message(status)
            self.status.value = ('{mode}\n{state} | transport_live={transport_live} | content={content_status}\n'
                                 '接收 {received} | 本次/最近录像写盘 {written} | 丢弃 {dropped} | 队列 {queue}\n'
                                 '平均接收 {receive_rate_hz:.2f} reads/s（不是曝光 FPS）\n'
                                 '输出: {output}\n错误: {error}\n操作: {ui_action_error}').format(**status)
            self.camera.disabled = status['state'] != 'disconnected' or self.busy.locked()
            self.preview.visible = frame is not None
            if frame is not None and frame is not last:
                self.preview.image = frame.rgb()  # independent copy; no PNG writer dependency
                last = frame
            for button in self.buttons.values():
                button.disabled = self.busy.locked()

    def close(self):
        self.stop.set()
        if self.action_thread:
            self.action_thread.join(timeout=6.)
        self.controller.disconnect()
        self.poller.join(timeout=2.)
        self.server.stop()


def serve_capture(config_path, **kwargs):
    app = CaptureUI(config_path, **kwargs)
    try:
        while not app.stop.wait(.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
