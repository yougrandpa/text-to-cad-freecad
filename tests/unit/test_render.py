"""Render-layer tests: camera, rasteriser, PNG backends.

Written by the integrator after the original owner was cut off mid-task (it had
produced the implementation but zero tests). The renderer is not cosmetic: the
images it produces are fed to a multimodal model as *evidence about geometry*, so
a wrong axis or a phantom edge is misinformation, not a cosmetic defect. These
tests therefore assert semantics, not just "a file appeared".
"""

from __future__ import annotations

import warnings

import numpy as np
import pytest

from tcad.core.types import BBox, Mesh
from tcad.render import png as png_mod
from tcad.render.camera import camera_for
from tcad.render.png import write_png
from tcad.render.raster import render_views

BOX_BBOX = BBox(x=60.0, y=40.0, z=10.0, x_min=0.0, y_min=0.0, z_min=0.0)
VIEWS = ["iso", "front", "top", "right"]


# ── mesh builders ─────────────────────────────────────────────────────────


def box_mesh(nx: float = 60.0, ny: float = 40.0, nz: float = 10.0) -> Mesh:
    """A closed 12-facet box, triangulated the way TopoShape.tessellate does."""
    v = [
        (0, 0, 0), (nx, 0, 0), (nx, ny, 0), (0, ny, 0),
        (0, 0, nz), (nx, 0, nz), (nx, ny, nz), (0, ny, nz),
    ]
    quads = [
        (0, 3, 2, 1),  # bottom
        (4, 5, 6, 7),  # top
        (0, 1, 5, 4),  # front (y=0)
        (2, 3, 7, 6),  # back
        (1, 2, 6, 5),  # right (x=max)
        (0, 4, 7, 3),  # left
    ]
    facets: list[tuple[int, int, int]] = []
    for a, b, c, d in quads:
        facets.append((a, b, c))
        facets.append((a, c, d))
    return Mesh(vertices=v, facets=facets, volume=nx * ny * nz,
                bbox=BBox(x=nx, y=ny, z=nz, x_min=0.0, y_min=0.0, z_min=0.0))


def flat_quad_mesh() -> Mesh:
    """Two COPLANAR triangles forming a single flat square in the z=0 plane.

    This is the case that exposed a real bug: the first edge detector marked an
    edge wherever neighbouring pixels belonged to different *facets*, so every
    triangulation seam was drawn as a line. On a flat face that is a phantom
    edge — the model would believe the surface had a crease along the diagonal.
    """
    verts = [(0, 0, 0), (40, 0, 0), (40, 40, 0), (0, 40, 0)]
    facets = [(0, 1, 2), (0, 2, 3)]  # share the 0-2 diagonal, same normal
    return Mesh(vertices=verts, facets=facets, volume=0.0,
                bbox=BBox(x=40.0, y=40.0, z=0.0, x_min=0.0, y_min=0.0, z_min=0.0))


# ══════════════════════════════════════════════════════════════════════════
# camera
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("view", VIEWS)
def test_camera_basis_is_orthonormal(view):
    cam = camera_for(view, BOX_BBOX)
    for name in ("right", "up", "look"):
        assert np.linalg.norm(getattr(cam, name)) == pytest.approx(1.0, abs=1e-9), name
    assert float(np.dot(cam.right, cam.up)) == pytest.approx(0.0, abs=1e-9)
    assert float(np.dot(cam.right, cam.look)) == pytest.approx(0.0, abs=1e-9)
    assert float(np.dot(cam.up, cam.look)) == pytest.approx(0.0, abs=1e-9)
    # right x up should point along look (right-handed screen basis)
    assert np.allclose(np.cross(cam.right, cam.up), cam.look, atol=1e-9)


def test_unknown_view_raises_rather_than_silently_defaulting():
    # A typo must fail loudly. An earlier version called `ViewName(view)`, which
    # is a TypeError because ViewName is a typing.Literal — so this also guards
    # against that regression.
    with pytest.raises(ValueError, match="unknown view"):
        camera_for("isometic", BOX_BBOX)


def test_standard_views_point_the_right_way():
    """Each view's look direction points from the model toward the camera.
    FreeCAD convention: Front looks along +Y, Top looks down -Z, Right along -X."""
    assert np.allclose(camera_for("front", BOX_BBOX).look, [0, -1, 0])
    assert np.allclose(camera_for("top", BOX_BBOX).look, [0, 0, 1])
    assert np.allclose(camera_for("right", BOX_BBOX).look, [1, 0, 0])
    iso = camera_for("iso", BOX_BBOX).look
    assert np.allclose(iso, np.ones(3) / np.sqrt(3), atol=1e-9)


def test_screen_basis_is_right_handed_in_every_view():
    """right x up == look. A left-handed basis silently mirrors the image, which
    would show the model a reflection of its own part."""
    for view in VIEWS:
        cam = camera_for(view, BOX_BBOX)
        assert np.allclose(np.cross(cam.right, cam.up), cam.look, atol=1e-9), view


