"""Default Viser shadow-guidance workflow; strict research tools remain separate."""
from pathlib import Path
import threading
import time
from uuid import uuid4
import numpy as np

from .jobs import atomic_json, read_json, utc_now
from .navigation import NavigationTask
from .navigation_io import (prepare_index, observation_goals, CaptureReplay, LocalCaptureReader,
                            count_target_matches, reference_pixel_hashes)
from .navigation_localizer import ResidentLocalizer, LatestFrameWorker

STATES={'locating':'正在定位','planned':'路线已规划','turning':'请按提示转向','following':'沿历史路线前进',
    'relocalizing':'需要重新观察','blocked':'路线受阻','observation_region_reached':'已到观察区，等待新画面确认目标',
    'target_confirmed':'新画面已复现目标几何证据','paused':'已暂停','cancelled':'已取消','failed':'任务失败'}
REASONS={'ambiguous_branch':'附近有多段相似路线，请换一个视角，再观察前方与两侧。',
    'off_route':'当前位置不在可信历史路段附近，请先遥控回到熟悉路段。',
    'frame_stale_or_disconnected':'画面已过期或断开，方向提示已撤销。请检查本地采集页。',
    'no_reachable_good_observation':'当前方向无法到达优质目标观察区；请检查断点、单向路段或另选目标。',
    'unchanged_pixels_static_or_frozen':'画面内容重复，可能静止或冻结；不会据此推进路线。',
    'localization_required':'照片暂未定位成功，请换一个包含更多环境细节的视角。',
    'source_changed_restart_required':'相机或采集会话已改变，请重新开始引导。',
    'no_observed_progress':'连续观察未发现前进；请检查障碍，可封闭当前路段后重规划。',
    'current_segment_closed':'当前路段已封闭，无法从它继续。请回到另一条已知路段。',
    'stale_result':'本次计算完成时图像已过期，等待更新画面。',
    'heading_undefined':'当前朝向无法投影到地图地面，请核对相机安装方向。'}
REASONS['branch_change_needs_second_observation']='定位发生较大路段跳变，正在等待第二张实际画面确认；请保持观察，不按旧方向行走。'
REASONS['replay_ended_without_fabricated_arrival']='这段独立录像已经读完，但尚未取得到达证据。请采集沿已知路线走向目标的新录像。'
REASONS['reference_evidence_missing']='缺少可靠参考图对应，等待更清晰的新视角。'


