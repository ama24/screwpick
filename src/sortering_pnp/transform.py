"""Plate millimetres (camera calibration frame) <-> printer X/Y (G-code frame of the bed).

The plate frame is fixed to the bed, so one 2D similarity (rotation, scale, shift) maps it to
the printer's bed coordinates, whatever the kinematics (moving bed or moving head). It is fitted
from point pairs: a marker seen by the camera, and the tool tip jogged onto that marker.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .io import read_json, write_json


@dataclass
class Transform2D:
    A: np.ndarray  # 2x2: rotation times scale
    t: np.ndarray  # 2

    @classmethod
    def identity(cls) -> Transform2D:
        return cls(np.eye(2), np.zeros(2))

    def apply(self, pts: Sequence[float] | np.ndarray) -> np.ndarray:
        p = np.asarray(pts, dtype=float)
        return p @ self.A.T + self.t

    def inverse(self) -> Transform2D:
        Ai = np.linalg.inv(self.A)
        return Transform2D(Ai, -Ai @ self.t)

    @property
    def angle_deg(self) -> float:
        return math.degrees(math.atan2(self.A[1, 0], self.A[0, 0]))

    @property
    def scale(self) -> float:
        return float(math.sqrt(abs(np.linalg.det(self.A))))

    def rotate_angle(self, plate_deg: float) -> float:
        """A direction angle in the plate frame -> the same direction in printer X/Y (deg)."""
        return plate_deg + self.angle_deg

    def to_dict(self) -> dict[str, Any]:
        return {"A": self.A.tolist(), "t": self.t.tolist()}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Transform2D:
        return cls(np.asarray(d["A"], dtype=float), np.asarray(d["t"], dtype=float))


def fit_similarity(
    src: Sequence[Sequence[float]], dst: Sequence[Sequence[float]], allow_scale: bool = True
) -> tuple[Transform2D, np.ndarray]:
    """Least-squares similarity src -> dst (Umeyama), and the residual of each pair (mm)."""
    s = np.asarray(src, dtype=float).reshape(-1, 2)
    d = np.asarray(dst, dtype=float).reshape(-1, 2)
    if len(s) != len(d) or len(s) < 2:
        raise ValueError("need at least 2 point pairs (3 or more to check the fit)")
    ms, md = s.mean(0), d.mean(0)
    sc, dc = s - ms, d - md
    cov = dc.T @ sc / len(s)
    U, S, Vt = np.linalg.svd(cov)
    D = np.diag([1.0, 1.0 if np.linalg.det(U @ Vt) > 0 else -1.0])
    R = U @ D @ Vt
    var = (sc**2).sum() / len(s)
    k = float(np.trace(np.diag(S) @ D) / var) if allow_scale and var > 0 else 1.0
    A = k * R
    t = md - A @ ms
    T = Transform2D(A, t)
    return T, np.linalg.norm(T.apply(s) - d, axis=1)


MIN_SPREAD_MM = 20.0  # the tip positions of a camera-to-printer fit: at least this far apart
MIN_SCALE, MAX_SCALE = 0.5, 2.0  # camera mm per printer mm (about 1; off when the height is)


@dataclass
class BedCalibration:
    """The fitted transform plus the pairs it came from (kept so points can be added later)."""

    transform: Transform2D
    pairs: list[dict[str, Any]]  # {"marker": id, "plate": [x, y], "bed": [x, y]}
    residuals_mm: list[float]

    @classmethod
    def fit(cls, pairs: list[dict[str, Any]], allow_scale: bool = True) -> BedCalibration:
        """ValueError when the points cannot give a fit: the tip at (nearly) the same place for
        every code, or a scale no camera gives (camera mm and printer mm are close to 1:1)."""
        bed = np.asarray([p["bed"] for p in pairs], dtype=float).reshape(-1, 2)
        spread = float(np.ptp(bed, axis=0).max()) if len(bed) else 0.0
        if spread < MIN_SPREAD_MM:
            raise ValueError(
                f"the tip was within {spread:.0f} mm for every code: jog it onto each code "
                "before its 'Tip is here' (Forget points, then again)"
            )
        T, res = fit_similarity([p["plate"] for p in pairs], [p["bed"] for p in pairs], allow_scale)
        if not MIN_SCALE <= T.scale <= MAX_SCALE:
            raise ValueError(
                f"the camera's millimetres are {T.scale:.2f} x the printer's: the tip was not on "
                "the codes it was recorded for, or the camera's height is far off "
                "(Forget points, then again)"
            )
        return cls(T, pairs, [round(float(r), 3) for r in res])

    def save(self, path: Path) -> None:
        write_json(
            path,
            {
                "transform": self.transform.to_dict(),
                "angle_deg": round(self.transform.angle_deg, 4),
                "scale": round(self.transform.scale, 6),
                "pairs": self.pairs,
                "residuals_mm": self.residuals_mm,
            },
        )

    @classmethod
    def load(cls, path: Path) -> BedCalibration | None:
        if not path.is_file():
            return None
        d = read_json(path)
        cal = cls(
            Transform2D.from_dict(d["transform"]), d.get("pairs", []), d.get("residuals_mm", [])
        )
        return cal if MIN_SCALE <= cal.transform.scale <= MAX_SCALE else None  # unusable: none
