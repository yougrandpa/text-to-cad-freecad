"""Orthographic camera for headless software rendering.

FreeCAD cannot rasterise headlessly (every raster path lives in ``src/Gui`` and
``FreeCAD.GuiUp == False``), so the worker hands us a triangle mesh and *we*
project + rasterise it in the supervisor with numpy. This module owns the
projection maths.

Why the maths is commented heavily: an orthographic camera that silently
mirrors or swaps an axis produces plausible-but-wrong images — the model would
"see what it drew" inverted. Every view direction, right/up basis and the
margin-aware fit below is written out explicitly so a wrong sign cannot hide.

Convention (FreeCAD-style, Z up):
  - ``front``: camera sits on -Y looking toward +Y. Screen right = +X, up = +Z.
    Shows the X-Z plane.
  - ``top``  : camera sits on +Z looking toward -Z. Screen right = +X, up = +Y.
    Shows the X-Y plane (the footprint).
  - ``right``: camera sits on +X looking toward -X. Screen right = +Y, up = +Z.
    Shows the Y-Z plane.
  - ``iso``  : camera at (1,1,1) looking at the origin (standard isometric).

Every view's basis is right-handed: ``right x up == look``. That invariant is
asserted in the tests, because a left-handed basis silently mirrors the image —
and a mirrored view is misinformation, not a cosmetic defect.

A point's *depth* is ``dot(point - center, look)`` where ``look`` points from
the model centre toward the camera. Larger depth == closer to the camera, so
the z-buffer keeps the **maximum** depth (closest surface wins).
"""

from __future__ import annotations

from typing import get_args

import numpy as np

from tcad.core.types import BBox, ViewName

# The Literal's members, used for runtime validation (a Literal cannot be
# instantiated, so `ViewName(x)` is a TypeError).
_VIEW_NAMES: frozenset[str] = frozenset(get_args(ViewName))

# World "up" used as the anchor when deriving the screen-up basis for iso.
_WORLD_UP = np.array([0.0, 0.0, 1.0], dtype=float)


class OrthoCamera:
    """Orthographic camera with an explicit (right, up, look) basis.

    Not a pydantic model on purpose: it carries numpy arrays, and pydantic
    would force ``arbitrary_types_allowed`` everywhere it is nested. The
    projection is plain numeric code, so a plain class is the right tool.
    """

    def __init__(
        self,
        view: ViewName,
        right: np.ndarray,
        up: np.ndarray,
        look: np.ndarray,
        center: np.ndarray,
        margin_ratio: float = 0.08,
    ) -> None:
        if view not in _VIEW_NAMES:
            raise ValueError(f"unknown view {view!r}; expected one of {sorted(_VIEW_NAMES)}")
        self.view = view
        # Orthonormal basis (unit vectors).
        self.right = np.asarray(right, dtype=float) / np.linalg.norm(right)
        self.up = np.asarray(up, dtype=float) / np.linalg.norm(up)
        self.look = np.asarray(look, dtype=float) / np.linalg.norm(look)
        self.center = np.asarray(center, dtype=float)
        self.margin_ratio = float(margin_ratio)
        # Filled by ``fit``.
        self.width = 1
        self.height = 1
        self.scale = 1.0  # pixels per world unit

    def fit(self, bbox: BBox, width: int, height: int) -> "OrthoCamera":
        """Scale + centre so the whole bbox is in frame with ``margin_ratio``.

        ``width``/``height`` are the *target* pixel dimensions (callers pass the
        supersampled size when anti-aliasing). Computes the model extent
        projected onto the screen right/up axes from the 8 bbox corners, then
        picks the smaller scale so both axes fit inside the available area
        after reserving the margin on every side.
        """
        self.width = int(width)
        self.height = int(height)

        # BBox carries axis-aligned lengths (x=XLength, y=YLength, z=ZLength)
        # and the min corner (x_min, y_min, z_min) — see types.BBox / design
        # §7.5 (populated from shape.BoundBox).
        x0, y0, z0 = bbox.x_min, bbox.y_min, bbox.z_min
        x1 = x0 + bbox.x
        y1 = y0 + bbox.y
        z1 = z0 + bbox.z
        corners = np.array(
            [
                [x0, y0, z0], [x1, y0, z0], [x0, y1, z0], [x1, y1, z0],
                [x0, y0, z1], [x1, y0, z1], [x0, y1, z1], [x1, y1, z1],
            ],
            dtype=float,
        )
        rel = corners - self.center
        # Full extent (diameter) along each screen axis.
        ext_r = 2.0 * float(np.max(np.abs(rel @ self.right)))
        ext_u = 2.0 * float(np.max(np.abs(rel @ self.up)))
        # A flat (degenerate-in-view) model would divide by zero; guard it.
        ext_r = max(ext_r, 1e-9)
        ext_u = max(ext_u, 1e-9)

        avail_w = max(self.width * (1.0 - 2.0 * self.margin_ratio), 1.0)
        avail_h = max(self.height * (1.0 - 2.0 * self.margin_ratio), 1.0)
        self.scale = min(avail_w / ext_r, avail_h / ext_u)
        return self

    def project(self, vertices: np.ndarray) -> np.ndarray:
        """Project world vertices to float pixel coords (x right, y down).

        Returns an ``(N, 2)`` float array. Invalid input (wrong ndim/shape)
        yields an all-zero array of the right row count so callers can still
        build a blank image without raising.
        """
        v = np.asarray(vertices, dtype=float)
        if v.ndim != 2 or v.shape[1] != 3:
            n = v.shape[0] if v.ndim == 2 else 0
            return np.zeros((n, 2), dtype=float)
        rel = v - self.center
        sx = rel @ self.right * self.scale + self.width / 2.0
        # Image y grows downward, so +up maps to a *smaller* row index.
        sy = self.height / 2.0 - (rel @ self.up) * self.scale
        return np.stack([sx, sy], axis=1)

    def depth(self, vertices: np.ndarray) -> np.ndarray:
        """Per-vertex depth along the view axis (larger == closer to camera)."""
        v = np.asarray(vertices, dtype=float)
        if v.ndim != 2 or v.shape[1] != 3:
            return np.zeros((v.shape[0] if v.ndim == 2 else 0,), dtype=float)
        return (v - self.center) @ self.look


