"""One local capture/map workspace, with in-process source-frame subscriptions.

Constructing the workspace never connects to a robot. Existing capture and
Viser applications remain usable independently; no motion adapter is imported.
"""
from pathlib import Path
import time
from uuid import uuid4

from PIL import Image

from .capture_web import CaptureWeb
from .desktop import available_port, load_config
from .jobs import utc_now
from .video import sha256_file
from .web_capture import CAPTURE_ROOT, capture_metadata, list_captures


class LiveFrameReader:
    """Read the same validated source pixels used by the PNG recorder, not UI JPEG."""
    is_live = True

    def __init__(self, controller, output):
        self.controller = controller
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.key = None
        self.identity = None
        self.sequence = 0

    def check_available(self):
        state, frame = self.controller.view()
        if not state['live'] or frame is None:
            raise ValueError('没有新鲜的相机画面：连接中断或疑似冻结，请检查采集页。')
        if frame.header['source_kind'] not in ('go1_shm_readonly', 'orbbec_rgb_mjpeg'):
            raise ValueError('合成演示不能作为实时机器狗定位输入。')
        if self.identity is not None and frame.identity() != self.identity:
            raise ValueError('相机或采集会话已改变，请重新开始定位。')
        return frame

    def next(self):
        frame = self.check_available()
        h = frame.header
        key = (h['session_id'], h['camera_id'], h['agent_seq'])
        if key == self.key:
            return None
        path = self.output / ('current-' + uuid4().hex + '.png')
        Image.fromarray(frame.rgb()).save(path)
        current = self.check_available()
        if current.header['session_id'] != h['session_id'] or current.header['camera_id'] != h['camera_id']:
            raise ValueError('相机连接已改变，请重新开始定位。')
        if not 0 <= time.monotonic() - frame.pc_receive_monotonic <= self.controller.fresh_timeout:
            raise ValueError('保存期间画面已过期，请等待新鲜画面。')
        self.key = key
        self.identity = frame.identity()
        self.sequence += 1
        return path, dict(sequence=self.sequence, session_id=h['session_id'],
            camera_id='camera' + str(h['camera_id']), source_frame_key=f"{h['session_id']}:{h['agent_seq']}",
            source_kind=h['source_kind'], time_mode='live_current',
            image_time_s=frame.pc_receive_monotonic, received_monotonic=frame.pc_receive_monotonic,
            pc_receive_time=frame.pc_receive_time, source_read_time=h['agent_read_monotonic'],
            source_read_wall_time=h['agent_read_wall_time'], runtime_at=utc_now(), sensor_capture_time=None,
            time_source='PC_source_receive_monotonic_not_sensor_exposure', sensor_freshness='unverified',
            query_sha256=sha256_file(path), execution_permitted=False)


class WorkbenchBridge:
    """Fixed local actions; no arbitrary paths, URLs, commands, or motion methods."""
    def __init__(self, viewer, capture):
        self.viewer, self.capture = viewer, capture
        viewer.live_reader_factory = lambda output: LiveFrameReader(capture.controller, output)

    def summary(self):
        state, frame = self.capture.controller.view()
        with self.viewer._state_lock:
            app = self.viewer
            task = app._nav_task
            last = task.last if task else None
            pose_available = bool(state['live'] and frame and task and last
                and task.current_pose_available(time.monotonic())
                and last.get('time_mode') == 'live_current' and last.get('pose_status') == 'localized'
                and last.get('session_id') == frame.header['session_id']
                and last.get('camera_id') == 'camera' + str(frame.header['camera_id'])
                and 0 <= time.monotonic() - last.get('received_monotonic', 0) <= task.max_age_s)
            return dict(enabled=True, viewer_url=f'http://127.0.0.1:{app.server.get_port()}/',
                map_ready=app.controller.mode == 'final_map_replay' and bool(app.controller.model),
                target_ready=bool(app.target_result), current_pose_available=pose_available,
                navigation_status=app.nav_status.value, build_status=app.web_status.value,
                recordings=list_captures(), execution_permitted=False)

    def action(self, action, data):
        app = self.viewer
        with app._state_lock:
            if action == 'mapping-recording':
                capture_id = data.get('capture_id')
                capture_metadata(capture_id)  # No active/incomplete recording or caller paths.
                app.select_capture_for_build(capture_id)
            elif action == 'navigation-live':
                app._require_final()
                frame = LiveFrameReader(self.capture.controller, app.sidecar/'navigation'/'frames').check_available()
                camera = 'camera' + str(frame.header['camera_id'])
                if camera not in app.nav_camera.options:
                    raise ValueError('该视角尚无导航安装配置，请选择前视 camera1 或 camera6。')
                app._nav_invalidate()
                app.nav_source_mode.value = '已开启的本地相机'
                app.nav_camera.value = camera
                app._nav_camera_changed(None)
                app.nav_status.value = '已接入录制台的源帧。核对地图、相机安装与目标后，点击“开始方向引导”。'
            else:
                raise ValueError('unsupported_workspace_action')


class Workbench(CaptureWeb):
    def __init__(self, config_path, output, *, port=8772, viewer_config=None, workspace_dir=None):
        if Path(output).resolve() != CAPTURE_ROOT.resolve():
            raise ValueError('统一工作台使用本项目 data/captures；请保留旧目录并显式迁移后再修改配置。')
        super().__init__(config_path, output, port=port)
        self.viewer = None
        try:
            from .viser_viewer import ViewerApplication
            root = Path(workspace_dir or CAPTURE_ROOT.parent/'workbench').resolve()
            if viewer_config:
                config = load_config(Path(viewer_config))
            else:
                config = dict(map_dir=root/'initial_map', sidecar_dir=root/'viewer', snapshot_dir=None)
            self.viewer = ViewerApplication(config['map_dir'], config['sidecar_dir'],
                snapshot_dir=config['snapshot_dir'], port=available_port(min(self.server.server_port+1,65505)),
                load_initial=bool(viewer_config))
            self.workspace = WorkbenchBridge(self.viewer, self)
        except Exception:
            super().close()
            if self.viewer:
                self.viewer.close()
            raise

    def close(self):
        try:
            super().close()
        finally:
            if self.viewer:
                self.viewer.close()


def main(argv=None):
    from .capture_desktop import main as launch
    return launch(argv)


if __name__ == '__main__':
    raise SystemExit(main())