def test_fit_keeps_every_vertex_inside_the_frame():
    for view in VIEWS:
        cam = camera_for(view, BOX_BBOX).fit(BOX_BBOX, 320, 240)
        corners = np.array(
            [(x, y, z) for x in (0, 60) for y in (0, 40) for z in (0, 10)], dtype=float
        )
        s = cam.project(corners)
        assert s[:, 0].min() > 0 and s[:, 0].max() < cam.width, (view, s[:, 0])
        assert s[:, 1].min() > 0 and s[:, 1].max() < cam.height, (view, s[:, 1])


def test_deeper_points_are_closer_to_the_camera():
    """z-buffer contract: larger depth == closer. A sign error here would make
    the renderer keep back faces and draw an inside-out solid.

    Derived from the camera basis rather than hard-coded per view, so it keeps
    working if a view's direction is corrected.
    """
    for view in VIEWS:
        cam = camera_for(view, BOX_BBOX)
        # centre, then step a long way along the look direction (toward the camera)
        centre = np.array([30.0, 20.0, 5.0])
        near = (centre + cam.look * 100.0)[None, :]
        far = (centre - cam.look * 100.0)[None, :]
        assert float(cam.depth(near)[0]) > float(cam.depth(far)[0]), view


# ══════════════════════════════════════════════════════════════════════════
# rasteriser
# ══════════════════════════════════════════════════════════════════════════


def test_render_returns_one_image_per_view_with_the_right_shape():
    out = render_views(box_mesh(), views=VIEWS, width=160, height=120, supersample=1)
    assert set(out) == set(VIEWS)
    for view, arr in out.items():
        assert arr.shape == (120, 160, 3), (view, arr.shape)
        assert arr.dtype == np.uint8


def test_every_view_actually_draws_something():
    out = render_views(box_mesh(), views=VIEWS, width=200, height=150)
    for view, arr in out.items():
        ink = float((arr < 250).mean())
        assert ink > 0.02, f"{view} is nearly blank (ink={ink:.3%})"
        assert arr.std() > 5.0, f"{view} has no contrast (std={arr.std():.2f})"


def test_axis_orientation_is_not_swapped_or_mirrored():
    """A box with a distinct extent on every axis must render with the right
    aspect in each view. This is the assertion that catches a swapped or
    transposed camera basis — the failure mode the camera module warns about.

    For a 80 x 20 x 40 box:
      front looks along Y -> shows X-Z -> 80 wide x 40 tall
      top   looks down  Z -> shows X-Y -> 80 wide x 20 tall
      right looks along X -> shows Y-Z -> 20 wide x 40 tall
    """
    mesh = box_mesh(nx=80.0, ny=20.0, nz=40.0)
    imgs = render_views(mesh, views=VIEWS, width=240, height=240, supersample=1)

    def ink_extent(arr):
        # single-channel mask: np.where on an (H,W,3) mask yields three arrays
        mask = arr[:, :, 0] < 250
        ys, xs = np.where(mask)
        assert mask.any(), "nothing drawn"
        return int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)

    fw, fh = ink_extent(imgs["front"])
    tw, th = ink_extent(imgs["top"])
    rw, rh = ink_extent(imgs["right"])

    # front: 80 x 40 -> clearly wider than tall
    assert fw > fh * 1.5, f"front should be wide (80x40), got {fw}x{fh}"
    # top: 80 x 20 -> even more so
    assert tw > th * 2.5, f"top should be wide (80x20), got {tw}x{th}"
    # right: 20 x 40 -> taller than wide (this is the one a Y/Z swap breaks)
    assert rh > rw * 1.5, f"right should be tall (20x40), got {rw}x{rh}"


def test_coplanar_triangles_do_not_produce_a_phantom_edge():
    """Regression: a flat square made of two triangles must render with NO
    internal line. The original facet-id-based edge detector drew the
    triangulation diagonal, which would tell the model a crease exists where the
    surface is flat."""
    arr = render_views(
        flat_quad_mesh(), views=["top"], width=200, height=200,
        style="edges_only", supersample=1,
    )["top"]
    dark = arr[:, :, 0] < 128
    # The only dark pixels may come from the outer silhouette. Count dark pixels
    # strictly inside the shape: for a flat quad seen face-on there should be none.
    ys, xs = np.where(dark)
    assert dark.any(), "the outline should still be drawn"
    interior = dark[ys.min() + 4: ys.max() - 3, xs.min() + 4: xs.max() - 3]
    assert interior.sum() == 0, (
        f"{interior.sum()} interior edge pixels — a triangulation seam is being "
        "drawn as a feature edge"
    )


def test_real_feature_edges_are_still_drawn():
    """The counterpart to the test above: turning the crease rule off must not
    turn real box edges off too."""
    arr = render_views(
        box_mesh(), views=["iso"], width=240, height=180,
        style="edges_only", supersample=1,
    )["iso"]
    assert (arr[:, :, 0] < 128).sum() > 100, "box edges vanished"


