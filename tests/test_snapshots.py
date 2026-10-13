import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from go1loc.events import BoundedEventWriter, read_events
from go1loc.snapshots import SnapshotPublisher, SnapshotError, list_ready_snapshots


def fixture_model(directory, ids=(1, 2)):
    """Intentionally fake binary bytes, used only with the explicit fake reader."""
    directory.mkdir(parents=True)
    for name in ('cameras', 'images', 'points3D', 'rigs', 'frames'):
        (directory / f'{name}.bin').write_bytes(b'fixture model bytes')
    return SimpleNamespace(is_valid=lambda: True, num_reg_images=lambda: len(ids),
                           num_points3D=lambda: 3,
                           images={i: SimpleNamespace(name=f'{i}.png') for i in ids})


class SnapshotTests(unittest.TestCase):
    def test_invalid_video_manifest_is_not_replaced_by_filename_order(self):
        from go1loc.mapping import _video_references
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'images'
            images.mkdir()
            for frames in [[], [{'kept': True, 'image_path': 'images/a.png', 'pts_time_s': None}],
                           [{'kept': True, 'image_path': '../outside.png', 'pts_time_s': 1.0}],
                           [{'kept': True, 'image_path': 'images/a.png', 'pts_time_s': 1.0}] * 2]:
                (root / 'frames_manifest.json').write_text(json.dumps({'frames': frames}), encoding='utf-8')
                with self.assertRaises(Exception):
                    _video_references(images.resolve(), ['a.png'])

    def test_mapping_failure_has_no_final_and_releases_ascii_stage(self):
        from go1loc.mapping import build_map
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'images'
            images.mkdir()
            for name in ('a.png', 'b.png'):
                (images / name).write_bytes(b'fixture')
            staged = root / 'stage'
            staged.mkdir()
            events = BoundedEventWriter(root / 'events.jsonl')
            with patch('go1loc.mapping._ascii_stage', return_value=staged), \
                 patch('go1loc.mapping._require_backend', side_effect=RuntimeError('backend failure')):
                with self.assertRaisesRegex(RuntimeError, 'backend failure'):
                    build_map(images, root / 'map', events=events)
            self.assertFalse(staged.exists())
            self.assertEqual(read_events(events.path)[-1]['event'], 'failed')
            self.assertNotIn('final', [r['event'] for r in read_events(events.path)])

    def test_injected_writer_wrong_map_is_rejected_before_any_mapping(self):
        from go1loc.mapping import build_map, MappingConfig
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = BoundedEventWriter(root / 'events.jsonl', map_id='other-map')
            with self.assertRaisesRegex(Exception, 'event_map_id_mismatch'):
                build_map(root / 'images', root / 'map', MappingConfig(map_id='target-map'), events=events)

    def test_new_manifest_hashes_feature_bytes_and_embeds_video_provenance(self):
        import hashlib
        from go1loc.mapping import _write_manifest, MappingConfig
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'video' / 'images'
            images.mkdir(parents=True)
            (images / 'frame.png').write_bytes(b'fixture')
            frames = {'frames': [{'image_path': 'images/frame.png', 'pts_time_s': 13.75}],
                      'probe': {'source_sha256': 'synthetic-unit-test'}}
            source = images.parent / 'frames_manifest.json'
            source.write_text(json.dumps(frames), encoding='utf-8')
            output = root / 'map'
            (output / 'sfm' / 'models').mkdir(parents=True)
            feature = output / 'features.h5'
            feature.write_bytes(b'feature fixture')
            model = SimpleNamespace(images={}, points3D={}, export_PLY=Mock(), write_text=Mock(),
                                    num_reg_images=lambda: 0, num_points3D=lambda: 0, summary=lambda: 'fixture')
            manifest = _write_manifest(output, MappingConfig(), images, model, ['frame.png'], {}, None,
                                       feature, 'exhaustive')
            self.assertEqual(manifest['frames_manifest'], frames)
            self.assertEqual(manifest['frames_manifest_sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(manifest['local_features_sha256'], hashlib.sha256(feature.read_bytes()).hexdigest())
            declared = manifest.pop('manifest_sha256')
            self.assertEqual(declared, hashlib.sha256(json.dumps(manifest, sort_keys=True,
                             ensure_ascii=False).encode('utf-8')).hexdigest())

    def test_native_421_snapshot_api_and_limit_with_synthetic_database(self):
        import pycolmap
        from go1loc.snapshots import run_incremental_mapping
        self.assertEqual(pycolmap.__version__, '4.2.1')
        options = pycolmap.IncrementalPipelineOptions()
        self.assertTrue(hasattr(options, 'snapshot_path'))
        self.assertTrue(hasattr(options, 'snapshot_frames_freq'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'images'
            images.mkdir()
            database_path = root / 'synthetic.db'
            with pycolmap.Database.open(database_path) as database:
                synthetic = pycolmap.SyntheticDatasetOptions()
                synthetic.num_rigs = 1
                synthetic.num_cameras_per_rig = 1
                synthetic.num_frames_per_rig = 8
                synthetic.num_points3D = 100
                pycolmap.synthesize_dataset(synthetic, database)
            events = BoundedEventWriter(root / 'events.jsonl', map_id='synthetic-test-not-Go1')
            publisher = SnapshotPublisher(root / 'snapshots', events=events, max_count=2)
            with patch.object(publisher, 'publish', wraps=publisher.publish) as publish:
                models = run_incremental_mapping(database_path, images, root / 'models',
                                             {'extract_colors': False, 'num_threads': 1,
                                              'min_model_size': 2, 'mapper': {'init_min_num_inliers': 20}},
                                             publisher, frames_freq=1)
            self.assertEqual(publish.call_count, 2, 'native writing must stop, not merely discard excess output')
            self.assertTrue(models)
            self.assertEqual(publisher.count, 2)
            self.assertEqual(len(list_ready_snapshots(root / 'snapshots')), 2)
            self.assertFalse(list(root.glob('.native-*')))
            self.assertTrue(all(r['source'] == 'pycolmap_native_snapshot'
                                for r in list_ready_snapshots(root / 'snapshots')))
            self.assertIn('registration_progress', [r['event'] for r in read_events(events.path)])

    def test_complete_atomic_publish_and_component_replacement_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = fixture_model(root / 'native')
            writer = BoundedEventWriter(root / 'events.jsonl', run_id='run', map_id='map')
            publisher = SnapshotPublisher(root / 'snapshots', events=writer, model_loader=lambda _: model)
            with patch('go1loc.snapshots.os.replace', wraps=__import__('os').replace) as replace:
                first = publisher.publish(root / 'native', 'component:A',
                                          frame_times={'1.png': {'pts_time_s': 8.2, 'pts': 82}})
            self.assertTrue(replace.called)
            self.assertEqual(first['mode'], 'build_snapshot')
            self.assertEqual(first['state_semantics'], 'replace_component')
            self.assertEqual(first['frames'][0]['pts_time_s'], 8.2)
            self.assertIsNone(first['frames'][1]['pts_time_s'])
            second = publisher.publish(root / 'native', 'component:B')
            self.assertNotEqual(first['component_id'], second['component_id'])
            self.assertEqual(len(list_ready_snapshots(root / 'snapshots')), 2)
            self.assertEqual(read_events(writer.path)[-1]['event'], 'snapshot_ready')
            self.assertFalse(list((root / 'snapshots').glob('.pending-*')))

    def test_incomplete_invalid_or_changed_model_never_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = fixture_model(root / 'native')
            publisher = SnapshotPublisher(root / 'snapshots', model_loader=lambda _: model)
            (root / 'native' / 'frames.bin').unlink()
            with self.assertRaises(SnapshotError):
                publisher.publish(root / 'native', 'c')
            self.assertEqual(list_ready_snapshots(root / 'snapshots'), [])
            (root / 'native' / 'frames.bin').write_bytes(b'fixture model bytes')
            model.is_valid = lambda: False
            with self.assertRaisesRegex(SnapshotError, 'invalid'):
                publisher.publish(root / 'native', 'c')
            model.is_valid = lambda: True
            def changing_loader(_):
                (root / 'native' / 'images.bin').write_bytes(b'changed source while copying')
                return model
            publisher.model_loader = changing_loader
            with self.assertRaisesRegex(SnapshotError, 'changed'):
                publisher.publish(root / 'native', 'c')
            self.assertEqual(list_ready_snapshots(root / 'snapshots'), [])

    def test_count_byte_budget_and_no_final_replay_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = fixture_model(root / 'native')
            publisher = SnapshotPublisher(root / 'snapshots', max_count=1, model_loader=lambda _: model)
            self.assertEqual(list_ready_snapshots(root / 'snapshots'), [])
            self.assertIsNotNone(publisher.publish(root / 'native', 'a'))
            self.assertIsNone(publisher.publish(root / 'native', 'a'))
            self.assertTrue(publisher.exhausted)
            tiny = SnapshotPublisher(root / 'tiny', max_bytes=32, model_loader=lambda _: model)
            self.assertIsNone(tiny.publish(root / 'native', 'a'))
            self.assertEqual(list_ready_snapshots(root / 'tiny'), [])
            self.assertTrue(tiny.exhausted)

    def test_reader_rejects_tampered_snapshot_and_ignores_staging(self):
        import os
        import stat
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = fixture_model(root / 'native')
            publisher = SnapshotPublisher(root / 'snapshots', model_loader=lambda _: model)
            row = publisher.publish(root / 'native', '../component label')
            (root / 'snapshots' / '.pending-incomplete').mkdir()
            self.assertEqual(len(list_ready_snapshots(root / 'snapshots')), 1)
            target = Path(row['snapshot_path']) / 'images.bin'
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
            target.write_bytes(b'tampered')
            with self.assertRaises(SnapshotError):
                list_ready_snapshots(root / 'snapshots')

    def test_mapping_defaults_disabled_and_sidecar_must_be_outside_map(self):
        from go1loc.mapping import MappingConfig, build_map
        self.assertEqual(MappingConfig().snapshot_frames_freq, 0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(Exception, 'sidecar_overlaps'):
                build_map(root / 'images', root / 'map',
                          MappingConfig(sidecar_dir=str(root / 'map' / 'sidecar')))

    def test_small_video_only_claims_temporal_window_and_pts_order(self):
        from go1loc.mapping import MappingConfig, build_map
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'video' / 'images'
            images.mkdir(parents=True)
            for name in ('a.png', 'b.png', 'c.png'):
                (images / name).write_bytes(b'fixture')
            (images.parent / 'frames_manifest.json').write_text(json.dumps({
                'geometry': {'status': 'uncalibrated'},
                'frames': [{'kept': True, 'image_path': f'images/{name}', 'pts_time_s': pts}
                           for name, pts in [('c.png', 2.0), ('a.png', 3.0), ('b.png', 5.0)]]}), encoding='utf-8')
            extract = SimpleNamespace(confs={'superpoint_aachen': {'output': 'features', 'model': {}}}, main=Mock())
            match = SimpleNamespace(confs={'superpoint+lightglue': {'output': 'matches'}}, main=Mock())
            model = SimpleNamespace(num_reg_images=lambda: 3, num_points3D=lambda: 4)
            backend = (SimpleNamespace(CameraMode=SimpleNamespace(SINGLE='SINGLE')), extract, match,
                       Mock(), Mock(), SimpleNamespace(main=Mock(return_value=model)))
            writer = BoundedEventWriter(root / 'events.jsonl')
            with patch('go1loc.mapping._require_backend', return_value=backend), \
                 patch('go1loc.mapping._ascii_stage', return_value=None), \
                 patch('go1loc.mapping._write_manifest', return_value={'frozen': True}) as manifest:
                build_map(images, root / 'map', MappingConfig(temporal_window=1), events=writer)
            self.assertEqual(manifest.call_args.args[-1], 'temporal_window')
            self.assertEqual((root / 'map' / 'pairs-mapping.txt').read_text().splitlines(), ['c.png a.png', 'a.png b.png'])
            self.assertEqual(read_events(writer.path)[-1]['event'], 'final')


if __name__ == '__main__':
    unittest.main()
