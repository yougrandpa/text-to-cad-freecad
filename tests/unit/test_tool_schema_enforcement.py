"""Tool argument schemas are enforced, not just advertised (task §5-A).

``params_schema`` was sent to the model in the function declaration and never
checked. ``geo_view {"views": "iso"}`` therefore reached the handler with a
truthy string, which was forwarded to the worker as-is.

Deterministic; no FreeCAD.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tcad.core.types import ToolContext, ToolErrorKind, ToolResult, ToolSpec, ToolTier
from tcad.tools.base import build_default_registry, execute_tool
from tcad.tools.ir_tools import _ir_patch_schema
from tcad.tools.schema_check import ANNOTATIONS, SUPPORTED, check, validate_tool_args


def _ctx(**kw) -> ToolContext:
    base = dict(thread_id="th", turn_id="tn", model_id="m", data_dir="/tmp")
    base.update(kw)
    return ToolContext(**base)


# ══════════════════════════════════════════════════════════════════════════
# 1. the checker itself
# ══════════════════════════════════════════════════════════════════════════


def test_type_mismatch_is_reported_with_the_path():
    schema = {"type": "object",
              "properties": {"views": {"type": "array", "items": {"type": "string"}}}}
    problems = check({"views": "iso"}, schema)
    assert problems and "arguments.views" in problems[0]
    assert "expected array" in problems[0] and "str" in problems[0]


def test_booleans_are_not_integers():
    """``True`` must not satisfy ``{"type": "integer"}`` (Python's bool is an int)."""
    assert check(True, {"type": "integer"})
    assert check(1, {"type": "integer"}) == []
    assert check(True, {"type": "number"})
    assert check(1.5, {"type": "number"}) == []


def test_required_and_unknown_properties():
    schema = {"type": "object", "required": ["path"],
              "properties": {"path": {"type": "string"}},
              "additionalProperties": False}
    assert any("missing required" in p for p in check({}, schema))
    assert any("unknown property" in p for p in check({"path": "x", "nope": 1}, schema))
    assert check({"path": "x"}, schema) == []


def test_extra_properties_allowed_when_the_schema_says_so():
    schema = {"type": "object", "properties": {"a": {"type": "string"}},
              "additionalProperties": True}
    assert check({"a": "x", "extra": [1, 2]}, schema) == []


def test_enum_and_any_of_and_ref():
    root = {
        "$defs": {"Op": {"type": "object", "required": ["op"],
                         "properties": {"op": {"enum": ["pad", "pocket"]}}}},
        "type": "object",
        "properties": {
            "ops": {"type": "array", "items": {"$ref": "#/$defs/Op"}},
            "version": {"anyOf": [{"type": "integer"}, {"type": "string", "enum": ["current"]}]},
        },
    }
    assert check({"ops": [{"op": "pad"}], "version": "current"}, root) == []
    assert check({"ops": [{"op": "pad"}], "version": 3}, root) == []
    bad = check({"ops": [{"op": "fillet"}], "version": "latest"}, root)
    assert any("not one of" in p for p in bad)


def test_a_broken_ref_is_reported_rather_than_ignored():
    problems = check({}, {"$ref": "#/$defs/Nope"})
    assert problems and "not in $defs" in problems[0]


def test_nullable_assembly_error_identifies_connector_mistake_instead_of_null_branch():
    reg = build_default_registry(SimpleNamespace())
    args = {'assembly': {'grounded': ['base'], 'joints': [{
        'id':'rotor_axis','type':'Revolute','axis':[0,0,1],
        'side1':{'body_id':'base'},'side2':{'body_id':'rotor'}}]}, 'reason':'Connect rotor'}
    problems = validate_tool_args(args, reg.get('assembly_configure'))
    assert problems and 'joints[0]' in problems[0] and "unknown property 'axis'" in problems[0]
    assert 'expected null' not in problems[0]
    assert not validate_tool_args({'assembly':None,'reason':'Clear joints'}, reg.get('assembly_configure'))
    assert not validate_tool_args({'assembly':None,'base_version':'2','reason':'Clear joints'}, reg.get('assembly_configure'))


def test_snapshot_styles_and_views_match_the_renderer_instead_of_allowing_guesses():
    from tcad.render.snapshot import STYLES, VIEWS
    spec=build_default_registry(SimpleNamespace()).get('geo_view')
    for style in STYLES:
        assert not validate_tool_args({'style':style,'views':sorted(VIEWS)},spec)
    for args in ({'style':'shaded'},{'style':'solid'},{'views':['side']},{'views':[]}):
        assert validate_tool_args(args,spec)


def test_annotations_are_ignored():
    schema = {"type": "object", "title": "T", "description": "d", "default": {},
              "properties": {"a": {"type": "string", "title": "A", "description": "x"}}}
    assert check({"a": "y"}, schema) == []


# ══════════════════════════════════════════════════════════════════════════
# 2. no tool schema may use a keyword nothing enforces
# ══════════════════════════════════════════════════════════════════════════


_SCHEMA_VALUED = ("items", "additionalProperties", "not")
_SCHEMA_LIST = ("anyOf", "oneOf", "allOf")
_SCHEMA_MAP = ("properties", "$defs", "definitions")


def _schema_keywords(node, found: set[str]) -> None:
    """Collect keywords from *schema* objects only.

    ``properties``/``$defs`` keys are property names, not keywords, so a naive
    ``dict.keys()`` walk reports nonsense like ``"IrPatchOp"``.
    """
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        found.add(key)
        if key in _SCHEMA_MAP and isinstance(value, dict):
            for sub in value.values():
                _schema_keywords(sub, found)
        elif key in _SCHEMA_LIST and isinstance(value, list):
            for sub in value:
                _schema_keywords(sub, found)
        elif key in _SCHEMA_VALUED and isinstance(value, dict):
            _schema_keywords(value, found)


def test_every_keyword_used_by_any_tool_schema_is_handled():
    """A schema cannot quietly start using a constraint nothing checks.

    This is the guard that keeps "the schema is enforced" true as tools are
    added: introducing ``pattern`` or ``minimum`` without teaching the checker
    would make the schema partly decorative again.
    """
    svc = SimpleNamespace(store=None, worker=None, gate=None, renderer=None,
                          hooks=None, context=None, llm=None)
    registry = build_default_registry(svc, enable_privileged=True)
    assert registry._tools, "no tools registered"

    for name, spec in registry._tools.items():
        found: set[str] = set()
        _schema_keywords(spec.params_schema, found)
        unknown = found - SUPPORTED
        assert not unknown, (
            f"{name}: schema uses {sorted(unknown)}, which tcad/tools/schema_check "
            f"does not enforce (supported: {sorted(SUPPORTED)})")


# ══════════════════════════════════════════════════════════════════════════
# 3. execute_tool refuses a malformed call before the handler runs
# ══════════════════════════════════════════════════════════════════════════


def _spec(schema, handler):
    return ToolSpec(name="t", tier=ToolTier.READ, description="d",
                    params_schema=schema, handler=handler)


async def test_a_malformed_call_never_reaches_the_handler():
    called = []

    async def handler(args, ctx):
        called.append(args)
        return ToolResult(ok=True, content="ran")

    spec = _spec({"type": "object",
                  "properties": {"views": {"type": "array", "items": {"type": "string"}}}},
                 handler)
    outcome = await execute_tool(spec, {"views": "iso"}, _ctx(), allowed_tiers={ToolTier.READ})

    assert outcome.result.ok is False
    assert outcome.result.error.kind == ToolErrorKind.SCHEMA
    assert "arguments.views" in outcome.result.error.message
    assert called == [], "a rejected call must not execute"


async def test_a_well_formed_call_still_runs():
    async def handler(args, ctx):
        return ToolResult(ok=True, content="ran")

    spec = _spec({"type": "object", "required": ["views"],
                  "properties": {"views": {"type": "array", "items": {"type": "string"}}}},
                 handler)
    outcome = await execute_tool(spec, {"views": ["iso"]}, _ctx(), allowed_tiers={ToolTier.READ})
    assert outcome.result.ok is True and outcome.result.content == "ran"


async def test_a_missing_required_argument_is_refused():
    async def handler(args, ctx):
        return ToolResult(ok=True, content="ran")

    spec = _spec({"type": "object", "required": ["code"],
                  "properties": {"code": {"type": "string"}}}, handler)
    outcome = await execute_tool(spec, {}, _ctx(), allowed_tiers={ToolTier.READ})
    assert outcome.result.ok is False
    assert "missing required property 'code'" in outcome.result.error.message


# ══════════════════════════════════════════════════════════════════════════
# 4. the declared schema matches what the real tools accept
# ══════════════════════════════════════════════════════════════════════════


def test_ir_patch_schema_accepts_the_documented_current_spelling():
    """``base_version: "current"`` is a supported call, so it must validate.

    The schema used to declare ``integer``; enforcing that verbatim would have
    refused a documented convention. Fixing the declaration is part of making it
    enforceable.
    """
    spec = {"type": "object",
            "properties": {"base_version": _ir_patch_schema()["properties"]["base_version"],
                           "ops": {"type": "array"}},
            "required": ["base_version", "ops"]}
    assert check({"base_version": "current", "ops": []}, spec) == []
    assert check({"base_version": 3, "ops": []}, spec) == []
    assert check({"base_version": "latest", "ops": []}, spec)
    assert check({"ops": []}, spec), "base_version is required"


def test_the_real_geo_view_schema_rejects_a_string_views():
    """The exact defect, against the registered tool rather than a stub."""
    svc = SimpleNamespace(store=None, worker=None, gate=None, renderer=None,
                          hooks=None, context=None, llm=None)
    registry = build_default_registry(svc)
    spec = registry.get("geo_view")
    assert validate_tool_args({"views": "iso"}, spec)
    assert validate_tool_args({"views": ["iso"]}, spec) == []
    assert validate_tool_args({}, spec) == []


def test_real_tools_accept_their_documented_minimal_call():
    svc = SimpleNamespace(store=None, worker=None, gate=None, renderer=None,
                          hooks=None, context=None, llm=None)
    registry = build_default_registry(svc, enable_privileged=True)
    for name, args in [
        ("ir_get", {}), ("ir_digest", {}), ("ir_list_features", {}),
        ("geo_view", {"views": ["iso"], "style": "flat_edges"}),
        ("geo_measure", {"what": ["volume"]}),
        ("asset_export", {"fmt": "step"}), ("asset_import", {"path": "/tmp/x.step"}),
        ("ir_commit", {"message": "go"}),
        ("ir_patch", {"base_version": "current", "ops": [{"op":"add_body", "payload":{"id":"body", "name":"body"}, "reason":"Create body"}]}),
        ("raw_python", {"code": "print(1)"}),
    ]:
        spec = registry.get(name)
        assert spec is not None, name
        assert validate_tool_args(args, spec) == [], name


@pytest.mark.parametrize("value", [-721, 721, float("nan"), float("inf")])
def test_motion_angle_numeric_bounds_are_enforced(value):
    assert check(value, {"type": "number", "minimum": -720, "maximum": 720})


@pytest.mark.parametrize("value", [-720, 0, 720])
def test_motion_angle_boundary_values_are_allowed(value):
    assert check(value, {"type": "number", "minimum": -720, "maximum": 720}) == []


def test_an_unknown_op_answers_with_the_full_legal_set_not_one_branch():
    """The anyOf's first branch is not the operation set.

    A live session read `is not one of ['add_body']` as "only add_body is
    legal" and nearly abandoned edits (set_assembly/remove_body) it had
    already used successfully.
    """
    problems = check({'base_version': 'current', 'ops': [
        {'op': 'made_up_op', 'payload': {}, 'reason': 'probe'}]}, _ir_patch_schema())
    assert len(problems) == 1 and 'made_up_op' in problems[0]
    for op in ('add_body', 'update_body', 'remove_body', 'set_assembly', 'add_sketch'):
        assert op in problems[0], f"{op} missing from the legal-set answer"


async def test_a_write_tool_in_an_inspect_turn_points_at_the_right_turn_kind():
    """The denial must name the remedy; a bare "not permitted" cost a probe."""

    async def handler(args, ctx):
        return ToolResult(ok=True, content="ran")

    spec = ToolSpec(name="design_review", tier=ToolTier.WRITE, description="d",
                    params_schema={"type": "object"}, handler=handler)
    outcome = await execute_tool(spec, {}, _ctx(), allowed_tiers={ToolTier.READ})
    assert outcome.result.ok is False
    assert outcome.result.error.kind == ToolErrorKind.DENIED
    assert "create or modify turn" in (outcome.result.error.hint or "")
