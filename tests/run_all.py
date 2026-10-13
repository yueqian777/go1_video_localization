import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'src'))
suite = unittest.defaultTestLoader.discover(str(ROOT))
raise SystemExit(0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1)
