"""Feature help unlocks FIELDS, not the whole table (task P1-3).

Measured before this: one ``ir_help(topic=feature)`` call put ~16 KB of feature
schema in front of the model, most of it describing operations it had not
asked about. A smaller model reads that as noise. After loft help it now sees
loft's fields; the rest stay one scoped help call away.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from tcad.core.types import TurnKind
from tcad.loop.budget import BudgetLimits
from tcad.loop.engine import LoopConfig, LoopEngine
from tcad.tools.base import build_default_registry
from tcad.tools.feature_scope import MAX_SCOPED_OPS, scoped_fields
from tcad.tools.schema_check import check


def make_engine():
    services = SimpleNamespace()
    registry = build_default_registry(services)
    engine = LoopEngine(services, registry, BudgetLimits(), LoopConfig(require_design_review=True))
    return engine, registry.as_openai_tools(TurnKind.CREATE)


def patch_schema(tools):
    tool = next(t for t in tools if t["function"]["name"] == "ir_patch")
    return tool["function"]["parameters"]


def feature_branches(schema):
    items = schema["properties"]["ops"]["items"]
    return [b for b in items["anyOf"]
            if b["properties"]["op"]["enum"][0] in ("add_feature", "update_feature")]


def test_loft_help_exposes_only_loft_fields():
    engine, all_tools = make_engine()
    engine._authoring_topics.add("feature")
    engine._authoring_features = ["additive_loft"]
    schema = patch_schema(engine._authoring_surface(all_tools))
    branches = feature_branches(schema)
    assert len(branches) == 2  # one add, one update
    for branch in branches:
        props = branch["properties"]["payload"]["properties"]
        assert set(props) >= {"id", "name", "op", "profile_sketch", "sections", "params"}
        # Fields of OTHER operations are gone, not merely deprecated.
        assert not {"placement", "base_feature", "sub_elements", "plane", "refs"} & set(props)
        assert branch["properties"]["payload"]["properties"]["op"]["enum"] == ["additive_loft"]
        params = props["params"]
        assert set(params["properties"]) == {"ruled", "closed", "refine"}
        assert "additive_loft" in params["description"]
    add_branch = next(b for b in branches if b["properties"]["op"]["enum"] == ["add_feature"])
    assert "body_id" in add_branch["properties"]["payload"]["properties"]


def test_scoped_schema_accepts_the_documented_example():
    from tcad.tools.authoring import help_handler
    import asyncio
    engine, all_tools = make_engine()
    engine._authoring_topics.add("feature")
    engine._authoring_features = ["additive_loft"]
    schema = patch_schema(engine._authoring_surface(all_tools))
    example = json.loads(asyncio.run(help_handler(
        None, {"topic": "feature", "feature_op": "additive_loft"}, None)).content)["example"]
    # The scoped view is a presentation of the same contract: the documented
    # example must still validate against it.
    assert not check({"base_version": "current", "ops": [example]}, schema)


def test_scoping_never_grows_the_declaration():
    engine, all_tools = make_engine()
    engine._authoring_topics.add("feature")
    engine._authoring_features = []
    unscoped = len(json.dumps(patch_schema(engine._authoring_surface(all_tools))))

    def size(ops):
        engine._authoring_features = ops
        return len(json.dumps(patch_schema(engine._authoring_surface(all_tools))))

    assert size(["additive_loft"]) < unscoped
    assert size(["additive_loft", "fillet"]) < unscoped
    # The guarantee is structural: a union that would not be smaller falls
    # back to the complete branches instead of spending equal attention.
    assert size(["pad", "pocket", "fillet", "chamfer", "datum_plane", "hole"]) <= unscoped


def test_scope_branches_caps_the_op_count():
    from tcad.tools.feature_scope import scope_branches

    def fake_branch(action):
        blob = {"type": "object",
                "properties": {f"unused_{i}": {"type": "string", "description": "x" * 40}
                               for i in range(30)}}
        return {"type": "object", "additionalProperties": False,
                "required": ["op", "payload", "reason"],
                "properties": {"op": {"type": "string", "enum": [action]},
                               "payload": {"type": "object", "properties": {
                                   "op": {"type": "string", "enum": ["pad"]},
                                   "id": {"type": "string"}, "blob": blob}},
                               "reason": {"type": "string"}}}

    branches = [fake_branch("add_feature"), fake_branch("update_feature")]
    many = ["pad", "pocket", "fillet", "chamfer", "datum_plane", "hole"]
    scoped = scope_branches(branches, many)
    enum = scoped[0]["properties"]["payload"]["properties"]["op"]["enum"]
    assert enum == many[-MAX_SCOPED_OPS:]


def test_fillet_help_does_not_advertise_profile_fields():
    engine, all_tools = make_engine()
    engine._authoring_topics.add("feature")
    engine._authoring_features = ["fillet"]
    props = feature_branches(patch_schema(engine._authoring_surface(all_tools)))[0][
        "properties"]["payload"]["properties"]
    assert {"base_feature", "sub_elements"} <= set(props)
    assert "profile_sketch" not in props and "sections" not in props
    assert "use_all_edges" in props["params"]["properties"]


def test_scoped_field_table_matches_the_validator_tables():
    """A field cannot be advertised for an op the validator would refuse."""
    from tcad.ir.validate import _PLACEMENT_OPS, _PLANE_OPS, _SUB_ELEMENT_OPS

    for op in ("pad", "fillet", "mirrored", "additive_box", "datum_plane", "polar_pattern"):
        fields = scoped_fields(op)
        assert ("placement" in fields) == (op in _PLACEMENT_OPS)
        assert ("plane" in fields) == (op in _PLANE_OPS)
        assert ({"base_feature", "sub_elements"} <= fields) == (op in _SUB_ELEMENT_OPS)
