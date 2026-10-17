import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from go1loc.video import _ffprobe_json


class BackgroundVideoTests(unittest.TestCase):
    def test_probe_does_not_inherit_console_or_wait_for_input_under_pythonw(self):
        with patch('go1loc.video.shutil.which',return_value='ffprobe'),patch(
                'go1loc.video.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='{}')) as run:
            _ffprobe_json(Path('fixture.mkv'))
        self.assertEqual(run.call_args.kwargs['stdin'],subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs['creationflags'],getattr(subprocess,'CREATE_NO_WINDOW',0))
