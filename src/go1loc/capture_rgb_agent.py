"""Independent Pi RGB-only MJPEG agent. Standard library; no services or motion.

The fixed USB identity, RGB interface and profile are deliberate allowlists.
Only an owned ffmpeg child is signalled. Format changes are temporary and restored.
"""
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import select
import signal
import struct
import subprocess
import sys
import threading
import time
from uuid import uuid4

try:
    from .capture_wire import encode_frame, make_header
except ImportError:
    from capture_wire import encode_frame, make_header

SERIAL = 'CP2B7530008K'
KIND = 'orbbec_rgb_mjpeg'
MAX_JPEG = 2 * 1024 * 1024


def parse_command(command):
    match = re.fullmatch(r'go1-rgb-720p15 ([1-9][0-9]{0,2})', command)
    if not match or not 1 <= int(match.group(1)) <= 600:
        raise ValueError('rgb_command_denied')
    return int(match.group(1))


def read_mjpeg(stream):
    """Parse ffmpeg mpjpeg's explicit lengths, never scan JPEG bytes for EOI."""
    boundary = stream.readline(128)
    if boundary == b'\r\n':
        boundary = stream.readline(128)
    if boundary != b'--ffmpeg\r\n':
        raise ValueError('rgb_invalid_multipart_boundary')
    fields = {}
    for _ in range(8):
        line = stream.readline(256)
        if line == b'\r\n':
            break
        if not line.endswith(b'\r\n') or b':' not in line:
            raise ValueError('rgb_invalid_multipart_header')
        key, value = line[:-2].split(b':', 1)
        key = key.lower()
        if key in fields:
            raise ValueError('rgb_duplicate_multipart_header')
        fields[key] = value.strip()
    else:
        raise ValueError('rgb_multipart_header_limit')
    length = fields.get(b'content-length', b'')
    if fields.get(b'content-type') != b'image/jpeg' or not length.isdigit() or not 4 <= int(length) <= MAX_JPEG:
        raise ValueError('rgb_invalid_multipart_length')
    payload = stream.read(int(length))
    # This camera pads the UVC MJPEG buffer with zero bytes after EOI.
    # Keep the complete length-delimited payload/CRC; permit only zero padding.
    if len(payload) != int(length) or not payload.startswith(b'\xff\xd8') or not payload.rstrip(b'\x00').endswith(b'\xff\xd9'):
        raise ValueError('rgb_truncated_or_invalid_jpeg')
    return payload


class BoundedReader:
    def __init__(self, fd, stop, timeout=5.):
        self.fd, self.stop, self.timeout = fd, stop, timeout
        self.buffer = bytearray()
        self.deadline = time.monotonic() + timeout

    def _fill(self):
        while True:
            if self.stop.is_set():
                raise ValueError('rgb_capture_stopped')
            if time.monotonic() > self.deadline:
                raise ValueError('rgb_capture_timeout')
            if select.select([self.fd], [], [], .1)[0]:
                part = os.read(self.fd, 65536)
                if not part:
                    raise ValueError('rgb_capture_eof')
                self.buffer.extend(part)
                return

    def read(self, count):
        if count > MAX_JPEG:
            raise ValueError('rgb_read_limit')
        while len(self.buffer) < count:
            self._fill()
        out = bytes(self.buffer[:count])
        del self.buffer[:count]
        return out

    def readline(self, limit):
        while b'\n' not in self.buffer[:limit] and len(self.buffer) < limit:
            self._fill()
        length = self.buffer.find(b'\n', 0, limit) + 1
        return self.read(length or limit)


def discover(root=Path('/sys/class/video4linux')):
    found = []
    for node in root.glob('video*'):
        try:
            interface = (node / 'device').resolve()
            usb = interface.parent
            if ((node / 'index').read_text().strip() == '0'
                    and (interface / 'interface').read_text().strip() == 'Orbbec Gemini 335L RGB Camera'
                    and (usb / 'idVendor').read_text().strip() == '2bc5'
                    and (usb / 'idProduct').read_text().strip() == '0804'
                    and (usb / 'serial').read_text().strip() == SERIAL):
                found.append(('/dev/' + node.name, str(usb), (usb / 'devnum').read_text().strip()))
        except OSError:
            continue
    if len(found) != 1:
        raise ValueError('rgb_camera_missing_or_ambiguous')
    return found[0]


def require_idle(device):
    result = subprocess.run(['/bin/fuser', device], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
    if result.returncode != 1 or result.stdout or result.stderr:
        raise ValueError('rgb_camera_busy')


FMT_SIZE = 208 if struct.calcsize('P') == 8 else 204
FMT_OFFSET = 8 if struct.calcsize('P') == 8 else 4
G_FMT = 0xc0005604 | (FMT_SIZE << 16)
S_FMT = 0xc0005605 | (FMT_SIZE << 16)


def parameters(device, restore=None):
    import fcntl
    fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
    try:
        if restore is not None:
            fcntl.ioctl(fd, S_FMT, bytearray(restore[0]), True)
            fcntl.ioctl(fd, 0xc0cc5616, bytearray(restore[1]), True)
        fmt, parm = bytearray(FMT_SIZE), bytearray(204)
        struct.pack_into('I', fmt, 0, 1)
        struct.pack_into('I', parm, 0, 1)
        fcntl.ioctl(fd, G_FMT, fmt, True)
        fcntl.ioctl(fd, 0xc0cc5615, parm, True)
        return fmt, parm
    finally:
        os.close(fd)


def same_parameters(a, b):
    return a[0][FMT_OFFSET:FMT_OFFSET+48] == b[0][FMT_OFFSET:FMT_OFFSET+48] and a[1][4:24] == b[1][4:24]


def stop_child(child):
    if child is not None and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=2.)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2.)


