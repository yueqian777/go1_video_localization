"""Local finalized-recording selection and bounded calibration uploads.

No discovery over the network, no camera start/stop, no caller-selected root.
The catalog names immediate children of the existing local capture directory.
"""
import hashlib
import json
from pathlib import Path
import re
import shutil
from uuid import uuid4

from .jobs import atomic_json, read_json, utc_now
from .video import sha256_file

CAPTURE_ROOT = Path(__file__).resolve().parents[2]/'data/captures'
MAX_CALIBRATION_BYTES = 64 * 1024


def capture_path(capture_id):
    if not isinstance(capture_id, str) or not re.fullmatch(r'[\w-]{1,160}', capture_id):
        raise ValueError('invalid_capture_id')
    root = CAPTURE_ROOT.resolve()
    path = (root/capture_id).resolve()
    if path.parent != root or (root/capture_id).is_symlink() or not path.is_dir():
        raise ValueError('capture_path_escape_or_missing')
    for name in ('session.json', 'frames.jsonl'):
        file = path/name
        if file.is_symlink() or file.resolve().parent != path or not file.is_file():
            raise ValueError('capture_metadata_path_invalid')
    return path


def capture_metadata(capture_id):
    path = capture_path(capture_id)
    if (path/'INCOMPLETE').exists() or (path/'session.json').stat().st_size > 65536:
        raise ValueError('capture_not_finalized')
    meta = read_json(path/'session.json')
    if (meta.get('schema_version') != 'go1loc.capture.v1' or meta.get('status') != 'complete'
            or type(meta.get('written_frames')) is not int or not 2 <= meta['written_frames'] <= 12000):
        raise ValueError('capture_requires_complete_recording_with_two_frames')
    if (path/'frames.jsonl').stat().st_size > 64*1024*1024:
        raise ValueError('capture_manifest_exceeds_budget')
    return path, meta


def capture_digest(path):
    return hashlib.sha256((sha256_file(path/'session.json') + sha256_file(path/'frames.jsonl')).encode()).hexdigest()


def list_captures():
    rows = []
    for directory in sorted(CAPTURE_ROOT.glob('*/session.json'), reverse=True)[:2000]:
        try:
            _, meta = capture_metadata(directory.parent.name)
            source = meta['source']
            rows.append(dict(capture_id=directory.parent.name, source_kind=meta['source_kind'],
                camera_id=source['camera_id'], width=source['width'], height=source['height'],
                frames=meta['written_frames'], recorded_at=meta.get('started_at', '')))
        except (ValueError, OSError, KeyError, TypeError):
            continue  # incomplete/photo-only recordings are not mapping inputs
    return sorted(rows, key=lambda row: (row['recorded_at'], row['capture_id']), reverse=True)


def register_capture(root, capture_id):
    path, meta = capture_metadata(capture_id)
    upload_id = uuid4().hex
    directory = Path(root)/'uploads'/upload_id
    directory.mkdir(parents=True, exist_ok=False)
    row = dict(upload_id=upload_id, input_kind='capture', capture_id=capture_id,
        original_name=capture_id, sha256=capture_digest(path), source_kind=meta['source_kind'],
        uploaded_at=utc_now(), execution_permitted=False)
    atomic_json(directory/'source.json', row)
    return row


def resolve_capture(row):
    path, meta = capture_metadata(row['capture_id'])
    if capture_digest(path) != row['sha256'] or meta['source_kind'] != row['source_kind']:
        raise ValueError('capture_changed_since_selection')
    return path, meta


def inspect_capture(row, output):
    from .capture_import import load_recording
    path, _ = resolve_capture(row)
    meta, frames = load_recording(path)
    resolve_capture(row)  # source must remain finalized throughout validation
    output.mkdir(parents=True, exist_ok=False)
    preview = output/'first_frame.png'
    shutil.copyfile(path/frames[0]['image_path'], preview)
    if sha256_file(preview) != frames[0]['image_sha256']:
        raise ValueError('capture_changed_during_preview')
    return dict(input_kind='capture', source_kind=meta['source_kind'], source=meta['source'],
        probe=dict(width=frames[0]['width'], height=frames[0]['height'],
                   duration_s=frames[-1]['agent_read_monotonic']-frames[0]['agent_read_monotonic'],
                   source_sha256=row['sha256'], source_path=str(path)), preview=str(preview),
        frames=len(frames), timing_source='agent_read_monotonic_not_exposure',
        sensor_capture_time=None, execution_permitted=False)


