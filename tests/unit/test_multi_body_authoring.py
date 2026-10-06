"""Independent parts keep explicit ownership through authoring and editing."""
import json
from types import SimpleNamespace

import pytest

from tcad.core.types import ToolContext, ToolErrorKind
from tcad.ir.patch import PatchError, apply_patch
from tcad.ir.schema import BodySpec, IrDocument, IrPatch, IrPatchOp
from tcad.tools.authoring import build_parts_handler, help_handler
from tcad.tools.ir_tools import ir_list_features_handler, ir_patch_handler


def creation(op):
    if op == "add_sketch":
        return {"id": "profile", "require_fully_constrained": False,
                "geometry": [{"id": "circle", "kind": "circle",
                              "points": [{"x": 0, "y": 0, "z": 0}], "radius": 3}]}
    return {"id": "box", "op": "additive_box",
            "params": {"length": 10, "width": 8, "height": 4}}


def patch(ir, *ops):
    return apply_patch(ir, IrPatch(base_version=ir.version,
        ops=[IrPatchOp(reason="Build independent parts", **op) for op in ops])).ir


@pytest.mark.parametrize("op", ["add_sketch", "add_feature"])
def test_multi_body_creation_requires_explicit_owner_and_rolls_back_batch(op):
    ir = IrDocument(model_id="assembly", bodies=[BodySpec(id="housing", name="housing")])
    before = ir.model_dump()
    with pytest.raises(PatchError, match="body_id is required") as exc:
        patch(ir,
              {"op": "add_body", "payload": {"id": "shaft"}},
              {"op": "add_feature", "payload": {**creation("add_feature"), "body_id": "shaft"}},
              {"op": op, "payload": {**creation(op), "id": "ambiguous"}})
    assert exc.value.error.kind == ToolErrorKind.SEMANTIC
    assert "housing" in exc.value.error.hint and "shaft" in exc.value.error.hint
    assert ir.model_dump() == before


@pytest.mark.parametrize("op", ["add_sketch", "add_feature"])
@pytest.mark.parametrize("body_count", [0, 1])
def test_single_part_shorthand_remains_supported(op, body_count):
    ir = IrDocument(model_id="part", bodies=[BodySpec(id="part", name="part")] * body_count)
    result = patch(ir, {"op": op, "payload": creation(op)})
    assert len(result.bodies) == 1
    assert result.bodies[0].id == ("part" if body_count else "body_1")
    entities = result.bodies[0].sketches if op == "add_sketch" else result.bodies[0].features
    assert len(entities) == 1


@pytest.mark.parametrize("op", ["add_sketch", "add_feature"])
@pytest.mark.parametrize("body_id", [None, "", " ", 0, [], {}])
def test_invalid_explicit_owner_cannot_fall_back_to_single_body(op, body_id):
    ir = IrDocument(model_id="part", bodies=[BodySpec(id="part", name="part")])
    with pytest.raises(PatchError, match="non-empty string") as exc:
        patch(ir, {"op": op, "payload": {**creation(op), "body_id": body_id}})
    assert exc.value.error.kind == ToolErrorKind.SCHEMA
    assert not ir.all_features() and not ir.all_sketches()


@pytest.mark.parametrize("op", ["add_sketch", "add_feature"])
def test_unknown_owner_is_actionable_and_does_not_create_a_body(op):
    ir = IrDocument(model_id="part")
    with pytest.raises(PatchError, match="not found") as exc:
        patch(ir, {"op": op, "payload": {**creation(op), "body_id": "missing"}})
    assert exc.value.error.kind == ToolErrorKind.NOT_FOUND
    assert "add the body first" in exc.value.error.hint
    assert not ir.bodies


async def test_explicit_owner_and_updates_preserve_feature_and_sketch_boundaries():
    ir = IrDocument(model_id="assembly", bodies=[BodySpec(id=id, name=id) for id in ("housing", "shaft")])
    ir = patch(ir,
        {"op": "add_feature", "payload": {**creation("add_feature"), "id": "housing_box", "body_id": "housing"}},
        {"op": "add_feature", "payload": {**creation("add_feature"), "id": "shaft_box", "body_id": "shaft"}},
        {"op": "add_sketch", "payload": {**creation("add_sketch"), "body_id": "shaft"}})
    ir = patch(ir,
        {"op": "update_feature", "target_id": "shaft_box", "payload": {"params": {"length": 20}}},
        {"op": "update_sketch", "target_id": "profile", "payload": {"name": "shaft_profile"}})
    assert ir.bodies[0].features[0].params["length"] == 10
    assert ir.bodies[1].features[0].params["length"] == 20
    assert not ir.bodies[0].sketches
    assert ir.bodies[1].sketches[0].name == "shaft_profile"
    services = SimpleNamespace(store=SimpleNamespace(load=lambda _: ir))
    result = await ir_list_features_handler(services, {}, SimpleNamespace(model_id="assembly"))
    features = json.loads(result.content)
    assert [(f["id"], f["body_id"]) for f in features] == [("housing_box", "housing"), ("shaft_box", "shaft")]


