from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from go1loc.local_motion import MotionCapabilities, LocalMotionAdapter, execution_gate
from go1loc.navigation_localizer import LatestFrameWorker
from go1loc.navigation_io import count_target_matches
from go1loc.navigation_io import CaptureReplay
from go1loc.jobs import atomic_json
import json


class ServiceTests(unittest.TestCase):
    def test_reencoded_mapping_pixels_are_not_independent_replay(self):
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve()
            row=dict(agent_read_monotonic=1.,agent_seq=1,session_id='s',
                image_sha256='new_encoded_hash',raw_payload_sha256='same_pixels')
            (root/'frames.jsonl').write_text(json.dumps(row)+'\n',encoding='utf-8')
            with patch('go1loc.web_capture.capture_metadata',return_value=(root,{
                    'stream_session_id':'s','source_kind':'orbbec_rgb_mjpeg'})):
                with self.assertRaisesRegex(ValueError,'同像素'):
                    CaptureReplay(root,map_image_hashes=['old_encoded_hash'],map_pixel_hashes=['same_pixels'])

    def test_mailbox_only_retains_latest_and_generation_fences_late_result(self):
        entered=threading.Event();release=threading.Event();calls=[]
        class Backend:
            def localize(self,photo,output,config):
                calls.append(photo.name);entered.set();release.wait(2)
                return {'pose_status':'localized'}, {}, output
        with TemporaryDirectory() as temp:
            w=LatestFrameWorker(Backend,Path(temp))
            try:
                w.submit(Path('old.png'),{'sequence':1});self.assertTrue(entered.wait(2))
                w.submit(Path('middle.png'),{'sequence':2});w.submit(Path('new.png'),{'sequence':3})
                self.assertEqual(w.dropped,1)
                w.reset();release.set()
                deadline=time.monotonic()+2
                while w.busy and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertIsNone(w.take());self.assertEqual(calls,['old.png'])
                w.submit(Path('current.png'),{'sequence':4})
                deadline=time.monotonic()+2;result=None
                while result is None and time.monotonic()<deadline:
                    time.sleep(.01);result=w.take()
                self.assertEqual(result['observation']['sequence'],4)
            finally:
                release.set();w.close();w.thread.join(2)

    def test_execution_gate_computed_but_no_transport_exists(self):
        caps=MotionCapabilities()
        args=dict(mode='shadow_guidance',live=True,metric_scale=True,camera_mount_verified=True,
            up_verified=True,current_authorization=True,fresh_observation=True,local_approach_verified=True,
            task_state='following')
        self.assertFalse(execution_gate(caps,**args)['execution_permitted'])
        caps=replace(caps,verified_velocity=True,verified_turning=True,state_feedback_fresh=True,
            single_command_arbiter_verified=True,avoidance_verified=True,avoidance_evidence=('test-fixture-only',),
            stops=True,front_range_verified=True,side_range_verified=True)
        args['mode']='supervised_execution'
        self.assertTrue(execution_gate(caps,**args)['execution_permitted'])
        self.assertFalse(execution_gate(caps,**{**args,'task_state':'paused'})['execution_permitted'])
        for key in ('live','metric_scale','camera_mount_verified','up_verified','current_authorization',
                    'fresh_observation','local_approach_verified'):
            self.assertFalse(execution_gate(caps,**{**args,key:False})['execution_permitted'])
        with self.assertRaisesRegex(RuntimeError,'No verified'):
            LocalMotionAdapter().submit({},execution_gate(caps,**args))

    def test_target_confirmation_counts_unique_image_points_not_ambiguous_duplicates(self):
        row=dict(query_keypoint_index=5,point3D_id=1,pnp_inlier=True,positive_depth=True,reprojection_px=1.)
        observation={'inlier_rows':[row,{**row,'point3D_id':2},{**row,'point3D_id':3}]}
        self.assertEqual(count_target_matches(observation,{'supporting_point3D_ids':[1,2,3]}),1)


if __name__=='__main__':
    unittest.main()
