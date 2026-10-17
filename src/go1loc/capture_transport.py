"""One authenticated SSH binary stdout channel; no implicit connections."""
from __future__ import annotations

import json
import math
import re
import shlex
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .capture_session import SourceFrame, utc_now
from .capture_wire import read_frame


@dataclass(frozen=True)
class ConnectionConfig:
    target: str
    jump: str | None = None
    identity_file: str | None = None
    ssh_config: str | None = None
    remote_agent: str = '/home/unitree/go1_capture_20261015/capture_agent.py'
    remote_python: str = '/usr/bin/python3'
    rate: float = 10.
    max_seconds: float = 120.
    max_mbps: float = 60.
    queue_size: int = 2
    width: int = 464
    height: int = 400
    owner_uid: int | None = None
    projection: str = 'unknown'
    timeout_s: float = 5.
    source_kind: str = 'go1_shm_readonly'


def load_connection(path, camera):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    return ConnectionConfig(**{**data.get('defaults', {}), **data['cameras'][str(camera)]})


def ssh_command(c, camera, *, authorized=False):
    if not authorized:
        raise PermissionError('manual_readonly_connection_authorization_required')
    host = r'(?:[a-zA-Z0-9_][a-zA-Z0-9_.-]*@)?[a-zA-Z0-9_][a-zA-Z0-9_.-]*'
    if not re.fullmatch(host, c.target) or (c.jump and not re.fullmatch(host, c.jump)):
        raise ValueError('invalid_ssh_target_use_host_or_config_alias')
    external = c.source_kind == 'orbbec_rgb_mjpeg'
    if c.source_kind not in ('go1_shm_readonly', 'orbbec_rgb_mjpeg'):
        raise ValueError('unsupported_source_kind')
    if (external and camera != 6) or (not external and camera not in range(1, 6)):
        raise ValueError('invalid_camera')
    if not (math.isfinite(c.rate) and .2 <= c.rate <= 20 and 1 <= c.max_seconds <= 600
            and 1 <= c.width <= 2048 and 1 <= c.height <= 2048 and 1 <= c.queue_size <= 8
            and 1 <= c.max_mbps <= 200 and 1 <= c.timeout_s <= 30):
        raise ValueError('invalid_connection_budget')
    demand = (c.width * c.height * 3 + 8192) * c.rate * 8 / 1e6
    if not external and demand > c.max_mbps:
        raise ValueError('bandwidth_budget_exceeded_reduce_read_rate')
    if external and ((c.width, c.height, c.rate) != (1280, 720, 15) or c.jump
                     or c.max_seconds != int(c.max_seconds)):
        raise ValueError('rgb_requires_direct_fixed_720p15_profile')
    if c.projection not in ('unknown', 'raw_fisheye', 'rectified_pinhole'):
        raise ValueError('invalid_projection')
    for value in (c.remote_agent, c.remote_python):
        if not value.startswith('/') or any(ch in value for ch in '\r\n\x00'):
            raise ValueError('remote_paths_must_be_absolute')
    command = ['ssh', '-T', '-o', 'StrictHostKeyChecking=yes', '-o', 'BatchMode=yes',
               '-o', 'Compression=no', '-o', 'LogLevel=ERROR',
               '-o', 'RequestTTY=no', '-o', 'ClearAllForwardings=yes',
               '-o', 'ServerAliveInterval=2', '-o', 'ServerAliveCountMax=2',
               '-o', 'ConnectTimeout=5']
    if c.ssh_config:
        command += ['-F', str(Path(c.ssh_config).expanduser())]
    if c.identity_file:
        command += ['-i', str(Path(c.identity_file).expanduser())]
    if c.jump:
        command += ['-J', c.jump]
    if external:
        return command + [c.target, 'go1-rgb-720p15 ' + str(int(c.max_seconds))]
    remote = [c.remote_python, '-u', c.remote_agent, '--authorize-readonly', '--camera', str(camera),
              '--rate', str(c.rate), '--max-seconds', str(c.max_seconds), '--max-mbps', str(c.max_mbps),
              '--queue-size', str(c.queue_size), '--width', str(c.width), '--height', str(c.height),
              '--projection', c.projection]
    if c.owner_uid is not None:
        if type(c.owner_uid) is not int or c.owner_uid < 0:
            raise ValueError('invalid_owner_uid')
        remote += ['--owner-uid', str(c.owner_uid)]
    return command + [c.target, 'exec ' + ' '.join(shlex.quote(x) for x in remote)]


