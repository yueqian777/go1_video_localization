"""Publish real COLMAP build snapshots at a native writer completion barrier.

Ready directories are immutable full states, never point/camera deltas. A viewer
must replace the selected component's state. Build-attempt component IDs are
not claimed to be aligned with each other or with the final normalized map.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import time
from uuid import uuid4

from .events import BoundedEventWriter, utc_now

MODEL_FILES = ('cameras.bin', 'images.bin', 'points3D.bin', 'rigs.bin', 'frames.bin')


class SnapshotError(RuntimeError):
    pass


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file_hashes(directory: Path) -> dict[str, str]:
    for name in MODEL_FILES:
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size < 8:
            raise SnapshotError('incomplete_snapshot:' + name)
    return {name: _hash(directory / name) for name in MODEL_FILES}


def _load_model(path: Path):
    import pycolmap
    return pycolmap.Reconstruction(path)


def _remove_owned_directory(path: Path, parent: Path) -> None:
    """Only remove a direct, nonsymlink child of a directory owned by this run."""
    if path.is_symlink() or path.resolve().parent != parent.resolve():
        raise SnapshotError('unsafe_snapshot_cleanup_target')
    for file in path.iterdir():
        if file.is_file() and not file.is_symlink():
            file.chmod(stat.S_IWRITE | stat.S_IREAD)
    shutil.rmtree(path)


class SnapshotPublisher:
    def __init__(self, root: Path, *, events: BoundedEventWriter | None = None,
                 max_count: int = 20, max_bytes: int = 512 * 1024 * 1024,
                 model_loader=None):
        if not 1 <= max_count <= 1000 or max_bytes < 1:
            raise ValueError('invalid_snapshot_budget')
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=False)
        self.events = events
        self.max_count, self.max_bytes = max_count, max_bytes
        self.model_loader = model_loader or _load_model
        self.count = 0
        self.bytes_written = 0
        self.overhead_s = 0.0
        self.exhausted = False

    def publish(self, source: Path, component_id: str, *, frame_times: dict | None = None) -> dict | None:
        """Call only when the native writer is stopped at its registration callback.

        All five 4.2 model files must parse and be internally consistent, and
        their hashes must stay unchanged across copying/validation. Budget
        exhaustion stops publication; it never substitutes the final model.
        """
        started = time.perf_counter()
        pending = None
        try:
            if self.exhausted or self.count >= self.max_count:
                self.exhausted = True
                return None
            source = Path(source).resolve()
            before = _file_hashes(source)
            source_bytes = sum((source / name).stat().st_size for name in MODEL_FILES)
            if self.bytes_written + source_bytes > self.max_bytes:
                self.exhausted = True
                return None
            pending = self.root / ('.pending-' + uuid4().hex)
            pending.mkdir()
            for name in MODEL_FILES:
                shutil.copyfile(source / name, pending / name)
            try:
                model = self.model_loader(pending)
                if not model.is_valid() or model.num_reg_images() < 2 or model.num_points3D() < 1:
                    raise SnapshotError('invalid_or_empty_snapshot_model')
            except SnapshotError:
                raise
            except Exception as exc:
                raise SnapshotError('invalid_snapshot_model:' + str(exc)[:500]) from exc
            if before != _file_hashes(pending) or before != _file_hashes(source):
                raise SnapshotError('snapshot_changed_during_publication')
            number = self.count + 1
            # Component labels are metadata, never filesystem path components.
            destination = self.root / f'snapshot-{number:06d}'
            times = frame_times or {}
            frames = [{'image_id': int(image_id), 'image_name': image.name,
                       'pts_time_s': times.get(image.name, {}).get('pts_time_s'),
                       'pts': times.get(image.name, {}).get('pts'),
                       'relative_time_s': times.get(image.name, {}).get('relative_time_s')}
                      for image_id, image in model.images.items()]
            row = {'schema_version': 'go1loc.snapshot.v1', 'mode': 'build_snapshot',
                   'source': 'pycolmap_native_snapshot', 'snapshot_index': number,
                   'run_id': self.events.run_id if self.events else None,
                   'map_id': self.events.map_id if self.events else None,
                   'component_id': component_id, 'state_semantics': 'replace_component',
                   'alignment_to_final_map': 'not_established',
                   'processing_time': utc_now(), 'snapshot_path': str(destination),
                   'registered_images': int(model.num_reg_images()),
                   'points3D': int(model.num_points3D()), 'model_sha256': before,
                   'frames': frames, 'frame_order': 'unordered_use_pts_for_video_time',
                   'exposure_time': None, 'timing_source': 'video_pts_not_exposure_time'}
            encoded = json.dumps(row, ensure_ascii=False, allow_nan=False, indent=2).encode('utf-8')
            if self.bytes_written + source_bytes + len(encoded) > self.max_bytes:
                self.exhausted = True
                return None
            with (pending / 'READY.json').open('xb') as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            for file in pending.iterdir():
                file.chmod(stat.S_IREAD)
            # The marker and model become visible together; consumers ignore .pending-*.
            os.replace(pending, destination)
            pending = None
            self.count = number
            self.bytes_written += source_bytes + len(encoded)
            self.exhausted = self.count >= self.max_count
            if self.events:
                self.events.emit('snapshot_ready', stage='reconstruction', component_id=component_id,
                                 registered_images=row['registered_images'], points3D=row['points3D'],
                                 snapshot_path=str(destination), data={'mode': 'build_snapshot',
                                 'state_semantics': 'replace_component', 'snapshot_index': number,
                                 'alignment_to_final_map': 'not_established'})
            return row
        finally:
            if pending is not None and pending.exists():
                _remove_owned_directory(pending, self.root)
            self.overhead_s += time.perf_counter() - started


def list_ready_snapshots(root: Path) -> list[dict]:
    """No snapshots means no build view; never synthesize one from a final map."""
    rows = []
    for marker in sorted(Path(root).glob('snapshot-*/READY.json')):
        row = json.loads(marker.read_text(encoding='utf-8'))
        if (row.get('mode') != 'build_snapshot' or marker.is_symlink()
                or marker.parent.is_symlink()
                or Path(row['snapshot_path']).resolve() != marker.parent.resolve()
                or row.get('model_sha256') != _file_hashes(marker.parent)):
            raise SnapshotError('invalid_ready_snapshot')
        rows.append(row)
    return rows


def run_incremental_mapping(database_path: Path, image_path: Path, output_path: Path,
                            mapper_options: dict, publisher: SnapshotPublisher,
                            *, frames_freq: int, frame_times: dict | None = None) -> dict:
    """PyCOLMAP 4.2 worker adapter; callbacks read disk, never expose live models.

    Unlike incremental_mapping() (which copies its options), IncrementalPipeline
    retains shared options. This lets the completion callback disable native
    snapshot writing as soon as the retained count/byte budget is reached.
    """
    import pycolmap
    if pycolmap.__version__ != '4.2.1':
        raise SnapshotError('snapshot_backend_requires_verified_pycolmap_4.2.1')
    if frames_freq < 1:
        raise ValueError('invalid_snapshot_frequency')
    native = publisher.root.parent / ('.native-' + uuid4().hex)
    native.mkdir()
    options = pycolmap.IncrementalPipelineOptions(mapper_options)
    options.image_path = image_path
    options.snapshot_path = native
    options.snapshot_frames_freq = frames_freq
    manager = pycolmap.ReconstructionManager()
    events = publisher.events
    attempt = 0
    component_id = None

    def consume():
        # Native 4.2.1 writes the complete snapshot before NEXT_IMAGE_REG_CALLBACK.
        for source in sorted(native.iterdir()):
            if not source.is_dir():
                continue
            try:
                publisher.publish(source, component_id, frame_times=frame_times)
            except SnapshotError as exc:
                if events:
                    events.emit('snapshot_rejected', stage='reconstruction', component_id=component_id,
                                data={'error': str(exc)[:1000]})
            finally:
                _remove_owned_directory(source, native)
        if publisher.exhausted:
            pipeline.options.snapshot_frames_freq = 0

    def initial():
        nonlocal attempt, component_id
        attempt += 1
        component_id = f'{events.map_id if events else "map"}:build-attempt:{attempt}'
        if events:
            events.emit('started', stage='reconstruction_component', component_id=component_id,
                        data={'alignment_to_other_components': 'not_established'})

    def registered():
        consume()
        if events and manager.size():
            model = manager.get(manager.size() - 1)
            events.emit('registration_progress', stage='reconstruction', component_id=component_id,
                        registered_images=int(model.num_reg_images()), points3D=int(model.num_points3D()))

    def finished_component():
        consume()
        if events:
            events.emit('completed', stage='reconstruction_component', component_id=component_id,
                        data={'retention_in_final_map': 'not_assumed'})

    try:
        with pycolmap.Database.open(database_path) as database:
            pipeline = pycolmap.IncrementalPipeline(options, database, manager)
            callbacks = pycolmap.IncrementalPipelineCallback
            pipeline.add_callback(callbacks.INITIAL_IMAGE_PAIR_REG_CALLBACK, initial)
            pipeline.add_callback(callbacks.NEXT_IMAGE_REG_CALLBACK, registered)
            pipeline.add_callback(callbacks.LAST_IMAGE_REG_CALLBACK, finished_component)
            pipeline.run()
            consume()
            models = {index: manager.get(index) for index in range(manager.size())}
            output_path.mkdir(parents=True, exist_ok=True)
            for index, model in models.items():
                path = output_path / str(index)
                path.mkdir()
                model.write(path)
            return models
    finally:
        # Never remove user paths; this directory was exclusively created above.
        for source in list(native.iterdir()):
            if source.is_dir():
                _remove_owned_directory(source, native)
        native.rmdir()
