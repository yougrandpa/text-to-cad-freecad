"""Vectorised software rasteriser (numpy z-buffer).

The worker sends us a triangle mesh (``TopoShape.tessellate`` headlessly); this
module turns it into RGB images for the four standard views. It must be a
*real* rasteriser — one that works because the maths is right, not because it
special-cases cubes.

Pipeline per view:
  1. build an :class:`OrthoCamera` and ``fit`` it to the supersampled size;
  2. project vertices to pixels;
  3. for each triangle, take its pixel bounding box and run a **vectorised**
     barycentric coverage + depth test over those pixels (no Python
     pixel-by-pixel loop; we loop over triangles, which are few);
  4. detect edges from the facet-id / depth buffers (not polygon outlines);
  5. composite the chosen style and box-downsample the supersample buffer.

Robustness: empty meshes, degenerate (zero-area) triangles, a single-facet
mesh and malformed arrays all return a blank-but-valid image instead of
raising. Supersample and dimensions are clamped against absurd inputs.
"""

from __future__ import annotations

import numpy as np

from tcad.core.types import Mesh, RenderStyle, ViewName
from tcad.render.camera import camera_for
from tcad.render.contract import CONTRACT

# Fixed directional light for flat shading (points up-and-toward +X/+Y).
_LIGHT = np.array([0.4, 0.5, 0.85], dtype=float)
_LIGHT = _LIGHT / np.linalg.norm(_LIGHT)

# Colour used for silhouette / crease lines.
_EDGE_RGB = np.array(CONTRACT["edge"], dtype=np.uint8)
_BACKGROUND_RGB = np.array([255, 255, 255], dtype=np.uint8)

_MAX_DIM = 8192
_MAX_SUPERSAMPLE = 8


def render_views(
    mesh: Mesh,
    views: list[ViewName],
    width: int,
    height: int,
    style: RenderStyle = "flat_edges",
    supersample: int = 2,
) -> dict[ViewName, np.ndarray]:
    """Render ``mesh`` into one RGB ``(H, W, 3)`` uint8 array per view.

    Returns a dict keyed by view name. ``supersample`` renders at Nx then
    box-downsamples for anti-aliasing.
    """
    width = int(min(max(int(width), 1), _MAX_DIM))
    height = int(min(max(int(height), 1), _MAX_DIM))
    supersample = int(min(max(int(supersample), 1), _MAX_SUPERSAMPLE))

    verts, facets = _mesh_arrays(mesh)

    out: dict[ViewName, np.ndarray] = {}
    for view in views:
        groups = mesh.facet_groups if len(mesh.facet_groups) == len(facets) else None
        buf = _render_one(verts, facets, mesh.bbox, view, width, height, supersample, style, groups)
        out[view] = buf

    if supersample > 1:
        out = {v: _downsample(arr, width, height, supersample) for v, arr in out.items()}
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────

def _mesh_arrays(mesh: Mesh):
    """Extract a clean (V,3) float vertex array and (F,3) int facet array.

    Defensive against: missing data, wrong shapes, out-of-range indices and
    non-finite values. Returns empty arrays (preserving ndim) on any problem.
    """
    try:
        if mesh.vertices:
            verts = np.asarray(mesh.vertices, dtype=float)
        else:
            verts = np.zeros((0, 3), dtype=float)
        if verts.ndim != 2 or verts.shape[1] != 3 or not np.isfinite(verts).all():
            verts = np.zeros((0, 3), dtype=float)

        if mesh.facets:
            facets = np.asarray(mesh.facets, dtype=np.int64)
        else:
            facets = np.zeros((0, 3), dtype=np.int64)
        if facets.ndim != 2 or facets.shape[1] != 3:
            facets = np.zeros((0, 3), dtype=np.int64)

        if verts.shape[0] > 0 and facets.shape[0] > 0:
            facets = np.clip(facets, 0, verts.shape[0] - 1)
            # Drop facets that reference an invalid vertex after clipping edge cases.
            valid = (facets >= 0).all(axis=1)
            facets = facets[valid]
    except Exception:
        verts = np.zeros((0, 3), dtype=float)
        facets = np.zeros((0, 3), dtype=np.int64)
    return verts, facets


