"""OFFLINE CANDIDATE. Python 3.6+. No deployment, service control or auto-restart.

Only supervises its own direct child. A hardware worker needs exclusive camera
ownership approved separately; never run it alongside the factory producer.
"""
import argparse
import json
import math
import re
import signal
import subprocess
import threading
import time
from pathlib import Path


def supervise(command, startup_timeout=5., frame_timeout=3., max_seconds=30., stop=None):
    for value in (startup_timeout, frame_timeout, max_seconds):
        if not math.isfinite(value) or not 0 < value <= 600:
            raise ValueError('invalid_supervision_budget')
    stop = stop or threading.Event()
    start = time.monotonic()
    state = {'seq': 0, 'frames': 0, 'last': start}
    lock = threading.Lock()
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, bufsize=0,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def read_heartbeat():
        pending, overflow = b'', False
        while True:
            chunk = process.stdout.read(1024)
            if not chunk:
                return
            # At most 128 bytes per line retained, even with hostile SDK logs.
            for part in chunk.splitlines(keepends=True):
                if not overflow:
                    pending += part
                    if len(pending) > 128:
                        pending, overflow = b'', True
                if part.endswith(b'\n'):
                    match = None if overflow else re.fullmatch(b'GO1_FRAME ([1-9][0-9]{0,18})\r?\n', pending)
                    if match:
                        seq = int(match.group(1))
                        with lock:
                            if seq > state['seq']:
                                state.update(seq=seq, frames=state['frames'] + 1, last=time.monotonic())
                    pending, overflow = b'', False

    def discard_stderr():
        while process.stderr.read(1024):
            pass

    threads = [threading.Thread(target=f, daemon=True) for f in (read_heartbeat, discard_stderr)]
    reason, killed = 'operator_stop', False
    try:
        for thread in threads:
            thread.start()
        while not stop.wait(.02):
            code = process.poll()
            if code is not None:
                threads[0].join(timeout=.2)
                reason = ('worker_complete' if state['frames'] else 'worker_no_frames') if code == 0 else 'worker_exit_' + str(code)
                break
            now = time.monotonic()
            with lock:
                frames, last = state['frames'], state['last']
            if now - start >= max_seconds:
                reason = 'run_budget_expired'
                break
            if now - last >= (frame_timeout if frames else startup_timeout):
                reason = 'new_frame_timeout' if frames else 'startup_frame_timeout'
                break
    finally:
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=.3)
            except subprocess.TimeoutExpired:
                killed = True
                try:
                    process.kill()
                    process.wait(timeout=1.)
                except (OSError, subprocess.TimeoutExpired):
                    reason = 'owned_worker_cleanup_failed'
            except OSError:
                if process.poll() is None:
                    reason = 'owned_worker_cleanup_failed'
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=.2)
        if not any(t.is_alive() for t in threads):
            process.stdout.close()
            process.stderr.close()
    return {'reason': reason, 'returncode': process.poll(), 'frames': state['frames'],
            'forced_kill': killed, 'elapsed_s': round(time.monotonic() - start, 3)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--authorize-exclusive-capture', action='store_true')
    parser.add_argument('--worker', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--startup-timeout', type=float, default=5.)
    parser.add_argument('--frame-timeout', type=float, default=3.)
    parser.add_argument('--seconds', type=float, default=30.)
    args = parser.parse_args(argv)
    if not args.authorize_exclusive_capture:
        parser.error('explicit_exclusive_capture_authorization_required')
    worker, config = Path(args.worker), Path(args.config)
    if not worker.is_absolute() or not config.is_absolute() or not worker.is_file() or not config.is_file():
        parser.error('worker_and_config_must_be_existing_absolute_files')
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    result = supervise([str(worker), '--authorize-exclusive-capture', str(config), str(args.seconds)],
                       args.startup_timeout, args.frame_timeout, args.seconds + 2., stop)
    print(json.dumps(result), flush=True)
    return 0 if result['reason'] == 'worker_complete' else 20


if __name__ == '__main__':
    raise SystemExit(main())
