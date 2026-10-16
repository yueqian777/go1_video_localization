"""G1C1 wire contract; standard library only, compatible with board Python 3.6.

12-byte network-order prefix: magic, JSON-header length, header CRC32.
UTF-8 metadata is followed by raw packed BGR bytes (never base64).
CRC is corruption detection, not authentication: SSH supplies authentication.
"""
import json
import math
import re
import struct
import zlib

PREFIX = struct.Struct('!4sII')
MAX_HEADER = 8192
MAX_PAYLOAD = 8 * 1024 * 1024
SOURCE_KINDS = ('go1_shm_readonly', 'synthetic_test', 'orbbec_rgb_mjpeg')


class ProtocolError(ValueError):
    pass


def make_header(session, camera, seq, width, height, monotonic, wall,
                source_kind='go1_shm_readonly', projection='unknown', **extra):
    header = dict(version=1, message_type='frame', session_id=session,
                  camera_id=camera, agent_seq=seq, width=width, height=height,
                  channels=3, pixel_format='BGR8', stride=width * 3,
                  payload_length=width * height * 3,
                  agent_read_monotonic=monotonic, agent_read_wall_time=wall,
                  agent_wall_time_trust='unverified', sensor_capture_time=None,
                  original_stream_pts=None, source_kind=source_kind,
                  projection=projection, orientation='unverified',
                  agent_seq_meaning='read_attempt_not_sensor_exposure')
    header.update(extra)
    return header


def validate_header(h):
    if not isinstance(h, dict) or h.get('version') != 1 or h.get('message_type') != 'frame':
        raise ProtocolError('unsupported_protocol')
    if not isinstance(h.get('session_id'), str) or not re.fullmatch('[0-9a-f]{32}', h['session_id']):
        raise ProtocolError('invalid_session_id')
    for k, lo, hi in (('camera_id', 1, 6), ('width', 1, 2048), ('height', 1, 2048),
                      ('agent_seq', 0, 2**53), ('payload_length', 1, MAX_PAYLOAD)):
        if type(h.get(k)) is not int or not lo <= h[k] <= hi:
            raise ProtocolError('invalid_' + k)
    external = h.get('source_kind') == 'orbbec_rgb_mjpeg'
    if external and (h['camera_id'] != 6 or (h['width'], h['height']) != (1280, 720)
                     or h.get('camera_serial') != 'CP2B7530008K'
                     or h.get('source_encoding') != 'MJPEG'):
        raise ProtocolError('unexpected_rgb_identity_or_profile')
    if h['camera_id'] == 6 and not external and h.get('source_kind') != 'synthetic_test':
        raise ProtocolError('unexpected_camera_or_source')
    compressed = external and h.get('pixel_format') == 'MJPEG'
    if compressed:
        if h.get('channels') != 3 or h.get('stride') != 0 or h['payload_length'] > 2 * 1024 * 1024:
            raise ProtocolError('unsupported_jpeg_layout')
    elif (h.get('channels') != 3 or h.get('pixel_format') != 'BGR8'
            or h.get('stride') != h['width'] * 3
            or h['payload_length'] != h['stride'] * h['height']):
        raise ProtocolError('unsupported_pixel_layout')
    for k in ('agent_read_monotonic', 'agent_read_wall_time'):
        if type(h.get(k)) not in (int, float) or not math.isfinite(h[k]) or h[k] < 0:
            raise ProtocolError('invalid_' + k)
    if h.get('source_kind') not in SOURCE_KINDS:
        raise ProtocolError('unsupported_source_kind')
    if h.get('projection') not in ('unknown', 'raw_fisheye', 'rectified_pinhole'):
        raise ProtocolError('invalid_projection')
    if h.get('sensor_capture_time') is not None or h.get('agent_wall_time_trust') != 'unverified':
        raise ProtocolError('unsupported_exposure_or_clock_claim')
    crc = h.get('payload_crc32')
    if type(crc) is not int or not 0 <= crc <= 0xffffffff:
        raise ProtocolError('invalid_payload_crc')


def encode_frame(header, payload):
    header['payload_crc32'] = zlib.crc32(payload) & 0xffffffff
    validate_header(header)
    if len(payload) != header['payload_length']:
        raise ProtocolError('payload_length_mismatch')
    raw = json.dumps(header, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) > MAX_HEADER:
        raise ProtocolError('header_too_large')
    return PREFIX.pack(b'G1C1', len(raw), zlib.crc32(raw) & 0xffffffff) + raw + payload


def _exact(stream, count, clean_eof=False):
    chunks = bytearray()
    while len(chunks) < count:
        part = stream.read(count - len(chunks))
        if not part:
            if clean_eof and not chunks:
                return None
            raise ProtocolError('truncated_packet')
        chunks.extend(part)
    return bytes(chunks)


def _unique_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ProtocolError('duplicate_header_key')
        out[key] = value
    return out


def read_frame(stream):
    prefix = _exact(stream, PREFIX.size, clean_eof=True)
    if prefix is None:
        return None
    magic, size, crc = PREFIX.unpack(prefix)
    if magic != b'G1C1' or not 0 < size <= MAX_HEADER:
        raise ProtocolError('invalid_packet_prefix')
    raw = _exact(stream, size)
    if zlib.crc32(raw) & 0xffffffff != crc:
        raise ProtocolError('header_checksum_mismatch')
    try:
        header = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_pairs)
    except (UnicodeError, ValueError) as exc:
        raise ProtocolError('invalid_header_json') from exc
    validate_header(header)
    payload = _exact(stream, header['payload_length'])
    if zlib.crc32(payload) & 0xffffffff != header['payload_crc32']:
        raise ProtocolError('payload_checksum_mismatch')
    return header, payload
