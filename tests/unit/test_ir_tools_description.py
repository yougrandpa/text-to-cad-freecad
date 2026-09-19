"""The tool description the model reads is part of the contract.

A model cannot use a capability it cannot see. That is not hypothetical: the
description advertised `additive_*`, so a model asked to put arms on a cube never
learned `additive_box` existed — it reached for `pad` instead, produced three
separate lumps, and the Gate's `solid_count` check failed. The harness had the
capability the whole time; the model could not find it.

These tests keep the description honest against the schema it describes.
"""

from __future__ import annotations

import re
from typing import get_args

from tcad.ir.schema import FeatureOp
from tcad.tools.ir_tools import _IR_PATCH_DESCRIPTION


def test_every_supported_op_is_named_in_the_description():
    """No wildcards, no "etc." — each op the IR accepts must appear literally."""
    missing = [op for op in get_args(FeatureOp) if op not in _IR_PATCH_DESCRIPTION]
    assert not missing, f"工具描述里没有列出这些 op：{missing}"


def _op_enumeration_block() -> str:
    """The lines directly under the "ops must be exact" heading, up to the blank
    line that ends them. Split on blank lines rather than a magic string."""
    lines = _IR_PATCH_DESCRIPTION.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if "op 必须精确" in ln)
    except StopIteration:  # pragma: no cover - the heading was renamed
        raise AssertionError("找不到 op 枚举段的小标题 —— 描述结构变了") from None
    out: list[str] = []
    for line in lines[start + 1:]:
        if not line.strip():
            break
        out.append(line)
    return "\n".join(out)


def test_the_op_enumeration_lists_every_op_and_no_wildcard():
    """`additive_*` reads as a family to a model and tells it nothing."""
    block = _op_enumeration_block()
    assert "additive_*" not in block, "枚举行里出现了通配写法"
    assert "subtractive_*" not in block
    for op in get_args(FeatureOp):
        assert op in block, f"op 枚举缺少 {op}"


def test_the_description_explains_why_growing_material_fails():
    """The sentence that would have saved the reported session: merging requires
    spatial overlap, and `refs` is not a geometric relationship."""
    text = _IR_PATCH_DESCRIPTION
    assert "overlap" in text, "没有说明「必须几何相交才会合并」"
    assert "no geometric relationship" in text, "没有说明 refs 不产生几何关系"


def test_the_description_names_the_face_attachment_route():
    """'Put it on the torso' is only actionable if the model knows how."""
    text = _IR_PATCH_DESCRIPTION
    assert '"kind":"face"' in text.replace(" ", "")
    assert "sub" in text, "没有说明面附着要写 sub（面编号）"


def test_the_primitives_are_described_as_sketch_free():
    """`additive_box` takes no profile — a model that assumes otherwise will pass
    a profile_sketch and wonder why nothing happened."""
    text = _IR_PATCH_DESCRIPTION
    assert "PRIMITIVES" in text or "no sketch needed" in text


def test_the_description_says_requirements_are_the_only_evidence():
    """A model that does not record the user's numbers leaves the Gate with
    nothing to judge against — and a green Gate then means almost nothing."""
    text = _IR_PATCH_DESCRIPTION
    assert "ONLY THING THE GATE JUDGES AGAINST" in text
    assert "confirmed" in text


def test_the_description_still_carries_the_two_silent_failure_traps():
    """Both were learned the hard way; losing either one costs a model a whole
    session. (Regression guard on the earlier fixes.)"""
    text = _IR_PATCH_DESCRIPTION
    assert "reversed" in text and "empty space" in text, "Pocket 方向那条丢了"
    assert "Invalid constraint index" in text, "草图定位那条丢了"
