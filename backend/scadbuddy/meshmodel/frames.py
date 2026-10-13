"""Rotations and frames for a scene (spec 2026-10-11 §3.1, §5).

An object is ``translate(t) rotate(r) scale(s)`` of its mesh, as OpenSCAD applies them:
the linear part is ``M = R·S`` with ``R = Rz·Ry·Rx``. A world plane (point ``p``,
normal ``n``) is the object-frame plane ``M⁻¹(p - t)``, ``Mᵀn`` renormalised: a
plane's normal goes by the inverse-transpose, which only differs once the scale is not
uniform. All arguments are plain floats."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

_Z = np.array([0.0, 0.0, 1.0])


def unit(v: Sequence[float] | np.ndarray) -> np.ndarray:
    array = np.asarray(v, dtype=float)
    length = float(np.linalg.norm(array))
    if length < 1e-12:
        raise ValueError("a direction of zero length has no orientation")
    return np.asarray(array / length, dtype=float)


def _rx(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def _ry(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def _rz(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def rotation(degrees: Sequence[float]) -> np.ndarray:
    """OpenSCAD's ``rotate([x, y, z])``: about X, then Y, then Z."""
    x, y, z = (math.radians(d) for d in degrees)
    return np.asarray(_rz(z) @ _ry(y) @ _rx(x), dtype=float)


def linear(rotate: Sequence[float], scale: Sequence[float]) -> np.ndarray:
    return np.asarray(rotation(rotate) @ np.diag(np.asarray(scale, dtype=float)), dtype=float)


def to_object_plane(
    translate: Sequence[float],
    rotate: Sequence[float],
    scale: Sequence[float],
    point: Sequence[float],
    normal: Sequence[float],
) -> tuple[np.ndarray, np.ndarray]:
    try:
        n = unit(normal)
    except ValueError:
        raise ValueError("the plane's normal is zero") from None
    m = linear(rotate, scale)
    local_point = np.linalg.solve(
        m, np.asarray(point, dtype=float) - np.asarray(translate, dtype=float)
    )
    return local_point, unit(m.T @ n)


def normal_to_world(
    rotate: Sequence[float], scale: Sequence[float], normal: Sequence[float] | np.ndarray
) -> np.ndarray:
    """An object-frame face normal as it faces in the world."""
    return unit(np.linalg.inv(linear(rotate, scale)).T @ np.asarray(normal, dtype=float))


def frame(direction: Sequence[float] | np.ndarray) -> np.ndarray:
    """The rotation taking +Z onto ``direction``."""
    d = unit(direction)
    c = float(_Z @ d)
    if c > 1 - 1e-12:
        return np.eye(3)
    if c < -1 + 1e-12:
        return np.diag([1.0, -1.0, -1.0])  # a half turn about X
    v = np.cross(_Z, d)
    s = float(np.linalg.norm(v))
    k = v / s
    skew = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.asarray(np.eye(3) + s * skew + (1 - c) * (skew @ skew), dtype=float)


def turn_to_down(direction: Sequence[float] | np.ndarray) -> np.ndarray:
    """The rotation taking ``direction`` onto -Z."""
    return np.asarray(np.diag([1.0, -1.0, -1.0]) @ frame(direction).T, dtype=float)


def euler(r: np.ndarray) -> tuple[float, float, float]:
    """The ``rotate([x, y, z])`` degrees whose rotation is ``r`` (Rz·Ry·Rx)."""
    y = math.asin(max(-1.0, min(1.0, -float(r[2, 0]))))
    if abs(math.cos(y)) > 1e-9:
        x = math.atan2(float(r[2, 1]), float(r[2, 2]))
        z = math.atan2(float(r[1, 0]), float(r[0, 0]))
    else:  # gimbal lock: only x + z (or x - z) is fixed; take x = 0
        x = 0.0
        z = math.atan2(-float(r[0, 1]), float(r[1, 1]))
    return (round(math.degrees(x), 9), round(math.degrees(y), 9), round(math.degrees(z), 9))
