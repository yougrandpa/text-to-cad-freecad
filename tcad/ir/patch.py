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

import difflib
from typing import NoReturn

from pydantic import BaseModel, ValidationError

from tcad.core.types import ToolError, ToolErrorKind
from tcad.ir import naming
from tcad.ir.schema import (
    BodySpec,
    ConstraintExpr,
    FeatureSpec,
    IrDocument,
    IrPatch,
    IrPatchOp,
    PlacementSpec,
    PlaneRef,
    SketchConstraint,
    SketchGeom,
    SketchSpec,
    Vec3,
)
from tcad.ir.validate import validate_ir


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


def _brief(exc: ValidationError, limit: int = 3) -> str:
    """A short, actionable rendering of a pydantic error.

    The default ``str(exc)`` is a multi-line dump with a documentation URL; it
    tells the model *that* something was wrong and not *which field*. This keeps
    the field path and the reason.
    """
    errors = exc.errors()
    parts = []
    for e in errors[:limit]:
        loc = ".".join(str(p) for p in (e.get("loc") or ()))
        parts.append(f"{loc or '<root>'}: {e.get('msg')}")
    extra = len(errors) - limit
    if extra > 0:
        parts.append(f"(+{extra} more)")
    return "; ".join(parts)


def _require_list(value: object, *, field: str) -> list:
    """A list field must actually be a list.

    ``list("ft_pad")`` is ``['f','t','_','p','a','d']`` — a string would be
    silently *exploded into references* instead of rejected, and the build would
    fail much later for a reason unrelated to what the model wrote.
    """
    if not isinstance(value, list):
        raise _reject(
            kind=ToolErrorKind.SCHEMA,
            message=f"{field} must be a JSON array, got {type(value).__name__}",
            hint=f'write {field} as a list, e.g. {field.replace("_append", "")}: ["id1", "id2"]',
        )
    return value


def _require_mapping(value: object, *, field: str) -> dict:
    """An object field must actually be an object.

    ``dict("abc")`` and ``dict(5)`` raise a bare ``ValueError``/``TypeError`` —
    a crash the model cannot act on. This turns the same input into a schema
    error naming the field.
    """
    if not isinstance(value, dict):
        raise _reject(
            kind=ToolErrorKind.SCHEMA,
            message=f"{field} must be a JSON object, got {type(value).__name__}",
            hint=f"write {field} as an object, e.g. {{{field}: {{...}}}}",
        )
    return value


#: The payload keys each patch op actually reads.
#:
#: Before this table, the payload was an untyped bag: ``_op_add_feature`` read
#: the keys it knew about and *silently ignored the rest*, so
#: ``{"op": "pad", "parms": {...}}`` produced a feature with no parameters at
#: all — a document that is type-valid, passes ``validate_ir``, and is not what
#: the model asked for. The failure only surfaced later, at compile time, as a
#: message about a missing profile rather than about the typo that caused it.
#: Same for ``"profiles_sketch"``, ``"params"`` misspelled, or any other slip.
#:
#: Derived from the declared model fields (plus the explicit ``*_append``
#: affordances) so a field added to a spec is immediately accepted here — a
#: handwritten list would start refusing valid payloads the day a field is added,
#: which is the failure mode that makes people delete whitelists.
_PAYLOAD_FIELDS: dict[str, frozenset[str]] = {
    "add_body": frozenset({"id", "name", "motion"}),
    "update_body": frozenset({"name", "motion"}),
    "add_sketch": frozenset(SketchSpec.model_fields) | {"body_id", "geometry_append", "constraints_append"},
    "update_sketch": frozenset(SketchSpec.model_fields) | {"geometry_append", "constraints_append"},
    "add_feature": frozenset(FeatureSpec.model_fields) | {"body_id"},
    "update_feature": frozenset(FeatureSpec.model_fields) | {"refs_append"},
    "remove_feature": frozenset({"cascade"}),
    "update_requirement": frozenset({"raw_text", "constraints", "constraints_append"}),
    "rename": frozenset({"name"}),
}


