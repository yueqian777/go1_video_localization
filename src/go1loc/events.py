"""Bounded, single-writer JSONL event protocol for offline worker processes.

Readers may reconnect with ``after_sequence``. Rotation retains the newest
complete records, so a sequence gap means history was evicted, not progress.
There is deliberately no socket, UI callback, or robot connection here.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_replace(source: Path, destination: Path) -> None:
    """Briefly retry Windows sharing locks without ever truncating live data."""
    for attempt in range(6):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.01 * (attempt + 1))


def outside_artifacts(path: Path, *artifacts: Path) -> Path:
    """Reject both directions of overlap, including resolved symlinks."""
    path = Path(path).resolve()
    for artifact in artifacts:
        artifact = Path(artifact).resolve()
        if path == artifact or path in artifact.parents or artifact in path.parents:
            raise ValueError('sidecar_overlaps_artifact_directory')
    return path


class BoundedEventWriter:
    """One worker owns a fresh file; threads in that worker are serialized.

    Limits cover both disk bytes and resident serialized history. Existing
    files are never overwritten/reopened as a new run. No partial line is
    published during rotation (atomic replacement on the same filesystem).
    """
    def __init__(self, path: Path, *, run_id: str | None = None, map_id: str | None = None,
                 max_events: int = 10000, max_bytes: int = 8 * 1024 * 1024,
                 max_event_bytes: int = 64 * 1024):
        if max_events < 1 or max_bytes < 1024 or max_event_bytes < 512:
            raise ValueError('invalid_event_budget')
        self.path = Path(path).resolve()
        self.run_id = run_id or uuid4().hex
        self.map_id = map_id
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.max_event_bytes = min(max_event_bytes, max_bytes)
        self.sequence = 0
        self.overhead_s = 0.0
        self._started = time.perf_counter()
        self._lines: deque[bytes] = deque()
        self._bytes = 0
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('xb'):
            pass

    def emit(self, event: str, *, stage: str | None = None, component_id: str | None = None,
             video_pts_s: float | None = None, registered_images: int | None = None,
             points3D: int | None = None, snapshot_path: str | None = None,
             data: dict | None = None) -> dict:
        started = time.perf_counter()
        with self._lock:
            row = {'schema_version': 'go1loc.event.v1', 'run_id': self.run_id,
                   'map_id': self.map_id, 'component_id': component_id,
                   'sequence': self.sequence + 1, 'event': event, 'stage': stage,
                   'processing_time': utc_now(), 'elapsed_s': started - self._started,
                   'video_pts_s': video_pts_s, 'registered_images': registered_images,
                   'points3D': points3D, 'snapshot_path': snapshot_path, 'data': data or {}}
            line = (json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n').encode('utf-8')
            if len(line) > self.max_event_bytes:
                raise ValueError('event_too_large')
            retained = deque(self._lines)
            byte_count = self._bytes + len(line)
            retained.append(line)
            while len(retained) > self.max_events or byte_count > self.max_bytes:
                byte_count -= len(retained.popleft())
            if len(retained) < len(self._lines) + 1:
                temporary = self.path.with_name(self.path.name + '.pending-' + uuid4().hex)
                try:
                    with temporary.open('xb') as stream:
                        stream.writelines(retained)
                        stream.flush()
                        os.fsync(stream.fileno())
                    atomic_replace(temporary, self.path)
                finally:
                    temporary.unlink(missing_ok=True)
            else:
                with self.path.open('ab') as stream:
                    stream.write(line)
                    stream.flush()
            self._lines, self._bytes = retained, byte_count
            self.sequence += 1
            self.overhead_s += time.perf_counter() - started
            return row

    @contextmanager
    def stage(self, name: str, **fields):
        started = time.perf_counter()
        self.emit('started', stage=name, **fields)
        try:
            yield
        except BaseException as exc:
            try:
                self.emit('cancelled' if isinstance(exc, KeyboardInterrupt) else 'failed', stage=name,
                          data={'error_type': type(exc).__name__, 'error': str(exc)[:128],
                                'duration_s': time.perf_counter() - started})
            except Exception as diagnostic_error:
                exc.add_note('Failed to persist diagnostic event: ' + str(diagnostic_error)[:256])
            raise
        else:
            self.emit('completed', stage=name, data={'duration_s': time.perf_counter() - started})


def read_events(path: Path, *, after_sequence: int = 0) -> list[dict]:
    """Read complete JSONL records; an interrupted append's last line is ignored."""
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    with path.open('rb') as stream:
        for line in stream:
            if not line.endswith(b'\n'):
                break
            row = json.loads(line)
            if row['sequence'] > after_sequence:
                rows.append(row)
    return rows
