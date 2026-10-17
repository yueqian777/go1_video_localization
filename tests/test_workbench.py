"""Unified flow tests use a manual fake source, never SSH or a robot."""
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np
from PIL import Image

from go1loc.capture_control import CaptureController
from go1loc.workbench import LiveFrameReader, WorkbenchBridge, Workbench
from tests.test_capture_control import ManualSource
from tests.test_capture_session import frame
import test_viser_viewer as vf
import test_capture_web as capture_tests


def real_frame(seq=1, **kwargs):
    source=frame(seq, **kwargs)
    return replace(source,header={**source.header,'source_kind':'go1_shm_readonly'})


class LiveReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.controller=CaptureController();self.source=ManualSource()
        self.controller.connect(self.source);self.addCleanup(self.controller.disconnect)
        self.reader=LiveFrameReader(self.controller,Path(self.tmp.name))

    def test_exact_source_pixels_receive_time_and_duplicate_rejection(self):
        f=replace(real_frame(),preview_jpeg=b'not-source-pixels');self.source.on_frame(f)
        image,meta=self.reader.next()
        np.testing.assert_array_equal(np.asarray(Image.open(image)),f.rgb())
        self.assertEqual(meta['received_monotonic'],f.pc_receive_monotonic)
        self.assertEqual(meta['source_read_time'],f.header['agent_read_monotonic'])
        self.assertIsNone(meta['sensor_capture_time'])
        self.assertEqual(meta['time_mode'],'live_current')
        self.assertFalse(meta['execution_permitted'])
        self.assertIsNone(self.reader.next())

    def test_no_frame_stale_frozen_and_synthetic_are_not_live_localization(self):
        with self.assertRaisesRegex(ValueError,'新鲜'):self.reader.next()
        self.source.on_frame(replace(real_frame(),pc_receive_monotonic=time.monotonic()-10))
        with self.assertRaisesRegex(ValueError,'新鲜'):self.reader.next()
        self.source.on_frame(frame(2))
        with self.assertRaisesRegex(ValueError,'合成'):self.reader.next()

    def test_reader_does_not_connect_or_record_and_session_switch_requires_restart(self):
        self.assertIsNone(self.controller.recorder)
        self.source.on_frame(real_frame());_,a=self.reader.next()
        self.source.on_frame(real_frame(2,session='b'*32))
        with self.assertRaisesRegex(ValueError,'会话'):self.reader.check_available()
        _,b=LiveFrameReader(self.controller,self.tmp.name).next()
        self.assertNotEqual(a['session_id'],b['session_id'])
        self.assertEqual(b['camera_id'],'camera1')

    def test_frozen_source_rejected_without_fabricating_capture_time(self):
        self.source.on_frame(real_frame())
        with patch.object(self.controller.health,'view',return_value={'content_status':'suspected_frozen'}):
            with self.assertRaisesRegex(ValueError,'冻结'):self.reader.next()


class BridgeTests(unittest.TestCase):
    setUp=vf.ApplicationTests.setUp

    def bridge(self):
        self.server.get_port=lambda: 8781
        self.controller=CaptureController();self.source=ManualSource()
        self.controller.connect(self.source);self.addCleanup(self.controller.disconnect)
        self.capture=SimpleNamespace(controller=self.controller,output=self.root/'captures',mode='robot')
        return WorkbenchBridge(self.app,self.capture)

    def test_live_source_is_selected_without_starting_capture_or_navigation(self):
        bridge=self.bridge();self.source.on_frame(real_frame())
        bridge.action('navigation-live',{})
        self.assertEqual(self.app.nav_source_mode.value,'已开启的本地相机')
        self.assertEqual(self.app.nav_camera.value,'camera1')
        self.assertIsNone(self.app._nav_task)
        self.assertIsNone(self.controller.recorder)

    def test_recording_handoff_rejects_unfinished_and_uses_catalog_id(self):
        bridge=self.bridge()
        with patch.object(self.app,'select_capture_for_build') as select:
            with patch('go1loc.workbench.capture_metadata',side_effect=ValueError('capture_not_finalized')):
                with self.assertRaises(ValueError):bridge.action('mapping-recording',{'capture_id':'clip'})
            select.assert_not_called()
            with patch('go1loc.workbench.capture_metadata',return_value=(self.root,{})):
                bridge.action('mapping-recording',{'capture_id':'clip'})
            select.assert_called_once_with('clip')

    def test_summary_never_exposes_stale_pose_or_motion_permission(self):
        bridge=self.bridge()
        task=Mock(state='following',last={'pose_status':'localized'},guidance={'direction':'前进'})
        self.app._nav_task=task
        state=bridge.summary()
        self.assertFalse(state['current_pose_available'])
        self.assertFalse(state['execution_permitted'])

    def test_blank_viewer_starts_without_fake_frozen_map_or_inspection_job(self):
        with patch('go1loc.viser_viewer.JobManager',vf.Jobs), patch('go1loc.viser_viewer.threading.Thread'):
            app=vf.ViewerApplication(self.root/'empty-map',self.root/'new-ui',server=vf.Server(),load_initial=False)
            self.addCleanup(app.close)
            self.assertFalse((app.map_dir/'map_manifest.json').exists())
            self.assertFalse(app.jobs.list_jobs())

    def test_lost_live_frame_clears_old_pose_route_and_pending_result(self):
        bridge=self.bridge()
        from go1loc.navigation import NavigationTask
        from test_navigation_junctions import junction_fixture
        graph,_=junction_fixture()
        task=NavigationTask(graph,[],up=[0,-1,0])
        task.last=dict(time_mode='live_current',pose_status='localized',T_WC_query=np.eye(4).tolist(),
                       received_monotonic=time.monotonic())
        task.state='following';task.plan={'status':'planned','path':[[0,0,0],[1,0,0]],'goal_observation_pose':np.eye(4).tolist()}
        self.app._nav_task=task;self.app._nav_worker=Mock()
        self.app._nav_worker.take.return_value=None
        self.app._nav_source=LiveFrameReader(self.controller,self.root/'live')
        self.app.controller.pose('navigation_current',np.eye(4),color=(1,2,3))
        self.app._nav_tick()
        self.assertNotIn('navigation_current',self.app.controller.groups)
        self.assertNotIn('navigation_route',self.app.controller.groups)
        self.assertIsNone(task.guidance)
        self.app._nav_worker.reset.assert_called_once()


