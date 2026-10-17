"""Decode bounded RGB MJPEG on the PC, never on the robot or in the UI writer."""
import io
import time
import zlib

import numpy as np
from PIL import Image, UnidentifiedImageError

from .capture_session import SourceFrame, utc_now
from .capture_wire import validate_header


def decode_rgb_frame(header, payload):
    validate_header(header)
    if header['source_kind'] != 'orbbec_rgb_mjpeg' or header['pixel_format'] != 'MJPEG':
        raise ValueError('expected_rgb_mjpeg')
    if len(payload) != header['payload_length'] or zlib.crc32(payload) & 0xffffffff != header['payload_crc32']:
        raise ValueError('jpeg_payload_mismatch')
    received, wall = time.monotonic(), utc_now()
    try:
        with Image.open(io.BytesIO(payload)) as image:
            # Check dimensions before allocating decoded pixels.
            if image.format != 'JPEG' or image.size != (1280, 720):
                raise ValueError('jpeg_size_or_encoding_mismatch')
            bgr = np.asarray(image.convert('RGB'))[:, :, ::-1].tobytes()
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError('invalid_rgb_jpeg') from exc
    canonical = dict(header, pixel_format='BGR8', stride=1280 * 3, payload_length=len(bgr),
                     payload_crc32=zlib.crc32(bgr) & 0xffffffff,
                     source_jpeg_bytes=len(payload), source_jpeg_crc32=header['payload_crc32'],
                     pixel_provenance='PC_decoded_camera_lossy_MJPEG_no_resize')
    return SourceFrame(canonical, bgr, wall, received, preview_jpeg=payload)
