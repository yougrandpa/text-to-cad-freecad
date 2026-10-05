"""The interactive Core is also the headless snapshot renderer."""

import os
import shutil
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tcad.config.schema import Config
from tcad.core.wiring import RendererAdapter
from tcad.inspect.artifact import ArtifactReader
from tcad.render.snapshot import render_snapshot
from tcad.render.webgl import WebGLRenderer
from tcad.server.app import create_app
from tests.fixtures.artifact_scene import publish_scene

pytestmark = pytest.mark.contract


@pytest.fixture
def browser_executable():
    pytest.importorskip("playwright.sync_api")
    candidates = [os.environ.get("TCAD_TEST_BROWSER"),
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  shutil.which("chromium"), shutil.which("google-chrome")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    from playwright.sync_api import sync_playwright
    with sync_playwright() as runtime:
        candidate = runtime.chromium.executable_path
        if Path(candidate).is_file():
            return candidate
    pytest.skip("Chromium/Chrome unavailable for the shared WebGL contract")


def test_webgl_agent_and_http_capture_share_core_and_backend_cache(tmp_path, browser_executable):
    manifest, root = publish_scene(tmp_path)
    reader = ArtifactReader(tmp_path)
    renderer = WebGLRenderer(executable=browser_executable, supersample=1)
    images = render_snapshot(reader, manifest, root, renderer, views=["iso"], width=240, height=180)
    with Image.open(images[0].path) as image:
        assert image.size == (240, 180)
        assert np.asarray(image).std() > 10  # A real solid, not a blank canvas.
    software = render_snapshot(reader, manifest, root, RendererAdapter(1), views=["iso"], width=240, height=180)
    assert Path(software[0].path).parent != Path(images[0].path).parent
    config = Config()
    config.storage.data_dir = str(tmp_path)
    config.context.render.backend = "webgl"
    config.context.render.browser_executable = browser_executable
    config.context.render.supersample = 1
    app = create_app(config=config)
    with TestClient(app) as client:
        response = client.get("/models/part/render", params={"artifact_id": manifest.artifact_id,
                              "width": 240, "height": 180, "force": True})
        assert response.status_code == 200, response.text
        assert response.content == Path(images[0].path).read_bytes()
        assert response.headers["x-artifact-id"] == manifest.artifact_id
        assert app.state.services is None
        assert client.get("/viewer/core/webgl.js").status_code == 200
        assert client.get("/viewer/hosts/snapshot.js").status_code == 200
    shutil.rmtree(tmp_path / "derived")
    rebuilt = render_snapshot(reader, manifest, root, renderer, views=["iso"], width=240, height=180)
    assert Path(rebuilt[0].path).read_bytes() == response.content
