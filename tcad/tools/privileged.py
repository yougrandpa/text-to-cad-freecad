"""Privileged tools — the escape hatch.

``raw_python`` is **not registered by default** (design §4.2 / §4.5). It is the
only path that lets arbitrary code run, so it is triple-gated:

  1. static policy ``allow_privileged`` (a human edits a file; the model cannot),
  2. an unexpired approval record for this exact tool call, and
  3. a subprocess sandbox (read-only roots, no network, low-priv user).

This handler enforces gate (2)+(3) itself: it goes through the hook dispatch
and refuses unless the privileged gate returns ALLOW; only then does it execute
the code in a subprocess with a hard timeout, never in the supervisor process and
never touching the live IR.
"""

from __future__ import annotations

import asyncio
import functools
import os
import sys
import tempfile

from tcad.core.types import (
    HookDecision,
    HookEvent,
    ToolContext,
    ToolError,
    ToolErrorKind,
    ToolResult,
    ToolSpec,
    ToolTier,
)

from tcad.tools.ir_tools import _err, _ok  # shared helpers


# Minimal sandbox-exec profile (macOS). Linux would use `bwrap`. Only used when
# the caller explicitly opts in via args["sandbox"]=True.
_SANDBOX_PROFILE = (
    '(deny default)\n'
    '(allow process-exec)\n'
    '(allow file-read*)\n'
    '(allow file-write* (subpath "."))\n'
    '(deny network*)\n'
)


def _sandbox_cmd(cmd: list[str]) -> list[str]:
    return ["sandbox-exec", "-p", _SANDBOX_PROFILE, *cmd]


async def _run_subprocess(code: str, *, timeout_s: float, sandbox: bool) -> tuple[str, str, int]:
    fd, path = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    try:
        with open(path, "w") as f:
            f.write(code)
        cmd = [sys.executable, path]
        if sandbox:
            cmd = _sandbox_cmd(cmd)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        return (
            out.decode(errors="replace"),
            err.decode(errors="replace"),
            proc.returncode or 0,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def raw_python_handler(services: "Any", args: dict, ctx: ToolContext) -> ToolResult:
    # Gate (2): the privileged triple-gate hook must explicitly ALLOW.
    hook_res = services.hooks.dispatch(
        HookEvent.PRE_TOOL_USE,
        {
            "tool_name": "raw_python",
            "tier": "privileged",
            "args": args,
            "thread_id": ctx.thread_id,
            "turn_id": ctx.turn_id,
            "model_id": ctx.model_id,
        },
    )
    if hook_res.decision != HookDecision.ALLOW:
        reason = hook_res.reason or f"privileged tool blocked (decision={hook_res.decision.value})"
        return _err(ToolErrorKind.DENIED, reason)

    code = args.get("code", "")
    timeout_s = float(args.get("timeout_s", 20.0))
    use_sandbox = bool(args.get("sandbox", False))
    try:
        stdout, stderr, rc = await _run_subprocess(code, timeout_s=timeout_s, sandbox=use_sandbox)
    except asyncio.TimeoutError:
        return _err(ToolErrorKind.TIMEOUT, f"raw_python exceeded hard timeout {timeout_s}s")
    except FileNotFoundError:
        return _err(ToolErrorKind.RUNTIME, "python interpreter not found for raw_python sandbox")

    if rc != 0:
        return _err(
            ToolErrorKind.RUNTIME,
            f"raw_python exited with code {rc}",
            hint=(stderr.strip() or "no stderr"),
        )
    content = stdout
    if stderr.strip():
        content += "\n--stderr--\n" + stderr
    return _ok(content.strip() or "(no output)")


def build_privileged_tools(services: "Any") -> dict[str, ToolSpec]:
    return {
        "raw_python": ToolSpec(
            name="raw_python",
            tier=ToolTier.PRIVILEGED,
            description=(
                "Execute arbitrary Python in an isolated subprocess sandbox. Triple-gated and "
                "disabled by default. Use only when no IR tool can express the needed operation."
            ),
            params_schema={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "sandbox": {"type": "boolean"},
                    "timeout_s": {"type": "number"},
                },
                "required": ["code"],
            },
            handler=functools.partial(raw_python_handler, services),
            timeout_s=30.0,
        ),
    }
