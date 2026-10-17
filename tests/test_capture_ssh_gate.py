import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('capture_ssh_gate', ROOT / 'tools/capture_restricted_ssh.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.access = {'agent': '/home/unitree/go1_capture_20261015_field02/capture_agent.py', 'cameras': [1, 2]}
        self.command = ('exec /usr/bin/python3 -u ' + self.access['agent'] + ' --authorize-readonly --camera 1 '
                        '--rate 5.0 --max-seconds 600.0 --max-mbps 30.0 --queue-size 2 '
                        '--width 464 --height 400 --projection unknown --owner-uid 1000')

    def test_exact_readonly_camera_command_allowed(self):
        self.assertEqual(gate.allowed_argv(self.command, self.access)[0], '/usr/bin/python3')

    def test_higher_sampling_bounded_to_twenty_and_bandwidth_checked(self):
        for rate in (5, 10, 15, 20):
            command = self.command.replace('--rate 5.0', '--rate ' + str(rate)).replace('--max-mbps 30.0', '--max-mbps 110')
            self.assertEqual(gate.allowed_argv(command, self.access)[0], '/usr/bin/python3')
        for rate, budget in ((20.1, 110), (15, 30), (20, 111), (20, float('inf'))):
            command = self.command.replace('--rate 5.0', '--rate ' + str(rate)).replace('--max-mbps 30.0', '--max-mbps ' + str(budget))
            with self.subTest(rate=rate, budget=budget), self.assertRaises(ValueError):
                gate.allowed_argv(command, self.access)

    def test_shell_extra_args_wrong_camera_and_unbounded_budgets_rejected(self):
        for command in ('true', 'sh', 'sudo reboot', self.command + '; true', self.command + ' --rate 1',
                        self.command.replace('--camera 1', '--camera 3'),
                        self.command.replace('--rate 5.0', '--rate nan'),
                        self.command.replace('--max-seconds 600.0', '--max-seconds 10000'),
                        self.command.replace('--width 464', '--width 2048')):
            with self.subTest(command=command), self.assertRaises(ValueError):
                gate.allowed_argv(command, self.access)

    def test_forced_command_probe_reject_and_hash_check(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = root / 'capture_agent.py'
            agent.write_text('# fixture only')
            access = {**self.access, 'agent': agent.as_posix(),
                      'sha256': {str(agent): hashlib.sha256(agent.read_bytes()).hexdigest()}}
            (root / 'access.json').write_text(json.dumps(access))
            with patch.object(gate, '__file__', str(root / 'gate.py')), patch('builtins.print'):
                with patch.dict('os.environ', {'SSH_ORIGINAL_COMMAND': 'go1-capture-check'}):
                    self.assertEqual(gate.main(), 0)
                with patch.dict('os.environ', {'SSH_ORIGINAL_COMMAND': 'true'}):
                    self.assertEqual(gate.main(), 126)
                command = self.command.replace(self.access['agent'], agent.as_posix())
                with patch.dict('os.environ', {'SSH_ORIGINAL_COMMAND': command}), patch.object(gate.os, 'execv') as execute:
                    gate.main()
                    execute.assert_called_once()
                    agent.write_text('# changed')
                    execute.reset_mock()
                    self.assertEqual(gate.main(), 126)
                    execute.assert_not_called()
