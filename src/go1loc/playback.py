"""Source-PTS image replay. No interpolation, route edges or robot execution."""
from bisect import bisect_right
import math
from pathlib import Path
import threading
import time


def playback_timeline(frames_manifest, cameras, image_dir):
    """Use only the verified map's embedded timeline, not a mutable adjacent file."""
    image_dir = Path(image_dir).resolve()
    camera_names = {c['name'] for c in cameras}
    source = (frames_manifest or {}).get('source_kind', 'registered_reference_subset')
    raw = (frames_manifest or {}).get('frames') if frames_manifest is not None else None
    if raw is None:
        raw = [dict(image_path=c.get('image_path') or c['name'], pts_time_s=c.get('pts_time_s')) for c in cameras]
    result = dict(available=False, frames=[], source_kind=source,
                  time_source='screen_display' if source == 'go1_screen_recording' else 'recorded_pts',
                  explanation='没有可核验的时间顺序；无序照片不冒充连续视频。')
    if not isinstance(raw, list) or not raw or len(raw) > 100000:
        return result
    rows = []
    for index, row in enumerate(raw):
        value = row.get('pts_time_s')
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            result['explanation'] = '存在缺失或无效 PTS，不能伪造播放顺序。'
            return result
        name = Path(row['image_path']).name if row.get('image_path') else row.get('image_name')
        path = (image_dir/name).resolve() if name else None
        if path is not None and not path.is_relative_to(image_dir):
            raise ValueError('playback_image_path_escape')
        rows.append(dict(pts_time_s=float(value), frame_index=row.get('frame_index'), source_row=index,
            image_name=name, image_path=str(path) if path else None,
            camera_name=name if name in camera_names else None,
            discontinuity=bool(row.get('discontinuity_before') or row.get('discontinuity_after')),
            quality_reasons=(row.get('quality') or {}).get('rejection_reasons', [])))
    rows.sort(key=lambda row: (row['pts_time_s'], row['source_row']))
    if len({r['pts_time_s'] for r in rows}) != len(rows):
        result['explanation'] = '存在重复 PTS，时间顺序不明确，请核查素材清单。'
        return result
    result.update(available=True, frames=rows, explanation='播放已有抽帧，不是原视频逐帧解码；不表示当前可通行。')
    return result


class PlaybackClock:
    """Monotonic wall clock maps to original PTS; long gaps are never collapsed."""
    def __init__(self, times):
        self.times = list(times)
        if any(not math.isfinite(t) for t in self.times) or self.times != sorted(self.times):
            raise ValueError('invalid_playback_times')
        self.playing = False
        self.speed = 1.
        self.index = 0
        self.position = self.times[0] if self.times else 0.
        self.anchor_time = 0.
        self.anchor_position = self.position

    def tick(self, now):
        if self.playing and self.times:
            self.position = min(self.times[-1], self.anchor_position + max(0, now-self.anchor_time)*self.speed)
            self.index = max(0, bisect_right(self.times, self.position)-1)
            if self.position >= self.times[-1]:
                self.playing = False
        return self.index

    def play(self, now):
        if not self.times: return
        if self.index == len(self.times)-1:
            self.seek(0, now)
        self.anchor_time, self.anchor_position = now, self.position
        self.playing = len(self.times) > 1

    def pause(self, now):
        self.tick(now)
        self.playing = False

    def seek(self, index, now):
        if type(index) is not int or not 0 <= index < len(self.times):
            raise ValueError('invalid_playback_index')
        self.index, self.position = index, self.times[index]
        self.anchor_time, self.anchor_position = now, self.position

    def set_speed(self, speed, now):
        if not .25 <= speed <= 8:
            raise ValueError('invalid_playback_speed')
        self.tick(now)
        self.speed = speed
        self.anchor_time, self.anchor_position = now, self.position


