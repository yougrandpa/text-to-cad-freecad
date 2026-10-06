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
