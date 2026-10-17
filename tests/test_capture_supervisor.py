"""The SDK double never opens a device; native compilation uses MSYS2 only."""
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / 'tools/chin_capture_candidate/supervise.py'


class SupervisorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('chin_candidate_guard', GUARD)
        cls.guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.guard)

    def test_no_heartbeat_startup_timeout_and_reaped(self):
        result = self.guard.supervise([sys.executable, '-c', 'import time;time.sleep(20)'],
                                      startup_timeout=.2, frame_timeout=.2, max_seconds=2.)
        self.assertEqual(result['reason'], 'startup_frame_timeout')
        self.assertIsNotNone(result['returncode'])
        self.assertLess(result['elapsed_s'], 2.)

    def test_only_advancing_exact_heartbeat_counts(self):
        code = "import time;print('GO1_FRAME 1',flush=True);\nwhile True: print('GO1_FRAME 1',flush=True);time.sleep(.02)"
        result = self.guard.supervise([sys.executable, '-u', '-c', code],
                                      startup_timeout=1., frame_timeout=.2, max_seconds=2.)
        self.assertEqual(result['frames'], 1)
        self.assertEqual(result['reason'], 'new_frame_timeout')

    def test_bounded_log_noise_and_nonzero_exit(self):
        code = "import sys;sys.stdout.write('x'*100000+'\\n');sys.stderr.write('private'*10000);sys.exit(7)"
        result = self.guard.supervise([sys.executable, '-c', code])
        self.assertEqual(result['reason'], 'worker_exit_7')
        self.assertEqual(result['frames'], 0)
        self.assertNotIn('private', str(result))

    def test_budget_and_authorization_before_launch(self):
        from unittest.mock import patch
        with patch.object(self.guard.subprocess, 'Popen') as launch:
            for number in (0, -1, float('inf'), float('nan')):
                with self.assertRaises(ValueError):
                    self.guard.supervise(['unused'], frame_timeout=number)
            with self.assertRaises(SystemExit):
                self.guard.main(['--worker', 'unused', '--config', 'unused'])
            launch.assert_not_called()

    def test_cli_file_gate_and_exit_codes(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as t, patch.object(self.guard.signal, 'signal'), \
                patch('sys.stderr', io.StringIO()), patch('sys.stdout', io.StringIO()):
            worker, config = Path(t) / 'worker', Path(t) / 'config'
            argv = ['--authorize-exclusive-capture', '--worker', str(worker), '--config', str(config)]
            with patch.object(self.guard, 'supervise') as run:
                with self.assertRaises(SystemExit):
                    self.guard.main(argv)
                run.assert_not_called()
                worker.touch()
                config.touch()
                for reason, exit_code in [('worker_complete', 0), ('new_frame_timeout', 20)]:
                    run.return_value = {'reason': reason}
                    self.assertEqual(self.guard.main(argv), exit_code)

    def test_run_budget_and_operator_stop(self):
        import threading
        result = self.guard.supervise([sys.executable, '-c', 'import time;time.sleep(20)'],
                                      max_seconds=.1)
        self.assertEqual(result['reason'], 'run_budget_expired')
        stop = threading.Event()
        stop.set()
        result = self.guard.supervise([sys.executable, '-c', 'import time;time.sleep(20)'], stop=stop)
        self.assertEqual(result['reason'], 'operator_stop')
        self.assertIsNotNone(result['returncode'])

    def test_kill_escalation_when_owned_worker_ignores_terminate(self):
        import threading
        from unittest.mock import Mock, patch
        proc = Mock(stdout=io.BytesIO(), stderr=io.BytesIO())
        proc.poll.side_effect = [None, -9]
        proc.wait.side_effect = [subprocess.TimeoutExpired('mock', .3), -9]
        stop = threading.Event()
        stop.set()
        with patch.object(self.guard.subprocess, 'Popen', return_value=proc):
            result = self.guard.supervise(['owned_mock'], stop=stop)
        self.assertTrue(result['forced_kill'])
        proc.kill.assert_called_once()


class NativeWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        SupervisorTests.setUpClass.__func__(cls)
        compiler = Path(os.environ.get('GO1_TEST_MSYS_GXX', 'C:/msys64/mingw64/bin/g++.exe'))
        if not compiler.is_file():
            raise unittest.SkipTest('MSYS2 C++ compiler unavailable; native mock validation NOT performed')
        cls.temp = tempfile.TemporaryDirectory(prefix='go1_sdk_double_')
        build = Path(cls.temp.name)
        shutil.copyfile(ROOT / 'tools/chin_capture_candidate/capture_worker.cc', build / 'capture_worker.cc')
        shutil.copyfile(ROOT / 'tests/fixtures/camera_sdk_double/UnitreeCameraSDK.hpp', build / 'UnitreeCameraSDK.hpp')
        cls.worker = build / 'capture_worker_mock.exe'
        env = dict(os.environ, PATH=str(compiler.parent) + os.pathsep + os.environ.get('PATH', ''))
        result = subprocess.run([str(compiler), '-std=c++11', '-Wall', '-Wextra', '-Werror',
                                 '-static', '-pthread', '-I', str(build), str(build / 'capture_worker.cc'),
                                 '-o', str(cls.worker)], capture_output=True, text=True, timeout=45., env=env, check=False)
        if result.returncode:
            cls.temp.cleanup()
            raise AssertionError(f'MSYS2 compiler exit {result.returncode}: {result.stderr}')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_mode(self, mode):
        return self.guard.supervise([str(self.worker), '--authorize-exclusive-capture', mode, '0.2'],
                                    startup_timeout=.5, frame_timeout=.15, max_seconds=1.)

    def test_native_normal_static_pixels_advancing_sdk_timestamp(self):
        result = self.run_mode('normal')
        self.assertEqual(result['reason'], 'worker_complete')
        self.assertGreater(result['frames'], 2)

    def test_native_failures_and_all_blocking_phases(self):
        expected = {'open_fail': 'worker_exit_10', 'start_fail': 'worker_exit_11',
                    'constructor_hang': 'startup_frame_timeout', 'read_hang': 'startup_frame_timeout',
                    'read_false': 'worker_no_frames', 'empty': 'worker_no_frames',
                    'timestamp_stuck': 'new_frame_timeout', 'stop_hang': 'new_frame_timeout',
                    'stop_fail': 'worker_exit_12', 'throw': 'worker_exit_13', 'closed': 'worker_exit_14'}
        for mode, reason in expected.items():
            with self.subTest(mode=mode):
                result = self.run_mode(mode)
                self.assertEqual(result['reason'], reason)
                self.assertIsNotNone(result['returncode'])
                self.assertLess(result['elapsed_s'], 2.)

    def test_native_does_nothing_without_explicit_authorization(self):
        result = subprocess.run([str(self.worker)], capture_output=True, timeout=2., check=False)
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
