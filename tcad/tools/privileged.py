"""Privileged tools — the escape hatch.

The Web operator may explicitly select full access for a turn, which skips
tool policy approval and executes Python without an application sandbox.
The triple-gated defaults below still apply to legacy embedded callers that
have not selected a per-turn access mode. Tool arguments cannot change modes.

``raw_python`` is **not registered by default** (design §4.2 / §4.5). It is the
only path that lets arbitrary code run, so it is triple-gated:

  1. static policy ``allow_privileged`` (a human edits a file; the model cannot),
  2. an unexpired approval record for this exact tool call, and
  3. the sandbox reported healthy by ``policy.sandbox_probe``.

This handler enforces gate (2)+(3) itself: it goes through the hook dispatch and
refuses unless the privileged gate returns ALLOW; only then does it execute the
code in a subprocess with a hard timeout, never in the supervisor process and
never touching the live IR.

**Whether that subprocess is sandboxed is a server decision**
(``config.sandbox.enabled`` + ``config.sandbox.backend``), not an argument. It
used to be ``args["sandbox"]``, defaulting to ``False`` — i.e. by default the
model got an unsandboxed subprocess and could not have been stopped from asking
for one, because asking was the only way to get the sandbox at all. A model
argument can never *weaken* the sandbox now; sending one is refused outright, so
the capability does not look controllable either.

What the macOS profile actually enforces, stated precisely because the previous
comment claimed more than the profile did:

  * ``(deny default)`` + ``(allow process-exec)`` — arbitrary exec is permitted;
    this is not a no-exec sandbox.
  * ``(allow file-read*)`` — reads are NOT restricted to ``read_only_roots``.
    That config field is currently advisory and is listed as an open gap rather
    than implied to be in force.
  * ``(allow file-write* (subpath <writable_root>))`` — writes are confined to
    ``config.sandbox.writable_root``.
  * ``(deny network*)`` when ``config.sandbox.no_network``.
  * No privilege drop: the child runs as the supervisor's user.

The profile now starts with ``(version 1)``. Without it ``sandbox-exec`` refuses
the whole profile ("no version specified", exit 65) and every sandboxed call
failed before running a line of the model's code — so the "sandboxed" path had
never actually worked. ``bwrap`` (Linux) remains unimplemented; asking for it
fails closed rather than running unsandboxed.
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


# Minimal sandbox-exec profile (macOS). Linux would use `bwrap` (not implemented
# — see the module docstring). `(version 1)` is REQUIRED: without it sandbox-exec
# rejects the profile outright ("no version specified", exit 65), which is what
# made every "sandboxed" call fail before this line existed.
def _sandbox_profile(*, writable_root: str, no_network: bool) -> str:
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec)",
        "(allow file-read*)",
        f'(allow file-write* (subpath "{writable_root}"))',
    ]
    if no_network:
        lines.append("(deny network*)")
    return "\n".join(lines) + "\n"


def _sandbox_cmd(cmd: list[str], profile: str) -> list[str]:
    return ["sandbox-exec", "-p", profile, *cmd]


class _SandboxDecision:
    """The server's answer to "should this run be sandboxed?".

    Separate from the handler so the policy is one readable object: a refusal is
    a decision with a reason, not a ``None`` that has to be interpreted.
    """

    def __init__(self, *, sandbox: bool, profile: str = "", refuse: str = "") -> None:
        self.sandbox = sandbox
        self.profile = profile
        self.refuse = refuse


def _sandbox_decision(services: "Any") -> _SandboxDecision:
    """Read the sandbox policy from the SERVER config — never from the payload.

    An unconfigured bundle (a unit test with a bare ``SimpleNamespace``) is
    treated as "sandbox on", the stricter default: the permissive direction must
    be the one an operator opted into, not the one that happens when nobody
    configured anything.
    """
    cfg = getattr(getattr(services, "config", None), "sandbox", None)
    if cfg is None:
        return _SandboxDecision(sandbox=True, profile=_sandbox_profile(
            writable_root=".", no_network=True))
    backend = getattr(cfg, "backend", "none")
    if not getattr(cfg, "enabled", True) or backend == "none":
        # Explicit operator choice, recorded in the config a human edits.
        return _SandboxDecision(sandbox=False)
    if backend == "bwrap":
        return _SandboxDecision(
            sandbox=False,
            refuse=("sandbox backend 'bwrap' is configured but not implemented in "
                    "this build; refusing to run raw_python unsandboxed"),
        )
    return _SandboxDecision(sandbox=True, profile=_sandbox_profile(
        writable_root=str(getattr(cfg, "writable_root", ".") or "."),
        no_network=bool(getattr(cfg, "no_network", True)),
    ))



async def _run_subprocess(code: str, *, timeout_s: float,
                          sandbox: bool, profile: str = "") -> tuple[str, str, int]:
    fd, path = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    try:
        with open(path, "w") as f:
            f.write(code)
        cmd = [sys.executable, path]
        if sandbox:
            cmd = _sandbox_cmd(cmd, profile)
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
    # Through this turn's dispatcher, so the decision is the one the front end
    # that started the turn can see and audit.
    hooks = getattr(ctx, "hooks", None) or services.hooks
    hook_res = hooks.dispatch(
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

    # The sandbox is a server policy. A payload that tries to set it is refused
    # rather than ignored: an ignored `sandbox` would still be a parameter the
    # model believes it controls, and the whole point is that it does not.
    if "sandbox" in args:
        return _err(
            ToolErrorKind.SCHEMA,
            "raw_python does not accept a 'sandbox' argument: whether this runs "
            "sandboxed is a server-side policy (config.sandbox) and cannot be "
            "chosen per call. Remove the key and retry.",
            hint="the answer is 'no', not 'which would you prefer'",
        )

    # Only the operator's per-turn selection may disable the sandbox; no tool
    # argument can select full access.
    decision = (_SandboxDecision(sandbox=False) if ctx.access_mode == "full"
                else _sandbox_decision(services))
    if decision.refuse:
        return _err(ToolErrorKind.DENIED, decision.refuse)

    try:
        stdout, stderr, rc = await _run_subprocess(
            code, timeout_s=timeout_s, sandbox=decision.sandbox,
            profile=decision.profile)
    except asyncio.TimeoutError:
        return _err(ToolErrorKind.TIMEOUT, f"raw_python exceeded hard timeout {timeout_s}s")
    except FileNotFoundError as exc:
        which = "sandbox-exec" if decision.sandbox else "python interpreter"
        return _err(ToolErrorKind.RUNTIME, f"{which} not found for raw_python: {exc}")

    if rc != 0:
        # A sandbox that refuses to start is a *configuration* problem, and it
        # used to be indistinguishable from the model's code failing ("exited
        # with code 65" and no clue why — on macOS that was the profile missing
        # its version declaration).
        hint = stderr.strip() or "no stderr"
        if decision.sandbox and "sandbox-exec" in (hint + str(rc)):
            hint = (f"{hint}\nsandbox-exec refused the profile; the code did not run. "
                    f"This is a server configuration problem, not a problem with the code.")
        return _err(
            ToolErrorKind.RUNTIME,
            f"raw_python exited with code {rc}",
            hint=hint,
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
                "Execute arbitrary Python in a subprocess. Triple-gated and disabled by "
                "default. Use only when no IR tool can express the needed operation. "
                "Whether the subprocess is sandboxed is decided by the server "
                "(config.sandbox) and is deliberately not a parameter — sending a "
                "'sandbox' key is refused."
            ),
            params_schema={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "timeout_s": {"type": "number"},
                },
                "required": ["code"],
                "additionalProperties": False,
            },
            handler=functools.partial(raw_python_handler, services),
            timeout_s=30.0,
        ),
    }
