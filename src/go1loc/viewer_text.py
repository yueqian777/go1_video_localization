"""Chinese presentation helpers. Persisted protocols and evidence stay unchanged."""
import hashlib
from io import BytesIO
from pathlib import Path
import re

VIEW_MODES = {'最终地图回看': 'final_map_replay', '建图过程快照': 'build_snapshot'}
TIME_MODES = {'真实当前照片（检查时效）': 'live_current', '历史照片离线回放': 'offline_replay'}
JOBS = {'inspect_map': '读取最终地图', 'inspect_snapshot': '校验并读取完整快照',
        'locate_object': '定位目标照片并求解三维支持点', 'localize_current': '定位独立当前照片',
        'prepare_route_review': '生成待审核路段', 'save_route_review': '保存审核结果与有向图',
        'plan_route': '计算离线路线预览'}
ERRORS = {
    'route_requires_explicit_start_and_object_evidence': '暂不能规划：请先在「② 找物」完成目标求解，再在「③ 起点」设置起点。',
    'route_graph_required': '缺少已审核路线图：请先在「④ 路段」审核并保存，或在「⑤ 路线」载入已有路线图。',
    'no_ready_snapshot': '没有可读取的完整快照。请确认启动时配置了快照目录，并等待快照发布完成。',
    'final_map_required': '此操作需要最终地图。请在「① 地图」切回最终地图并点击加载。',
    'snapshot_alignment_not_established': '快照未与最终地图建立对齐关系，不能叠加照片定位或路线。请切回最终地图。',
    'open_raw_photo': '请先在「② 找物」打开目标原图，再按住 Shift 拖动鼠标框选。',
    'photo_view_not_front_parallel': '照片视角已旋转，框选可能不准确。请重新打开照片后再框选。',
    'current_photo_must_be_independent': '当前照片必须独立采集，不能复制或改名使用目标照片。',
    'target_photo_is_not_current_pose': '目标照片不能冒充当前位置，请换一张独立采集的当前照片。',
    'capture_time_requires_timezone': '拍摄时间必须包含时区，例如结尾的 +08:00；不能用计算完成时间代替。',
    'Invalid isoformat': '拍摄时间格式不完整。请填写真实采集时间及其时区；未知时间请勿编造。',
    'inspect_valid_attachment': '请先导入接入证据并点击检查，检查通过后才能明确采用。',
    'connection_source_file_required_or_hash_mismatch': '接入来源文件缺失或哈希不符，未采用该证据。请检查原始来源文件。',
    'connection_inputs_changed': '接入文件、地图、起点或时间已改变，请重新检查证据后再采用。',
    'stale_start': '当前照片超过新鲜度要求。历史实验请明确选择离线回放，并填写回放参考时间。',
    'no_path': '没有符合已审核有向边的路径。请检查路线方向、断边与接入证据，不会自动跨越缺口。',
    'no_displayed_planning_result': '尚无可导出的路线。请先完成规划或载入有效的已有结果。',
    'generate_candidates_first': '请先生成待审核候选路段，再执行审核或保存。',
    'reviewer': '请填写审核人，并记录你实际查看的素材和连续性判断依据。',
    'evidence_note': '请填写审核依据：核对了哪段原视频、是否存在跳切或中断。',
    'insufficient_pnp_quality': '照片定位证据不足，未接受位姿。请换用更清晰、有重叠视野的独立照片。',
    'ambiguous_reference_consensus': '多个参考图组给出互相冲突的位置，暂不采用位姿。请提供新的重叠视角，不会任取一个结果。',
    'FileNotFoundError': '未找到输入文件。请检查路径是否存在，或使用选择照片按钮。',
    'IsADirectoryError': '需要文件，但当前路径是文件夹或为空。请选择对应文件。',
    'PermissionError': '文件暂时无法访问。请检查权限或文件是否被其他程序占用。',
}


def protocol_value(value, labels):
    return labels.get(value, value)


def query_failure_notice(result, *, roi_retained=True):
    quality = result.get('quality') or {}
    reason = result.get('failure_reason') or result.get('pose_status') or 'unknown'
    lines = ['计算结束：照片定位未通过，未生成目标三维位置。']
    if 'inlier_ratio' in quality:
        line = (f"有效匹配 {quality.get('num_inliers', '?')} / {quality.get('num_2d3d', '?')}，"
                f"内点率 {quality['inlier_ratio']:.2%}")
        threshold = quality.get('acceptance_thresholds', {}).get('min_inlier_ratio')
        if threshold is not None:
            line += f'（要求至少 {threshold:.2%}）'
        lines.append(line + '。')
    lines.append(ERRORS.get(reason, '原因：' + str(reason)))
    lines.append(('选框已保留；' if roi_retained else '可重新打开原图框选；') +
                 '检查照片、标定后重新计算，不会自动沿用本次失败结果。')
    return '\n'.join(lines)


def query_camera_notice(result):
    quality = result.get('quality') or {}
    source = quality.get('query_intrinsics')
    label = {'self_estimated_not_calibrated': '由图像与地图匹配估计焦距（不是实测标定）',
             'explicit': '采用手动输入的相机参数（固定）',
             'calibrated_rectified': '采用标定及去畸变后的参数（固定）',
             'inferred_or_approximate': '历史结果：使用近似参数，建议重新计算'}.get(source, '未记录相机参数来源')
    camera = quality.get('query_camera') or {}
    if camera:
        label += '\n' + str(camera.get('model')) + '：' + str([round(float(x), 4) for x in camera.get('params', [])])
    return label


