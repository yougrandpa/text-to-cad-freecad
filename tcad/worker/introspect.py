"""Document -> GeometryDigest (runs inside FreeCADCmd).

Produces the GeometryDigest dict shape documented in tcad/core/types.py
(GeometryDigest / Topology / BBox / FeatureDigest). Read-only measurements only;
the digest is program-generated, never model-summarised.

Measurement truth: shape.BoundBox (axis-aligned) for the box, shape.isValid()
as the primary validity signal, shape.check() inside try/except (it returns None
on success and RAISES on failure — never write ``if shape.check():``).
"""

from __future__ import annotations

import os
import tempfile

import FreeCAD as App  # runs inside FreeCADCmd

from tcad.worker.compiler import _build, _measure, _close_doc


def _unit(v) -> "App.Vector":
    """Unit vector, sign-canonical.

    Two faces of one hole can carry opposite axis directions (seam/orientation
    dependent). Without a canonical sign they group as two holes and the count
    is simply wrong.
    """
    length = v.Length
    out = App.Vector(v.x / length, v.y / length, v.z / length)
    if out.z < 0 or (out.z == 0 and out.y < 0) or (out.z == 0 and out.y == 0 and out.x < 0):
        out = App.Vector(-out.x, -out.y, -out.z)
    return out


def _scaled(v, k: float) -> "App.Vector":
    """Scalar multiple without mutating ``v`` (App.Vector.multiply is in place)."""
    return App.Vector(v.x * k, v.y * k, v.z * k)


def _hole_span(surf, param_range, axis) -> tuple[float, float]:
    """Axial extent of a cylindrical face, as the min/max projection of its
    parametrisation onto ``axis``.

    Uses the surface parametrisation rather than the face bounding box: a bbox
    over-estimates the span of an inclined cylinder, and an over-estimated
    depth would let a blind hole be certified as through.

    Along a cylinder the axial coordinate is a linear function of exactly one
    parameter, so the extremes of that parameter's range are extremes of the
    span — probing the four corners of the parameter rectangle is enough and
    avoids having to work out which of u/v is the axial one (a full-circle seam
    makes a "compare the two edge vectors" heuristic pick two identical points
    and report a zero depth, which is what it did here before).

    Points come from ``surf.value`` — ``Part.Face`` has no ``value`` method at
    all, so the face-level variant raised on every hole and the measurement
    silently came back empty.
    """
    u0, u1, v0, v1 = param_range
    ts = [surf.value(u, v).dot(axis) for u in (u0, u1) for v in (v0, v1)]
    return min(ts), max(ts)


