import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from go1loc import capture_synthetic
from go1loc.capture_ui import CaptureUI
from go1loc.capture_wire import read_frame


class Handle:
    def __init__(self, value=None):
        self.value, self.disabled, self.visible = value, False, True

    def on_click(self, callback):
        self.callback = callback


class FakeGui:
    def configure_theme(self, **kwargs):
        pass

    def add_markdown(self, *args, **kwargs):
        pass

    def add_dropdown(self, label, values):
        return Handle(values[0])

    def add_checkbox(self, label, value):
        return Handle(value)

    def add_text(self, label, value, **kwargs):
        return Handle(value)

    def add_image(self, image, **kwargs):
        handle = Handle()
        handle.image = image
        return handle

    def add_button(self, label):
        return Handle()


def wait_until(predicate, timeout=4.):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('timed out waiting for tested state')
        time.sleep(.01)


class UITests(unittest.TestCase):
    def test_freeze_warning_and_recovery_are_visible_without_stopping_recording(self):
        from dataclasses import replace

        from tests.test_capture_control import ManualSource
        from tests.test_capture_session import frame
        server = SimpleNamespace(gui=FakeGui(), stop=lambda: None)
        with tempfile.TemporaryDirectory() as t:
            app = CaptureUI('unused', synthetic=True, server=server, output=t)
            try:
                source = ManualSource()
                app.controller.health.freeze_after = .01
                app.controller.connect(source)
                source.on_frame(frame())
                app.controller.start_recording(t, 'freeze')
                time.sleep(.02)
                source.on_frame(frame(2))
                wait_until(lambda: '疑似冻结' in app.health_status.value)
                self.assertTrue(app.preview.visible)
                self.assertEqual(app.controller.state, 'recording')
                source.on_frame(replace(frame(3), payload=b'x' + frame().payload[1:]))
                wait_until(lambda: '已观察到画面内容变化' in app.health_status.value)
                source.on_end('injected_failure')
                wait_until(lambda: '故障退出' in app.health_status.value)
                self.assertFalse(app.preview.visible)
            finally:
                app.close()

    def test_gui_callbacks_source_png_preview_independence_and_export(self):
        server = SimpleNamespace(gui=FakeGui(), stop=lambda: None)
        with tempfile.TemporaryDirectory() as t:
            app = CaptureUI('not_loaded_for_synthetic', synthetic=True, server=server, output=t)
            try:
                self.assertEqual(app.controller.state, 'disconnected')
                with self.assertRaises(PermissionError):
                    app.connect()
                app.authorize.value = True
                app.buttons['连接所选相机'].callback(None)
                wait_until(lambda: app.controller.state == 'previewing' and not app.busy.locked())
                app.buttons['开始录制'].callback(None)
                wait_until(lambda: app.controller.state == 'recording' and not app.busy.locked())
                wait_until(lambda: app.controller.recorder.written >= 2)
                # Modify ONLY a display copy; saved pixels come from immutable bytes.
                _, source = app.controller.view()
                original = source.payload
                preview = source.rgb()
                preview[:] = 0
                self.assertEqual(source.payload, original)
                app.buttons['保存下一张源图'].callback(None)
                wait_until(lambda: not app.busy.locked())
                app.buttons['停止录制（不停止机器人）'].callback(None)
                wait_until(lambda: app.controller.state == 'previewing' and not app.busy.locked())
                app.buttons['导出最近录像 FFV1 MKV'].callback(None)
                wait_until(lambda: not app.busy.locked())
                self.assertIsNone(app.action_error)
                self.assertTrue(list(Path(t).rglob('source.mkv')))
                self.assertIn('SYNTHETIC', app.status.value)
                app.buttons['断开 / 换相机前先断开'].callback(None)
                wait_until(lambda: app.controller.state == 'disconnected' and not app.busy.locked())
                wait_until(lambda: not app.preview.visible)
            finally:
                app.close()

    def test_busy_repeat_click_and_error_reporting(self):
        server = SimpleNamespace(gui=FakeGui(), stop=lambda: None)
        app = CaptureUI('unused', server=server)
        gate = threading.Event()
        try:
            self.assertTrue(app.submit(lambda: gate.wait(1.)))
            self.assertFalse(app.submit(lambda: None))
            gate.set()
            wait_until(lambda: not app.busy.locked())
            app.submit(app.export)
            wait_until(lambda: not app.busy.locked())
            self.assertIn('stop_recording', app.action_error)
        finally:
            gate.set()
            app.close()

    def test_synthetic_generator_is_labelled_and_has_binary_frames(self):
        import io
        output = io.BytesIO()
        with patch.object(capture_synthetic.sys, 'stdout', SimpleNamespace(buffer=output)):
            capture_synthetic.main(['--seconds', '.04', '--rate', '100'])
        output.seek(0)
        header, payload = read_frame(output)
        self.assertEqual(header['source_kind'], 'synthetic_test')
        self.assertEqual(len(payload), 464 * 400 * 3)
