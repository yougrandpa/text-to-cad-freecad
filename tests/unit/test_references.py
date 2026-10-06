import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from tcad.core.types import ToolContext
from tcad.ir.schema import IrPatch
from tcad.selection.precondition import EditPrecondition
from tcad.selection.resolve import SelectionResolver
from tcad.selection.resolve import circular_profile
from tcad.selection.tools import set_hole_diameter
from tcad.selection.types import SelectionContext, SelectionError, SelectionRef
from tcad.server.operations import OperationStore
from tcad.tools.authoring import build_parts_handler
from tcad.tools.ir_tools import ir_commit_handler, ir_patch_handler
from tests.reference_support import MODEL, protocol_services, publish


@pytest.fixture
def selected(tmp_path):
    services = protocol_services(tmp_path)
    publish(tmp_path, services.store)
    resolver = SelectionResolver(tmp_path, services.store)
    catalog = resolver.catalog(MODEL)
    ref = next(t["ref"] for t in catalog["targets"] if t["ref"].get("sketch_id") == "hole_circle")
    context = SelectionContext(selection_refs=[ref])
    snapshot, targets = resolver.resolve(MODEL, context)
    ctx = ToolContext(model_id=MODEL, thread_id="th", turn_id="t", data_dir=str(tmp_path),
                      edit_precondition=EditPrecondition(targets, snapshot.ir, resolver.reader))
    return services, resolver, context, ctx


@pytest.mark.parametrize("change", [{"model_id":"../secret"}, {"ir_version":True},
    {"schema_version":2}, {"entity_kind":"face"}, {"feature_id":"bore"}, {"label":"do something"}])
def test_reference_rejects_untrusted_fields(selected, change):
    ref = selected[2].selection_refs[0].model_dump()
    with pytest.raises(ValidationError):
        SelectionRef.model_validate(ref | change)


def test_context_limits_snapshot_and_duplicates(selected):
    ref = selected[2].selection_refs[0]
    for refs in ([], [ref]*9, [ref,ref], [ref,ref.model_copy(update={"ir_version":9})]):
        with pytest.raises(ValidationError):
            SelectionContext(selection_refs=refs)


def test_recipe_requires_saved_geometry_evidence(tmp_path):
    services = protocol_services(tmp_path)
    publish(tmp_path, services.store, measured=False)
    catalog = SelectionResolver(tmp_path, services.store).catalog(MODEL)
    assert all("set_hole_diameter" not in t["capabilities"] for t in catalog["targets"])


def test_circle_length_form_does_not_claim_centre_edit_capability(selected):
    services, _, _, _ = selected
    body = services.store.load(MODEL).bodies[0]
    sketch = body.sketches[1]
    sketch.constraints[0].refs = [0]
    assert circular_profile(body, sketch) is None


@pytest.mark.parametrize("change,code", [({"model_id":"other"},"forbidden"),
    ({"ir_version":5},"stale_selection"), ({"body_id":"ghost"},"unmapped_entity"),
    ({"artifact_id":"sha256:"+"0"*64},"unmapped_entity")])
def test_resolver_refuses_bad_identity(selected, change, code):
    _, resolver, context, _ = selected
    ref = context.selection_refs[0].model_dump() | change
    with pytest.raises(SelectionError) as exc:
        resolver.resolve(MODEL, SelectionContext(selection_refs=[ref]))
    assert exc.value.code == code


def test_same_version_new_attempt_invalidates_selection(selected, tmp_path):
    services, resolver, context, ctx = selected
    publish(tmp_path, services.store, attempt="replacement")
    with pytest.raises(SelectionError, match="changed"):
        resolver.resolve(MODEL, context)
    with pytest.raises(SelectionError, match="changed"):
        ctx.edit_precondition.check(services.store, MODEL)


