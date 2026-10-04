"""HTTP preview contract against the real FreeCAD geometry kernel.

No LLM needed: seed a real parametric solid, fetch its mesh, patch its length,
then fetch both versions. Previewing must leave Gate verification untouched.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tcad.config.loader import load_default_config, resolve_paths
from tcad.core.wiring import build_services
from tcad.ir.schema import IrDocument, IrPatch, IrPatchOp
from tcad.server.app import create_app
from tests.contract.test_e2e_pipeline import make_ir

TestClient = pytest.importorskip("fastapi.testclient").TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
FREECAD_CMD = os.environ.get(
    "TCAD_FREECAD_CMD",
    str(REPO_ROOT / "free-cad" / "FreeCAD" / "build" / "debug" / "bin" / "FreeCADCmd"),
)
pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(not Path(FREECAD_CMD).exists(), reason="FreeCADCmd build not found"),
]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cfg = load_default_config()
    cfg.runtime.freecad_cmd = FREECAD_CMD
    cfg.storage.data_dir = str(tmp_path_factory.mktemp("mesh_http"))
    cfg.storage.sqlite_path = str(Path(cfg.storage.data_dir) / "tcad.sqlite3")
    resolve_paths(cfg, root=REPO_ROOT)
    services = build_services(cfg)
    try:
        with TestClient(create_app(services)) as client:
            client.services = services
            yield client
    finally:
        services._worker_handle.close()


def test_real_geometry_preview_and_versioned_edit(client):
    services = client.services
    services.store.create("mesh-part", make_ir(model_id="mesh-part"))
    before = services.store.verdict("mesh-part", 0)
    response = client.get("/models/mesh-part/mesh?version=0")
    assert response.status_code == 200, response.text
    initial = response.json()
    assert initial["version"] == 0
    mesh = initial["mesh"]
    assert mesh["volume"] == pytest.approx(24000)
    assert mesh["bbox"]["x"] == pytest.approx(60)
    assert mesh["bbox"]["y"] == pytest.approx(40)
    assert mesh["bbox"]["z"] == pytest.approx(10)
    assert mesh["vertex_count"] > 0 and mesh["facet_count"] >= 12
    assert all(0 <= index < mesh["vertex_count"] for face in mesh["facets"] for index in face)
    assert services.store.verdict("mesh-part", 0) == before
    assert before["verified"] is False
    assert not services.store.artifact_dir("mesh-part", 0).exists()

    services.store.apply_patch("mesh-part", IrPatch(
        base_version=0,
        ops=[IrPatchOp(op="update_feature", target_id="pad0", payload={"params": {"length": 20}})],
    ))
    changed = client.get("/models/mesh-part/mesh")
    assert changed.status_code == 200, changed.text
    assert changed.json()["version"] == 1
    assert changed.json()["mesh"]["volume"] == pytest.approx(48000)
    assert changed.json()["mesh"]["bbox"]["z"] == pytest.approx(20)
    assert client.get("/models/mesh-part/mesh?version=0").json() == initial
    assert services.store.verdict("mesh-part", 1)["verified"] is False
    assert list((Path(services.config.storage.data_dir) / ".preview-mesh").iterdir()) == []


def test_empty_and_unbuildable_real_models_never_return_partial_mesh(client):
    services = client.services
    services.store.create("mesh-empty", IrDocument(model_id="mesh-empty"))
    response = client.get("/models/mesh-empty/mesh")
    assert response.status_code == 422, response.text
    assert "mesh" not in response.json()

    ir = make_ir(model_id="mesh-broken")
    # The valid pad still exists, but a broken later feature must not make the
    # last good Tip look like the declared final geometry.
    from tcad.ir.schema import FeatureSpec

    ir.bodies[0].features.append(FeatureSpec(
        id="broken", name="broken", op="pad", profile_sketch="missing-sketch",
        params={"length": 5},
    ))
    services.store.create("mesh-broken", ir)
    response = client.get("/models/mesh-broken/mesh")
    assert response.status_code == 422, response.text
    assert "mesh" not in response.json()
    assert services.store.verdict("mesh-broken", 0)["verified"] is False


def test_review_enabled_chat_builds_cavity_after_first_pass_and_returns_draft(client):
    """Real FreeCAD/Gate/HTTP: the initial green box is not the final turn."""
    from tcad.llm.client import LlmReply, ToolCall
    from tests.unit.test_loop_engine import ScriptedLlm
    services = client.services
    model_id = "review-box"
    services.store.create(model_id, IrDocument(model_id=model_id))
    def patch(id, payload):
        return LlmReply(tool_calls=[ToolCall(id=id, name="ir_patch", args={
            "base_version":"current", "ops":[{"op":"add_feature", "reason":id, "payload":payload}]
        })])
    def commit(id):
        return LlmReply(tool_calls=[ToolCall(id=id,name="ir_commit",args={"message":id})])
    original = services.llm
    services.llm = ScriptedLlm([
        patch("base", {"id":"box", "op":"additive_box", "params":{"length":90,"width":45,"height":35}}),
        commit("base-build"),
        patch("cavity", {"id":"cavity", "op":"subtractive_box", "params":{"length":84,"width":39,"height":32},
                         "placement":{"position":{"x":3,"y":3,"z":3}}}),
        commit("final-build"),
        LlmReply(tool_calls=[ToolCall(id="review",name="design_review",args={
            "summary":"已生成箱体草稿，刀片和实际切削性能待验收", "checklist":[
                {"source_text":"削铅笔工具箱","check_ids":[]}],
            "remaining_work":["刀片和实际削铅笔性能尚未验证"]})]),
    ])
    try:
        response = client.post("/chat",json={"model_id":model_id,"thread_id":"review-box-thread",
                                            "text":"创建一个削铅笔工具箱"})
        assert response.status_code == 200
        import json
        results = [json.loads(frame.split("data: ",1)[1]) for frame in response.text.split("\n\n")
                   if frame.startswith("event: result")]
        assert len(results) == 1, response.text
        result=results[0]
        assert result["state"] == "draft", result
        assert result["steps"] == 5
        assert result["gate_report"]["passed"]
        assert not result["completion_review"]["verified"]
        assert services.store.load(model_id).find_feature("cavity") is not None
        mesh = client.get(f"/models/{model_id}/mesh").json()["mesh"]
        assert mesh["volume"] == pytest.approx(90*45*35 - 84*39*32)
        assert services.store.load(model_id).requirements.raw_text == "创建一个削铅笔工具箱"
    finally:
        services.llm=original
