"""Per-operation field scoping for ``ir_patch``'s feature branches.

Before this, one successful ``ir_help(topic=feature, feature_op=loft)`` unlocked
``add_feature``/``update_feature`` with the *complete* FeatureSpec payload —
every field of every operation, tens of kilobytes the model must read before it
can write eight fields of a loft. A smaller model pays that cost in attention,
not just tokens.

The scoping rules live here, derived from the same tables the validator uses
(``tcad/ir/validate``), so a field cannot be advertised for an operation that
would refuse it. When an operation's parameter bag is verified, the scoped
schema lists exactly those keys; otherwise the bag stays generic on purpose —
under-promising is the safe direction.

Scope is a *presentation* concern only: the server-side checks still run against
the complete declaration, and the IR validator still refuses anything the
compiler cannot honour.
"""

from __future__ import annotations

import copy

from tcad.ir.validate import (
    _PLACEMENT_OPS,
    _PLANE_OPS,
    _SUB_ELEMENT_OPS,
    _VERIFIED_OP_PARAMS,
)

#: Operations whose feature reads a profile sketch (ValueError if wrong).
_PROFILE_OPS = frozenset({
    "pad", "pocket", "revolution", "groove", "hole",
    "additive_loft", "subtractive_loft",
})
#: Operations whose ``refs`` names the features they repeat (semantic, not just
#: build order — build-order-only refs are dropped from a scoped schema).
_REF_OPS = frozenset({
    "mirrored", "linear_pattern", "polar_pattern", "circular_pattern",
    "multi_transform",
})

#: How many scoped operations may stay unlocked at once. Each branch is small,
#: but the cap keeps a help-happy model from rebuilding the full table.
MAX_SCOPED_OPS = 4


def scoped_fields(op: str) -> set[str]:
    """Top-level payload fields ``op`` actually reads (plus the identity set)."""
    fields = {"id", "name", "body_id", "op", "params", "after_feature"}
    if op in _PROFILE_OPS:
        fields.add("profile_sketch")
    if op in {"additive_loft", "subtractive_loft"}:
        fields.add("sections")
    if op in _SUB_ELEMENT_OPS:
        fields |= {"base_feature", "sub_elements"}
    if op in _PLANE_OPS:
        fields.add("plane")
    if op in _PLACEMENT_OPS:
        fields.add("placement")
    if op in _REF_OPS:
        fields.add("refs")
    return fields


def scoped_params(op: str) -> frozenset[str] | None:
    """The verified parameter keys for ``op``, or None for a permissive bag."""
    return _VERIFIED_OP_PARAMS.get(op)


def scope_payload_multi(payload: dict, ops: list[str]) -> dict:
    """Narrow one payload schema to the UNION of ``ops``' fields.

    One branch per action (add/update) carrying every scoped operation, rather
    than one branch per operation: the per-operation precision moves into the
    parameter bag's description ("which keys belong to which op"), and the
    branch boilerplate is paid once. For a single scoped op this is exactly
    the per-op field set.
    """
    scoped = copy.deepcopy(payload)
    properties = scoped.get("properties") or {}
    allowed = set().union(*(scoped_fields(op) for op in ops))
    keep = set(allowed) | {"params_remove"}
    if "refs" in allowed:
        keep.add("refs_append")
    properties = {name: schema for name, schema in properties.items() if name in keep}
    if "op" in properties:
        properties["op"] = {"type": "string", "enum": list(ops)}
    tables = {op: scoped_params(op) for op in ops}
    if "params" in properties and all(table is not None for table in tables.values()):
        union = set().union(*tables.values()) if tables else set()
        listing = "; ".join(
            f"{op}: " + (", ".join(sorted(table)) if table else "(no parameters)")
            for op, table in tables.items())
        properties["params"] = {
            "type": "object", "additionalProperties": True,
            "description": "Allowed keys per operation — " + listing
            + ". Unknown keys are refused before anything is persisted.",
            "properties": {key: {} for key in sorted(union)},
        }
    scoped["properties"] = properties
    scoped["required"] = [name for name in scoped.get("required") or []
                          if name in keep and name != "params"]
    return scoped


def scope_branches(branches: list[dict], ops: list[str]) -> list[dict]:
    """Replace the add_feature/update_feature branches with scoped ones.

    Body-level branches (add_body, rename, …) pass through untouched; a branch
    whose op enum is not add_feature/update_feature is left alone. The scoped
    feature branches keep their action (add/update) and carry only the fields
    of the operations whose help was actually read.

    Scoping is an optimization, and it is measured as one: when the scoped
    variant would not actually be smaller (a union of many operations with
    large parameter tables can approach the full table), the complete branches
    are returned unchanged rather than spending the same attention on a
    narrower view.
    """
    import json

    scoped_ops = [op for op in dict.fromkeys(ops)][-MAX_SCOPED_OPS:]
    result: list[dict] = []
    replaced = False
    for branch in branches:
        names = branch["properties"]["op"]["enum"]
        if names[0] not in ("add_feature", "update_feature"):
            result.append(branch)
            continue
        variant = copy.deepcopy(branch)
        # The BRANCH op stays add_feature/update_feature (that is the patch
        # operation); the payload's op enum names the scoped feature ops.
        variant["properties"]["payload"] = scope_payload_multi(
            branch["properties"]["payload"], scoped_ops)
        result.append(variant)
        replaced = True
    if not replaced:
        return branches
    if sum(len(json.dumps(b)) for b in result) >= sum(len(json.dumps(b)) for b in branches):
        return branches
    return result
