"""Read-only Linux SysV agent. Deploy this file AND capture_wire.py manually.

No SDK, NumPy, ROS, V4L2, service management or shared-memory writes.
Python >=3.6; only stdout carries binary image data. Finite lifetime by default.
"""
import argparse
import ctypes
import json
import os
import queue
import select
import signal
import sys
import threading
import time
import uuid

try:
    from .capture_wire import encode_frame, make_header
except ImportError:
    from capture_wire import encode_frame, make_header


def segment_info(key, expected_uid, expected_size, proc='/proc/sysvipc/shm'):
    with open(proc) as handle:
        lines = [line.split() for line in handle]
    entries = [dict(zip(lines[0], row)) for row in lines[1:]]
    matches = [row for row in entries if int(row['key']) == key]
    if len(matches) != 1:
        raise RuntimeError('shm_missing_or_ambiguous')
    row = matches[0]
    if int(row['uid']) != expected_uid or int(row['size']) != expected_size:
        raise RuntimeError('shm_owner_or_size_mismatch')
    return row


class ReadonlyShm:
    def __init__(self, key, width=464, height=400, expected_uid=None, libc=None, info=segment_info):
        self.size = 4 + width * height * 3
        uid = os.getuid() if expected_uid is None else expected_uid
        self.key, self.uid, self.info = key, uid, info
        self.next_identity_check = 0.
        row = info(key, uid, self.size)
        self.libc = libc or ctypes.CDLL(None, use_errno=True)
        self.libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        self.libc.shmat.restype = ctypes.c_void_p
        self.libc.shmdt.argtypes = [ctypes.c_void_p]
        self.libc.shmdt.restype = ctypes.c_int
        self.ptr = self.libc.shmat(int(row['shmid']), None, 0o10000)  # SHM_RDONLY
        if self.ptr == ctypes.c_void_p(-1).value:
            self.ptr = None
            raise OSError(ctypes.get_errno(), 'readonly_shmat_failed')
        try:
            again = info(key, uid, self.size)
            if any(row.get(k) != again.get(k) for k in ('shmid', 'ctime', 'uid', 'size')):
                raise RuntimeError('shm_replaced_during_attach')
        except BaseException:
            self.close()
            raise
        self.identity = {k: row.get(k) for k in ('key', 'shmid', 'uid', 'size', 'ctime')}

    def read(self):
        now = time.monotonic()
        if now >= self.next_identity_check:
            row = self.info(self.key, self.uid, self.size)
            if any(row.get(k) != self.identity.get(k) for k in ('shmid', 'ctime', 'uid', 'size')):
                raise RuntimeError('shm_replaced_during_stream')
            self.next_identity_check = now + 1.
        a = ctypes.string_at(self.ptr, self.size)
        b = ctypes.string_at(self.ptr, self.size)
        if a != b:
            return None
        return a[4:], int.from_bytes(a[:4], 'little')

    def close(self):
        if self.ptr is not None:
            ptr, self.ptr = self.ptr, None
            if self.libc.shmdt(ptr) != 0:
                raise OSError(ctypes.get_errno(), 'shmdt_failed')


def offer_latest(q, item):
    """Drop the new sample when full; never block the producer or grow memory."""
    try:
        q.put_nowait(item)
        return True
    except queue.Full:
        return False


