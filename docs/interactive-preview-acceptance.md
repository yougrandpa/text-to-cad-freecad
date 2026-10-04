# Interactive CAD preview acceptance

Date: 2026-10-02. Base: `ea9b193229d94a7c39a719ccba4aee933e856a22`.

## Delivered scope

The existing chat, IR, Gate, history, settings and export architecture remains
in place. The center panel now draws the actual FreeCAD mesh with a small local
WebGL renderer. It supports orbit, zoom, pan, seven standard views, fit, axes,
keyboard shortcuts and touch gestures. Updating the same model retains the
camera; switching conversations resets it. No CDN or frontend build is needed.

`GET /models/{id}/mesh?version=N&tolerance=0.5&force=false` returns
`{model_id, version, mesh}`. Mesh contains vertices, facets, bbox, volume,
tolerance and explicit counts. Omit version for latest. Limits: tolerance
0.1–5mm, 100k vertices, 200k facets, 16MiB response, 16-entry/32MiB cache,
four active preview jobs. Overflow returns 429 with Retry-After. CPU/CAD work
stays off the event loop; cancelled HTTP callers retain admission until work
actually finishes. Preview scratch data never changes Gate/published exports.

Other fixes: one active chat turn per thread/model; PATH command resolution;
explicit Linux native-Python FreeCAD adapter; FreeCAD 1.0 pattern enum
compatibility; honest optional API diagnostics; deterministic demo sketch.

## Final automated result

- Full Python suite: 1,085 passed, 6 skipped
- Node frontend runtime: 17 passed
- Compileall and git diff whitespace checks: passed
- Skips: 4 unconfigured commercial-provider tests, 1 macOS sandbox test,
  1 legacy embedded-FreeCADCmd flag probe (the native adapter and real kernel
  have separate passing coverage)
- Demo repair: 30 repeated native rebuilds remained centered and fully constrained;
  permanent regression repeats compile, independent introspection and STEP checks

## Actual live-model acceptance

A fresh reasoning-model agent received only the app's actual messages and
declared tool contract through `tools/native_agent_bridge.py`. It did not read
repository samples or directly alter CAD/IR state. Four original responses
(ir_patch, ir_commit, ir_patch, ir_commit) passed through the existing HTTP
chat, tool validation, native FreeCAD, Gate and artifact pipeline.

1. Request: 80×50×8mm plate, centered 40×20mm rectangular through-slot
   - Original model: additive_box + subtractive_box, both editable PartDesign features
   - Slot X=20–60mm, Y=15–35mm, Z=0–8mm
   - One valid solid, STEP read-back volume 25,600mm³, Gate passed at v1
2. Follow-up: change thickness to 12mm, preserve outline and centered through-slot
   - Same stable IDs `ft_plate` and `ft_center_slot`
   - Only heights and matching dimensional/volume contract changed
   - One valid solid, STEP read-back volume 38,400mm³, Gate passed at v2
   - STEP, STL and editable FCStd exported for both versions

This is a genuine model-driven test through a local test bridge. It is not
validation of a commercial provider's authentication, streaming, token billing
or API availability. Token usage is unavailable for this bridge and is not
invented. Production providers still use the existing configurable settings.

## Reproducible checks

```bash
python -m venv .venv
.venv/bin/pip install -e '.[server,dev]'
export TCAD_FREECAD_CMD=/path/to/FreeCADCmd
# Explicit Linux alternative, matching the installed Python ABI:
# export TCAD_FREECAD_CMD="$PWD/tools/freecad_python.py"
.venv/bin/python -m pytest tests -q
node --test tests/frontend/*.test.mjs
.venv/bin/python -m compileall -q tcad tools
git diff --check
```

The native kernel tested here is FreeCAD 1.0.0. The distro command's embedded
Python startup failed; the explicit adapter runs the same native modules in
matching system Python 3.13. Supervisor Python is 3.12. Required selftest APIs
pass; experimental CircularPattern is unavailable and remains visibly flagged.
Missing newer scalar properties fail closed. No tolerance was widened.

Runtime cases include exact old/new mesh versions, malformed/empty/partial
geometry refusal, thread-pool fanout/cancellation, concurrent chat exclusion,
stale session responses, camera math/controls and PNG fallback. The real HTTP
interrupt probe returned `aborted` 0.03s after stop, then the same session ran a
new turn. That probe's finite scripted model later ends `failed` when exhausted;
this checks recoverability, not successful model reasoning.

## Explicit limits and manual browser checklist

The cloud browser refused the localhost preview with `net::ERR_BLOCKED_BY_CLIENT`.
Therefore actual browser WebGL shader rendering, desktop dragging and a finished
UI screenshot were **not verified**. Node tests execute camera/handler/loader
logic with a test DOM, not an actual GPU. The delivered PNG is a static render
of the genuine model, not a screenshot of interactive controls.

On a browser that can open the local app, complete this checklist:

- Open `/ui/`, create a model, orbit with left drag, zoom, right-drag pan
- Try all seven views, fit, axes, keyboard controls and touch
- Submit a follow-up edit and confirm orientation/pan/zoom stay unchanged
- Switch sessions during load and confirm no old geometry/history reappears
- Refresh repeatedly during a slow build; 429 must show retry status without PNG fallback
- Disable WebGL and check explicit static fallback; download STEP/STL/FCStd
- Stop a running turn, reconnect/reload and continue the same conversation

The app remains unauthenticated and loopback-only by default. Do not expose it
publicly. The pre-existing PNG fallback cache is version-based; force-refresh
after manually restoring/replacing a snapshot under the same version.
