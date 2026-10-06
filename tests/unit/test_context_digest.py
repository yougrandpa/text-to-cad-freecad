"""Tests for the program-generated geometry digest text (design §4.3)."""

from __future__ import annotations

from tcad.context.digest import render_digest_text, _UNVERIFIED_BANNER
from tests.fixtures.gate_fixtures import make_digest, make_ir


def test_good_digest_has_no_banner_and_fits_budget():
    d = make_digest()
    ir = make_ir()
    text = render_digest_text(d, ir)
    assert _UNVERIFIED_BANNER not in text
    # hard cap is ~2000 tokens; the projection must stay well under
    assert len(text) // 4 <= 2000
    # required content blocks present
    assert "feature_chain" in text
    assert "topology:" in text
    assert "bbox(mm):" in text
    assert "volume(mm^3):" in text
    assert "sk_base" in text  # per-sketch constraint state
    assert "fully-constrained" in text


def test_unverified_digest_carries_banner():
    d = make_digest(measurements_available=False)
    ir = make_ir()
    text = render_digest_text(d, ir)
    assert _UNVERIFIED_BANNER in text
    assert len(text) // 4 <= 2000


def test_underconstrained_sketch_reported():
    d = make_digest()
    d.key_dimensions["sk_base__fully_constrained"] = 0.0
    d.key_dimensions["sk_base__dof"] = 2.0
    ir = make_ir()
    text = render_digest_text(d, ir)
    assert "UNDER-CONSTRAINED" in text


def test_sub_element_names_show_the_feature_that_owns_the_number():
    from tcad.core.types import FaceInfo, EdgeInfo
    d = make_digest()
    d.faces = [FaceInfo(name="Face6", feature_id="base", normal=[0, 0, 1], center=[0, 0, 8])]
    d.edges = [EdgeInfo(name="Edge1", feature_id="button", kind="Line", length=3)]
    text = render_digest_text(d, make_ir())
    assert "base/Face6" in text
    assert "button/Edge1" in text
    d.faces[0].feature_id = None
    assert "[legacy owner unknown] Face6" in render_digest_text(d, make_ir())
