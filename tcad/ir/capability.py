"""Which ``FeatureOp`` values are proven, and which are only instantiated.

The compiler turns an op into a FreeCAD object with one ``addObject`` call, so
"we have a handler for fillet" and "fillet produced the fillet that was asked
for" are very different claims. This module keeps them apart in ONE place, so
the model-facing tool description, the validator and the review report cannot
each invent their own version of what works.

Tiers:
  * ``VERIFIED``      — a test that runs the real FreeCAD kernel builds this op
                        and measures the geometry it claims.
  * ``EXPERIMENTAL``  — an object mapping exists, but availability depends on the
                        FreeCAD build and nothing proves the resulting shape.
                        Several of these also cannot express the references they
                        need (see ``gap``).

``gap`` records the structural reason an op cannot currently work, when known:
the IR carries no sub-element reference field, so a PartDesign feature that
reads its edges from a ``Base`` link has nothing to be given. The compiler
attempts a plain ``setattr`` for scalar params and a ``LinkSub`` target is not a
scalar — the attempt fails and the build errors out, which is why those keys
stay out of the model's instructions rather than being half-documented.
"""

from __future__ import annotations

from typing import NamedTuple, get_args

from tcad.ir.schema import FeatureOp

VERIFIED = "verified"
EXPERIMENTAL = "experimental"


def all_ops() -> tuple[str, ...]:
    """Every op the IR schema admits — the single source the tables are checked
    against, so a new enum member cannot arrive without a capability tier."""
    return get_args(FeatureOp)


class OpCapability(NamedTuple):
    tier: str
    proof: str          # for VERIFIED: where the claim is measured
    gap: str = ""       # for EXPERIMENTAL: what is missing / what can go wrong


_CAPABILITIES: dict[str, OpCapability] = {
    "additive_cone": OpCapability(VERIFIED, "tests/contract/test_cone_assembly_motion.py (placed analytic cone volume and conical bore subtraction)"),
    "subtractive_cone": OpCapability(VERIFIED, "tests/contract/test_cone_assembly_motion.py (placed analytic cone volume and conical bore subtraction)"),
    # ── verified on the real kernel ─────────────────────────────────────────
    "pad": OpCapability(
        VERIFIED,
        "tests/contract/test_sketch_planes.py, test_samples_acceptance.py "
        "(volume + bbox + STEP read-back)"),
    "pocket": OpCapability(
        VERIFIED,
        "tests/contract/test_samples_acceptance.py, test_measured_holes.py "
        "(through and blind holes measured off the BRep)"),
    "additive_box": OpCapability(
        VERIFIED,
        "tests/contract/test_sketch_planes.py (at the origin), "
        "test_primitive_placement.py (placed and rotated: a 20x10x5 box turned "
        "90 deg about Z sits at x[40,50] y[25,45] on the plate, +1000 mm^3)"),
    "revolution": OpCapability(
        VERIFIED,
        "tests/contract/test_revolution.py (analytic volume of a stepped shaft, "
        "bbox, STEP read-back, FCStd reopen + Angle edit)"),
    "groove": OpCapability(
        VERIFIED,
        "tests/contract/test_groove.py (a revolved cut removes its analytic ring, "
        "half an Angle removes half of it, a cut that misses the material errors, "
        "STEP read-back, FCStd reopen + Angle edit)"),
    "additive_loft": OpCapability(VERIFIED,
        "tests/contract/test_loft.py — native multi-section loft, analytic volume, STEP read-back and live datum-plane edit"),
    "subtractive_loft": OpCapability(VERIFIED,
        "tests/contract/test_loft.py — a native tapered bore removes its analytic volume and rejects missing sections"),
    "additive_cylinder": OpCapability(
        VERIFIED,
        "tests/contract/test_primitive_placement.py — r6 h20 at (10,10,0) merges "
        "with the 80x50x8 plate into ONE solid of 32000 + 432*pi mm^3, and the "
        "same pin turned 90 deg about Y lies along +X (volume == plate + 720*pi "
        "minus the measured circular segment below z=8)"),
    "additive_sphere": OpCapability(
        VERIFIED,
        "tests/contract/test_primitive_placement.py — r6 centred on the plate's "
        "top face adds exactly half a sphere: 32000 + 144*pi mm^3, still one solid"),
    "subtractive_box": OpCapability(
        VERIFIED,
        "tests/contract/test_primitive_placement.py — 10x10x4 at (40,20,4) "
        "removes exactly 400 mm^3 out of the plate's thickness, bbox unchanged"),
    "subtractive_cylinder": OpCapability(
        VERIFIED,
        "tests/contract/test_primitive_placement.py — r5 h20 at (20,25,-6) drills "
        "the plate through (32000 - 200*pi mm^3, one solid); the same cut moved "
        "off the material is refused by name ('did not change the solid')"),
    "subtractive_sphere": OpCapability(
        VERIFIED,
        "tests/contract/test_primitive_placement.py — r3 buried at (40,25,4) "
        "removes exactly its own volume (32000 - 36*pi = 31886.90 mm^3, one solid); "
        "the same sphere left at the origin scoops one octant (14.14) out of the "
        "plate's corner"),

    # ── instantiated but unverified ─────────────────────────────────────────
    "hole": OpCapability(
        EXPERIMENTAL, "no real-kernel test",
        "use a circular sketch + pocket instead: that is the idiom whose holes "
        "are measured off the BRep"),
    "fillet": OpCapability(
        VERIFIED,
        "tests/contract/test_fillet_chamfer.py — four named vertical edges rounded "
        "with r, volume == w·h·t - 4(1-π/4)r²t to 1e-6, bbox unchanged; the "
        "delivered FCStd reopens and Radius 5→8 re-cuts; a bad edge name and an "
        "impossible radius are refused, not silently ignored",
        ""),
    "chamfer": OpCapability(
        VERIFIED,
        "tests/contract/test_fillet_chamfer.py — same edges beveled with d, "
        "volume == w·h·t - 4(d²/2)t to 1e-6 (the edges are selected through the "
        "names ir_digest publishes, so the discovery path is exercised too)",
        ""),
    "draft": OpCapability(
        VERIFIED,
        "tests/contract/test_draft_thickness.py — the four side faces of a 40x40x20 "
        "box drafted 5 deg off the XY plane land at exactly the analytic prismatoid "
        "29282.0083 mm^3 (h/6*(A0+4Am+A1), 1e-6) with the footprint unchanged; "
        "reversed=true expands to 34881.2827 mm^3 and the envelope grows to 43.5; "
        "neutral plane as the bottom FACE gives the same volume; a bad face name and "
        "a missing plane are refused by name (the latter would return a null shape)"),
    "thickness": OpCapability(
        VERIFIED,
        "tests/contract/test_draft_thickness.py — opening the top face of a 40x40x20 "
        "box with value=2 shells it to exactly the analytic open box 8672 mm^3; the "
        "delivered FCStd reopens and editing Value 2->4 recomputes to 15616 mm^3; a "
        "missing face list is refused by name"),
    "mirrored": OpCapability(
        VERIFIED,
        "tests/contract/test_patterns_mirror.py — mirrored across XZ/XY and across a "
        "named face of the part itself; volume doubles to 2·w·h·t and the envelope "
        "reflects exactly, the FCStd reopens and Suppressed=True returns the plain "
        "plate, a missing plane or a bad FaceN is refused",
        ""),
    "linear_pattern": OpCapability(
        VERIFIED,
        "tests/contract/test_patterns_mirror.py — Extent and Spacing modes place the "
        "hole at measured positions (20/35/50, 20/45, and along Y), volume == "
        "w·h·t − n·πr²t, FCStd reopens with Occurrences 3→5; no axis is a refusal, "
        "because the kernel otherwise returns ONE occurrence without an error",
        ""),
    "circular_pattern": OpCapability(
        EXPERIMENTAL, "no real-kernel test",
        "PartDesign::CircularPattern is unavailable on FreeCAD 1.0; builds that "
        "provide it use a spacing-driven pattern "
        "(NumberCircles/RadialDistance) and cannot express 'N copies over an angle'; "
        "use polar_pattern for bolt circles"),
    "polar_pattern": OpCapability(
        VERIFIED,
        "tests/contract/test_patterns_mirror.py — 4 occurrences over 270° and 2 over "
        "180° put the holes on the measured bolt circle (15,0)/(0,15)/(−15,0)/(0,−15); "
        "requires params.axis ('X'/'Y'/'Z'), and its absence is a refusal rather "
        "than a silently un-rotated copy",
        ""),
    "multi_transform": OpCapability(EXPERIMENTAL, "no real-kernel test"),
    "datum_plane": OpCapability(
        VERIFIED, "tests/contract/test_loft.py — placed and rotated native datum planes position world-coordinate profiles and remain live in FCStd"),
}


