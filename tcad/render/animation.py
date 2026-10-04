"""Export solved animation frames using a fixed camera extent."""
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image

from tcad.core.types import Mesh
from tcad.ir.animation import pose_frame
from tcad.render.raster import render_views


def export_animation(result, path, *, view='iso', width=480, height=360, stride=2):
    frames = result['frames']
    indices = list(range(0,len(frames),stride))
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
    if path.suffix=='.gif':
        pictures=[Image.fromarray(rgb) for rgb in images()]
        try:
            pictures[0].save(path,save_all=True,append_images=pictures[1:],duration=max(10,round(duration*1000)),loop=0)
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
            for rgb in images():
                for packet in stream.encode(av.VideoFrame.from_ndarray(rgb,format='rgb24')):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
    return {'path':str(path),'frames':len(indices),'frame_duration_s':duration,'view':view}
