"""Downloads are prepared only by an explicit request, using pinned geometry."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tcad.artifacts.manifest import ArtifactSet
from tcad.config.loader import load_default_config
from tcad.config.schema import Config
from tcad.loop.engine import LoopConfig
from tcad.server.app import create_app
from tests.unit.test_artifact_boundary import make_artifact
from tests.unit.test_commit_event_loop import _services
from tcad.loop.commit import run_commit


def publish_document(data_dir, *, native=False, passed=True):
    store, root, manifest, _ = make_artifact(data_dir, passed=passed)
    (root / "part.step").unlink()
    (root / "part.FCStd").write_bytes(b"editable document")
    if native:
        (root / "assembly.FCStd").write_bytes(b"editable assembly with joints")
    store.write_manifest(root, model_id="part", version=0, attempt_id="a1",
                         ir_sha256=manifest.ir_sha256,
                         status="verified" if passed else "failed")
    manifest = ArtifactSet.model_validate_json((root / "manifest.json").read_bytes())
    # Failed builds remain available as evidence, but cannot be downloaded.
    if passed:
        store.publish("part", 0, root)
    else:
        retained = store.data_dir / "artifact_sets" / manifest.artifact_id.split(":")[1]
        retained.parent.mkdir(parents=True, exist_ok=True)
        root.rename(retained)
    return manifest


def make_app(data_dir):
    config = Config()
    config.storage.data_dir = str(data_dir)
    return create_app(config=config)


def test_defaults_do_not_request_download_formats():
    assert Config().storage.artifact_exports == []
    assert load_default_config().storage.artifact_exports == []
    assert LoopConfig().artifact_exports == ()


async def test_legacy_commit_does_not_convert_empty_export_list(tmp_path):
    services, _ = _services()
    services.config = Config()
    result, report = await run_commit(services, model_id="m1", ir_version=1,
        message="build", workdir=str(tmp_path), data_dir=str(tmp_path))
    assert result.ok and report.passed
    assert "compile_ir" in services.worker.calls
    assert "export_artifacts" not in services.worker.calls


@pytest.mark.parametrize("fmt", ["step", "stl", "brep"])
def test_click_converts_only_requested_format_then_returns_download(tmp_path, fmt):
    manifest = publish_document(tmp_path)
    calls = []

    def request(method, params, **kwargs):
        calls.append((method, params))
        assert method == "export_saved"
        assert params["fmt"] == fmt
        Path(params["out_dir"], params["name"] + "." + fmt).write_bytes(b"converted geometry")
        return {"ok": True}

    app = make_app(tmp_path)
    app.state.services = SimpleNamespace(config=app.state.config,
        worker=SimpleNamespace(request=request))
    with TestClient(app) as client:
        assert calls == []
        response = client.post("/models/part/exports", json={"artifact_id": manifest.artifact_id, "fmt": fmt})
        assert response.status_code == 200, response.text
        exported = response.json()
        assert exported["artifact_id"] == manifest.artifact_id
        download = client.get(exported["url"])
        assert download.content == b"converted geometry"
        assert download.headers["Content-Disposition"] == f'attachment; filename="part.{fmt}"'
    assert len(calls) == 1
    assert calls[0][1]["path"].endswith("part.FCStd")
    assert [p.suffix for p in (tmp_path / "derived/exports").rglob("part.*")] == ["." + fmt]
    assert not any(p.suffix in {".step", ".stl", ".brep"} for p in (tmp_path / "artifact_sets").rglob("*"))


@pytest.mark.parametrize("native", [False, True])
def test_native_download_needs_no_worker_and_preserves_assembly(tmp_path, native):
    manifest = publish_document(tmp_path, native=native)
    app = make_app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/models/part/exports", json={"artifact_id": manifest.artifact_id, "fmt": "fcstd"})
        assert response.status_code == 200, response.text
        download = client.get(response.json()["url"])
        assert download.content == (b"editable assembly with joints" if native else b"editable document")
        assert download.headers["Content-Disposition"] == 'attachment; filename="part.FCStd"'
        assert (tmp_path / "derived/exports").is_dir()
        assert app.state.services is None


def test_export_rejects_bad_format_wrong_model_missing_build_and_tampering(tmp_path):
    manifest = publish_document(tmp_path)
    app = make_app(tmp_path)
    with TestClient(app) as client:
        body = {"artifact_id": manifest.artifact_id, "fmt": "stl"}
        assert client.post("/models/other/exports", json=body).status_code == 409
        assert client.post("/models/part/exports", json={**body, "fmt": "exe"}).status_code == 422
        assert client.post("/models/part/exports", json={"fmt": "stl"}).status_code == 422
        assert client.post("/models/part/exports", json={**body, "artifact_id": "sha256:" + "f" * 64}).status_code == 404
        root = tmp_path / "artifact_sets" / manifest.artifact_id.split(":")[1]
        (root / "part.FCStd").write_bytes(b"tampered")
        assert client.post("/models/part/exports", json=body).status_code == 409
        assert app.state.services is None


def test_failed_build_is_not_downloadable(tmp_path):
    manifest = publish_document(tmp_path, passed=False)
    with TestClient(make_app(tmp_path)) as client:
        response = client.post("/models/part/exports", json={"artifact_id": manifest.artifact_id, "fmt": "fcstd"})
        assert response.status_code == 409
        assert "verified" in response.json()["detail"]


def test_export_failure_returns_no_download_and_can_be_retried(tmp_path):
    manifest = publish_document(tmp_path)
    attempts = []
    def request(method, params, **kwargs):
        attempts.append(method)
        if len(attempts) == 1:
            return {"ok": False, "error": {"message": "conversion failed"}}
        Path(params["out_dir"], "part.stl").write_bytes(b"STL")
        return {"ok": True}
    app = make_app(tmp_path)
    app.state.services = SimpleNamespace(config=app.state.config,
        worker=SimpleNamespace(request=request))
    body = {"artifact_id": manifest.artifact_id, "fmt": "stl"}
    with TestClient(app) as client:
        response = client.post("/models/part/exports", json=body)
        assert response.status_code == 409
        assert "Content-Disposition" not in response.headers
        assert not list((tmp_path / "derived/exports").rglob("*.stl"))
        exported = client.post("/models/part/exports", json=body).json()
        assert client.get(exported["url"]).content == b"STL"


@pytest.mark.parametrize("empty", [True, False])
def test_worker_cannot_report_success_without_a_nonempty_download(tmp_path, empty):
    manifest = publish_document(tmp_path)
    def request(method, params, **kwargs):
        if empty:
            Path(params["out_dir"], "part.stl").write_bytes(b"")
        return {"ok": True}
    app = make_app(tmp_path)
    app.state.services = SimpleNamespace(config=app.state.config,
        worker=SimpleNamespace(request=request))
    with TestClient(app) as client:
        response = client.post("/models/part/exports", json={"artifact_id": manifest.artifact_id, "fmt": "stl"})
        assert response.status_code == 409
        assert not list((tmp_path / "derived/exports").rglob("*.stl"))
