"""First-build monitoring: synthetic native models, never video/route acceptance."""
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from go1loc.events import BoundedEventWriter
from go1loc.snapshots import SnapshotPublisher, SnapshotError, list_ready_snapshots


def put_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


class BuildWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.run_dir = self.root / 'sidecar' / 'run-1'
        self.images = self.root / 'images'
        self.images.mkdir()
        self.output = self.root / 'map'

    def create_run(self):
        from go1loc.build_watch import publish_run
        self.run_dir.mkdir(parents=True)
        events = BoundedEventWriter(self.run_dir/'events.jsonl', run_id=self.run_dir.name, map_id='test-map')
        self.run = dict(schema_version='go1loc.build_run.v1', run_id=events.run_id,
                        map_id=events.map_id, image_dir=str(self.images), output_dir=str(self.output),
                        events_path=str(events.path), scale_status='arbitrary_monocular_scale',
                        units='map_units', status='running', stage='initializing', failure_reason=None,
                        created_at='2026-10-13T00:00:00+00:00', execution_permitted=False)
        publish_run(self.run_dir, self.run)
        return events

    def publish_snapshot(self, events, attempt=1):
        import pycolmap
        native = self.root / ('native-' + str(attempt))
        native.mkdir()
        options = pycolmap.SyntheticDatasetOptions()
        options.num_rigs = 1
        options.num_cameras_per_rig = 1
        options.num_frames_per_rig = 3
        options.num_points3D = 20
        model = pycolmap.synthesize_dataset(options)
        model.write(native)
        self.model = model
        if not hasattr(self, 'publisher'):
            self.publisher = SnapshotPublisher(self.run_dir/'snapshots', events=events)
        times = {im.name: {'pts_time_s': float(i), 'pts': i*10, 'relative_time_s': float(i)}
                 for i, im in enumerate(model.images.values())}
        return self.publisher.publish(native, f'test-map:build-attempt:{attempt}', frame_times=times)

    def rewrite_snapshot(self, row, **changes):
        row = dict(row, **changes)
        unsigned = {k: v for k, v in row.items() if k != 'snapshot_sha256'}
        row['snapshot_sha256'] = hashlib.sha256(json.dumps(unsigned, sort_keys=True,
                       ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()
        marker = Path(row['snapshot_path'])/'READY.json'
        marker.chmod(stat.S_IWRITE | stat.S_IREAD)
        put_json(marker, row)

    def test_waits_for_metadata_and_first_snapshot_without_frozen_map(self):
        from go1loc.build_watch import read_build_state, snapshot_scene_payload
        waiting = read_build_state(self.run_dir)
        self.assertEqual(waiting['status'], 'waiting')
        self.assertIsNone(waiting['run'])
        self.assertEqual(waiting['snapshots'], [])
        self.create_run()
        state = read_build_state(self.run_dir)
        self.assertEqual(state['status'], 'running')
        self.assertEqual(state['stage'], 'initializing')
        self.assertIsNone(state['final_map_dir'])
        with self.assertRaisesRegex(ValueError, 'no_ready_snapshot'):
            snapshot_scene_payload(self.run_dir)
        self.assertFalse(self.output.exists())

    def test_native_scene_without_frozen_manifest_and_attempt_replacement(self):
        from go1loc.build_watch import read_build_state, snapshot_scene_payload
        events = self.create_run()
        first = self.publish_snapshot(events)
        second = self.publish_snapshot(events, attempt=2)
        state = read_build_state(self.run_dir)
        self.assertEqual(len(state['snapshots']), 2)
        self.assertEqual(state['stage'], 'reconstruction')
        for index, row in [(0, first), (-1, second)]:
            scene = snapshot_scene_payload(self.run_dir, snapshot_index=index, point_budget=5)
            self.assertEqual(scene['schema_version'], 'go1loc.viewer_scene.v1')
            self.assertEqual(scene['mode'], 'build_snapshot')
            self.assertEqual(scene['run_id'], 'run-1')
            self.assertEqual(scene['component_id'], row['component_id'])
            self.assertEqual(scene['revision'], row['snapshot_index'])
            self.assertEqual(len(scene['cameras']), 3)
            self.assertEqual(len(scene['points']), 5)
            self.assertEqual(scene['total_points'], 20)
            self.assertTrue(scene['pts_available'])
            self.assertEqual(len(scene['cameras'][0]['T_WC']), 4)
            self.assertTrue(Path(scene['cameras'][0]['image_path']).is_relative_to(self.images))
            self.assertIsNone(scene['map_hash'])
            self.assertFalse(scene['execution_permitted'])
        self.assertFalse((self.output/'map_manifest.json').exists())

    def test_truncated_event_ignored_failure_and_cancel_preserve_snapshots(self):
        from go1loc.build_watch import read_build_state, publish_run
        events = self.create_run()
        self.publish_snapshot(events)
        events.emit('started', stage='matching')
        with events.path.open('ab') as stream:
            stream.write(b'{"truncated":')
        self.assertEqual(read_build_state(self.run_dir)['stage'], 'matching')
        for terminal in ('failed', 'cancelled'):
            publish_run(self.run_dir, dict(self.run, status=terminal, failure_reason='fixture stopped'))
            state = read_build_state(self.run_dir)
            self.assertEqual(state['status'], terminal)
            self.assertEqual(state['failure_reason'], 'fixture stopped')
            self.assertEqual(len(state['snapshots']), 1)
            self.assertIsNone(state['final_map_dir'])

    def test_event_failure_when_metadata_is_still_running(self):
        from go1loc.build_watch import read_build_state
        events = self.create_run()
        events.emit('failed', stage='matching', data={'error': 'backend error'})
        state = read_build_state(self.run_dir)
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['failure_reason'], 'backend error')

    def test_only_validated_completed_final_map_is_returned(self):
        from go1loc.build_watch import read_build_state, publish_run
        from go1loc.evidence import digest_json
        events = self.create_run()
        snap = self.publish_snapshot(events)
        self.output.mkdir()
        # Native synthetic test model is genuine; never claim it is real video.
        manifest = dict(schema_version='go1loc.map.v1', frozen=True, map_id='test-map',
                        build_run_id='run-1', image_dir=str(self.images),
                        model_dir=snap['snapshot_path'], native_model_sha256=snap['model_sha256'])
        manifest['manifest_sha256'] = digest_json(manifest)
        put_json(self.output/'map_manifest.json', manifest)
        self.assertIsNone(read_build_state(self.run_dir)['final_map_dir'])
        events.emit('final', stage='mapping')
        state = read_build_state(self.run_dir)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['final_map_dir'], str(self.output))
        publish_run(self.run_dir, dict(self.run, status='completed'))
        manifest['map_id'] = 'other-map'
        manifest['manifest_sha256'] = digest_json({k: v for k, v in manifest.items() if k != 'manifest_sha256'})
        put_json(self.output/'map_manifest.json', manifest)
        invalid = read_build_state(self.run_dir)
        self.assertIsNone(invalid['final_map_dir'])
        self.assertEqual(invalid['status'], 'failed')
        self.assertIn('final_map', invalid['failure_reason'])

    def test_partial_snapshots_not_parsed_and_missing_native_rejected(self):
        from go1loc.build_watch import read_build_state, snapshot_scene_payload
        events = self.create_run()
        row = self.publish_snapshot(events)
        (self.run_dir/'snapshots'/'.pending-unfinished').mkdir()
        (self.run_dir/'snapshots'/'snapshot-999999').mkdir()
        self.assertEqual(len(read_build_state(self.run_dir)['snapshots']), 1)
        bad = Path(row['snapshot_path'])/'images.bin'
        bad.chmod(stat.S_IWRITE | stat.S_IREAD)
        bad.unlink()
        with self.assertRaises(SnapshotError):
            snapshot_scene_payload(self.run_dir)

    def test_native_tampering_run_mismatch_and_wrong_attempt_rejected(self):
        from go1loc.build_watch import snapshot_scene_payload
        events = self.create_run()
        row = self.publish_snapshot(events)
        for changes in ({'run_id': 'different-run'}, {'map_id': 'different-map'},
                        {'component_id': 'test-map:component:0'}):
            self.rewrite_snapshot(row, **changes)
            with self.assertRaisesRegex(ValueError, 'snapshot_.*mismatch|snapshot_component'):
                snapshot_scene_payload(self.run_dir)
        self.rewrite_snapshot(row)
        target = Path(row['snapshot_path'])/'images.bin'
        target.chmod(stat.S_IWRITE | stat.S_IREAD)
        target.write_bytes(b'tampered native data')
        with self.assertRaises(SnapshotError):
            snapshot_scene_payload(self.run_dir)

    def test_metadata_checksum_and_event_identity_are_enforced(self):
        from go1loc.build_watch import read_build_state
        events = self.create_run()
        events.emit('started', stage='mapping')
        event = json.loads(events.path.read_text())
        event['run_id'] = 'wrong'
        events.path.write_text(json.dumps(event)+'\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'event_run_mismatch'):
            read_build_state(self.run_dir)
        events.path.write_text('', encoding='utf-8')
        metadata = json.loads((self.run_dir/'run.json').read_text())
        metadata['map_id'] = 'tampered'
        put_json(self.run_dir/'run.json', metadata)
        with self.assertRaisesRegex(ValueError, 'run_metadata_hash'):
            read_build_state(self.run_dir)

    def test_metadata_available_before_backend_and_terminal_failure_persisted(self):
        from go1loc.build_watch import read_build_state
        from go1loc.mapping import build_map, MappingConfig
        for name in ('a.png', 'b.png'):
            (self.images/name).write_bytes(b'fixture')
        def backend():
            runs = list((self.root/'map.sidecar').iterdir())
            state = read_build_state(runs[0])
            self.assertEqual(state['status'], 'running')
            self.assertEqual(state['run']['image_dir'], str(self.images))
            self.assertEqual(state['run']['output_dir'], str(self.output))
            raise RuntimeError('backend unavailable fixture')
        with patch('go1loc.mapping._ascii_stage', return_value=None), \
             patch('go1loc.mapping._require_backend', side_effect=backend):
            with self.assertRaisesRegex(RuntimeError, 'backend unavailable fixture'):
                build_map(self.images, self.output, MappingConfig(map_id='test-map'))
        state = read_build_state(next((self.root/'map.sidecar').iterdir()))
        self.assertEqual(state['run']['status'], 'failed')
        self.assertIn('backend unavailable fixture', state['failure_reason'])

    def test_keyboard_interrupt_persists_cancel_and_injected_event_path(self):
        from go1loc.build_watch import read_build_state
        from go1loc.mapping import build_map, MappingConfig
        for name in ('a.png', 'b.png'):
            (self.images/name).write_bytes(b'fixture')
        events = BoundedEventWriter(self.root/'external-events.jsonl', run_id='injected-run')
        with patch('go1loc.mapping._ascii_stage', return_value=None), \
             patch('go1loc.mapping._require_backend', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                build_map(self.images, self.output, MappingConfig(map_id='test-map'), events=events)
        state = read_build_state(self.root/'map.sidecar'/'injected-run')
        self.assertEqual(state['status'], 'cancelled')
        self.assertEqual(state['run']['status'], 'cancelled')
        self.assertEqual(state['events'][-1]['event'], 'cancelled')
        self.assertEqual(state['run']['events_path'], str(events.path))

    def test_completed_metadata_checks_the_declared_final_hash(self):
        from go1loc.build_watch import read_build_state, publish_run
        from go1loc.evidence import digest_json
        events = self.create_run()
        snap = self.publish_snapshot(events)
        manifest = dict(frozen=True, map_id='test-map', build_run_id='run-1',
                        image_dir=str(self.images), model_dir=snap['snapshot_path'],
                        native_model_sha256=snap['model_sha256'])
        manifest['manifest_sha256'] = digest_json(manifest)
        put_json(self.output/'map_manifest.json', manifest)
        publish_run(self.run_dir, dict(self.run, status='completed', final_manifest_sha256='wrong-version'))
        state = read_build_state(self.run_dir)
        self.assertEqual(state['status'], 'failed')
        self.assertIsNone(state['final_map_dir'])
        self.assertIn('final_map', state['failure_reason'])

    def test_snapshot_metadata_hash_and_legacy_reader_compatibility(self):
        from go1loc.build_watch import snapshot_scene_payload
        events = self.create_run()
        row = self.publish_snapshot(events)
        marker = Path(row['snapshot_path'])/'READY.json'
        marker.chmod(stat.S_IWRITE | stat.S_IREAD)
        put_json(marker, dict(row, points3D=99))
        with self.assertRaisesRegex(SnapshotError, 'metadata_hash'):
            snapshot_scene_payload(self.run_dir)
        row.pop('snapshot_sha256')
        put_json(marker, row)
        self.assertEqual(len(list_ready_snapshots(self.run_dir/'snapshots')), 1)
        with self.assertRaisesRegex(ValueError, 'checksum_missing'):
            snapshot_scene_payload(self.run_dir)

    def test_model_counts_and_frame_metadata_must_match_native_model(self):
        from go1loc.build_watch import snapshot_scene_payload
        events = self.create_run()
        row = self.publish_snapshot(events)
        cases = [({'registered_images': 100}, 'native_counts'),
                 ({'frames': row['frames'][:-1]}, 'frame_identity'),
                 ({'frames': [dict(f, pts_time_s='not-a-number') for f in row['frames']]}, 'pts_invalid')]
        for changes, reason in cases:
            self.rewrite_snapshot(row, **changes)
            with self.assertRaisesRegex(ValueError, reason):
                snapshot_scene_payload(self.run_dir)

    def test_invalid_budget_or_index_is_rejected_before_native_parse(self):
        from go1loc.build_watch import snapshot_scene_payload
        events = self.create_run()
        self.publish_snapshot(events)
        for budget in (0, 50001, 3.2, True):
            with self.assertRaisesRegex(ValueError, 'point_budget'):
                snapshot_scene_payload(self.run_dir, point_budget=budget)
        for index in (3, -8, 'latest'):
            with self.assertRaisesRegex(ValueError, 'snapshot_index'):
                snapshot_scene_payload(self.run_dir, snapshot_index=index)


if __name__ == '__main__':
    unittest.main()
