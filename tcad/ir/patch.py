"""The ONLY mutation path for an :class:`IrDocument`.

The model never edits an IR in place; it proposes an :class:`IrPatch`, and this
module applies it. Two hard rules live here:

1. **Optimistic concurrency is mandatory.** ``patch.base_version`` must equal the
   current ``ir.version``. If it does not, the patch is *rejected* with a
   ``ToolError(kind=SEMANTIC)`` — we never silently rebase. (design §4.4)

2. **apply_patch is pure.** It returns a *new* ``IrDocument`` (input is
   deep-copied, never mutated) plus a :class:`PatchOutcome`. It performs no IO;
   the store layer turns the outcome into an event + snapshot. This keeps the
   mutation semantics unit-testable and the store the sole owner of durability.

Semantics of the ops
---------------------
* ``add_sketch`` / ``add_feature`` — payload carries the new entity's fields
  (``id``/``name`` optional; minted via :mod:`tcad.ir.naming` when omitted). The
  entity is appended to a body (``body_id`` in payload, else first body, else a
  fresh body is created).
* ``update_sketch`` / ``update_feature`` — **partial** updates. Scalar fields are
  set in place. **List fields are replace-not-append** by default: assign the
  whole list via ``geometry``/``constraints``/``refs``. To *append*, pass
  ``geometry_append``/``constraints_append``/``refs_append`` instead. ``params``
  (a dict) is merged field-wise. Ambiguity here is the #1 multi-turn editing bug,
  hence this explicit rule.
* ``remove_feature`` — refuses (semantic error) if another feature references the
  victim via ``refs``; the error names the dependents so the model can fix them.
  Pass ``cascade=true`` only for the rare deliberate case: the victim is removed
  and stripped from dependents' ``refs`` (dependents themselves are *rewired*,
  not deleted — deleting a chain of dependents the model never asked to remove
  would be a nastier surprise).
* ``update_requirement`` — merges into ``ir.requirements`` (``raw_text`` set,
  ``constraints`` replaced or ``constraints_append``-ed).
* ``rename`` — only touches ``name`` (the stable multi-turn handle) and refuses
  to create a name collision (design §12-9: an ambiguous name is unreferenceable).

After all ops, the resulting IR is run through :func:`tcad.ir.validate.validate_ir`.
Any ``error``-severity issue rejects the whole patch (the IR is left untouched).
"""

from __future__ import annotations

from typing import NoReturn

from pydantic import BaseModel

from tcad.core.types import ToolError, ToolErrorKind
from tcad.ir import naming


class PatchError(Exception):
    """Raised by :func:`apply_patch` when a patch cannot be applied.

    ``ToolError`` (in :mod:`tcad.core.types`) is a pydantic *data* model — it is
    the error payload that flows back to the loop as ``ToolResult.error`` and is
    never raised directly. This exception is the transport: it carries the
    ``ToolError`` so callers (the loop, the tests) can inspect ``.error.kind``,
    ``.error.message`` and ``.error.feature_id`` without a custom exception type
    leaking into the frozen ``core.types`` contract.
    """

    def __init__(self, error: ToolError) -> None:
        super().__init__(error.message)
        self.error = error

def _reject(kind, message, *, feature_id=None, hint="") -> "NoReturn":
    """Build a ToolError and raise it wrapped in PatchError."""
    raise PatchError(ToolError(kind=kind, message=message,
                              feature_id=feature_id, hint=hint))



from tcad.ir.schema import (
    BodySpec,
    ConstraintExpr,
    FeatureSpec,
    IrDocument,
    IrPatch,
    IrPatchOp,
    PlaneRef,
    SketchConstraint,
    SketchGeom,
    SketchSpec,
    Vec3,
)
from tcad.ir.validate import validate_ir