class PipeSource:
    """Read/watch/drain threads; only this owned Popen is terminated on cleanup."""
    def __init__(self, command, camera, *, source_kind, timeout=5.):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('invalid_frame_timeout')
        self.command, self.camera = command, camera
        self.source_kind, self.timeout = source_kind, timeout
        self.process = None
        self.stop = threading.Event()
        self.threads = []
        self.stderr_tail = deque(maxlen=8)  # bounded, not printed or persisted
        self.stderr_bytes = 0
        self.last_frame = time.monotonic()
        self.failure = None
        self.shutdown_lock = threading.Lock()
        self.end_lock = threading.Lock()
        self.ended = False

    def start(self, on_frame, on_end):
        if self.process:
            raise RuntimeError('source_already_started')
        self.process = subprocess.Popen(self.command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, bufsize=0,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.last_frame = time.monotonic()

        def finish(reason):
            # Also called by watchdog: completion cannot depend on a blocked read.
            with self.end_lock:
                if self.ended:
                    return
                self.ended = True
            self.stop.set()
            self._shutdown()
            self.threads[0].join(timeout=.3)
            on_end(self.failure or self._safe_remote_error() or reason)

        def drain():
            while True:
                part = self.process.stderr.read(1024)
                if not part:
                    break
                self.stderr_bytes += len(part)
                self.stderr_tail.append(part)

        def receive():
            identity = None
            seq, stamp = -1, -1.
            reason = 'stream_eof'
            try:
                while not self.stop.is_set():
                    packet = read_frame(self.process.stdout)
                    if packet is None:
                        break
                    header, payload = packet
                    if header['camera_id'] != self.camera or header['source_kind'] != self.source_kind:
                        raise ValueError('unexpected_camera_or_source')
                    if self.source_kind == 'orbbec_rgb_mjpeg':
                        from .capture_rgb import decode_rgb_frame
                        frame = decode_rgb_frame(header, payload)
                    else:
                        frame = SourceFrame(header, payload, utc_now(), time.monotonic())
                    if identity is not None and frame.identity() != identity:
                        raise ValueError('stream_or_format_changed')
                    if header['agent_seq'] <= seq or header['agent_read_monotonic'] <= stamp:
                        raise ValueError('nonmonotonic_stream')
                    identity, seq, stamp = frame.identity(), header['agent_seq'], header['agent_read_monotonic']
                    self.last_frame = frame.pc_receive_monotonic
                    if self.stop.is_set():
                        break
                    try:
                        on_frame(frame)
                    except Exception:  # noqa: BLE001 - callback boundary must fail closed and reap child
                        reason = 'frame_callback_failed'
                        break
                if reason == 'stream_eof' and not self.stop.is_set():
                    try:
                        code = self.process.wait(timeout=.2)
                        if code:
                            reason = 'source_exit_code_' + str(code)
                    except subprocess.TimeoutExpired:
                        reason = 'source_stdout_closed_while_running'
            except (OSError, ValueError) as exc:
                reason = type(exc).__name__ + ':' + str(exc)
            finally:
                finish(reason)

        def watchdog():
            while not self.stop.wait(.1):
                if time.monotonic() - self.last_frame > self.timeout:
                    self.failure = 'frame_timeout_no_live_preview'
                    finish(self.failure)
                    break

        self.threads = [threading.Thread(target=f, daemon=True, name='capture-' + name)
                        for f, name in ((drain, 'stderr'), (receive, 'receive'), (watchdog, 'watchdog'))]
        for thread in self.threads:
            thread.start()

    def _shutdown(self):
        """Bounded terminate/kill/reap; never signal by name or touch other PIDs."""
        with self.shutdown_lock:
            if self.process and self.process.poll() is None:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=.3)
                except subprocess.TimeoutExpired:
                    try:
                        self.process.kill()
                        self.process.wait(timeout=1.)
                    except (OSError, subprocess.TimeoutExpired):
                        self.failure = 'owned_process_cleanup_failed'
                except OSError:
                    if self.process.poll() is None:
                        self.failure = 'owned_process_cleanup_failed'

    def _safe_remote_error(self):
        """Only emit fixed diagnostic codes, never arbitrary remote stderr text."""
        tail = b''.join(self.stderr_tail)
        known = {
            b'shm_missing_or_ambiguous': 'shm_missing_or_ambiguous',
            b'shm_owner_or_size_mismatch': 'shm_owner_or_size_mismatch',
            b'shm_replaced_during_': 'shm_replaced_reconnect_requires_layout_check',
            b'readonly_shmat_failed': 'readonly_shmat_failed_check_permissions',
            b'sampler_read_timeout': 'sampler_read_timeout',
            b'sampler_shutdown_timeout': 'sampler_shutdown_timeout',
            b'transport_write_timeout': 'transport_write_timeout',
            b'Permission denied': 'ssh_authentication_or_remote_permission_failed',
            b'Host key verification failed': 'ssh_host_key_verification_failed',
            b'No such file or directory': 'remote_agent_or_interpreter_missing',
            b'rgb_camera_missing_or_ambiguous': 'rgb_camera_missing_or_ambiguous',
            b'rgb_camera_busy': 'rgb_camera_busy',
            b'rgb_capture_timeout': 'rgb_capture_timeout',
            b'rgb_restore_failed': 'rgb_restore_failed',
        }
        return next((code for pattern, code in known.items() if pattern in tail), None)

    def close(self):
        self.stop.set()
        self._shutdown()
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2.)
        if self.process and not any(t.is_alive() for t in self.threads):
            for pipe in (self.process.stdout, self.process.stderr):
                pipe.close()


class SSHSource(PipeSource):
    def __init__(self, config, camera, *, authorized=False):
        super().__init__(ssh_command(config, camera, authorized=authorized), camera,
                         source_kind=config.source_kind, timeout=config.timeout_s)
