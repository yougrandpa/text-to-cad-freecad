"""Deterministic sampled involute spur profiles, not a manufacturing solver.

Dimensions follow standard unshifted spur proportions (KHK technical reference).
Root transitions are radial, not generated trochoids; samples approximate curves.
"""
import math


def involute_outline(teeth, module, pressure_angle=20, backlash=0.1, phase_deg=0, samples=6):
    if (isinstance(teeth, bool) or not isinstance(teeth, int) or not 18 <= teeth <= 120
            or not isinstance(samples, int) or not 3 <= samples <= 12
            or not all(math.isfinite(v) for v in (module, pressure_angle, backlash, phase_deg))
            or module <= 0 or not 15 <= pressure_angle <= 30 or not 0 <= backlash < module):
        raise ValueError('teeth 18..120, module>0, pressure_angle 15..30, backlash in [0,module), samples 3..12 required')
    alpha = math.radians(pressure_angle)
    if teeth < math.ceil(2 / math.sin(alpha)**2):
        raise ValueError('unshifted tooth count risks undercut at this pressure angle; increase teeth or pressure angle')
    pitch = module * teeth / 2
    base, tip, root = pitch * math.cos(alpha), pitch + module, pitch - 1.25 * module
    half = (math.pi * module / 2 - backlash) / (2 * pitch)
    inv = math.tan(alpha) - alpha
    def width(r):
        a = math.acos(min(1, base / r))
        return half + inv - (math.tan(a) - a)
    if width(tip) <= 0:
        raise ValueError('tooth tip has zero or negative thickness')
    low = max(root, base)
    points = []
    def point(r, theta):
        p = (r * math.cos(theta), r * math.sin(theta))
        if not points or math.dist(points[-1], p) > 1e-10:
            points.append(p)
    for tooth in range(teeth):
        center = math.radians(phase_deg) + tooth * 2 * math.pi / teeth
        point(root, center - width(low))
        for i in range(samples + 1):
            r = low + (tip - low) * i / samples
            point(r, center - width(r))
        for i in range(1, samples + 1):
            point(tip, center - width(tip) + 2 * width(tip) * i / samples)
        for i in range(samples, -1, -1):
            r = low + (tip - low) * i / samples
            point(r, center + width(r))
        point(root, center + width(low))
        for i in range(1, samples):
            point(root, center + width(low) + (2*math.pi/teeth - 2*width(low))*i/samples)
    return points


def gear_sketch(args):
    points = involute_outline(args['teeth'], args['module'], args.get('pressure_angle', 20),
                              args.get('backlash', 0.1), args.get('phase_deg', 0), args.get('samples', 6))
    plane = args.get('plane', 'XY')
    dims = {'XY': ('x', 'y'), 'XZ': ('x', 'z'), 'YZ': ('y', 'z')}[plane]
    center = args.get('center', {'x': 0, 'y': 0, 'z': 0})
    def world(p):
        return {**center, dims[0]: center[dims[0]] + p[0], dims[1]: center[dims[1]] + p[1]}
    geometry = [{'id': f'g{i}', 'kind': 'line', 'points': [world(p), world(points[(i+1) % len(points)])]}
                for i,p in enumerate(points)]
    support = args.get('support_feature')
    attachment = ({'kind': 'face', 'feature_id': support, 'sub': args['support_face']} if support else
                  {'kind': 'origin_plane', 'plane': plane})
    if not support and abs(center[({'XY':'z', 'XZ':'y', 'YZ':'x'})[plane]]) > 1e-9:
        raise ValueError('origin-plane center must lie on its plane; use support_feature and support_face for offset gears')
    return {'id': args['id'], 'name': args['id'], 'body_id': args['body_id'], 'plane': attachment,
            'geometry': geometry, 'constraints': [{'type': 'Block', 'refs': [i]} for i in range(len(points))]}
