"""Unit tests for geo_measure's digest mapping (design §5 B: geo_measure must
read the real introspect_document structure, not a imagined "measurements" key).

The worker is stubbed to return the exact dict shape introspect_document
produces — these tests pin the contract on the read side so the two sides
cannot drift apart silently again.
"""

from __future__ import annotations

import json
import tempfile
from types import SimpleNamespace

from tcad.core.types import ToolContext, ToolErrorKind
from tcad.ir.schema import IrDocument
from tcad.tools.geo_tools import geo_measure_handler


def _digest(measurements_available=True) -> dict:
    """The dict shape worker/introspect.py::introspect_document returns."""
    return {
        "model_id": "m1",
        "ir_version": 1,
        "feature_chain": [],
        "topology": {"solids": 1, "faces": 10, "edges": 24,
                     "vertexes": 16, "shells": 1},
        "bbox": {"x": 80.0, "y": 50.0, "z": 8.0,
                 "x_min": 0.0, "y_min": 0.0, "z_min": 0.0},
        "volume": 25600.0,
        "area": 5000.0,
        "shape_type": "solid",
        "is_valid": True,
        "key_dimensions": {},
        "spec_deviation": {},
        "measurements_available": measurements_available,
        "text": "model: m1 (v1)",
    }


def _services(digest: dict | None, *, worker_ok: bool = True):
    worker_result = (
        {"ok": True, "result": digest}
        if worker_ok
        else {"ok": False, "error": {"kind": "compile", "message": "recompute blew up"}}
    )
    store = SimpleNamespace(
        current_version=lambda model_id: 1,
        load=lambda model_id, version=None: IrDocument(model_id="m1", version=1),
    )
    worker = SimpleNamespace(request=lambda method, params, timeout_s=60.0: worker_result)
    return SimpleNamespace(store=store, worker=worker)


def _ctx() -> ToolContext:
    return ToolContext(
        thread_id="t1", turn_id="turn1", model_id="m1",
        data_dir=tempfile.mkdtemp(),
    )


async def test_geo_measure_honors_what_against_real_digest_shape():
    svc = _services(_digest())
    res = await geo_measure_handler(
        svc, {"what": ["volume", "solids", "bbox"]}, _ctx())
    assert res.ok, res.error
    payload = json.loads(res.content)
    assert payload["volume"] == 25600.0
    assert payload["solids"] == 1
    assert payload["bbox"] == {"x": 80.0, "y": 50.0, "z": 8.0,
                               "x_min": 0.0, "y_min": 0.0, "z_min": 0.0}


async def test_geo_measure_defaults_cover_the_basic_set():
    svc = _services(_digest())
    res = await geo_measure_handler(svc, {}, _ctx())
    assert res.ok, res.error
    payload = json.loads(res.content)
    for key in ("volume", "bbox", "faces", "edges", "solids"):
        assert key in payload


async def test_geo_measure_rejects_unknown_measurement():
    svc = _services(_digest())
    res = await geo_measure_handler(svc, {"what": ["volume", "nope"]}, _ctx())
    assert not res.ok
    assert res.error.kind == ToolErrorKind.SEMANTIC
    assert "nope" in res.error.message


async def test_geo_measure_errors_when_build_produced_no_solid():
    # measurements_available=False (empty shape) must be an honest error, not
    # a report of "{}" — the model needs to know the build is broken.
    svc = _services(_digest(measurements_available=False))
    res = await geo_measure_handler(svc, {"what": ["volume"]}, _ctx())
    assert not res.ok
    assert res.error.kind == ToolErrorKind.RUNTIME
    assert "no solid" in res.error.message


async def test_geo_measure_propagates_worker_failure():
    svc = _services(None, worker_ok=False)
    res = await geo_measure_handler(svc, {"what": ["volume"]}, _ctx())
    assert not res.ok
    assert "recompute blew up" in res.error.message


async def test_geo_measure_returns_brep_measured_holes():
    """`holes` is the worker's BRep measurement, and it is the only hole
    evidence the model is allowed to reason from — a model that cannot see it
    has nothing to correct against but its own IR."""
    digest = _digest()
    digest["holes"] = [{"index": 0, "diameter": 6.0, "radius": 3.0,
                        "axis": [0.0, 0.0, 1.0], "center": [10.0, 10.0, 0.0],
                        "depth": 8.0, "through": True, "faces": 1}]
    res = await geo_measure_handler(_services(digest), {"what": ["holes"]}, _ctx())
    assert res.ok, res.error
    hole = json.loads(res.content)["holes"][0]
    assert hole["diameter"] == 6.0 and hole["through"] is True


async def test_geo_measure_holes_are_empty_not_missing_when_unmeasured():
    """A digest that measured no holes reports `[]`, so the model sees "the
    kernel found no hole wall" rather than a KeyError or an absent field it can
    read as "not measured yet, my IR is fine"."""
    res = await geo_measure_handler(_services(_digest()), {"what": ["holes"]}, _ctx())
    assert res.ok, res.error
    assert json.loads(res.content)["holes"] == []
