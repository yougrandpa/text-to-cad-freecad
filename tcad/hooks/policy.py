"""Built-in deterministic policy hooks (L layer).

These run in-process and must be pure functions of their inputs — no model
classification, no network, no surprises. Each returns a :class:`HookResult`.

Defence-in-depth is the point: the privileged gate below requires *three*
independent conditions to all hold; a single satisfied check is never enough.
"""

from __future__ import annotations

import fnmatch
import inspect
import os
import re
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol, runtime_checkable

from tcad.core.types import HookDecision, HookEvent, HookResult, HookSpec
from tcad.hooks.approval import args_fingerprint

# A tool name is "privileged" only when the payload explicitly says so. The gate
# never infers tier from the model — it trusts the structural ToolTier.
_PRIVILEGED_TIER = "privileged"


def _callable_accepts(fn: Callable, param: str) -> bool:
    """Whether ``fn`` declares ``param`` (or takes **kwargs).

    Used to keep a one-argument ``approval_lookup`` working without the gate
    silently dropping the argument binding for callers that do want it.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if param in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


# ─────────────────────────────────────────────────────────────────────────────
# ApprovalLike — what the triple gate needs from an approval record
# ─────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class ApprovalLike(Protocol):
    """Structural view of an approval the privileged gate consults."""

    granted: bool
    expires_at: datetime
    tool_name: str
    args_hash: Optional[str]


# ─────────────────────────────────────────────────────────────────────────────
# PathGuard
# ─────────────────────────────────────────────────────────────────────────────

def _default_extract_paths(payload: dict) -> list[str]:
    """Best-effort path extraction when no extractor is supplied.

    Looks in ``payload["args"]`` / ``payload["tool_args"]`` (string values) and
    a top-level ``path`` key. Callers should pass their own extractor so the
    guard is testable without the tool layer.
    """
    paths: list[str] = []
    for key in ("args", "tool_args"):
        args = payload.get(key)
        if isinstance(args, dict):
            for v in args.values():
                if isinstance(v, str):
                    paths.append(v)
    if isinstance(payload.get("path"), str):
        paths.append(payload["path"])
    return paths


def _normalize_glob(glob: str) -> str:
    """Bring a deny-glob into the same coordinate system as a resolved path.

    Two failures this fixes, both of which made a deny-list silently useless:

      * ``~`` was never expanded, so ``~/.ssh/**`` compiled to a pattern that
        matched nothing at all, ever.
      * Candidates are compared *after* ``os.path.realpath``, but the glob was
        not. On macOS ``/etc`` is a symlink to ``/private/etc``, so the resolved
        candidate ``/private/etc/passwd`` never matched the configured
        ``/etc/**`` — the exact paths the default policy lists as denied were the
        ones it let through.

    Only the literal prefix (everything before the first wildcard) is resolved;
    resolving a pattern would be meaningless.
    """
    glob = os.path.expanduser(glob)
    idx = len(glob)
    for i, ch in enumerate(glob):
        if ch in "*?[":
            idx = i
            break
    prefix, rest = glob[:idx], glob[idx:]
    if not prefix:
        return glob
    trailing_slash = prefix.endswith("/")
    resolved = os.path.realpath(prefix.rstrip("/") or "/")
    if trailing_slash and not resolved.endswith("/"):
        resolved += "/"
    return resolved + rest


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    """Compile a glob (supporting ``**``) to an anchored regex.

    ``fnmatch`` does not understand ``**`` as "any path", so we translate it
    explicitly. A trailing ``/**`` matches the directory and everything under.
    """
    pat = glob
    # Normalise the recursive marker first.
    pat = pat.replace("**", "\x00")  # placeholder
    # Escape regex metacharacters except our placeholder and '*'/'?'.
    out: list[str] = []
    for ch in pat:
        if ch == "\x00":
            out.append(".*")
        elif ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
    rx = "".join(out)
    if rx.endswith("/.*"):  # "dir/**" should also match "dir" itself
        rx = rx[:-len("/.*")] + "(/.*)?"
    return re.compile("^" + rx + "$")


class PathGuard:
    """Deny any tool argument path that resolves into a denied glob.

    Paths are **resolved** (symlinks + ``..`` collapsed to an absolute real
    path) before matching — a glob that only string-matches is a bug, because
    ``"../../etc/passwd"`` would slip through.
    """

    def __init__(
        self,
        deny_globs: list[str],
        extractor: Optional[Callable[[dict], list[str]]] = None,
    ) -> None:
        # Keep the operator's original spelling for error messages, but compile
        # and match against the normalised form (see _normalize_glob).
        self._globs = [g for g in deny_globs]
        self._norm_globs = [_normalize_glob(g) for g in self._globs]
        self._regexes = [_glob_to_regex(g) for g in self._norm_globs]
        self._extract = extractor or _default_extract_paths

    def __call__(self, event: HookEvent, payload: dict) -> HookResult:
        paths = self._extract(payload)
        for raw in paths:
            if not isinstance(raw, str) or not raw:
                continue
            # Resolve symlinks and '..' to the true absolute path.
            try:
                resolved = os.path.realpath(raw)
            except Exception:
                resolved = os.path.abspath(raw)
            for glob, norm, rx in zip(self._globs, self._norm_globs, self._regexes):
                if rx.match(resolved) or fnmatch.fnmatch(resolved, norm):
                    return HookResult(
                        decision=HookDecision.DENY,
                        hook_name="path_guard",
                        reason=f"path {resolved!r} matches denied glob {glob!r}",
                    )
        return HookResult(
            decision=HookDecision.ALLOW, hook_name="path_guard", reason="no denied paths"
        )


# ─────────────────────────────────────────────────────────────────────────────
# NetworkGuard
# ─────────────────────────────────────────────────────────────────────────────

class NetworkGuard:
    """Deny network-capable tools unless explicitly allowed.

    ``allowed`` is the static policy switch (typically from config). When False,
    any tool flagged network-capable in the payload is denied.
    """

    def __init__(
        self,
        allowed: bool,
        network_tools: Optional[list[str]] = None,
    ) -> None:
        self._allowed = bool(allowed)
        self._network_tools = set(network_tools or [])

    def __call__(self, event: HookEvent, payload: dict) -> HookResult:
        if self._allowed:
            return HookResult(
                decision=HookDecision.ALLOW, hook_name="network_guard", reason="network allowed"
            )
        tool_name = payload.get("tool_name")
        capable = bool(payload.get("network_capable", False))
        if self._network_tools and tool_name in self._network_tools:
            capable = True
        if capable:
            return HookResult(
                decision=HookDecision.DENY,
                hook_name="network_guard",
                reason=f"network-capable tool {tool_name!r} denied (network not allowed)",
            )
        return HookResult(
            decision=HookDecision.ALLOW, hook_name="network_guard", reason="not network-capable"
        )


# ─────────────────────────────────────────────────────────────────────────────
# PrivilegedTripleGate — defence in depth (design §4.5 item 5)
# ─────────────────────────────────────────────────────────────────────────────

class PrivilegedTripleGate:
    """Allow a ``privileged``-tier call only when ALL THREE hold:

      1. static config permits it  (``allow_privileged`` is True);
      2. an unexpired, granted approval exists whose ``tool_name`` matches
         (looked up via ``approval_lookup``);
      3. the sandbox reports healthy (``sandbox_ok()`` is True).

    Any single missing condition -> DENY, and the reason names the failed
    condition(s). This does NOT depend on any model-side classification: the
    tier comes from the structural ``ToolTier``, and the other two conditions
    are environmental/stateful checks.

    ``approval_lookup(tool_name, args_hash, thread_id) -> ApprovalLike | None`` is
    expected to scope by all three (a shorter signature is accepted, with the
    ALLOW reason naming what was *not* bound). The gate computes ``args_hash`` itself from the payload it
    was handed — with the same :func:`tcad.hooks.approval.args_fingerprint` the
    engine used to create the request — so the binding cannot be lost by a caller
    that forgets to pass it. (It used to be the caller's job, and the production
    wiring did exactly that: ``lambda tool_name: ...lookup_valid(tool_name)``. One
    approval for ``raw_python`` therefore authorised *any* code for the whole
    TTL.) The gate additionally re-checks ``granted`` and expiry.

    A one-argument lookup is still accepted for embedders with no per-call
    binding; the ALLOW reason then says the match was by tool name only, so the
    weakening is visible rather than implicit.
    """

    def __init__(
        self,
        allow_privileged: bool,
        approval_lookup: Callable[..., Optional[ApprovalLike]],
        sandbox_ok: Callable[[], bool],
    ) -> None:
        self._allow_privileged = bool(allow_privileged)
        self._approval_lookup = approval_lookup
        self._sandbox_ok = sandbox_ok
        self._lookup_takes_hash = _callable_accepts(approval_lookup, "args_hash")
        self._lookup_takes_thread = _callable_accepts(approval_lookup, "thread_id")

    def __call__(self, event: HookEvent, payload: dict) -> HookResult:
        # Non-privileged tools are simply not this gate's concern.
        if payload.get("tier") != _PRIVILEGED_TIER:
            return HookResult(
                decision=HookDecision.ALLOW,
                hook_name="privileged_triple_gate",
                reason="not a privileged-tier call",
            )

        tool_name = payload.get("tool_name", "<unknown>")
        args_hash = args_fingerprint(payload.get("args"))
        failed: list[str] = []

        # Condition 1: static config.
        if not self._allow_privileged:
            failed.append("static_config(allow_privileged=false)")

        # Condition 3: sandbox health.
        try:
            healthy = bool(self._sandbox_ok())
        except Exception:
            healthy = False
        if not healthy:
            failed.append("sandbox_unhealthy")

        # Condition 2: a valid approval for this tool name AND this exact payload.
        approval = self._safe_lookup(tool_name, args_hash, payload.get("thread_id"))
        if not self._approval_valid(approval):
            failed.append("no_valid_approval")

        if failed:
            return HookResult(
                decision=HookDecision.DENY,
                hook_name="privileged_triple_gate",
                reason=f"privileged gate denied: missing {', '.join(failed)}",
            )

        bound = "tool+args" if self._lookup_takes_hash else "tool only (no args binding)"
        if self._lookup_takes_hash and self._lookup_takes_thread:
            bound += "+session"
        return HookResult(
            decision=HookDecision.ALLOW,
            hook_name="privileged_triple_gate",
            reason=f"privileged call to {tool_name!r} approved ({bound})",
        )

    def _safe_lookup(
        self, tool_name: str, args_hash: str, thread_id: Optional[str]
    ) -> Optional[ApprovalLike]:
        try:
            if self._lookup_takes_hash and self._lookup_takes_thread:
                return self._approval_lookup(tool_name, args_hash, thread_id)
            if self._lookup_takes_hash:
                return self._approval_lookup(tool_name, args_hash)
            return self._approval_lookup(tool_name)
        except Exception:
            return None

    @staticmethod
    def _approval_valid(approval: Optional[ApprovalLike]) -> bool:
        if approval is None:
            return False
        if not getattr(approval, "granted", False):
            return False
        expires = getattr(approval, "expires_at", None)
        if not isinstance(expires, datetime):
            return False
        # Tolerate both aware and naive timestamps by comparing in UTC.
        now = datetime.now(timezone.utc)
        exp = expires
        if expires.tzinfo is None:
            exp = expires.replace(tzinfo=timezone.utc)
        return exp > now
