import json
from pathlib import Path
import tempfile
import unittest
from go1loc.build_guide import build_recipe, ps_quote


class BuildGuideTests(unittest.TestCase):
    def setUp(self):
        cache = Path(__file__).resolve().parents[1]/'.cache/tests'
        cache.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.video = self.root/"a ' $video.mp4"
        self.video.write_bytes(b'not decoded by command generator')

    def test_generates_existing_cli_recipe_without_building(self):
        text = build_recipe(self.root, self.root/'sidecar', self.video, self.root/'new', 'video')
        for command in ('prepare-video', 'build-map', 'watch-build', 'install_desktop_shortcut.ps1'):
            self.assertIn(command, text)
        self.assertIn("a '' $video.mp4", text)
        self.assertFalse((self.root/'new').exists())
        config = json.loads(next((self.root/'sidecar').rglob('frames.json')).read_text())
        self.assertEqual(config['max_frames'], 60)
        self.assertTrue(config['quality_filter'])

    def test_existing_output_and_unconfirmed_screen_rejected_before_write(self):
        with self.assertRaises(ValueError):
            build_recipe(self.root, self.root/'sidecar', self.video, self.root/'new', 'screen')
        with self.assertRaises(ValueError):
            build_recipe(self.root, self.root/'sidecar', self.video, self.root, 'video')
        self.assertFalse((self.root/'sidecar').exists())

    def test_quotes_and_sampling_budget(self):
        self.assertEqual(ps_quote("a'b $x"), "'a''b $x'")
        with self.assertRaises(ValueError):
            build_recipe(self.root, self.root/'sidecar', self.video, self.root/'new', 'video', duration=600, interval=.1)

    def test_confirmed_screen_config_is_copied_not_relabelled(self):
        from go1loc.jobs import atomic_json
        config = self.root/'screen.json'
        atomic_json(config, dict(source_size_wh=[640, 480], crop_xywh=[0, 0, 640, 480],
                               selected_window='confirmed single view', camera_id=None, display_mode='unknown'))
        text = build_recipe(self.root, self.root/'sidecar', self.video, self.root/'new', 'screen', screen_config=config)
        self.assertIn('--screen-config', text)
        copied = json.loads(next((self.root/'sidecar').rglob('screen.json')).read_text())
        self.assertIsNone(copied['camera_id'])
        self.assertEqual(copied['display_mode'], 'unknown')


if __name__ == '__main__': unittest.main()
