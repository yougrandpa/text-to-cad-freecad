"""Export solved animation frames using a fixed camera extent."""
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image

from tcad.core.types import Mesh
from tcad.ir.animation import pose_frame
from tcad.render.raster import render_views


def assembly_transition(result, *, mode, grounded_body_ids, duration_s=3, frames=41,
                        explode_distance_mm=None):
    """Stage rigid parts from a saved solved pose for an assembly illustration.

    This is a presentation path, independent of native joints and simulation.
    It does not claim that parts can physically follow the illustrated paths.
    """
    import math

    if mode not in {'assemble', 'explode'}:
        raise ValueError('assembly transition mode must be assemble or explode')
    if not math.isfinite(duration_s) or not 0.1 <= duration_s <= 60:
        raise ValueError('assembly transition duration_s must be between 0.1 and 60')
    if isinstance(frames, bool) or not isinstance(frames, int) or not 2 <= frames <= 600:
        raise ValueError('assembly transition requires 2..600 frames')
    if explode_distance_mm is not None and (not math.isfinite(explode_distance_mm) or explode_distance_mm <= 0):
        raise ValueError('explode_distance_mm must be positive and finite')
    parts, pose = result['parts'], result['frames'][0]
    fixed = set(grounded_body_ids)
    ids = {part['body_id'] for part in parts}
    if not fixed or fixed - ids:
        raise ValueError('assembly transition requires existing grounded body IDs')
    if not ids - fixed:
        raise ValueError('assembly transition requires at least one ungrounded part')
    vertices = np.asarray(pose_frame(result['mesh']['vertices'], parts, pose))
    centers = {}
    for part in parts:
        start, count = part['vertex_start'], part['vertex_count']
        if count <= 0:
            raise ValueError('assembly transition requires nonempty part meshes')
        points = vertices[start:start+count]
        centers[part['body_id']] = (points.min(axis=0)+points.max(axis=0))/2
    anchor = np.mean([centers[id] for id in sorted(fixed)], axis=0)
    distance = explode_distance_mm or max(float(np.ptp(vertices, axis=0).max())*0.3, 1)
    offsets = {}
    for id in sorted(ids):
        direction = centers[id]-anchor
        length = float(np.linalg.norm(direction))
        if length <= 1e-9:
            direction, length = np.array([0., 0., 1.]), 1.
        offsets[id] = np.zeros(3) if id in fixed else direction/length*distance
    poses = []
    for index in range(frames):
        progress = index/(frames-1)
        eased = progress*progress*(3-2*progress)
        fraction = 1-eased if mode == 'assemble' else eased
        frame = {id: list(matrix) for id, matrix in pose.items()}
        for id, offset in offsets.items():
            for axis, column in enumerate((3, 7, 11)):
                frame[id][column] += float(offset[axis])*fraction
        poses.append(frame)
    return {**result, 'frames': poses, 'start': 0, 'step': duration_s/(frames-1),
            'solver': 'assembly presentation',
            'scope': 'Illustrated assembly sequence from saved solved parts; paths are not joint-solved or collision-validated.'}


def export_animation(result, path, *, view='iso', width=480, height=360, stride=2):
    frames = result['frames']
    indices = list(range(0,len(frames),stride))
    if indices and indices[-1] != len(frames)-1:
        indices.append(len(frames)-1)
    if len(indices) < 2:
        raise ValueError('at least two sampled frames are required')
    source, parts = result['mesh'], result['parts']
    lows, highs = np.full(3,np.inf), np.full(3,-np.inf)
    for index in indices:
        posed = np.asarray(pose_frame(source['vertices'],parts,frames[index]))
        lows,highs = np.minimum(lows,posed.min(axis=0)),np.maximum(highs,posed.max(axis=0))
    bbox = dict(zip(('x','y','z','x_min','y_min','z_min'),[*list(highs-lows),*list(lows)]))
    def images():
        for index in indices:
            mesh=Mesh.model_validate({**source,'vertices':pose_frame(source['vertices'],parts,frames[index]),'bbox':bbox})
            yield render_views(mesh,[view],width,height,supersample=1)[view]
    path=Path(path)
    duration=result['step']*stride
    # The appended final sample may be closer than a full stride. Preserve its
    # actual arrival time and give the endpoint the usual final-frame hold.
    durations=[(right-left)*result['step'] for left,right in zip(indices,indices[1:])]+[duration]
    if path.suffix=='.gif':
        pictures=[Image.fromarray(rgb) for rgb in images()]
        try:
            pictures[0].save(path,save_all=True,append_images=pictures[1:],
                duration=[max(10,round(value*1000)) for value in durations],loop=0)
        finally:
            for picture in pictures:
                picture.close()
    else:
        try:
            import av
        except ImportError as exc:
            raise ValueError('video export requires pip install -e ".[animation]"') from exc
        codec={'.mp4':'libx264','.avi':'mpeg4','.webm':'libvpx-vp9'}[path.suffix]
        if codec not in av.codecs_available:
            raise ValueError(f'installed PyAV has no {codec} encoder')
        with av.open(str(path),'w') as container:
            stream=container.add_stream(codec,rate=Fraction(1/duration).limit_denominator(10000))
            stream.width,stream.height=width,height
            stream.pix_fmt='yuv420p'
            time_base=Fraction(str(result['step'])).limit_denominator(1_000_000)
            stream.time_base=stream.codec_context.time_base=time_base
            for index,rgb in zip(indices,images()):
                frame=av.VideoFrame.from_ndarray(rgb,format='rgb24')
                frame.pts=index
                frame.time_base=time_base
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
    return {'path':str(path),'frames':len(indices),'frame_duration_s':duration,
            'frame_durations_s':durations,'view':view}
