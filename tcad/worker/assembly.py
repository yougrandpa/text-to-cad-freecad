"""Native Assembly solver adapter. Runs only in FreeCAD's interpreter."""
import os

from tcad.worker.compiler import _build, _close_doc, _measure

SOLVE_ERRORS = {-6: 'no grounded parts', -4: 'over-constrained assembly', -3: 'conflicting constraints',
                -5: 'malformed constraints', -1: 'native solver error', -2: 'redundant constraints'}


def _native_simulation_classes():
    """Load only upstream data proxies; GUI-only player classes require QtCore.

    Keep upstream module/class names so exported FCStd restores native proxies
    in the desktop workbench. This executes installed trusted code, never an
    agent-provided Python expression.
    """
    import ast
    import sys
    import types
    import FreeCAD as App
    import JointObject
    from PySide.QtCore import QT_TRANSLATE_NOOP
    name = "CommandCreateSimulation"
    if name not in sys.modules:
        path = os.path.join(os.path.dirname(JointObject.__file__), name + '.py')
        with open(path, encoding='utf-8') as source:
            tree = ast.parse(source.read(), path)
        classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in ('Simulation', 'Motion')]
        if len(classes) != 2:
            raise RuntimeError('installed Assembly simulation data proxies are unavailable')
        module = types.ModuleType(name)
        module.__dict__.update(App=App, QT_TRANSLATE_NOOP=QT_TRANSLATE_NOOP, MotionTypes=['Angular', 'Linear'])
        exec(compile(ast.Module(body=classes, type_ignores=[]), path, 'exec'), module.__dict__)
        sys.modules[name] = module
    module = sys.modules[name]
    return module.Simulation, module.Motion