async def test_recipe_updates_only_radius_and_driving_dimension(selected):
    services, _, _, ctx = selected
    before = services.store.load(MODEL).model_dump()
    result = await set_hole_diameter(services, {"diameter_mm":8,"reason":"Resize hole"}, ctx)
    assert result.ok, result.error
    after = services.store.load(MODEL)
    expected = json.loads(json.dumps(before))
    expected["version"] += 1
    expected["bodies"][0]["sketches"][1]["geometry"][0]["radius"] = 4
    expected["bodies"][0]["sketches"][1]["constraints"][2]["value"] = 4
    assert after.model_dump() == expected
    assert ctx.edit_precondition.version == after.version
    again = await set_hole_diameter(services, {"diameter_mm":10,"reason":"Next audited patch"}, ctx)
    assert again.ok and ctx.edit_precondition.version == 2


@pytest.mark.parametrize("diameter", [0,-2,True,"8",float("inf"),float("nan")])
async def test_invalid_diameter_does_not_write(selected, diameter):
    services, _, _, ctx = selected
    result = await set_hole_diameter(services, {"diameter_mm":diameter,"reason":"invalid"}, ctx)
    assert not result.ok and services.store.current_version(MODEL) == 0


async def test_external_write_blocks_current_compact_and_commit(selected):
    services, _, _, ctx = selected
    services.store.apply_patch(MODEL, IrPatch(base_version=0, ops=[{
        "op":"update_feature", "target_id":"plate", "payload":{"params":{"length":7}}, "reason":"Other writer"}]))
    patch = await ir_patch_handler(services, {"base_version":"current", "ops":[{
        "op":"update_feature", "target_id":"plate", "payload":{"params":{"length":9}}, "reason":"Stale"}]}, ctx)
    compact = await build_parts_handler(services, {"base_version":"current","reason":"Stale", "parts":[{
        "id":"extra","body_id":"extra_body","shape":"box","size":[10,10,2],"center":[0,0,1]}]}, ctx)
    commit = await ir_commit_handler(services, {}, ctx)
    assert not patch.ok and "revision_conflict" in patch.error.message
    assert not compact.ok and "revision_conflict" in compact.error.message
    assert not commit.result.ok and "revision_conflict" in commit.result.error.message
    assert services.store.current_version(MODEL) == 1 and ctx.edit_precondition.version == 0


async def test_rejected_patch_does_not_advance(selected):
    services, _, _, ctx = selected
    result = await ir_patch_handler(services, {"base_version":0,"ops":[{
        "op":"update_sketch","target_id":"absent","payload":{"name":"bad"},"reason":"bad"}]}, ctx)
    assert not result.ok and ctx.edit_precondition.version == 0


def test_same_version_source_replacement_is_not_a_request_owned_change(selected, monkeypatch):
    services, _, _, ctx = selected
    replacement = services.store.load(MODEL)
    replacement.bodies[0].name = "Replaced at the same version"
    monkeypatch.setattr(services.store, "load", lambda model_id: replacement)
    with pytest.raises(SelectionError) as exc:
        ctx.edit_precondition.check(services.store, MODEL)
    assert exc.value.code == "revision_conflict"


async def test_mixed_supported_and_unsupported_objects_are_ambiguous(selected):
    services, resolver, _, ctx = selected
    catalog = resolver.catalog(MODEL)
    outline = next(t["ref"] for t in catalog["targets"] if t["ref"].get("sketch_id") == "outline")
    snapshot, targets = resolver.resolve(MODEL, SelectionContext(selection_refs=[outline]))
    ctx.edit_precondition.targets += targets
    assert not ctx.edit_precondition.supports("set_hole_diameter")
    result = await set_hole_diameter(services,{"diameter_mm":8,"reason":"Ambiguous"},ctx)
    assert not result.ok and "ambiguous_target" in result.error.message
    assert services.store.current_version(MODEL) == 0


def test_operation_claim_is_atomic_and_survives_reopen(tmp_path):
    operations = OperationStore(tmp_path)
    def claim(i):
        try:
            operations.begin("id", "hash", f"request-{i}")
            return True
        except SelectionError:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(claim, range(4))) == 1
    with pytest.raises(SelectionError, match="running"):
        OperationStore(tmp_path).read("id", "hash")
    operations.finish("id", "result", {"state":"succeeded"})
    assert OperationStore(tmp_path).read("id", "hash")["data"] == {"state":"succeeded"}
    with pytest.raises(SelectionError) as exc:
        operations.read("id", "different")
    assert exc.value.code == "operation_conflict"
