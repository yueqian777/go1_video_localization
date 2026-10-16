"""Authoritative source PNG writer. Display copies never enter this module."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import numpy as np
from PIL import Image


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, data):
    temporary = path.with_suffix(path.suffix + '.partial')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(temporary, path)


def safe_new_directory(path):
    path = Path(path).resolve()
    for parent in (path, *path.parents):
        if any((parent / name).exists() for name in ('map_manifest.json', 'frames_manifest.json', 'session.json')):
            raise ValueError('destination_overlaps_existing_artifact')
    path.mkdir(parents=True, exist_ok=False)
    return path


def session_path(root, name):
    if not re.fullmatch(r'[\w-]{1,64}', name, flags=re.UNICODE):
        raise ValueError('session_name_use_letters_numbers_hyphen_underscore')
    return Path(root) / (name + '_' + datetime.now().strftime('%Y%m%dT%H%M%S') + '_' + uuid4().hex[:8])


@dataclass(frozen=True)
class SourceFrame:
    header: dict
    payload: bytes
    pc_receive_time: str
    pc_receive_monotonic: float
    preview_jpeg: bytes | None = None

    def rgb(self):
        h = self.header
        return np.frombuffer(self.payload, np.uint8).reshape(h['height'], h['width'], 3)[:, :, ::-1].copy()

    def identity(self):
        return tuple(self.header.get(k) for k in ('session_id', 'camera_id', 'width', 'height',
                                                 'channels', 'pixel_format', 'stride', 'projection',
                                                 'orientation', 'source_kind'))


def save_png(path, frame):
    temporary = path.with_suffix('.png.partial')
    Image.fromarray(frame.rgb()).save(temporary, format='PNG', compress_level=1)
    os.replace(temporary, path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Recorder:
    """One stream/format per clip. Only frames after start's preview seq are saved."""
    def __init__(self, path, initial_frame, *, queue_size=8, start_worker=True):
        if not 1 <= queue_size <= 64:
            raise ValueError('invalid_record_queue_size')
        self.path = safe_new_directory(path)
        (self.path / 'frames').mkdir()
        (self.path / 'INCOMPLETE').write_text('Read session status; a crash or disk failure may leave partial data.\n')
        self.identity = initial_frame.identity()
        self.last_seq = initial_frame.header['agent_seq']
        self.last_time = initial_frame.header['agent_read_monotonic']
        self.queue = queue.Queue(queue_size)
        self.lock = threading.Lock()
        self.closing = threading.Event()
        self.accepting = True
        self.written = self.dropped = self.failed = 0
        self.error = None
        self.status = 'complete'
        self.reason = 'manual_stop'
        self.thread = None
        self.meta = dict(schema_version='go1loc.capture.v1', recording_id=uuid4().hex,
                         stream_session_id=initial_frame.header['session_id'],
                         status='recording', started_at=utc_now(),
                         start_after_agent_seq=self.last_seq, source=initial_frame.header.copy(),
                         source_kind=initial_frame.header['source_kind'],
                         sensor_capture_time=None, timing_source='agent_read_monotonic_not_exposure',
                         hardware_synchronized=False, execution_permitted=False,
                         authoritative_format='PNG_RGB8_reversible_from_BGR8',
                         recording_queue_capacity=queue_size,
                         limitations=['double_copy_is_not_atomic', 'identical_pixels_not_duplicate_exposure',
                                      'wall_clocks_unverified_no_one_way_latency', 'projection_requires_field_check'])
        if initial_frame.header['source_kind'] == 'orbbec_rgb_mjpeg':
            self.meta['limitations'] = ['camera_MJPEG_is_lossy', 'PNG_and_FFV1_preserve_decoded_pixels_only',
                                       'agent_receive_time_not_exposure_or_USB_timestamp',
                                       'wall_clocks_unverified_no_one_way_latency', 'projection_requires_field_check']
        write_json(self.path / 'session.json', self.meta)
        write_json(self.path / 'calibration_reference.json',
                   dict(status='not_provided', camera_id=initial_frame.header['camera_id'],
                        projection=initial_frame.header['projection'], image_size=[initial_frame.header['width'], initial_frame.header['height']]))
        if start_worker:
            self.start_worker()

    def start_worker(self):
        if self.thread is not None:
            raise RuntimeError('writer_already_started')
        self.thread = threading.Thread(target=self._run, name='png-recorder', daemon=True)
        self.thread.start()

    def submit(self, frame):
        with self.lock:
            if not self.accepting or self.error:
                return False
            if frame.identity() != self.identity:
                raise ValueError('stream_or_format_changed_new_clip_required')
            seq, stamp = frame.header['agent_seq'], frame.header['agent_read_monotonic']
            if seq <= self.last_seq:
                return False
            if stamp <= self.last_time:
                raise ValueError('nonmonotonic_agent_time')
            self.last_seq, self.last_time = seq, stamp
            try:
                self.queue.put_nowait(frame)
                return True
            except queue.Full:
                self.dropped += 1
                return False

    def finish(self, status='complete', reason='manual_stop', timeout=5.):
        with self.lock:
            self.accepting = False
            self.status, self.reason = status, reason
            self.closing.set()
        if self.thread:
            self.thread.join(timeout)
            if self.thread.is_alive():
                self.error = 'writer_finalize_timeout_INCOMPLETE_preserved'
                raise TimeoutError(self.error)
        return self.path

    def _run(self):
        previous_seq = self.meta['start_after_agent_seq']
        first_read = last_read = None
        equal_pixels = 0
        previous_digest = None
        events = manifest = None
        try:
            events = (self.path / 'events.jsonl').open('x', encoding='utf-8', buffering=1)
            manifest = (self.path / 'frames.jsonl').open('x', encoding='utf-8', buffering=1)
            events.write(json.dumps(dict(event='recording_started', time=self.meta['started_at'],
                                         after_agent_seq=previous_seq)) + '\n')
            while not self.closing.is_set() or not self.queue.empty():
                try:
                    frame = self.queue.get(timeout=.1)
                except queue.Empty:
                    continue
                try:
                    if self.error:
                        self.failed += 1
                        continue
                    dest = self.path / 'frames' / ('%08d.png' % self.written)
                    digest = save_png(dest, frame)
                    row = dict(frame.header, saved_index=self.written,
                               recording_id=self.meta['recording_id'], pc_receive_time=frame.pc_receive_time,
                               pc_receive_monotonic=frame.pc_receive_monotonic,
                               image_path=dest.relative_to(self.path).as_posix(), image_sha256=digest,
                               raw_payload_sha256=hashlib.sha256(frame.payload).hexdigest(),
                               saved_at=utc_now(), sensor_capture_time=None)
                    # A gap counts read attempts, NOT camera exposure drops.
                    gap = row['agent_seq'] - previous_seq - 1
                    if gap:
                        events.write(json.dumps(dict(event='agent_read_sequence_gap', missing_read_attempts=gap,
                                                     before_agent_seq=row['agent_seq'])) + '\n')
                    manifest.write(json.dumps(row, allow_nan=False) + '\n')
                    manifest.flush()
                    self.written += 1
                    stamp = row['agent_read_monotonic']
                    if first_read is None:
                        first_read = stamp
                    last_read = stamp
                    equal_pixels += row['raw_payload_sha256'] == previous_digest
                    previous_digest = row['raw_payload_sha256']
                    previous_seq = row['agent_seq']
                except (OSError, ValueError) as exc:
                    self.error = type(exc).__name__ + ': recording_write_failed'
                    self.failed += 1
                    self.closing.set()
                finally:
                    self.queue.task_done()
            events.write(json.dumps(dict(event='recording_finished', time=utc_now(),
                                         reason=self.reason, pc_queue_dropped=self.dropped,
                                         last_received_agent_seq=self.last_seq,
                                         last_saved_agent_seq=previous_seq,
                                         write_failed_or_discarded=self.failed)) + '\n')
        except (OSError, ValueError):
            self.error = 'recording_metadata_write_failed'
            # Release waiting tests/workers without accepting more input.
            while True:
                try:
                    self.queue.get_nowait()
                    self.queue.task_done()
                    self.failed += 1
                except queue.Empty:
                    break
        finally:
            for handle in (manifest, events):
                if handle:
                    try:
                        handle.close()
                    except OSError:
                        self.error = 'recording_close_failed'
            self.accepting = False
            duration = last_read - first_read if first_read is not None else 0.
            self.meta.update(status='interrupted' if self.error else self.status,
                             reason=self.error or self.reason, finished_at=utc_now(),
                             written_frames=self.written, pc_queue_dropped=self.dropped,
                             last_received_agent_seq=self.last_seq, last_saved_agent_seq=previous_seq,
                             write_failed_or_discarded=self.failed)
            try:
                write_json(self.path / 'quality_summary.json', dict(
                    saved_frames=self.written, pc_queue_dropped=self.dropped, write_failed_or_discarded=self.failed,
                    read_timeline_duration_s=duration,
                    saved_read_rate_hz=(self.written - 1) / duration if duration > 0 else None,
                    equal_adjacent_pixel_payloads=equal_pixels, exposure_fps=None,
                    identical_pixels_interpretation='not_proof_of_freeze_or_duplicate_exposure',
                    image_quality_acceptance='not_performed', execution_permitted=False))
                write_json(self.path / 'session.json', self.meta)
                (self.path / 'INCOMPLETE').unlink()
            except OSError:
                self.error = 'final_metadata_write_failed_INCOMPLETE_preserved'


def save_snapshot(root, name, frame):
    path = safe_new_directory(session_path(root, name + '_photo'))
    digest = save_png(path / 'source.png', frame)
    write_json(path / 'source.json', dict(frame.header, image_sha256=digest,
               pc_receive_time=frame.pc_receive_time, pc_receive_monotonic=frame.pc_receive_monotonic,
               source_kind=frame.header['source_kind'], sensor_capture_time=None,
               capture_time=None, execution_permitted=False))
    return path
