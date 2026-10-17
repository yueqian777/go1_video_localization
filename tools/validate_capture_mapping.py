"""Explicit public-photo replay through capture/PNG/import/real hloc mapping.

Not a robot recording or continuous camera trajectory. No network access.
Existing public photos are resized solely to create a bounded SYNTHETIC fixture;
the real SHM agent never resizes. Use a NEW output directory for each run.
"""
# ruff: noqa: E402 -- this standalone harness adds the source tree before imports.
import argparse
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src')]

import numpy as np
from PIL import Image

from go1loc.capture_import import prepare_capture
from go1loc.capture_session import Recorder, SourceFrame, utc_now, write_json
from go1loc.capture_wire import encode_frame, make_header, read_frame
from go1loc.mapping import MappingConfig, build_map
from go1loc.video import FrameConfig, sha256_file


def main():
    import io
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--images', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    images = sorted(p for p in args.images.iterdir() if p.suffix.lower() in ('.jpg', '.png'))[:8]
    if len(images) != 8:
        raise ValueError('eight_public_photos_required')
    stream = uuid.uuid4().hex
    provenance = []
    def make(file, seq):
        with Image.open(file) as im:
            im = im.convert('RGB')
            im.thumbnail((1000, 1000))
            rgb = np.asarray(im)
        h, w = rgb.shape[:2]
        header = make_header(stream, 1, seq, w, h, time.monotonic(), time.time(), source_kind='synthetic_test',
                             fixture_note='resized_public_photo_replay_not_a_continuous_route',
                             original_photo_sha256=sha256_file(file))
        packet = encode_frame(header, rgb[:, :, ::-1].tobytes())
        header, payload = read_frame(io.BytesIO(packet))
        provenance.append(dict(file=file.name, sha256=header['original_photo_sha256'],
                               agent_seq=seq, purpose='preview_boundary' if seq == 0 else 'recorded_fixture_frame'))
        return SourceFrame(header, payload, utc_now(), time.monotonic())
    recorder = Recorder(args.output / 'capture', make(images[0], 0))
    for i, file in enumerate(images, 1):
        recorder.submit(make(file, i))
        recorder.queue.join()
    recorder.finish()
    prepared = prepare_capture(args.output / 'capture', args.output / 'prepared',
                               FrameConfig(interval_s=.001, max_frames=8, quality_filter=False))
    # Unordered public-photo fixture is NEVER continuous route evidence.
    for row in prepared['frames']:
        row['discontinuity_before'] = True
    write_json(args.output / 'prepared/frames_manifest.json', prepared)
    manifest = build_map(args.output / 'prepared/images', args.output / 'map',
                         MappingConfig(map_id='synthetic-capture-public-replay', max_keypoints=2048,
                                       snapshot_frames_freq=2, snapshot_max_count=10))
    write_json(args.output / 'validation.json', dict(source_kind='synthetic_test', original_photos=provenance,
               capture_frames=recorder.written, prepared_frames=prepared['kept_count'],
               registered_images=manifest['num_registered_images'], points3D=manifest['num_points3D'],
               map_manifest_sha256=manifest['manifest_sha256'], timing_source=manifest['timing_source'],
               execution_permitted=False, real_go1_acceptance=False))
    print('Synthetic public-photo capture -> existing mapping validation completed')


if __name__ == '__main__':
    main()
