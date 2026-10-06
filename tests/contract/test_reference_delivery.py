"""Real FreeCAD and browser acceptance of the P1a reference workflow."""

import asyncio
import json
import math
import os
from pathlib import Path
import shutil

import httpx
import pytest
from fastapi.testclient import TestClient

from tcad.config.loader import load_default_config
from tcad.config.settings import LlmSettings, RuntimeSettings
from tcad.core.wiring import apply_llm_settings, build_services
from tcad.inspect.artifact import ArtifactReader
from tcad.llm.client import LlmReply, ToolCall
from tcad.loop.commit import run_commit
from tcad.server.app import create_app
from tcad.store.session_db import SessionDB
from tcad.worker.protocol import M_COMPILE_IR, M_IMPORT_ASSET
from tests.http_support import serve
from tests.reference_support import EDIT, MODEL, ROOT, THREAD, edit_payload, plate
from tests.unit.test_server import _sse

FREECAD = Path(os.environ.get("TCAD_FREECAD_CMD", str(ROOT / "free-cad/FreeCAD/build/debug/bin/FreeCADCmd")))
pytestmark = [pytest.mark.contract, pytest.mark.skipif(not FREECAD.exists(), reason="FreeCADCmd unavailable")]


class EditModel:
    def __init__(self):
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            calls = [("cad_set_hole_diameter", {"diameter_mm":8,"reason":EDIT}),
                ("ir_patch", {"base_version":"current","ops":[{"op":"update_requirement","reason":EDIT,
                    "payload":{"constraints":[{"kind":"hole_diameter","value":8,"tol":0.05,
                        "source_text":"直径改成 8 毫米","confirmed":True}]}}]}),
                ("ir_commit", {"message":"Publish the referenced diameter edit"})]
        elif self.calls == 2:
            calls = [("design_review", {"summary":"引用孔径已由几何测量验证", "remaining_work":[],
                "checklist":[{"source_text":"直径改成 8 毫米","check_ids":["spec_hole_diameter_all_0"]}]})]
        else:
            raise AssertionError("Completed operations must never run the model again")
        return LlmReply(tool_calls=[ToolCall(id=f"call-{self.calls}-{i}",name=name,args=args)
                                    for i,(name,args) in enumerate(calls)])


@pytest.fixture
def published(tmp_path):
    cfg = load_default_config()
    cfg.storage.data_dir = str(tmp_path)
    cfg.storage.sqlite_path = ""
    cfg.runtime.freecad_cmd = str(FREECAD)
    cfg.context.render.backend = "software"
    services = build_services(cfg)
    services.store.create(MODEL, plate())
    try:
        result, gate = asyncio.run(run_commit(services, MODEL, 0, "Initial Ø6 publication", str(ROOT), str(tmp_path)))
        assert result.ok and gate.passed, result.error
        apply_llm_settings(services, RuntimeSettings(llm=LlmSettings(provider="custom",
            base_url="http://127.0.0.1:1/v1",model="deterministic-test",use_env_proxy=False)), persist=False)
        model = EditModel()
        services.llm._client = model
        app = create_app(services)
        app.state.session_db = SessionDB(cfg.storage.sqlite_file(), check_same_thread=False)
        app.state.session_db.create_thread(MODEL, thread_id=THREAD)
        reader = ArtifactReader(tmp_path)
        original, _ = reader.resolve(MODEL)
        yield services, app, reader, original, model
    finally:
        services._worker_handle.close()
        if 'app' in locals():
            app.state.session_db.close()


