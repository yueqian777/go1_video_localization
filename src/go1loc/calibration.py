"""Authenticated camera calibration and fisheye rectification metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np


class CalibrationError(ValueError):
    pass


@dataclass(frozen=True)
class Calibration:
    source: str
    model: str
    image_size: tuple[int, int]
    K: list[list[float]]
    distortion: list[float]
    output_size: tuple[int, int]
    output_K: list[list[float]] | None
    map_version: str
    valid_mask_path: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def load_calibration(path: Path) -> Calibration:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationError("calibration_file_unreadable") from exc
    required = {"source", "model", "image_size", "K", "distortion", "output_size", "map_version"}
    missing = sorted(required - set(data))
    if missing:
        raise CalibrationError("calibration_missing:" + ",".join(missing))
    if str(data["source"]).lower() in {"invented", "placeholder", "unknown"}:
        raise CalibrationError("calibration_source_not_authenticated")
    K = np.asarray(data["K"], dtype=float)
    if K.shape != (3, 3) or not np.isfinite(K).all():
        raise CalibrationError("invalid_intrinsic_matrix")
    if K[0, 0] <= 0 or K[1, 1] <= 0:
        raise CalibrationError("invalid_focal_length")
    image_size = tuple(int(v) for v in data["image_size"])
    output_size = tuple(int(v) for v in data["output_size"])
    if len(image_size) != 2 or len(output_size) != 2 or min(*image_size, *output_size) <= 0:
        raise CalibrationError("invalid_image_size")
    return Calibration(
        source=str(data["source"]),
        model=str(data["model"]),
        image_size=image_size,
        K=K.tolist(),
        distortion=[float(v) for v in data["distortion"]],
        output_size=output_size,
        output_K=data.get("output_K"),
        map_version=str(data["map_version"]),
        valid_mask_path=data.get("valid_mask_path"),
    )


def rectify_points(points_xy: np.ndarray, calibration: Calibration) -> np.ndarray:
    """Rectify coordinates with the authenticated model; requires OpenCV."""

    try:
        import cv2
    except ImportError as exc:
        raise CalibrationError("opencv_not_available_for_rectification") from exc
    points = np.asarray(points_xy, dtype=np.float32).reshape(-1, 1, 2)
    K = np.asarray(calibration.K, dtype=np.float64)
    D = np.asarray(calibration.distortion, dtype=np.float64)
    out_K = np.asarray(calibration.output_K or calibration.K, dtype=np.float64)
    if "fisheye" in calibration.model.lower():
        result = cv2.fisheye.undistortPoints(points, K, D, P=out_K)
    else:
        result = cv2.undistortPoints(points, K, D, P=out_K)
    return result.reshape(-1, 2)


def build_rectification(calibration: Calibration) -> dict:
    """Create nonlinear lookup maps and a conservative all-valid central crop."""
    import cv2
    K = np.asarray(calibration.K, dtype=np.float64)
    D = np.asarray(calibration.distortion, dtype=np.float64)
    out_K = np.asarray(calibration.output_K or calibration.K, dtype=np.float64)
    if not np.isfinite(D).all() or not np.isfinite(out_K).all() or out_K.shape != (3,3):
        raise CalibrationError('invalid_rectification_parameters')
    if calibration.model.lower() in {'opencv_fisheye','fisheye'}:
        if D.size != 4:
            raise CalibrationError('fisheye_requires_four_distortion_parameters')
        mx,my = cv2.fisheye.initUndistortRectifyMap(K,D,np.eye(3),out_K,calibration.output_size,cv2.CV_32FC1)
    elif calibration.model.lower() in {'pinhole','opencv'}:
        mx,my = cv2.initUndistortRectifyMap(K,D,None,out_K,calibration.output_size,cv2.CV_32FC1)
    else:
        raise CalibrationError('unsupported_calibration_model')
    sw,sh = calibration.image_size
    mask = ((mx>=0)&(my>=0)&(mx<sw-1)&(my<sh-1)).astype(np.uint8)*255
    h,w = mask.shape
    crop = None
    for margin in range(0,min(w,h)//2):
        if np.all(mask[margin:h-margin or None,margin:w-margin or None]):
            crop = [margin,margin,w-2*margin,h-2*margin]
            break
    if crop is None or min(crop[2:])<16:
        raise CalibrationError('insufficient_valid_rectified_region')
    cropped_K = out_K.copy()
    cropped_K[0,2] -= crop[0]
    cropped_K[1,2] -= crop[1]
    return {'map_x':mx,'map_y':my,'valid_mask':mask,'crop_xywh':crop,'output_K':cropped_K}
