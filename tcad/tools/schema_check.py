"""Server-side enforcement of the tool argument schemas (task book §5-A).

The ``params_schema`` on every ``ToolSpec`` was sent to the model as part of the
function declaration and then **never checked**. So ``geo_view {"views": "iso"}``
— a string where an array belongs — reached the handler, where ``views`` is truthy
and got passed to the worker as-is. The declaration was documentation, not a
contract.

This module turns it into a contract using only the standard library. The
supported keyword set is deliberately small and closed: :data:`SUPPORTED`
lists it, and a unit test walks every registered tool's schema asserting that no
keyword outside that set appears. A schema can therefore not start using a
constraint that nothing enforces.

What it does *not* do: full JSON Schema. ``$ref`` resolves against ``$defs``;
``anyOf``/``oneOf`` pass when at least one branch passes. Deep, op-specific
validation stays where it already lives (``IrPatch.model_validate`` inside
``ir_patch``), because that is a typed model and this is a shape check.
"""

from __future__ import annotations

from typing import Any
import math
import re

#: Keywords this checker understands.
SUPPORTED: frozenset[str] = frozenset({
    "type", "required", "properties", "items", "enum",
    "minLength", "maxLength", "minItems", "maxItems",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "pattern", "const",
    "anyOf", "oneOf", "$ref", "$defs", "additionalProperties",
    # annotations: carried in the schema, no constraint to enforce
    "title", "description", "default",
})

#: Annotations only — present in the schemas, deliberately not enforced.
ANNOTATIONS: frozenset[str] = frozenset({"title", "description", "default"})

_TYPE_NAMES = ("object", "array", "string", "integer", "number", "boolean", "null")


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    # An unknown type name is a schema we cannot enforce; refusing to guess is
    # safer than passing everything (or nothing) silently.
    return False


def _describe(value: Any, limit: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _resolve_ref(ref: str, root: dict) -> dict | None:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    node: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, dict) else None


def _compatible_type(value: Any, schema: dict, root: dict) -> bool:
    if '$ref' in schema:
        schema = _resolve_ref(schema['$ref'], root) or {}
    expected = schema.get('type')
    if expected is not None:
        names = [expected] if isinstance(expected, str) else expected
        return any(_type_ok(value, name) for name in names)
    branches = schema.get('anyOf') or schema.get('oneOf')
    return any(_compatible_type(value, branch, root) for branch in branches) if branches else True


def check(args: Any, schema: dict, *, path: str = "arguments", root: dict | None = None) -> list[str]:
    """Return a list of human-readable problems; empty means the args conform."""
    if not isinstance(schema, dict):
        return []
    root = root if root is not None else schema
    problems: list[str] = []

    ref = schema.get("$ref")
    if ref is not None:
        target = _resolve_ref(ref, root)
        if target is None:
            return [f"{path}: schema references {ref!r}, which is not in $defs"]
        return check(args, target, path=path, root=root)

    for branch_key in ("anyOf", "oneOf"):
        branches = schema.get(branch_key)
        if isinstance(branches, list) and branches:
            if isinstance(args, dict):
                for discriminator in ('op','shape'):
                    if discriminator not in args: continue
                    matching = [b for b in branches if args[discriminator] in b.get('properties', {}).get(discriminator, {}).get('enum', [])]
                    if len(matching) == 1:
                        return check(args, matching[0], path=path, root=root)
                    if not matching:
                        # A value that matches NO branch must answer with the FULL
                        # legal set. Reporting the "closest" branch's single enum
                        # value instead told a live model that `['add_body']` was
                        # the whole operation set — it nearly abandoned legal
                        # edits (set_assembly/remove_body) it had already used.
                        allowed = sorted({value for branch in branches
                                          for value in branch.get('properties', {}).get(discriminator, {}).get('enum', [])})
                        if allowed:
                            return [f"{path}: {_describe(args[discriminator])} is not one of {allowed}"]
            attempt = [check(args, b, path=path, root=root) for b in branches]
            if not any(not a for a in attempt):
                # Prefer the value's actual type, then the fewest problems.
                # A nullable object's one-error null branch otherwise masks
                # the nested fields that the caller actually needs to fix.
                compatible = [errors for branch, errors in zip(branches, attempt)
                              if _compatible_type(args, branch, root)]
                best = min(compatible or attempt, key=len)
                problems.append(
                    f"{path}: matches none of the {branch_key} alternatives "
                    f"(closest: {'; '.join(best)})"
                )
            return problems

    expected = schema.get("type")
    if expected is not None:
        names = [expected] if isinstance(expected, str) else list(expected)
        if not any(_type_ok(args, n) for n in names):
            return [f"{path}: expected {'/'.join(names)}, got {type(args).__name__} "
                    f"({_describe(args)})"]

    enum = schema.get("enum")
    if isinstance(enum, list) and args not in enum:
        return [f"{path}: {_describe(args)} is not one of {enum}"]
    if "const" in schema and args != schema['const']:
        return [f"{path}: must equal {schema['const']!r}"]
    if isinstance(args, str) and 'pattern' in schema and re.search(schema['pattern'], args) is None:
        problems.append(f"{path}: does not match pattern {schema['pattern']}")

    if isinstance(args, (int, float)) and not isinstance(args, bool):
        if not math.isfinite(args):
            return [f"{path}: number must be finite"]
        if "minimum" in schema and args < schema["minimum"]:
            problems.append(f"{path}: {args} is below minimum={schema['minimum']}")
        if "maximum" in schema and args > schema["maximum"]:
            problems.append(f"{path}: {args} exceeds maximum={schema['maximum']}")
        if "exclusiveMinimum" in schema and args <= schema['exclusiveMinimum']:
            problems.append(f"{path}: must exceed {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and args >= schema['exclusiveMaximum']:
            problems.append(f"{path}: must be below {schema['exclusiveMaximum']}")

    bounds = (("minLength", "maxLength") if isinstance(args, str) else
              ("minItems", "maxItems") if isinstance(args, list) else None)
    if bounds:
        minimum, maximum = bounds
        if minimum in schema and len(args) < schema[minimum]:
            problems.append(f"{path}: length {len(args)} is below {minimum}={schema[minimum]}")
        if maximum in schema and len(args) > schema[maximum]:
            problems.append(f"{path}: length {len(args)} exceeds {maximum}={schema[maximum]}")

    if isinstance(args, dict):
        for name in schema.get("required") or []:
            if name not in args:
                problems.append(f"{path}: missing required property {name!r}")
        props = schema.get("properties") or {}
        extra = schema.get("additionalProperties")
        for key, value in args.items():
            if key in props:
                problems.extend(check(value, props[key], path=f"{path}.{key}", root=root))
            elif extra is False:
                problems.append(
                    f"{path}: unknown property {key!r} "
                    f"(allowed: {sorted(props) or 'none'})")

    if isinstance(args, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, value in enumerate(args):
                problems.extend(check(value, items, path=f"{path}[{i}]", root=root))

    return problems


def validate_tool_args(args: Any, spec: Any) -> list[str]:
    """Problems with ``args`` against a ``ToolSpec``'s declared schema."""
    schema = getattr(spec, "params_schema", None) or {}
    return check(args if args is not None else {}, schema)
