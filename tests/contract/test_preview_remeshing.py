"""Real OCC cached triangulation must not defeat the preview budget ladder."""
import json
import subprocess

import pytest

from tests.contract.test_param_capability import FREECAD_CMD, REPO_ROOT, pytestmark


def test_dense_export_triangulation_is_replaced_on_a_copy(tmp_path):
    script = tmp_path / 'preview_probe.py'
    script.write_text('''import json, Part
from tcad.worker.preview import adaptive_pick_mesh
shape = Part.makeSphere(30)
volume = shape.Volume
fine_vertices, fine_facets = shape.tessellate(0.01)
result = adaptive_pick_mesh([('curved', shape)], 0.5)
indexed = result['pick_mesh']
after_vertices, after_facets = shape.tessellate(0.01)
print('##PREVIEW##' + json.dumps({
    'status': result['status'], 'fine_facets': len(fine_facets),
    'preview_facets': len(indexed.facets) if indexed else None,
    'after_facets': len(after_facets), 'volume_before': volume,
    'volume_after': shape.Volume, 'valid': shape.isValid(),
    'faces': len(shape.Faces),
    'mapped_faces': len([e for e in indexed.entities if e['entity_kind'] == 'face']) if indexed else None,
}), flush=True)
''', encoding='utf-8')
    process = subprocess.run([FREECAD_CMD, '--console', '-P', str(REPO_ROOT), str(script)],
                             capture_output=True, text=True, timeout=60)
    assert process.returncode == 0, process.stderr[-2000:]
    result = json.loads(next(line[len('##PREVIEW##'):] for line in process.stdout.splitlines()
                             if line.startswith('##PREVIEW##')))
    assert result['status'] in {'ok', 'degraded'}
    assert result['preview_facets'] < result['fine_facets']
    assert result['after_facets'] == result['fine_facets']
    assert result['valid'] and result['volume_after'] == pytest.approx(result['volume_before'])
    assert result['mapped_faces'] == result['faces']