class PatchOutcome(BaseModel):
    """Result of a successful :func:`apply_patch`.

    ``ir`` is the new document; ``version`` is exactly ``old_version + 1``.
    ``created_ids`` / ``renamed_ids`` let the loop tell the model what appeared
    or was renamed. ``changes`` are human-readable records for the event log.
    ``ir`` is populated on the returned outcome; it is optional only so the
    outcome can be constructed incrementally while ops are applied.
    """

    ir: IrDocument | None = None
    version: int
    applied: int
    summary: str
    created_ids: list[str] = []
    renamed_ids: list[str] = []
    changes: list[str] = []


# ── helpers ───────────────────────────────────────────────────────────────────

def _all_ids(ir: IrDocument) -> tuple[set[str], set[str]]:
    return ({s.id for s in ir.all_sketches()},
            {f.id for f in ir.all_features()})


def _require_body(ir: IrDocument, payload: dict) -> BodySpec:
    body_id = payload.get("body_id")
    if body_id:
        body = next((b for b in ir.bodies if b.id == body_id), None)
        if body is None:
            raise _reject(
                kind=ToolErrorKind.NOT_FOUND,
                message=f"body '{body_id}' not found",
                hint="add the body first, or omit body_id to target the first body")
        return body
    if not ir.bodies:
        body = BodySpec(id=naming.unique_name({b.id for b in ir.bodies}, "body_1"),
                        name="body_1")
        ir.bodies.append(body)
        return body
    return ir.bodies[0]


def _merge_sketch(sk: SketchSpec, payload: dict) -> None:
    for k, v in payload.items():
        if k == "geometry_append":
            sk.geometry = list(sk.geometry) + [SketchGeom(**g) for g in v]
        elif k == "constraints_append":
            sk.constraints = list(sk.constraints) + [SketchConstraint(**c) for c in v]
        elif k == "geometry":
            sk.geometry = [SketchGeom(**g) for g in v]
        elif k == "constraints":
            sk.constraints = [SketchConstraint(**c) for c in v]
        elif k == "plane":
            setattr(sk, k, PlaneRef(**v))
        elif k == "offset":
            setattr(sk, k, Vec3(**v) if v is not None else None)
        elif k not in ("id",):
            setattr(sk, k, v)


def _merge_feature(f: FeatureSpec, payload: dict) -> None:
    for k, v in payload.items():
        if k == "refs_append":
            f.refs = list(f.refs) + list(v)
        elif k == "refs":
            f.refs = list(v)
        elif k == "params":
            f.params = {**f.params, **v}
        elif k not in ("id",):
            setattr(f, k, v)


# ── op handlers ───────────────────────────────────────────────────────────────

def _op_add_sketch(ir: IrDocument, op: IrPatchOp, out: PatchOutcome) -> None:
    p = op.payload
    sk_ids, _ = _all_ids(ir)
    sid = p.get("id") or naming.unique_name(
        sk_ids, naming.default_sketch_name(p.get("name"), len(ir.all_sketches())))
    if sid in sk_ids:
        raise _reject(kind=ToolErrorKind.SEMANTIC,
                        message=f"sketch id '{sid}' already exists",
                        feature_id=sid)
    name = p.get("name") or naming.default_sketch_name(None, len(ir.all_sketches()))
    body = _require_body(ir, p)
    sk = SketchSpec(
        id=sid,
        name=name,
        plane=PlaneRef(**p["plane"]) if "plane" in p else PlaneRef(kind="origin_plane", plane="XY"),
        map_mode=p.get("map_mode", "FlatFace"),
        reversed=p.get("reversed", False),
        offset=Vec3(**p["offset"]) if p.get("offset") else None,
        geometry=[SketchGeom(**g) for g in p.get("geometry", [])],
        constraints=[SketchConstraint(**c) for c in p.get("constraints", [])],
        require_fully_constrained=p.get("require_fully_constrained", True),
    )
    body.sketches.append(sk)
    out.created_ids.append(sid)
    out.changes.append(f"add_sketch '{name}' (id={sid}) to body '{body.id}': {op.reason or '-'}")


