"""Bin markers: ArUco codes (a sturdy kind of QR code, read reliably at 1.6 mm per cell).

DICT_4X4_250, ids 50-249 (50-99 on corner-post bins, 100-249 on shelf bins). The camera
calibration uses DICT_5X5_100 for its board and the first 50 codes of the 4x4 family for its
corner markers, so these never get confused with them.
A marker's top edge points to the back of its bin (+Y of the bin).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from . import binspec

DICTIONARY = "DICT_4X4_250"


def _dictionary():  # noqa: ANN202 - cv2 type
    import cv2

    return cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, DICTIONARY))


def marker_bits(marker: int) -> np.ndarray:
    """MARKER_CELLS x MARKER_CELLS booleans, True = black, row 0 = the top edge."""
    import cv2

    img = cv2.aruco.generateImageMarker(
        _dictionary(), int(marker), binspec.MARKER_CELLS, borderBits=1
    )
    return np.asarray(img) < 128


def marker_svg(marker: int, cell_mm: float = binspec.MARKER_CELL_MM, quiet_cells: int = 1) -> str:
    """One marker as an SVG group (mm units), with a white quiet zone and a thin cut outline."""
    bits = marker_bits(marker)
    n = bits.shape[0]
    size = (n + 2 * quiet_cells) * cell_mm
    q = quiet_cells * cell_mm
    rects = "".join(
        f'<rect x="{q + c * cell_mm:.3f}" y="{q + r * cell_mm:.3f}" width="{cell_mm:.3f}" '
        f'height="{cell_mm:.3f}"/>'
        for r in range(n)
        for c in range(n)
        if bits[r, c]
    )
    return (
        f'<rect width="{size:.3f}" height="{size:.3f}" fill="#fff" stroke="#999" stroke-width="0.1"/>'
        f'<g fill="#000">{rects}</g>'
    )


def labels_sheet_svg(markers: list[int], names: dict[int, str] | None = None) -> str:
    """A4 sheet of paper labels (print at 100 %, glue onto the shelf of a plain bin)."""
    names = names or {}
    cell = binspec.MARKER_CELL_MM
    tile = (binspec.MARKER_CELLS + 2) * cell
    pitch_x, pitch_y, cols = tile + 22.0, tile + 10.0, 7
    parts = []
    for i, m in enumerate(markers):
        x, y = 12 + (i % cols) * pitch_x, 14 + (i // cols) * pitch_y
        text = f"{m}" + (f" {names[m]}" if names.get(m) else "")
        parts.append(
            f'<g transform="translate({x:.2f},{y:.2f})">{marker_svg(m)}'
            f'<text x="0" y="{tile + 3.2:.2f}" font-size="2.6" font-family="sans-serif">'
            f"{_xml(text)}</text></g>"
        )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" viewBox="0 0 210 297">'
        '<text x="12" y="8" font-size="3.5" font-family="sans-serif">sortering_pnp bin markers: print at '
        "100 % (no scaling); each black cell must measure "
        f"{cell:g} mm</text>{''.join(parts)}</svg>"
    )


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass
class SeenMarker:
    marker: int
    corners_px: (
        np.ndarray
    )  # (4, 2) full-frame pixels: top-left, top-right, bottom-right, bottom-left

    @property
    def centre_px(self) -> np.ndarray:
        return self.corners_px.mean(axis=0)


def detect(image: np.ndarray) -> list[SeenMarker]:
    """All bin markers in a photo (BGR or grey)."""
    import cv2

    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    det = cv2.aruco.ArucoDetector(_dictionary(), params)
    corners, ids, _ = det.detectMarkers(gray)
    if ids is None:
        return []
    return [
        SeenMarker(int(i), np.asarray(c, dtype=float).reshape(4, 2))
        for c, i in zip(corners, ids.ravel(), strict=True)
        if int(i) in binspec.MARKER_IDS
    ]


@dataclass
class MarkerPose:
    marker: int
    centre: tuple[float, float]  # plate mm (at the marker's height)
    up_deg: float  # direction of the marker's top edge, plate frame
    view_px: tuple[float, float] | None = None  # its centre in the page's photo (Shot.view)


def marker_pose(seen: SeenMarker, to_plate: Callable[[np.ndarray], np.ndarray]) -> MarkerPose:
    """Plate position and direction of a marker; `to_plate` maps (N, 2) pixels to plate mm at the
    marker's height (the camera's ray intersection)."""
    p = np.asarray(to_plate(seen.corners_px))[:, :2]
    top = (p[0] + p[1]) / 2
    bottom = (p[2] + p[3]) / 2
    up = top - bottom
    c = p.mean(axis=0)
    return MarkerPose(
        seen.marker, (float(c[0]), float(c[1])), math.degrees(math.atan2(up[1], up[0]))
    )
