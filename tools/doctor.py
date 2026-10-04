"""Pre-flight environment diagnosis — what is runnable here, and what is not.

Answers the question a report must not fudge: which of the three test layers can
actually execute in THIS checkout on THIS machine.

    supervisor side   python + dependencies + config            → unit tests
    geometry side     a headless FreeCADCmd worker round-trip    → contract/integration tests
    model side        an OpenAI-compatible endpoint + a capable model → real-LLM e2e

Usage:
    .venv/bin/python tools/doctor.py             # full check (starts one worker)
    .venv/bin/python tools/doctor.py --fast      # skip the worker (offline/CPU-tight)
    TCAD_FREECAD_CMD=/opt/freecad/bin/FreeCADCmd .venv/bin/python tools/doctor.py

Exit code: 0 everything checked out, 1 supervisor side broken (nothing runs),
2 the FreeCAD worker is unavailable (geometry layers BLOCKED, unit tests still
runnable), 3 the model provider is unreachable (real-LLM e2e BLOCKED).

Secrets are never printed: an API key is reported as set/unset only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# (import name, distribution name) — kept in sync with pyproject.toml
CORE_DEPS = [("pydantic", "pydantic>=2.7"), ("yaml", "pyyaml>=6.0"),
             ("numpy", "numpy>=1.26"), ("PIL", "pillow>=10.0"),
             ("openai", "openai>=1.40")]
SERVER_DEPS = [("fastapi", "fastapi>=0.110"), ("uvicorn", "uvicorn>=0.29")]
TEST_DEPS = [("pytest", "pytest>=8.0"), ("pytest_asyncio", "pytest-asyncio>=0.23"),
             ("httpx", "httpx>=0.27")]

LINE = "─" * 72


class Report:
    def line(self, status: str, key: str, value: str = "") -> None:
        print(f"{status:<5} {key:<34} {value}".rstrip())

    def ok(self, key: str, value: str = "") -> None:
        self.line("PASS", key, value)

    def bad(self, key: str, value: str = "") -> None:
        self.line("FAIL", key, value)

    def warn(self, key: str, value: str = "") -> None:
        self.line("WARN", key, value)

    def info(self, key: str, value: str = "") -> None:
        self.line("INFO", key, value)


def check_supervisor(r: Report) -> bool:
    """Python + dependencies + config load. Without these nothing runs."""
    print(LINE)
    print("supervisor side  (deterministic unit tests need only this)")
    print(LINE)
    r.info("platform", f"{platform.system()} {platform.release()} {platform.machine()}")
    r.info("python", f"{sys.version.split()[0]}  ({sys.executable})")
    r.info("repo", str(REPO_ROOT))
    try:
        head = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10)
        r.info("git", head.stdout.strip() if head.returncode == 0 else "not a git checkout")
    except Exception:  # noqa: BLE001 — informational
        r.info("git", "unavailable")

    healthy = True
    for group, deps in (("core", CORE_DEPS), ("server", SERVER_DEPS), ("test", TEST_DEPS)):
        missing = [spec for mod, spec in deps if importlib.util.find_spec(mod) is None]
        if missing:
            healthy = False
            r.bad(f"deps:{group}", "missing " + ", ".join(missing))
        else:
            r.ok(f"deps:{group}", f"{len(deps)} packages")
    if not healthy:
        r.info("fix", '.venv/bin/pip install -e ".[server,dev]"')

    try:
        from tcad.config.loader import load_default_config
        cfg = load_default_config()
    except Exception as exc:  # noqa: BLE001 — a config error IS the finding
        r.bad("config", f"{type(exc).__name__}: {exc}")
        return False
    r.ok("config", "configs/default.yaml loaded")
    return healthy


def resolve_freecad_cmd(cfg) -> tuple[Path, str]:
    """TCAD_FREECAD_CMD wins; else the configured path (relative to the repo).

    A bare name like ``FreeCADCmd`` is looked up on PATH, so a packaged install
    needs no config edit.
    """
    env = os.environ.get("TCAD_FREECAD_CMD", "").strip()
    if env:
        return Path(env).expanduser(), "TCAD_FREECAD_CMD"
    raw = str(getattr(cfg.runtime, "freecad_cmd", "") or "").strip()
    if not raw:
        return Path(""), "config empty"
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p, "runtime.freecad_cmd"
    if "/" not in raw and os.sep not in raw:
        found = shutil.which(raw)
        if found:
            return Path(found), "PATH"
    return REPO_ROOT / raw, "runtime.freecad_cmd (relative to repo)"


def check_worker(r: Report, cfg, fast: bool) -> bool:
    print(LINE)
    print("geometry side  (contract + integration tests need a live worker)")
    print(LINE)
    if fast:
        r.warn("worker", "skipped (--fast)")
        return False

    cmd, source = resolve_freecad_cmd(cfg)
    r.info("freecad_cmd", f"{cmd or '(none)'}  [{source}]")
    if not cmd:
        r.bad("worker", "no FreeCADCmd path configured")
        return False
    if not cmd.exists():
        r.bad("worker", f"not found: {cmd}")
        r.info("fix", "build FreeCAD (pixi run build) or set TCAD_FREECAD_CMD")
        return False
    if not os.access(cmd, os.X_OK):
        r.bad("worker", f"not executable: {cmd}")
        return False

    try:
        out = subprocess.run([str(cmd), "--version"], capture_output=True,
                             text=True, timeout=120)
        first = (out.stdout or out.stderr).strip().splitlines()
        if out.returncode != 0:
            r.bad("freecad --version",
                  f"exit {out.returncode}: {first[0] if first else '(no output)'}")
            return False
        r.ok("freecad --version", first[0] if first else "(no output)")
    except Exception as exc:  # noqa: BLE001
        r.bad("freecad --version", f"{type(exc).__name__}: {exc}")
        return False

    try:
        from tcad.core.worker_client import WorkerHandle
        handle = WorkerHandle(str(cmd), REPO_ROOT, worker_id="doctor",
                              startup_timeout_s=180.0, request_timeout_s=120.0)
        handle.start()
    except Exception as exc:  # noqa: BLE001 — startup failure is the finding
        r.bad("worker", f"startup failed: {type(exc).__name__}: {exc}")
        return False
    try:
        ping = handle.request_sync("ping", {}, timeout_s=60.0)
        if not ping.get("ok"):
            r.bad("worker:ping", str(ping.get("error") or ping))
            return False
        r.ok("worker:ping", "JSONL RPC over stdio up")
        # request_sync returns the worker's result payload directly, not an
        # {'ok', 'result'} envelope — reading it as one used to report a false
        # FAIL while dumping a healthy selftest.
        st = handle.request_sync("api_selftest", {}, timeout_s=120.0)
        if st.get("ok"):
            required = [check for check in st.get("checks", [])
                        if check.get("required", True)]
            r.ok("worker:api_selftest",
                 f"{len(required)} required FreeCAD API probes passed")
            if st.get("optional_missing"):
                r.warn("optional FreeCAD APIs",
                       f"unavailable on FreeCAD {st.get('freecad_version', 'unknown')}: "
                       + ", ".join(st["optional_missing"]))
            # The selftest reports `feature:<op>` for all 21 ops, which reads as
            # "21 features work". It only proves addObject() returned an object.
            #
            # The counts come from the capability table rather than being written
            # here: this line said "3 ops are kernel-verified (pad, pocket,
            # additive_box)" long after that had stopped being true, and a number
            # restated in a diagnostic is a number that drifts away from the
            # table it describes.
            from tcad.ir.capability import ops_by_tier

            verified, experimental = ops_by_tier()
            r.warn("api_selftest scope",
                   f"instantiation only — {len(verified)}/"
                   f"{len(verified) + len(experimental)} ops are kernel-verified; "
                   f"the rest are {', '.join(experimental)}. "
                   f"See tcad/ir/capability.py")
        else:
            r.bad("worker:api_selftest", f"missing={st.get('missing') or st}")
        return bool(st.get("ok"))
    finally:
        handle.close()


def check_provider(r: Report, cfg) -> bool:
    print(LINE)
    print("model side  (real-LLM end-to-end tests need a reachable provider)")
    print(LINE)
    # Where the provider values came from. `settings.json` (what the UI saved)
    # overrides the YAML, so a doctor that only reads the YAML reports the
    # placeholder endpoint in `configs/default.yaml` and calls the configured
    # provider "unreachable" — a false negative that sends people to the wrong
    # place. Report the effective values AND their source.
    source = "configs/default.yaml"
    data_dir = str(getattr(getattr(cfg, "storage", None), "data_dir", "") or "")
    try:
        from tcad.config.settings import effective, settings_path

        eff = effective(cfg, data_dir)
        path = settings_path(data_dir)
        if path.exists():
            llm = eff.llm
            source = f"{path.name} (saved in the UI)"
        else:
            llm = cfg.llm
    except Exception:  # noqa: BLE001 — a broken settings file is not this function's finding
        llm = cfg.llm
    base = str(getattr(llm, "base_url", "") or "").rstrip("/")
    r.info("base_url", base or "(unset)")
    r.info("model", str(getattr(llm, "model", "") or "(unset)"))
    r.info("settings_from", source)
    # The key can live in the settings file rather than an environment variable.
    inline_key = str(getattr(llm, "api_key", "") or "")
    key_env = str(getattr(llm, "api_key_env", "") or "")
    if inline_key:
        r.info("api_key", f"set in settings ({len(inline_key)} chars)")  # never the value
    else:
        r.info("api_key", f"{key_env}: {'set' if os.environ.get(key_env) else 'NOT SET'}"
                          if key_env else "(no key env)")

    # Privileged tools live in `tools.privileged`, gated by `policy.allow_privileged`.
    # This used to read `cfg.security.privileged` — a section that does not exist,
    # so the line always printed "not even registered" no matter how the operator
    # had configured it.
    privileged = list(getattr(getattr(cfg, "tools", None), "privileged", []) or [])
    allowed = bool(getattr(getattr(cfg, "policy", None), "allow_privileged", False))
    probe = getattr(getattr(cfg, "policy", None), "sandbox_probe", None)
    if not privileged:
        r.info("privileged_ops", "[]  (none declared in tools.privileged)")
    else:
        r.warn("privileged_ops",
               f"{', '.join(privileged)} declared; allow_privileged={allowed}, "
               f"sandbox_probe={'set' if probe else 'NOT SET -> the gate denies'}")

    if not base:
        r.bad("provider", "no base_url configured")
        return False
    req = urllib.request.Request(f"{base}/models", headers={"Accept": "application/json"})
    key = inline_key or (os.environ.get(key_env) if key_env else "")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        r.warn("provider", f"HTTP {exc.code} from /models")
        return False
    except Exception as exc:  # noqa: BLE001 — unreachable is the finding
        r.bad("provider", f"{type(exc).__name__}: unreachable at {base}")
        r.info("fix", "start vLLM/Ollama and set TCAD_LLM_BASE_URL / TCAD_LLM_MODEL")
        return False
    names = []
    try:
        payload = json.loads(body)
        data = payload.get("data") if isinstance(payload, dict) else payload
        names = [str(item.get("id")) for item in (data or []) if isinstance(item, dict)]
    except Exception:  # noqa: BLE001 — some providers do not serve /models as JSON
        pass
    r.ok("provider", "/models answered" +
         (f": {', '.join(names[:5])}" + ("…" if len(names) > 5 else "") if names else ""))

    # …and now the question that actually matters. `GET /models` can answer 200
    # while every completion is refused (an account with no balance answers 200
    # there and 402 to every chat request), so a verdict based on it says
    # "real-LLM e2e RUNNABLE" for a provider that cannot serve a single token.
    can, why = _can_complete(base, str(getattr(llm, "model", "") or ""), key)
    if can:
        r.ok("provider:can_complete", "a 1-token completion was answered")
        r.warn("provider:capability",
               "serving a token is not the same as driving a tool loop — the e2e "
               "test decides that")
    else:
        r.warn("provider:can_complete",
               f"cannot serve a completion: {why}. Layer 3 will report BLOCKED, "
               f"not a product failure")
    return can


def _can_complete(base: str, model: str, key: str, *, timeout_s: float = 20.0) -> tuple[bool, str]:
    """``(usable, why)`` — can this provider answer one minimal completion?

    Deliberately the same classification the layer-3 e2e pre-flight uses, because
    the two grew apart once already: the e2e probe called this "BLOCKED" while the
    doctor's verdict, reading the same provider, called it "RUNNABLE".
    """
    if not model:
        return False, "no model configured"
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": "ping"}],
                       "max_tokens": 1}).encode()
    req = urllib.request.Request(f"{base}/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            if resp.status == 200:
                return True, ""
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = (json.loads(exc.read()).get("error") or {}).get("message") or ""
        except Exception:  # noqa: BLE001
            detail = ""
        hint = {401: "credential rejected", 402: "credential valid but the account "
                                                 "cannot pay for a request",
                404: f"no model named {model!r}", 429: "rate limited"}.get(
            exc.code, "the provider refused the request")
        return False, f"HTTP {exc.code} — {hint}{': ' + detail if detail else ''}"
    except Exception as exc:  # noqa: BLE001 — unreachable is the finding
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fast", action="store_true", help="skip the FreeCAD worker startup")
    args = ap.parse_args()

    r = Report()
    supervisor_ok = check_supervisor(r)
    cfg = None
    if supervisor_ok:
        from tcad.config.loader import load_default_config
        cfg = load_default_config()
    worker_ok = check_worker(r, cfg, args.fast) if cfg else False
    provider_ok = check_provider(r, cfg) if cfg else False

    print(LINE)
    print("verdict")
    print(LINE)
    print(f"unit tests (tests/unit)               {'RUNNABLE' if supervisor_ok else 'BLOCKED'}")
    print(f"real-kernel tests (tests/contract)    {'RUNNABLE' if worker_ok else 'BLOCKED'}")
    print(f"real-LLM e2e (tests/e2e)              "
          f"{'RUNNABLE if the model can drive tools' if (worker_ok and provider_ok) else 'BLOCKED'}")
    if not supervisor_ok:
        return 1
    if not worker_ok:
        return 2
    if not provider_ok:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
