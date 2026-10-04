"""Prescribed rigid rotation of measured meshes (stdlib-only for the worker).

This provides kinematic preview evidence, never contact or cutting simulation.
"""
import math


def pose_vertices(vertices, motion, angle):
    if not isinstance(angle, (int, float)) or not math.isfinite(angle):
        raise ValueError("driver_angle_deg must be finite")
    result = [list(p) for p in vertices]
    ranges = []
    for part in motion:
        start, count = part["vertex_start"], part["vertex_count"]
        end = start + count
        pivot = [part["pivot"][k] for k in ("x", "y", "z")]
        axis = [part["axis"][k] for k in ("x", "y", "z")]
        norm = math.hypot(*axis)
        theta = angle * math.pi / 180 * part["ratio"]
        if (not isinstance(start, int) or not isinstance(count, int) or start < 0 or count <= 0
                or end > len(vertices) or norm <= 1e-12
                or not all(math.isfinite(v) for v in (*pivot, *axis, theta))
                or any(start < hi and end > lo for lo, hi in ranges)):
            raise ValueError("invalid body motion range or axis")
        ranges.append((start, end))
        x, y, z = [v / norm for v in axis]
        c, s = math.cos(theta), math.sin(theta)
        for i in range(start, end):
            a, b, d = [vertices[i][j] - pivot[j] for j in range(3)]
            dot = x*a + y*b + z*d
            cross = (y*d-z*b, z*a-x*d, x*b-y*a)
            result[i] = [pivot[j] + p*c + cross[j]*s + u*dot*(1-c)
                         for j, (p, u) in enumerate(zip((a,b,d), (x,y,z)))]
    return result