def capability(op: str) -> OpCapability | None:
    return _CAPABILITIES.get(op)


def is_verified(op: str) -> bool:
    cap = _CAPABILITIES.get(op)
    return bool(cap and cap.tier == VERIFIED)


def ops_by_tier() -> tuple[list[str], list[str]]:
    """(verified, experimental) — sorted, for stable prompts and reports."""
    verified = sorted(op for op, c in _CAPABILITIES.items() if c.tier == VERIFIED)
    experimental = sorted(op for op, c in _CAPABILITIES.items()
                          if c.tier == EXPERIMENTAL)
    return verified, experimental


def describe_for_model() -> str:
    """The op list + capability block of the ir_patch tool description.

    Generated from these tables so the promise the model reads and the tier the
    validator uses are one fact stated once, rather than two lists that drift.
    """
    verified, experimental = ops_by_tier()
    lines = [
        "      op 必须精确，不存在通配写法：",
        f"        {' | '.join(all_ops())}",
        "      OP CAPABILITY — verified means a test on the real FreeCAD kernel",
        "      measured the geometry; experimental means only an object mapping",
        "      exists. Installed-build availability varies; NOTHING proves the shape.",
        "      Prefer verified ops. An experimental op can silently change nothing",
        "      while still reporting a successful build, so re-measure after it",
        "      (ir_digest) instead of assuming your parameters took effect.",
        f"      verified:     {' | '.join(verified)}",
        f"      experimental: {' | '.join(experimental)}",
    ]
    notes = [f"        - {op}: {cap.gap}"
             for op, cap in sorted(_CAPABILITIES.items())
             if cap.tier == EXPERIMENTAL and cap.gap]
    lines.extend(notes)
    return "\n".join(lines)
