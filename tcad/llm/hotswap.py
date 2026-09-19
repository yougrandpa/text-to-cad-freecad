"""Switching the model without restarting anything.

The problem this solves
-----------------------
The engine reads ``services.llm`` at call time (``engine.py:184``). Rebuilding
the whole service stack to change models would also tear down the FreeCAD worker
process — seconds of startup, plus any in-process state. So instead of replacing
``services``, we replace the object *behind* it:

    engine ──reads──> services.llm  (the same object forever)
                            │
                            └─ HotSwapLlm._client ──(configure: build new, then swap)──> OpenAIClient

Consequences worth stating explicitly:

* the engine needs no change and cannot hold a stale client — it never caches one;
* the swap is a single attribute assignment, so a request in flight either used
  the old client entirely or the new one entirely, never a mix;
* **build first, swap second** — if the new client cannot be constructed, the old
  one stays live and the harness keeps working. A failed reconfigure must not
  leave you with no model at all.

``probe_llm`` exists because "is this configuration usable?" deserves a direct
answer rather than a guess from the first turn's failure.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Protocol

from pydantic import BaseModel, Field

from tcad.config.providers import get_provider
from tcad.config.settings import LlmSettings
from tcad.llm.client import LlmReply, TokenUsage

# ══════════════════════════════════════════════════════════════════════════
# probing
# ══════════════════════════════════════════════════════════════════════════


class ProbeResult(BaseModel):
    ok: bool = False
    provider: str = ""
    base_url: str = ""
    model: str = ""
    latency_ms: float | None = None
    models: list[str] = Field(default_factory=list)
    """Live model list, when the provider exposes one. The UI prefers this over
    the preset's stale candidate names."""
    method: str = ""
    """``models.list`` | ``chat`` | ``""`` — how we established reachability."""
    via_proxy: bool = False
    error: str | None = None
    error_type: str | None = None
    detail: str = ""


def _transport(settings: LlmSettings, *, timeout_s: float) -> Any:
    """An httpx client honouring (or deliberately ignoring) the ambient proxy."""
    import httpx

    if settings.use_env_proxy:
        return httpx.AsyncClient(timeout=timeout_s, trust_env=True)
    return httpx.AsyncClient(timeout=timeout_s, trust_env=False)


def build_client(settings: LlmSettings, *, timeout_s: float | None = None):
    """Construct a concrete client for these settings.

    Kept as a module-level function so tests can substitute it, and so the
    settings -> client mapping lives in exactly one place.

    **Refuses an empty endpoint.** ``AsyncOpenAI`` with no ``base_url`` silently
    defaults to ``api.openai.com`` — so a user who picks "custom", leaves the
    field blank and thinks they are talking to a local server would be sending
    their prompts (and their key) to OpenAI. A refusal is the only acceptable
    behaviour here.
    """
    from tcad.llm.client import OpenAIClient

    base = settings.resolved_base_url()
    if not base:
        raise ValueError(
            "base_url 为空：请选择供应商，或填写一个 OpenAI 兼容端点。"
            "（不能留空——SDK 会静默回退到 api.openai.com）"
        )

    timeout = float(timeout_s if timeout_s is not None else settings.request_timeout_s)
    return OpenAIClient(
        base_url=base,
        api_key=settings.resolved_api_key() or "EMPTY",
        model=settings.resolved_model(),
        request_timeout_s=timeout,
        max_retries=int(settings.max_retries),
        temperature=float(settings.temperature),
        max_tokens=int(settings.max_tokens_per_step),
        http_client=_transport(settings, timeout_s=timeout),
    )


def _describe_failure(settings: LlmSettings, exc: Exception) -> str:
    """A failure message that names the proxy question.

    Checked against this machine specifically: with ``HTTPS_PROXY`` set to a
    proxy that cannot reach the endpoint, the SDK reports a connection error
    that reads like the *provider* is down. Saying which transport was used
    removes that ambiguity in one line.
    """
    transport = (
        "经环境代理 (HTTP(S)_PROXY)"
        if settings.use_env_proxy
        else "直连（已忽略环境代理）"
    )
    return f"{type(exc).__name__}: {exc} [{transport}, base_url={settings.resolved_base_url()}]"


