"""Re-derive the param classification from the real kernel (task §5-A / R-7).

``tcad/ir/validate._VERIFIED_OP_PARAMS`` claims every key it lists can be honoured
by the compiler. "Honoured" means ``_assign_props`` reaches a FreeCAD property it
can set from a JSON value — not merely that a property with that name exists.
Half the keys that used to be listed resolved to ``App::PropertyLinkSub``, which
``setattr`` cannot take from a string:

    TypeError: type must be 'DocumentObject', 'NoneType' or
               ('DocumentObject',['String',]) not str

…raised *after* the patch was persisted. This test creates each PartDesign object
on the real kernel and checks the property type behind every allowed key, so the
table cannot quietly regain an unhonourable entry.

``tcad.ir`` cannot be imported inside FreeCADCmd (pydantic is unavailable there),
so the allow-list is handed to the worker-side script as JSON and the
classification is printed back.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from tcad.ir.validate import _GUIDANCE_BASE, _GUIDANCE_BY_OP, _guidance_for, _VERIFIED_OP_PARAMS

REPO_ROOT = Path(__file__).resolve().parents[2]
FREECAD_CMD = os.environ.get(
    "TCAD_FREECAD_CMD",
    str(REPO_ROOT / "free-cad" / "FreeCAD" / "build" / "debug" / "bin" / "FreeCADCmd"),
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(not Path(FREECAD_CMD).exists(), reason="FreeCADCmd build not found"),
]

#: Property types `_assign_props` knows how to set from a JSON value. Anything
#: else is a reference/container the IR cannot express.
_SCALAR_TYPES = {
    "App::PropertyLength", "App::PropertyDistance", "App::PropertyAngle",
    "App::PropertyBool", "App::PropertyEnumeration", "App::PropertyFloat",
    "App::PropertyInteger", "App::PropertyString", "App::PropertyPercent",
    "App::PropertyFloatConstraint", "App::PropertyIntegerConstraint",
    "App::PropertyQuantityConstraint",
    # NOTE: App::PropertyVector is NOT settable — `_assign_props` has no Vector
    # branch, so a JSON {"x":…} reaches setattr and fails.
}

#: `axis` for these ops is a compiler-consumed *name* ("X"/"Y"/"Z", or a sketch
#: axis for revolution/groove), not a property key: `_set_axis_reference`
#: translates it into the LinkSub / vector the feature actually takes. Probing
#: the key name against the object therefore says nothing about whether it is
#: honourable — the compiler never hands it to `setattr` as a scalar.
_AXIS_NAME_OPS = {("revolution", "axis"), ("groove", "axis"),
                  ("linear_pattern", "axis"), ("polar_pattern", "axis")}

# These are measured version boundaries, not a blanket allowance for absent
# properties. Every absence must also exercise the real worker refusal below.
# The 26.3 development build's wider scalar vocabulary stays tested unchanged.
_FREECAD_1_0_ABSENT_SCALARS = {
    "chamfer": {"operation"}, "draft": {"operation"}, "fillet": {"operation"},
    "groove": {"operation", "side_type", "start_offset", "start_type", "type2"},
    "hole": {"base_profile_type", "cosmetic_thread", "operation", "start_offset", "start_type"},
    "linear_pattern": {"length2", "mode2", "occurrences2", "reversed2"},
    "pad": {"offset2", "side_type", "start_type", "type2"},
    "pocket": {"offset2", "side_type", "start_type", "type2"},
    "revolution": {"fuse_order", "operation", "side_type", "start_offset", "start_type", "type2"},
    "thickness": {"operation"},
}

_WORKER_SCRIPT = textwrap.dedent('''
    """Print the property type behind every probed key. Runs in FreeCADCmd."""
    import json
    import FreeCAD
    from tcad.worker.compiler import FEATURE_TYPE_MAP, _prop_name, _assign_props, _apply_feature

    ALLOW = json.load(open(ALLOW_JSON))     # {op: [allowed keys]}
    REFUSED = json.load(open(REFUSED_JSON)) # {op: [refused keys]}

    doc = FreeCAD.newDocument("KeyProbe")
    body = doc.addObject("PartDesign::Body", "Body")
    objs = {}
    for op, type_id in FEATURE_TYPE_MAP.items():
        try:
            o = doc.addObject(type_id, "P_" + op)
            body.addObject(o)
            objs[op] = o
        except Exception:
            pass

    def kind(obj, key):
        pname = _prop_name(obj, key)
        if pname is None:
            return "absent"
        return obj.getTypeIdOfProperty(pname)

    out = {"allowed": {}, "refused": {}, "unavailable_errors": {},
           "version": FreeCAD.Version()[:3]}
    for op in sorted(ALLOW):
        obj = objs.get(op)
        if obj is None:
            out["allowed"][op] = {"__op__": "NO_OBJECT"}
            state = _apply_feature(doc, body,
                {"id": "missing_" + op, "op": op, "params": {}}, {})
            out["unavailable_errors"][op] = state["errors"]
            continue
        out["allowed"][op] = {k: kind(obj, k) for k in ALLOW[op]}
        for key, type_id in out["allowed"][op].items():
            if type_id == "absent" and key != "axis":
                out["unavailable_errors"][op + "." + key] = _assign_props(obj, {key: 1})
    for op in sorted(REFUSED):
        obj = objs.get(op)
        if obj is None:
            continue
        out["refused"][op] = {k: kind(obj, k) for k in REFUSED[op]}
    FreeCAD.closeDocument("KeyProbe")
    print("###KEYS###" + json.dumps(out))
''')


@pytest.fixture(scope="module")
def measured(tmp_path_factory) -> dict:
    """``{"allowed": {op: {key: type}}, "refused": {op: {key: type}}}`` from the kernel."""
    work = tmp_path_factory.mktemp("paramcap")
    allow_path = work / "allow.json"
    refused_path = work / "refused.json"
    script_path = work / "probe.py"

    allow = {k: sorted(v) for k, v in _VERIFIED_OP_PARAMS.items()}
    # Probe every guided key against every op that does NOT allow it, so the
    # classification behind each refusal is measured rather than asserted.
    refused: dict[str, list[str]] = {}
    guided_keys = set(_GUIDANCE_BASE) | {k for (_o, k) in _GUIDANCE_BY_OP}
    for op, allowed in _VERIFIED_OP_PARAMS.items():
        refused[op] = sorted(k for k in guided_keys
                             if k not in allowed and _guidance_for(op, k))

    allow_path.write_text(json.dumps(allow), encoding="utf-8")
    refused_path.write_text(json.dumps(refused), encoding="utf-8")
    script_path.write_text(
        _WORKER_SCRIPT.replace("ALLOW_JSON", repr(str(allow_path)))
                     .replace("REFUSED_JSON", repr(str(refused_path))),
        encoding="utf-8")

    proc = subprocess.run(
        [FREECAD_CMD, "--console", "-P", str(REPO_ROOT), str(script_path)],
        capture_output=True, text=True, timeout=300, cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    marker = [ln for ln in proc.stdout.splitlines() if ln.startswith("###KEYS###")]
    assert marker, f"probe printed no result.\nstdout tail: {proc.stdout[-2000:]}"
    return json.loads(marker[0][len("###KEYS###"):])


def test_no_allowed_key_is_a_reference_or_container_property(measured):
    """A scalar must be settable, or precisely version-gated and refused."""
    offenders = []
    legacy = measured["version"][:2] == ["1", "0"]
    for op, row in measured["allowed"].items():
        for key, type_id in row.items():
            if type_id == "NO_OBJECT":
                assert legacy and op == "circular_pattern", (op, measured["version"])
                errors = measured["unavailable_errors"][op]
                assert errors and errors[0]["kind"] == "compile", errors
                assert errors[0]["feature_id"] == "missing_" + op, errors
                assert "FreeCAD 1.0" in errors[0]["message"], errors
                continue
            if (op, key) in _AXIS_NAME_OPS:
                continue
            if (type_id == "absent" and legacy
                    and key in _FREECAD_1_0_ABSENT_SCALARS.get(op, set())):
                errors = measured["unavailable_errors"][op + "." + key]
                assert errors and errors[0]["kind"] == "compile", errors
                assert key in errors[0]["message"] and "FreeCAD 1.0" in errors[0]["message"], errors
                continue
            if type_id == "absent" or type_id not in _SCALAR_TYPES:
                offenders.append(f"{op}.{key} -> {type_id}")
    assert not offenders, (
        "these allow-listed keys do not reach a property the compiler can set "
        f"from a JSON value: {offenders}")


def test_every_guidance_key_really_is_unhonourable(measured):
    """Guidance must describe a real limitation, not a stale note."""
    wrong = []
    for op, row in measured["refused"].items():
        for key, type_id in row.items():
            if (op, key) in _AXIS_NAME_OPS:
                continue  # allowed for those ops; not probed as refused there
            if not _guidance_for(op, key):
                continue  # not something we refuse
            if type_id != "absent" and type_id in _SCALAR_TYPES:
                wrong.append(f"{op}.{key} -> {type_id} (settable, so the refusal is wrong)")
    assert not wrong, wrong


def test_a_vector_typed_refusal_says_vector_not_reference(measured):
    """The message must describe the property that is actually there.

    ``Base`` is a LinkSub on Fillet but a Vector (the base *point*) on Revolution;
    ``Direction`` is a Vector on Pad but a LinkSub on LinearPattern. A refusal that
    talks about "an edge reference" for a Vector sends the model looking for the
    wrong fix — which is the failure mode this whole round is about.
    """
    misleading = []
    for op, row in measured["refused"].items():
        for key, type_id in row.items():
            if not _guidance_for(op, key):
                continue
            if type_id == "App::PropertyVector":
                msg = _guidance_for(op, key)
                if "Vector" not in msg:
                    misleading.append(f"{op}.{key}: {msg!r}")
    assert not misleading, (
        "these refusals describe a reference, but the property is a Vector: "
        f"{misleading}")


def test_the_measurement_actually_reached_the_kernel(measured):
    """A probe that silently produced nothing would pass the tests above.

    It has to be able to see three different answers: a scalar property, a vector,
    and a genuine reference — otherwise "no offenders" proves nothing.
    """
    allowed = measured["allowed"]
    assert allowed, "no ops were measured"
    assert allowed["pad"].get("length") == "App::PropertyLength"
    assert allowed["revolution"].get("axis") == "App::PropertyVector"
    assert allowed["pocket"].get("reversed") == "App::PropertyBool"
    # The axis-name exception above is measured here, not assumed: for patterns the
    # key reaches a reference (polar) or no property at all (linear — the property
    # is `Direction`), which is exactly why the compiler translates the name
    # instead of handing it to setattr.
    assert allowed["polar_pattern"].get("axis") == "App::PropertyLinkSub"
    assert allowed["linear_pattern"].get("axis") == "absent"

    refused = measured["refused"]
    assert refused["pad"].get("up_to_face") == "App::PropertyLinkSub"
    assert refused["mirrored"].get("originals") == "App::PropertyLinkList"
    assert refused["pad"].get("depth_type") == "absent"


def test_the_samples_keys_are_all_measurably_settable(measured):
    """The recipes the acceptance samples use must survive this measurement."""
    checks = [("pad", "length"), ("pad", "type"), ("pocket", "type"),
              ("pocket", "reversed"), ("revolution", "angle"), ("hole", "diameter")]
    for op, key in checks:
        type_id = measured["allowed"].get(op, {}).get(key)
        assert type_id in _SCALAR_TYPES, f"{op}.{key} -> {type_id}"