class WorkspaceHTTPTests(unittest.TestCase):
    setUp=capture_tests.WebTests.setUp
    tearDown=capture_tests.WebTests.tearDown
    request=capture_tests.WebTests.request
    action=capture_tests.WebTests.action

    def test_user_document_navigation_allowed_but_cross_site_api_still_denied(self):
        navigation={'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate',
                    'Sec-Fetch-Dest':'document','Sec-Fetch-User':'?1'}
        self.assertEqual(self.request('GET','/',headers=navigation)[0],200)
        self.assertEqual(self.request('GET','/api/v1/state',auth=True,headers=navigation)[0],403)
        self.assertEqual(self.request('GET','/',headers={**navigation,'Sec-Fetch-Dest':'iframe'})[0],403)
        self.assertEqual(self.request('GET','/',headers={**navigation,'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('POST','/api/v1/actions',{'action':'navigation-live'},auth=True,
                                      headers=navigation)[0],403)

    def test_workspace_api_uses_same_session_csrf_and_action_allowlist(self):
        self.assertEqual(self.request('GET','/api/v1/workspace')[0],403)
        self.assertEqual(json.loads(self.request('GET','/api/v1/workspace',auth=True)[2])['data'],{'enabled':False})
        workspace=Mock();workspace.viewer.server.get_port.return_value=8781
        workspace.summary.return_value={'enabled':True,'execution_permitted':False}
        self.app.workspace=workspace
        code,headers,body=self.request('GET','/')
        self.assertIn('frame-src http://127.0.0.1:8781;',headers['Content-Security-Policy'])
        self.assertIn('workspace-map',body.decode())
        self.assertEqual(self.request('POST','/api/v1/actions',{'action':'navigation-live'},auth=True,
                                      headers={'X-Go1-CSRF':'bad'})[0],403)
        workspace.action.assert_not_called()
        self.assertEqual(self.action('mapping-recording',capture_id='clip')[0],202)
        workspace.action.assert_called_once_with('mapping-recording',{'action':'mapping-recording','capture_id':'clip'})
        self.assertEqual(self.action('navigation-live',url='http://evil')[0],422)
        self.assertEqual(self.action('motion')[0],422)
        self.assertIsNone(self.app.controller.source)

    def test_source_selection_with_wrong_map_or_missing_frame_is_visible_error(self):
        self.app.workspace=Mock()
        self.app.workspace.action.side_effect=ValueError('请先加载地图并连接相机')
        self.assertEqual(self.action('navigation-live')[0],202)
        self.assertEqual(self.app.view()['action_error'],'请先加载地图并连接相机')


class WorkbenchStartupTests(unittest.TestCase):
    def test_blank_local_workbench_starts_two_uis_but_no_camera(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            with patch('go1loc.workbench.CAPTURE_ROOT',root/'captures'), patch(
                    'go1loc.web_capture.CAPTURE_ROOT',root/'captures'):
                app=Workbench(root/'not-needed-until-connect.json',root/'captures',port=0,workspace_dir=root/'ui')
                try:
                    app.start()
                    self.assertIsNone(app.controller.source)
                    self.assertIsNotNone(app.viewer.live_reader_factory)
                    self.assertTrue(app.workspace.summary()['enabled'])
                    self.assertFalse(app.workspace.summary()['map_ready'])
                    self.assertFalse((root/'ui'/'initial_map'/'map_manifest.json').exists())
                finally:app.close()

    def test_other_output_root_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError,'data/captures'):
                Workbench(Path(temp)/'config',temp,port=0)


if __name__=='__main__':unittest.main()
