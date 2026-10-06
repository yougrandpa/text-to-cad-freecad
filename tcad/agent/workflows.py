"""Optional domain workflows, discovered explicitly rather than injected globally."""

WORKFLOWS = {
    'radial_wheel': {
        'description': 'Connected radial structures with a rim and optional rear support.',
        'tools': ['cad_wheel_support'],
        'rules': (
            'cad_wheel builds a connected rim, spokes and hub. Use cad_wheel_support only when '
            'a rear A-frame and shaft along world Y match the requested support geometry. '
            'Build other support structures with general authoring operations. '
            'Commit and measure connections; geometry does not verify structural loads.'
        ),
    },
    'gravity_suspension': {
        'description': 'Horizontal prescribed rotation with passive gravity-hanging bodies.',
        'tools': ['cad_cabins', 'assembly_motion'],
        'rules': (
            'Use assembly_motion only for a horizontal rotation axis with upright suspended '
            'bodies whose centers of mass are below their pivots. cad_cabins optionally '
            'generates repeated open boxes and hangers for this geometry. Other geometries '
            'use general part recipes and explicit suspension pivots. The compiler measures '
            'uniform-density BRep mass properties and integrates planar gravity pendulums '
            'with moving-hinge acceleration and damping. Commit, inspect saved frames through '
            'assembly_simulate and export them through assembly_export. Native joints use '
            'assembly_configure instead. This workflow does not solve contact or structural loads.'
        ),
    },
}

SPECIALIZED_TOOLS = frozenset(tool for workflow in WORKFLOWS.values() for tool in workflow['tools'])
