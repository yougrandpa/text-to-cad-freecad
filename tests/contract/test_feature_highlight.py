"""Selection outlines come from real saved sketches and feature surface deltas."""

import asyncio
import math

import pytest

from fastapi.testclient import TestClient

from tcad.ir.schema import IrDocument
from tcad.loop.commit import run_commit

from tests.contract.test_reference_delivery import published, pytestmark
from tests.reference_support import MODEL, ROOT, THREAD
from tests.http_support import serve


def test_saved_sketch_and_pocket_highlight_only_the_hole(published):
    services, app, reader, manifest, _ = published
    source = services.store.load(MODEL)
    body = source.bodies[0]
    pocket = next(feature for feature in body.features if feature.op == "pocket")
    with TestClient(app) as client:
        def outline(kind, node):
            response = client.get(f"/artifact-sets/{manifest.artifact_id}/highlight", params={
                "model_id": MODEL, "body_id": body.id, "kind": kind, "node_id": node})
            assert response.status_code == 200, response.text
            return response.json()["vertices"]

        sketch = outline("sketch", "hole_circle")
        cut = outline("feature", pocket.id)
        assert sketch and cut
        for vertices in (sketch, cut):
            assert all(math.hypot(x - 20, y - 15) == pytest.approx(3, abs=1e-6)
                       for x, y, z in vertices)
        assert max(p[2] for p in cut) - min(p[2] for p in cut) == pytest.approx(6)
        # Cached selection remains available without a worker or mutable IR.
        services.worker.request = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("cache used worker"))
        services.store.load = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("selection read live IR"))
        assert outline("feature", pocket.id) == cut
        invalid = client.get(f"/artifact-sets/{manifest.artifact_id}/highlight", params={
            "model_id": MODEL, "body_id": body.id, "kind": "feature", "node_id": "missing"})
        assert invalid.status_code == 404


def test_additive_feature_outline_excludes_the_previous_solid(published):
    services, app, reader, _, _ = published
    model = "highlight-boss"
    ir = IrDocument.model_validate({"model_id": model, "bodies": [{"id": "body", "name": "body",
        "features": [
            {"id": "base", "name": "base", "op": "additive_box",
             "params": {"length": 40, "width": 30, "height": 6}},
            {"id": "boss", "name": "boss", "op": "additive_cylinder",
             "params": {"radius": 3, "height": 6}, "placement": {"position": {"x": 20, "y": 15, "z": 6}}},
            {"id": "hole", "name": "hole", "op": "subtractive_cylinder",
             "params": {"radius": 2, "height": 6}, "placement": {"position": {"x": 5, "y": 5, "z": 0}}},
        ]}]})
    services.store.create(model, ir)
    result, gate = asyncio.run(run_commit(services, model, 0, "Highlight surface delta", str(ROOT),
                                          services.config.storage.data_dir))
    assert result.ok and gate.passed, result.error
    manifest, _ = reader.resolve(model)
    with TestClient(app) as client:
        def outline(node):
            response = client.get(f"/artifact-sets/{manifest.artifact_id}/highlight", params={
                "model_id": model, "body_id": "body", "kind": "feature", "node_id": node})
            assert response.status_code == 200, response.text
            return response.json()["vertices"]

        boss = outline("boss")
        assert boss
        assert all(math.hypot(x - 20, y - 15) == pytest.approx(3, abs=1e-6) for x, y, z in boss)
        assert min(p[2] for p in boss) == pytest.approx(6)
        assert max(p[2] for p in boss) == pytest.approx(12)
        base = outline("base")
        assert base and all(-1e-6 <= p[2] <= 6 + 1e-6 for p in base)


def test_browser_sketch_selection_renders_a_local_outline(published):
    playwright = pytest.importorskip("playwright.sync_api")
    from PIL import Image

    _, app, _, _, _ = published
    with serve(app) as base, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(f"{base}/ui/?thread={THREAD}")
            playwright.expect(page.locator("#expandStructure")).to_be_enabled(timeout=30_000)
            page.locator("#expandStructure").click()
            with page.expect_response(lambda response: "/highlight?" in response.url) as response:
                page.locator("#structureTree .structure-select", has_text="通孔圆").click()
            assert response.value.status == 200
            screenshot = ROOT / "output/playwright/reference-sketch-highlight.png"
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            # Screenshot waits for the next paint after the response is consumed.
            page.wait_for_function("document.querySelector('#structureDetail').textContent.includes('仅高亮此草图')")
            page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            page.locator("#viewCanvas").screenshot(path=str(screenshot))
            image = Image.open(screenshot).convert("RGB")
            yellow = [(i % image.width, i // image.width) for i, (r, g, b) in enumerate(image.get_flattened_data())
                      if r > 150 and g > 100 and r > g and g > b * 1.5]
            assert len(yellow) > 20
            assert max(x for x, y in yellow) - min(x for x, y in yellow) < image.width * 0.3
            assert max(y for x, y in yellow) - min(y for x, y in yellow) < image.height * 0.3
            assert not errors, errors
        finally:
            browser.close()
