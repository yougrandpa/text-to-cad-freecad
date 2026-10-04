"""Capability-based mode aliases must preserve native enums and fail closed.

Pure Python boundary tests; real shapes, holes and FCStd parametric editing are
covered by contract/test_patterns_mirror.py on the installed FreeCAD kernel.
"""

from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def compiler(monkeypatch):
    monkeypatch.setitem(sys.modules, "FreeCAD", SimpleNamespace(Version=lambda: ["1", "0", "0"]))
    monkeypatch.setitem(sys.modules, "Part", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "Sketcher", SimpleNamespace())
    path = Path(__file__).resolve().parents[2] / "tcad/worker/compiler.py"
    return runpy.run_path(str(path))


class EnumFeature:
    TypeId = "PartDesign::LinearPattern"
    Name = "pattern"
    PropertiesList = ["Mode", "Mode2"]

    def __init__(self, choices):
        self.choices = choices

    def getEnumerationsOfProperty(self, name):
        return self.choices

    def getTypeIdOfProperty(self, name):
        return "App::PropertyEnumeration"

    def __setattr__(self, key, value):
        if key in self.PropertiesList and value not in self.choices:
            raise ValueError(f"{value!r} is not a supported enumeration")
        super().__setattr__(key, value)


@pytest.mark.parametrize("choices,given,expected", [
    (["length", "offset"], "Extent", "length"),
    (["length", "offset"], "Spacing", "offset"),
    (["length", "offset"], "length", "length"),
    (["length", "offset"], "offset", "offset"),
    (["Extent", "Spacing"], "Extent", "Extent"),
    (["Extent", "Spacing"], "Spacing", "Spacing"),
    (["Extent", "Spacing"], "length", "Extent"),
    (["Extent", "Spacing"], "offset", "Spacing"),
])
@pytest.mark.parametrize("key,property_name", [("mode", "Mode"), ("mode2", "Mode2")])
def test_known_pattern_modes_use_live_enumeration(compiler, choices, given, expected, key, property_name):
    obj = EnumFeature(choices)
    assert compiler["_assign_props"](obj, {key: given}) == []
    assert getattr(obj, property_name) == expected


def test_unknown_mode_is_never_coerced_to_a_working_default(compiler):
    obj = EnumFeature(["length", "offset"])
    errors = compiler["_assign_props"](obj, {"mode": "make_something"})
    assert errors and errors[0]["kind"] == "compile"
    assert "make_something" in errors[0]["message"]


def test_alias_is_not_applied_to_other_feature_enumerations(compiler):
    obj = EnumFeature(["length", "offset"])
    obj.TypeId = "PartDesign::AnotherFeature"
    errors = compiler["_assign_props"](obj, {"mode": "Extent"})
    assert errors and "Extent" in errors[0]["message"]


def test_absent_property_is_refused_with_actual_kernel_version(compiler):
    errors = compiler["_assign_props"](EnumFeature(["length"]), {"side_type": "Two sides"})
    assert len(errors) == 1
    assert errors[0]["feature_id"] == "pattern"
    assert "unsupported property 'side_type'" in errors[0]["message"]
    assert "FreeCAD 1.0.0" in errors[0]["message"]


def test_missing_feature_type_retains_identity_and_actionable_error(compiler):
    def missing(*args):
        raise TypeError("not a document object type")

    state = compiler["_apply_feature"](
        SimpleNamespace(addObject=missing), None,
        {"id": "rings", "op": "circular_pattern", "params": {}}, {})
    assert state["errors"][0]["feature_id"] == "rings"
    assert "FreeCAD 1.0.0" in state["errors"][0]["message"]
    assert "polar_pattern" in state["errors"][0]["message"]


def test_only_explicitly_experimental_types_can_be_optional(compiler):
    from tcad.ir.capability import EXPERIMENTAL, capability

    path = Path(__file__).resolve().parents[2] / "tcad/worker/selftest.py"
    optional = runpy.run_path(str(path))["_OPTIONAL_FEATURE_OPS"]
    assert optional == {"circular_pattern"}
    assert all(capability(op).tier == EXPERIMENTAL for op in optional)