def write_packet(fd, packet, stop):
    view, deadline = memoryview(packet), time.monotonic() + 3.
    while view:
        if stop.is_set() or time.monotonic() > deadline:
            raise ValueError('transport_write_timeout')
        if select.select([], [fd], [], .1)[1]:
            try:
                count = os.write(fd, view)
                if not count:
                    raise ValueError('rgb_output_closed')
                view = view[count:]
            except BlockingIOError:
                continue


def capture(seconds, directory):
    import fcntl
    stop = threading.Event()
    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    # Nonblocking output means lost SSH consumers cannot hold the USB camera forever.
    fd = sys.stdout.fileno()
    fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_NONBLOCK)
    lock = (directory / 'capture.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise ValueError('rgb_camera_busy')
    child = worker = original = identity = None
    report = {'session_id': uuid4().hex, 'source_kind': KIND, 'restored': False,
              'received': 0, 'sent': 0, 'queue_dropped': 0, 'error': None}
    frames, failures = queue.Queue(2), []
    try:
        identity = discover()
        require_idle(identity[0])
        original = parameters(identity[0])
        command = ['/usr/bin/ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'error',
                   '-f', 'v4l2', '-input_format', 'mjpeg', '-video_size', '1280x720',
                   '-framerate', '15', '-i', identity[0], '-map', '0:v:0', '-an',
                   '-c:v', 'copy', '-f', 'mpjpeg', '-flush_packets', '1', 'pipe:1']
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, bufsize=0)
        reader = BoundedReader(child.stdout.fileno(), stop)
        def receive():
            try:
                while not stop.is_set():
                    reader.deadline = time.monotonic() + 5.
                    payload = read_mjpeg(reader)
                    report['received'] += 1
                    item = (report['received'], time.monotonic(), time.time(), payload)
                    try:
                        frames.put_nowait(item)
                    except queue.Full:
                        try:
                            frames.get_nowait()
                            report['queue_dropped'] += 1
                        except queue.Empty:
                            pass
                        frames.put_nowait(item)
            except (OSError, ValueError) as exc:
                if not stop.is_set():
                    failures.append(str(exc))
                stop.set()
        worker = threading.Thread(target=receive, daemon=True)
        worker.start()
        deadline = time.monotonic() + seconds
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                seq, stamp, wall, payload = frames.get(timeout=.1)
            except queue.Empty:
                continue
            header = make_header(report['session_id'], 6, seq, 1280, 720, stamp, wall,
                                 source_kind=KIND, pixel_format='MJPEG', stride=0,
                                 payload_length=len(payload), source_encoding='MJPEG',
                                 camera_serial=SERIAL, requested_rate_hz=15.,
                                 agent_queue_dropped=report['queue_dropped'],
                                 agent_seq_meaning='received_MJPEG_packet_not_sensor_exposure')
            write_packet(fd, encode_frame(header, payload), stop)
            report['sent'] += 1
        if failures:
            raise ValueError(failures[0])
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        report['error'] = str(exc)[:200]
    finally:
        stop.set()
        try:
            stop_child(child)
            if worker:
                worker.join(timeout=2.)
            if child and child.stdout:
                child.stdout.close()
            if original is not None:
                if discover() != identity:
                    raise ValueError('rgb_identity_changed_no_restore')
                require_idle(identity[0])
                report['restored'] = same_parameters(original, parameters(identity[0], original))
                if not report['restored']:
                    raise ValueError('rgb_restore_failed')
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            report['restore_error'] = 'rgb_restore_failed:' + str(exc)[:200]
        report['finished_wall_unverified'] = time.time()
        temporary = directory / 'last_capture.json.partial'
        try:
            temporary.write_text(json.dumps(report), encoding='utf-8')
            os.replace(str(temporary), str(directory / 'last_capture.json'))
        finally:
            lock.close()
        sys.stderr.write(json.dumps(report) + '\n')
    return 1 if report['error'] or report.get('restore_error') else 0


def main():
    directory = Path(__file__).resolve().parent
    if os.getuid() != 1000 or Path.home() != Path('/home/pi') or sys.argv[1:] != ['--gate']:
        raise ValueError('rgb_user_and_gate_required')
    expected = json.loads((directory / 'hashes.json').read_text())
    for name in ('capture_rgb_agent.py', 'capture_wire.py'):
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected[name]:
            raise ValueError('rgb_agent_hash_changed')
    command = os.environ.get('SSH_ORIGINAL_COMMAND', '')
    if command == 'go1-rgb-check':
        print(json.dumps({'device': discover(), 'uid': os.getuid(), 'hashes_verified': True}))
        return 0
    return capture(parse_command(command), directory)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        sys.stderr.write(str(error)[:200] + '\n')
        sys.exit(2)
