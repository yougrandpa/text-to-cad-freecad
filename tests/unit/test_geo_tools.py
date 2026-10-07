"""Unit tests for geo_measure's digest mapping (design §5 B: geo_measure must
read the real introspect_document structure, not a imagined "measurements" key).

The artifact contains the exact dict shape introspect_document
produces — these tests pin the contract on the read side so the two sides
cannot drift apart silently again.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from tcad.core.types import ToolContext, ToolErrorKind
from tcad.store.artifacts import ArtifactStore
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


def _services(digest, tmp_path):
    store = ArtifactStore(tmp_path)
    if digest is not None:
        root = store.ensure("m1", 1)
        store.write_digest_at(root, digest)
        store.write_manifest(root, model_id="m1", version=1,
                             attempt_id="attempt-1", ir_sha256="f" * 64)
    def forbidden(*args, **kwargs):
        raise AssertionError("measurement must never read IR or call FreeCAD")
    return SimpleNamespace(
        store=SimpleNamespace(current_version=lambda model_id: 1, load=forbidden),
        worker=SimpleNamespace(request=forbidden),
    )


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(thread_id="t1", turn_id="turn1", model_id="m1",
                       data_dir=str(tmp_path))


async def test_geo_measure_honors_what_against_real_digest_shape(tmp_path):
    svc = _services(_digest(), tmp_path)
    res = await geo_measure_handler(
        svc, {"what": ["volume", "solids", "bbox"]}, _ctx(tmp_path))
    assert res.ok, res.error
    payload = json.loads(res.content)
    assert payload["volume"] == 25600.0
    assert payload["solids"] == 1
    assert payload["bbox"] == {"x": 80.0, "y": 50.0, "z": 8.0,
                               "x_min": 0.0, "y_min": 0.0, "z_min": 0.0}


async def test_geo_measure_defaults_cover_the_basic_set(tmp_path):
    svc = _services(_digest(), tmp_path)
    res = await geo_measure_handler(svc, {}, _ctx(tmp_path))
    assert res.ok, res.error
    payload = json.loads(res.content)
    for key in ("volume", "bbox", "faces", "edges", "solids"):
        assert key in payload


async def test_geo_measure_rejects_unknown_measurement(tmp_path):
    svc = _services(_digest(), tmp_path)
    res = await geo_measure_handler(svc, {"what": ["volume", "nope"]}, _ctx(tmp_path))
    assert not res.ok
    assert res.error.kind == ToolErrorKind.SEMANTIC
    assert "nope" in res.error.message


async def test_geo_measure_errors_when_build_produced_no_solid(tmp_path):
    # measurements_available=False (empty shape) must be an honest error, not
    # a report of "{}" — the model needs to know the build is broken.
    svc = _services(_digest(measurements_available=False), tmp_path)
    res = await geo_measure_handler(svc, {"what": ["volume"]}, _ctx(tmp_path))
    assert not res.ok
    assert res.error.kind == ToolErrorKind.RUNTIME
    assert "no solid" in res.error.message


async def test_geo_measure_requires_a_committed_artifact(tmp_path):
    svc = _services(None, tmp_path)
    res = await geo_measure_handler(svc, {"what": ["volume"]}, _ctx(tmp_path))
    assert not res.ok
    assert "call ir_commit first" in res.error.message


async def test_geo_measure_returns_brep_measured_holes(tmp_path):
    """`holes` is the worker's BRep measurement, and it is the only hole
    evidence the model is allowed to reason from — a model that cannot see it
    has nothing to correct against but its own IR."""
    digest = _digest()
    digest["holes"] = [{"index": 0, "diameter": 6.0, "radius": 3.0,
                        "axis": [0.0, 0.0, 1.0], "center": [10.0, 10.0, 0.0],
                        "depth": 8.0, "through": True, "faces": 1}]
    res = await geo_measure_handler(_services(digest, tmp_path), {"what": ["holes"]}, _ctx(tmp_path))
    assert res.ok, res.error
    hole = json.loads(res.content)["holes"][0]
    assert hole["diameter"] == 6.0 and hole["through"] is True


async def test_geo_measure_holes_are_empty_not_missing_when_unmeasured(tmp_path):
    """A digest that measured no holes reports `[]`, so the model sees "the
    kernel found no hole wall" rather than a KeyError or an absent field it can
    read as "not measured yet, my IR is fine"."""
    res = await geo_measure_handler(_services(_digest(), tmp_path), {"what": ["holes"]}, _ctx(tmp_path))
    assert res.ok, res.error
    assert json.loads(res.content)["holes"] == []


def test_geo_measure_schema_declares_what_is_an_array():
    """A probe passed `what="bbox"` and got a type error it could have avoided:
    the description said "volume/bbox/faces/edges/solids/holes" without saying
    the argument is a list."""
    from tcad.tools.geo_tools import build_geo_tools
    spec = build_geo_tools(SimpleNamespace())["geo_measure"]
    assert "ARRAY" in spec.description
    what = spec.params_schema["properties"]["what"]
    assert "array" in what["description"] and "bbox" in what["description"]


def test_assembly_reads_name_the_default_artifact():
    from tcad.tools.geo_tools import build_geo_tools
    tools = build_geo_tools(SimpleNamespace())
    for name in ("assembly_simulate", "assembly_export"):
        assert "latest committed build" in tools[name].description, name