def _measure_holes(shape) -> list[dict]:
    """Holes read off the BRep: concave cylindrical faces, merged per axis.

    Concave is decided geometrically — the surface's outward normal points
    TOWARD the axis, so the material lies outside it and the surface bounds
    void. A pad's outer cylinder is convex and is therefore excluded: a Ø10
    boss cannot be certified as the Ø10 hole that was asked for.

    Faces of one hole split by a seam or a crossing cut merge into a single
    entry (same radius, axis direction and axis position), so the count is the
    number of holes rather than the number of faces.
    """
    if shape is None or shape.isNull():
        return []
    try:
        solids = shape.Solids
    except Exception:  # noqa: BLE001
        return []
    if len(solids) != 1:
        # With several solids, "which body does this hole belong to" is not
        # something this function can answer without guessing.
        return []
    solid = solids[0]
    try:
        bb = solid.BoundBox
        corners = [App.Vector(x, y, z) for x in (bb.XMin, bb.XMax)
                   for y in (bb.YMin, bb.YMax) for z in (bb.ZMin, bb.ZMax)]
    except Exception:  # noqa: BLE001
        return []

    groups: dict[tuple, dict] = {}
    for face in solid.Faces:
        try:
            surf = face.Surface
            # FreeCAD names the geometry class Part::GeomCylinder (the wrapper
            # type is `Cylinder`); matching a single literal here silently
            # measured zero holes on every part until this was probed on the
            # real kernel.
            if not str(getattr(surf, "TypeId", "")).endswith("Cylinder"):
                continue
            radius = float(surf.Radius)
            raw_axis = App.Vector(surf.Axis)
            if raw_axis.Length < 1e-9:
                continue
            axis = _unit(raw_axis)
            # Canonical point on the axis: the foot of the perpendicular from
            # the world origin, so it does not depend on where the surface's
            # own centre happens to sit.
            centre = App.Vector(surf.Center)
            foot = centre.sub(_scaled(axis, centre.dot(axis)))
            u_mid = 0.5 * (face.ParameterRange[0] + face.ParameterRange[1])
            v_mid = 0.5 * (face.ParameterRange[2] + face.ParameterRange[3])
            p = surf.value(u_mid, v_mid)
            normal = face.normalAt(u_mid, v_mid)
            radial = p.sub(foot).sub(_scaled(axis, p.sub(foot).dot(axis)))
            if radial.Length < 1e-9 or normal.dot(radial) >= 0:
                continue  # convex outer surface, or degenerate: not a hole wall
            t0, t1 = _hole_span(surf, face.ParameterRange, axis)
        except Exception:  # noqa: BLE001 — one unreadable face must not cost the digest
            continue
        key = (round(radius, 6), round(foot.x, 6), round(foot.y, 6), round(foot.z, 6),
               tuple(sorted((round(axis.x, 6), round(axis.y, 6), round(axis.z, 6)))))
        g = groups.get(key)
        if g is None:
            groups[key] = {"radius": radius, "axis": [axis.x, axis.y, axis.z],
                           "center": [foot.x, foot.y, foot.z],
                           "t_min": t0, "t_max": t1, "faces": 1}
        else:
            g["t_min"] = min(g["t_min"], t0)
            g["t_max"] = max(g["t_max"], t1)
            g["faces"] += 1

    holes: list[dict] = []
    for g in groups.values():
        axis = App.Vector(*g["axis"])
        projected = [c.dot(axis) for c in corners]
        solid_span = max(projected) - min(projected)
        depth = g["t_max"] - g["t_min"]
        holes.append({
            "radius": round(g["radius"], 6),
            "diameter": round(2.0 * g["radius"], 6),
            "axis": [round(v, 6) for v in g["axis"]],
            "center": [round(v, 6) for v in g["center"]],
            "depth": round(depth, 6),
            "through": bool(solid_span > 0 and depth >= solid_span - 1e-6),
            "faces": g["faces"],
        })
    holes.sort(key=lambda h: (h["axis"], h["center"], h["radius"]))
    for i, h in enumerate(holes):
        h["index"] = i
    return holes


#: How many faces the digest lists. A part with hundreds of faces would blow the
#: context budget, and a model that cannot be told about all of them is better
#: served by an explicit "(truncated)" than by a silently shortened list.
_FACE_LIMIT = 32

#: Pairwise wall search is O(n^2) in faces. Above this the measurement is
#: skipped and the digest says so, rather than taking an unbounded amount of
#: time inside the worker for a part nobody asked to have measured.
_WALL_FACE_CAP = 64