def _render_one(verts, facets, bbox, view, width, height, supersample, style, groups=None):
    W = width * supersample
    H = height * supersample

    if verts.shape[0] == 0 or facets.shape[0] == 0:
        return np.tile(_BACKGROUND_RGB, (H, W, 1))

    cam = camera_for(view, bbox)
    cam.fit(bbox, W, H)
    proj = cam.project(verts)          # (V, 2) float pixel coords
    vdepth = cam.depth(verts)          # (V,) larger == closer

    # Per-facet Lambert grey. Normals are oriented toward the camera so a closed
    # mesh is lit two-sided and never comes out black.
    fnormal = _facet_normals(verts, facets, cam.look)
    light = cam.look + CONTRACT["light"]["up"] * cam.up + CONTRACT["light"]["right"] * cam.right
    light = light / np.linalg.norm(light)
    shade = (CONTRACT["ambient"] + CONTRACT["diffuse"] * np.clip(fnormal @ light, 0, 1)
             + CONTRACT["rim"] * np.clip(fnormal @ -cam.right, 0, 1))
    grey = np.clip(shade[:, None] * np.array(CONTRACT["color"])[None, :] * 255, 0, 255).astype(np.uint8)

    facet_buf = np.full((H, W), -1, dtype=np.int32)
    depth_buf = np.full((H, W), -np.inf, dtype=np.float64)

    for i in range(facets.shape[0]):
        a, b, c = int(facets[i, 0]), int(facets[i, 1]), int(facets[i, 2])
        p0, p1, p2 = proj[a], proj[b], proj[c]
        # Signed 2D area (not taking abs yet — sign drives barycentric divide).
        area = (p1[0] - p0[0]) * (p2[1] - p0[1]) - (p2[0] - p0[0]) * (p1[1] - p0[1])
        if abs(area) < 1e-9:
            continue  # degenerate (zero-area) triangle: skip, never raise

        # Pixel bounding box of the triangle, clamped to the frame.
        minx = int(np.floor(min(p0[0], p1[0], p2[0])))
        maxx = int(np.ceil(max(p0[0], p1[0], p2[0])))
        miny = int(np.floor(min(p0[1], p1[1], p2[1])))
        maxy = int(np.ceil(max(p0[1], p1[1], p2[1])))
        if maxx < 0 or minx >= W or maxy < 0 or miny >= H:
            continue
        minx = max(minx, 0)
        maxx = min(maxx, W - 1)
        miny = max(miny, 0)
        maxy = min(maxy, H - 1)
        if minx > maxx or miny > maxy:
            continue

        # Vectorised coverage test over the whole bbox at once.
        xs = np.arange(minx, maxx + 1)
        ys = np.arange(miny, maxy + 1)
        gx, gy = np.meshgrid(xs, ys)
        qx = gx.ravel().astype(float)
        qy = gy.ravel().astype(float)

        l1 = ((p1[1] - p2[1]) * (qx - p2[0]) + (p2[0] - p1[0]) * (qy - p2[1])) / area
        l2 = ((p2[1] - p0[1]) * (qx - p2[0]) + (p0[0] - p2[0]) * (qy - p2[1])) / area
        l3 = 1.0 - l1 - l2
        inside = (l1 >= -1e-6) & (l2 >= -1e-6) & (l3 >= -1e-6)
        if not inside.any():
            continue

        px = qx[inside]
        py = qy[inside]
        # Interpolated depth at the covered pixels.
        pd = l1[inside] * vdepth[a] + l2[inside] * vdepth[b] + l3[inside] * vdepth[c]
        ix = px.astype(np.int64)
        iy = py.astype(np.int64)

        # Z-buffer: keep the closest surface (max depth).
        cur = depth_buf[iy, ix]
        nearer = pd > cur
        if not nearer.any():
            continue
        fi = ix[nearer]
        fj = iy[nearer]
        facet_buf[fj, fi] = i
        depth_buf[fj, fi] = pd[nearer]

    return _compose(facet_buf, depth_buf, grey, style, H, W, fnormal, groups)


def _facet_normals(verts, facets, look):
    fnormal = np.zeros((facets.shape[0], 3), dtype=float)
    for i in range(facets.shape[0]):
        a, b, c = facets[i, 0], facets[i, 1], facets[i, 2]
        n = np.cross(verts[b] - verts[a], verts[c] - verts[a])
        ln = np.linalg.norm(n)
        if ln > 1e-12:
            n = n / ln
            if np.dot(n, look) < 0.0:
                n = -n  # face the camera (two-sided lighting)
        fnormal[i] = n
    return fnormal


def _compose(facet_buf, depth_buf, grey, style, H, W, fnormal, groups=None):
    covered = facet_buf >= 0
    edge = _edge_mask(facet_buf, covered, fnormal, groups)

    img = np.tile(_BACKGROUND_RGB, (H, W, 1)).copy()

    if style in ("flat", "flat_edges"):
        # Map facet id -> grey, background (-1) clipped to facet 0 then masked out.
        shade = grey[np.clip(facet_buf, 0, grey.shape[0] - 1)]
        shade_rgb = shade.astype(np.uint8)
        img[covered] = shade_rgb[covered]

    if style in ("edges_only", "flat_edges"):
        img[edge] = _EDGE_RGB

    return img


