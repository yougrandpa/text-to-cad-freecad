"""Headless snapshots rendered by the shipped interactive Viewer Core."""

import base64
import io
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image

from tcad.build.pool import check_cancelled
from tcad.core.ids import contained_path
from tcad.core.types import ImageRef


class WebGLRenderer:
    backend = "webgl"

    def __init__(self, *, executable=None, supersample=2):
        self.executable = executable
        self.supersample = min(max(int(supersample), 1), 2)

    def render(self, mesh, *, out_dir, views, style, width, height):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ValueError('WebGL snapshots require pip install -e ".[webgl]" and playwright install chromium') from exc
        from tcad.core.wiring import estimate_image_tokens
        viewer = Path(__file__).resolve().parents[1] / "viewer"
        output = Path(out_dir)
        output.mkdir(parents=True, exist_ok=True)
        images = []
        with sync_playwright() as runtime:
            options = {"headless": True}
            if self.executable:
                options["executable_path"] = self.executable
            with runtime.chromium.launch(**options) as browser:
                page = browser.new_page(viewport={"width": width, "height": height},
                                        device_scale_factor=self.supersample)

                def serve(route):
                    path = urlparse(route.request.url).path
                    if path == "/":
                        return route.fulfill(content_type="text/html", body=
                            f'<html><body style="margin:0"><canvas id="scene" style="width:{width}px;height:{height}px"></canvas></body></html>')
                    try:
                        source = contained_path(viewer, path.lstrip("/"))
                        if source.suffix not in {".js", ".json"}:
                            raise ValueError("unsupported render resource")
                        route.fulfill(content_type="application/json" if source.suffix == ".json" else "text/javascript",
                                      body=source.read_bytes())
                    except (OSError, ValueError):
                        route.fulfill(status=404, body="unknown Viewer Core resource")

                # Resources and geometry stay inside this process. Captures do
                # not connect to the Web host, source store or FreeCAD worker.
                page.route("**/*", serve)
                page.goto("http://tcad-render.local/")
                for view in views:
                    check_cancelled()
                    encoded = page.evaluate("""async ({ mesh, view, style }) => {
                      const { renderSceneSnapshot } = await import('/hosts/snapshot.js');
                      return renderSceneSnapshot(document.getElementById('scene'), { mesh }, { view, style });
                    }""", {"mesh": mesh.model_dump(mode="json"), "view": view, "style": style})
                    raw = base64.b64decode(encoded.split(",", 1)[1], validate=True)
                    target = output / f"{view}.png"
                    with Image.open(io.BytesIO(raw)) as image:
                        image.convert("RGB").resize((width, height), Image.Resampling.LANCZOS).save(target)
                    check_cancelled()
                    images.append(ImageRef(path=str(target), view=view, width=width, height=height,
                                           tokens_estimate=estimate_image_tokens(width, height)))
        return images
