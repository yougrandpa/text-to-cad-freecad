"""The operator IS the LLM: drive the harness by hand through its real tool surface.

Why this exists
---------------
The harness's contract with a model is not "it is smart", it is:

  1. the tool surface is discoverable and the arguments are guessable;
  2. every failure comes back as text a model can act on (which feature, what
     value, what was expected);
  3. the Gate is the only thing that decides "done".

Those three can be tested without any model provider — by a human/agent issuing
tool calls directly. This script is that path. It uses the **real** registry, the
**real** `ToolContext`, and the **real** hook dispatch (pre/post tool use), so
whatever the model would experience, this experiences.

Usage
-----
    # what does the model actually see?
    python -m tools.agent_driver --list-tools --kind create

    # run a scripted "turn" — a JSON list of tool calls, executed in order
    python -m tools.agent_driver --model-id bracket --calls step1.json

    # inspect state between turns
    python -m tools.agent_driver --model-id bracket --digest
    python -m tools.agent_driver --model-id bracket --show-ir

A calls file looks like::

    [
      {"name": "ir_list_features", "args": {}},
      {"name": "ir_patch", "args": {"base_version": "current", "summary": "...",
                                    "ops": [{"op": "add_sketch", "payload": {...},
                                             "reason": "..."}]}}
    ]

``"base_version": "current"`` is a convenience for the operator (a real model
would read the version from the last tool result).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tcad.config.loader import load_default_config  # noqa: E402
from tcad.core.types import (  # noqa: E402
    HookContext,
    HookDecision,
    HookEvent,
    ToolContext,
    ToolTier,
    TurnKind,
)
from tcad.core.wiring import build_services  # noqa: E402
from tcad.tools.base import _ALLOWED_TIERS, build_default_registry, execute_tool  # noqa: E402


# ── pretty output ─────────────────────────────────────────────────────────


def _hr(title: str) -> None:
    print(f"\n{'─' * 4} {title} {'─' * max(0, 60 - len(title))}")


def _print_tool_result(name: str, result) -> None:
    if result.ok:
        print(f"✓ {name}")
        if result.content:
            print(result.content)
    else:
        err = result.error
        kind = err.kind.value if err else "?"
        print(f"✗ {name}  [{kind}]")
        print(f"   {err.message if err else 'no detail'}")
        if err and err.feature_id:
            print(f"   feature_id={err.feature_id}")
        if err and err.hint:
            print(f"   hint: {err.hint}")
    for img in result.images:
        print(f"   [image] {img.view}: {img.path} ({img.width}x{img.height})")


# ══════════════════════════════════════════════════════════════════════════


async def run_calls(svc, model_id: str, calls: list[dict], *, kind: TurnKind) -> int:
    """Execute a list of tool calls as one Turn. Returns the process exit code."""
    registry = build_default_registry(svc, enable_privileged=bool(svc.config.policy.allow_privileged))
    allowed = set(_ALLOWED_TIERS[kind])
    if svc.config.policy.allow_privileged:
        allowed.add(ToolTier.PRIVILEGED)

    ir = svc.store.load(model_id)
    ctx = ToolContext(
        thread_id=f"hand-{model_id}",
        turn_id="hand-turn-1",
        model_id=model_id,
        ir=ir,
        workdir=str(REPO_ROOT),
        data_dir=str(svc.store.data_dir),
        worker=svc.worker,
        hook_ctx=HookContext(
            thread_id=f"hand-{model_id}", turn_id="hand-turn-1", model_id=model_id
        ),
        # `geo_view` is gated on declared visual checkpoints; when the operator is
        # driving by hand every step is treated as a checkpoint (the engine
        # normally sets this per step from the turn kind). Set on THIS context —
        # the shared services bundle is not ours to mutate.
        visual_ok=True,
    )

    failures = 0
    for i, call in enumerate(calls, 1):
        name = call["name"]
        args = dict(call.get("args") or {})
        if args.get("base_version") == "current":
            args["base_version"] = svc.store.current_version(model_id)

        _hr(f"call {i}/{len(calls)}: {name}  args={json.dumps(args, ensure_ascii=False)[:200]}")

        spec = registry.get(name)
        if spec is None:
            print(f"✗ unknown tool {name!r}; available: {', '.join(sorted(registry.names_for(kind)))}")
            failures += 1
            continue

        payload = {
            "tool_name": name,
            "tier": spec.tier.value,
            "args": args,
            "thread_id": ctx.thread_id,
            "turn_id": ctx.turn_id,
            "model_id": model_id,
        }
        hook_res = svc.hooks.dispatch(HookEvent.PRE_TOOL_USE, payload)
        if hook_res.decision is not HookDecision.ALLOW:
            print(f"✗ {name} blocked by hook {hook_res.hook_name}: {hook_res.reason}")
            failures += 1
            svc.hooks.dispatch(HookEvent.POST_TOOL_USE, {**payload, "ok": False})
            continue

        outcome = await execute_tool(spec, args, ctx, allowed_tiers=allowed)
        svc.hooks.dispatch(
            HookEvent.POST_TOOL_USE, {**payload, "ok": outcome.result.ok}
        )
        _print_tool_result(name, outcome.result)
        if not outcome.result.ok:
            failures += 1
        if outcome.gate_report is not None:
            rep = outcome.gate_report
            print(f"\n   GATE passed={rep.passed}")
            for r in rep.results:
                mark = {"pass": "·", "fail": "✗", "skip": "-", "error": "!"}.get(
                    r.status.value, "?"
                )
                extra = ""
                if r.measurements:
                    extra = "  " + ", ".join(f"{k}={v}" for k, v in r.measurements.items())
                print(f"   {mark} {r.check_id:<26} {r.message}{extra}")
            if rep.skipped_checks:
                print(f"   skipped: {', '.join(rep.skipped_checks)}")

        ctx.ir = svc.store.load(model_id)
    return 0 if failures == 0 else 1


# ══════════════════════════════════════════════════════════════════════════


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="drive tcad by hand (the operator is the LLM)")
    p.add_argument("--model-id")
    p.add_argument("--data-dir", default=".tcad_hand")
    p.add_argument("--calls", help="JSON file: list of {name, args}")
    p.add_argument("--kind", default="create", choices=[k.value for k in TurnKind])
    p.add_argument("--new", action="store_true", help="create the model first")
    p.add_argument("--requirement", default="", help="requirement text for --new")
    p.add_argument("--list-tools", action="store_true", help="print the model-facing tool surface")
    p.add_argument("--full", action="store_true",
                   help="with --list-tools: print complete JSON schemas")
    p.add_argument("--only", help="with --list-tools: show just this tool")
    p.add_argument("--show-ir", action="store_true")
    p.add_argument("--digest", action="store_true")
    p.add_argument("--worker-probe", action="store_true",
                   help="ask the worker directly for compile/introspect/tessellate results")

    args = p.parse_args(argv)
    cfg = load_default_config()
    cfg.storage.data_dir = str((REPO_ROOT / args.data_dir).resolve())
    cfg.storage.sqlite_path = str(Path(cfg.storage.data_dir) / "tcad.sqlite3")

    if args.list_tools:
        # No worker needed to inspect the surface.
        svc = build_services(cfg, start_worker=False)
        registry = build_default_registry(svc)
        kinds = [TurnKind(args.kind)] if args.only or args.full else list(TurnKind)
        for kind in kinds:
            tools = registry.as_openai_tools(kind)
            if args.only:
                tools = [t for t in tools if t["function"]["name"] == args.only]
            _hr(f"{kind.value}: {len(tools)} tools")
            for t in tools:
                fn = t["function"]
                if args.full:
                    print(json.dumps(fn, ensure_ascii=False, indent=2))
                    continue
                params = fn["parameters"].get("properties", {})
                required = fn["parameters"].get("required", [])
                print(f"  {fn['name']}({', '.join(params)})")
                print(f"      {fn['description'][:110]}")
                if required:
                    print(f"      required: {required}")
        return 0

    svc = build_services(cfg)
    try:
        if args.new:
            from tcad.ir.schema import IrDocument, RequirementSpec

            ir = IrDocument(
                model_id=args.model_id,
                version=0,
                requirements=RequirementSpec(raw_text=args.requirement),
            )
            created = svc.store.create(args.model_id, ir)
            print(f"created {args.model_id!r} v{int(created.version)}")

        if args.show_ir:
            ir = svc.store.load(args.model_id)
            _hr(f"IR {args.model_id} v{ir.version}")
            print(json.dumps(json.loads(ir.model_dump_json()), ensure_ascii=False, indent=2))

        if args.digest:
            v = svc.store.current_version(args.model_id)
            digest = svc.context.digest(args.model_id, v)
            _hr(f"digest {args.model_id} v{v}")
            print(digest.text)

        if args.worker_probe:
            ir = svc.store.load(args.model_id)
            out_dir = str(svc.store.artifact_dir(args.model_id, ir.version))
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            for method, params in (
                ("compile_ir", {"ir": ir.model_dump(), "out_dir": out_dir}),
                ("introspect_document", {"ir": ir.model_dump(), "out_dir": out_dir}),
                ("export_artifacts",
                 {"ir": ir.model_dump(), "out_dir": out_dir, "exports": ["step", "stl"]}),
                ("tessellate", {"ir": ir.model_dump(), "out_dir": out_dir}),
            ):
                _hr(f"worker: {method}")
                res = svc.worker.request(method, params, timeout_s=120.0)
                print(f"envelope ok={res.get('ok')}")
                if not res.get("ok"):
                    print("  error:", json.dumps(res.get("error"), ensure_ascii=False))
                    continue
                payload = res.get("result") or {}
                # Print a compact shape summary rather than the whole mesh — but
                # never summarise errors: they are the whole point of probing.
                summary = {}
                for k, v in payload.items():
                    if k in ("errors", "error"):
                        summary[k] = v
                    elif isinstance(v, list):
                        summary[k] = f"<{len(v)} items>"
                    elif isinstance(v, dict):
                        summary[k] = {kk: (f"<{len(vv)} items>" if isinstance(vv, list) else vv)
                                      for kk, vv in v.items()}
                    else:
                        summary[k] = v
                print("  result:", json.dumps(summary, ensure_ascii=False, default=str)[:2000])

        if args.calls:
            calls = json.loads(Path(args.calls).read_text(encoding="utf-8"))
            return asyncio.run(run_calls(svc, args.model_id, calls, kind=TurnKind(args.kind)))
        return 0
    finally:
        svc._worker_handle.close()


if __name__ == "__main__":
    sys.exit(main())
