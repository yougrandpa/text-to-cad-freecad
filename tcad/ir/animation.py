"""Apply native row-major rigid transforms without accumulating frame drift."""
import math


def pose_frame(vertices, parts, poses):
    result = [list(p) for p in vertices]
    for part in parts:
        matrix = poses[part['body_id']]
        if len(matrix) != 16 or not all(math.isfinite(v) for v in matrix):
            raise ValueError('invalid native transform')
        start, end = part['vertex_start'], part['vertex_start']+part['vertex_count']
        if start < 0 or end > len(vertices):
            raise ValueError('invalid native body range')
        for i in range(start,end):
            x,y,z=vertices[i]
            result[i]=[matrix[j]*x+matrix[j+1]*y+matrix[j+2]*z+matrix[j+3] for j in (0,4,8)]
    return result