def simulate_assembly(ir=None, out_dir='', check_pairs=None, check_stride=1, solve_only=False, **_extra):
    import FreeCAD as App
    import JointObject
    Simulation, Motion = _native_simulation_classes()
    spec = ir.get('assembly')
    if not spec:
        return {'ok': False, 'error': 'configure the native assembly first'}
    os.makedirs(out_dir, exist_ok=True)
    built = _build(ir, out_dir)
    try:
        if built['errors']:
            return {'ok': False, 'errors': built['errors']}
        doc = built['doc']
        assembly = doc.addObject('Assembly::AssemblyObject', 'Assembly')
        group = assembly.newObject('Assembly::JointGroup', 'Joints')
        parts, initial, ranges, vertices, facets = {}, {}, [], [], []
        for body in built['body_results']:
            part = assembly.newObject('Part::Feature', 'Component')
            part.Label = body['id']
            part.Shape = body['shape'].copy()
            parts[body['id']] = part
            initial[body['id']] = part.Placement.copy()
            verts, tris = part.Shape.tessellate(0.5)
            start = len(vertices)
            vertices.extend([[float(v.x), float(v.y), float(v.z)] for v in verts])
            facets.extend([[int(a)+start, int(b)+start, int(c)+start] for a,b,c in tris])
            ranges.append({'body_id': body['id'], 'vertex_start': start, 'vertex_count': len(verts)})
        for id in spec['grounded']:
            obj = group.newObject('App::FeaturePython', 'GroundedJoint')
            JointObject.GroundedJoint(obj, parts[id])
        joints = {}
        def connector(side, part):
            world = App.Placement(App.Vector(*side['position']),
                App.Rotation(App.Vector(0,0,1), App.Vector(*side['axis'])).multiply(
                    App.Rotation(App.Vector(0,0,1), side.get('angle_deg', 0))))
            return part.Placement.inverse().multiply(world)
        for definition in spec['joints']:
            obj = group.newObject('App::FeaturePython', 'Joint')
            obj.Label = definition['id']
            JointObject.Joint(obj, JointObject.JointTypes.index(definition['type']))
            joints[definition['id']] = obj
            obj.Detach1 = obj.Detach2 = True
            for index in (1,2):
                side = definition[f'side{index}']
                part = parts[side['body_id']]
                element, vertex = side.get('element', ''), side.get('vertex', '')
                for name in (element, vertex):
                    if name:
                        part.Shape.getElement(name)
                setattr(obj, f'Reference{index}', (part, [element, vertex]))
                setattr(obj, f'Placement{index}', connector(side, part))
            obj.Distance = definition.get('distance', 0)
            obj.Distance2 = definition.get('distance2', 0)
            obj.Angle = definition.get('angle', 0)
            obj.Suppressed = definition.get('suppressed', False)
            for key, prop in (('length_min','LengthMin'), ('length_max','LengthMax'), ('angle_min','AngleMin'), ('angle_max','AngleMax')):
                value = definition.get(key)
                setattr(obj, f'Enable{prop}', value is not None)
                if value is not None:
                    setattr(obj, prop, value)
        doc.recompute()
        code = assembly.solve()
        if code != 0:
            return {'ok': False, 'error': {'kind':'solver', 'message': SOLVE_ERRORS.get(code, f'solve code {code}')}}
        count = 1
        if not solve_only:
            sim_group = assembly.newObject('Assembly::SimulationGroup', 'Simulations')
            sim = sim_group.newObject('App::FeaturePython', 'Simulation')
            Simulation(sim)
            sim.aTimeStart, sim.bTimeEnd, sim.cTimeStepOutput = spec['start'], spec['end'], spec['step']
            sim.fGlobalErrorTolerance = 1e-6
            for definition in spec['drivers']:
                obj = doc.addObject('App::FeaturePython', 'Motion')
                Motion(obj, definition['type'], (joints[definition['joint_id']], ['']), definition['formula'])
                sim.addObject(obj)
            if not spec['drivers']:
                return {'ok': False, 'error': 'simulation requires at least one native driver'}
            code = assembly.generateSimulation(sim)
            if code != 0:
                return {'ok': False, 'error': {'kind':'solver', 'message': SOLVE_ERRORS.get(code, f'simulation code {code}')}}
            count = assembly.numberOfFrames()
            if not 2 <= count <= 601:
                return {'ok': False, 'error': f'native solver returned invalid frame count {count}'}
        selected = check_pairs or []
        if not isinstance(check_stride, int) or not 1 <= check_stride <= 30 or len(selected) > 100 or any(len(pair) != 2 or pair[0] == pair[1] or any(id not in parts for id in pair) for pair in selected):
            raise ValueError('invalid collision body pairs or frame stride')
        frames, interferences, frames_checked = [], [], 0
        # Ondsel prepends the unsolved input state before the t=start frame.
        # Skip it so frame 0 means the first actual simulation solution.
        for index, native_index in enumerate([None] if solve_only else range(1,count)):
            if native_index is not None:
                assembly.updateForFrame(native_index)
            poses = {}
            for id, part in parts.items():
                delta = part.Placement.multiply(initial[id].inverse())
                poses[id] = list(delta.toMatrix().A)
            frames.append(poses)
            if selected and index % check_stride == 0:
                frames_checked += 1
                for a,b in selected:
                    common = parts[a].Shape.common(parts[b].Shape)
                    if not common.isValid():
                        raise ValueError('invalid native collision intersection')
                    volume = float(common.Volume)
                    if volume > 1e-6:
                        interferences.append({'frame': index, 'time': spec['start']+index*spec['step'], 'bodies':[a,b], 'overlap_mm3':volume})
        if not solve_only:
            assembly.updateForFrame(1)
        path = os.path.join(out_dir, 'assembly.FCStd')
        doc.recompute()
        doc.saveAs(path)
        return {'ok': True, 'mesh': {'vertices': vertices, 'facets': facets, 'vertex_count':len(vertices), 'facet_count':len(facets), 'bbox': _measure(built['result_shape'])['bbox'], 'volume': _measure(built['result_shape'])['volume'], 'tolerance':0.5}, 'parts': ranges,
                'frames': frames, 'interferences':interferences if selected else None, 'frames_checked':frames_checked, 'start': spec['start'], 'step': spec['step'], 'export': path,
                'solver': 'FreeCAD Assembly / OndselSolver',
                'scope': 'Native kinematic joint solution; no contact forces or material removal.'}
    finally:
        _close_doc(built['doc'])


def solve_assembly(**params):
    return simulate_assembly(**{**params, "solve_only": True})