# cos(~10 degrees): two neighbouring samples belong to the same flat surface
# unless their normals diverge by more than this.
_CREASE_COS = CONTRACT["crease_cos"]


def _edge_mask(facet_buf, covered, fnormal, groups=None):
    """Feature edges + silhouette contour, computed on the buffers.

    Two rules, and deliberately only two:

    1. **Normal discontinuity** -> a real feature edge (a 90-degree box corner, a
       fillet boundary). Coplanar neighbours share a normal and draw nothing.

       This replaced an earlier facet-id-difference rule, which marked an edge
       between *any* two triangles sharing a pixel border. Since a tessellated
       BRep has a seam between every pair of coplanar triangles, every flat face
       came out with a diagonal line across it (plainly visible on a box top).
       That is not cosmetic: these images are given to a multimodal model as
       evidence about the geometry, so a triangulation seam drawn as an edge is a
       *false feature* the model would believe in.

    2. **Coverage boundary** -> the outer contour (a covered pixel with an
       uncovered 4-neighbour).

       This replaced a per-pixel depth-gradient rule. A depth threshold cannot
       tell a silhouette from a steeply slanted face, so it fired across the whole
       surface: measured on a plain box, `edges_only` came out 26% dark — i.e.
       the entire shape, not its edges. The boundary rule is exact, costs nothing,
       and is the only rule that also gives a *flat* part an outline (a flat plate
       has no normal discontinuity anywhere, so rule 1 alone drew nothing at all).
    """
    n = fnormal[facet_buf]                      # (H, W, 3), per-pixel facet normal
    n = np.where(covered[:, :, None], n, 0.0)   # neutralise background

    # ── rule 1: normal discontinuity between covered neighbours ──
    dot_r = np.zeros(n.shape[:2], dtype=np.float64)
    dot_r[:, 1:] = np.sum(n[:, 1:, :] * n[:, :-1, :], axis=2)
    dot_d = np.zeros(n.shape[:2], dtype=np.float64)
    dot_d[1:, :] = np.sum(n[1:, :, :] * n[:-1, :, :], axis=2)

    both_r = covered[:, 1:] & covered[:, :-1]
    both_d = covered[1:, :] & covered[:-1, :]
    if groups is not None:
        # Curved BRep faces contain skinny tessellation triangles whose chord
        # normals can differ sharply. They are not physical feature edges.
        faces = np.asarray(groups)[facet_buf]
        same_r = (faces[:, 1:] >= 0) & (faces[:, 1:] == faces[:, :-1])
        same_d = (faces[1:, :] >= 0) & (faces[1:, :] == faces[:-1, :])
        both_r &= ~same_r
        both_d &= ~same_d
    crease = np.zeros_like(covered)
    crease[:, 1:] |= both_r & (dot_r[:, 1:] < _CREASE_COS)
    crease[1:, :] |= both_d & (dot_d[1:, :] < _CREASE_COS)

    # ── rule 2: silhouette — covered pixel touching an uncovered neighbour ──
    contour = np.zeros_like(covered)
    contour[:, :-1] |= covered[:, :-1] & ~covered[:, 1:]
    contour[:, 1:] |= covered[:, 1:] & ~covered[:, :-1]
    contour[:-1, :] |= covered[:-1, :] & ~covered[1:, :]
    contour[1:, :] |= covered[1:, :] & ~covered[:-1, :]
    # A 1px shape needs its outer edge too; the rules above only mark inner
    # boundaries, so a fully-isolated covered pixel is all contour.
    isolated = covered & ~np.roll(covered, 1, 0) & ~np.roll(covered, -1, 0) \
                        & ~np.roll(covered, 1, 1) & ~np.roll(covered, -1, 1)
    contour |= isolated

    return (crease | contour) & covered


def _downsample(arr, width, height, supersample):
    """Box-downsample by taking the per-block MIN.

    MIN (not mean) preserves thin dark edge lines through anti-aliasing: a flat
    facet is uniform, so its block-min equals its colour, while a 1px edge
    pixel drives the whole block dark instead of washing out to light grey.
    """
    H, W, C = arr.shape
    arr = arr[: height * supersample, : width * supersample]
    block = arr.reshape(height, supersample, width, supersample, C)
    return block.min(axis=(1, 3)).astype(np.uint8)
