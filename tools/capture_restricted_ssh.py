"""Forced-command gate for a dedicated camera key (Python 3.6 compatible)."""
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import sys


def allowed_argv(command, access):
    argv = shlex.split(command)
    if argv[:1] == ['exec']:
        argv = argv[1:]
    prefix = ['/usr/bin/python3', '-u', access['agent'], '--authorize-readonly']
    if argv[:4] != prefix or len(argv[4:]) != 18:
        raise ValueError('capture_key_command_not_allowed')
    pairs = list(zip(argv[4::2], argv[5::2]))
    values = dict(pairs)
    expected = {'--camera', '--rate', '--max-seconds', '--max-mbps', '--queue-size',
                '--width', '--height', '--projection', '--owner-uid'}
    # Nine option/value pairs (18 tokens), never shell execution or extra flags.
    if len(values) != len(pairs) or set(values) != expected:
        raise ValueError('capture_key_options_not_allowed')
    if (int(values['--camera']) not in access['cameras'] or values['--width'] != '464'
            or values['--height'] != '400' or values['--owner-uid'] != '1000'
            or values['--projection'] != 'unknown' or not 1 <= int(values['--queue-size']) <= 2):
        raise ValueError('capture_key_camera_or_format_not_allowed')
    for key, minimum, maximum in (('--rate', .2, 20), ('--max-seconds', 1, 600), ('--max-mbps', 1, 110)):
        value = float(values[key])
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError('capture_key_budget_not_allowed')
    demand = (464 * 400 * 3 + 8192) * float(values['--rate']) * 8 / 1e6
    if demand > float(values['--max-mbps']):
        raise ValueError('capture_key_bandwidth_budget_exceeded')
    return argv


def main():
    access = json.loads(Path(__file__).with_name('access.json').read_text())
    command = os.environ.get('SSH_ORIGINAL_COMMAND', '')
    if command == 'go1-capture-check':
        print(json.dumps({'access': 'camera_readonly', 'cameras': access['cameras'],
                          'agent_exists': Path(access['agent']).is_file()}))
        return 0
    try:
        argv = allowed_argv(command, access)
        for path, expected in access['sha256'].items():
            if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
                raise ValueError('capture_agent_changed_requires_review')
    except (ValueError, KeyError, OSError):
        print('capture_key_command_not_allowed_or_agent_changed', file=sys.stderr)
        return 126
    os.execv(argv[0], argv)


if __name__ == '__main__':
    sys.exit(main())
