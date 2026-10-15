"""Presentation contracts; no robot or real-video acceptance claims."""
import unittest
from types import SimpleNamespace
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from PIL import Image
import test_viser_viewer as f
from go1loc.jobs import atomic_json


class ChineseViewerTests(unittest.TestCase):
    setUp = f.ApplicationTests.setUp

    def test_three_dimensional_canvas_has_no_text_labels(self):
        from unittest.mock import patch
        import numpy as np
        with patch.object(self.server.scene, 'add_label') as labels:
            self.app.controller.replace_model(f.model())
            self.app._show_frame(self.event)
            self.app.controller.load_query(dict(map_id='m', map_hash='h', component_id='c0',
                pose_status='localized', T_WC_query=np.eye(4).tolist()))
            self.app.controller.load_plan(f.plan_fixture())
            self.app._draw_preview('review_candidate', [[0, 0, 0], [1, 0, 0]], 'legacy text', (1, 2, 3))
            labels.assert_not_called()
        self.assertIn('/model/points', self.server.scene.active)
        self.assertIn('/reference/camera', self.server.scene.active)
        self.assertIn('/review_candidate/path', self.server.scene.active)

    def test_guided_tabs_and_local_only_language(self):
        self.assertEqual(self.server.gui.tabs, ['① 地图', '② 找物', '③ 起点', '④ 路段', '⑤ 路线'])
        text = '\n'.join(str(x) for x in self.server.gui.labels)
        for expected in ['不会控制机器狗', '恢复地图总览', '只确认一部分', '没有点不代表没有障碍']:
            self.assertIn(expected, text)

    def test_chinese_error_action_keeps_raw_diagnostic(self):
        self.app._guard(lambda _: self.app._plan_route(None))(None)
        self.assertIn('起点', self.app.notice.value)
        self.assertIn('目标', self.app.notice.value)
        self.assertIn('route_requires_explicit', self.app.status.value)
        self.assertFalse(self.app.jobs.submitted[-1][0] == 'plan_route')

    def test_reference_photo_no_longer_teleports_camera_by_default(self):
        previous = deepcopy(self.client.camera.position)
        self.app._show_frame(self.event)
        self.assertEqual(list(self.client.camera.position), list(previous))
        self.assertTrue(self.app.reference_image.visible)

    def test_replacing_map_hides_stale_reference_photo(self):
        self.app.reference_image.visible = True
        payload = deepcopy(self.app.controller.model)
        atomic_json(self.root/'scene.json', payload)
        self.app._completed({'kind': 'inspect_map', 'result_path': str(self.root/'scene.json')})
        self.assertFalse(self.app.reference_image.visible)

    def test_chinese_dropdowns_keep_machine_protocol_values(self):
        self.app.mode.value = '建图过程快照'
        self.app.snapshot_dir = self.root/'snapshots'
        self.app._load_mode(None)
        self.assertEqual(self.app.jobs.submitted[-1][0], 'inspect_snapshot')
        self.app.time_mode.value = '历史照片离线回放'
        self.app.replay_time.value = '2026-10-14T12:00:00+08:00'
        self.assertEqual(self.app._planning_time()['time_mode'], 'offline_replay')

    def test_photo_upload_preserves_bytes_and_does_not_run_localization(self):
        from go1loc.viewer_text import save_uploaded_image
        data = BytesIO()
        Image.new('RGB', (10, 10), 'blue').save(data, format='PNG')
        raw = data.getvalue()
        path = save_uploaded_image(self.app.sidecar, '../outside.png', raw)
        self.assertTrue(path.is_relative_to(self.app.sidecar/'imports'))
        self.assertEqual(path.read_bytes(), raw)
        self.assertFalse((self.app.sidecar.parent/'outside.png').exists())
        with self.assertRaises(ValueError):
            save_uploaded_image(self.app.sidecar, 'bad.png', b'not an image')

    def test_viser_upload_event_uses_value_without_submitting_job(self):
        data = BytesIO()
        Image.new('RGB', (5, 5)).save(data, format='PNG')
        button = SimpleNamespace()
        button.on_upload = lambda callback: setattr(button, 'callback', callback)
        from unittest.mock import patch
        count = len(self.app.jobs.submitted)
        self.app.target_result = {'status': 'supported', 'obsolete': True}
        self.app.plan_result = {'obsolete': True}
        with patch.object(self.server.gui, 'add_upload_button', return_value=button):
            self.app._photo_upload('测试选图', self.app.photo)
        button.callback(SimpleNamespace(target=SimpleNamespace(value=SimpleNamespace(name='test.png', content=data.getvalue()))))
        self.assertEqual(Path(self.app.photo.value).read_bytes(), data.getvalue())
        self.assertEqual(len(self.app.jobs.submitted), count)
        self.assertIn('照片已复制', self.app.notice.value)
        self.assertIsNone(self.app.target_result)
        self.assertIsNone(self.app.plan_result)


if __name__ == '__main__':
    unittest.main()
