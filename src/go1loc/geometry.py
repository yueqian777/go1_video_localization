"""Coordinate conventions shared by mapping, localization, and object grounding."""

from __future__ import annotations

import numpy as np


def _matrix3(value: np.ndarray, name: str) -> np.ndarray:
    value = np.asarray(value, dtype=float)
    if value.shape != (3, 3):
        raise ValueError(f"{name} must have shape (3, 3), got {value.shape}")
    return value


def _vector3(value: np.ndarray, name: str) -> np.ndarray:
    value = np.asarray(value, dtype=float).reshape(-1)
    if value.shape != (3,):
        raise ValueError(f"{name} must have shape (3,), got {value.shape}")
    return value


def quaternion_wxyz_to_rotation(quaternion: np.ndarray) -> np.ndarray:
    """Convert a Hamilton quaternion in COLMAP's QW,QX,QY,QZ order to R."""

    q = np.asarray(quaternion, dtype=float).reshape(-1)
    if q.shape != (4,):
        raise ValueError("quaternion must contain [qw, qx, qy, qz]")
    norm = np.linalg.norm(q)
    if not np.isfinite(norm) or norm < 1e-12:
        raise ValueError("quaternion must be finite and non-zero")
    qw, qx, qy, qz = q / norm
    return np.array(
        [
            [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)],
            [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)],
            [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)],
        ],
        dtype=float,
    )


def colmap_world_to_camera_to_camera_to_world(
    rotation_cw: np.ndarray, translation_cw: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Invert COLMAP's X_C = R_CW X_W + t_CW into T_WC."""

    r_cw = _matrix3(rotation_cw, "rotation_cw")
    t_cw = _vector3(translation_cw, "translation_cw")
    r_wc = r_cw.T
    t_wc = -r_wc @ t_cw
    return r_wc, t_wc


def camera_center_from_colmap(rotation_cw: np.ndarray, translation_cw: np.ndarray) -> np.ndarray:
    """Return the camera center in world coordinates."""

    return colmap_world_to_camera_to_camera_to_world(rotation_cw, translation_cw)[1]


def project_points_pinhole(
    points_xyz: np.ndarray, *, fx: float, fy: float, cx: float, cy: float
) -> np.ndarray:
    points = np.asarray(points_xyz, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("points_xyz must have shape (N, 3)")
    if not all(np.isfinite(v) and v > 0 for v in (fx, fy)):
        raise ValueError("fx and fy must be positive finite values")
    if np.any(points[:, 2] <= 0):
        raise ValueError("all points must be in front of the camera")
    return np.column_stack((fx * points[:, 0] / points[:, 2] + cx, fy * points[:, 1] / points[:, 2] + cy))


def transform_points(rotation: np.ndarray, translation: np.ndarray, points: np.ndarray) -> np.ndarray:
    r = _matrix3(rotation, "rotation")
    t = _vector3(translation, "translation")
    p = np.asarray(points, dtype=float)
    if p.ndim != 2 or p.shape[1] != 3:
        raise ValueError("points must have shape (N, 3)")
    return (r @ p.T).T + t
