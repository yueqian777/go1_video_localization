"""Local browser build jobs. Immutable uploads, bounded inputs, existing pipeline."""
from dataclasses import asdict
import hashlib
import math
from pathlib import Path
import re
from uuid import uuid4

from .jobs import JobManager, atomic_json, read_json, utc_now

MAX_UPLOAD_BYTES = 512 * 1024 * 1024


def save_video_upload(root, name, content):
    if Path(name).suffix.lower() != '.mp4' or not isinstance(content, bytes) or not content:
        raise ValueError('请选择非空 MP4 视频文件。')
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError('网页选片上限为 512 MiB，请先录制较短的试片。')
    upload_id = uuid4().hex
    directory = Path(root).resolve()/'uploads'/upload_id
    directory.mkdir(parents=True, exist_ok=False)
    path = directory/'source.mp4'  # Client name is metadata, never a filesystem path.
    with path.open('xb') as stream:
        stream.write(content)
    row = dict(upload_id=upload_id, path=str(path), original_name=str(name)[:240],
               sha256=hashlib.sha256(content).hexdigest(), bytes=len(content),
               uploaded_at=utc_now(), execution_permitted=False)
    atomic_json(directory/'source.json', row)
    return row


def _upload(root, upload_id):
    if not re.fullmatch('[a-f0-9]{32}', str(upload_id)):
        raise ValueError('invalid_upload_id')
    directory = (Path(root)/'uploads'/upload_id).resolve()
    if directory.parent != (Path(root)/'uploads').resolve() or directory.is_symlink():
        raise ValueError('upload_path_escape')
    source = directory/'source.mp4'
    if source.is_symlink() or not 0 < source.stat().st_size <= MAX_UPLOAD_BYTES:
        raise ValueError('invalid_upload_file')
    metadata = read_json(directory/'source.json')
    from .video import sha256_file
    if sha256_file(source) != metadata['sha256']:
        raise ValueError('upload_hash_mismatch')
    return source, metadata


def validate_settings(settings, probe):
    from .video import FrameConfig
    from .screen_import import ScreenConfig, effective_mask
    if settings.get('confirmed') is not True:
        raise ValueError('请先确认预览、固定单路视图和遮挡区域。')
    if type(settings.get('exploratory')) is not bool:
        raise ValueError('invalid_exploratory_choice')
    values = [settings.get(key) for key in ('start', 'duration', 'interval')]
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        raise ValueError('invalid_time_budget')
    start, duration, interval = values
    if start < 0 or not 0 < duration <= 600 or not .05 <= interval <= 10:
        raise ValueError('处理时长需在 0–600 秒内，间隔需在 0.05–10 秒内。')
    if probe.get('duration_s') is not None and start >= probe['duration_s']:
        raise ValueError('开始时间已超出视频长度。')
    count = math.ceil(duration/interval)
    if not 2 <= count <= 240:
        raise ValueError('网页单次处理 2–240 个候选帧，请缩短片段或增大间隔。')
    frames = FrameConfig(start_s=start, duration_s=duration, interval_s=interval,
                         max_frames=count, quality_filter=not settings['exploratory'])
    if settings.get('kind') == 'video':
        return frames, None
    if settings.get('kind') != 'screen':
        raise ValueError('invalid_video_source_kind')
    if len(settings.get('exclusions', [])) > 32:
        raise ValueError('最多添加 32 个排除区域。')
    screen = ScreenConfig(source_size_wh=[probe['width'], probe['height']],
        crop_xywh=settings['crop'], selected_window=settings['window'],
        camera_id=settings.get('camera_id') or None, camera_label=settings.get('camera_label') or 'unknown',
        display_mode=settings['display_mode'],
        display_evidence=settings.get('display_evidence') or 'unknown; no additional undistortion performed',
        expected_source_sha256=probe['source_sha256'], exclude_rectangles=settings['exclusions'])
    screen.validate(probe['width'], probe['height'])
    if (effective_mask(screen) > 0).mean() < .2:
        raise ValueError('有效场景区域不足 20%，请重新选择裁剪或排除区域。')
    return frames, screen