def _measure_min_wall_thickness(shape) -> float | None:
    """Minimum material thickness of the solid, or ``None`` if not measurable.

    Method: for every pair of faces whose outward normals point roughly *away*
    from each other, take the closest distance between their surfaces and keep
    the smallest one whose mid-point lies inside the material. That pair-wise
    minimum is the local wall thickness for the shapes this harness builds —
    a plate's two large faces, a tube's inner and outer cylinders, a boss beside
    a pocket.

    Why this shape of a measurement rather than a ray cast: the OCC
    ``IntCurvesFace`` intersector is **not** present in this FreeCAD build's
    bindings (probed: ``ModuleNotFoundError``), and the first attempt at a
    raycast therefore measured nothing. ``distToShape`` + ``isInside`` are part
    of the public ``Part`` API and were probed on the real kernel: an 80x50x8
    plate reports 8.0, a Ø30/Ø10 tube reports 10.0, the same plate with a Ø8
    hole still reports 8.0.

    Known limits, stated rather than hidden:
      * It is a lower bound built from *pairs of faces*: a thickness field that
        exists only between two points on a single curved face is not seen.
      * Beyond :data:`_WALL_FACE_CAP` faces it returns ``None`` instead of
        guessing or hanging, and the caller reports "not measured".
    """
    if shape is None or shape.isNull():
        return None
    try:
        faces = list(shape.Faces)
        solids = list(shape.Solids)
    except Exception:  # noqa: BLE001
        return None
    if not faces or not solids or len(faces) > _WALL_FACE_CAP:
        return None

    best: float | None = None
    for i, fi in enumerate(faces):
        for fj in faces[i + 1:]:
            try:
                d, pts, _info = fi.distToShape(fj)
                if d <= 1e-9:
                    continue
                n1 = fi.normalAt(0.5, 0.5)
                n2 = fj.normalAt(0.5, 0.5)
                # Roughly opposed, i.e. material sits between them. A pair
                # pointing the same way is two sides of one feature (a hole wall
                # and the plate's top face), not a wall.
                if n1.dot(n2) > -0.5:
                    continue
                a = App.Vector(pts[0][0])
                b = App.Vector(pts[0][1])
                mid = a.add(b)
                mid.multiply(0.5)
                if not any(s.isInside(mid, 1e-6, True) for s in solids):
                    continue
            except Exception:  # noqa: BLE001 — a pair we cannot measure is skipped
                continue
            if best is None or d < best:
                best = float(d)
    return round(best, 6) if best is not None else None


def _measure_faces(shape) -> list[dict]:
    """Planar faces of the built shape, with the name a sketch attaches to.

    ``Face{N}`` is FreeCAD's 1-based index, which is what ``AttachmentSupport =
    (feature, ["FaceN"])`` takes. The normal and centre are included so a reader
    can choose by intent rather than by a number that moves when the part changes.
    """
    if shape is None or shape.isNull():
        return []
    rows: list[dict] = []
    for i, face in enumerate(shape.Faces):
        if i >= _FACE_LIMIT:
            break
        try:
            # Only planar faces can host a FlatFace-attached sketch.
            if face.Surface.__class__.__name__ != "Plane":
                continue
            n = face.normalAt(0.5, 0.5)
            c = face.CenterOfMass
            rows.append({
                "name": f"Face{i + 1}",
                "area": round(float(face.Area), 6),
                "normal": [round(float(n.x), 6), round(float(n.y), 6), round(float(n.z), 6)],
                "center": [round(float(c.x), 6), round(float(c.y), 6), round(float(c.z), 6)],
            })
        except Exception:  # noqa: BLE001 — a face we cannot measure is simply not listed
            continue
    return rows


_EDGE_LIMIT = 48


