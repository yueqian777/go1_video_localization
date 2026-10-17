"""Default Viser shadow-guidance workflow; strict research tools remain separate."""
from pathlib import Path
import json
import threading
import time
from uuid import uuid4
import numpy as np

from .jobs import atomic_json, read_json, utc_now
from .navigation import NavigationTask
from .navigation_io import (prepare_index, observation_goals, CaptureReplay, LocalCaptureReader,
                            count_target_matches, reference_pixel_hashes, save_navigation_index)
from .navigation_localizer import ResidentLocalizer, LatestFrameWorker

STATES={'locating':'正在定位','planned':'路线已规划','turning':'请按提示转向','following':'沿历史路线前进',
    'relocalizing':'需要重新观察','blocked':'路线受阻','observation_region_reached':'已到观察区，等待新画面确认目标',
    'target_confirmed':'新画面已复现目标几何证据','paused':'已暂停','cancelled':'已取消','failed':'任务失败'}
REASONS={'ambiguous_branch':'附近有多段相似路线，请换一个视角，再观察前方与两侧。',
    'off_route':'已测得相机位姿，但尚未满足历史路段的接入条件；不自动吸附，也不生成方向。',
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
REASONS['reverse_travel_not_enabled']='当前位置已定位，但目标在历史行进方向后方，当前图未允许这段反向通行。请核对一次性反向设置；不会自动新增反向边。'
REASONS['opposite_view_evidence_insufficient']='相机朝向与历史视角相反，缺少双端参考或正立证据，暂不能可靠关联；不是单纯距离偏离。'
REASONS['target_photo_reused']='目标原照片不能充当当前观测，请使用新采集画面。'
REASONS['nonmonotonic_observation']='观测源时间未递增，未采用该结果。'


class NavigationControls:
    def _navigation_ui(self):
        gui=self.server.gui
        self._nav_generation=0;self._nav_worker=None;self._nav_task=None;self._nav_source=None
        self._nav_graph=None;self._nav_preparing=False;self._nav_events=[];self._nav_replay_done=False
        self._nav_index_revision=0
        self._nav_error=None
        self._nav_diagnostic=None;self._nav_source_problem=False
        self._nav_backend=None;self._nav_setup_lock=threading.Lock();self._nav_retiring_workers=[]
        self._nav_render_key=None
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
            with gui.add_folder('路口融合与路线重组',expand_by_default=False):
                self.nav_junction_summary=gui.add_text('路口连接状态','加载地图后自动检查重复经过的位置。',multiline=True,disabled=True)
                self.nav_junction_choice=gui.add_dropdown('查看自动连接',['（暂无连接）'])
                self._button('查看这个连接的两张历史照片',self._nav_show_junction)
                self.nav_junction_a=gui.add_image(np.zeros((2,2,3),np.uint8),label='第一次经过',visible=False)
                self.nav_junction_b=gui.add_image(np.zeros((2,2,3),np.uint8),label='另一次经过',visible=False)
                self._button('本次任务停用此连接并重新规划',self._nav_disable_junction)
                gui.add_markdown('连接只在同一冻结地图内推断，不合并未对齐的地图。它不是新走廊或机器人通行保证；原始路段、方向与拐角保持不变。')
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
            self._nav_retiring_workers.append(self._nav_worker)
        self._nav_flush()
        self._nav_worker=None;self._nav_task=None;self._nav_source=None;self._nav_replay_done=False
        self._nav_diagnostic=None;self._nav_source_problem=False;self._nav_error=None
        self._nav_render_key=None
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
                    item=self._nav_live_reader(output).next()
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

    def _nav_live_reader(self, output):
        factory=getattr(self,'live_reader_factory',None) or LocalCaptureReader
        return factory(output)

    def _nav_map_loaded(self):
        self._nav_index_revision+=1
        self._nav_invalidate();self._nav_graph=None
        self._nav_refresh_junctions()
        if self.controller.mode!='final_map_replay':
            return
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
        self._nav_index_revision+=1
        self._nav_launch(profile,start=False)

    def _nav_start(self,_):
        profile=self._nav_profile()
        if not self.nav_upright.value:
            raise ValueError('请先确认所选相机朝前、画面正立；camera1 与 camera6 的设置不共用。')
        if not self.target_result:
            raise ValueError('请先上传目标照片，框选并计算；需获得目标支持证据。')
        self._nav_invalidate();self._nav_preparing=True
        self._nav_index_revision+=1
        self._nav_launch(profile,start=True)

    def _nav_launch(self,profile,*,start):
        generation=self._nav_generation;map_dir=self.map_dir;sidecar=self.sidecar/'navigation'
        index_revision=self._nav_index_revision
        target=self.target_result;mode=self.nav_source_mode.value;camera=self.nav_camera.value
        recording=self._nav_catalog.get(self.nav_recording.value);photo=self.current_photo.value
        from .jobs import _localization_config
        calibration=profile['mounts'][camera].get('calibration_path')
        config=_localization_config({'calibration_path':calibration} if calibration else {})
        self.nav_status.value='正在准备导航索引与常驻定位器…' if start else '正在生成导航索引…'
        def setup():
            worker=None
            try:
                graph=prepare_index(map_dir,sidecar,profile,publish=False)
                if start:
                    run=sidecar/('task-'+uuid4().hex)
                    # A single resident GPU backend per viewer. Retire old
                    # inference before reuse/replacement, including rapid restarts.
                    with self._nav_setup_lock:
                        for old in self._nav_retiring_workers:
                            old.thread.join()
                            old.backend=None;old.factory=None
                        self._nav_retiring_workers=[]
                        if generation!=self._nav_generation or self._stop.is_set():return
                        backend=self._nav_backend
                        if backend is None or backend.map_dir!=Path(map_dir).resolve():
                            self._nav_backend=None
                            backend=None
                            backend=ResidentLocalizer(map_dir)
                            self._nav_backend=backend
                        backend.verified_manifest(map_dir)
                        self.nav_status.value='正在预热同一套定位模型；完成后才读取新的当前画面…'
                        warmup=backend.warmup(run/'warmup',config)
                        backend.last_references=[]
                    atomic_json(run/'warmup.json',warmup)
                    atomic_json(run/'parameters.json',dict(profile=profile,max_age_s=3.,
                        no_progress_s=20.,entry_step_multiple=2.,tracking_step_multiple=2.5,
                        typical_step=graph['typical_step'],map_hash=graph['map_hash'],
                        localization_config=str(config),execution_permitted=False))
                    goals=observation_goals(backend.reconstruction,graph,target)
                    atomic_json(run/'goals.json',goals)
                    atomic_json(run/'navigation_index.json',graph)
                    if mode=='独立录像回放':
                        from .web_capture import capture_path
                        source=CaptureReplay(capture_path(recording),
                            map_image_hashes=backend.manifest['reference_image_sha256'].values(),
                            map_pixel_hashes=reference_pixel_hashes(backend.manifest))
                    elif mode=='已开启的本地相机':
                        source=self._nav_live_reader(run/'frames')
                    else:
                        source=None
                        if not Path(photo).is_file():
                            raise ValueError('请上传当前位置照片；拍摄时间未知时仅作为离线观测。')
                    task=NavigationTask(graph,goals,up=profile.get('up_override') or graph['up_estimate'],
                        lookahead_steps=profile.get('lookahead_steps',3.))
                    worker=LatestFrameWorker(lambda:backend,run/'queries',
                        not_before_monotonic=warmup['completed_monotonic'])
                with self._state_lock:
                    if (self._stop.is_set() or index_revision!=self._nav_index_revision or map_dir!=self.map_dir
                            or (start and generation!=self._nav_generation)):
                        if worker: worker.close()
                        return
                    save_navigation_index(sidecar,map_dir,graph)
                    self._nav_graph=graph;self._nav_preparing=False
                    self._nav_refresh_junctions()
                    self.nav_exceptions.value=f"自动连接 {len(graph['edges'])} 条；粗导航点 {len(graph['navigation_nodes'])} 个。\n"+('\n'.join(
                        f"{r['from_frame']} → {r['to_frame']}："+('已由加密原视频观测补足（原帧仍未注册）' if r.get('resolved_by')
                        else ', '.join(r['reasons'])) for r in graph['exceptions']) or '未检测到断点。')
                    if not start:
                        self.nav_status.value='导航索引已准备好，不需要逐段点确认。';return
                    self._nav_worker=worker;self._nav_task=task;self._nav_source=source
                    self._nav_config=config;self._nav_camera_at_start=camera;self._nav_run=run;self._nav_events=[]
                    self.nav_status.value='预热完成，等待预热之后的新画面定位。'
                    if source is None:
                        from .video import sha256_file
                        worker.submit(Path(photo),dict(sequence=1,session_id=uuid4().hex,camera_id=camera,
                            query_sha256=sha256_file(Path(photo)),time_mode='offline_replay',image_time_s=0.,
                            sensor_capture_time=None,received_monotonic=time.monotonic(),runtime_at=utc_now(),
                            time_source='single_offline_photo_capture_time_unknown'),config)
            except Exception as exc:
                if worker: worker.close()
                with self._state_lock:
                    if (not self._stop.is_set() and index_revision==self._nav_index_revision
                            and (not start or generation==self._nav_generation)):
                        self._nav_preparing=False;self.nav_status.value='无法开始：'+str(exc)
        threading.Thread(target=setup,daemon=True,name='navigation-setup').start()

    def _nav_pause(self):
        if self._nav_task:
            self._nav_task.pause()
            self._nav_worker.reset()
            self._nav_render()
            self._nav_flush()

    def _nav_resume(self):
        if self._nav_task:
            self._nav_task.resume();self._nav_render()

    def _nav_cancel(self):
        if self._nav_task:
            self._nav_task.cancel();self._nav_worker.reset();self._nav_source=None;self._nav_render()
            self._nav_flush()

    def _nav_append(self,name,row):
        run=getattr(self,'_nav_run',None)
        if run is not None:
            run.mkdir(parents=True,exist_ok=True)
            with (run/name).open('a',encoding='utf-8') as f:
                f.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')

    def _nav_flush(self):
        # Compatibility snapshot on pause/end; do not rewrite a growing 24MB
        # JSON array in every frame's UI critical path. JSONL is authoritative live.
        if getattr(self,'_nav_run',None) is not None and self._nav_events:
            atomic_json(self._nav_run/'guidance_log.json',self._nav_events)

    def _nav_close_current(self,_):
        if self._nav_task and (self._nav_task.association or {}).get('status')=='associated':
            self._nav_task.close_edge(self._nav_task.association['edge_id'])
            self._nav_render()

    def _nav_refresh_junctions(self):
        self.nav_junction_a.visible=False;self.nav_junction_b.visible=False
        self.controller.clear('navigation_junction')
        graph=self._nav_graph or {}
        self._nav_junction_labels={f"{i+1} · {j['frame_names'][0]} ↔ {j['frame_names'][1]}":jid
            for i,(jid,j) in enumerate(graph.get('junctions',{}).items())}
        self.nav_junction_choice.options=list(self._nav_junction_labels) or ['（暂无连接）']
        self.nav_junction_choice.value=self.nav_junction_choice.options[0]
        candidates=graph.get('junction_candidates',[])
        rejected=sum(j['status']=='rejected' for j in candidates)
        records=graph.get('junctions',{})
        stationary=sum(j.get('repeat_kind')=='stationary_observation' for j in records.values())
        self.nav_junction_summary.value=(f'自动采用 {len(records)} 处重复位置连接；{rejected} 处近邻候选证据不足。\n'
            f'其中 {stationary} 处属于重复停留观察，不能当作复杂路口验收。\n'
            '依据位置、高度、共同三维特征及前后帧；无需逐段人工确认。\n'+
            '\n'.join(f"{j['frame_names'][0]} ↔ {j['frame_names'][1]}：{j['shared_track_count']} 个共同三维特征"
                      for j in list(records.values())[:8]))

    def _nav_show_junction(self,_):
        self._require_final()
        jid=self._nav_junction_labels.get(self.nav_junction_choice.value)
        if not jid or not self._nav_graph:
            raise ValueError('本地图暂未找到证据充分的重复位置连接；不会仅凭距离近连接。')
        if self._nav_graph['map_hash']!=self.controller.model['map_hash']:
            raise ValueError('地图已改变，请重新生成本地图的路口连接。')
        from .viser_viewer import _read_image
        j=self._nav_graph['junctions'][jid]
        cameras={c['name']:c for c in self.controller.model['cameras']}
        for name,handle in zip(j['frame_names'],(self.nav_junction_a,self.nav_junction_b)):
            handle.image=_read_image(Path(cameras[name]['image_path']));handle.visible=True
        path=[self._nav_graph['nodes'][n]['T_MC'] for n in j['frame_names']]
        self._draw_preview('navigation_junction',[np.asarray(T)[:3,3].tolist() for T in path],'',(190,70,210))
        self._message(f"这两次经过的位置被自动关联；有 {j['shared_track_count']} 个共同三维特征。仅用于路线重组预览，不控制机器狗。")

    def _nav_disable_junction(self,_):
        jid=self._nav_junction_labels.get(self.nav_junction_choice.value)
        if not self._nav_task or not jid:
            raise ValueError('请先开始引导并选择连接；停用只影响本次任务，不改写原地图。')
        for eid in self._nav_task.graph['junctions'][jid]['edge_ids']:
            self._nav_task.close_edge(eid)
        self._nav_worker.reset();self._nav_task.resume()
        self._nav_render()
        self._message('本次任务已停用此处的双向连接，等待新画面重新规划；没有替代路线会明确显示无路。')

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
        if hasattr(self._nav_source,'check_available') and task.state not in ('paused','cancelled'):
            try:
                frame=self._nav_source.check_available()
                if task.last and task.last.get('session_id') and (
                        task.last['session_id']!=frame.header['session_id'] or
                        task.last['camera_id']!='camera'+str(frame.header['camera_id'])):
                    raise ValueError('采集会话改变，请重新开始引导。')
            except ValueError:
                self._nav_source_problem=True
                worker.reset();task.state='relocalizing';task.guidance=None;task.plan=None
                task.clear_tracking()
                task.reason='frame_stale_or_disconnected'
                self.nav_current.visible=False
                self._nav_render()
                return
            self._nav_source_problem=False
        completed=worker.take()
        event=None
        if completed:
            if completed['error']:
                self._nav_diagnostic=None
                task.state='relocalizing';task.guidance=None;task.plan=None;task.reason='localization_required'
                task.clear_tracking()
                self._nav_error=completed['error']
            else:
                self._nav_error=None
                observation=completed['observation']
                quality=observation.get('quality') or {}
                self._nav_diagnostic=dict(sequence=observation['sequence'],
                    pose_status=observation.get('pose_status'),failure_reason=observation.get('failure_reason'),
                    received_to_evaluation_s=time.monotonic()-observation['received_monotonic'],
                    quality={k:quality.get(k) for k in ('num_inliers','inlier_ratio',
                        'mean_inlier_reprojection_px','acceptance_thresholds','query_intrinsics','query_camera')})
                if observation['camera_id']!=self._nav_camera_at_start:
                    self._nav_pause();self.nav_status.value='输入相机与所选安装配置不同，请选择对应相机重新开始。';return
                observation['target_matches']=count_target_matches(observation,self.target_result)
                task.observe(observation,now=time.monotonic());task.confirm(now=time.monotonic())
                from .viser_viewer import _read_image
                self.nav_current.image=_read_image(Path(observation['image_path']));self.nav_current.visible=True
                self._nav_latency=completed['latency_s']
            # Retain source clocks even for stale/rejected results. A task's last
            # accepted sequence alone cannot reconstruct inference freshness.
            metadata=completed.get('observation') or completed.get('metadata') or {}
            event={**task.snapshot(),'at':utc_now(),'latency_s':completed['latency_s'],
                'observation_metadata':{k:metadata.get(k) for k in ('sequence','session_id','camera_id',
                    'time_mode','image_time_s','received_monotonic','runtime_at','time_source','sensor_capture_time')},
                'evaluated_monotonic':time.monotonic(),
                'query_directory':completed.get('directory'),'error':completed['error'],
                'localization_diagnostic':self._nav_diagnostic,
                'timing':dict(completed.get('timing') or {})}
        task.tick(time.monotonic())
        self._nav_render()
        if event:
            adopted=time.monotonic()
            valid=task.current_pose_available(adopted)
            metadata=completed.get('observation') or completed.get('metadata') or {}
            accepted=bool(valid and task.last and task.last['sequence']==metadata.get('sequence'))
            event.update(task.snapshot())
            event.update(adoption='accepted' if accepted else 'rejected',
                adoption_reason=('guidance' if task.guidance else task.reason or task.state) if accepted
                    else completed.get('error') or task.reason or task.state)
            event['timing'].update(ui_applied_monotonic=adopted,
                source_received_monotonic=metadata.get('received_monotonic'),
                ui_clock_scope='Viser_server_scene_state_not_browser_paint')
            self._nav_events.append(event)
            self._nav_append('guidance_events.jsonl',event)
        if self._nav_source and task.state not in ('paused','cancelled','target_confirmed'):
            live=isinstance(self._nav_source,LocalCaptureReader) or getattr(self._nav_source,'is_live',False)
            # Fetch only when the single worker can consume it. A queued PNG
            # otherwise spends an extra inference interval aging before use.
            if not worker.busy and worker.pending is None:
                try:
                    item=self._nav_source.next()
                    if item:
                        worker.submit(*item,self._nav_config)
                    elif not live:
                        self._nav_replay_done=True;self._nav_source=None
                        task.pause();task.reason='replay_ended_without_fabricated_arrival'
                except Exception as exc:
                    self._nav_source_problem=True
                    task.state='relocalizing';task.guidance=None;task.reason='frame_stale_or_disconnected'
                    task.plan=None;task.clear_tracking()
                    self.nav_status.value='采集不可用：'+str(exc)
        self._nav_render()

    def _nav_render(self):
        task=self._nav_task
        if task is None:
            return
        now=time.monotonic()
        current=task.current_pose_available(now)
        key=(task.last.get('sequence') if task.last else None, task.state, task.reason,
             current,tuple(sorted(task.closed)), task.progress_notice,
             (self._nav_diagnostic or {}).get('sequence'),self._nav_source_problem)
        if key==self._nav_render_key:return
        self._nav_render_key=key
        prefix='独立录像/照片回放 · ' if task.last and task.last['time_mode']=='offline_replay' else '本地当前画面 · '
        self.nav_status.value=prefix+STATES.get(task.state,task.state)+'\n'+REASONS.get(task.reason,task.reason or '')
        if current:
            route_status='路线尚未接入' if task.state=='relocalizing' else STATES.get(task.state,task.state)
            self.nav_status.value=prefix+'当前位置已定位 · '+route_status+'\n'+REASONS.get(task.reason,task.reason or '')
        elif task.reason=='localization_required' and self._nav_error:
            self.nav_status.value=prefix+'定位计算失败：'+self._nav_error+'\n请检查启动日志；这不是已证实的画面质量问题。'
        diagnostic=self._nav_diagnostic
        if (diagnostic and not self._nav_source_problem and task.state=='relocalizing'
                and task.reason in ('localization_required','stale_result','frame_stale_or_disconnected')
                and diagnostic['pose_status']!='localized'):
            failure=diagnostic.get('failure_reason') or diagnostic['pose_status']
            detail='位姿质量不足' if failure=='insufficient_pnp_quality' else '未取得合格位姿：'+str(failure)
            if failure=='ambiguous_reference_consensus':
                detail='多个参考图组给出互相冲突的位置，暂不采用位姿'
            slow=diagnostic['received_to_evaluation_s']>task.max_age_s
            self.nav_status.value=prefix+'本次照片定位失败：'+detail+('；计算结果同时超时。' if slow else '。')
            self.nav_status.value+='\n这是已收到图像的计算结果，不是已证实的相机断流；未生成方向。'
        if self._nav_replay_done:
            self.nav_status.value+='\n没有生成机器人运动。'
        guidance=task.guidance
        self.nav_detail.value=(f"{guidance['direction']} · 朝向误差 {guidance['yaw_error_degrees']:.0f}°\n"
            f"剩余参考路线 {guidance['remaining']:.2f} {guidance['units']}\n"
            f"下一观察图：{guidance['reference']}\n"
            f"定位计算 {getattr(self,'_nav_latency',0):.2f} 秒；以实际新图推进，不按时间动画推进。"
            if guidance else '当前无有效方向指令。影子引导不会控制机器狗。')
        if task.plan and task.plan.get('recombined'):
            self.nav_detail.value+=f"\n已重组历史路段：经过 {len(task.plan['junction_transitions'])} 处自动连接，不是原录像整段重放。"
        if current and task.progress_notice:
            self.nav_detail.value+='\n未观察到前进（可能正在静止测试）；未自动封路。确认障碍后可手动封路。'
        association=task.association or {}
        if current and association.get('status')=='associated':
            self.nav_detail.value+=('\n接入：'+('有界跟踪缓冲' if association.get('association_mode')=='bounded_tracking'
                else '正常距离范围')+f" · {association['edge_id']} · 距离 {association['distance']:.3f} / "
                f"门限 {association['distance_limit']:.3f} {task.graph['units']}")
        elif current and association.get('rejected_candidates'):
            nearest=association['rejected_candidates'][0]
            reasons={'distance':'超出距离门限','reference_support':'缺少端点参考证据','heading':'朝向不符'}
            self.nav_detail.value+=(f"\n最近候选 {nearest['edge_id']}：距离 {nearest['distance']:.3f} / "
                f"门限 {nearest['distance_limit']:.3f} {task.graph['units']}；"+
                '、'.join(reasons[r] for r in nearest['rejection_reasons']))
        if current:
            self.controller.pose('navigation_current',task.last['T_WC_query'],color=(255,130,0))
        else:
            self.controller.clear('navigation_current');self.nav_current.visible=False
        if current and task.state not in ('relocalizing','blocked') and task.plan and task.plan['status']=='planned':
            self._draw_preview('navigation_route',task.plan['path'],'',(0,190,220))
            self.controller.pose('navigation_goal',task.plan['goal_observation_pose'],color=(45,210,70))
        else:
            self.controller.clear('navigation_route');self.controller.clear('navigation_goal')
            if current and task.plan and task.plan['status']=='observation_region':
                self.controller.pose('navigation_goal',task.plan['goal_observation_pose'],color=(45,210,70))
        if guidance:
            camera=next((c for c in self.controller.model['cameras'] if c['name']==guidance['reference']),None)
            if camera is None:
                camera=task.graph['nodes'].get(guidance['reference'])
            if camera:
                from .viser_viewer import _read_image
                self.nav_next.image=_read_image(Path(camera['image_path']));self.nav_next.visible=True
            else:
                self.nav_next.visible=False
        else:
            self.nav_next.visible=False
        self._nav_append('ui_states.jsonl',dict(at=utc_now(),ui_applied_monotonic=time.monotonic(),
            state=task.state,reason=task.reason,current_pose_available=current,
            guidance_available=bool(current and task.guidance),
            direction=task.guidance['direction'] if current and task.guidance else None,
            sequence=task.last.get('sequence') if task.last else None,
            source_received_monotonic=task.last.get('received_monotonic') if task.last else None,
            ui_clock_scope='Viser_server_scene_state_not_browser_paint',execution_permitted=False))
