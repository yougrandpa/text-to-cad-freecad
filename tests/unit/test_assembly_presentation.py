"""Assembly illustrations retain the saved solved pose and artifact identity."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tcad.ir.animation import pose_frame
from tcad.render.animation import assembly_transition, export_animation


IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


def assembly():
    rotated = [0, -1, 0, 8, 1, 0, 0, 2, 0, 0, 1, 3, 0, 0, 0, 1]
    return {'mesh': {'vertices': [[0, 0, 0], [1, 0, 0], [0, 1, 0]]*2,
                     'facets': [[0, 1, 2], [3, 4, 5]]},
            'parts': [{'body_id': 'base', 'vertex_start': 0, 'vertex_count': 3},
                      {'body_id': 'rotor', 'vertex_start': 3, 'vertex_count': 3}],
            'frames': [{'base': IDENTITY.copy(), 'rotor': rotated}],
            'start': 0, 'step': 0.1, 'artifact_id': 'sha256:'+'a'*64,
            'scope': 'Native saved kinematics'}


def test_assemble_and_explode_are_reversible_rigid_paths_ending_at_solved_pose():
    source = assembly()
    before = copy.deepcopy(source)
    kwargs = {'grounded_body_ids': ['base'], 'frames': 9,
              'duration_s': 4, 'explode_distance_mm': 20}
    assembled = assembly_transition(source, mode='assemble', **kwargs)
    exploded = assembly_transition(source, mode='explode', **kwargs)
    assert source == before
    assert assembled['frames'][-1] == source['frames'][0]
    assert exploded['frames'][0] == source['frames'][0]
    for forward, backward in zip(assembled['frames'], reversed(exploded['frames'])):
        assert forward['rotor'] == pytest.approx(backward['rotor'])
        assert forward['base'] == source['frames'][0]['base']
        # Translation alone must preserve the rotation and homogeneous row.
        assert [forward['rotor'][i] for i in range(16) if i not in (3, 7, 11)] == [
            source['frames'][0]['rotor'][i] for i in range(16) if i not in (3, 7, 11)]
    first = pose_frame(source['mesh']['vertices'], source['parts'], assembled['frames'][0])
    last = pose_frame(source['mesh']['vertices'], source['parts'], assembled['frames'][-1])
    import math
    assert math.dist(first[3], last[3]) == pytest.approx(20)
    assert assembled['step']*(len(assembled['frames'])-1) == 4
    assert 'not joint-solved or collision-validated' in assembled['scope']


def test_automatic_separation_keeps_model_scale_readable():
    import math
    source = assembly()
    result = assembly_transition(source, mode='assemble', grounded_body_ids=['base'])
    separated = pose_frame(source['mesh']['vertices'], source['parts'], result['frames'][0])
    assembled = pose_frame(source['mesh']['vertices'], source['parts'], result['frames'][-1])
    span = max(max(p[axis] for p in assembled)-min(p[axis] for p in assembled) for axis in range(3))
    assert span*0.1 < math.dist(separated[3], assembled[3]) <= span*0.5


@pytest.mark.parametrize('options', [
    {'mode': 'other'}, {'frames': 1}, {'frames': True}, {'duration_s': float('nan')},
    {'explode_distance_mm': -2}, {'grounded_body_ids': ['unknown']},
    {'grounded_body_ids': ['base', 'rotor']},
])
def test_invalid_presentation_arguments_cannot_produce_misleading_frames(options):
    with pytest.raises(ValueError):
        assembly_transition(assembly(), **{'mode': 'assemble', 'grounded_body_ids': ['base'], **options})


def test_export_sampling_keeps_final_assembled_pose(tmp_path, monkeypatch):
    result = assembly_transition(assembly(), mode='assemble', grounded_body_ids=['base'], frames=4)
    captured = []
    import numpy as np
    def render(mesh, views, width, height, supersample):
        captured.append(mesh.vertices)
        return {views[0]: np.zeros((height, width, 3), dtype=np.uint8)}
    monkeypatch.setattr('tcad.render.animation.render_views', render)
    summary = export_animation(result, tmp_path/'assembly.gif', width=128, height=128, stride=2)
    assert summary['frames'] == 3
    assert [list(point) for point in captured[-1]] == pose_frame(result['mesh']['vertices'], result['parts'], result['frames'][-1])
    assert (tmp_path/'assembly.gif').is_file()


def test_appended_final_gif_frame_arrives_at_the_original_time(tmp_path, monkeypatch):
    from PIL import Image
    import numpy as np
    result = assembly_transition(assembly(), mode='assemble', grounded_body_ids=['base'],
                                 frames=4, duration_s=3)
    colors = iter((40, 120, 200))
    monkeypatch.setattr('tcad.render.animation.render_views', lambda mesh, views, *args, **kwargs:
                        {views[0]: np.full((128, 128, 3), next(colors), dtype=np.uint8)})
    path = tmp_path/'timed.gif'
    summary = export_animation(result, path, width=128, height=128, stride=2)
    with Image.open(path) as gif:
        assert gif.n_frames == 3
        delays = []
        for index in range(gif.n_frames):
            gif.seek(index)
            delays.append(gif.info['duration']/1000)
    assert delays == [2, 1, 2]
    assert sum(delays[:-1]) == 3
    assert summary['frame_durations_s'] == delays


@pytest.mark.parametrize('suffix,codec', [('.mp4', 'libx264'), ('.avi', 'mpeg4'), ('.webm', 'libvpx-vp9')])
def test_appended_final_video_frame_keeps_saved_timestamps(tmp_path, monkeypatch, suffix, codec):
    av = pytest.importorskip('av')
    if codec not in av.codecs_available:
        pytest.skip(f'{codec} encoder unavailable')
    import numpy as np
    result = assembly_transition(assembly(), mode='assemble', grounded_body_ids=['base'],
                                 frames=4, duration_s=3)
    colors = iter((40, 120, 200))
    monkeypatch.setattr('tcad.render.animation.render_views', lambda mesh, views, *args, **kwargs:
                        {views[0]: np.full((128, 128, 3), next(colors), dtype=np.uint8)})
    path = tmp_path/('timed'+suffix)
    export_animation(result, path, width=128, height=128, stride=2)
    with av.open(str(path)) as video:
        timestamps = [float(frame.pts*frame.time_base) for frame in video.decode(video=0)]
    assert timestamps == pytest.approx([0, 2, 3], abs=0.01)


def test_assembly_export_uses_pinned_grounding_without_reading_live_ir(tmp_path, monkeypatch):
    from tcad.inspect.operations import export_saved_animation
    result = assembly()
    def forbidden(*args, **kwargs):
        raise AssertionError('Presentation must use immutable artifact data')
    services = SimpleNamespace(store=SimpleNamespace(load=forbidden, current_version=forbidden),
                               worker=SimpleNamespace(request=forbidden))
    ctx = SimpleNamespace(data_dir=str(tmp_path), model_id='part')
    monkeypatch.setattr('tcad.inspect.operations.saved_assembly', lambda *_: (result, 'scene.json'))
    reads = []
    def resolve(_services, _ctx, args):
        reads.append(args)
        reader = SimpleNamespace(read_file=lambda *_: json.dumps({'assembly': {'grounded': ['base']}}))
        return reader, SimpleNamespace(artifact_id=result['artifact_id']), Path(tmp_path)
    monkeypatch.setattr('tcad.inspect.operations.resolve_context', resolve)
    exported = export_saved_animation(services, ctx, {'mode': 'assemble', 'frames': 5, 'stride': 1,
                                                     'width': 128, 'height': 128})
    assert reads == [{'artifact_id': result['artifact_id']}]
    assert exported['artifact_id'] == result['artifact_id']
    assert exported['mode'] == 'assemble' and exported['frames'] == 5
    assert Path(exported['path']).stat().st_size > 100