def _reject_unknown_payload_keys(op_name: str, payload: dict) -> None:
    """Refuse a payload key the op does not read, naming the likely intent.

    Silently dropping is the one behaviour that must not happen here: the model
    cannot tell "the key was wrong" from "the key was right", so it repeats the
    same mistake with the same confidence. The hint uses a close-match so a
    near-miss is answered with the field it meant.
    """
    allowed = _PAYLOAD_FIELDS.get(op_name)
    if allowed is None or not isinstance(payload, dict):
        return
    unknown = [k for k in payload if k not in allowed]
    if not unknown:
        return
    suggestions = []
    for key in unknown:
        close = difflib.get_close_matches(str(key), sorted(allowed), n=1, cutoff=0.6)
        suggestions.append(f"{key!r}" + (f" (did you mean {close[0]!r}?)" if close else ""))
    raise _reject(
        kind=ToolErrorKind.SCHEMA,
        message=(f"op '{op_name}' does not accept payload key(s) "
                 f"{', '.join(suggestions)}; it accepts "
                 f"{', '.join(sorted(allowed))}. Ignoring an unknown key would "
                 f"build something you did not ask for."),
        hint="re-propose the op with only the accepted keys",
    )


def _require_str_list(value: object, *, field: str) -> list[str]:
    """A list of id/sub-element names, refused if it is a bare string.

    ``list("Edge1")`` is ``['E','d','g','e','1']`` — five names that exist
    nowhere, in a field that type-checks. Same class of mistake as ``refs``.
    """
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise _reject(
            kind=ToolErrorKind.SCHEMA,
            message=f"{field} must be a list of strings, got {type(value).__name__}",
            hint=f'write it as a JSON array, e.g. "{field}": ["Edge1", "Edge3"]',
        )
    out = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise _reject(
                kind=ToolErrorKind.SCHEMA,
                message=f"{field}[{i}] must be a string, got {type(item).__name__}",
            )
        out.append(item)
    return out