def write_packet(fd, packet, stop, timeout=3.):
    deadline = time.monotonic() + timeout
    view = memoryview(packet)
    while view and not stop.is_set():
        if time.monotonic() > deadline:
            raise TimeoutError('transport_write_timeout')
        if not select.select([], [fd], [], .1)[1]:
            continue
        try:
            n = os.write(fd, view[:65536])
            if not n:
                raise BrokenPipeError('transport_closed')
            view = view[n:]
        except BlockingIOError:
            continue


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--authorize-readonly', action='store_true')
    p.add_argument('--camera', type=int, choices=range(1, 6), required=True)
    p.add_argument('--width', type=int, default=464)
    p.add_argument('--height', type=int, default=400)
    p.add_argument('--owner-uid', type=int)
    p.add_argument('--rate', type=float, default=10.)
    p.add_argument('--max-seconds', type=float, default=120.)
    p.add_argument('--read-timeout', type=float, default=3.)
    p.add_argument('--max-mbps', type=float, default=60.)
    p.add_argument('--queue-size', type=int, default=2)
    p.add_argument('--projection', choices=['unknown', 'raw_fisheye', 'rectified_pinhole'], default='unknown')
    args = p.parse_args(argv)
    if not args.authorize_readonly:
        p.error('explicit_readonly_authorization_required')
    if not (1 <= args.width <= 2048 and 1 <= args.height <= 2048
            and 0.2 <= args.rate <= 20 and 1 <= args.max_seconds <= 600
            and 1 <= args.queue_size <= 8 and 1 <= args.max_mbps <= 200
            and .1 <= args.read_timeout <= 30):
        p.error('invalid_resource_budget')
    demand = (args.width * args.height * 3 + 8192) * args.rate * 8 / 1e6
    if demand > args.max_mbps:
        p.error('bandwidth_budget_exceeded_reduce_sampling_rate_not_resolution')
    import fcntl
    import resource
    source = ReadonlyShm(9000 + args.camera, args.width, args.height, args.owner_uid)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    q = queue.Queue(args.queue_size)
    counts = {'read_attempts': 0, 'unstable_rejected': 0, 'queue_dropped': 0, 'sent': 0}
    failures = []
    session = uuid.uuid4().hex
    start = time.monotonic()
    progress = [start]

    def sample_loop():
        try:
            while not stop.is_set() and time.monotonic() - start < args.max_seconds:
                before = time.monotonic()
                counts['read_attempts'] += 1
                sample = source.read()
                progress[0] = time.monotonic()
                if sample is None:
                    counts['unstable_rejected'] += 1
                else:
                    data, flag = sample
                    usage = resource.getrusage(resource.RUSAGE_SELF)
                    h = make_header(session, args.camera, counts['read_attempts'], args.width, args.height,
                                    before, time.time(), projection=args.projection,
                                    shm_identity=source.identity, shm_flag_uninterpreted=flag,
                                    consistency='two_copies_equal_not_atomic', layout_trust='configured_requires_field_validation',
                                    counters=dict(counts), sampling_target_hz=args.rate,
                                    agent_cpu_s=usage.ru_utime + usage.ru_stime, agent_maxrss_kib=usage.ru_maxrss)
                    if not offer_latest(q, (h, data)):
                        counts['queue_dropped'] += 1
                stop.wait(max(0., 1. / args.rate - (time.monotonic() - before)))
        except (OSError, RuntimeError, ValueError) as exc:
            failures.append(str(exc))
        finally:
            stop.set()

    sampler = threading.Thread(target=sample_loop, name='readonly-sampler', daemon=True)
    fd = sys.stdout.fileno()
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    sampler.start()
    try:
        while not stop.is_set():
            now = time.monotonic()
            # The main thread must enforce these, not the possibly blocked reader.
            if now - progress[0] > max(args.read_timeout, 2. / args.rate):
                failures.append('sampler_read_timeout')
                break
            if now - start >= args.max_seconds:
                break
            try:
                h, data = q.get(timeout=.2)
            except queue.Empty:
                continue
            write_packet(fd, encode_frame(h, data), stop)
            counts['sent'] += 1
    finally:
        stop.set()
        sampler.join(timeout=.3)
        if sampler.is_alive():
            # Never unmap memory beneath an in-flight ctypes read. The daemon
            # belongs to this standalone process; OS process teardown detaches.
            failures.append('sampler_shutdown_timeout')
        else:
            source.close()
        print(json.dumps({'event': 'agent_exit', 'counters': counts, 'queued_at_exit': q.qsize(),
                          'session_id': session, 'sampler_errors': failures}), file=sys.stderr, flush=True)
    if failures:
        raise RuntimeError('readonly_sampler_failed:' + failures[0])
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - standalone process must return a failure exit code
        print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr, flush=True)
        sys.exit(2)