def _op_update_sketch(ir: IrDocument, op: IrPatchOp, out: PatchOutcome) -> None:
    sk = ir.find_sketch(op.target_id)
    if sk is None:
        raise _reject(kind=ToolErrorKind.NOT_FOUND,
                        message=f"sketch '{op.target_id}' not found",
                        feature_id=op.target_id)
    _merge_sketch(sk, op.payload)
    out.changes.append(f"update_sketch '{sk.name}' (id={sk.id}): {op.reason or '-'}")


def _op_add_feature(ir: IrDocument, op: IrPatchOp, out: PatchOutcome) -> None:
    p = op.payload
    if "op" not in p:
        raise _reject(kind=ToolErrorKind.SEMANTIC,
                        message="add_feature requires 'op'", feature_id=op.target_id)
    _, f_ids = _all_ids(ir)
    fid = p.get("id") or naming.unique_name(
        f_ids, naming.default_feature_name(p["op"], len(ir.all_features())))
    if fid in f_ids:
        raise _reject(kind=ToolErrorKind.SEMANTIC,
                        message=f"feature id '{fid}' already exists",
                        feature_id=fid)
    name = p.get("name") or naming.default_feature_name(p["op"], len(ir.all_features()))
    body = _require_body(ir, p)
    f = FeatureSpec(
        id=fid,
        name=name,
        op=p["op"],
        profile_sketch=p.get("profile_sketch"),
        params=dict(p.get("params", {})),
        refs=list(p.get("refs", [])),
        suppress=p.get("suppress", False),
    )
    body.features.append(f)
    out.created_ids.append(fid)
    out.changes.append(f"add_feature '{name}' (op={f.op}, id={fid}) to body '{body.id}': {op.reason or '-'}")


def _op_update_feature(ir: IrDocument, op: IrPatchOp, out: PatchOutcome) -> None:
    f = ir.find_feature(op.target_id)
    if f is None:
        raise _reject(kind=ToolErrorKind.NOT_FOUND,
                        message=f"feature '{op.target_id}' not found",
                        feature_id=op.target_id)
    _merge_feature(f, op.payload)
    out.changes.append(f"update_feature '{f.name}' (id={f.id}): {op.reason or '-'}")


def _op_remove_feature(ir: IrDocument, op: IrPatchOp, out: PatchOutcome) -> None:
    victim = ir.find_feature(op.target_id)
    if victim is None:
        raise _reject(kind=ToolErrorKind.NOT_FOUND,
                        message=f"feature '{op.target_id}' not found",
                        feature_id=op.target_id)
    dependents = [f.id for f in ir.all_features() if op.target_id in f.refs]
    if dependents and not op.payload.get("cascade"):
        raise _reject(
            kind=ToolErrorKind.SEMANTIC,
            message=(f"cannot remove feature '{op.target_id}' ('{victim.name}'): "
                     f"it is referenced by {dependents}"),
            feature_id=op.target_id,
            hint="rewire or remove these dependents first, or pass cascade=true")
    for body in ir.bodies:
        body.features = [f for f in body.features if f.id != op.target_id]
    if op.payload.get("cascade"):
        for f in ir.all_features():
            if op.target_id in f.refs:
                f.refs = [r for r in f.refs if r != op.target_id]
        out.changes.append(
            f"remove_feature '{victim.name}' (id={op.target_id}) cascade-rewired "
            f"dependents {dependents}: {op.reason or '-'}")
    else:
        out.changes.append(
            f"remove_feature '{victim.name}' (id={op.target_id}): {op.reason or '-'}")


def _op_update_requirement(ir: IrDocument, op: IrPatchOp, out: PatchOutcome) -> None:
    req = ir.requirements
    if "raw_text" in op.payload:
        req.raw_text = op.payload["raw_text"]
    if "constraints" in op.payload:
        req.constraints = [c if isinstance(c, ConstraintExpr) else ConstraintExpr(**c)
                           for c in op.payload["constraints"]]
    if "constraints_append" in op.payload:
        req.constraints = list(req.constraints) + list(op.payload["constraints_append"])
    out.changes.append(f"update_requirement: {op.reason or '-'}")


