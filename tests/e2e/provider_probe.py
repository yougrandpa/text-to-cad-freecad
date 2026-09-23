"""Is the configured model service actually able to serve layer 3?

``GET /models`` is not the question. It can answer **200** while every completion
is refused — a key whose account has no balance does exactly that: 200 on the
model list, ``402 Insufficient Balance`` on every ``/chat/completions``. The
probe used to be that model-list call, so such a provider was reported as
"reachable", the four layer-3 tests ran, and they failed with assertions about
the *model's output* (``state=failed``, no artifacts, no tool calls) — four
messages, none of which named the actual cause.

Layer 3 answers "can a real model drive this harness". A provider that refuses
to serve is an **environment** condition, in the same class as no provider at
all: it must be reported as BLOCKED with the reason, not as a product failure.
So the probe sends one minimal completion and classifies the outcome.

Kept out of the test module so it can be unit-tested without importing FastAPI,
and so the reason string is testable without a network.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

#: One token is enough. The point is not the answer, it is whether an answer is
#: possible at all.
_PROBE_BODY = {"messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}


def probe_provider(base_url: str, model: str, api_key: str | None = None,
                   *, timeout_s: float = 20.0) -> tuple[bool, str]:
    """``(usable, reason)``. ``reason`` is empty when usable.

    Every failure path returns a reason that names what happened and says
    BLOCKED, because that string becomes the pytest skip message.
    """
    if not base_url or not model:
        return False, ("real-LLM e2e not configured: set TCAD_E2E_BASE_URL and "
                       "TCAD_E2E_MODEL (optionally TCAD_E2E_API_KEY) to run layer 3")
    payload = dict(_PROBE_BODY, model=model)
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            if r.status == 200:
                return True, ""
            return False, (f"the configured model service answered HTTP {r.status} to a "
                           f"trivial completion — environment BLOCKED, not a pass")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            body = json.loads(e.read())
            detail = (body.get("error") or {}).get("message") or ""
        except Exception:  # noqa: BLE001 — a body we cannot parse is not the point
            detail = ""
        if e.code in (401, 403):
            why = "the credential was rejected"
        elif e.code == 402:
            why = "the credential is valid but the account cannot pay for a request"
        elif e.code == 404:
            why = f"the provider has no model named {model!r}"
        elif e.code == 429:
            why = "the account is rate limited"
        else:
            why = "the provider refused the request"
        return False, (
            f"the configured model service cannot serve a completion: HTTP {e.code} "
            f"— {why}{': ' + detail if detail else ''}. Environment BLOCKED, not a pass."
        )
    except Exception as exc:  # noqa: BLE001 — unreachable is a skip, not a failure
        return False, (f"the configured model service did not answer "
                       f"({type(exc).__name__}: {exc}) — environment BLOCKED, not a pass")


def configured_provider(repo_root: Path) -> tuple[str, str, str]:
    """``(base_url, model, api_key)``, env first, then the saved UI settings.

    Layer 3 needs an explicit provider. Environment variables come first so a CI
    run can point somewhere else, but falling back to ``data/settings.json``
    means a provider already configured in the UI works without hand-wiring the
    same values again. The key is read and passed straight through; nothing here
    prints it.
    """
    base_url = os.environ.get("TCAD_E2E_BASE_URL", "")
    model = os.environ.get("TCAD_E2E_MODEL", "")
    api_key = os.environ.get("TCAD_E2E_API_KEY", "")
    if base_url and model:
        return base_url, model, api_key

    try:
        saved = json.loads((repo_root / "data" / "settings.json").read_text())
        llm = saved.get("llm") or {}
    except Exception:  # noqa: BLE001 — absent/unreadable settings mean "not configured"
        return base_url, model, api_key
    return (
        base_url or llm.get("base_url", ""),
        model or llm.get("model", ""),
        api_key or llm.get("api_key", ""),
    )
