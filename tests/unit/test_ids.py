"""Ids and names that become paths must not escape their directory (task §5-E).

``model_id`` comes from an HTTP body or URL path; ``asset_export``'s ``name``
comes from the model itself. Both were used verbatim to build a filesystem path,
so ``"../../../../tmp/pwned"`` wrote outside the data directory and an absolute
id read wherever it pointed. ``asset_import`` took an arbitrary path from the
model and read it.

These tests are deterministic and need no FreeCAD.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from tcad.core.ids import (
    InvalidIdentifier,
    contained_path,
    ensure_contained,
    ensure_safe_id,
    is_safe_id,
)
from tcad.core.types import ToolContext, ToolErrorKind, ToolResult
from tcad.ir.schema import IrDocument
from tcad.store.artifacts import ArtifactStore, gate_report_path
from tcad.store.ir_store import IrStore

REPO_ROOT = Path(__file__).resolve().parents[2]


# ══════════════════════════════════════════════════════════════════════════
# 1. the shape rule
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("good", [
    "m1", "bracket", "part-b6bbedbd", "accept_b_v2", "sample_a_80", "rev_12_5",
    "th-1", "cli-m1", "a", "0", "x" * 64,
])
def test_real_identifiers_are_accepted(good):
    assert is_safe_id(good)
    assert ensure_safe_id(good) == good


@pytest.mark.parametrize("bad", [
    "", " ", "..", ".", "../x", "../../../../tmp/pwned", "a/b", "a\\b",
    "/etc/passwd", "/tmp/x", "a b", "模型", "a\x00b", "-leading", "_leading",
    ".hidden", "x" * 65, "a\nb", "a..b/../c", "~/x", "%2e%2e%2fx",
])
def test_traversal_shaped_identifiers_are_refused(bad):
    assert not is_safe_id(bad)
    with pytest.raises(InvalidIdentifier):
        ensure_safe_id(bad)


def test_contained_path_refuses_escape_and_symlinks(tmp_path):
    root = tmp_path / "root"
    (root / "inner").mkdir(parents=True)
    assert contained_path(root, "inner", "x.json").name == "x.json"

    with pytest.raises(InvalidIdentifier):
        contained_path(root, "..", "outside")

    # A symlink pointing out of the root must be caught on the RESOLVED path.
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover — platform
        pytest.skip("symlinks unavailable")
    with pytest.raises(InvalidIdentifier):
        contained_path(root, "link", "escaped.json")
    with pytest.raises(InvalidIdentifier):
        ensure_contained(link / "escaped.json", root)


# ══════════════════════════════════════════════════════════════════════════
# 2. the store refuses to build a path from a bad id
# ══════════════════════════════════════════════════════════════════════════


def test_ir_store_refuses_a_traversing_model_id(tmp_path):
    """The escape target is inside tmp_path, so the assertion is exact.

    ``data/models/../../pwned`` resolves to ``tmp_path/pwned``. Checking that
    precise path (rather than a guess about where the traversal lands) is what
    makes "nothing escaped" a real assertion instead of a hopeful one.
    """
    store = IrStore(tmp_path / "data")
    evil = "../../pwned"
    with pytest.raises(InvalidIdentifier):
        store.create(evil, IrDocument(model_id=evil, version=0))
    assert not (tmp_path / "pwned").exists(), "the store wrote outside data_dir"


def test_ir_store_refuses_an_absolute_model_id(tmp_path):
    store = IrStore(tmp_path / "data")
    with pytest.raises(InvalidIdentifier):
        store.load("/etc/passwd")


def test_artifact_store_refuses_a_traversing_model_id(tmp_path):
    store = ArtifactStore(tmp_path / "data")
    with pytest.raises(InvalidIdentifier):
        store.dir_for("../../evil", 0)
    with pytest.raises(InvalidIdentifier):
        gate_report_path(tmp_path / "data", "../evil", 0)


def test_artifact_store_still_accepts_real_ids(tmp_path):
    store = ArtifactStore(tmp_path / "data")
    assert store.dir_for("plate", 2) == (tmp_path / "data" / "artifacts" / "plate" / "v2")
    p = gate_report_path(tmp_path / "data", "plate", 2)
    assert p.name == "v2.json" and p.is_file() is False


# ══════════════════════════════════════════════════════════════════════════
# 3. the API boundary turns it into a 4xx, not a 500
# ══════════════════════════════════════════════════════════════════════════


def _client(tmp_path):
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from tests.unit import test_server as ts
    from tcad.server.app import create_app

    services = ts.make_services(tmp_path)
    return TestClient(create_app(services))


@pytest.mark.parametrize("bad_id", ["../x", "../../../../etc/passwd", "/abs", "a/b", ".."])
def test_creating_a_model_with_a_traversing_id_is_a_422(tmp_path, bad_id):
    with _client(tmp_path) as client:
        r = client.post("/models", json={"model_id": bad_id, "raw_requirement": "x"})
        assert r.status_code == 422, r.text
        assert "invalid model_id" in r.text or "model_id" in r.text


def test_chat_refuses_a_traversing_model_id(tmp_path):
    with _client(tmp_path) as client:
        r = client.post("/chat", json={"model_id": "../escape", "text": "hi"})
        assert r.status_code == 422, r.text


def test_chat_refuses_a_bad_thread_id(tmp_path):
    with _client(tmp_path) as client:
        r = client.post("/chat", json={
            "model_id": "ok-model", "thread_id": "../escape", "text": "hi"})
        assert r.status_code == 422, r.text


def test_a_normal_model_id_still_works(tmp_path):
    with _client(tmp_path) as client:
        r = client.post("/models", json={"model_id": "plate-01", "raw_requirement": "x"})
        assert r.status_code == 200, r.text
        assert r.json()["model_id"] == "plate-01"


# ══════════════════════════════════════════════════════════════════════════
# 4. the tools: export name and import path
# ══════════════════════════════════════════════════════════════════════════


def _ctx(tmp_path, **kw):
    base = dict(thread_id="th", turn_id="tn", model_id="m",
                workdir=str(tmp_path / "work"), data_dir=str(tmp_path / "data"))
    base.update(kw)
    (tmp_path / "work").mkdir(exist_ok=True)
    (tmp_path / "data").mkdir(exist_ok=True)
    return ToolContext(**base)


def _services(**kw):
    return SimpleNamespace(**kw)


async def test_asset_export_refuses_a_traversing_name(tmp_path):
    from tcad.tools.geo_tools import asset_export_handler

    res = await asset_export_handler(_services(), {"fmt": "step", "name": "../../evil"}, _ctx(tmp_path))
    assert res.ok is False
    assert res.error.kind == ToolErrorKind.SCHEMA
    assert "export name" in res.error.message


async def test_asset_export_accepts_a_normal_name(tmp_path):
    from tcad.tools.geo_tools import asset_export_handler
    from tests.fixtures.artifact_scene import publish_scene
    ctx = _ctx(tmp_path)
    publish_scene(ctx.data_dir, model_id=ctx.model_id)
    services = _services(store=SimpleNamespace(current_version=lambda m: 0))
    res = await asset_export_handler(services, {"fmt": "step", "name": "plate_v2"}, ctx)
    assert res.ok, res.error
    import json
    assert Path(json.loads(res.content)["path"]).name == "plate_v2.step"


async def test_asset_import_refuses_a_path_outside_the_roots(tmp_path):
    from tcad.tools.geo_tools import asset_import_handler

    calls = []
    services = _services(worker=SimpleNamespace(
        request=lambda *a, **k: calls.append(a) or {"ok": True, "result": {}}))

    res = await asset_import_handler(services, {"path": "/etc/passwd", "fmt": "step"}, _ctx(tmp_path))
    assert res.ok is False
    assert res.error.kind == ToolErrorKind.DENIED
    assert "outside the permitted roots" in res.error.message
    assert calls == [], "a refused path must never reach the worker"

    res2 = await asset_import_handler(
        services, {"path": "../../../../etc/passwd", "fmt": "step"}, _ctx(tmp_path))
    assert res2.ok is False and res2.error.kind == ToolErrorKind.DENIED


async def test_asset_import_allows_a_path_inside_the_data_dir(tmp_path):
    from tcad.tools.geo_tools import asset_import_handler

    ctx = _ctx(tmp_path)                      # creates the roots first
    target = tmp_path / "data" / "incoming.step"
    target.write_text("ISO-10303-21;", encoding="utf-8")
    calls = []

    def request(method, params=None, **kw):
        calls.append(params)
        return {"ok": True, "result": {"shape_summary": {"volume": 1.0}}}

    services = _services(worker=SimpleNamespace(request=request))
    res = await asset_import_handler(services, {"path": str(target), "fmt": "step"}, ctx)
    assert res.ok is True, res.error
    assert calls and Path(calls[0]["path"]) == target.resolve()


# ══════════════════════════════════════════════════════════════════════════
# 5. the worker's mirrored rule must not drift from the supervisor's
# ══════════════════════════════════════════════════════════════════════════


def _worker_pattern() -> str:
    """Read ``_SAFE_COMPONENT_RE`` out of the worker source.

    ``tcad.worker.exporters`` imports FreeCAD, so it cannot be imported in a unit
    test; the pattern is a literal, so reading it from the AST is exact (the same
    technique ``test_op_capability`` uses for the compiler's type map).
    """
    source = (REPO_ROOT / "tcad" / "worker" / "exporters.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_SAFE_COMPONENT_RE":
                    return node.value.args[0].value
    raise AssertionError("exporters.py no longer defines _SAFE_COMPONENT_RE")


def test_the_worker_and_the_supervisor_agree_on_safe_names():
    worker_re = re.compile(_worker_pattern())
    corpus = [
        "m1", "bracket", "part-b6bbedbd", "x" * 63, "x" * 64, "x" * 65,
        "", "..", ".", "../x", "a/b", "a\\b", "/etc/passwd", "a b", "模型",
        "-lead", "_lead", ".hidden", "a\x00b",
    ]
    for name in corpus:
        assert bool(worker_re.match(name)) == is_safe_id(name), name
