"""BRep interference at explicitly sampled crank angles; no dynamics solver."""
import math
from itertools import combinations

from tcad.worker.compiler import _build, _close_doc


def check_motion(ir=None, angles=None, pairs=None, volume_tolerance=1e-6, out_dir='', **_extra):
    import FreeCAD as App
    angles = [0, 90, 180, 270, 360] if angles is None else angles
    if (not angles or len(angles) > 73 or not all(isinstance(a, (int, float)) and math.isfinite(a) and abs(a) <= 720 for a in angles)
            or not math.isfinite(volume_tolerance) or volume_tolerance < 0):
        raise ValueError('provide 1..73 finite angles within ±720 and nonnegative tolerance')
    built = _build(ir, out_dir)
    try:
        if built['errors']:
            return {'ok': False, 'errors': built['errors']}
        shapes = {b['id']: b['shape'] for b in built['body_results']}
        declared = {b['id']: b.get('motion') for b in ir.get('bodies', [])}
        selected = list(combinations(shapes, 2)) if pairs is None else pairs
        if len(selected) > 100 or any(len(p) != 2 or p[0] == p[1] or any(x not in shapes for x in p) for p in selected):
            raise ValueError('pairs must contain two distinct existing body IDs; maximum 100 pairs')
        findings = []
        for angle in angles:
            posed = {}
            for id, source in shapes.items():
                shape = source.copy()
                spec = declared[id]
                if spec:
                    shape.rotate(App.Vector(*(spec['pivot'][k] for k in ('x', 'y', 'z'))),
                                 App.Vector(*(spec['axis'][k] for k in ('x', 'y', 'z'))), angle * spec['ratio'])
                posed[id] = shape
            for a, b in selected:
                common = posed[a].common(posed[b])
                volume = float(common.Volume)
                if not common.isValid() or not math.isfinite(volume):
                    raise ValueError(f"invalid intersection result for {a}/{b} at {angle}")
                if volume > volume_tolerance:
                    findings.append({'angle_deg': angle, 'bodies': [a, b], 'overlap_mm3': volume})
        return {'ok': True, 'angles_deg': angles, 'pairs_checked': len(selected),
                'volume_tolerance_mm3': volume_tolerance, 'interferences': findings,
                'sampled_clear': not findings,
                'scope': 'Sampled BRep overlap only; gaps between angles, contact forces and material removal are unverified.'}
    finally:
        _close_doc(built['doc'])
