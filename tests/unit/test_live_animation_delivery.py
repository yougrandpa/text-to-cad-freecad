"""Live-run acceptance distinguishes assembly presentation and working motion."""
from pathlib import Path

import pytest
from PIL import Image

from tools.run_model_e2e import animation_deliveries, delivery_passed


def gif(path, frames=3):
    images = [Image.new('RGB', (16, 16), (i*80, 0, 0)) for i in range(frames)]
    images[0].save(path, save_all=True, append_images=images[1:], duration=100, loop=0)
    return path


def test_animation_delivery_is_bound_to_current_artifact_and_actual_frames(tmp_path):
    media = gif(tmp_path/'current.gif')
    current = {'path': str(media), 'artifact_id': 'current', 'mode': 'assemble', 'frames': 41}
    old = {**current, 'artifact_id': 'old', 'mode': 'motion'}
    static = {**current, 'path': str(gif(tmp_path/'static.gif', frames=1))}
    broken = {**current, 'path': str(tmp_path/'broken.gif')}
    Path(broken['path']).write_bytes(b'not a gif')
    delivered = animation_deliveries([current, old, static, broken], 'current', tmp_path)
    assert delivered == [{**current, 'file_frames': 3}]
    assert not animation_deliveries([current], 'old', tmp_path)


@pytest.mark.parametrize('mode', ['motion', 'assemble', 'explode'])
def test_mode_is_recorded_without_promoting_one_animation_kind_to_another(tmp_path, mode):
    media = gif(tmp_path/'animation.gif')
    delivered = animation_deliveries([{'path': str(media), 'artifact_id': 'current', 'mode': mode}],
                                     'current', tmp_path)
    assert {item['mode'] for item in delivered} == {mode}
    assert not animation_deliveries([{'path': str(media), 'artifact_id': 'current', 'mode': mode}],
                                   'current', tmp_path/'other-run')


def test_static_native_assembly_can_deliver_assembly_presentation_but_motion_cannot_substitute():
    summary = {'state': 'draft', 'current_build': True, 'http_scene_status': 200,
               'http_artifact_status': {'assembly.FCStd': 200}, 'animation_frames': 1,
               'gif_paths': ['assembly.gif'], 'animation_modes': ['assemble'],
               'http_animation_status': {'assembly.gif': 200}}
    assert delivery_passed(summary, required_mode='assemble')
    assert not delivery_passed(summary, required_mode='motion')
    summary['http_animation_status']['assembly.gif'] = 404
    assert not delivery_passed(summary, required_mode='assemble')
    # Geometry-only runs retain their independent delivery criterion.
    assert delivery_passed(summary)


def test_standing_fan_case_requires_motion_media_without_extra_cli_flags():
    summary = {'state': 'draft', 'current_build': True, 'http_scene_status': 200,
               'http_artifact_status': {'fan.FCStd': 200},
               'gif_paths': [], 'animation_modes': [], 'http_animation_status': {}}
    assert delivery_passed(summary)
    assert not delivery_passed(summary, case='standing-fan')
    summary.update(gif_paths=['fan.gif'], animation_modes=['assemble'],
                   http_animation_status={'fan.gif': 200})
    assert not delivery_passed(summary, case='standing-fan', required_mode='assemble')
    summary['animation_modes'] = ['motion']
    assert delivery_passed(summary, case='standing-fan')
    summary['http_animation_status']['fan.gif'] = 404
    assert not delivery_passed(summary, case='standing-fan')


def test_resume_copies_cad_state_without_settings_or_mutating_source(tmp_path):
    from tools.run_model_e2e import copy_saved_model
    source = tmp_path/'source'
    saved = source/'models'/'e2e-model'/'v24.json'
    saved.parent.mkdir(parents=True)
    saved.write_text('{"version":24}')
    (source/'settings.json').write_text('private provider settings')
    destination = tmp_path/'continuation'
    copy_saved_model(source, destination)
    copy = destination/'models'/'e2e-model'/'v24.json'
    assert copy.read_text() == saved.read_text()
    assert not (destination/'settings.json').exists()
    copy.write_text('{"version":25}')
    assert saved.read_text() == '{"version":24}'
    with pytest.raises(ValueError, match='separate'):
        copy_saved_model(source, source/'nested')
