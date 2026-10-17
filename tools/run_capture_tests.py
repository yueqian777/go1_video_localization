"""Run offline capture tests + coverage without overwriting project .coverage."""
import argparse
import json
import sys
import unittest
from pathlib import Path

import coverage

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
parser = argparse.ArgumentParser()
parser.add_argument('--output', default='results/capture_development_20261015')
parser.add_argument('--pattern', default='test_capture*.py')
parser.add_argument('--minimum-coverage', type=float, default=80.)
args = parser.parse_args()
out = ROOT / args.output
out.mkdir(parents=True, exist_ok=True)
cov = coverage.Coverage(data_file=str(out / '.coverage'), source=['go1loc', str(ROOT / 'tools/chin_capture_candidate')])
cov.start()
suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern=args.pattern)
with (out / 'unittest.log').open('w', encoding='utf-8') as log:
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
cov.stop()
cov.save()
included = ['*/capture*.py', '*/chin_capture_candidate/*.py']
cov.json_report(outfile=str(out / 'coverage.json'), include=included)
percent = cov.report(include=included, show_missing=True)
summary = {'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
           'skipped': len(result.skipped), 'coverage_percent': percent,
           'problems': [str(test) for test, _ in result.failures + result.errors],
           'passed': result.wasSuccessful() and percent >= args.minimum_coverage}
(out / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
print(json.dumps(summary, indent=2))
raise SystemExit(0 if result.wasSuccessful() and percent >= args.minimum_coverage else 1)