class PlaybackControls:
    def _playback_ui(self):
        gui = self.server.gui
        self.playback_clock = PlaybackClock([])
        self.playback_data = {'frames': []}
        self._playback_rendered = None
        with gui.add_folder('按视频顺序播放全程图片', expand_by_default=True):
            gui.add_markdown('播放整个素材清单的抽帧，图片显示在上方。粉色相机表示当前历史帧；未注册帧不显示位姿。不生成路线、不控制机器狗。')
            self.playback_info = gui.add_text('播放状态', '等待地图时间线。', multiline=True, disabled=True)
            self.playback_slider = gui.add_slider('播放进度（抽帧序号，0 起）', min=0, max=1, step=1, initial_value=0, disabled=True)
            self.playback_slider.on_update(self._guard(self._playback_slider_changed))
            self.playback_speed = gui.add_dropdown('播放速度', ['0.5×', '1×', '2×', '4×'], initial_value='1×')
            self.playback_speed.on_update(self._guard(self._playback_speed_changed))
            self._button('播放全程图片', self._playback_play)
            self._button('暂停图片播放', lambda _: self._pause_playback())
            self._button('上一张图片', lambda _: self._playback_step(-1))
            self._button('下一张图片', lambda _: self._playback_step(1))
            self._button('从第一张重新播放', self._playback_restart)

    def _reset_playback(self, model):
        self.playback_clock.playing = False
        self.controller.clear('reference')
        self.reference_image.visible = False
        data = model.get('playback')
        if data is None:
            # Snapshot mode has only its own verified, registered references.
            cameras = model.get('cameras', [])
            directory = Path(cameras[0].get('image_path', '.')).parent if cameras else Path('.')
            data = playback_timeline(None, cameras, directory)
        self.playback_data = data
        self.playback_clock = PlaybackClock([r['pts_time_s'] for r in data['frames']] if data.get('available') else [])
        self.playback_clock.set_speed(float(self.playback_speed.value.rstrip('×')), time.monotonic())
        self._playback_rendered = None
        self.playback_slider.max = max(1, len(self.playback_clock.times)-1)
        self.playback_slider.value = 0
        self.playback_slider.disabled = not self.playback_clock.times
        self.playback_info.value = (f"共 {len(self.playback_clock.times)} 张时间线图片。点击播放。\n" if self.playback_clock.times else '') + data.get('explanation', '')

    def _pause_playback(self):
        if hasattr(self, 'playback_clock'):
            self.playback_clock.pause(time.monotonic())
            if self._playback_rendered is not None:
                if self._playback_rendered != self.playback_clock.index:
                    self._render_playback()
                else:
                    self._playback_status()

    def _playback_play(self, _):
        if not self.playback_clock.times:
            raise ValueError(self.playback_data.get('explanation', '没有可播放的时间线。'))
        self.playback_clock.play(time.monotonic())
        self._render_playback()

    def _playback_restart(self, event):
        if self.playback_clock.times:
            self._playback_seek(0)
        self._playback_play(event)

    def _playback_seek(self, index):
        self._pause_playback()
        self.playback_clock.seek(index, time.monotonic())
        self._render_playback()

    def _playback_step(self, step):
        if self.playback_clock.times:
            self._playback_seek(max(0, min(len(self.playback_clock.times)-1, self.playback_clock.index+step)))

    def _playback_slider_changed(self, event):
        # Viser server-side updates emit callbacks with no client. Never seek back
        # to a stale tick or pause the clock because of our own slider updates.
        if event.client is not None and self.playback_clock.times:
            self._playback_seek(int(event.target.value))

    def _playback_speed_changed(self, _):
        self.playback_clock.set_speed(float(self.playback_speed.value.rstrip('×')), time.monotonic())

    def _playback_status(self):
        row = self.playback_data['frames'][self.playback_clock.index]
        state = '播放中' if self.playback_clock.playing else '已暂停 / 到达末帧'
        pose = '有当前分量的历史相机位姿' if row['camera_name'] else '未在当前分量注册，不显示位姿'
        self.playback_info.value = (f"{state} · {self.playback_clock.index+1} / {len(self.playback_clock.times)} 张\n"
            f"源帧 {row.get('frame_index')} · PTS {row['pts_time_s']:.3f} 秒 · {self.playback_clock.speed:g}×\n{pose}\n"
            f"{self.playback_data.get('time_source')}：不是现实当前时间。")
        if not self.reference_image.visible:
            self.playback_info.value += '\n图片缺失、未保留或无法解码；保留时间槽，不显示上一张冒充本帧。'
        if row.get('discontinuity') or row.get('quality_reasons'):
            self.playback_info.value += '\n本帧有质量或连续性风险，请核对原始素材。'

    def _render_playback(self):
        row = self.playback_data['frames'][self.playback_clock.index]
        self.reference_image.visible = False
        self.controller.clear('reference')
        self._playback_rendered = self.playback_clock.index
        path = row.get('image_path')
        if path and Path(path).is_file():
            from .viser_viewer import _read_image
            try:
                self.reference_image.image = _read_image(path)
                self.reference_image.visible = True
            except (OSError, ValueError):
                pass
        camera = next((c for c in (self.controller.model or {}).get('cameras', []) if c['name'] == row['camera_name']), None)
        if camera:
            self.frame.value = camera['name']
            self.controller.pose('reference', camera['T_WC'], color=(240, 125, 235))
            if self.follow_reference.value:
                from .viser_viewer import _pose
                T, q = _pose(camera['T_WC'])
                for client in self.server.get_clients().values():
                    client.camera.position, client.camera.wxyz, client.camera.fov = T[:3, 3], q, camera['fov']
        self.reference_info.value = f"回放：{row.get('image_name') or '未保留图片'}\n源 PTS：{row['pts_time_s']:.3f} 秒；不是当前位置。"
        self.playback_slider.value = self.playback_clock.index
        self._playback_status()

    def _playback_tick(self, now):
        if self.playback_clock.playing:
            index = self.playback_clock.tick(now)
            if index != self._playback_rendered:
                self._render_playback()
            elif not self.playback_clock.playing:
                self._playback_status()

    def _start_playback_thread(self):
        def run():
            while not self._stop.wait(.05):
                with self._state_lock:
                    try:
                        self._playback_tick(time.monotonic())
                    except Exception as exc:
                        self.playback_clock.playing = False
                        self.playback_info.value = '播放已暂停：'+str(exc)
        self._playback_thread = threading.Thread(target=run, daemon=True, name='go1loc-image-playback')
        self._playback_thread.start()
