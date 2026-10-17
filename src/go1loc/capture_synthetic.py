"""Explicit synthetic test source, not Go1 footage. No network or shared memory."""
import argparse
import sys
import time
import uuid

try:
    from .capture_wire import encode_frame, make_header
except ImportError:
    from capture_wire import encode_frame, make_header


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--camera', type=int, choices=range(1, 7), default=1)
    p.add_argument('--seconds', type=float, default=120.)
    p.add_argument('--rate', type=float, default=10.)
    args = p.parse_args(argv)
    session, start, seq = uuid.uuid4().hex, time.monotonic(), 0
    width, height = 464, 400
    while time.monotonic() - start < args.seconds:
        seq += 1
        # Byte-exact changing bars; synthetic provenance is in every packet.
        row = bytes(v for x in range(width) for v in ((x + seq) % 256, (x // 2) % 256, seq % 256))
        packet = encode_frame(make_header(session, args.camera, seq, width, height,
                              time.monotonic(), time.time(), source_kind='synthetic_test'), row * height)
        sys.stdout.buffer.write(packet)
        sys.stdout.buffer.flush()
        time.sleep(1. / args.rate)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        pass