class NavigationControls:
    def _navigation_ui(self):
        gui=self.server.gui
        self._nav_generation=0;self._nav_worker=None;self._nav_task=None;self._nav_source=None
        self._nav_graph=None;self._nav_preparing=False;self._nav_events=[];self._nav_replay_done=False
        with self.navigation_panel:
            gui.add_markdown('**选地图 → 选当前画面 → 框选目标 → 开始引导。**\n\n'
                '你用遥控器行走；网页依据新画面给方向，不发送运动指令。没有尺度时显示地图单位。')
            self.nav_status=gui.add_text('导航状态','等待地图和目标。',multiline=True,disabled=True)
            self.nav_source_mode=gui.add_dropdown('当前画面来源',['已开启的本地相机','独立录像回放','单张照片（离线）'])
            self.nav_camera=gui.add_dropdown('当前相机安装配置',['camera6','camera1'])
            self.nav_source_mode.on_update(self._guard(lambda _:self._nav_invalidate()))
            self.nav_camera.on_update(self._guard(self._nav_camera_changed))
            self.nav_recording=gui.add_dropdown('独立回放录像',['（刷新后选择）'])
            self.nav_recording.on_update(self._guard(lambda _:self._nav_invalidate()))
            self.current_photo.on_update(self._guard(lambda _:self._nav_invalidate()))
            self._button('刷新可用录像',self._nav_refresh_recordings)
            self._photo_upload('上传单张当前位置照片',self.current_photo)
            self._photo_upload('选择目标照片',self.photo)
            self._button('取一张当前画面作为目标照片',self._nav_current_as_target)
            self._button('打开目标照片，用鼠标框选',self._open_photo)
            self._button('计算选框内的目标',self._locate_object)
            self.nav_target_status=gui.add_text('目标状态',self.roi_status.value,multiline=True,disabled=True)
            self.nav_target_preview=gui.add_image(np.zeros((2,2,3),np.uint8),label='目标选框',visible=False)
            self._button('开始方向引导（不控制机器狗）',self._nav_start)
            self._button('暂停引导',lambda _:self._nav_pause())
            self._button('继续引导',lambda _:self._nav_resume())
            self._button('取消任务',lambda _:self._nav_cancel())
            self.nav_current=gui.add_image(np.zeros((2,2,3),np.uint8),label='实际用于定位的当前画面',visible=False)
            self.nav_next=gui.add_image(np.zeros((2,2,3),np.uint8),label='下一观察位置的参考画面',visible=False)
            self.nav_detail=gui.add_text('方向与进度','尚无有效定位。',multiline=True,disabled=True)
            with gui.add_folder('首次使用：地图来源与相机安装',expand_by_default=False):
                self.nav_traversed=gui.add_checkbox('本地图全程由机器狗自行行走，未搬动、剪辑或倒放',False)
                self.nav_upright=gui.add_checkbox('所选相机已固定朝前，画面上方朝上',False)
                self.nav_reverse=gui.add_checkbox('允许反向：已确认本图为可逆平面通道（反向画面未验证）',False)
                self.nav_use_up=gui.add_checkbox('使用手动地图竖直方向（否则从正立参考相机估计）',False)
                self.nav_up=gui.add_vector3('手动地图向上矢量 x/y/z',(0.,-1.,0.))
                self.nav_calibration=gui.add_text('当前相机标定文件（可选；不沿用目标照片参数）','')
                self.nav_lookahead=gui.add_number('前视跨度（参考采样间隔倍数）',3.,min=1.,max=10.,step=.5)
                gui.add_markdown('配置按地图与相机分别保存。地图竖直方向暂由正立相机估计，不是外参或尺度标定；仅用于影子引导。')
                self._button('保存一次性设置并生成导航索引',self._nav_prepare_only)
            with gui.add_folder('例外、封路与能力状态',expand_by_default=False):
                self.nav_exceptions=gui.add_text('自动检查出的例外','尚未生成索引。',multiline=True,disabled=True)
                self._button('封闭当前路段并重规划',self._nav_close_current)
                self._button('重试定位（保留封路）',lambda _:self._nav_resume())
                gui.add_markdown('执行适配器：未连接。现有 go1_hri 已验证档案为直行；转向、局部目标、实时避障反馈均未验收。'
                    '不会把 gait=4 或界面勾选当成避障证明，也不会另开 UDP 控制源。')
            self._nav_refresh_recordings(None)

    def _nav_refresh_recordings(self,_):
        from .web_capture import list_captures
        rows=[r for r in list_captures() if r['source_kind']!='synthetic_test']
        self._nav_catalog={f"{r['capture_id']} · 相机{r['camera_id']} · {r['frames']}帧":r['capture_id'] for r in rows}
        self.nav_recording.options=list(self._nav_catalog) or ['（没有完整录像）']
        self.nav_recording.value=self.nav_recording.options[0]

    def _nav_invalidate(self):
        if not hasattr(self,'_nav_generation'):
            return
        self._nav_generation+=1;self._nav_preparing=False
        if self._nav_worker:
            self._nav_worker.close()
        self._nav_worker=None;self._nav_task=None;self._nav_source=None;self._nav_replay_done=False
        self.nav_status.value='等待开始；旧任务已失效。'
        self.nav_next.visible=False;self.nav_current.visible=False
        self.nav_detail.value='输入已改变，旧路线和方向提示已撤销。'
        for group in ('navigation_route','navigation_goal','navigation_current'):
            self.controller.clear(group)

    def _nav_profile_path(self):
        return self.sidecar/'navigation'/'setup.json'

    def _nav_camera_changed(self,_):
        self._nav_invalidate()
        self.nav_upright.value=False
        self.nav_calibration.value=''
        if self._nav_profile_path().is_file():
            profile=read_json(self._nav_profile_path())
            if self.controller.model and profile.get('map_hash')==self.controller.model['map_hash']:
                self.nav_upright.value=profile.get('mounts',{}).get(self.nav_camera.value,{}).get('forward_upright') is True
                self.nav_calibration.value=profile.get('mounts',{}).get(self.nav_camera.value,{}).get('calibration_path') or ''

    def _nav_current_as_target(self,_):
        mode=self.nav_source_mode.value;camera=self.nav_camera.value
        last=self._nav_task.last if self._nav_task else None
        if mode=='已开启的本地相机':
            # A first target must not depend on a navigation task that itself
            # requires a target. Read one fresh frame, never start acquisition.
            self._nav_invalidate()
            generation=self._nav_generation
            output=self.sidecar/'navigation'/'target_inputs'/uuid4().hex
            self.nav_status.value='正在读取已有相机画面，不会启动采集或运动…'
            def fetch():
                try:
                    item=LocalCaptureReader(output).next()
                    if not item:
                        raise ValueError('尚无新画面，请检查本地采集页后重试。')
                    path,metadata=item
                    if metadata['camera_id']!=camera:
                        raise ValueError('实际输入相机与所选安装配置不同，请选对应相机。')
                    with self._state_lock:
                        if generation==self._nav_generation:
                            self._nav_accept_target_frame(path)
                except Exception:
                    with self._state_lock:
                        if generation==self._nav_generation:
                            self.nav_status.value='未取得对应相机的新画面。请检查本地采集页及相机选择后重试；原目标保留。'
            threading.Thread(target=fetch,daemon=True,name='navigation-target-frame').start()
            return
        if mode=='单张照片（离线）' and Path(self.current_photo.value).is_file():
            path=self.current_photo.value
        elif last and last.get('image_path'):
            path=last['image_path']
        else:
            raise ValueError('请先上传当前位置照片，或取得回放画面；也可直接上传目标照片。')
        self._nav_accept_target_frame(path)

    def _nav_accept_target_frame(self,path):
        self._invalidate_target(clear_query=True)
        self.photo.value=str(path);self._query_selected=True;self.query_path.value=''
        self.nav_status.value='已取得目标照片，请打开原图框选并计算。'
        self._message('当前原图已设为待框选目标。请打开原图框选；研究模式的独立照片检查保持不变。')

    def _nav_map_loaded(self):
        self._nav_invalidate();self._nav_graph=None
        self.nav_traversed.value=False;self.nav_upright.value=False;self.nav_reverse.value=False
        self.nav_use_up.value=False;self.nav_lookahead.value=3.
        self.nav_calibration.value=''
        path=self._nav_profile_path()
        if path.is_file():
            profile=read_json(path)
            if profile.get('map_hash')==self.controller.model['map_hash']:
                self.nav_traversed.value=profile.get('robot_traversed') is True and profile.get('unhandled_unedited') is True
                self.nav_reverse.value=profile.get('reverse_allowed') is True
                self.nav_upright.value=profile.get('mounts',{}).get(self.nav_camera.value,{}).get('forward_upright') is True
                self.nav_calibration.value=profile.get('mounts',{}).get(self.nav_camera.value,{}).get('calibration_path') or ''
                self.nav_use_up.value=profile.get('up_override') is not None
                self.nav_up.value=tuple(profile.get('up_override') or (0.,-1.,0.))
                self.nav_lookahead.value=profile.get('lookahead_steps',3.)
                if self.nav_traversed.value:
                    self._nav_prepare_only(None)

    def _nav_profile(self):
        self._require_final()
        if not self.nav_traversed.value:
            raise ValueError('请在首次使用设置中确认本地图的实际采集方式；不是每条路段逐一审核。')
        mounts={}
        if self._nav_profile_path().is_file():
            prior=read_json(self._nav_profile_path())
            if prior.get('map_hash')==self.controller.model['map_hash']:
                mounts=prior.get('mounts',{})
        mounts[self.nav_camera.value]={'forward_upright':bool(self.nav_upright.value),
            'source':'operator_declaration_not_calibrated_extrinsics',
            'calibration_path':self.nav_calibration.value.strip() or None}
        if self.nav_calibration.value.strip():
            cal=read_json(Path(self.nav_calibration.value.strip()))
            if 'capture_camera_id' in cal and 'camera'+str(cal['capture_camera_id'])!=self.nav_camera.value:
                raise ValueError('标定文件绑定了另一台相机，请重新选择；不会把 camera1 的标定用于 camera6。')
        profile=dict(map_hash=self.controller.model['map_hash'],robot_traversed=True,unhandled_unedited=True,
            reverse_allowed=bool(self.nav_reverse.value),reversible_plane_confirmed=bool(self.nav_reverse.value),
            mounts=mounts,declared_at=utc_now(),lookahead_steps=float(self.nav_lookahead.value),
            up_override=list(self.nav_up.value) if self.nav_use_up.value else None)
        if profile['up_override'] is not None:
            from .navigation import unit
            profile['up_override']=unit(profile['up_override']).tolist()
        atomic_json(self._nav_profile_path(),profile)
        return profile

    def _nav_prepare_only(self,_):
        profile=self._nav_profile();self._nav_invalidate();self._nav_preparing=True
        self._nav_launch(profile,start=False)

    def _nav_start(self,_):
        profile=self._nav_profile()
        if not self.nav_upright.value:
            raise ValueError('请先确认所选相机朝前、画面正立；camera1 与 camera6 的设置不共用。')
        if not self.target_result:
            raise ValueError('请先上传目标照片，框选并计算；需获得目标支持证据。')
        self._nav_invalidate();self._nav_preparing=True
        self._nav_launch(profile,start=True)

    def _nav_launch(self,profile,*,start):
        generation=self._nav_generation;map_dir=self.map_dir;sidecar=self.sidecar/'navigation'
        target=self.target_result;mode=self.nav_source_mode.value;camera=self.nav_camera.value
        recording=self._nav_catalog.get(self.nav_recording.value);photo=self.current_photo.value
        from .jobs import _localization_config
        calibration=profile['mounts'][camera].get('calibration_path')
        config=_localization_config({'calibration_path':calibration} if calibration else {})
        self.nav_status.value='正在准备导航索引与常驻定位器…' if start else '正在生成导航索引…'
        def setup():
            worker=None
            try:
                graph=prepare_index(map_dir,sidecar,profile)
                if start:
                    backend=ResidentLocalizer(map_dir)
                    goals=observation_goals(backend.reconstruction,graph,target)
                    run=sidecar/('task-'+uuid4().hex)
                    atomic_json(run/'goals.json',goals)
                    if mode=='独立录像回放':
                        from .web_capture import capture_path
                        source=CaptureReplay(capture_path(recording),
                            map_image_hashes=backend.manifest['reference_image_sha256'].values(),
                            map_pixel_hashes=reference_pixel_hashes(backend.manifest))
                    elif mode=='已开启的本地相机':
                        source=LocalCaptureReader(run/'frames')
                    else:
                        source=None
                        if not Path(photo).is_file():
                            raise ValueError('请上传当前位置照片；拍摄时间未知时仅作为离线观测。')
                    task=NavigationTask(graph,goals,up=profile.get('up_override') or graph['up_estimate'],
                        lookahead_steps=profile.get('lookahead_steps',3.))
                    worker=LatestFrameWorker(lambda:backend,run/'queries')
                with self._state_lock:
                    if generation!=self._nav_generation:
                        if worker: worker.close()
                        return
                    self._nav_graph=graph;self._nav_preparing=False
                    self.nav_exceptions.value=f"自动连接 {len(graph['edges'])} 条；粗导航点 {len(graph['navigation_nodes'])} 个。\n"+('\n'.join(
                        f"{r['from_frame']} → {r['to_frame']}：{', '.join(r['reasons'])}" for r in graph['exceptions']) or '未检测到断点。')
                    if not start:
                        self.nav_status.value='导航索引已准备好，不需要逐段点确认。';return
                    self._nav_worker=worker;self._nav_task=task;self._nav_source=source
                    self._nav_config=config;self._nav_camera_at_start=camera;self._nav_run=run;self._nav_events=[]
                    self.nav_status.value='等待当前画面定位。'
                    if source is None:
                        from .video import sha256_file
                        worker.submit(Path(photo),dict(sequence=1,session_id=uuid4().hex,camera_id=camera,
                            query_sha256=sha256_file(Path(photo)),time_mode='offline_replay',image_time_s=0.,
                            sensor_capture_time=None,received_monotonic=time.monotonic(),runtime_at=utc_now(),
                            time_source='single_offline_photo_capture_time_unknown'),config)
            except Exception as exc:
                if worker: worker.close()
                with self._state_lock:
                    if generation==self._nav_generation:
                        self._nav_preparing=False;self.nav_status.value='无法开始：'+str(exc)
        threading.Thread(target=setup,daemon=True,name='navigation-setup').start()

    def _nav_pause(self):
        if self._nav_task:
            self._nav_task.pause()
            self._nav_worker.reset()
            self._nav_render()

    def _nav_resume(self):
        if self._nav_task:
            self._nav_task.resume();self._nav_render()

    def _nav_cancel(self):
        if self._nav_task:
            self._nav_task.cancel();self._nav_worker.reset();self._nav_source=None;self._nav_render()

    def _nav_close_current(self,_):
        if self._nav_task and (self._nav_task.association or {}).get('status')=='associated':
            self._nav_task.close_edge(self._nav_task.association['edge_id'])
            self._nav_render()

    def _nav_tick(self):
        if not hasattr(self,'_nav_task'):
            return
        self.nav_target_status.value=self.roi_status.value
        self.nav_target_preview.visible=self.roi_preview.visible
        if self.roi_preview.visible:
            self.nav_target_preview.image=self.roi_preview.image
        worker,task=self._nav_worker,self._nav_task
        if worker is None or task is None:
            return
        completed=worker.take()
        if completed:
            if completed['error']:
                task.state='relocalizing';task.guidance=None;task.reason='localization_required'
                self.nav_status.value='定位未完成：'+completed['error']
            else:
                observation=completed['observation']
                if observation['camera_id']!=self._nav_camera_at_start:
                    self._nav_pause();self.nav_status.value='输入相机与所选安装配置不同，请选择对应相机重新开始。';return
                observation['target_matches']=count_target_matches(observation,self.target_result)
                task.observe(observation,now=time.monotonic());task.confirm(now=time.monotonic())
                from .viser_viewer import _read_image
                self.nav_current.image=_read_image(Path(observation['image_path']));self.nav_current.visible=True
                self._nav_latency=completed['latency_s']
            self._nav_events.append({**task.snapshot(),'at':utc_now(),'latency_s':completed['latency_s'],
                'query_directory':completed.get('directory'),'error':completed['error']})
            atomic_json(self._nav_run/'guidance_log.json',self._nav_events[-10000:])
        task.tick(time.monotonic())
        if self._nav_source and task.state not in ('paused','cancelled','target_confirmed'):
            live=isinstance(self._nav_source,LocalCaptureReader)
            if live or (not worker.busy and worker.pending is None):
                try:
                    item=self._nav_source.next()
                    if item:
                        worker.submit(*item,self._nav_config)
                    elif not live:
                        self._nav_replay_done=True;self._nav_source=None
                        task.pause();task.reason='replay_ended_without_fabricated_arrival'
                except Exception as exc:
                    task.state='relocalizing';task.guidance=None;task.reason='frame_stale_or_disconnected'
                    self.nav_status.value='采集不可用：'+str(exc)
        self._nav_render()

    def _nav_render(self):
        task=self._nav_task
        if task is None:
            return
        prefix='独立录像/照片回放 · ' if task.last and task.last['time_mode']=='offline_replay' else '本地当前画面 · '
        self.nav_status.value=prefix+STATES.get(task.state,task.state)+'\n'+REASONS.get(task.reason,task.reason or '')
        if self._nav_replay_done:
            self.nav_status.value+='\n没有生成机器人运动。'
        guidance=task.guidance
        self.nav_detail.value=(f"{guidance['direction']} · 朝向误差 {guidance['yaw_error_degrees']:.0f}°\n"
            f"剩余参考路线 {guidance['remaining']:.2f} {guidance['units']}\n"
            f"下一观察图：{guidance['reference']}\n"
            f"定位计算 {getattr(self,'_nav_latency',0):.2f} 秒；以实际新图推进，不按时间动画推进。"
            if guidance else '当前无有效方向指令。影子引导不会控制机器狗。')
        if task.last and task.last.get('pose_status')=='localized':
            self.controller.pose('navigation_current',task.last['T_WC_query'],color=(255,130,0))
        if task.plan and task.plan['status']=='planned':
            self._draw_preview('navigation_route',task.plan['path'],'',(0,190,220))
            self.controller.pose('navigation_goal',task.plan['goal_observation_pose'],color=(45,210,70))
        else:
            self.controller.clear('navigation_route');self.controller.clear('navigation_goal')
        if guidance:
            camera=next((c for c in self.controller.model['cameras'] if c['name']==guidance['reference']),None)
            if camera:
                from .viser_viewer import _read_image
                self.nav_next.image=_read_image(Path(camera['image_path']));self.nav_next.visible=True
        else:
            self.nav_next.visible=False
