"""Task-neutral shape selection and compact, executable recipe examples."""

SHAPE_CHOICES = {
    'box': 'Prismatic stock, plates and rectangular enclosures; not a substitute for a requested shaped outline.',
    'beam': 'Straight constant rectangular-section members; tapered or curved members need loft or sketch/pad.',
    'cylinder': 'Round shafts, pins, bosses and cylindrical cuts.',
    'tube': 'Straight hollow round members; inner_radius is smaller than radius.',
    'loft': 'Smooth or ruled transitions through elliptical sections; changing radii and centers shapes the primary silhouette.',
    'rotor': 'A cylindrical hub with flat rectangular blades only; other blade profiles need native sketch/feature operations.',
}

FORM_ROUTES = {
    'prismatic': 'box/beam for constant rectangular sections.',
    'round': 'cylinder/tube for constant round sections; native sketch/pad for other profiles.',
    'tapered': 'ir_help(topic=shape,shape=loft) for varying elliptical sections; native cone or custom loft profiles otherwise.',
    'curved': 'ir_help(topic=shape,shape=loft) for shaped elliptical sections; ir_help(topic=sketch) for other curves.',
}

SHAPE_REVIEW = (
    'Before detailed modeling, describe the primary silhouette, proportions, cross-section changes '
    'and part boundaries. Match operations to those shapes: prismatic parts may stay box/beam; '
    'curved or tapered outlines need appropriate profiles or lofts rather than stacked boxes. '
    'Smoothness alone is not fidelity: retain structural landmarks and plausible section '
    'transitions; avoid unnecessary waves or bulges. '
    'After commit, render an isometric view and a relevant orthographic view with geo_view; '
    'inspect the returned images. Compare the outline and proportions to the request, repair '
    'placeholder shapes, then add secondary details. Small fillets and holes alone do not '
    'correct a wrong silhouette. Describe visible observations and unresolved differences '
    'in design_review; render availability and solid validity do not prove visual fidelity.'
)


def shape_example(shape):
    """Examples are standalone native recipes, not a task-specific template."""
    dimensions = {
        'box': {'center': [0, 0, 5], 'size': [40, 24, 10]},
        'beam': {'start': [0, 0, 0], 'end': [30, 0, 40], 'width': 6, 'depth': 4},
        'cylinder': {'start': [0, 0, 0], 'end': [0, 0, 20], 'radius': 4},
        'tube': {'start': [0, 0, 0], 'end': [0, 0, 20], 'radius': 5, 'inner_radius': 3},
        'loft': {'section_axis': 'X', 'ruled': False, 'sections': [
            {'center': [-30, 0, 15], 'radii': [6, 8]},
            {'center': [-10, 0, 18], 'radii': [12, 15]},
            {'center': [15, 0, 18], 'radii': [11, 13]},
            {'center': [30, 0, 15], 'radii': [5, 6]},
        ]},
        'rotor': {'center': [0, 0, 0], 'axis': [0, 0, 1], 'radius': 30,
                  'blade_count': 3, 'blade_width': 8, 'thickness': 4, 'hub_radius': 8},
    }
    return {'parts': [{'id': 'outline', 'body_id': 'part', 'shape': shape,
                       **dimensions[shape]}], 'reason': 'Create the chosen primary outline'}
