"""Thread-safe manual capture lifecycle, independent of maps and vision models."""
from __future__ import annotations

import threading
import time
from dataclasses import replace

from .capture_health import FrameHealth
from .capture_session import Recorder, save_snapshot, session_path


class CaptureController:
    def __init__(self, *, fresh_timeout=2., freeze_after=3.):
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.operation = threading.Lock()
        self.state = 'disconnected'
        self.source = self.latest = self.recorder = None
        self.generation = 0
        self.error = None
        self.received = 0
        self.started = time.monotonic()
        self.ended = None
        self.fresh_timeout = fresh_timeout
        self.last_output = None
        self.last_written = self.last_dropped = 0
        self.pending_finalizer = None
        self.health = FrameHealth(freeze_after)

    def connect(self, source):
        with self.operation:
            with self.lock:
                if self.source is not None or self.state not in ('disconnected', 'error'):
                    raise RuntimeError('disconnect_before_connecting_again')
                self.generation += 1
                token = self.generation
                self.source, self.latest = source, None
                self.state, self.error = 'connecting', None
                self.received, self.started = 0, time.monotonic()
                self.ended = None
                self.health.reset()
            try:
                source.start(lambda f: self._received(token, f), lambda reason: self._ended(token, reason))
            except Exception:
                source.close()
                with self.lock:
                    self.state, self.source, self.error = 'error', None, 'connection_start_failed_check_ssh_setup'
                raise

    def _received(self, token, frame):
        with self.changed:
            if token != self.generation or self.state not in ('connecting', 'previewing', 'recording', 'finalizing'):
                return
            frame = replace(frame, header={**frame.header, 'pc_health': self.health.observe(frame)})
            self.latest = frame
            self.received += 1
            if self.state == 'connecting':
                self.state = 'previewing'
            if self.recorder:
                if self.recorder.error or not self.recorder.accepting:
                    raise ValueError(self.recorder.error or 'writer_not_accepting')
                self.recorder.submit(frame)
            self.changed.notify_all()

    def _ended(self, token, reason):
        with self.changed:
            if token != self.generation or self.state in ('error', 'disconnected'):
                return
            self.latest, self.error, self.state = None, reason, 'error'
            self.ended = time.monotonic()
            recorder, self.recorder = self.recorder, None
            self.changed.notify_all()
        if recorder:
            self._finish(recorder, 'interrupted', reason)

    def _finish(self, recorder, status='complete', reason='manual_stop'):
        try:
            recorder.finish(status, reason)
        except (OSError, TimeoutError) as exc:
            self.error = str(exc)
            self.pending_finalizer = recorder
        finally:
            with self.lock:
                self.last_output = str(recorder.path)
                self.last_written, self.last_dropped = recorder.written, recorder.dropped
                if recorder.error:
                    self.error, self.state = recorder.error, 'error'

    def start_recording(self, root, name):
        with self.operation:
            with self.lock:
                if self.state != 'previewing' or self.recorder is not None or not self._fresh():
                    raise RuntimeError('fresh_preview_required_to_start')
                if self.pending_finalizer and self.pending_finalizer.thread.is_alive():
                    raise RuntimeError('previous_writer_still_finalizing')
                initial, token = self.latest, self.generation
            recorder = Recorder(session_path(root, name), initial)
            with self.lock:
                if token != self.generation or self.state != 'previewing':
                    recorder.finish('interrupted', 'connection_changed_during_start')
                    raise RuntimeError('connection_changed_during_start')
                self.recorder, self.state = recorder, 'recording'
                self.last_output = str(recorder.path)
            return recorder.path

    def stop_recording(self):
        with self.operation:
            with self.lock:
                if self.state != 'recording' or self.recorder is None:
                    raise RuntimeError('not_recording')
                recorder, self.recorder = self.recorder, None
                self.state = 'finalizing'
            self._finish(recorder)
            with self.lock:
                if self.state == 'finalizing':
                    self.state = 'previewing' if self._fresh() else 'error'
            return recorder.path

    def disconnect(self):
        with self.operation:
            with self.changed:
                self.generation += 1
                source, self.source = self.source, None
                recorder, self.recorder = self.recorder, None
                self.latest = None
                self.health.reset()
                if self.ended is None:
                    self.ended = time.monotonic()
                self.state = 'finalizing'
                self.changed.notify_all()
            if source:
                source.close()
            if recorder:
                self._finish(recorder, 'interrupted', 'manual_disconnect')
            with self.lock:
                self.state = 'disconnected'

    def snapshot(self, root, name, *, timeout=3.):
        with self.operation:
            with self.changed:
                if self.state not in ('previewing', 'recording') or not self._fresh():
                    raise RuntimeError('fresh_preview_required_for_snapshot')
                initial, token = self.latest, self.generation
                deadline = time.monotonic() + timeout
                while True:
                    if self.generation != token or self.state not in ('previewing', 'recording'):
                        raise RuntimeError('snapshot_interrupted')
                    if self.latest is not initial and self._fresh():
                        frame = self.latest
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('snapshot_timeout_no_new_source_frame')
                    self.changed.wait(remaining)
            return save_snapshot(root, name, frame)

    def _fresh(self):
        return self.latest is not None and time.monotonic() - self.latest.pc_receive_monotonic <= self.fresh_timeout

    def view(self):
        with self.lock:
            r = self.recorder
            fresh = self._fresh() and self.state not in ('error', 'disconnected')
            health = self.health.view()
            return dict(state=self.state, error=self.error,
                        live=fresh and health['content_status'] != 'suspected_frozen',
                        transport_live=fresh, **health,
                        received=self.received, receive_rate_hz=self.received / max(.001, (self.ended or time.monotonic())-self.started),
                        written=r.written if r else self.last_written, dropped=r.dropped if r else self.last_dropped,
                        queue=r.queue.qsize() if r else 0, output=self.last_output,
                        source_metadata=self.latest.header.copy() if fresh else None), self.latest if fresh else None
