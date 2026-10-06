# Assembly operation contract

Choose single-body or multi-body modeling yourself based on the requirements and
their complexity. Assess function, integral construction, manufacturing boundaries,
removability and relative motion. Use the simplest structure that faithfully meets
the request: an integral part can stay in one Body even with many features; separate
components that need independent geometry or motion use distinct named Bodies.
Feature count alone does not require splitting. Honor explicit single-part or
assembly intent, and preserve existing part boundaries when editing. Briefly
explain the chosen structure in the feature plan when it affects the design.

When multiple Bodies are needed, identify the parts, their roles, placement and
connections before creating geometry. Touching or synchronized components can
still be distinct parts; connected features of an integral part share its history.

Create local parts with `add_body` before adding sketches/features and explicitly
set their `payload.body_id`. With multiple bodies, omitting `body_id` is rejected;
with zero or one body, the single-part shorthand remains supported. Updates use
the existing sketch/feature `target_id` and preserve ownership. Read `ir_get` or
`ir_list_features` to inspect IDs and ownership before editing an existing model.

For an assembly, place parts in a coherent assembled pose and configure native joints for their
actual connections and requested motion. Use Fixed for rigid connections and
appropriate movable joints for moving parts; do not invent drivers for a static
assembly. A multi-body compound is geometry, not evidence of solved constraints.
For a constrained assembly, inspect the saved solve after committing; for motion,
inspect saved simulation frames and check the relevant part pairs for overlap.

A body is one sequential PartDesign feature history. Independent bodies may be
compiled in parallel; dependent features within one body must stay sequential.

Reuse a verified component with `add_body` / `update_body` `part_ref` containing
`model_id`, `artifact_id`, `body_id` and optional `placement`. A PartRef body cannot
also contain local sketches/features. Pin immutable IDs; never refer to a live
document or a mutable source version as built geometry.

Configure native joints/drivers through `assembly_configure`, then `ir_commit`.
Optional domain workflows are discoverable through `ir_help(topic=workflow)`.
Select one only when its assumptions match the requested geometry and motion.
`assembly_solve` and `assembly_simulate` read saved solutions/frames; edits need a
new commit. Prescribed `body.motion` and native assembly drivers cannot be mixed.
Use `frame_index` for native snapshot frames and `driver_angle_deg` for prescribed
rotation. `assembly_export` produces media from saved poses.

`geo_check_motion` and `assembly_simulate(check_pairs=...)` check sampled BRep
overlap on the pinned artifact. A clear sample set does not prove continuous
clearance, contact forces, gear meshing or material removal. State that scope and
keep unresolved functional requirements visible.
