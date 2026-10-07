from types import SimpleNamespace

import pytest

from tcad.core.types import GeometryDigest, ToolContext
from tcad.ir.schema import IrDocument
from tcad.tools.ir_tools import ir_digest_handler


@pytest.mark.asyncio
async def test_scoped_digest_selects_earlier_feature_without_mutating_stored_digest():
    digest = GeometryDigest(model_id="m", ir_version=2, volume=400,
        faces=[{"name": "Face2", "feature_id": "cut"}],
        feature_geometry={"base": {"faces": [{"name": "Face6", "feature_id": "base"}],
                                    "edges": [{"name": "Edge1", "feature_id": "base"}]}})
    services = SimpleNamespace(
        store=SimpleNamespace(current_version=lambda _: 2,
                              load=lambda *args: IrDocument(model_id="m", version=2)),
        context=SimpleNamespace(digest=lambda *args: digest))
    result = await ir_digest_handler(services, {"feature_id": "base"}, ToolContext(model_id="m", thread_id="t", turn_id="t"))
    assert result.ok
    assert "base/Face6" in result.content and "base/Edge1" in result.content
    assert "cut/Face2" not in result.content
    assert "volume(mm^3): 400" in result.content
    assert digest.faces[0].feature_id == "cut"


@pytest.mark.asyncio
async def test_scoped_digest_refuses_unmeasured_or_legacy_feature():
    digest = GeometryDigest(model_id="m", ir_version=2)
    services = SimpleNamespace(
        store=SimpleNamespace(current_version=lambda _: 2),
        context=SimpleNamespace(digest=lambda *args: digest))
    result = await ir_digest_handler(services, {"feature_id": "unknown"}, ToolContext(model_id="m", thread_id="t", turn_id="t"))
    assert not result.ok
    assert "fresh commit" in result.error.hint


@pytest.mark.asyncio
async def test_historical_digest_uses_frozen_source_after_pending_edit(tmp_path):
    from tests.unit.test_artifact_boundary import make_artifact

    store, staging, manifest, _ = make_artifact(tmp_path, version=1, volume=400)
    store.publish('part', 1, staging)

    def forbidden(*args):
        raise AssertionError('Historical geometry must not depend on current IR/context')

    services = SimpleNamespace(store=SimpleNamespace(current_version=forbidden, load=forbidden),
                               context=SimpleNamespace(digest=forbidden))
    ctx = ToolContext(model_id='part', thread_id='t', turn_id='t', data_dir=str(tmp_path))
    result = await ir_digest_handler(services, {'artifact_id': manifest.artifact_id}, ctx)
    assert result.ok, result.error
    assert 'volume(mm^3): 400' in result.content
    assert manifest.artifact_id in result.content and 'historical evidence' in result.content
    # An artifact for another model must never become evidence for this model.
    result = await ir_digest_handler(services, {'artifact_id': manifest.artifact_id},
        ctx.model_copy(update={'model_id': 'another'}))
    assert not result.ok and 'does not match' in result.error.message


@pytest.mark.asyncio
async def test_historical_digest_rejects_tampered_measurements(tmp_path):
    from tests.unit.test_artifact_boundary import make_artifact
    from tcad.inspect.artifact import ArtifactReader

    store, staging, manifest, _ = make_artifact(tmp_path, version=1)
    store.publish('part', 1, staging)
    (ArtifactReader(tmp_path).object_dir(manifest.artifact_id) / 'digest.json').write_text('{}')
    result = await ir_digest_handler(None, {'artifact_id': manifest.artifact_id},
        ToolContext(model_id='part', thread_id='t', turn_id='t', data_dir=str(tmp_path)))
    assert not result.ok and 'integrity check' in result.error.message
