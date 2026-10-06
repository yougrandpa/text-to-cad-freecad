"""Frozen BRep topology and actual browser picking, independent per case."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tcad.ir.schema import IrDocument
from tcad.loop.commit import run_commit
from tcad.render.scene import SceneModel
from tcad.selection.resolve import SelectionResolver
from tests.contract.test_reference_delivery import published, pytestmark
from tests.http_support import serve
from tests.reference_support import MODEL, ROOT, THREAD, RecordingModel, edit_payload


def test_saved_face_edge_identity_survives_different_tessellation(published):
    services, _, reader, manifest, _ = published
    _, root = reader.resolve(MODEL)
    source = services.store.load(MODEL).model_dump(mode="json")
    fcstd = next(root / name for name in manifest.files if name.endswith(".FCStd"))
    scenes = []
    for tolerance in (0.1, 1.0):
        response = services.worker.request("read_artifact_scene", {"fcstd_path":str(fcstd),
            "bodies":source["bodies"],"tolerance":tolerance}, timeout_s=60)
        assert response["ok"], response
        scenes.append(SceneModel.from_build(response["result"], source))
    assert scenes[0].pick_mapping.mesh_digest != scenes[1].pick_mapping.mesh_digest
    identities = lambda s: [(e.body_id,e.entity_kind,e.local_sub_id) for e in s.pick_mapping.entities]
    assert identities(scenes[0]) == identities(scenes[1])
    assert sum(len(e.segments) for e in scenes[0].pick_mapping.entities) != sum(
        len(e.segments) for e in scenes[1].pick_mapping.entities)
    assert reader.scene(manifest, root).pick_mapping is not None


def test_static_repeated_geometry_has_separate_complete_body_ranges(published):
    services, app, reader, _, _ = published
    data = {"model_id":"copies", "bodies":[{"id":id,"name":id,"features":[{
        "id":id+"_box","name":id+"_box","op":"additive_box","params":{"length":4,"width":3,"height":2},
        "placement":{"position":{"x":x,"y":0,"z":0}}}]} for id,x in (("left",0),("right",10))]}
    services.store.create("copies",IrDocument.model_validate(data))
    result, gate = asyncio.run(run_commit(services,"copies",0,"Repeated bodies",str(ROOT),services.config.storage.data_dir))
    assert result.ok and gate.passed, result.error
    manifest, root = reader.resolve("copies")
    scene = reader.scene(manifest, root)
    assert len(scene.pick_mapping.parts) == 2
    assert {(e.body_id,e.local_sub_id) for e in scene.pick_mapping.entities if e.local_sub_id=="Face1"} == {
        ("left","Face1"),("right","Face1")}
    # Both the read API and resolver must use saved bytes when the worker is unavailable.
    request = services.worker.request
    services.worker.request = lambda *a,**kw: (_ for _ in ()).throw(AssertionError("selection touched worker"))
    try:
        with TestClient(app) as client:
            payload = client.get("/models/copies/mesh").json()
            assert payload["pick_mapping"]["mesh_digest"] == scene.pick_mapping.mesh_digest
            catalog = client.get("/models/copies/selection-targets").json()
            faces = [t for t in catalog["targets"] if t["ref"].get("local_sub_id")=="Face1"]
            assert len(faces)==2 and all(not t["editable"] for t in faces)
    finally:
        services.worker.request = request


def test_face_reference_cannot_modify_and_inspection_is_replayed(published):
    services, app, _, _, original_model = published
    with TestClient(app) as client:
        catalog = client.get(f"/models/{MODEL}/selection-targets").json()
        ref = next(t["ref"] for t in catalog["targets"] if t["ref"]["entity_kind"]=="face")
        payload = edit_payload(ref)
        assert client.post("/chat",json=payload).status_code == 403
        assert original_model.calls == 0
        model = RecordingModel()
        services.llm._client = model
        payload.update(kind="inspect",access_mode="read_only",text="解释选中的面",operation_id="inspect-face")
        response = client.post("/chat",json=payload)
        assert response.status_code==200 and model.calls==1, response.text
        assert not any(tool in model.tools[0] for tool in ("ir_patch","ir_commit","raw_python","cad_set_hole_diameter"))
        assert services.store.current_version(MODEL)==0
        replay = client.post("/chat",json=payload).text
        assert '"replayed": true' in replay
        assert model.calls==1


def test_browser_pick_highlight_remove_and_inspect(published):
    playwright = pytest.importorskip("playwright.sync_api")
    services, app, reader, original, _ = published
    services.llm._client = RecordingModel()
    with serve(app) as base, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        page = browser.new_page(viewport={"width":1440,"height":900})
        errors=[]
        page.on("pageerror",lambda error:errors.append(str(error)))
        try:
            page.goto(f"{base}/ui/?thread={THREAD}")
            playwright.expect(page.locator("#pickKind")).to_be_enabled(timeout=30_000)
            playwright.expect(page.locator("li",has_text="通孔圆").locator(".reference-button")).to_be_enabled()
            page.locator('[data-view="top"]').click()
            canvas = page.locator("#viewCanvas")
            canvas.focus(); page.keyboard.press("f")
            page.select_option("#pickKind","face")
            box=canvas.bounding_box()
            point=page.evaluate('''async ({width,height}) => {
              const {OrbitCamera,projectPoint} = await import('/viewer/core/camera.js');
              const {meshBounds} = await import('/viewer/core/scene.js');
              const response = await fetch('/models/reference_plate/mesh');
              const {mesh} = await response.json();
              const camera = new OrbitCamera(); camera.setPreset('top'); camera.setBounds(meshBounds(mesh.vertices),width/height);
              return projectPoint([10,10,6],camera,width,height);
            }''', {"width":box["width"],"height":box["height"]})
            page.mouse.click(box["x"]+point[0],box["y"]+point[1])
            chip=page.locator("#referenceChips .reference-chip")
            playwright.expect(chip).to_contain_text("Face")
            screenshot=ROOT / "review/geometry-picking/browser.png"
            screenshot.parent.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(screenshot),full_page=True)
            # The screenshot is evidence that the native WebGL highlighter renders.
            from PIL import Image
            image=Image.open(screenshot).convert("RGB")
            assert sum(r>220 and 130<g<210 and b<90 for r,g,b in image.get_flattened_data()) > 20
            chip.click()
            playwright.expect(page.locator("#referenceChips")).to_be_hidden()
            page.select_option("#pickKind","edge")
            edgepoint=page.evaluate('''async ({width,height}) => {
              const {OrbitCamera,projectPoint} = await import('/viewer/core/camera.js');
              const {meshBounds} = await import('/viewer/core/scene.js');
              const {mesh} = await (await fetch('/models/reference_plate/mesh')).json();
              const camera = new OrbitCamera(); camera.setPreset('top'); camera.setBounds(meshBounds(mesh.vertices),width/height);
              return projectPoint([10,0,6],camera,width,height);
            }''', {"width":box["width"],"height":box["height"]})
            page.mouse.click(box["x"]+edgepoint[0],box["y"]+edgepoint[1])
            playwright.expect(chip).to_contain_text("Edge")
            chip.click()
            page.select_option("#pickKind","face")
            page.mouse.click(box["x"]+point[0],box["y"]+point[1])
            page.fill("#input","解释这个面的作用")
            with page.expect_request(lambda r:r.url.endswith("/chat") and r.method=="POST") as request:
                page.click("#sendBtn")
            sent=request.value.post_data_json
            assert sent["kind"]=="inspect" and sent["access_mode"]=="read_only"
            assert sent["selection_context"]["selection_refs"][0]["local_sub_id"].startswith("Face")
            assert sent["selection_context"]["selection_refs"][0]["artifact_id"]==original.artifact_id
            playwright.expect(page.locator("#sendBtn")).to_be_enabled(timeout=30_000)
            assert services.store.current_version(MODEL)==0 and not errors,errors
        finally:
            browser.close()
