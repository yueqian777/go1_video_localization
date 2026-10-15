"""Generate an explicit, inspectable PowerShell recipe; never execute it."""
from dataclasses import asdict
import math
from pathlib import Path
from uuid import uuid4
from .jobs import atomic_json, read_json
from .video import FrameConfig


def ps_quote(value):
    text = str(value)
    if '\n' in text or '\r' in text or '\x00' in text:
        raise ValueError('路径中不能包含换行或空字符。')
    return "'"+text.replace("'", "''")+"'"


def build_recipe(project, sidecar, video, output, kind, *, screen_config=None, duration=30., interval=.5):
    project, sidecar = Path(project).resolve(), Path(sidecar).resolve()
    video, output = Path(video).resolve(), Path(output).resolve()
    if not video.is_file() or video.suffix.lower() not in {'.mp4', '.mov', '.mkv', '.avi'}:
        raise ValueError('请选择实际存在的视频文件。')
    if output.exists():
        raise ValueError('请使用尚不存在的新输出目录，旧地图与旧结果不能覆盖。')
    if kind not in ('screen', 'video'):
        raise ValueError('请选择录屏或原始单路视频，不能混淆来源。')
    if not math.isfinite(duration) or not math.isfinite(interval) or not 0 < duration <= 600 or interval <= 0:
        raise ValueError('试片时长需在 0 至 600 秒内，抽帧间隔必须为正。')
    count = math.ceil(duration/interval)
    if count > 1000:
        raise ValueError('超过已有单次 1000 帧上限，请分段处理或增大间隔。')
    screen = None
    if kind == 'screen':
        if not screen_config or not Path(screen_config).is_file():
            raise ValueError('录屏需要先按原尺寸预览确认裁剪和掩膜，再提供录屏配置文件。见新视频建图指南。')
        from .screen_import import ScreenConfig
        screen = read_json(screen_config)
        config = ScreenConfig(**screen)
        config.validate(*config.source_size_wh)
        if config.selected_window.upper().startswith('EXAMPLE'):
            raise ValueError('示例配置尚未确认，不能直接套用旧录屏坐标。')
    for path in (project, sidecar, video, output):
        ps_quote(path)
    recipe = sidecar/'build_guides'/uuid4().hex
    atomic_json(recipe/'frames.json', asdict(FrameConfig(interval_s=interval, duration_s=duration, max_frames=count)))
    if screen is not None:
        atomic_json(recipe/'screen.json', screen)
    python = ps_quote(project/'runtime/python312/python.exe')
    prefix = f"Set-Location -LiteralPath {ps_quote(project)}\n"
    prepare = f"& {python} -m go1loc.cli prepare-video {ps_quote(video)} --output {ps_quote(output/'prepared')} --config {ps_quote(recipe/'frames.json')}"
    if screen:
        prepare += f" --screen-config {ps_quote(recipe/'screen.json')}"
    build = f"& {python} -m go1loc.cli build-map --images {ps_quote(output/'prepared/images')} --output {ps_quote(output/'map')} --map-id {ps_quote(output.name)} --max-keypoints 2048 --snapshot-frames-freq 8 --sidecar {ps_quote(output/'build_run')}"
    run_selector = (f"$buildRuns = @(Get-ChildItem -LiteralPath {ps_quote(output/'build_run')} -Directory | Where-Object {{ Test-Path -LiteralPath (Join-Path $_.FullName 'run.json') }})\n"
                    "if ($buildRuns.Count -ne 1) { throw 'Expected exactly one build attempt; inspect run.json before choosing.' }\n"
                    "$buildRun = $buildRuns[0].FullName\n")
    view = f"& {python} -m go1loc.cli view --root {ps_quote(output/'map')} --sidecar {ps_quote(output/'viewer')} --snapshots (Join-Path $buildRun 'snapshots') --port 8770"
    install = f"& {ps_quote(project/'tools/install_desktop_shortcut.ps1')} -MapDirectory {ps_quote(output/'map')} -SidecarDirectory {ps_quote(output/'viewer')} -SnapshotDirectory (Join-Path $buildRun 'snapshots') -Port 8767"
    return (f"本操作单仅生成命令，未解码、建图或连接机器狗。先试 {duration:g} 秒，最多 {count} 帧；不是默认处理整部视频。\n\n"
        "【1】打开 PowerShell，复制以下命令抽帧。完成后检查 prepared 中的图片、frames_manifest.json 和质量报告。\n"+prefix+prepare+
        "\n\n【2】素材质量合适后，在同一终端执行建图（可能持续数分钟）。低质量输入不要强行关闭过滤以制造结果。\n"+build+
        "\n\n【3】建图运行期间，另开 PowerShell；run.json 出现后执行以下命令并访问 http://127.0.0.1:8766/。\n"+prefix+run_selector+
        f"& {python} -m go1loc.cli watch-build --run $buildRun --sidecar {ps_quote(output/'watcher')} --port 8766"+
        "\n\n【4】最终地图成功后，在新终端执行并访问 http://127.0.0.1:8770/。失败时保留已有快照和日志，不复制假清单。\n"+prefix+run_selector+view+
        "\n\n【5】可选：先停止旧桌面查看器，再让桌面快捷方式改为打开这张新图。\n"+
        f"& {python} -m go1loc.desktop --config {ps_quote(project/'.cache/desktop_viewer.json')} --stop --no-browser\n"+install)


class BuildGuideControls:
    def _build_guide_ui(self):
        gui = self.server.gui
        with gui.add_folder('以后自己建图：生成操作单', expand_by_default=False):
            gui.add_markdown('当前快捷方式只打开已有地图。这里生成新视频的操作命令，**不会自动开始建图**。先试 30 秒，检查质量后再重建；每次使用新目录。')
            self.build_video = gui.add_text('新视频完整路径', '')
            self.build_output = gui.add_text('新建图输出目录（必须尚不存在）', '')
            self.build_kind = gui.add_dropdown('视频来源类型', ['Go1 界面录屏', '原始单路视频'])
            self.build_screen = gui.add_text('录屏裁剪 / 掩膜配置文件（仅录屏）', '')
            self.build_duration = gui.add_number('从开头处理的秒数（先试 30 秒）', 30., min=.1, max=600., step=1.)
            self.build_interval = gui.add_number('抽帧间隔（秒）', .5, min=.05, max=10., step=.05)
            self._button('生成新视频建图操作单', self._generate_build_recipe)
            self.build_commands = gui.add_text('按阶段复制到 PowerShell 执行', '尚未生成。录屏先按原尺寸确认裁剪，不可直接套用旧坐标。', multiline=True, disabled=True)

    def _generate_build_recipe(self, _):
        self.build_commands.value = '正在核对输入；旧操作单已清除。'
        self.build_commands.value = build_recipe(Path(__file__).resolve().parents[2], self.sidecar,
            self.build_video.value, self.build_output.value,
            'screen' if self.build_kind.value == 'Go1 界面录屏' else 'video',
            screen_config=self.build_screen.value.strip() or None,
            duration=float(self.build_duration.value), interval=float(self.build_interval.value))
        self._message('操作单已生成。先执行抽帧并检查质量，确认后再执行建图；尚未启动任何计算。')
