import io
import json
import struct
import unittest
import zlib

from go1loc.capture_wire import ProtocolError, encode_frame, make_header, read_frame


def sample(seq=1, camera=1, session='a' * 32):
    payload = bytes([0, 10, 255, 255, 20, 0]) * 2
    header = make_header(session, camera, seq, 2, 2, seq / 10., 1000. + seq,
                         source_kind='synthetic_test')
    return header, payload


class Partial(io.BytesIO):
    def read(self, n=-1):
        return super().read(min(n, 3))


class WireTests(unittest.TestCase):
    def test_partial_and_concatenated(self):
        h, p = sample()
        stream = Partial(encode_frame(h, p) + encode_frame(*sample(2)))
        self.assertEqual(read_frame(stream), (h, p))
        self.assertEqual(read_frame(stream)[0]['agent_seq'], 2)
        self.assertIsNone(read_frame(stream))

    def test_truncated_and_checksum(self):
        packet = encode_frame(*sample())
        for broken in (packet[:3], packet[:-1], packet[:-1] + bytes([packet[-1] ^ 1])):
            with self.subTest(length=len(broken)), self.assertRaises(ProtocolError):
                read_frame(io.BytesIO(broken))

    def test_bad_magic_and_length(self):
        for data in (struct.pack('!4sII', b'BAD!', 1, 0),
                     struct.pack('!4sII', b'G1C1', 2**31, 0)):
            with self.assertRaises(ProtocolError):
                read_frame(io.BytesIO(data))

    def test_metadata_rejected_before_payload(self):
        for key, value in [('pixel_format', 'YUV420'), ('stride', 1), ('width', 99999),
                           ('sensor_capture_time', 123), ('agent_seq', -1),
                           ('agent_read_monotonic', float('nan')), ('payload_length', 99)]:
            h, p = sample()
            encode_frame(h, p)
            h[key] = value
            raw = json.dumps(h).encode()
            packet = struct.pack('!4sII', b'G1C1', len(raw), zlib.crc32(raw)) + raw
            with self.subTest(field=key), self.assertRaises(ProtocolError):
                read_frame(io.BytesIO(packet))


if __name__ == '__main__':
    unittest.main()