def execute_web_job(request):
    """Called only by the fixed jobs worker; accepts IDs, not arbitrary commands."""
    from .screen_import import inspect_screen
    from .video import prepare_video
    directory = Path(request['job_dir']).resolve()
    root = directory.parents[2]  # WebBuildSession always owns root/worker/jobs/UUID.
    source, metadata = _upload(root, request['params']['upload_id'])
    output = directory/'output'
    if request['kind'] == 'inspect_video':
        from .video import probe_video
        probe = probe_video(source)
        if not all(type(n) is int and 1 <= n <= 8192 for n in (probe.width, probe.height)):
            raise ValueError('视频尺寸无效或超过网页预览上限 8192。')
        result = inspect_screen(source, output)
        atomic_json(source.parent/'inspection.json', result)
        path = output/'inspection.json'
        atomic_json(path, result)
        return dict(result_path=str(path), output_dir=str(output))
    inspection = read_json(source.parent/'inspection.json')
    if inspection['probe']['source_sha256'] != metadata['sha256']:
        raise ValueError('inspection_hash_mismatch')
    settings = request['params']['settings']
    frames, screen = validate_settings(settings, inspection['probe'])
    if output.exists():
        raise ValueError('web_build_output_exists_use_new_job')
    output.mkdir()
    atomic_json(output/'settings.json', dict(settings=settings, frames=asdict(frames),
                 screen=asdict(screen) if screen else None, source=metadata, execution_permitted=False))
    progress = dict(stage='prepare_video', map_dir=str(output/'map'),
                    run_root=str(output/'build_runs'), prepared_dir=str(output/'prepared'),
                    exploratory=settings['exploratory'], execution_permitted=False)
    atomic_json(directory/'web_progress.json', progress)
    prepared = prepare_video(source, output/'prepared', frames, screen_config=screen)
    progress.update(candidate_count=prepared['candidate_count'], kept_count=prepared['kept_count'])
    atomic_json(directory/'web_progress.json', progress)
    if prepared['kept_count'] < 2:
        raise ValueError('quality_insufficient: 有效帧少于 2 张；请重录清晰素材，或明确勾选低质量探索后新建任务。质量报告已保留。')
    from .mapping import MappingConfig, build_map
    progress['stage'] = 'build_map'
    atomic_json(directory/'web_progress.json', progress)
    config = MappingConfig(map_id='web-'+directory.name, max_keypoints=2048,
                           snapshot_frames_freq=5, snapshot_max_count=20,
                           sidecar_dir=str(output/'build_runs'))
    manifest = build_map(output/'prepared/images', output/'map', config)
    progress.update(stage='completed', map_id=manifest['map_id'])
    path = output/'web_result.json'
    atomic_json(path, progress)
    atomic_json(directory/'web_progress.json', progress)
    return dict(result_path=str(path), output_dir=str(output))


class WebBuildSession:
    """One persisted selection; browser disconnect never starts/stops workers."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.jobs = JobManager(self.root/'reserved-map', self.root/'worker')
        self.record = read_json(self.root/'active.json') if (self.root/'active.json').exists() else {}

    def _save(self):
        atomic_json(self.root/'active.json', self.record)

    def busy(self):
        return any(j['status'] in ('queued', 'running') for j in self.jobs.list_jobs())

    def upload(self, name, content):
        if self.busy():
            raise ValueError('当前视频任务尚未结束，请勿重复上传或启动。')
        row = save_video_upload(self.root, name, content)
        state = self.jobs.submit('inspect_video', {'upload_id': row['upload_id']})
        self.record = dict(upload=row, inspection_job=state['job_id'])
        self._save()
        return state

    def inspection(self):
        job_id = self.record.get('inspection_job')
        if not job_id:
            return None
        state = self.jobs.status(job_id)
        return read_json(state['result_path']) if state['status'] == 'completed' else None

    def start(self, settings):
        if self.busy():
            raise ValueError('当前视频任务尚未结束，不能重复开始。')
        inspection = self.inspection()
        if inspection is None:
            raise ValueError('请先选择视频并等待原尺寸预览。')
        validate_settings(settings, inspection['probe'])
        state = self.jobs.submit('build_video', dict(upload_id=self.record['upload']['upload_id'], settings=settings))
        self.record.update(build_job=state['job_id'], settings=settings)
        self._save()
        return state

    def state(self):
        job_id = self.record.get('build_job') or self.record.get('inspection_job')
        if not job_id:
            return None
        state = self.jobs.status(job_id)
        directory = self.jobs.root/job_id
        state['job_dir'] = str(directory)
        state['progress'] = read_json(directory/'web_progress.json') if (directory/'web_progress.json').exists() else {}
        runs = list((directory/'output/build_runs').glob('*/run.json'))
        if len(runs) > 1:
            raise ValueError('ambiguous_build_run')
        if runs:
            from .build_watch import read_build_state
            state['run_dir'] = str(runs[0].parent)
            state['build'] = read_build_state(runs[0].parent)
        return state
