"""§5-A: the op enum, the compiler's type map, and what is actually proven.

The compiler handles every op with one ``addObject`` call, so "fillet is wired"
and "fillet works" were being reported as the same fact — to the validator, to
the model-facing tool description, and in the docs. ``tcad/ir/capability.py`` is
now the single source separating them; these tests are what keeps it a single
source rather than a third opinion:

  * no op may exist without a tier (a new enum member must be classified),
  * the IR enum, the schema map and the worker compiler's hand-copied map may
    not drift apart,
  * an op may not be claimed VERIFIED unless the test file it cites exists,
  * the tier the model reads is generated from the tier the validator uses.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tcad.ir import capability
from tcad.ir.capability import EXPERIMENTAL, VERIFIED, OpCapability
from tcad.ir.schema import FEATURE_TYPE_MAP as SCHEMA_TYPE_MAP
from tcad.ir.schema import FeatureSpec, IrDocument, BodySpec
from tcad.ir.validate import validate_ir
from tcad.tools.ir_tools import _IR_PATCH_DESCRIPTION

REPO_ROOT = Path(__file__).resolve().parents[2]


def _compiler_type_map() -> dict[str, str]:
    """``tcad.worker.compiler.FEATURE_TYPE_MAP`` without importing FreeCAD.

    The worker module is under an import ban (stdlib + FreeCAD only) and imports
    FreeCAD at module level, so it cannot be loaded in a unit test. The table is
    a plain literal, so reading it out of the source is exact.
    """
    source = (REPO_ROOT / "tcad" / "worker" / "compiler.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "FEATURE_TYPE_MAP":
                    return ast.literal_eval(node.value)
    raise AssertionError("compiler.py no longer defines FEATURE_TYPE_MAP")


# ── the table covers the enum, and nothing else ─────────────────────────────

def test_every_op_has_a_capability_tier():
    """A new FeatureOp must be classified, not silently treated as working."""
    unclassified = [op for op in capability.all_ops() if capability.capability(op) is None]
    assert not unclassified, f"未分级的 op：{unclassified}"


def test_table_has_no_stale_entries():
    unknown = sorted(set(capability._CAPABILITIES) - set(capability.all_ops()))
    assert not unknown, f"能力表里有 enum 之外的 op：{unknown}"


def test_tiers_are_only_verified_or_experimental():
    bad = {op: c.tier for op, c in capability._CAPABILITIES.items()
           if c.tier not in (VERIFIED, EXPERIMENTAL)}
    assert not bad


def test_every_experimental_op_admits_there_is_no_kernel_proof():
    """The claim an experimental op must never be able to lose: no test ran."""
    for op, c in capability._CAPABILITIES.items():
        if c.tier == EXPERIMENTAL:
            assert "no real-kernel test" in c.proof, f"{op} 的实验性说明不诚实"


# ── enum / schema map / compiler map agree ──────────────────────────────────

def test_schema_type_map_covers_exactly_the_enum():
    assert sorted(SCHEMA_TYPE_MAP) == sorted(capability.all_ops())


def test_compiler_map_has_not_drifted_from_the_schema():
    assert _compiler_type_map() == SCHEMA_TYPE_MAP


def test_capability_table_names_the_same_ops_the_compiler_handles():
    assert sorted(capability._CAPABILITIES) == sorted(_compiler_type_map())


# ── "verified" must point at a test file that exists ────────────────────────

_TEST_FILE = re.compile(r"test_\w+\.py")


def test_verified_claims_cite_real_test_files():
    for op, c in capability._CAPABILITIES.items():
        if c.tier != VERIFIED:
            continue
        names = _TEST_FILE.findall(c.proof)
        assert names, f"{op} claims verified but cites no test file"
        for name in names:
            hits = list((REPO_ROOT / "tests").rglob(name))
            assert hits, f"{op}: proof cites {name}, which does not exist"


def test_only_proven_ops_are_claimed_verified():
    """The phase-1 scope, plus the ops promoted out of it.

    ``revolution`` and ``groove`` joined the list once real-kernel tests measured
    an analytic volume each, a STEP read-back, and a parametric edit of the
    feature's own Angle in the delivered FCStd. ``fillet``/``chamfer`` joined
    after ``test_fillet_chamfer.py`` measured that rounding/beveling the named
    edges removes exactly ``(1-π/4)r²t`` / ``(d²/2)t`` per edge, reopened the
    delivered FCStd and re-cut it with a new radius. ``mirrored`` and the two
    pattern ops joined after ``test_patterns_mirror.py`` measured reflected/
    repeated positions off the BRep and re-parametrised them in the reopened
    FCStd. The five primitives joined last, once the IR could carry a placement
    and ``test_primitive_placement.py`` measured that a placed pin merges into
    the plate at the coordinates it names, a placed cut removes exactly its own
    volume, and a placed cut that misses is refused. ``draft``/``thickness``
    joined when ``test_draft_thickness.py`` measured the analytic prismatoid and
    wall volumes, the reopened FCStd recomputing on a new Angle/Value, and the
    two refusal families (a missing plane; faces parallel to the neutral plane).
    Growing this list is fine; this assertion forces it to be a decision rather
    than a drift.
    """
    verified, experimental = capability.ops_by_tier()
    assert verified == ["additive_box", "additive_cone", "additive_cylinder", "additive_sphere",
                        "chamfer", "draft", "fillet", "groove", "linear_pattern",
                        "mirrored", "pad", "pocket", "polar_pattern",
                        "revolution", "subtractive_box", "subtractive_cone", "subtractive_cylinder",
                        "subtractive_sphere", "thickness"]
    assert len(experimental) == len(capability.all_ops()) - 19


# ── the model reads the same facts ──────────────────────────────────────────

def test_describe_for_model_lists_every_op_and_both_tiers():
    text = capability.describe_for_model()
    assert "{OP_CAPABILITY}" not in text
    for op in capability.all_ops():
        assert op in text, f"生成的 op 清单缺少 {op}"
    assert "verified:" in text and "experimental:" in text
    assert "NOTHING proves the shape" in text


def test_ir_patch_description_carries_the_generated_block():
    """The placeholder must be substituted, and substituted with the live table."""
    assert "{OP_CAPABILITY}" not in _IR_PATCH_DESCRIPTION
    assert capability.describe_for_model() in _IR_PATCH_DESCRIPTION


_EXPERIMENTAL_OPS = [
    op for op, c in capability._CAPABILITIES.items() if c.tier == EXPERIMENTAL
]


def test_ir_patch_description_tiers_every_op_it_names():
    """An op the model is told about must be tiered in the same breath."""
    section = _IR_PATCH_DESCRIPTION[_IR_PATCH_DESCRIPTION.index("verified:"):
                                    _IR_PATCH_DESCRIPTION.index("pad params:")]
    for op in _EXPERIMENTAL_OPS:
        assert op in section, f"{op} 出现在枚举里但不在能力段中"


def test_describe_for_model_carries_a_note_for_each_gap():
    for op, c in capability._CAPABILITIES.items():
        if c.tier == EXPERIMENTAL and c.gap:
            assert f"{op}: {c.gap}" in capability.describe_for_model()


# ── the validator uses the same table ───────────────────────────────────────

def _doc_with_op(op: str, params: dict) -> IrDocument:
    return IrDocument(model_id="m", bodies=[BodySpec(
        id="b", name="b",
        features=[FeatureSpec(id="f", name="f", op=op, params=params)])])  # type: ignore[arg-type]


def test_validate_warns_when_an_experimental_op_is_used():
    issues = validate_ir(_doc_with_op("multi_transform", {"some_prop": 1}))
    warn = [i for i in issues if i.code == "op_experimental"]
    assert warn, "multi_transform 未被标为实验性"
    assert warn[0].severity == "warn"
    assert "no real-kernel test" in warn[0].message
    assert warn[0].target_id == "f"


def test_a_promoted_op_no_longer_warns():
    """fillet/chamfer/mirrored/patterns were EXPERIMENTAL until the kernel
    measured them."""
    for op, params in (("fillet", {"radius": 2.0}), ("chamfer", {"size": 2.0}),
                       ("mirrored", {"plane": {"kind": "origin_plane", "plane": "XZ"}}),
                       ("linear_pattern", {"axis": "X", "occurrences": 3}),
                       ("polar_pattern", {"axis": "Z", "occurrences": 4})):
        issues = validate_ir(_doc_with_op(op, params))
        assert not [i for i in issues if i.code == "op_experimental"], op


def test_validate_stays_quiet_for_a_verified_op():
    issues = validate_ir(_doc_with_op("pad", {"length": 8.0}))
    assert not [i for i in issues if i.code == "op_experimental"]
    assert not [i for i in issues if i.severity == "error"], issues


def test_capability_helper_agrees_with_the_table():
    assert capability.is_verified("pad")
    assert capability.is_verified("fillet") and capability.is_verified("chamfer")
    assert capability.is_verified("draft") and capability.is_verified("thickness")
    assert not capability.is_verified("multi_transform")
    assert capability.capability("not-an-op") is None
    assert isinstance(OpCapability(VERIFIED, "proof"), tuple)
