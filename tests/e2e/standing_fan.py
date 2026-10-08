"""A public-output oracle; acceptance criteria never enter the model prompt."""
from tcad.inspect.motion import measure_saved_motion

REQUEST = '创建一个落地风扇，带动画和摇头。'


def standing_fan_evidence(source: dict, scene) -> dict:
    assembly = source.get('assembly') or {}
    animation = scene.animation
    if not animation:
        return {'passed': False, 'failures': ['No saved native motion frames.']}
    summary = measure_saved_motion(animation, scene.mesh.vertices, assembly=assembly)
    fixed = []
    for joint in assembly.get('joints', []):
        if not joint.get('suppressed') and joint['type'] == 'Fixed':
            fixed.append((joint['side1']['body_id'], joint['side2']['body_id']))

    def rigid_group(seeds):
        group = set(seeds)
        while True:
            old = set(group)
            for a,b in fixed:
                if a in group or b in group:
                    group.update((a,b))
            if old == group:
                return group

    grounded = rigid_group(assembly.get('grounded', []))
    joints = summary['joints']
    carriers = []
    rotors = []
    for yaw in joints:
        a,b = yaw['body_ids']
        if abs(yaw['axis_world'][2]) < 0.95 or ((a in grounded) == (b in grounded)):
            continue
        moving = rigid_group([b if a in grounded else a])
        carriers.append(yaw)
        for spin in joints:
            c,d = spin['body_ids']
            if abs(spin['axis_world'][2]) < 0.2 and ((c in moving) != (d in moving)):
                rotors.append((yaw, spin))
    valid = [(yaw,spin) for yaw,spin in rotors
             if yaw['max_angle_from_first_deg'] - yaw['min_angle_from_first_deg'] > 10
             and yaw['direction_reversals'] >= 1
             and yaw['sampled_angular_path_deg'] >= 2*(yaw['max_angle_from_first_deg'] - yaw['min_angle_from_first_deg']) - 1e-4
             and spin['sampled_angular_path_deg'] >= 360 - 1e-5
             and spin['direction_reversals'] == 0 and spin['max_step_deg'] < 120]
    body_evidence = {b['body_id']: b for b in summary['bodies']}
    stationary = bool(grounded) and all(body_evidence[b]['max_rotation_from_first_deg'] < 1e-5
        and body_evidence[b]['preview_bbox_center']['max_displacement_from_first_mm'] < 1e-5
        for b in grounded)
    bbox = scene.mesh.bbox
    upright = bbox.z >= 1.5*max(bbox.x, bbox.y) and abs(bbox.z_min) < max(1e-5, bbox.z*0.01)
    failures = []
    if not stationary: failures.append('Base/support moves in the saved poses.')
    if not upright: failures.append('No upright floor-standing proportions at the ground plane.')
    if len(animation['frames']) < 20: failures.append('Too few saved frames to inspect the full motion.')
    if not valid: failures.append('No nested vertical oscillation with a visible back-and-forth angular range plus horizontal relative rotor spin of at least one turn.')
    if summary['warnings']: failures.extend(summary['warnings'])
    return {'passed': not failures, 'failures': failures, 'stationary_bodies': sorted(grounded),
            'frames_examined': len(animation['frames']), 'joint_motion': joints,
            'nested_motion_candidates': [{'oscillation_joint': yaw['joint_id'], 'spin_joint': spin['joint_id']}
                                         for yaw,spin in rotors],
            'scope': 'Saved-pose concept motion and gross standing proportions only. Blade/guard fidelity, '
                     'physical attachments, sampled collision checks and media must be reviewed separately; '
                     'this is not engineering or continuous-clearance acceptance.'}
