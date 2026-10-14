"""Query rectification follows video.py's lookup maps and crop, retaining raw ROI coordinates."""
from pathlib import Path
import shutil
import numpy as np
from PIL import Image
from .calibration import load_calibration, build_rectification
from .video import sha256_file


def prepare_query(photo: Path, workspace: Path, calibration_path: Path | None):
    """Return a workspace-local algorithm image, NOT a new observation identity.

    Callers bind the original photo bytes separately via make_binding(photo, ...).
    The fixed rectified basename must never establish cross-query equality.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    with Image.open(photo) as image:
        size = list(image.size)
        raw = np.asarray(image.convert('RGB'))
    if calibration_path is None:
        target = workspace/photo.name
        if target.resolve() != photo.resolve():
            shutil.copyfile(photo, target)
        return target, {'status': 'original', 'source_size': size, 'output_size': size,
                        'roi_coordinate_space': 'original_zero_based_pixel_centers'}, None
    import cv2
    calibration = load_calibration(calibration_path)
    if tuple(size) != calibration.image_size:
        raise ValueError('query_calibration_image_size_mismatch')
    rect = build_rectification(calibration)
    processed = cv2.remap(raw, rect['map_x'], rect['map_y'], cv2.INTER_LINEAR)
    x, y, w, h = rect['crop_xywh']
    target = workspace/'query_rectified.png'
    Image.fromarray(processed[y:y+h, x:x+w]).save(target)
    geometry = {'status': 'calibrated_rectified', 'source_size': size, 'output_size': [w, h],
                'output_K': rect['output_K'].tolist(), 'crop_xywh': rect['crop_xywh'],
                'calibration': calibration.to_dict(), 'calibration_sha256': sha256_file(calibration_path),
                'roi_coordinate_space': 'original_zero_based_pixel_centers'}
    return target, geometry, rect


def original_coordinates(uv, rect):
    uv = np.asarray(uv, dtype=np.float32).reshape(-1, 2)
    if rect is None or not len(uv):
        return uv
    import cv2
    # Inverse image lookup, not a transformed rectangular bounding box.
    mx = (uv[:, 0] + rect['crop_xywh'][0]).reshape(1, -1)
    my = (uv[:, 1] + rect['crop_xywh'][1]).reshape(1, -1)
    return np.column_stack([cv2.remap(rect[key], mx, my, cv2.INTER_LINEAR).ravel()
                            for key in ('map_x', 'map_y')])