async def test_rejected_tool_write_preserves_saved_ir_and_can_be_corrected(tmp_path):
    from tcad.config.loader import load_default_config
    from tcad.core.wiring import build_services

    cfg = load_default_config()
    cfg.storage.data_dir = str(tmp_path)
    cfg.storage.sqlite_path = ""
    services = build_services(cfg, start_worker=False)
    ctx = ToolContext(model_id="assembly", thread_id="t", turn_id="turn", data_dir=str(tmp_path))
    services.store.create(ctx.model_id, IrDocument(model_id=ctx.model_id))
    try:
        prompt = services.loop_config.system_prompt
        assert "Choose single-body or multi-body modeling" in prompt
        assert "requirements and their complexity" in prompt
        assert "Feature count alone does not require splitting" in prompt
        assert "Honor explicit single-part or assembly intent" in prompt
        assert "Default to a parts-first" not in prompt
        result = await build_parts_handler(services, {"parts": [
            {"id": "housing_box", "body_id": "housing", "shape": "box", "center": [0, 0, 0], "size": [10, 10, 10]},
            {"id": "shaft_box", "body_id": "shaft", "shape": "box", "center": [20, 0, 0], "size": [4, 4, 4]},
        ], "reason": "Create separate components"}, ctx)
        assert result.ok, result.error
        before = services.store.load(ctx.model_id).model_dump()
        args = {"base_version": "current", "ops": [
            {"op": "update_body", "target_id": "housing", "payload": {"name": "changed"}, "reason": "Rename"},
            {"op": "add_sketch", "payload": creation("add_sketch"), "reason": "Add shaft profile"},
        ]}
        result = await ir_patch_handler(services, args, ctx)
        assert not result.ok and "body_id is required" in result.error.message
        assert "shaft" in result.error.hint
        assert services.store.load(ctx.model_id).model_dump() == before
        args["ops"][1]["payload"]["body_id"] = ""
        result = await ir_patch_handler(services, args, ctx)
        assert not result.ok and result.error.kind == ToolErrorKind.SCHEMA
        assert "existing body ID" in result.error.hint
        assert services.store.load(ctx.model_id).model_dump() == before
        args["ops"][1]["payload"]["body_id"] = "shaft"
        result = await ir_patch_handler(services, args, ctx)
        assert result.ok, result.error
        saved = services.store.load(ctx.model_id)
        assert saved.version == before["version"] + 1
        assert not saved.bodies[0].sketches and saved.bodies[1].sketches[0].id == "profile"
    finally:
        services.worker.close()


@pytest.mark.parametrize("topic", ["sketch", "feature", "patch", "assembly"])
async def test_scoped_help_explains_component_ownership(topic):
    result = await help_handler(None, {"topic": topic}, None)
    data = json.loads(result.content)
    assert "requirements and their complexity" in data["body_routing"]
    assert "integral part can use one Body even with many features" in data["body_routing"]
    assert "required with multiple bodies" in data["body_routing"]
    assert "add_body" in data["body_routing"]
    if topic == "assembly":
        assert "ir_commit" in data["rules"] and "assembly_solve" in data["rules"]
        assert "drivers only for requested motion" in data["rules"]


@pytest.mark.parametrize("body_route", [{}, {"body_id": None}, {"body_id": ""}])
def test_legacy_event_recovery_preserves_first_body_routing_without_permitting_new_writes(tmp_path, body_route):
    from tcad.core.types import IrEvent
    from tcad.store.ir_store import IrStore

    store = IrStore(tmp_path)
    ir = store.create("assembly", IrDocument(model_id="assembly",
        bodies=[BodySpec(id=id, name=id) for id in ("housing", "shaft")]))
    legacy_patch = IrPatch(base_version=0, ops=[
        IrPatchOp(op="add_feature", payload={**creation("add_feature"), **body_route}, reason="Old unscoped write"),
    ])
    store._log.append("assembly", IrEvent(model_id="assembly", kind="patch_applied",
        ir_version_before=0, ir_version_after=1,
        payload={"action": "apply", "patch": legacy_patch.model_dump()}))
    recovered = store.rebuild_from_events("assembly")
    assert recovered.version == 1
    assert recovered.bodies[0].features[0].id == "box"
    assert not recovered.bodies[1].features
    assert not store._snapshot_path("assembly", 1).exists()
    assert legacy_patch.ops[0].payload == {**creation("add_feature"), **body_route}
    with pytest.raises(PatchError):
        store.apply_patch("assembly", legacy_patch)
    assert store.load("assembly").model_dump() == ir.model_dump()
