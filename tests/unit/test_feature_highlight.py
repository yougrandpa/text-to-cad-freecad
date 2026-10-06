import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tcad.server.feature_highlight import feature_highlight
from tcad.server.mesh import MeshPreviewCache


@pytest.fixture
def outline_reader(monkeypatch, tmp_path):
    body = {"id": "cup", "features": [{"id": "handle"}], "sketches": []}
    class Reader:
        def __init__(self, data_dir):
            pass

        def resolve(self, model_id, artifact_id):
            return SimpleNamespace(files={"part.FCStd": {}, "ir.json": {}}), tmp_path

        def read_file(self, *args):
            return json.dumps({"bodies": [body]}).encode()

    monkeypatch.setattr("tcad.server.feature_highlight.ArtifactReader", Reader)
    return body


@pytest.mark.parametrize("vertices", [None, [[0, 0, 0]], [[0, 0], [1, 0]],
    [[float("nan"), 0, 0], [1, 0, 0]], [[True, 0, 0], [1, 0, 0]], [[0, 0, 0]] * 100_002])
def test_worker_outline_is_bounded_and_validated(outline_reader, tmp_path, vertices):
    worker = SimpleNamespace(request=lambda *args, **kwargs: {"ok": True, "result": {"vertices": vertices}})
    with pytest.raises(HTTPException) as error:
        feature_highlight(tmp_path, MeshPreviewCache(), worker, "model", "build", "cup", "feature", "handle")
    assert error.value.status_code == 502


def test_unknown_tree_nodes_never_reach_the_worker(outline_reader, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid selection drove the worker")
    worker = SimpleNamespace(request=forbidden)
    for kind, body, node, status in [("body", "cup", "cup", 400), ("feature", "cup", "missing", 404),
                                    ("feature", "other", "handle", 404)]:
        with pytest.raises(HTTPException) as error:
            feature_highlight(tmp_path, MeshPreviewCache(), worker, "model", "build", body, kind, node)
        assert error.value.status_code == status


def test_empty_outline_and_worker_failures_do_not_become_body_highlights(outline_reader, tmp_path):
    worker = SimpleNamespace(request=lambda *a, **kw: {"ok": False})
    cache = MeshPreviewCache()
    with pytest.raises(HTTPException) as error:
        feature_highlight(tmp_path, cache, worker, "model", "build", "cup", "feature", "handle")
    assert error.value.status_code == 502
    worker.request = lambda *a, **kw: {"ok": True, "result": {"vertices": []}}
    response = feature_highlight(tmp_path, cache, worker, "model", "build", "cup", "feature", "handle")
    assert json.loads(response.body)["vertices"] == []
