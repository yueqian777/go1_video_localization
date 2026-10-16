"""UI lifecycle tests; actual Chrome demonstration is recorded separately."""
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
import test_viser_viewer as f
from go1loc.jobs import atomic_json


class NavigationUITests(unittest.TestCase):
    setUp=f.ApplicationTests.setUp

    def test_default_ui_has_normal_controls_not_json_entry(self):
        labels=[x for x in self.server.gui.labels if isinstance(x,str)]
        for label in ('开始方向引导（不控制机器狗）','暂停引导','取消任务','上传单张当前位置照片'):
            self.assertIn(label,labels)
        self.assertFalse(self.app.research_mode.value)
        self.app._refresh_overview()
        self.assertIn('自动路线关联',self.app.overview.content)

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


if __name__=='__main__':
    unittest.main()