def assert_eight_mm_publication(services, reader):
    current = services.store.load(MODEL)
    manifest, root = reader.resolve(MODEL)
    digest = reader.digest(manifest, root)
    assert manifest.ir_version == current.version and manifest.status.value == "verified"
    assert current.find_sketch("hole_circle").geometry[0].radius == 4
    assert digest.key_dimensions["hole_circle__solve_status"] == 0
    assert len(digest.holes) == 1 and digest.holes[0].through
    assert digest.holes[0].diameter == pytest.approx(8)
    assert digest.holes[0].depth == pytest.approx(6)
    assert digest.holes[0].center[:2] == pytest.approx([20,15])
    assert (digest.bbox.x,digest.bbox.y,digest.bbox.z) == pytest.approx((40,30,6))
    assert digest.topology.solids == 1
    volume = 40*30*6 - math.pi*4**2*6
    assert digest.volume == pytest.approx(volume, rel=1e-6)
    gate = reader.gate_report(manifest, root)
    assert gate.passed and gate.ir_version == current.version
    hole = next(r for r in gate.results if r.check_id == "spec_hole_diameter_all_0")
    assert hole.status.value == "pass" and "8.0" in json.dumps(hole.measurements)
    step = next(root / name for name in manifest.files if name.endswith(".step"))
    imported = services.worker.request(M_IMPORT_ASSET, {"path":str(step)}, timeout_s=60)
    assert imported["result"]["shape_summary"]["volume"] == pytest.approx(volume, rel=1e-6)
    return current, manifest


def test_http_edit_rebuild_gate_delivery_and_replay(published):
    services, app, reader, original, model = published
    with TestClient(app) as client:
        catalog = client.get(f"/models/{MODEL}/selection-targets").json()
        target = next(t for t in catalog["targets"] if t["ref"].get("sketch_id") == "hole_circle")
        assert "set_hole_diameter" in target["capabilities"]
        payload = edit_payload(target["ref"])
        response = client.post("/chat", json=payload)
        result = _sse(response.text)[-1][1]
        assert result["state"] == "succeeded", result
        current, manifest = assert_eight_mm_publication(services, reader)
        assert manifest.artifact_id != original.artifact_id
        replay = _sse(client.post("/chat", json=payload).text)
        assert replay[0][1]["replayed"] is True and replay[-1][1] == result
        assert services.store.current_version(MODEL) == current.version and model.calls == 2
        assert client.post("/chat", json=payload | {"operation_id":"new-stale-operation"}).status_code == 409


def test_browser_reference_send_clear_and_reference_again(published):
    playwright = pytest.importorskip("playwright.sync_api")
    services, app, reader, original, model = published
    with serve(app) as base, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            page.goto(f"{base}/ui/?thread={THREAD}")
            button = page.locator("li", has_text="通孔圆").locator(".reference-button")
            playwright.expect(button).to_be_enabled(timeout=30_000)
            button.click()
            chip = page.locator("#referenceChips .reference-chip")
            playwright.expect(chip).to_contain_text("v0")
            page.fill("#input", EDIT)
            with page.expect_request(lambda r: r.url.endswith("/chat") and r.method == "POST") as request:
                page.click("#sendBtn")
            sent = request.value.post_data_json
            assert sent["selection_context"]["selection_refs"][0]["artifact_id"] == original.artifact_id
            assert sent["operation_id"] != sent["request_id"]
            playwright.expect(page.locator("#sendBtn")).to_be_enabled(timeout=60_000)
            playwright.expect(page.locator("#referenceChips")).to_be_hidden()
            playwright.expect(button).to_be_enabled()
            button.click()
            current, manifest = assert_eight_mm_publication(services, reader)
            playwright.expect(chip).to_contain_text(f"v{current.version}")
            assert current.version > 0 and model.calls == 2 and not errors, errors
            # Save a reviewable screenshot of the actual UI and final artifact.
            image = ROOT / "review/reference-rewrite/browser.png"
            image.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(image), full_page=True)
            _, root = reader.resolve(MODEL)
            delivery = image.parent / "artifact"
            if delivery.exists():
                shutil.rmtree(delivery)
            shutil.copytree(root, delivery)
        finally:
            browser.close()


def test_malformed_circle_constraint_is_rejected_by_real_kernel(published, tmp_path):
    services, _, _, _, _ = published
    ir = plate().model_dump(mode="json")
    ir["bodies"][0]["sketches"][1]["constraints"] = [{"type":"DistanceX","refs":[0],"value":20}]
    result = services.worker.request(M_COMPILE_IR, {"ir":ir,"out_dir":str(tmp_path / "invalid")}, timeout_s=60)
    assert not result["ok"]
    assert result["error"]["kind"] == "solver"
    assert result["error"]["feature_id"] == "hole_circle"
