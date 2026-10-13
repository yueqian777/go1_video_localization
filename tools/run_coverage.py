"""Coverage runner preserving the Windows embedded Python extension search path.

coverage's CLI removes the directory next to python312.zip from sys.path.
Embedded Python keeps _ctypes/_overlapped there, so use its public API instead.
"""
import argparse
import runpy
import sys
from pathlib import Path
import coverage


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--append', action='store_true')
    parser.add_argument('script', type=Path)
    parser.add_argument('args', nargs=argparse.REMAINDER)
    args=parser.parse_args()
    cov=coverage.Coverage(source=['go1loc'])
    if args.append:
        cov.load()
    sys.argv=[str(args.script),*args.args]
    cov.start()
    try:
        runpy.run_path(str(args.script),run_name='__main__')
    finally:
        cov.stop(); cov.save()


if __name__=='__main__':
    main()
