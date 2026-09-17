"""Deterministic, fail-closed hook dispatcher (L layer).

Hooks are the deterministic safety gate. The dispatcher runs every hook
registered for an event, in a deterministic order, and merges their decisions:

    any DENY  -> DENY
    else ASK  -> ASK
    else      -> ALLOW

**Fail-closed, everywhere.** A hook that times out, exits non-zero, emits
unparseable/unknown output, or raises an exception is treated as ``DENY`` — it
never fails *open*. The reason of every fail-closed result carries the literal
marker ``hook_failed`` so the caller can log it (``HookResult`` has no free-form
payload field in the frozen contract, so the marker lives in ``reason``).

Hooks are frozen at construction: there is no public API to add, remove or
replace a hook at runtime. The hook list is stored as a tuple and no mutator
methods exist.

Two hook kinds:
  - ``policy``  : in-process callable resolved from ``module="pkg.mod:Factory"``
                  via importlib *or* supplied pre-built through ``registry``.
  - ``command`` : external process; JSON on stdin, JSON on stdout, ``timeout_s``
                  enforced.
"""

from __future__ import annotations

import importlib
import json
import shlex
import subprocess
from typing import Callable, Optional

from tcad.core.types import HookDecision, HookEvent, HookResult, HookSpec

# Accepted decision strings (case-insensitive) from policy/command hooks.
_VALID_DECISIONS = {d.value for d in HookDecision}


