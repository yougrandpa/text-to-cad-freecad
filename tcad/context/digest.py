"""``GeometryDigest`` text projection (design §4.3, the core of the C layer).

This renders a *program-generated* plain-text summary of the model's geometry
for the LLM context window. It is deterministic and NEVER calls an LLM — a
model must not be asked to "summarise its own geometry" (that would be the
generator grading its own paper).

Budget: the rendered text targets ≤ ~2000 tokens. We estimate tokens as
``len(text) // 4`` (≈4 chars/token, conservative) and the assembler enforces the
hard 2000-token cap. The text must stay well under that even for an 8-feature
part, because it projects (counts + key dims), it never dumps vertices.

Contract with worker/verify (frozen ``GeometryDigest`` has no per-sketch field):
  * per-sketch constraint state lives in ``digest.key_dimensions`` under the
    reserved keys ``"<sketch_id>__fully_constrained"`` (1.0/0.0) and
    ``"<sketch_id>__dof"`` (float). Same convention ``checks_solid`` reads.
  * ``measurements_available is False`` means the worker was unreachable and the
    digest is structure-only: we MUST stamp an explicit ``[未验证几何]`` banner so
    the model knows it is flying blind rather than assuming the geometry is
    confirmed (design §4.3 failure handling).
"""

from __future__ import annotations

from tcad.core.types import GeometryDigest
from tcad.ir.schema import IrDocument


_UNVERIFIED_BANNER = "[未验证几何]"


def _fmt_params(params: dict) -> str:
    if not params:
        return ""
    # keep it short: round floats, drop long nested structures
    parts = []
    for k, v in params.items():
        if isinstance(v, float):
            parts.append(f"{k}={v:g}")
        elif isinstance(v, (int, str, bool)):
            parts.append(f"{k}={v}")
        else:
            parts.append(f"{k}=…")
    return " " + " ".join(parts)


def _face_fields(f: Any) -> tuple[str, float, list, list]:
    """Read a face entry whether it arrived as a ``FaceInfo`` or a raw dict."""
    if isinstance(f, dict):
        return (str(f.get("name", "?")), float(f.get("area") or 0.0),
                list(f.get("normal") or []), list(f.get("center") or []))
    return (str(getattr(f, "name", "?")), float(getattr(f, "area", 0.0) or 0.0),
            list(getattr(f, "normal", None) or []), list(getattr(f, "center", None) or []))


def _sketch_state_line(digest: GeometryDigest, ir: IrDocument) -> list[str]:
    lines: list[str] = []
    for sk in ir.all_sketches():
        fc = digest.key_dimensions.get(f"{sk.id}__fully_constrained")
        dof = digest.key_dimensions.get(f"{sk.id}__dof")
        if fc is None:
            state = "unmeasured"
        elif fc == 1.0:
            state = "fully-constrained"
        else:
            state = f"UNDER-CONSTRAINED (DoF={dof})"
        lines.append(f"  - sketch {sk.name} ({sk.id}): {state}")
    return lines


def render_digest_text(digest: GeometryDigest, ir: IrDocument) -> str:
    """Deterministic, program-generated geometry summary (no LLM)."""
    out: list[str] = []

    if not digest.measurements_available:
        out.append(
            f"{_UNVERIFIED_BANNER} geometry not measured (worker unreachable); "
            "values below are structure-only and NOT confirmed."
        )
        out.append("")

    out.append(f"# Geometry digest — model {digest.model_id} v{digest.ir_version}")
    out.append(f"shape_type: {digest.shape_type or 'n/a'}  valid={digest.is_valid}")

    # Feature chain in build order (name, stable id, op, key params).
    #
    # The stable ``id`` is printed, not just the human name: a later turn edits
    # the model by referencing these ids ("the four holes the user just made"),
    # and a model that only ever saw names has to guess at ids it was never told.
    out.append("feature_chain (build order; id is the stable reference for ir_patch):")
    if digest.feature_chain:
        for fd in digest.feature_chain:
            mark = " [SUPPRESSED]" if fd.suppressed else ""
            out.append(f"  - {fd.name} [{fd.id}] ({fd.op}){_fmt_params(fd.params)}{mark}")
    else:
        # fall back to the IR feature order if the worker left feature_chain empty
        for b in ir.bodies:
            for f in b.features:
                out.append(f"  - {f.name} [{f.id}] ({f.op}){_fmt_params(f.params)}")

    # per-sketch constraint state
    out.append("sketches:")
    sk_lines = _sketch_state_line(digest, ir)
    if sk_lines:
        out.extend(sk_lines)
    else:
        out.append("  (none)")

    # topology counts
    t = digest.topology
    out.append(
        f"topology: solids={t.solids} faces={t.faces} edges={t.edges} "
        f"vertexes={t.vertexes} shells={t.shells}"
    )

    # bbox XYZ + volume
    b = digest.bbox
    out.append(f"bbox(mm): x={b.x:g} y={b.y:g} z={b.z:g} "
               f"(min x={b.x_min:g} y={b.y_min:g} z={b.z_min:g})")
    out.append(f"volume(mm^3): {digest.volume:g}  area(mm^2): {digest.area:g}")

    # key dimensions (carried by the worker, e.g. holes / thickness)
    kd = {k: v for k, v in digest.key_dimensions.items()
          if not k.endswith("__fully_constrained") and not k.endswith("__dof")}
    if kd:
        out.append("key_dimensions: " + " ".join(f"{k}={v:g}" for k, v in kd.items()))

    # Planar faces, with the name a `plane: {"kind": "face"}` sketch attaches to.
    # Printed because the tool description tells the model to look the name up
    # here rather than guess it — an instruction that was unfollowable while this
    # block did not exist.
    faces = getattr(digest, "faces", None) or []
    if faces:
        out.append(
            f"planar faces ({len(faces)}; attach a sketch with "
            'plane={"kind":"face","feature_id":<feature>,"sub":"<name>"}):'
        )
        for f in faces:
            name, area, normal, center = _face_fields(f)
            line = f"  - {name}: area={area:g}"
            if normal and center:
                line += (f" normal=({normal[0]:g}, {normal[1]:g}, {normal[2]:g})"
                         f" center=({center[0]:g}, {center[1]:g}, {center[2]:g})")
            out.append(line)

    # Edges, with the name a fillet/chamfer selects. Same reasoning as the faces:
    # the description points the model here, so here is where the names must be.
    edges = getattr(digest, "edges", None) or []
    if edges:
        out.append(
            f"edges ({len(edges)}; select with base_feature=<feature> + "
            'sub_elements=["EdgeN", …]):'
        )
        for e in edges:
            name = e.get("name") if isinstance(e, dict) else getattr(e, "name", "?")
            kind = e.get("kind") if isinstance(e, dict) else getattr(e, "kind", "")
            length = e.get("length") if isinstance(e, dict) else getattr(e, "length", 0.0)
            mid = (e.get("mid") if isinstance(e, dict) else getattr(e, "mid", None)) or []
            direction = ((e.get("direction") if isinstance(e, dict)
                          else getattr(e, "direction", None)) or [])
            line = f"  - {name}: {kind} length={length:g}"
            if mid:
                line += f" mid=({mid[0]:g}, {mid[1]:g}, {mid[2]:g})"
            if direction:
                line += f" dir=({direction[0]:g}, {direction[1]:g}, {direction[2]:g})"
            out.append(line)

    # spec deviation
    if digest.spec_deviation:
        out.append("spec_deviation: " + " ".join(
            f"{k}={v:g}" for k, v in digest.spec_deviation.items()
        ))

    return "\n".join(out)
