"""The shipped session scripts must not encode a way of building that the IR
layer refuses.

``tools/sessions/*.json`` are the offline demo scripts (``stub_llm.py --script``,
``agent_driver.py --calls``). They are not covered by the behavioural tests, so
nothing noticed when the recipe they used — "model the profile around the
sketch's OWN origin, then place it with the sketch offset" — turned out to
deform the part (measured: a 40x20 rectangle padded 5 gives 4000 at offset
(0,0,0) and 2500 at (10,10,0), same bounding box).

A script that cannot be applied is a broken demo; a script that *can* be applied
and builds the wrong shape is a broken demo that looks like it worked. This
guard keeps them on the first side of that line.

Deterministic, no FreeCAD.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SESSIONS = REPO_ROOT / "tools" / "sessions"


def _iter_dicts(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_dicts(value)


def _session_files() -> list[Path]:
    return sorted(SESSIONS.glob("*.json"))


def test_there_are_session_scripts_to_check():
    """A guard over an empty glob guards nothing."""
    assert _session_files(), f"no session scripts under {SESSIONS}"


@pytest.mark.parametrize("path", _session_files(), ids=lambda p: p.name)
def test_no_session_script_uses_a_sketch_offset(path: Path):
    offenders = []
    for node in _iter_dicts(json.loads(path.read_text())):
        if node.get("op") not in ("add_sketch", "update_sketch"):
            continue
        payload = node.get("payload") or {}
        off = payload.get("offset")
        if not off:
            continue
        if any(abs(float(off.get(k, 0.0))) > 1e-9 for k in ("x", "y", "z")):
            offenders.append((node.get("target_id"), off))
    assert not offenders, (
        f"{path.name} sets a non-zero sketch offset {offenders}. Sketch coordinates "
        f"are world coordinates, so an offset does not position the profile and "
        f"deforms one bound to the sketch origin; the IR layer refuses it. Write the "
        f"coordinates where the profile should be instead."
    )


@pytest.mark.parametrize("path", _session_files(), ids=lambda p: p.name)
def test_no_session_script_carries_the_refused_offset_key_at_all(path: Path):
    """Even a zero offset is a recipe being taught — it says "this is how you
    position a profile", which is the part that was wrong."""
    offenders = [
        node.get("target_id")
        for node in _iter_dicts(json.loads(path.read_text()))
        if node.get("op") in ("add_sketch", "update_sketch")
        and "offset" in (node.get("payload") or {})
    ]
    assert not offenders, (
        f"{path.name} still writes an `offset` key on sketch(es) {offenders}; the "
        f"key is refused, so the script reads as a working example of a recipe "
        f"that is not one."
    )
