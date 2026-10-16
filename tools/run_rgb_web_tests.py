"""Offline regression log/coverage runner for the RGB web mapping increment."""
import argparse
from pathlib import Path
import sys
import unittest

import coverage

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tests'), str(ROOT/'src')]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    cov = coverage.Coverage(data_file=str(args.output/'.coverage'), source=[
        'go1loc.web_capture', 'go1loc.web_build', 'go1loc.web_build_ui', 'go1loc.capture_import', 'go1loc.planning'])
    cov.start()
    suite = unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    with (args.output/'unittest.log').open('w', encoding='utf-8') as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    cov.stop()
    cov.save()
    cov.json_report(outfile=str(args.output/'coverage.json'))
    percent = cov.report(show_missing=True)
    from go1loc.jobs import atomic_json
    summary = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
        skipped=len(result.skipped), coverage_percent=percent, passed=result.wasSuccessful(),
        scope='software_regression_not_localization_accuracy_or_robot_acceptance', execution_permitted=False)
    atomic_json(args.output/'summary.json', summary)
    print(summary)
    raise SystemExit(not result.wasSuccessful())


if __name__ == '__main__':
    main()
