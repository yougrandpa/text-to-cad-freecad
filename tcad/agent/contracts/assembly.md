# Assembly operation contract

A body is one sequential PartDesign feature history. Independent bodies may be
compiled in parallel; dependent features within one body must stay sequential.

Reuse a verified component with `add_body` / `update_body` `part_ref` containing
`model_id`, `artifact_id`, `body_id` and optional `placement`. A PartRef body cannot
also contain local sketches/features. Pin immutable IDs; never refer to a live
document or a mutable source version as built geometry.

Configure native joints/drivers through `assembly_configure`, then `ir_commit`.
`assembly_solve` and `assembly_simulate` read saved solutions/frames; edits need a
new commit. Prescribed `body.motion` and native assembly drivers cannot be mixed.
Use `frame_index` for native snapshot frames and `driver_angle_deg` for prescribed
rotation. `assembly_export` produces media from saved poses.

`geo_check_motion` and `assembly_simulate(check_pairs=...)` check sampled BRep
overlap on the pinned artifact. A clear sample set does not prove continuous
clearance, contact forces, gear meshing or material removal. State that scope and
keep unresolved functional requirements visible.