class HookDispatcher:
    """Runs a frozen set of hooks for lifecycle events, fail-closed."""

    def __init__(
        self,
        hooks: list[HookSpec],
        registry: dict[str, Callable] | None = None,
    ) -> None:
        self._registry = dict(registry or {})
        # Freeze: store as a tuple so the public surface cannot be mutated.
        self._specs: tuple[HookSpec, ...] = tuple(hooks)
        # Priority = position in the supplied list (lower index == higher
        # priority); ties broken by name. This is the deterministic order.
        self._priority: dict[str, int] = {s.name: i for i, s in enumerate(self._specs)}
        # Pre-resolve every hook once at construction (no lazy resolution, no
        # runtime mutation of the hook set).
        self._chain: list[tuple[HookSpec, str, Optional[Callable]]] = []
        for spec in self._specs:
            handler: Optional[Callable] = None
            if spec.kind == "policy":
                if spec.name in self._registry and callable(self._registry[spec.name]):
                    handler = self._registry[spec.name]
                else:
                    handler = _resolve_policy(spec)
            # command hooks carry their executable in spec.command.
            self._chain.append((spec, spec.kind, handler))

    # Read-only view of the frozen spec list.
    @property
    def hooks(self) -> tuple[HookSpec, ...]:
        return self._specs

    def dispatch(self, event: HookEvent, payload: dict) -> HookResult:
        """Run all hooks for ``event`` and merge to a single decision.

        Never raises: any internal failure becomes a ``DENY`` result.
        """
        matched = [c for c in self._chain if event in c[0].events]
        # Deterministic order: (priority, name).
        matched.sort(key=lambda c: (self._priority[c[0].name], c[0].name))

        if not matched:
            return HookResult(
                decision=HookDecision.ALLOW,
                hook_name="<no-hooks>",
                reason="no hooks registered for this event",
            )

        results: list[HookResult] = []
        for spec, kind, handler in matched:
            results.append(self._run_one(spec, kind, handler, event, payload))

        denies = [r for r in results if r.decision == HookDecision.DENY]
        if denies:
            return denies[0]
        asks = [r for r in results if r.decision == HookDecision.ASK]
        if asks:
            return asks[0]

        # All allowed. Carry forward any argument rewrites: an earlier version
        # synthesised a bare ALLOW here and silently dropped `mutated_args`, so a
        # hook that sanitised a path or clamped a value had no effect at all while
        # still reporting success. Later hooks win on conflicts (they are applied
        # in order).
        allows = [r for r in results if r.decision == HookDecision.ALLOW]
        merged_args: Optional[dict] = None
        for r in allows:
            if r.mutated_args:
                merged_args = {**(merged_args or {}), **r.mutated_args}
        # With a single allowing hook, pass its reason through verbatim — it is
        # the most informative thing we have, and swallowing it makes hook
        # behaviour hard to debug.
        reason = (
            allows[0].reason
            if len(allows) == 1 and allows[0].reason
            else "allowed by " + ", ".join(r.hook_name for r in allows)
            if allows
            else "all hooks allowed"
        )
        return HookResult(
            decision=HookDecision.ALLOW,
            hook_name=allows[0].hook_name if allows else matched[0][0].name,
            reason=reason,
            mutated_args=merged_args,
        )

    # ── single-hook execution ──────────────────────────────────────────────

    def _run_one(self, spec, kind, handler, event, payload) -> HookResult:
        try:
            if kind == "command":
                return self._run_command(spec, event, payload)
            # policy
            if handler is None:
                return _deny(spec.name, "hook_failed: policy handler unresolved")
            raw = handler(event, payload)
            return _normalise(spec.name, raw)
        except Exception as exc:  # noqa: BLE001 — fail-closed on any error
            return _deny(spec.name, f"hook_failed: {type(exc).__name__}: {exc}")

    def _run_command(self, spec: HookSpec, event: HookEvent, payload: dict) -> HookResult:
        if not spec.command:
            return _deny(spec.name, "hook_failed: command hook has no command")
        # `shell=False` keeps this injection-proof, but that also means the string
        # cannot be handed to a shell to split — `subprocess.run("python x.py")`
        # would look for a file literally named "python x.py". Split it ourselves
        # so an operator can configure "python3 hooks/audit.py" (or a list, for
        # paths containing spaces).
        try:
            argv = list(spec.command) if isinstance(spec.command, (list, tuple)) \
                else shlex.split(spec.command)
        except ValueError as exc:
            return _deny(spec.name, f"hook_failed: cannot parse command: {exc}")
        if not argv:
            return _deny(spec.name, "hook_failed: command hook has an empty command")
        try:
            proc = subprocess.run(
                argv,
                input=json.dumps({"event": event.value, "payload": payload}),
                capture_output=True,
                text=True,
                timeout=spec.timeout_s,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return _deny(spec.name, "hook_failed: command timed out")
        except Exception as exc:  # noqa: BLE001
            return _deny(spec.name, f"hook_failed: command spawn error: {exc}")

        if proc.returncode != 0:
            return _deny(
                spec.name,
                f"hook_failed: command exited {proc.returncode}: "
                f"{(proc.stderr or '').strip()[:200]}",
            )

        try:
            out = json.loads(proc.stdout)
        except Exception:
            return _deny(spec.name, "hook_failed: unparseable command stdout")

        # Valid JSON but the wrong shape (e.g. a bare string or a list) must be
        # reported as such. It used to fall through to an AttributeError on
        # `.get`, which reached the operator as a confusing internal error.
        if not isinstance(out, dict):
            return _deny(
                spec.name,
                f"hook_failed: command returned a JSON {type(out).__name__}, "
                "expected an object",
            )

        decision = str(out.get("decision", "")).strip().lower()
        if decision not in _VALID_DECISIONS:
            return _deny(spec.name, f"hook_failed: unknown decision {decision!r}")
        reason = str(out.get("reason", ""))
        return HookResult(
            decision=HookDecision(decision),
            hook_name=spec.name,
            reason=reason,
            mutated_args=out.get("mutated_args"),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _deny(hook_name: str, reason: str) -> HookResult:
    return HookResult(decision=HookDecision.DENY, hook_name=hook_name, reason=reason)


def _normalise(hook_name: str, raw) -> HookResult:
    """Coerce a policy hook's return value into a HookResult, fail-closed."""
    if isinstance(raw, HookResult):
        if raw.decision not in (HookDecision.ALLOW, HookDecision.ASK, HookDecision.DENY):
            return _deny(hook_name, f"hook_failed: unknown decision {raw.decision!r}")
        return raw
    if isinstance(raw, HookDecision):
        return HookResult(decision=raw, hook_name=hook_name, reason="")
    if isinstance(raw, str) and raw.lower() in _VALID_DECISIONS:
        return HookResult(decision=HookDecision(raw.lower()), hook_name=hook_name, reason="")
    # Anything else (wrong type, unexpected object) is a hook fault.
    return _deny(hook_name, f"hook_failed: bad hook return type {type(raw).__name__}")


def _resolve_policy(spec: HookSpec) -> Callable:
    """Resolve a ``policy`` hook from ``module="pkg.mod:Factory"`` via importlib.

    The target may be a class (instantiated with the spec, falling back to a
    zero-arg constructor) or an already-callable factory.
    """
    if not spec.module:
        raise ValueError(f"policy hook {spec.name!r} has neither registry entry nor module")
    modpath, _, attr = spec.module.partition(":")
    if not modpath or not attr:
        raise ValueError(f"invalid module spec {spec.module!r} for hook {spec.name!r}")
    module = importlib.import_module(modpath)
    obj = getattr(module, attr)
    if isinstance(obj, type):
        try:
            return obj(spec)
        except TypeError:
            return obj()
    if callable(obj):
        return obj
    raise ValueError(f"{spec.module!r} is neither callable nor a class")