def _measure_edges(shape) -> list[dict]:
    """Edges of the built shape, with the name a fillet/chamfer selects.

    ``base_feature`` + ``sub_elements`` need real names; a model cannot act on
    "there are 12 edges". Length and mid-point are included so a reader can pick
    "the 8 mm vertical edge" rather than a number that shifts when the part does.
    """
    if shape is None or shape.isNull():
        return []
    rows: list[dict] = []
    for i, edge in enumerate(shape.Edges):
        if i >= _EDGE_LIMIT:
            break
        try:
            curve = edge.Curve
            kind = curve.__class__.__name__
            # `valueAt` takes a *parameter*, not a fraction: for a line the range
            # is [0, length], so `valueAt(0.5)` is half a millimetre along an
            # 8 mm edge — a point that says nothing about where the edge is, and
            # for edges shorter than 0.5 it can fall outside the edge entirely
            # (a line extrapolates). `normalAt` on a *face* is normalised, which
            # is why the face code below looks similar and is still right.
            mid_param = (edge.FirstParameter + edge.LastParameter) / 2.0
            mid = edge.valueAt(mid_param)
            row = {
                "name": f"Edge{i + 1}",
                "kind": kind,
                "length": round(float(edge.Length), 6),
                "mid": [round(float(mid.x), 6), round(float(mid.y), 6), round(float(mid.z), 6)],
                "direction": [],
            }
            if kind == "Line":
                t = edge.tangentAt(mid_param)
                row["direction"] = [round(float(t.x), 6), round(float(t.y), 6),
                                    round(float(t.z), 6)]
            rows.append(row)
        except Exception:  # noqa: BLE001 — an edge we cannot measure is simply not listed
            continue
    return rows


def _render_text(model_id: str, ir_version: int, feature_chain, measure: dict,
                 sketches, holes: list | None = None,
                 min_wall_thickness: float | None = None) -> str:
    """Deterministic, compact, <=~2000-token text rendering of the digest."""
    lines = []
    lines.append(f"model: {model_id} (v{ir_version})")
    lines.append(f"feature chain ({len(feature_chain)}):")
    for i, fc in enumerate(feature_chain, 1):
        params = fc.get("params") or {}
        ps = " ".join(f"{k}={v}" for k, v in params.items())
        flag = " [suppressed]" if fc.get("suppressed") else ""
        lines.append(f"  {i}. {fc.get('name')} [{fc.get('op')}]{flag} {ps}".rstrip())
    topo = measure
    lines.append(
        f"topology: solids={topo['solids']} faces={topo['faces']} "
        f"edges={topo['edges']} vertexes={topo['vertexes']} shells={topo['shells']}"
    )
    bb = topo["bbox"]
    lines.append(
        f"bbox: x={bb['x']:.4f} y={bb['y']:.4f} z={bb['z']:.4f} "
        f"(min x={bb['x_min']:.4f} y={bb['y_min']:.4f} z={bb['z_min']:.4f})"
    )
    lines.append(f"volume: {topo['volume']:.6f}  area: {topo['area']:.6f}")
    lines.append(f"shape_type: {topo['shape_type']}  valid: {topo['is_valid']}")
    if min_wall_thickness is not None:
        lines.append(f"min_wall_thickness: {min_wall_thickness:.4f} "
                     "(closest opposed-face distance with material between)")
    if holes:
        lines.append(f"holes measured on the BRep ({len(holes)}):")
        for h in holes:
            cx, cy, cz = h["center"]
            ax, ay, az = h["axis"]
            lines.append(
                f"  #{h['index']} d={h['diameter']:.4f} axis=({ax:+.2f},{ay:+.2f},{az:+.2f}) "
                f"through ({cx:.3f},{cy:.3f},{cz:.3f}) depth={h['depth']:.4f} "
                f"{'THROUGH' if h['through'] else 'BLIND'}"
            )
    elif holes == []:
        lines.append("holes measured on the BRep: none (no concave cylindrical face)")
    if sketches:
        lines.append("sketches:")
        for sk in sketches:
            fc_state = "?" if sk["fully_constrained"] is None else (
                "yes" if sk["fully_constrained"] else "no")
            lines.append(
                f"  {sk['name']}: fully_constrained={fc_state} "
                f"dof={sk['dof']} solve_status={sk['solve_status']}"
            )
    return "\n".join(lines)