async def probe_llm(
    settings: LlmSettings, *, timeout_s: float = 15.0
) -> ProbeResult:
    """Establish whether these settings can actually reach a model.

    Two-step, cheapest first. ``GET /models`` is one request and also yields the
    live model list; only if that is unavailable do we spend a real (1-token)
    completion to prove the chat path works. A provider that answers ``/models``
    but cannot complete is still reported as failing the chat fallback.
    """
    base = settings.resolved_base_url()
    result = ProbeResult(
        provider=settings.provider,
        base_url=base,
        model=settings.resolved_model(),
        via_proxy=settings.use_env_proxy,
    )
    if not base:
        result.error = "base_url 为空 — 请先选择供应商或填写端点"
        return result

    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url=base,
        api_key=settings.resolved_api_key() or "EMPTY",
        timeout=timeout_s,
        max_retries=0,
        http_client=_transport(settings, timeout_s=timeout_s),
    )

    errors: list[str] = []
    try:
        t0 = time.perf_counter()
        listing = await client.models.list()
        result.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        try:
            result.models = sorted(
                {str(m.id) for m in getattr(listing, "data", []) if getattr(m, "id", None)}
            )
        except Exception:  # noqa: BLE001 — a listing we cannot parse is not fatal
            result.models = []
        if result.models:
            result.ok = True
            result.method = "models.list"
            return result
        errors.append("models.list 返回空列表")
    except Exception as exc:  # noqa: BLE001
        errors.append(_describe_failure(settings, exc))

    if not result.model:
        result.error = "; ".join(errors) or "no model configured"
        result.error_type = "no_model"
        result.detail = "端点可达性未能确认，且未指定模型名"
        return result

    try:
        t0 = time.perf_counter()
        resp = await client.chat.completions.create(
            model=result.model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        result.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        if getattr(resp, "choices", None):
            result.ok = True
            result.method = "chat"
            result.detail = "模型列表不可用，已用一次最小补全确认可用"
            return result
        errors.append("chat 返回空 choices")
    except Exception as exc:  # noqa: BLE001
        errors.append(_describe_failure(settings, exc))

    result.ok = False
    result.error = "; ".join(errors)
    result.error_type = "unreachable"
    return result


# ══════════════════════════════════════════════════════════════════════════


class _Swappable(Protocol):
    async def chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        temperature: float | None = None,
    ) -> LlmReply: ...


ClientFactory = Callable[[LlmSettings], _Swappable]


class HotSwapLlm:
    """An :class:`~tcad.llm.client.LlmClient` whose backing client is replaceable.

    Satisfies the same Protocol the engine consumes, so nothing upstream knows
    it is swappable.
    """

    def __init__(
        self,
        settings: LlmSettings,
        *,
        client: _Swappable | None = None,
        factory: ClientFactory | None = None,
    ) -> None:
        self._factory: ClientFactory = factory or build_client
        self._settings = settings
        # ``client`` is for tests and for a caller that already has one.
        self._client: _Swappable = (
            client if client is not None else self._factory(settings)
        )
        self._generation = 0

    # ── introspection ─────────────────────────────────────────────────────

    @property
    def settings(self) -> LlmSettings:
        return self._settings

    @property
    def client(self) -> _Swappable:
        return self._client

    @property
    def generation(self) -> int:
        """Increments on every successful swap. Lets a caller prove a swap
        happened without reaching into private state."""
        return self._generation

    @property
    def descriptor(self) -> dict:
        preset = get_provider(self._settings.provider)
        return {
            "provider": self._settings.provider,
            "provider_label": preset.label if preset else self._settings.provider,
            "model": self._settings.resolved_model(),
            "base_url": self._settings.resolved_base_url(),
            "temperature": self._settings.temperature,
            "generation": self._generation,
        }

    # ── the swap ──────────────────────────────────────────────────────────

    def configure(self, settings: LlmSettings) -> None:
        """Point at a new configuration.

        Builds the replacement **before** touching ``self._client``: if
        construction raises, the previous client remains in place and the
        harness keeps working with the old model rather than with none.
        """
        new_client = self._factory(settings)
        old = self._client
        self._client = new_client
        self._settings = settings
        self._generation += 1
        if old is not new_client:
            _close_quietly(old)

    def adopt(self, settings: LlmSettings, client: _Swappable) -> None:
        """Install a caller-supplied client (tests, or a pre-warmed connection)."""
        old = self._client
        self._client = client
        self._settings = settings
        self._generation += 1
        if old is not client:
            _close_quietly(old)

    # ── LlmClient ─────────────────────────────────────────────────────────

    async def chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        temperature: float | None = None,
    ) -> LlmReply:
        client = self._client
        return await client.chat(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
        )

    async def aclose(self) -> None:
        await _aclose_quietly(self._client)


# ══════════════════════════════════════════════════════════════════════════
# teardown helpers — never let cleanup break a live turn
# ══════════════════════════════════════════════════════════════════════════


def _close_quietly(client: Any) -> None:
    closer = getattr(client, "aclose", None) or getattr(client, "close", None)
    if closer is None:
        return
    try:
        result = closer()
        if hasattr(result, "__await__"):
            import asyncio

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is None:
                asyncio.run(result)
            else:  # pragma: no cover — a live loop means the GC will handle it
                result.close()
    except Exception:  # noqa: BLE001
        pass


async def _aclose_quietly(client: Any) -> None:
    for name in ("aclose", "close"):
        closer = getattr(client, name, None)
        if closer is None:
            continue
        try:
            result = closer()
            if hasattr(result, "__await__"):
                await result
            return
        except Exception:  # noqa: BLE001
            return


__all__ = [
    "HotSwapLlm",
    "ProbeResult",
    "build_client",
    "probe_llm",
    "TokenUsage",
]