def test_native_face_mapping_suppresses_curve_tessellation_creases_only():
    from tcad.render.raster import _edge_mask
    # Two sharply different chord normals on one native smooth face must not
    # produce a phantom feature. Distinct native faces still retain the crease.
    facets = np.array([[0, 0, 1, 1]]*4)
    covered = np.ones((4, 4), dtype=bool)
    normals = np.array([[0, 0, 1], [0, 1, 0]], dtype=float)
    assert not _edge_mask(facets, covered, normals, [0, 0]).any()
    assert _edge_mask(facets, covered, normals, [0, 1])[:, 2].all()
    assert _edge_mask(facets, covered, normals)[:, 2].all()


@pytest.mark.parametrize("bad_mesh", [
    Mesh(vertices=[], facets=[], bbox=BOX_BBOX),
    Mesh(vertices=[(0, 0, 0)], facets=[], bbox=BOX_BBOX),
    Mesh(vertices=[(0, 0, 0), (1, 0, 0), (2, 0, 0)], facets=[(0, 1, 2)], bbox=BOX_BBOX),
    Mesh(vertices=[(0, 0, 0), (1, 0, 0), (0, 1, 0)], facets=[(0, 1, 99)], bbox=BOX_BBOX),
])
def test_degenerate_inputs_render_a_blank_image_instead_of_raising(bad_mesh):
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        out = render_views(bad_mesh, views=["iso"], width=64, height=64, supersample=1)
    arr = out["iso"]
    assert arr.shape == (64, 64, 3)
    assert arr.dtype == np.uint8


def test_no_runtime_warnings_from_infinite_depth_background():
    """Uncovered pixels hold -inf depth; differencing them produces NaN and a
    flood of numpy RuntimeWarnings unless masked before the subtraction."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        render_views(box_mesh(), views=VIEWS, width=180, height=140, supersample=1)


def test_dimensions_and_supersample_are_clamped():
    """Absurd inputs must not allocate absurd buffers or raise."""
    out = render_views(box_mesh(), views=["iso"], width=0, height=10**9,
                       supersample=99, style="flat")
    h, w = out["iso"].shape[:2]
    assert 1 <= w <= 8192
    assert 1 <= h <= 8192


def test_supersampling_preserves_output_size():
    a = render_views(box_mesh(), views=["iso"], width=100, height=80, supersample=1)["iso"]
    b = render_views(box_mesh(), views=["iso"], width=100, height=80, supersample=4)["iso"]
    assert a.shape == b.shape == (80, 100, 3)


def test_styles_differ_meaningfully():
    mesh = box_mesh()
    flat = render_views(mesh, views=["iso"], width=120, height=90, style="flat", supersample=1)["iso"]
    edges = render_views(mesh, views=["iso"], width=120, height=90, style="edges_only", supersample=1)["iso"]
    both = render_views(mesh, views=["iso"], width=120, height=90, style="flat_edges", supersample=1)["iso"]
    assert flat.std() > 1.0, "flat shading should vary across facets"
    assert edges.mean() > flat.mean(), "edges_only should be mostly background"
    assert both.mean() < flat.mean(), "flat_edges should be darker than flat"


# ══════════════════════════════════════════════════════════════════════════
# PNG — both backends
# ══════════════════════════════════════════════════════════════════════════


def test_write_png_produces_a_readable_file(tmp_path):
    arr = render_views(box_mesh(), views=["iso"], width=80, height=60)["iso"]
    path = tmp_path / "iso.png"
    w, h = write_png(arr, str(path))
    assert (w, h) == (80, 60)
    assert path.stat().st_size > 0
    with open(path, "rb") as fh:
        assert fh.read(8) == b"\x89PNG\r\n\x1a\n"


def test_stdlib_backend_round_trips_identically_through_pillow(tmp_path):
    """The stdlib encoder is the guarantee that rendering survives without
    Pillow, so it must produce a byte-faithful image, not an approximation."""
    PIL = pytest.importorskip("PIL.Image", reason="needs Pillow to verify the stdlib encoder")

    rng = np.random.default_rng(1234)
    arr = rng.integers(0, 256, size=(37, 53, 3), dtype=np.uint8)

    std_path = tmp_path / "std.png"
    w, h = png_mod._write_png_stdlib(arr, str(std_path))
    assert (w, h) == (53, 37)

    back = np.asarray(PIL.open(std_path).convert("RGB"))
    assert np.array_equal(back, arr), "stdlib PNG did not round-trip exactly"


def test_stdlib_backend_rejects_wrong_shape(tmp_path):
    with pytest.raises(ValueError):
        png_mod._write_png_stdlib(np.zeros((4, 4, 4), dtype=np.uint8), str(tmp_path / "x.png"))


def test_stdlib_backend_accepts_2d_grayscale(tmp_path):
    arr = np.full((8, 8), 200, dtype=np.uint8)
    w, h = png_mod._write_png_stdlib(arr, str(tmp_path / "g.png"))
    assert (w, h) == (8, 8)


def test_backend_in_use_is_reported():
    assert png_mod.backend_in_use() in {"pillow", "stdlib"}
