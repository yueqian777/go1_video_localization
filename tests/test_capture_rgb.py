import io
import json
import tempfile
import threading
import time
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch, MagicMock
from contextlib import ExitStack

from PIL import Image

from go1loc.capture_wire import encode_frame, make_header, read_frame, validate_header
from go1loc.capture_transport import ConnectionConfig, ssh_command
from go1loc.capture_session import Recorder, save_snapshot
from go1loc.capture_import import load_recording


def jpeg(size=(1280, 720)):
    out = io.BytesIO()
    Image.new('RGB', size, (150, 40, 20)).save(out, format='JPEG')
    return out.getvalue()


def rgb_packet(seq=1, payload=None):
    payload = jpeg() if payload is None else payload
    header = make_header('b' * 32, 6, seq, 1280, 720, seq / 15., 1000. + seq,
                         source_kind='orbbec_rgb_mjpeg', pixel_format='MJPEG', stride=0,
                         payload_length=len(payload), source_encoding='MJPEG',
                         camera_serial='CP2B7530008K', requested_rate_hz=15.)
    return header, payload


class RGBTests(unittest.TestCase):
    def test_jpeg_wire_and_canonical_decode(self):
        from go1loc.capture_rgb import decode_rgb_frame
        h, p = read_frame(io.BytesIO(encode_frame(*rgb_packet())))
        frame = decode_rgb_frame(h, p)
        validate_header(frame.header)
        self.assertEqual(frame.header['pixel_format'], 'BGR8')
        self.assertEqual(frame.payload[:3], bytes(frame.rgb()[0, 0][::-1]))
        self.assertEqual(frame.preview_jpeg, p)
        self.assertEqual(frame.header['source_jpeg_crc32'], zlib.crc32(p))

    def test_reject_wrong_source_identity_size_or_corrupt_image(self):
        from go1loc.capture_rgb import decode_rgb_frame
        for field, value in [('camera_id', 1), ('camera_serial', 'another-camera'),
                             ('width', 640), ('source_encoding', 'IR')]:
            h, p = rgb_packet()
            h[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                encode_frame(h, p)
        for payload in [b'not-jpeg', jpeg((640, 480))]:
            h, p = read_frame(io.BytesIO(encode_frame(*rgb_packet(payload=payload))))
            with self.assertRaises(ValueError):
                decode_rgb_frame(h, p)

    def test_only_fixed_rgb_profile_and_manual_authorization(self):
        config = ConnectionConfig('pi@192.168.12.1', source_kind='orbbec_rgb_mjpeg',
                                  width=1280, height=720, rate=15)
        self.assertEqual(ssh_command(config, 6, authorized=True)[-1], 'go1-rgb-720p15 120')
        with self.assertRaises(PermissionError):
            ssh_command(config, 6)
        for cfg, cam in [(replace(config, rate=30), 6), (config, 1),
                         (ConnectionConfig('pi'), 6), (replace(config, jump='pi'), 6)]:
            with self.assertRaises(ValueError):
                ssh_command(cfg, cam, authorized=True)

    def test_recording_and_snapshot_preserve_decoded_pixels_and_provenance(self):
        from go1loc.capture_rgb import decode_rgb_frame
        frames = [decode_rgb_frame(*read_frame(io.BytesIO(encode_frame(*rgb_packet(i)))))
                  for i in range(1, 4)]
        with tempfile.TemporaryDirectory() as folder:
            recording = Recorder(Path(folder) / 'clip', frames[0])
            for frame in frames[1:]:
                recording.submit(frame)
            recording.finish()
            meta, rows = load_recording(recording.path)
            self.assertEqual(meta['source_kind'], 'orbbec_rgb_mjpeg')
            self.assertIn('camera_MJPEG_is_lossy', meta['limitations'])
            self.assertEqual(len(rows), 2)
            photo = save_snapshot(folder, 'rgb', frames[-1])
            self.assertEqual(Image.open(photo / 'source.png').size, (1280, 720))

    def test_multipart_parser_bounds_and_embedded_markers(self):
        from go1loc.capture_rgb_agent import read_mjpeg
        payload = b'\xff\xd8APP\xff\xd9inside\xff\xd9'
        packet = b'--ffmpeg\r\nContent-type: image/jpeg\r\nContent-length: %d\r\n\r\n' % len(payload) + payload + b'\r\n'
        stream = io.BytesIO(packet + packet)
        self.assertEqual(read_mjpeg(stream), payload)
        self.assertEqual(read_mjpeg(stream), payload)
        padded = payload + bytes(65536)
        padded_packet = b'--ffmpeg\r\nContent-type: image/jpeg\r\nContent-length: %d\r\n\r\n' % len(padded) + padded
        self.assertEqual(read_mjpeg(io.BytesIO(padded_packet)), padded)
        for data in [b'--bad\r\n', packet[:-5],
                     b'--ffmpeg\r\nContent-length: 99999999\r\n\r\n',
                     b'--ffmpeg\r\nX:' + b'a' * 5000]:
            with self.assertRaises(ValueError):
                read_mjpeg(io.BytesIO(data))

    def test_gate_rejects_shell_commands_and_bad_duration(self):
        from go1loc.capture_rgb_agent import parse_command
        self.assertEqual(parse_command('go1-rgb-720p15 600'), 600)
        for command in ['', 'id', 'go1-rgb-720p15 601', 'go1-rgb-720p15 0',
                        'go1-rgb-720p15 10; id', 'go1-rgb-720p15 nan']:
            with self.assertRaises(ValueError):
                parse_command(command)

    def test_sixth_camera_on_page_no_auto_connection(self):
        from go1loc.capture_web import CaptureWeb, CAMERAS
        with tempfile.TemporaryDirectory() as folder:
            app = CaptureWeb('missing.json', folder, port=0)
            try:
                self.assertEqual(CAMERAS[-1][0], 6)
                self.assertIsNone(app.controller.source)
                with self.assertRaises(PermissionError):
                    app.submit({'action': 'connect', 'mode': 'robot', 'camera': 6})
            finally:
                app.close()

    def test_reader_partial_line_eof_timeout_and_stop(self):
        from go1loc.capture_rgb_agent import BoundedReader, MAX_JPEG
        stop = threading.Event()
        reader = BoundedReader(9, stop)
        with patch('go1loc.capture_rgb_agent.select.select', return_value=([9], [], [])), \
                patch('go1loc.capture_rgb_agent.os.read', side_effect=[b'ab', b'c\r\nrest', b'']):
            self.assertEqual(reader.readline(20), b'abc\r\n')
            self.assertEqual(reader.read(4), b'rest')
            with self.assertRaisesRegex(ValueError, 'eof'):
                reader.read(1)
        with self.assertRaisesRegex(ValueError, 'limit'):
            reader.read(MAX_JPEG + 1)
        reader.deadline = 0
        with self.assertRaisesRegex(ValueError, 'timeout'):
            reader.read(1)
        stop.set()
        with self.assertRaisesRegex(ValueError, 'stopped'):
            reader.read(1)

    def test_discover_only_serial_bound_rgb_node_and_reject_ambiguity(self):
        from go1loc.capture_rgb_agent import discover
        # No real devices: model sysfs paths and descriptor reads on Windows.
        nodes = [MagicMock(), MagicMock()]
        root = MagicMock()
        root.glob.return_value = nodes
        for i, node in enumerate(nodes):
            node.name = 'video' + str(i)
            interface = MagicMock()
            node.__truediv__.side_effect = lambda key, iface=interface: (
                MagicMock(resolve=lambda: iface) if key == 'device' else MagicMock(read_text=lambda: '0'))
            interface.__truediv__.return_value.read_text.return_value = 'Orbbec Gemini 335L RGB Camera'
            interface.parent.__str__.return_value = '/sys/usb1'
            values = {'idVendor': '2bc5', 'idProduct': '0804', 'serial': 'CP2B7530008K', 'devnum': '8'}
            interface.parent.__truediv__.side_effect = lambda key, values=values: MagicMock(read_text=lambda: values[key])
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            discover(root)
        root.glob.return_value = nodes[:1]
        self.assertEqual(discover(root)[0], '/dev/video0')
        root.glob.return_value = []
        with self.assertRaises(ValueError):
            discover(root)

    def test_idle_check_fail_closed_and_owned_child_cleanup(self):
        from go1loc.capture_rgb_agent import require_idle, stop_child
        import subprocess
        with patch('go1loc.capture_rgb_agent.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 1, b'', b'')
            require_idle('/dev/video6')
            for code, out, err in [(0, b'123', b''), (1, b'', b'denied'), (2, b'', b'')]:
                run.return_value = subprocess.CompletedProcess([], code, out, err)
                with self.assertRaisesRegex(ValueError, 'busy'):
                    require_idle('/dev/video6')
        child = MagicMock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('owned', 2), 0]
        stop_child(child)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        stop_child(None)

    def test_output_short_writes_and_timeout(self):
        from go1loc.capture_rgb_agent import write_packet
        stop = threading.Event()
        with patch('go1loc.capture_rgb_agent.select.select', return_value=([], [9], [])), \
                patch('go1loc.capture_rgb_agent.os.write', side_effect=[BlockingIOError(), 2, 2]) as write:
            write_packet(9, b'abcd', stop)
            self.assertEqual(write.call_count, 3)
        stop.set()
        with self.assertRaisesRegex(ValueError, 'timeout'):
            write_packet(9, b'a', stop)

    def test_capture_finally_restores_on_success_and_failure(self):
        from go1loc import capture_rgb_agent as agent
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
                fcntl = MagicMock()
                fcntl.fcntl.return_value = 0
                fcntl.F_SETFL, fcntl.F_GETFL = 1, 2
                fcntl.LOCK_EX, fcntl.LOCK_NB = 1, 2
                stack.enter_context(patch.dict('sys.modules', {'fcntl': fcntl}))
                stack.enter_context(patch.object(agent.signal, 'signal'))
                stack.enter_context(patch.object(agent.signal, 'SIGHUP', 1, create=True))
                stack.enter_context(patch.object(agent.os, 'O_NONBLOCK', 2048, create=True))
                stack.enter_context(patch.object(agent.sys, 'stdout', MagicMock()))
                stack.enter_context(patch.object(agent.sys, 'stderr', io.StringIO()))
                stack.enter_context(patch.object(agent, 'discover', return_value=('/dev/video6', '/usb', '1')))
                idle = stack.enter_context(patch.object(agent, 'require_idle'))
                original = (bytearray(agent.FMT_SIZE), bytearray(204))
                params = stack.enter_context(patch.object(agent, 'parameters', return_value=original))
                child = MagicMock()
                child.poll.return_value = None
                stack.enter_context(patch.object(agent.subprocess, 'Popen', return_value=child))
                stack.enter_context(patch.object(agent, 'BoundedReader'))
                sent = threading.Event()
                count = [0]
                def read(_):
                    count[0] += 1
                    if count[0] > 1:
                        sent.wait(1.)
                        raise ValueError('rgb_capture_timeout')
                    return jpeg()
                def write(fd, packet, stop):
                    self.assertEqual(read_frame(io.BytesIO(packet))[0]['camera_id'], 6)
                    sent.set()
                    stop.set()
                    if fail:
                        raise ValueError('transport_write_timeout')
                stack.enter_context(patch.object(agent, 'read_mjpeg', side_effect=read))
                stack.enter_context(patch.object(agent, 'write_packet', side_effect=write))
                result = agent.capture(1, Path(folder))
                report = json.loads((Path(folder) / 'last_capture.json').read_text())
                self.assertEqual(result, int(fail))
                self.assertTrue(report['restored'])
                self.assertEqual(params.call_args.args, ('/dev/video6', original))
                self.assertEqual(idle.call_count, 2)
                child.terminate.assert_called_once()

    def test_parameters_restore_and_close_on_ioctl_error(self):
        from go1loc import capture_rgb_agent as agent
        with patch.dict('sys.modules', {'fcntl': MagicMock()}) as modules, \
                patch.object(agent.os, 'O_NONBLOCK', 2048, create=True), \
                patch.object(agent.os, 'open', return_value=99), patch.object(agent.os, 'close') as close:
            result = agent.parameters('/dev/video6')
            agent.parameters('/dev/video6', result)
            self.assertTrue(agent.same_parameters(result, result))
            modules['fcntl'].ioctl.side_effect = OSError('test')
            with self.assertRaises(OSError):
                agent.parameters('/dev/video6')
            self.assertEqual(close.call_count, 3)

    def test_preview_serves_original_jpeg_without_reencoding(self):
        from go1loc.capture_rgb import decode_rgb_frame
        from go1loc.capture_web import CaptureWeb
        from tests.test_capture_control import ManualSource
        frame = decode_rgb_frame(*read_frame(io.BytesIO(encode_frame(*rgb_packet()))))
        with tempfile.TemporaryDirectory() as folder:
            app = CaptureWeb('missing.json', folder, port=0)
            try:
                source = ManualSource()
                app.controller.connect(source)
                source.on_frame(frame)
                with patch('go1loc.capture_web.Image.fromarray') as reencode:
                    self.assertEqual(app.preview(), frame.preview_jpeg)
                    reencode.assert_not_called()
            finally:
                app.close()


if __name__ == '__main__':
    unittest.main()
