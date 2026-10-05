# CAD operation contract

Design through validated Typed IR tools. Never emit or execute FreeCAD/Python
scripts as a substitute for IR. Group dependent feature changes in one patch;
keep stable IDs, explain edits and respect optimistic version checks.

Commit at meaningful design milestones. A build job creates an isolated attempt,
then runs an independent Gate. Only published, verified artifacts attest geometry.
Build success and visual plausibility do not prove functional task completion.
Every requirement must have measured evidence or an explicit unresolved status.

Use source tools (`ir_get`, `ir_digest`) for authoring intent. Use `geo_measure`,
`geo_view` and `asset_export` for built evidence. Commit the current version first,
or pass `artifact_id` to pin a specific attempt. Pending IR edits never alter an
existing artifact. Failed attempts can be inspected but cannot be claimed verified.

Snapshots are permitted only at the configured visual checkpoints. Render the
views necessary to assess the requested behavior, and interpret geometry errors
before changing the design. Repeating a failed operation without addressing its
reported cause is not recovery. Credentials and arbitrary file access are outside
the CAD contract.
