"""The geo tools must not run the FreeCAD worker on the event loop.

``geo_view`` / ``geo_measure`` / ``asset_export`` / ``asset_import`` are
``async def`` handlers that called ``services.worker.request`` — a synchronous
protocol — directly. One tessellation or STEP import therefore parked the whole
asyncio loop: every other session's SSE stalled, health checks stopped and a
cancelled turn found no cancellation point until FreeCAD answered. The commit
pipeline was fixed for exactly this; the geo tools had the same shape and were
missed, which is why this file exists next to ``test_commit_event_loop.py``.

Same method as there: watch a heartbeat tick while a deliberately slow fake
worker is busy. A blocked loop can only manage about one tick.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from tcad.core.types import GeometryDigest, ToolContext, ToolErrorKind
from tcad.tools.geo_tools import (
    asset_export_handler,
    asset_import_handler,
    geo_measure_handler,
    geo_view_handler,
)
from tcad.worker.protocol import M_EXPORT, M_IMPORT_ASSET, M_INTROSPECT, M_TESSELLATE

SLOW_S = 0.4
TICK_S = 0.01


class _SlowWorker:
    """RPC that blocks its calling thread, like the real handle does."""

    def __init__(self):
        self.calls: list[str] = []

    def request(self, method, params=None, *, timeout_s=30.0):
        self.calls.append(method)
        time.sleep(SLOW_S)
        if method == M_INTROSPECT:
            return {"ok": True, "result": GeometryDigest(
                model_id="m1", ir_version=1, text="d").model_dump()}
        if method == M_TESSELLATE:
            return {"ok": True, "result": {"mesh": {
                "vertices": [(0, 0, 0), (1, 0, 0), (0, 1, 0)], "facets": [(0, 1, 2)]}}}
        if method == M_EXPORT:
            return {"ok": True, "result": {"files": {}}}
        if method == M_IMPORT_ASSET:
            return {"ok": True, "result": {"shape_summary": {"faces": 6}}}
        return {"ok": True, "result": {}}


class _Store:
    def current_version(self, model_id):
        return 1

    def load(self, model_id, version=None):
        from tcad.ir.schema import IrDocument

        return IrDocument(model_id=model_id, version=1)


class _Renderer:
    def render(self, mesh, out_dir, views, style, width, height):
        return []


def _services():
    return SimpleNamespace(store=_Store(), worker=_SlowWorker(), renderer=_Renderer())


def _ctx(tmp_path, *, visual_ok=True):
    return ToolContext(thread_id="t", turn_id="t", model_id="m1",
                       data_dir=str(tmp_path), workdir=str(tmp_path),
                       visual_ok=visual_ok)


async def _heartbeat(counter):
    while True:
        await asyncio.sleep(TICK_S)
        counter["ticks"] += 1


async def _ticks_during(coro):
    counter = {"ticks": 0}
    hb = asyncio.create_task(_heartbeat(counter))
    try:
        await coro
    finally:
        hb.cancel()
    return counter["ticks"]


@pytest.mark.parametrize("handler, args", [
    (asset_export_handler, {"fmt": "step", "name": "out"}),
    (asset_import_handler, {}),
    (geo_view_handler, {"views": ["iso"]}),
])
async def test_a_geo_tool_leaves_the_event_loop_free(tmp_path, handler, args):
    services = _services()
    if handler is asset_import_handler:
        target = tmp_path / "in.step"
        target.write_text("DATA")
        args = {"path": str(target), "fmt": "step"}

    ticks = await _ticks_during(handler(services, args, _ctx(tmp_path)))

    assert services.worker.calls, f"{handler.__name__} never called the worker"
    assert ticks >= 15, (
        f"{handler.__name__} managed only {ticks} heartbeat ticks during a "
        f"{SLOW_S}s worker call — the RPC ran on the event loop"
    )


async def test_a_geo_tool_still_reports_worker_failure(tmp_path):
    """Moving off the loop must not change the error contract."""
    services = _services()
    services.worker.request = lambda method, params=None, *, timeout_s=30.0: {
        "ok": False, "error": {"kind": "runtime", "message": "no solid to tessellate"},
    }
    out = await geo_view_handler(services, {"views": ["iso"]}, _ctx(tmp_path))
    assert out.ok is False
    assert out.error.kind == ToolErrorKind.RUNTIME
    assert "no solid" in out.error.message


async def test_measurement_disk_read_leaves_event_loop_free(tmp_path, monkeypatch):
    from tcad.inspect.artifact import ArtifactReader
    from tests.unit.test_geo_tools import _services as artifact_services
    from tests.unit.test_geo_tools import _digest
    services = artifact_services(_digest(), tmp_path)
    original = ArtifactReader.digest

    def slow_read(self, *args):
        time.sleep(SLOW_S)
        return original(self, *args)

    monkeypatch.setattr(ArtifactReader, "digest", slow_read)
    ticks = await _ticks_during(geo_measure_handler(services, {}, _ctx(tmp_path)))
    assert ticks >= 15
