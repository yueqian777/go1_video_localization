"""Starter lifecycle checks; HTTP, subprocess and browser calls are mocked, no robot access."""
import io
import json
import pathlib
import socket
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'tools'))
import start_pc


class StarterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='go1_starter_test_')
        self.root = pathlib.Path(self.temporary.name)
        (self.root / 'config').mkdir()
        (self.root / 'config/pc.json').write_text(json.dumps({'host':'192.168.12.1','port':8765,'simulation':False}), encoding='utf-8')
        self.expected = {'app_name':'go1_remote_control','app_version':start_pc.APP_VERSION,
                         'address':'192.168.12.1:8765','simulation':False}
        self.root_patch = patch.object(start_pc, 'ROOT', self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.temporary.cleanup()

    def run_starter(self, response=None, error=None):
        with patch('start_pc.urllib.request.urlopen', side_effect=error,
                   return_value=io.BytesIO(json.dumps(response or {}).encode())) as http, \
             patch('start_pc.webbrowser.open') as browser, patch('start_pc.subprocess.call', return_value=0) as process, \
             patch('builtins.print'):
            code = start_pc.main()
            return code, http.call_count, browser.call_count, process.call_count

    def test_reopen_current_service_does_not_spawn_duplicate(self):
        self.assertEqual(self.run_starter(self.expected), (0,1,1,0))

    def test_old_service_requires_manual_restart(self):
        self.assertEqual(self.run_starter(dict(self.expected, app_version='0.2.0-20261018')), (1,1,0,0))

    def test_simulation_service_is_not_opened_as_real_controller(self):
        self.assertEqual(self.run_starter(dict(self.expected, simulation=True)), (1,1,0,0))

    def test_only_connection_refusal_starts_one_local_service(self):
        self.assertEqual(self.run_starter(error=urllib.error.URLError(ConnectionRefusedError())), (0,1,0,1))

    def test_unresponsive_existing_service_does_not_spawn_another(self):
        self.assertEqual(self.run_starter(error=urllib.error.URLError(socket.timeout('test'))), (1,1,0,0))


if __name__ == '__main__':
    unittest.main(verbosity=2)
