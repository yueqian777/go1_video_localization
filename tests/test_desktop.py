"""Desktop launch safety contracts; no browser/robot or network services started."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch, Mock

from go1loc.jobs import atomic_json
from go1loc.desktop import load_config, InstanceLock, instance_key, existing_url, run_viewer, available_port


class DesktopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.map = self.root/'中文 地图'
        atomic_json(self.map/'map_manifest.json', {'frozen': True})
        self.path = self.root/'desktop.json'
        self.settings = dict(map_dir=str(self.map), sidecar_dir=str(self.root/'viewer'), port=8767)
        atomic_json(self.path, self.settings)

    def test_configuration_requires_frozen_map_and_separate_sidecar(self):
        self.assertEqual(load_config(self.path)['map_dir'], str(self.map.resolve()))
        atomic_json(self.map/'map_manifest.json', {'frozen': False})
        with self.assertRaises(ValueError):
            load_config(self.path)
        atomic_json(self.map/'map_manifest.json', {'frozen': True})
        atomic_json(self.path, dict(self.settings, sidecar_dir=str(self.map/'viewer')))
        with self.assertRaises(ValueError):
            load_config(self.path)

    def test_invalid_ports_are_not_accepted(self):
        for value in (True, 0, 65536, '8767'):
            atomic_json(self.path, dict(self.settings, port=value))
            with self.assertRaises(ValueError):
                load_config(self.path)

    def test_lock_released_and_reacquired(self):
        first, second = InstanceLock(self.root/'instance.lock'), InstanceLock(self.root/'instance.lock')
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        first.close()
        self.assertTrue(second.acquire())
        second.close()

    def test_existing_url_never_uses_arbitrary_external_url_or_other_config(self):
        config = load_config(self.path)
        record = self.root/'server.json'
        atomic_json(record, dict(config_key='other', port=8767, url='https://example.com'))
        self.assertIsNone(existing_url(record, config))
        atomic_json(record, dict(config_key=instance_key(config), port=8768, url='https://example.com'))
        with patch('go1loc.desktop.http_ready', return_value=True):
            self.assertEqual(existing_url(record, config), 'http://127.0.0.1:8768/')

    def test_owned_live_instance_is_reused_without_constructing_app(self):
        config = load_config(self.path)
        lock = InstanceLock(self.root/'desktop.lock')
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.close)
        with patch('go1loc.desktop.existing_url', return_value='http://127.0.0.1:8768/'), \
             patch('go1loc.desktop.webbrowser.open') as browser:
            factory = Mock()
            run_viewer(self.path, factory=factory)
        factory.assert_not_called()
        browser.assert_called_once_with('http://127.0.0.1:8768/')

    def test_failed_start_never_opens_browser_and_releases_lock(self):
        factory = Mock(side_effect=RuntimeError('failure'))
        with patch('go1loc.desktop.webbrowser.open') as browser:
            with self.assertRaises(RuntimeError):
                run_viewer(self.path, factory=factory)
        browser.assert_not_called()
        lock = InstanceLock(self.root/'desktop.lock')
        self.assertTrue(lock.acquire())
        lock.close()

    def test_occupied_port_is_skipped_without_stopping_other_service(self):
        import socket
        with socket.socket() as service:
            service.bind(('127.0.0.1', 0))
            service.listen(1)
            port = service.getsockname()[1]
            if port == 65535:
                self.skipTest('OS selected last port')
            self.assertNotEqual(available_port(port), port)
            self.assertEqual(service.getsockname()[1], port)

    def test_success_uses_actual_port_and_token_stop(self):
        from types import SimpleNamespace
        config = load_config(self.path)
        app = SimpleNamespace(server=SimpleNamespace(get_port=lambda: 9123), close=Mock())
        def stop_owned_viewer(_):
            state = __import__('json').loads((self.root/'desktop.server.json').read_text())
            atomic_json(self.root/'desktop.stop.json', {'token': state['token']})
        with patch('go1loc.desktop.http_ready', return_value=True), \
             patch('go1loc.desktop.time.sleep', side_effect=stop_owned_viewer), \
             patch('go1loc.desktop.webbrowser.open') as browser:
            url = run_viewer(self.path, factory=Mock(return_value=app))
        self.assertEqual(url, 'http://127.0.0.1:9123/')
        browser.assert_called_once_with(url)
        app.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
