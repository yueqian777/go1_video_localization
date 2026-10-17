"""UI lifecycle tests; actual Chrome demonstration is recorded separately."""
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
import test_viser_viewer as f
from go1loc.jobs import atomic_json
from test_navigation_junctions import junction_fixture
from go1loc.junctions import fuse_junctions
from go1loc.navigation import NavigationTask


class NavigationUITests(unittest.TestCase):
    setUp=f.ApplicationTests.setUp

    def test_start_prewarms_before_source_and_reuses_one_retired_backend(self):
        from test_navigation import setup_graph
        g=setup_graph();order=[]
        profile={'mounts':{'camera6':{}},'map_hash':g['map_hash']}
        backend=Mock();backend.map_dir=self.app.map_dir.resolve()
        backend.warmup.side_effect=lambda *args: order.append('warm') or {'completed_monotonic':10.,'ready':True}
        worker=Mock();worker.thread.join.side_effect=lambda:order.append('retired')
        def reader(*args):order.append('reader');return Mock()
        def thread(*,target,**kwargs):return SimpleNamespace(start=target)
        self.app.target_result={'status':'supported'};self.app.nav_upright.value=True
        with patch.object(self.app,'_nav_profile',return_value=profile), patch(
                'go1loc.navigation_ui.threading.Thread',side_effect=thread), patch(
                'go1loc.navigation_ui.prepare_index',return_value=g), patch(
                'go1loc.navigation_ui.ResidentLocalizer',return_value=backend) as factory, patch(
                'go1loc.navigation_ui.observation_goals',return_value=[]), patch(
                'go1loc.navigation_ui.LatestFrameWorker',return_value=worker) as workers, patch.object(
                self.app,'_nav_live_reader',side_effect=reader):
            self.app._nav_start(None)
            self.assertEqual(order,['warm','reader'])
            self.app._nav_source.next.assert_not_called()
            self.app._nav_start(None)
        self.assertEqual(order,['warm','reader','retired','warm','reader'])
        self.assertEqual(factory.call_count,1)
        self.assertEqual(workers.call_args.kwargs['not_before_monotonic'],10.)

    def test_warmup_failure_never_reads_current_frames_or_publishes_worker(self):
        from test_navigation import setup_graph
        g=setup_graph();profile={'mounts':{'camera6':{}},'map_hash':g['map_hash']}
        backend=Mock();backend.map_dir=self.app.map_dir.resolve();backend.warmup.side_effect=ValueError('warmup failed')
        def thread(*,target,**kwargs):return SimpleNamespace(start=target)
        with patch('go1loc.navigation_ui.threading.Thread',side_effect=thread), patch(
                'go1loc.navigation_ui.prepare_index',return_value=g), patch(
                'go1loc.navigation_ui.ResidentLocalizer',return_value=backend), patch.object(
                self.app,'_nav_live_reader') as reader:
            self.app._nav_launch(profile,start=True)
        reader.assert_not_called()
        self.assertIsNone(self.app._nav_worker)
        self.assertIn('warmup failed',self.app.nav_status.value)

    def test_off_route_pose_visible_but_old_route_retired_and_age_checked(self):
        from test_navigation import setup_graph, observation
        g=setup_graph();task=NavigationTask(g,[dict(node_id='m.png',quality=1.)],up=[0,0,1])
        self.app._nav_task=task
        o=observation(g,(20,20,20));o['time_mode']='live_current'
        task.observe(o,now=1)
        with patch('go1loc.navigation_ui.time.monotonic',return_value=1):
            self.app._nav_render()
        self.assertIn('navigation_current',self.app.controller.groups)
        self.assertNotIn('navigation_route',self.app.controller.groups)
        self.assertIn('当前位置已定位',self.app.nav_status.value)
        self.assertIn('最近候选',self.app.nav_detail.value)
        self.assertIn('历史路线未覆盖当前定位的接入范围',self.app.nav_detail.value)
        self.assertIn('不能直接吸附',self.app.nav_detail.value)
        with patch('go1loc.navigation_ui.time.monotonic',return_value=10):
            self.app._nav_render()
        self.assertNotIn('navigation_current',self.app.controller.groups)

    def test_stationary_notice_and_association_limits_are_visible_without_closing(self):
        from test_navigation import setup_graph, observation
        g=setup_graph();task=NavigationTask(g,[dict(node_id='m.png',quality=1.)],up=[1,0,0])
        for node in g['nodes'].values():node['image_path']='synthetic-test.png'
        self.app._nav_task=task
        for seq in (1,30):task.observe(observation(g,seq=seq),now=seq)
        with patch('go1loc.viser_viewer._read_image',return_value=f.np.zeros((2,2,3),dtype=f.np.uint8)):
            self.app._nav_render()
        self.assertIn('未自动封路',self.app.nav_detail.value)
        self.assertIn('正常距离范围',self.app.nav_detail.value)
        self.assertIn('门限',self.app.nav_detail.value)
        self.assertFalse(task.closed)

    def test_rejected_completion_keeps_its_own_capture_clock_for_diagnostics(self):
        from test_navigation import setup_graph, observation
        g=setup_graph();task=NavigationTask(g,[],up=[1,0,0])
        self.app._nav_task=task;self.app._nav_worker=Mock();self.app._nav_source=None
        self.app._nav_run=self.app.sidecar/'clock-log';self.app._nav_camera_at_start='camera6'
        self.app.target_result={'supporting_point3D_ids':[]}
        o=observation(g);o.update(time_mode='live_current',image_path='local-test.png')
        self.app._nav_worker.take.return_value=dict(error=None,observation=o,latency_s=5.,directory='query',
                                                  processing_stages={'match_s':2.1})
        with patch('go1loc.navigation_ui.time.monotonic',return_value=6.), patch(
                'go1loc.viser_viewer._read_image',return_value=f.np.zeros((2,2,3),dtype=f.np.uint8)):
            self.app._nav_tick()
        event=self.app._nav_events[-1]
        self.assertEqual(event['reason'],'stale_result')
        self.assertIsNone(event['sequence'])
        self.assertEqual(event['observation_metadata']['sequence'],1)
        self.assertEqual(event['observation_metadata']['received_monotonic'],1.)
        self.assertEqual(event['evaluated_monotonic'],6.)
        self.assertEqual(event['adoption'],'rejected')
        self.assertEqual(event['processing_stages'],{'match_s':2.1})
        self.assertEqual(event['adoption_reason'],'stale_result')
        self.assertEqual(event['timing']['ui_applied_monotonic'],6.)
        self.assertTrue((self.app._nav_run/'guidance_events.jsonl').is_file())

    def test_model_error_is_not_replaced_by_bad_photo_advice(self):
        from test_navigation import setup_graph
        task=NavigationTask(setup_graph(),[],up=[0,0,1])
        self.app._nav_task=task;self.app._nav_worker=Mock();self.app._nav_source=None
        self.app._nav_run=self.app.sidecar/'run'
        self.app._nav_worker.take.return_value={'error':'model initialization failed','latency_s':.1}
        self.app._nav_tick()
        self.assertIn('model initialization failed',self.app.nav_status.value)
        self.assertNotIn('更多环境细节',self.app.nav_status.value)
        self.assertNotIn('navigation_current',self.app.controller.groups)

    def test_stale_failed_pose_keeps_both_causes_and_does_not_suggest_dead_camera(self):
        from test_navigation import setup_graph, observation
        g=setup_graph();self.app._nav_task=NavigationTask(g,[],up=[1,0,0])
        self.app._nav_worker=Mock();self.app._nav_source=None
        self.app._nav_run=self.app.sidecar/'failed-pose';self.app._nav_camera_at_start='camera6'
        self.app.target_result={'supporting_point3D_ids':[]}
        o=observation(g);o.update(time_mode='live_current',image_path='local-test.png',
            pose_status='insufficient_evidence',T_WC_query=None,failure_reason='insufficient_pnp_quality',
            quality={'inlier_ratio':.10,'num_inliers':90,'acceptance_thresholds':{'min_inlier_ratio':.25}})
        self.app._nav_worker.take.return_value=dict(error=None,observation=o,latency_s=3.2,directory='query')
        with patch('go1loc.navigation_ui.time.monotonic',return_value=5.), patch(
                'go1loc.viser_viewer._read_image',return_value=f.np.zeros((2,2,3),dtype=f.np.uint8)):
            self.app._nav_tick()
        event=self.app._nav_events[-1]
        self.assertEqual(event['localization_diagnostic']['failure_reason'],'insufficient_pnp_quality')
        self.assertEqual(event['localization_diagnostic']['pose_status'],'insufficient_evidence')
        self.assertEqual(event['localization_diagnostic']['received_to_evaluation_s'],4.)
        self.assertIn('位姿质量不足',self.app.nav_status.value)
        self.assertIn('超时',self.app.nav_status.value)
        self.assertIn('不是已证实的相机断流',self.app.nav_status.value)
        self.assertIsNone(self.app._nav_task.guidance)
        self.assertNotIn('navigation_current',self.app.controller.groups)

    def test_default_ui_has_normal_controls_not_json_entry(self):
        labels=[x for x in self.server.gui.labels if isinstance(x,str)]
        for label in ('开始方向引导（不控制机器狗）','暂停引导','取消任务','上传单张当前位置照片'):
            self.assertIn(label,labels)
        self.assertFalse(self.app.research_mode.value)
        self.app._refresh_overview()
        self.assertIn('自动路线关联',self.app.overview.content)

    def test_reference_consensus_ambiguity_explains_no_pose_or_direction(self):
        from test_navigation import setup_graph, observation
        from go1loc.viewer_text import query_failure_notice
        graph=setup_graph();self.app._nav_task=NavigationTask(graph,[],up=[1,0,0])
        self.app._nav_worker=Mock();self.app._nav_source=None
        self.app._nav_run=self.app.sidecar/'ambiguous-pose';self.app._nav_camera_at_start='camera6'
        self.app.target_result={'supporting_point3D_ids':[]}
        obs=observation(graph);obs.update(pose_status='ambiguous',T_WC_query=None,image_path='test.png',
            failure_reason='ambiguous_reference_consensus',quality={})
        self.app._nav_worker.take.return_value=dict(error=None,observation=obs,latency_s=.1,directory='query')
        with patch('go1loc.navigation_ui.time.monotonic',return_value=1.1), patch(
                'go1loc.viser_viewer._read_image',return_value=f.np.zeros((2,2,3),dtype=f.np.uint8)):
            self.app._nav_tick()
        self.assertIn('多个参考图组',self.app.nav_status.value)
        self.assertIn('多个参考图组',query_failure_notice(obs))
        self.assertIsNone(self.app._nav_task.guidance)
        self.assertNotIn('navigation_current',self.app.controller.groups)

    def test_target_invalidation_stops_worker_and_removes_all_guidance(self):
        worker=Mock();self.app._nav_worker=worker;self.app._nav_task=Mock()
        generation=self.app._nav_generation
        self.app.controller.pose('navigation_current',f.np.eye(4),color=(1,2,3))
        self.app._invalidate_target(clear_query=True)
        worker.close.assert_called_once()
        self.assertIsNone(self.app._nav_task)
        self.assertEqual(self.app._nav_generation,generation+1)
        self.assertNotIn('navigation_current',self.app.controller.groups)

    def test_camera_source_changes_require_restart(self):
        self.app._nav_worker=Mock();self.app._nav_task=Mock()
        self.app.nav_camera.update(SimpleNamespace())
        self.assertIsNone(self.app._nav_task)

    def test_recording_or_current_photo_change_invalidates_task(self):
        for field in (self.app.nav_recording,self.app.current_photo):
            worker=Mock();self.app._nav_worker=worker;self.app._nav_task=Mock()
            field.update(SimpleNamespace())
            worker.close.assert_called_once()
            self.assertIsNone(self.app._nav_task)

    def test_live_frame_can_become_first_target_without_existing_task(self):
        self.app.nav_source_mode.value='已开启的本地相机'
        self.app.nav_camera.value='camera6'
        photo=self.app.sidecar/'fresh.png'
        def immediate_thread(*,target,**kwargs):
            return SimpleNamespace(start=target)
        with patch('go1loc.navigation_ui.LocalCaptureReader') as reader, patch(
                'go1loc.navigation_ui.threading.Thread',side_effect=immediate_thread):
            reader.return_value.next.return_value=(photo,{'camera_id':'camera6'})
            self.app._nav_current_as_target(None)
        self.assertEqual(self.app.photo.value,str(photo))
        self.assertIsNone(self.app._nav_task)
        self.assertIsNone(self.app.target_result)

    def test_pending_target_frame_cannot_replace_new_task_input(self):
        pending=[]
        def deferred_thread(*,target,**kwargs):
            pending.append(target)
            return SimpleNamespace(start=lambda:None)
        self.app.nav_source_mode.value='已开启的本地相机'
        with patch('go1loc.navigation_ui.LocalCaptureReader') as reader, patch(
                'go1loc.navigation_ui.threading.Thread',side_effect=deferred_thread):
            reader.return_value.next.return_value=(self.app.sidecar/'old.png',{'camera_id':'camera6'})
            self.app._nav_current_as_target(None)
            self.app._nav_invalidate()
            self.app.photo.value='new-target.png'
            pending[0]()
        self.assertEqual(self.app.photo.value,'new-target.png')

    def test_no_source_declaration_does_not_start_model_or_robot(self):
        with self.assertRaisesRegex(ValueError,'采集方式'):
            self.app._nav_start(None)
        self.assertIsNone(self.app._nav_worker)

    def test_junction_panel_lists_evidence_and_disables_both_transfers(self):
        g,obs=junction_fixture();g=fuse_junctions(g,obs)
        self.app._nav_graph=g
        self.app._nav_refresh_junctions()
        self.assertIn('1',self.app.nav_junction_summary.value)
        self.assertIn('共同三维特征',self.app.nav_junction_summary.value)
        self.app._nav_task=NavigationTask(g,[dict(node_id='E.png',quality=1.)],up=[0,-1,0])
        self.app._nav_worker=Mock();self.app._nav_render=Mock()
        self.app._nav_disable_junction(None)
        self.assertEqual(self.app._nav_task.closed,set(next(iter(g['junctions'].values()))['edge_ids']))
        self.assertIn(self.app._nav_task.state,('locating','blocked'))

    def test_target_load_does_not_discard_map_only_index_preparation(self):
        g,obs=junction_fixture();g=fuse_junctions(g,obs)
        self.app.controller.model['map_hash']=g['map_hash']
        profile={'mounts':{'camera6':{}},'map_hash':g['map_hash']}
        pending=[]
        def thread(*,target,**kwargs):
            pending.append(target);return SimpleNamespace(start=lambda:None)
        with patch.object(self.app,'_nav_profile',return_value=profile), patch(
                'go1loc.navigation_ui.threading.Thread',side_effect=thread), patch(
                'go1loc.navigation_ui.prepare_index',return_value=g):
            self.app._nav_prepare_only(None)
            self.app._invalidate_target(clear_query=True)
            pending[0]()
        self.assertEqual(self.app._nav_graph,g)
        self.assertIn('1',self.app.nav_junction_summary.value)

    def test_snapshot_cannot_display_final_map_junction(self):
        g,obs=junction_fixture();self.app._nav_graph=fuse_junctions(g,obs)
        self.app._nav_refresh_junctions()
        self.app.controller.mode='build_snapshot'
        with self.assertRaisesRegex(ValueError,'final_map_required'):
            self.app._nav_show_junction(None)
        self.assertFalse(self.app.nav_junction_a.visible)

    def test_pending_index_cannot_publish_after_viewer_closes(self):
        g,obs=junction_fixture();g=fuse_junctions(g,obs)
        profile={'mounts':{'camera6':{}},'map_hash':g['map_hash']}
        pending=[]
        def thread(*,target,**kwargs):
            pending.append(target);return SimpleNamespace(start=lambda:None)
        with patch.object(self.app,'_nav_profile',return_value=profile), patch(
                'go1loc.navigation_ui.threading.Thread',side_effect=thread), patch(
                'go1loc.navigation_ui.prepare_index',return_value=g), patch(
                'go1loc.navigation_ui.save_navigation_index') as save:
            self.app._nav_prepare_only(None)
            self.app.close()
            pending[0]()
            save.assert_not_called()
        self.assertIsNone(self.app._nav_graph)


if __name__=='__main__':
    unittest.main()