def _key_dimensions(measure: dict, sketches: list,
                    min_wall_thickness: float | None = None) -> dict:
    """Build the digest's ``key_dimensions`` blob.

    Besides the axis-aligned extents, this carries the **reserved per-sketch
    keys** that are the agreed carrier for constraint state between the worker
    and the Gate:

        "<sketch_id>__fully_constrained"  -> 1.0 / 0.0
        "<sketch_id>__dof"                -> float

    ``tcad/verify/checks_solid.py::SketchFullyConstrainedCheck`` reads exactly
    these. If they are missing the check can only SKIP — which would silently
    drop the "every required sketch must be fully constrained" invariant out of
    the Gate. Keys are emitted ONLY when the state was actually measured, so an
    unmeasurable sketch produces an honest SKIP rather than a fake zero.

    Keyed by sketch **id** (not name): the id is the stable handle the IR and
    the Gate both refer to.
    """
    out: dict = {
        "x": measure["bbox"]["x"], "y": measure["bbox"]["y"],
        "z": measure["bbox"]["z"],
    }
    # Only emitted when it was actually measured: a confirmed wall-thickness
    # requirement must be able to tell "measured 3 mm" from "nobody measured
    # anything", and a fabricated 0.0 would read as a failing wall instead of an
    # absent measurement. Absent -> the Gate reports required_but_unverified.
    if min_wall_thickness is not None:
        out["min_wall_thickness"] = float(min_wall_thickness)
    for sk in sketches or []:
        sid = sk.get("id")
        if not sid:
            continue
        fc = sk.get("fully_constrained")
        if fc is not None:
            out[f"{sid}__fully_constrained"] = 1.0 if fc else 0.0
        dof = sk.get("dof")
        if dof is not None:
            out[f"{sid}__dof"] = float(dof)
    return out


def introspect_document(ir: dict | None = None, out_dir: str = "", **_extra) -> dict:
    """Return a GeometryDigest-shaped dict for the IR (rebuilds the document)."""
    if not ir:
        return {"ok": False, "error": "missing ir"}

    if not out_dir:
        out_dir = tempfile.mkdtemp(prefix="tcad_introspect_")
    os.makedirs(out_dir, exist_ok=True)

    built = _build(ir, out_dir)
    # A digest measured from a stale/partial Tip would certify geometry the
    # IR did not actually produce — refuse and carry the build errors.
    if built["errors"]:
        _close_doc(built["doc"])
        return {"ok": False, "error": "; ".join(
            f'{e.get("feature_id") or "?"}: {e.get("message")}' for e in built["errors"]
        )}
    try:
        return digest_built(ir, built)
    finally:
        _close_doc(built["doc"])


def digest_built(ir, built):
    shape = built["result_shape"]
    measure = _measure(shape)
    holes = _measure_holes(shape)
    faces = _measure_faces(shape)
    edges = _measure_edges(shape)
    min_wall = _measure_min_wall_thickness(shape)

    digest = {
        "model_id": ir.get("model_id") or "",
        "ir_version": int(ir.get("version", 0)),
        "feature_chain": [
            {"id": fc["id"], "name": fc["name"], "op": fc["op"],
             "params": fc["params"], "suppressed": fc["suppressed"]}
            for fc in built["feature_chain"]
        ],
        "topology": {
            "solids": measure["solids"], "faces": measure["faces"],
            "edges": measure["edges"], "vertexes": measure["vertexes"],
            "shells": measure["shells"],
        },
        "bbox": measure["bbox"],
        "volume": measure["volume"],
        "area": measure["area"],
        "shape_type": measure["shape_type"],
        "is_valid": measure["is_valid"],
        "key_dimensions": _key_dimensions(measure, built["sketches"], min_wall),
        "holes": holes,
        "faces": faces,
        "edges": edges,
        "spec_deviation": {},
        "measurements_available": shape is not None and not shape.isNull(),
        "body_solids": {b["id"]: len(b["shape"].Solids) for b in built["body_results"]},
        "text": "",
    }
    digest["text"] = _render_text(
        digest["model_id"], digest["ir_version"], built["feature_chain"], measure,
        built["sketches"], holes, min_wall,
    )

    digest["ok"] = True
    return digest
