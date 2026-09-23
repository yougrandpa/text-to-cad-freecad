"""The requirement contract, rendered for the model (design §4.6, task book §4).

The requirement contract is the machine-checkable half of what the user asked
for. It lives in ``IrDocument.requirements`` and is persisted with the IR
snapshot, so it is *independent of the generated geometry*: nothing here is
derived from a shape the compiler produced.

Why the model needs to see it
-----------------------------
Two failure modes this block exists to prevent:

  * **Silent requirement drift.** The Gate only judges ``confirmed=True``
    expressions. If the model never records the numbers the user gave, the Gate
    can only report that the geometry is self-consistent — see
    ``checks_spec.RequirementCoverageCheck``. Showing the contract every step
    makes "what am I being graded against?" visible instead of implicit.
  * **Inference masquerading as fact.** A dimension the model guessed is not the
    same as a dimension the user stated. ``confirmed`` is the difference, and it
    is printed next to every entry — including the verbatim ``source_text`` when
    one was recorded.

The rendering is deterministic and program-generated; it never calls an LLM.
"""

from __future__ import annotations

from typing import Any

from tcad.ir.schema import IrDocument


def _fmt_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (int, str)):
        return str(value)
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}={_fmt_value(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fmt_value(v) for v in value) + "]"
    return str(value)


def _constraint_line(index: int, expr: Any) -> str:
    """One requirement, with its provenance and whether it may block."""
    kind = getattr(expr, "kind", "?")
    target = getattr(expr, "target", None)
    value = getattr(expr, "value", None)
    tol = getattr(expr, "tol", None)
    source = (getattr(expr, "source_text", "") or "").strip()
    confirmed = bool(getattr(expr, "confirmed", False))

    parts = [f"[{index}] {kind}"]
    if target:
        parts.append(f"target={target}")
    if value is not None:
        parts.append(f"value={_fmt_value(value)}")
    if tol is not None:
        parts.append(f"tol={_fmt_value(tol)}")
    status = "CONFIRMED — may block the build" if confirmed else "UNCONFIRMED — advisory only, cannot block"
    parts.append(status)
    line = "  " + "  ".join(parts)
    if source:
        # Verbatim, so "the user said 8" can be told apart from "the model guessed 8".
        line += f'\n      source: "{source}"'
    return line


def render_requirements_text(ir: IrDocument) -> str:
    """Deterministic plain-text projection of ``ir.requirements``.

    Always emits the block, including when it is empty: "there is no contract"
    is itself information the model needs before it decides it is done.
    """
    requirements = getattr(ir, "requirements", None)
    raw_text = (getattr(requirements, "raw_text", "") or "").strip()
    constraints = list(getattr(requirements, "constraints", None) or [])

    out: list[str] = [
        "# Requirement contract (independent of the generated geometry)",
    ]
    if raw_text:
        out.append(f'user_request (verbatim): "{raw_text}"')
    else:
        out.append("user_request: (none recorded)")

    if constraints:
        confirmed = sum(1 for c in constraints if getattr(c, "confirmed", False))
        out.append(
            f"constraints ({len(constraints)} recorded, {confirmed} confirmed):"
        )
        out.extend(_constraint_line(i, c) for i, c in enumerate(constraints))
    else:
        out.append(
            "constraints: NONE. The Gate has nothing to judge the part against, so a"
            " green result would only prove the geometry is self-consistent. If the"
            ' user named any size, count or position, record it with ir_patch'
            ' op=update_requirement and "confirmed": true.'
        )
    return "\n".join(out)