def _require_placement(value: object) -> PlacementSpec | None:
    """A feature payload's ``placement``: absent, an object, or ``None``.

    The field is a typed model and the payload is JSON, so the conversion has to
    happen here — the same reason sketches convert ``plane``/``offset`` rather
    than assigning the dict. An unparsable value must come back as a schema
    error naming the field instead of a pydantic dump or, worse, a raw dict that
    rides along until the worker cannot make sense of it.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _reject(
            kind=ToolErrorKind.SCHEMA,
            message=f"placement must be an object, got {type(value).__name__}",
            hint='write placement as {"position": {"x": .., "y": .., "z": ..}}',
        )
    try:
        return PlacementSpec(**value)
    except ValidationError as exc:
        raise _reject(
            kind=ToolErrorKind.SCHEMA,
            message=f"placement does not match its schema: {_brief(exc)}",
            hint='position/axis take {"x": .., "y": .., "z": ..}; angle is degrees',
        ) from exc


def _revalidate(doc: IrDocument) -> IrDocument:
    """Re-parse the mutated document through its declared types.

    The merge helpers assign one field at a time, and pydantic v2 does **not**
    validate on assignment by default. So ``update_sketch {"reversed": "false"}``
    left the *string* ``"false"`` in a ``bool`` field — and ``"false"`` is truthy,
    so the compiler would do the opposite of what was asked with no error
    anywhere. Re-parsing makes every field pass its declared type again, so what
    gets stored is what the schema says it is.
    """
    import warnings

    try:
        with warnings.catch_warnings():
            # Serialising a wrong-typed attribute warns; the ValidationError
            # below is the actual report, so the warning is noise.
            warnings.simplefilter("ignore")
            raw = doc.model_dump()
        return IrDocument.model_validate(raw)
    except ValidationError as exc:
        raise _reject(
            kind=ToolErrorKind.SCHEMA,
            message=f"patch does not match the IR schema: {_brief(exc)}",
            hint="fix the named field(s) and re-propose the patch",
        ) from exc


def _merge_sketch(sk: SketchSpec, payload: dict) -> None:
    for k, v in payload.items():
        if k == "geometry_append":
            _require_list(v, field="geometry_append")
            sk.geometry = list(sk.geometry) + [SketchGeom(**g) for g in v]
        elif k == "constraints_append":
            _require_list(v, field="constraints_append")
            sk.constraints = list(sk.constraints) + [SketchConstraint(**c) for c in v]
        elif k == "geometry":
            _require_list(v, field="geometry")
            sk.geometry = [SketchGeom(**g) for g in v]
        elif k == "constraints":
            _require_list(v, field="constraints")
            sk.constraints = [SketchConstraint(**c) for c in v]
        elif k == "plane":
            if not isinstance(v, dict):
                raise _reject(kind=ToolErrorKind.SCHEMA,
                              message=f"plane must be an object, got {type(v).__name__}")
            setattr(sk, k, PlaneRef(**v))
        elif k == "offset":
            setattr(sk, k, Vec3(**v) if v is not None else None)
        elif k not in ("id",):
            setattr(sk, k, v)


def _merge_feature(f: FeatureSpec, payload: dict) -> None:
    for k, v in payload.items():
        if k == "refs_append":
            _require_list(v, field="refs_append")
            f.refs = list(f.refs) + _require_str_list(v, field="refs_append")
        elif k == "refs":
            _require_list(v, field="refs")
            f.refs = _require_str_list(v, field="refs")
        elif k == "params":
            if not isinstance(v, dict):
                raise _reject(kind=ToolErrorKind.SCHEMA,
                              message=f"params must be an object, got {type(v).__name__}")
            f.params = {**f.params, **v}
        elif k == "sub_elements":
            f.sub_elements = _require_str_list(v, field="sub_elements")
        elif k == "placement":
            # Same reason as ``plane`` on a sketch: the payload arrives as a raw
            # dict and the field is a typed model. ``None`` clears it, sending
            # the feature back to the origin.
            f.placement = _require_placement(v)
        elif k == "plane":
            # ``MirrorPlane`` / ``NeutralPlane`` are plane references, not
            # scalars, so the payload's object has to become a ``PlaneRef``
            # here. Assigning the raw dict would type-check at the attribute
            # and only be sorted out later by the whole-document re-parse.
            if v is None:
                f.plane = None
            elif not isinstance(v, dict):
                raise _reject(kind=ToolErrorKind.SCHEMA,
                              message=f"plane must be an object, got {type(v).__name__}")
            else:
                f.plane = PlaneRef(**v)
        elif k == "base_feature":
            if v is not None and not isinstance(v, str):
                raise _reject(kind=ToolErrorKind.SCHEMA,
                              message=f"base_feature must be a feature id string, "
                                      f"got {type(v).__name__}")
            f.base_feature = v
        elif k not in ("id",):
            setattr(f, k, v)


# ── op handlers ───────────────────────────────────────────────────────────────

def _op_add_body(ir: IrDocument, op: IrPatchOp, out: PatchOutcome) -> None:
    _reject_unknown_payload_keys("add_body", op.payload)
    ids = {b.id for b in ir.bodies} | set.union(*_all_ids(ir))
    bid = op.payload.get("id") or naming.unique_name(ids, "body")
    if bid in ids:
        _reject(ToolErrorKind.SEMANTIC, f"body id '{bid}' already exists")
    body = BodySpec(id=bid, name=op.payload.get("name") or bid,
                    motion=op.payload.get("motion"))
    ir.bodies.append(body)
    out.created_ids.append(bid)
    out.changes.append(f"add_body '{body.name}' (id={bid}): {op.reason or '-'}")


def _op_update_body(ir: IrDocument, op: IrPatchOp, out: PatchOutcome) -> None:
    _reject_unknown_payload_keys("update_body", op.payload)
    body = next((b for b in ir.bodies if b.id == op.target_id), None)
    if body is None:
        _reject(ToolErrorKind.NOT_FOUND, f"body '{op.target_id}' not found")
    updated = BodySpec.model_validate({**body.model_dump(), **op.payload})
    ir.bodies[ir.bodies.index(body)] = updated
    out.changes.append(f"update_body '{body.id}': {op.reason or '-'}")


def _op_add_sketch(ir: IrDocument, op: IrPatchOp, out: PatchOutcome) -> None:
    p = op.payload
    sk_ids, _ = _all_ids(ir)
    sid = p.get("id") or naming.unique_name(
        sk_ids, naming.default_sketch_name(p.get("name"), len(ir.all_sketches())))
    _reject_unknown_payload_keys("add_sketch", p)
    if sid in sk_ids:
        raise _reject(kind=ToolErrorKind.SEMANTIC,
                        message=f"sketch id '{sid}' already exists",
                        feature_id=sid)
    name = p.get("name") or naming.default_sketch_name(None, len(ir.all_sketches()))
    body = _require_body(ir, p)
    geometry = _require_list(p.get("geometry", []), field="geometry")
    constraints = _require_list(p.get("constraints", []), field="constraints")
    for i, g in enumerate(geometry):
        _require_mapping(g, field=f"geometry[{i}]")
    for i, c in enumerate(constraints):
        _require_mapping(c, field=f"constraints[{i}]")
    sk = SketchSpec(
        id=sid,
        name=name,
        plane=(PlaneRef(**_require_mapping(p["plane"], field="plane"))
               if "plane" in p else PlaneRef(kind="origin_plane", plane="XY")),
        map_mode=p.get("map_mode", "FlatFace"),
        reversed=p.get("reversed", False),
        offset=(Vec3(**_require_mapping(p["offset"], field="offset"))
                if p.get("offset") else None),
        geometry=[SketchGeom(**g) for g in geometry],
        constraints=[SketchConstraint(**c) for c in constraints],
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
    _reject_unknown_payload_keys("update_sketch", op.payload)
    _merge_sketch(sk, op.payload)
    out.changes.append(f"update_sketch '{sk.name}' (id={sk.id}): {op.reason or '-'}")


def _op_add_feature(ir: IrDocument, op: IrPatchOp, out: PatchOutcome) -> None:
    p = op.payload
    _reject_unknown_payload_keys("add_feature", p)
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
        refs=_require_str_list(p.get("refs", []), field="refs"),
        suppress=p.get("suppress", False),
    )
    # The reference fields go through the SAME merge helper `update_feature`
    # uses, on purpose. They used to be dropped here: `base_feature`,
    # `sub_elements` and `plane` were simply never read, so `fillet`, `chamfer`,
    # `mirrored`, `draft` and `thickness` could not be created in one op at all —
    # and the rejection that followed told the model to "set base_feature and
    # sub_elements", which it had just done. Sharing one mapping means the two
    # ops cannot drift apart again.
    _merge_feature(f, {k: v for k, v in p.items() if k != "body_id"})
    body.features.append(f)
    out.created_ids.append(fid)
    out.changes.append(f"add_feature '{name}' (op={f.op}, id={fid}) to body '{body.id}': {op.reason or '-'}")


def _op_update_feature(ir: IrDocument, op: IrPatchOp, out: PatchOutcome) -> None:
    f = ir.find_feature(op.target_id)
    if f is None:
        raise _reject(kind=ToolErrorKind.NOT_FOUND,
                        message=f"feature '{op.target_id}' not found",
                        feature_id=op.target_id)
    _reject_unknown_payload_keys("update_feature", op.payload)
    _merge_feature(f, op.payload)
    out.changes.append(f"update_feature '{f.name}' (id={f.id}): {op.reason or '-'}")


def _op_remove_feature(ir: IrDocument, op: IrPatchOp, out: PatchOutcome) -> None:
    victim = ir.find_feature(op.target_id)
    if victim is None:
        raise _reject(kind=ToolErrorKind.NOT_FOUND,
                        message=f"feature '{op.target_id}' not found",
                        feature_id=op.target_id)
    _reject_unknown_payload_keys("remove_feature", op.payload)
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
    _reject_unknown_payload_keys("update_requirement", op.payload)
    if "raw_text" in op.payload:
        req.raw_text = op.payload["raw_text"]
    if "constraints" in op.payload:
        req.constraints = [c if isinstance(c, ConstraintExpr) else ConstraintExpr(**c)
                           for c in op.payload["constraints"]]
    if "constraints_append" in op.payload:
        # Same coercion as the replace branch: a raw dict must never land in the
        # typed model. ConstraintExpr(**c) validates the shape; a ValidationError
        # here is turned into a clean PatchError so the model gets an actionable
        # message instead of a pydantic traceback.
        appended: list[ConstraintExpr] = []
        for i, c in enumerate(op.payload["constraints_append"]):
            if isinstance(c, ConstraintExpr):
                appended.append(c)
                continue
            try:
                appended.append(ConstraintExpr(**c))
            except Exception as exc:
                raise _reject(
                    kind=ToolErrorKind.SEMANTIC,
                    message=(f"constraints_append[{i}] is not a valid constraint: "
                             f"{type(exc).__name__}: {exc}"),
                    hint="each appended constraint needs kind/target/value/tol "
                         "with the documented shapes (see ir_patch schema)")
        req.constraints = list(req.constraints) + appended
    out.changes.append(f"update_requirement: {op.reason or '-'}")


def _op_rename(ir: IrDocument, op: IrPatchOp, out: PatchOutcome) -> None:
    _reject_unknown_payload_keys("rename", op.payload)
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
    "add_body": _op_add_body,
    "update_body": _op_update_body,
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
        try:
            handler(new_ir, op, out)
        except ValidationError as exc:
            # A payload that does not fit its typed model is a schema error the
            # model can fix, not a crash — and it must name the field.
            raise _reject(
                kind=ToolErrorKind.SCHEMA,
                message=f"op '{op.op}' payload does not match the IR schema: {_brief(exc)}",
                feature_id=op.target_id,
                hint="fix the named field(s) and re-propose the op",
            ) from exc
        out.applied += 1

    # Re-parse through the declared types before anything else looks at it. The
    # merge helpers assign field-by-field, and pydantic does not validate on
    # assignment — see _revalidate.
    new_ir = _revalidate(new_ir)

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
