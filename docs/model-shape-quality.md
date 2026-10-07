# Shape selection and static review

Valid solids and exportable files are build evidence, not visual acceptance.
The production prompt and public tool guidance ask the model to plan primary
silhouettes, proportions, section changes and part boundaries before details
or motion. These rules apply across tasks; they do not prescribe a robot-dog
template, forbid rectangular parts or impose a curved-feature quota.

`ir_help(topic="shape")` returns a short choice catalog and static review
guidance. `ir_help(topic="shape", shape="loft")` returns only the loft recipe
schema, its limitations and a standalone executable `cad_build_parts` example.
All six compact recipe shapes support this query. The schema is derived from
the real tool schema; it does not change tool permissions or restrict later
mixed-shape batches.

- Use box/beam for appropriate prismatic stock and constant rectangular members.
- Use cylinder/tube for round shafts, bosses and hollow round members.
- Use loft for varying elliptical sections. It is a solid, with finite-area
  end sections, rather than a shell or an arbitrary surface model.
- For other outlines, discover native sketch curves, pad/pocket and custom
  loft sections through scoped sketch/feature help.
- Treat fillets/chamfers as edge treatments after the main outline is right.

Smoothness is not fidelity. Preserve structural landmarks and plausible
transitions; adding many alternating loft stations can create unintended
waves or bulges rather than a convincing mechanical part.

After compiling the primary shape, render an isometric view and a relevant
orthographic view through `geo_view`, inspect the actual images, and compare
the outline and proportions with the request. Repair coarse placeholders
before adding secondary details or animation. Record visible observations
and unresolved differences in `design_review`; those observations remain
model claims requiring acceptance, not deterministic Gate measurements.

Curved BReps can carry finer cached triangulation from exports. OCC reuses
that triangulation for later coarse requests, so increasing preview tolerance
alone may leave counts unchanged. Preview face meshing now uses a cleaned
copy of each face, preserving the original BRep, local face/edge identities
and exports while making the existing adaptive tolerance ladder effective.
Preview limits remain bounded; an exhausted ladder leaves visual acceptance
unresolved, and repeating an unchanged commit is not offered as a repair.

Equal semi-axes describe a circle. Some FreeCAD Sketcher builds abort while
solving such a curve represented as an ellipse. Compact loft recipes emit an
exact circle for equal axes; the worker also handles explicit equal-axis
ellipse geometry as the equivalent circle. No radius perturbation or tolerance
relaxation is used; unequal-axis ellipses retain their original geometry.

The black-box CLI exercises real tools, compilation and inspection, but does
not run production provider conversations or enforce LoopEngine completion.
Passing two agent-operated examples cannot establish general provider success
rates or independently prove appearance, manufacturing fitness or locomotion.

Primary outline intent is stored with `ir_plan` using `form` (prismatic,
round, tapered or curved) and `outline_id` (the stable compact recipe ID or
native feature shaping that outline). For example, a helicopter airframe can
bind its tapered cabin to a loft recipe while shafts remain cylinders and
straight blades remain rectangular. This is a shape choice, not a helicopter
template. Scoped help is returned with the plan to make appropriate tools
easy to discover before authoring.

Plan updates that omit these fields retain their earlier values. Persisted
plans are reloaded into the fixed requirement context on subsequent turns and
when tool history is compacted. Repairs should preserve the primary outline;
preview mesh limits are display failures and do not justify replacing CAD
geometry with cheaper primitives. Intentional simplifications must be recorded.

Shape advisories inspect active features of the bound recipe/feature and
profiles actually consumed by additive operations. Suppressed features,
unused/construction curves, unrelated supports and drilled holes cannot stand
in for the primary outline. Body-level shape goals with multiple features ask
for an explicit outline binding. Legacy goals remain readable, including
`tapered` and Chinese section-change terms. These are conservative structural
hints: a loft alone does not establish the right taper, proportions or visual
fidelity, and custom outlines may still need manual acceptance.

Visual inspection requires a provider with actual image input capability.
Without it, render availability does not mean the agent saw the model, and
visual acceptance must remain unresolved. Enabling a capability flag cannot
add vision to a text-only provider. Unit regressions validate intent retention
and scoped review, not real-provider modeling quality or success rates.
