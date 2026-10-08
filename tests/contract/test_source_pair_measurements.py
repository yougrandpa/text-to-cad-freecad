"""Pinned source geometry reveals floating attachments and embedded rotors."""
import asyncio
import json
from pathlib import Path

import pytest

from tcad.core.types import ToolContext
from tcad.inspect.artifact import ArtifactReader
from tcad.ir.schema import IrDocument
from tcad.tools.authoring import build_parts_handler
from tcad.tools.geo_tools import geo_measure_handler
from tests.contract.test_build_runtime import services, commit
from tests.contract.test_primitive_placement import FREECAD_CMD

pytestmark=[pytest.mark.contract,pytest.mark.skipif(not Path(FREECAD_CMD).exists(),reason='FreeCAD unavailable')]


def test_source_pair_measurements_show_separation_and_overlap_on_pinned_build(services):
    services.store.create('fits',IrDocument(model_id='fits'))
    ctx=ToolContext(model_id='fits',thread_id='t',turn_id='turn',data_dir=services.config.storage.data_dir)
    result=asyncio.run(build_parts_handler(services,{'parts':[
        {'id':'housing','body_id':'housing','shape':'box','center':[5,5,5],'size':[10,10,10]},
        {'id':'rotor','body_id':'rotor','shape':'box','center':[2,2,2],'size':[2,2,2]},
        {'id':'support','body_id':'support','shape':'box','center':[22,5,5],'size':[4,4,4]}]},ctx))
    assert result.ok,result.error
    result,report=commit(services,'fits',services.store.current_version('fits'))
    assert report and report.passed,(result.error,result.content)
    reader=ArtifactReader(ctx.data_dir);manifest,_=reader.resolve('fits')
    # A pending source edit must not affect measurement of the frozen build.
    result=asyncio.run(build_parts_handler(services,{'parts':[
        {'id':'support','body_id':'support','shape':'box','center':[40,5,5],'size':[4,4,4]}]},ctx))
    assert result.ok,result.error
    calls=[]
    original=services.worker.request
    def record(method,*args,**kwargs):
        calls.append(method)
        return original(method,*args,**kwargs)
    services.worker.request=record
    measured=asyncio.run(geo_measure_handler(services,{'artifact_id':manifest.artifact_id,
        'what':['bbox'],'pairs':[['housing','support'],['housing','rotor']]},ctx))
    assert measured.ok,measured.error
    data=json.loads(measured.content)
    assert data['artifact_id'] == manifest.artifact_id
    gap,embedded=data['pair_measurements']
    assert gap['min_distance_mm'] == pytest.approx(10,abs=1e-6)
    assert gap['overlap_mm3'] == 0
    assert embedded['min_distance_mm'] == 0
    assert embedded['overlap_mm3'] == pytest.approx(8,rel=1e-6)
    assert embedded['min_surface_distance_mm'] >= 0
    assert gap['min_surface_distance_mm'] == pytest.approx(10,abs=1e-6)
    assert calls == ['check_saved_motion']  # no compilation or re-solving
    invalid=asyncio.run(geo_measure_handler(services,{'artifact_id':manifest.artifact_id,
        'pairs':[['housing','missing']]},ctx))
    assert not invalid.ok
