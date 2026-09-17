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

    # feature chain in build order (name, op, key params)
    out.append("feature_chain (build order):")
    if digest.feature_chain:
        for fd in digest.feature_chain:
            mark = " [SUPPRESSED]" if fd.suppressed else ""
            out.append(f"  - {fd.name} ({fd.op}){_fmt_params(fd.params)}{mark}")
    else:
        # fall back to the IR feature order if the worker left feature_chain empty
        for b in ir.bodies:
            for f in b.features:
                out.append(f"  - {f.name} ({f.op}){_fmt_params(f.params)}")

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

    # spec deviation
    if digest.spec_deviation:
        out.append("spec_deviation: " + " ".join(
            f"{k}={v:g}" for k, v in digest.spec_deviation.items()
        ))

    return "\n".join(out)
