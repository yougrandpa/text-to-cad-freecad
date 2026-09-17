"""Stable naming contract for IR sketches and features.

WHY THIS EXISTS
---------------
Multi-turn requests like ``"move that hole 5 mm right"`` can only resolve if the
features and sketches the model is talking about carry *stable, meaningful
names*. The model never addresses a FreeCAD object directly; it speaks through
the IR, and the human-facing handle it uses across turns is ``name`` (not the
internal ``id``). ``rename`` only ever touches ``name`` for exactly this reason.

THE HONEST LIMIT (design §12-9)
--------------------------------
A thing that was never named — or two things that share a name — cannot be
disambiguated by reference resolution. We therefore:
  * always mint a default ``name`` when the model omits one, and
  * refuse ``rename`` when the new name would collide with another entity.
An unnamed / ambiguously-named thing is, by construction, unreferenceable. We do
not pretend otherwise.

This module is dependency-light on purpose: it is imported by the IR patch path
and must not pull in pydantic or anything heavier.
"""

from __future__ import annotations

import re
from typing import Iterable

_INVALID = re.compile(r"[^0-9a-zA-Z_]+")


def slugify(text: str) -> str:
    """Lowercase, replace every run of non ``[0-9a-zA-Z_]`` chars with ``_``.

    Empty / whitespace-only input collapses to ``""`` so callers can fall back
    to a constant.
    """
    s = (text or "").strip().lower()
    s = _INVALID.sub("_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def unique_name(existing: Iterable[str], base: str) -> str:
    """Return ``base`` if free, else ``base_2``, ``base_3``, ... (never collides).

    ``existing`` is the set of names already in use; it is treated read-only.
    """
    base = slugify(base) or "item"
    if base not in existing:
        return base
    i = 2
    while f"{base}_{i}" in existing:
        i += 1
    return f"{base}_{i}"


def default_feature_name(op: str, index: int) -> str:
    """Propose a human-readable default name for a feature.

    ``op`` is the IR feature op (e.g. ``"pad"``); ``index`` is its 0-based
    position among the body's features. Result: ``"pad_1"``, ``"hole_2"``, ...
    """
    stem = slugify(op) or "feature"
    return f"{stem}_{index + 1}"


def default_sketch_name(base: str | None, index: int) -> str:
    """Propose a default name for a sketch.

    ``base`` is an optional semantic hint (e.g. ``"outline"``); ``index`` is the
    0-based position among the body's sketches. Result: ``"outline_1"``,
    ``"sketch_2"``, ...
    """
    stem = slugify(base) if base else "sketch"
    return f"{stem}_{index + 1}"