def camera_for(
    view: ViewName, bbox: BBox, margin_ratio: float = 0.08
) -> OrthoCamera:
    """Build a standard-view orthographic camera framed on ``bbox``.

    The camera basis is derived explicitly per view (see module docstring for
    the axis map). ``iso`` derives its right/up from a (1,1,1) look direction so
    it is guaranteed orthonormal rather than hand-typed.
    """
    # `ViewName` is a typing.Literal, not an enum — Literal(...) cannot be
    # instantiated (`TypeError: Cannot instantiate typing.Literal`). Validate
    # against its members instead, so a typo fails loudly here rather than
    # silently falling through to the iso branch.
    if view not in _VIEW_NAMES:
        raise ValueError(f"unknown view {view!r}; expected one of {sorted(_VIEW_NAMES)}")
    center = np.array(
        [
            bbox.x_min + bbox.x / 2.0,
            bbox.y_min + bbox.y / 2.0,
            bbox.z_min + bbox.z / 2.0,
        ],
        dtype=float,
    )

    if view == "front":
        # Camera on -Y looking toward +Y (FreeCAD's Front view convention).
        # Putting the camera on +Y instead would make (right, up, look)
        # left-handed — right x up = -look — and the projection would come out
        # MIRRORED: an asymmetric part would be shown to the model as its own
        # reflection. Only the front view was affected; top/right/iso were
        # already right-handed.
        look = np.array([0.0, -1.0, 0.0])
        right = np.array([1.0, 0.0, 0.0])  # +X → screen right
        up = np.array([0.0, 0.0, 1.0])     # +Z → screen up
    elif view == "top":
        look = np.array([0.0, 0.0, 1.0])   # camera on +Z, looking down
        right = np.array([1.0, 0.0, 0.0])  # +X → screen right
        up = np.array([0.0, 1.0, 0.0])     # +Y → screen up (footprint)
    elif view == "right":
        look = np.array([1.0, 0.0, 0.0])   # camera on +X, looking toward -X
        right = np.array([0.0, 1.0, 0.0])  # +Y → screen right
        up = np.array([0.0, 0.0, 1.0])     # +Z → screen up
    elif view == "iso":
        look = np.array([1.0, 1.0, 1.0])   # camera at (+,+,+) octant
        look = look / np.linalg.norm(look)
        # right = up_world × look  (keeps +X-ish to the right)
        right = np.cross(_WORLD_UP, look)
        right = right / np.linalg.norm(right)
        # up = look × right  (orthonormal completion)
        up = np.cross(look, right)
        up = up / np.linalg.norm(up)
    else:
        raise ValueError(f"unknown view {view!r}; expected iso/front/top/right")

    return OrthoCamera(view, right, up, look, center, margin_ratio)