def object_failure_notice(result):
    if result.get('failure_reason') == 'multiple_spatial_support_clusters':
        return (f"计算结束：照片已定位，但框内存在多个三维候选（{len(result.get('candidates', []))} 组），未确定唯一目标。"
                '选框可能混入背景或其他物体；请缩小选框到目标轮廓后重新计算。不会自动选择其中一组。')
    return ('计算结束：照片已定位，但框内三维证据不足，未生成目标位置。'
            '可调整选框或补拍有纹理、重叠视野的照片。')


def human_status(value):
    """A short actionable notice; the complete original is kept in diagnostics."""
    text = str(value)
    # Preserve structured result summaries even when raw diagnostic codes follow.
    if text.startswith(('计算结束：', '计算完成：')):
        return text.split('\n诊断：', 1)[0].split('\n结果文件：', 1)[0][:1200]
    for code, explanation in ERRORS.items():
        if code in text:
            return explanation
    for prefix, lead in [('Queued ', '任务已排队：'), ('Running ', '正在')]:
        if text.startswith(prefix):
            kind = text[len(prefix):].split(';')[0]
            return lead + JOBS.get(kind, '处理任务') + '。完成前无需重复点击；关闭浏览器不会取消计算。'
    match = re.search(r'^(final_map_replay|build_snapshot) \| (\d+) cameras \| (\d+)/(\d+) points', text)
    if match:
        mode, cameras, shown, total = match.groups()
        return f'{"最终地图" if mode == "final_map_replay" else "真实建图快照"}已载入：{cameras} 个相机位姿，显示 {shown} / {total} 个空间点。\n' + (
            '未建立米制尺度，距离仅使用地图单位。' if 'map_units' in text else '距离单位请以地图记录为准。')
    messages = [
        ('Reference ', '已显示所选参考照片与拍摄位置。这是历史画面，不是当前位置。'),
        ('PENDING candidates generated', '候选路段已生成，仍待人工审核。紫色线不代表通行许可。'),
        ('Selected raw-photo ROI', '已选中原图区域。下一步点击「计算目标位置与证据」。'),
        ('RAW PHOTO:', '已打开原图。按住 Shift 拖动鼠标框选目标；不要旋转照片。'),
        ('Saved explicitly MANUAL', '已设置人工历史起点。它不是实测当前位置，仅用于离线预览。'),
        ('Independent current camera localized', '独立当前照片已定位；仍需检查并采用合法接入证据，不会自动吸附路线。'),
        ('Loaded verified independent photo', '已载入独立照片定位结果，保留真实拍摄时间。接下来请检查接入证据。'),
        ('Computed camera route preview', '离线路线预览已生成。终点是观察位置，不是物体中心；禁止直接用于机器人执行。'),
        ('Loaded saved CAMERA ROUTE', '已载入通过校验的相机路线预览，不代表当前无障碍。'),
        ('Object result: supported', '目标获得三维支持点。黄色点代表可见表面，不是完整物体中心。'),
        ('Object result:', '目标证据不足，未生成可用于规划的目标。请查看诊断或换图。'),
        ('Query camera', '目标照片相机已定位；还需框选物体并完成目标求解。'),
        ('Query failed:', '照片定位失败，没有沿用旧目标或旧路线。请查看详细诊断。'),
        ('Route rejected:', '路线未通过证据检查，未生成可执行指令。请查看详细诊断。'),
        ('Ignored ', '已忽略过期任务的结果，当前选择不受影响；原结果文件仍保留。'),
        ('Saved 3D canvas', '已保存三维画布截图（不含右侧面板），文件位置见诊断详情。'),
        ('Saved ', '结果已保存在本地工作目录，文件位置见诊断详情。'),
        ('Job failed:', '后台任务失败；已有文件仍保留。请展开诊断详情查看原因。'),
    ]
    for prefix, explanation in messages:
        if text.startswith(prefix):
            return explanation
    if re.match(r'^[\u4e00-\u9fff]', text) or re.match(r'^ValueError: [\u4e00-\u9fff]', text):
        return text[:1200]
    return '操作未完成或有详细信息，请展开「诊断详情」查看原始记录。'


def save_uploaded_image(sidecar, name, content):
    """Copy exact bytes to a safe local inbox; never infer capture time or identity."""
    from PIL import Image, UnidentifiedImageError
    if not content or len(content) > 32 * 1024 * 1024:
        raise ValueError('照片不能为空且不能超过 32 MiB。')
    try:
        with Image.open(BytesIO(content)) as image:
            if image.width * image.height > 40_000_000:
                raise ValueError('照片像素过大，请使用原始相机的合理分辨率。')
            if image.format not in ('PNG', 'JPEG'):
                raise ValueError('请选择 PNG 或 JPEG 原图。')
            suffix = '.png' if image.format == 'PNG' else '.jpg'
            image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError('无法解码图片，请选择有效 PNG 或 JPEG 原图。') from exc
    folder = Path(sidecar).resolve()/'imports'
    folder.mkdir(parents=True, exist_ok=True)
    # Never use the browser-supplied path/name as a filesystem location.
    path = folder/(hashlib.sha256(content).hexdigest()+suffix)
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError('导入目录已有文件内容冲突，未覆盖。')
    else:
        with path.open('xb') as stream:
            stream.write(content)
    return path
