"""Artifacts bind queries to immutable build evidence, never current source."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tcad.artifacts.manifest import ArtifactSet, AttemptArtifact, PublishedArtifact
from tcad.core.types import BuildStamp, GateReport, GeometryDigest, ToolContext
from tcad.core.wiring import StoreAdapter, build_context_loader
from tcad.inspect.artifact import ArtifactReadError, ArtifactReader
from tcad.ir.schema import IrDocument
from tcad.store.artifacts import ArtifactStore, write_build_stamp
from tcad.tools.geo_tools import geo_measure_handler
from tcad.verify.context import CheckContextError, build_check_context
from tcad.verify.gate import Gate


def make_artifact(data_dir, *, version=0, attempt="a1", volume=10, passed=True):
    store = ArtifactStore(data_dir)
    root = store.staging_dir("part", version, attempt)
    root.mkdir(parents=True)
    ir = IrDocument(model_id="part", version=version)
    raw = ir.model_dump_json()
    ir_hash = hashlib.sha256(raw.encode()).hexdigest()
    (root / "ir.json").write_text(raw)
    (root / "part.step").write_text(f"STEP {volume}")
    store.write_digest_at(root, GeometryDigest(
        model_id="part", ir_version=version, volume=volume,
        measurements_available=True))
    write_build_stamp(root, BuildStamp(model_id="part", ir_version=version,
        attempt_id=attempt, started_at=0, ir_sha256=ir_hash))
    store.write_manifest(root, model_id="part", version=version,
        attempt_id=attempt, ir_sha256=ir_hash, status="verifying")
    draft = AttemptArtifact.model_validate_json((root / "manifest.json").read_text())
    report = GateReport(model_id="part", ir_version=version, attempt_id=attempt,
                        ir_sha256=ir_hash, passed=passed)
    (root / "gate_report.json").write_text(report.model_dump_json())
    store.write_manifest(root, model_id="part", version=version,
        attempt_id=attempt, ir_sha256=ir_hash, status="verified" if passed else "failed")
    manifest = ArtifactSet.model_validate_json((root / "manifest.json").read_text())
    return store, root, manifest, draft


def test_published_set_survives_replacement_of_same_version(tmp_path):
    store, staging, first, draft = make_artifact(tmp_path)
    assert draft.status.value == "verifying"
    PublishedArtifact.model_validate(first.model_dump())
    store.publish("part", 0, staging)
    reader = ArtifactReader(tmp_path)
    _, root = reader.resolve("part", artifact_id=first.artifact_id)
    assert reader.digest(first, root).volume == 10

    _, staging, second, _ = make_artifact(tmp_path, attempt="a2", volume=20)
    store.publish("part", 0, staging)
    assert first.artifact_id != second.artifact_id
    assert reader.digest(first, root).volume == 10
    latest, alias = reader.resolve("part", 0)
    assert latest.artifact_id == second.artifact_id
    assert reader.digest(latest, alias).volume == 20


def test_publish_refuses_failed_report_even_if_status_claims_verified(tmp_path):
    store, root, manifest, _ = make_artifact(tmp_path, passed=False)
    with pytest.raises(ValidationError):
        PublishedArtifact.model_validate(manifest.model_dump())
    store.write_manifest(root, model_id="part", version=0, attempt_id="a1",
                         ir_sha256=manifest.ir_sha256, status="verified")
    with pytest.raises(ArtifactReadError, match="does not attest"):
        store.publish("part", 0, root)
    assert not store.dir_for("part", 0).exists()


def test_file_tampering_is_not_measurement_evidence(tmp_path):
    store, staging, manifest, _ = make_artifact(tmp_path)
    root = store.publish("part", 0, staging)
    (root / "digest.json").write_text('{"volume":999}')
    with pytest.raises(ArtifactReadError, match="integrity"):
        ArtifactReader(tmp_path).digest(manifest, root)


def test_publish_does_not_accept_tampered_export(tmp_path):
    store, root, _, _ = make_artifact(tmp_path)
    (root / "part.step").write_text("other geometry")
    with pytest.raises(ArtifactReadError, match="integrity"):
        store.publish("part", 0, root)
    assert not store.dir_for("part", 0).exists()


def test_manifest_identity_is_checked(tmp_path):
    _, _, manifest, _ = make_artifact(tmp_path)
    raw = manifest.model_dump(mode="json")
    raw["attempt_id"] = "another-build"
    with pytest.raises(ValidationError, match="identity"):
        ArtifactSet.model_validate(raw)


@pytest.mark.parametrize("artifact_id", ["../../etc", "sha256:ABC", "sha256:" + "f" * 63])
def test_artifact_ids_cannot_escape_store(tmp_path, artifact_id):
    with pytest.raises(ArtifactReadError, match="invalid artifact_id"):
        ArtifactReader(tmp_path).resolve("part", artifact_id=artifact_id)


def test_cross_model_id_cannot_read_another_artifact(tmp_path):
    store, staging, manifest, _ = make_artifact(tmp_path)
    store.publish("part", 0, staging)
    with pytest.raises(ArtifactReadError, match="model/version"):
        ArtifactReader(tmp_path).resolve("other", artifact_id=manifest.artifact_id)


def test_gate_reads_bundled_snapshot_when_source_changes(tmp_path):
    store, root, _, _ = make_artifact(tmp_path)
    source = tmp_path / "source.json"
    changed = IrDocument(model_id="part", version=9, notes=["new source"])
    source.write_text(changed.model_dump_json())
    ctx = build_check_context(model_id="part", ir_version=0,
                             artifact_dir=str(root), ir_path=str(source))
    assert ctx.ir.version == 0
    assert ctx.ir.notes == []


def test_artifact_gate_entry_point_does_not_load_source_store(tmp_path):
    _, root, _, _ = make_artifact(tmp_path)
    store = StoreAdapter(tmp_path)
    # No source model exists in this store. The bundled artifact is sufficient
    # to reach all checks (the synthetic STEP cannot pass real kernel checks).
    gate = Gate(build_context_loader(store, None))
    report = gate.evaluate_artifact(root)
    assert report.model_id == "part" and report.ir_version == 0
    assert report.attempt_id == "a1"


def test_artifact_gate_refuses_loader_that_cannot_select_an_attempt(tmp_path):
    _, root, _, _ = make_artifact(tmp_path)
    gate = Gate(lambda model_id, version: None)
    with pytest.raises(TypeError, match="artifact-aware"):
        gate.evaluate_artifact(root)


def test_missing_bundled_input_cannot_fall_back_to_source(tmp_path):
    _, root, _, _ = make_artifact(tmp_path)
    (root / "ir.json").unlink()
    # There is no authoring model to fall back to. The invalid attempt still
    # yields a structured failing verdict rather than querying the store.
    report = Gate(build_context_loader(StoreAdapter(tmp_path), None)).evaluate_artifact(root)
    assert not report.passed
    assert "gate:cannot_attest_no_measurements" in report.blocking_failures


def test_gate_ignores_exports_not_indexed_by_artifact(tmp_path):
    _, root, _, _ = make_artifact(tmp_path)
    (root / "unindexed.stl").write_text("not part of this artifact")
    ctx = build_check_context(model_id="part", ir_version=0,
                             artifact_dir=str(root), ir_path="unused.json")
    assert set(ctx.exports) == {"step"}


def test_gate_rejects_tampered_artifact_and_cannot_borrow_source_digest(tmp_path):
    _, root, _, _ = make_artifact(tmp_path)
    (root / "digest.json").write_text("{}")
    with pytest.raises(CheckContextError, match="integrity"):
        build_check_context(model_id="part", ir_version=0,
                            artifact_dir=str(root), ir_path="missing.json")
    source_store = StoreAdapter(tmp_path)
    source_store.create("part", IrDocument(model_id="part"))
    source_store.artifacts.write_digest_at(source_store.artifact_dir("part", 0),
        GeometryDigest(model_id="part", ir_version=0, volume=999, measurements_available=True))
    report = Gate(build_context_loader(source_store, None)).evaluate(
        "part", 0, artifact_dir=str(root))
    assert not report.passed
    assert "gate:cannot_attest_no_measurements" in report.blocking_failures


async def test_measurement_can_pin_old_build_without_loading_current_ir(tmp_path):
    store, staging, manifest, _ = make_artifact(tmp_path)
    store.publish("part", 0, staging)
    def forbidden(*args, **kwargs):
        raise AssertionError("pinned query must never read current IR or call worker")
    services = SimpleNamespace(store=SimpleNamespace(current_version=forbidden, load=forbidden),
                               worker=SimpleNamespace(request=forbidden))
    ctx = ToolContext(thread_id="t", turn_id="turn", model_id="part", data_dir=str(tmp_path))
    result = await geo_measure_handler(services, {"artifact_id": manifest.artifact_id,
                                                "what": ["volume"]}, ctx)
    assert result.ok, result.error
    assert json.loads(result.content) == {"volume": 10}


async def test_unbuilt_version_does_not_borrow_previous_measurements(tmp_path):
    store, staging, _, _ = make_artifact(tmp_path)
    store.publish("part", 0, staging)
    services = SimpleNamespace(store=SimpleNamespace(current_version=lambda _: 1))
    ctx = ToolContext(thread_id="t", turn_id="turn", model_id="part", data_dir=str(tmp_path))
    result = await geo_measure_handler(services, {}, ctx)
    assert not result.ok
    assert "call ir_commit first" in result.error.message


def test_artifact_http_queries_work_without_starting_worker(tmp_path):
    from tcad.server.app import create_app
    from tcad.config.schema import Config
    from fastapi.testclient import TestClient

    store, staging, manifest, _ = make_artifact(tmp_path)
    store.publish("part", 0, staging)
    config = Config()
    config.storage.data_dir = str(tmp_path)
    app = create_app(config=config)
    with TestClient(app) as client:
        url = f"/artifact-sets/{manifest.artifact_id}"
        response = client.get(url, params={"model_id": "part"})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "verified"
        assert response.json()["gate_report"]["passed"]
        response = client.get(url + "/measurements", params={"model_id": "part"})
        assert response.status_code == 200, response.text
        assert response.json()["digest"]["volume"] == 10
        assert client.get(url, params={"model_id": "other"}).status_code == 409
        assert client.get("/artifact-sets/sha256:" + "f" * 64,
                          params={"model_id": "part"}).status_code == 404
        assert app.state.services is None
