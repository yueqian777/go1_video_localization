import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from go1loc.capture_desktop import existing_capture, load_settings, main, run_capture
from go1loc.capture_web import CaptureWeb
from go1loc.desktop import instance_key
from go1loc.jobs import atomic_json, read_json


class CaptureDesktopTests(unittest.TestCase):
    def test_settings_and_no_auto_connection(self):
        with tempfile.TemporaryDirectory() as t:
            config = Path(t) / 'launcher.json'
            connections = Path(t) / 'connections.json'
            atomic_json(connections, {'cameras': {}, 'defaults': {}})
            atomic_json(config, {'connection_config': str(connections), 'output': str(Path(t) / 'captures'), 'port': 8772})
            settings = load_settings(config)
            self.assertEqual(settings['port'], 8772)
            self.assertEqual(settings['output'], str((Path(t) / 'captures').resolve()))
            with patch('go1loc.capture_desktop.CaptureWeb') as app:
                run_capture(config, open_browser=False, stop=True)
                app.assert_not_called()
            atomic_json(config, {**settings, 'port': 80})
            with self.assertRaises(ValueError):
                load_settings(config)

    def test_live_launcher_reuse_and_graceful_stop(self):
        with tempfile.TemporaryDirectory() as t:
            config = Path(t) / 'launcher.json'
            connections = Path(t) / 'connections.json'
            atomic_json(connections, {})
            atomic_json(config, {'connection_config': str(connections), 'output': str(Path(t) / 'captures')})
            settings = load_settings(config)
            record = config.with_suffix('.server.json')
            outcome = []
            # Only a localhost HTTP server is started; no source is ever selected.
            thread = threading.Thread(target=lambda: outcome.append(run_capture(config, open_browser=False)))
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not existing_capture(record, settings) and time.monotonic() < deadline:
                    time.sleep(.05)
                url = existing_capture(record, settings)
                self.assertIsNotNone(url)
                original = read_json(record)
                with patch('go1loc.capture_desktop.webbrowser.open') as opened:
                    self.assertEqual(run_capture(config), url)
                    opened.assert_called_once_with(url)
                self.assertEqual(read_json(record), original)
            finally:
                run_capture(config, stop=True, open_browser=False)
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome, [url])
            self.assertIsNone(existing_capture(record, settings))

    def test_stale_record_and_missing_connection(self):
        with tempfile.TemporaryDirectory() as t:
            config = Path(t) / 'launcher.json'
            atomic_json(config, {'connection_config': str(Path(t) / 'missing'), 'output': t})
            with self.assertRaisesRegex(ValueError, 'connection_config_missing'):
                load_settings(config)
            self.assertIsNone(existing_capture(config, {}))
            atomic_json(config, {'config_key': 'other', 'port': 8772})
            self.assertIsNone(existing_capture(config, {}))

    def test_foreign_instance_and_busy_startup(self):
        with tempfile.TemporaryDirectory() as t:
            config = Path(t) / 'launcher.json'
            connections = Path(t) / 'connections.json'
            atomic_json(connections, {})
            atomic_json(config, {'connection_config': str(connections), 'output': t})
            settings = load_settings(config)
            app = CaptureWeb(connections, t, port=0)
            app.start()
            try:
                record = config.with_suffix('.server.json')
                atomic_json(record, {'config_key': instance_key(settings), 'port': app.server.server_port,
                                     'instance_id': 'not-the-running-instance'})
                self.assertIsNone(existing_capture(record, settings))
                with patch('go1loc.capture_desktop.InstanceLock') as lock, patch('go1loc.capture_desktop.time.sleep'):
                    lock.return_value.acquire.return_value = False
                    with self.assertRaises(RuntimeError):
                        run_capture(config, open_browser=False)
            finally:
                app.close()

    def test_main_logging_and_error_return(self):
        with tempfile.TemporaryDirectory() as t:
            config = Path(t) / 'launcher.json'
            with patch('go1loc.capture_desktop.run_capture') as run:
                self.assertEqual(main(['--config', str(config), '--no-browser', '--stop']), 0)
                run.assert_called_once_with(config, open_browser=False, stop=True)
                run.side_effect = RuntimeError('test launch failure')
                self.assertEqual(main(['--config', str(config), '--no-browser']), 1)
            self.assertIn('test launch failure', config.with_suffix('.log').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