def _op_rename(ir: IrDocument, op: IrPatchOp, out: PatchOutcome) -> None:
    new_name = op.payload.get("name")
    if not new_name:
        raise _reject(kind=ToolErrorKind.SEMANTIC,
                        message="rename requires a 'name'", feature_id=op.target_id)
    new_name = naming.slugify(new_name) or new_name
    sk = ir.find_sketch(op.target_id)
    f = ir.find_feature(op.target_id)
    if sk is None and f is None:
        raise _reject(kind=ToolErrorKind.NOT_FOUND,
                        message=f"no sketch or feature with id '{op.target_id}'",
                        feature_id=op.target_id)
    # uniqueness is document-wide (sketches + features share the name space)
    for s in ir.all_sketches():
        if s.id != op.target_id and s.name == new_name:
            raise _reject(
                kind=ToolErrorKind.SEMANTIC,
                message=(f"rename to '{new_name}' collides with sketch "
                         f"'{s.id}'"),
                feature_id=op.target_id)
    for other in ir.all_features():
        if other.id != op.target_id and other.name == new_name:
            raise _reject(
                kind=ToolErrorKind.SEMANTIC,
                message=(f"rename to '{new_name}' collides with feature "
                         f"'{other.id}'"),
                feature_id=op.target_id)
    if sk is not None:
        old = sk.name
        sk.name = new_name
        out.renamed_ids.append(sk.id)
        out.changes.append(f"rename sketch '{old}' -> '{new_name}' (id={sk.id})")
    else:
        old = f.name  # type: ignore[union-attr]
        f.name = new_name  # type: ignore[union-attr]
        out.renamed_ids.append(f.id)  # type: ignore[union-attr]
        out.changes.append(f"rename feature '{old}' -> '{new_name}' (id={f.id})")  # type: ignore[union-attr]


_HANDLERS = {
    "add_sketch": _op_add_sketch,
    "update_sketch": _op_update_sketch,
    "add_feature": _op_add_feature,
    "update_feature": _op_update_feature,
    "remove_feature": _op_remove_feature,
    "update_requirement": _op_update_requirement,
    "rename": _op_rename,
}


def apply_patch(ir: IrDocument, patch: IrPatch) -> PatchOutcome:
    """Apply ``patch`` to ``ir`` and return a new, version-bumped document.

    Raises :class:`ToolError` (``kind=SEMANTIC`` unless otherwise noted) on:
      * stale ``base_version`` (optimistic concurrency),
      * a target that does not exist (``NOT_FOUND``),
      * a forbidden dependent removal (named in the message),
      * a colliding ``rename``,
      * any ``error``-severity result from :func:`validate_ir` on the outcome.

    The input ``ir`` is never mutated.
    """
    if patch.base_version != ir.version:
        raise _reject(
            kind=ToolErrorKind.SEMANTIC,
            message=(f"stale base_version: patch wants {patch.base_version}, "
                     f"current is {ir.version}"),
            hint="re-fetch the current IR and rebase your ops before proposing")

    new_ir = ir.model_copy(deep=True)
    out = PatchOutcome(version=ir.version + 1, applied=0,
                       summary=patch.summary or "ir patch")

    for op in patch.ops:
        handler = _HANDLERS.get(op.op)
        if handler is None:  # pragma: no cover - Literal guards this
            raise _reject(kind=ToolErrorKind.SEMANTIC,
                            message=f"unknown patch op '{op.op}'")
        handler(new_ir, op, out)
        out.applied += 1

    # final semantic gate — the IR is the single source of truth
    errors = [i for i in validate_ir(new_ir) if i.severity == "error"]
    if errors:
        joined = "; ".join(f"[{i.code}] {i.message}" for i in errors)
        raise _reject(
            kind=ToolErrorKind.SEMANTIC,
            message=f"patch produces an invalid IR: {joined}",
            hint="fix the reported issues and re-propose the patch")

    new_ir.version = out.version
    out.ir = new_ir
    return out