def validate_calibration_file(path, row, inspection):
    from .calibration import load_calibration, build_rectification
    from .capture_import import capture_calibration
    data = read_json(path)
    # Standalone upload only: never follow a JSON-provided arbitrary mask path.
    if data.get('valid_mask_path'):
        raise ValueError('web_calibration_requires_standalone_json_no_external_paths')
    cal = load_calibration(path)
    if (max(*cal.image_size, *cal.output_size) > 8192
            or cal.output_size[0]*cal.output_size[1] > 16_000_000):
        raise ValueError('calibration_dimensions_exceed_budget')
    probe = inspection['probe']
    if cal.image_size != (probe['width'], probe['height']):
        raise ValueError('calibration_resolution_mismatch')
    if row.get('input_kind') == 'capture':
        _, meta = resolve_capture(row)
        capture_calibration(path, meta)
    else:
        if data.get('input_projection') not in {'raw_pinhole', 'raw_fisheye', 'rectified_pinhole'}:
            raise ValueError('calibration_input_projection_required')
        if not str(data.get('projection_evidence', '')).strip():
            raise ValueError('calibration_projection_evidence_required')
        projection = data['input_projection']
        models = {'fisheye', 'opencv_fisheye'} if projection == 'raw_fisheye' else {'pinhole', 'opencv'}
        if cal.model.lower() not in models:
            raise ValueError('calibration_projection_model_conflict')
        if projection == 'rectified_pinhole' and any(v != 0 for v in cal.distortion):
            raise ValueError('already_rectified_requires_zero_distortion')
    build_rectification(cal)  # reject malformed distortion / impossible crops now
    return cal


def save_calibration(root, row, inspection, name, content):
    if (Path(name).suffix.lower() != '.json' or not isinstance(content, bytes)
            or not 0 < len(content) <= MAX_CALIBRATION_BYTES):
        raise ValueError('请选择不超过 64 KiB 的独立标定 JSON。')
    data = json.loads(content.decode('utf-8-sig'))
    calibration_id = uuid4().hex
    directory = Path(root)/'uploads'/row['upload_id']/'calibrations'/calibration_id
    directory.mkdir(parents=True, exist_ok=False)
    path = directory/'calibration.json'
    atomic_json(path, data)
    if path.stat().st_size > MAX_CALIBRATION_BYTES:
        raise ValueError('calibration_expanded_json_exceeds_budget')
    cal = validate_calibration_file(path, row, inspection)
    result = dict(calibration_id=calibration_id, sha256=sha256_file(path), original_name=str(name)[:240],
                  original_sha256=hashlib.sha256(content).hexdigest(), input_sha256=row['sha256'],
                  model=cal.model, image_size=list(cal.image_size), execution_permitted=False)
    atomic_json(directory/'binding.json', result)
    return result


def resolve_calibration(root, row, inspection, calibration_id):
    if calibration_id is None:
        return None
    if not isinstance(calibration_id, str) or not re.fullmatch('[a-f0-9]{32}', calibration_id):
        raise ValueError('invalid_calibration_id')
    directory = Path(root)/'uploads'/row['upload_id']/'calibrations'/calibration_id
    if directory.resolve().parent != directory.parent.resolve() or directory.is_symlink():
        raise ValueError('calibration_path_escape')
    path = directory/'calibration.json'
    binding = read_json(directory/'binding.json')
    if (path.is_symlink() or path.stat().st_size > MAX_CALIBRATION_BYTES
            or binding['input_sha256'] != row['sha256'] or sha256_file(path) != binding['sha256']):
        raise ValueError('calibration_binding_or_hash_mismatch')
    validate_calibration_file(path, row, inspection)
    return path
