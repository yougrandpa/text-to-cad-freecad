import pytest
from fastapi.testclient import TestClient

from tcad.core.types import ToolTier
from tcad.ir.schema import BodySpec, PartRef
from tcad.selection.types import SelectionContext
from tcad.server.app import create_app
from tcad.tools.base import build_default_registry, execute_tool
from tests.reference_support import MODEL, THREAD, edit_payload, plate, protocol_services, publish
from tests.unit.test_server import _sse


@pytest.fixture
def stack(tmp_path):
    services = protocol_services(tmp_path)
    publish(tmp_path, services.store)
    with TestClient(create_app(services)) as client:
        targets = client.get(f"/models/{MODEL}/selection-targets").json()["targets"]
        ref = next(t["ref"] for t in targets if t["ref"].get("sketch_id") == "hole_circle")
        yield services, client, ref


def test_finished_operation_replays_even_when_source_is_now_stale(stack):
    services, client, ref = stack
    payload = edit_payload(ref)
    first = client.post("/chat", json=payload)
    assert first.status_code == 200
    second = client.post("/chat", json=payload | {"request_id":"another-cancellation-id"})
    assert second.status_code == 200
    frames = _sse(second.text)
    assert frames[0][1]["replayed"] is True and services.llm.calls == 1
    assert frames[-1] == _sse(first.text)[-1]
    assert client.post("/chat", json=payload | {"text":"different"}).status_code == 409


def test_plain_turn_does_not_offer_selection_recipe(stack):
    services, client, _ = stack
    assert client.post("/chat", json={"model_id":MODEL,"text":"inspect","kind":"inspect"}).status_code == 200
    assert "cad_set_hole_diameter" not in services.llm.tools[0]


def test_resolved_data_and_capability_reach_the_model(stack):
    services, client, ref = stack
    response = client.post("/chat", json=edit_payload(ref))
    assert response.status_code == 200
    assert "cad_set_hole_diameter" in services.llm.tools[0]
    notes = [m["content"] for m in services.llm.messages[0] if m["role"] == "system"]
    assert any(ref["artifact_id"] in text and "hole_circle" in text for text in notes)


def test_full_access_selection_still_omits_arbitrary_python(stack):
    services, client, ref = stack
    assert client.post("/chat",json=edit_payload(ref) | {"access_mode":"full"}).status_code == 200
    assert "raw_python" not in services.llm.tools[0]


@pytest.mark.parametrize("change,status", [({"body_id":"missing"},422),
    ({"model_id":"other"},403), ({"ir_version":3},409)])
def test_bad_selection_is_rejected_before_model_call(stack, change, status):
    services, client, ref = stack
    response = client.post("/chat", json=edit_payload(ref | change))
    assert response.status_code == status and services.llm.calls == 0


def test_reference_requires_operation_id(stack):
    services, client, ref = stack
    payload = edit_payload(ref); payload.pop("operation_id")
    assert client.post("/chat", json=payload).status_code == 422 and services.llm.calls == 0


def test_disable_switch_preserves_ordinary_inspection(stack):
    services, client, ref = stack
    services.config.selection.enabled = False
    assert client.get(f"/models/{MODEL}/selection-targets").status_code == 403
    assert client.post("/chat", json=edit_payload(ref)).status_code == 403
    assert client.get("/health").json()["selection_enabled"] is False
    assert build_default_registry(services).get("cad_set_hole_diameter") is None
    assert client.post("/chat", json={"model_id":MODEL,"text":"read","kind":"inspect"}).status_code == 200


def test_imported_reference_obeys_read_only_mode(tmp_path):
    ir = plate()
    ir.bodies.append(BodySpec(id="import",name="导入件",part_ref=PartRef(
        model_id="supplier",artifact_id="sha256:"+"c"*64,body_id="component")))
    services = protocol_services(tmp_path, ir=ir)
    publish(tmp_path, services.store)
    with TestClient(create_app(services)) as client:
        catalog = client.get(f"/models/{MODEL}/selection-targets").json()
        ref = next(t["ref"] for t in catalog["targets"] if t["ref"]["body_id"] == "import")
        payload = edit_payload(ref)
        assert client.post("/chat",json=payload).status_code == 403
        inspected = client.post("/chat",json=payload | {"access_mode":"read_only"})
        assert inspected.status_code == 200
        assert _sse(inspected.text)[-1][1]["state"] == "inspected"
        assert services.store.current_version(MODEL) == 0
