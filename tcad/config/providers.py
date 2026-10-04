"""Model provider presets.

Why this module exists
----------------------
Before it, pointing the harness at a real model meant hand-writing a ``base_url``
and exporting an environment variable whose name you had to guess. There was no
notion of "a provider" at all.

The load-bearing decision here is that a preset carries **candidate model names,
not an authoritative list**. Model names go stale, and fast — at the time of
writing DeepSeek's own documentation lists two generations side by side
(``deepseek-v4-flash`` / ``deepseek-v4-pro`` alongside ``deepseek-chat`` /
``deepseek-reasoner``). Hard-coding a model name into a preset guarantees it
eventually lies. So the real list is fetched at runtime from the provider's
``GET /models`` (see ``tcad.llm.hotswap.probe_llm``); ``models`` below is only a
fallback for providers that do not implement that endpoint.

A preset is deliberately data, not code: no credential is ever stored here, only
the *name of the environment variable* a credential would live in.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ══════════════════════════════════════════════════════════════════════════


class ProviderPreset(BaseModel):
    """Everything needed to talk to one provider, minus the credential."""

    id: str
    label: str
    base_url: str
    api_key_env: str = ""
    """Environment variable to read the credential from. Empty => no key needed."""
    needs_key: bool = True
    default_model: str = ""
    models: list[str] = Field(default_factory=list)
    """CANDIDATE names only — a fallback for providers without ``GET /models``.
    The UI prefers the runtime list and treats these as the offline default."""
    context_window: int = 128_000
    supports_tools: bool = True
    supports_vision: bool = False
    use_env_proxy: bool = False
    """Whether to honour ``HTTP(S)_PROXY`` from the environment.

    Defaults to False on purpose. This machine's environment carries a proxy that
    cannot reach every endpoint, and a request that fails because a proxy
    silently swallowed it produces an error message pointing at the *provider* —
    the most misleading possible failure. Local providers (Ollama/vLLM) and
    direct-reachable cloud providers should bypass it unless asked otherwise.
    """
    docs_url: str = ""
    notes: str = ""


# ══════════════════════════════════════════════════════════════════════════
# built-ins
# ══════════════════════════════════════════════════════════════════════════

DEEPSEEK = ProviderPreset(
    id="deepseek",
    label="DeepSeek",
    base_url="https://api.deepseek.com/v1",
    # The bare host also works (official curl uses it); /v1 is the documented
    # OpenAI-compatible spelling and is unrelated to the model version.
    api_key_env="DEEPSEEK_API_KEY",
    needs_key=True,
    default_model="deepseek-v4-flash",
    # Two generations are documented simultaneously; runtime listing wins.
    models=[
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "deepseek-chat",
        "deepseek-reasoner",
    ],
    context_window=128_000,
    supports_tools=True,
    supports_vision=False,
    use_env_proxy=False,
    docs_url="https://api-docs.deepseek.com/zh-cn",
    notes=(
        "OpenAI-compatible. Model names change between generations — the UI "
        "lists them live. Thinking mode ('deepseek-reasoner' / thinking) and "
        "tool calling together are NOT yet verified against this harness."
    ),
)

OPENAI = ProviderPreset(
    id="openai",
    label="OpenAI",
    base_url="https://api.openai.com/v1",
    api_key_env="OPENAI_API_KEY",
    needs_key=True,
    default_model="gpt-4o",
    models=["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini"],
    context_window=128_000,
    supports_tools=True,
    supports_vision=True,
    use_env_proxy=False,
    docs_url="https://platform.openai.com/docs",
)

OLLAMA = ProviderPreset(
    id="ollama",
    label="Ollama (本地)",
    base_url="http://127.0.0.1:11434/v1",
    api_key_env="",
    needs_key=False,
    default_model="qwen2.5:32b",
    models=[],
    context_window=32_768,
    supports_tools=True,
    supports_vision=False,
    use_env_proxy=False,
    docs_url="https://ollama.com",
    notes="本地推理，无需 API key。模型列表由 /models 动态取得。",
)

VLLM = ProviderPreset(
    id="vllm",
    label="vLLM / 自建 OpenAI 兼容服务",
    base_url="http://127.0.0.1:8000/v1",
    api_key_env="",
    needs_key=False,
    default_model="qwen2.5-72b-instruct",
    models=[],
    context_window=128_000,
    supports_tools=True,
    supports_vision=False,
    use_env_proxy=False,
    docs_url="",
)

OPENROUTER = ProviderPreset(
    id="openrouter",
    label="OpenRouter",
    base_url="https://openrouter.ai/api/v1",
    api_key_env="OPENROUTER_API_KEY",
    needs_key=True,
    default_model="~openai/gpt-sol-latest",
    models=["~openai/gpt-sol-latest"],
    docs_url="https://openrouter.ai/docs/quickstart",
    notes="使用 OpenAI 兼容接口；模型名使用完整 slug。请选支持工具调用的模型，图像输入能力因模型而异。",
)


OPENCODE = ProviderPreset(
    id="opencode",
    label="OpenCode",
    base_url="https://opencode.ai/inference/openai/v1",
    api_key_env="OPENCODE_API_KEY",
    needs_key=False,
    default_model="kimi-k2.6",
    models=["kimi-k2.6", "glm-5.1", "minimax-m2.7"],
    docs_url="https://opencode.ai/v2/docs/console/inference",
    notes="支持 Chat Completions 模型；付费模型需 service account key。GPT、Claude/Qwen、Gemini 需要其他协议，暂不支持。",
)


def opencode_chat_base_url(base_url: str) -> str:
    """Accept the gateway root or a full Chat Completions endpoint."""
    from urllib.parse import urlsplit

    url = urlsplit(base_url.strip())
    if url.hostname == "opencode.ai" and url.path.rstrip("/") in (
        "", "/inference", "/inference/openai/v1",
        "/inference/openai/v1/chat/completions",
    ):
        return f"{url.scheme}://{url.netloc}/inference/openai/v1"
    return base_url.strip()


CUSTOM = ProviderPreset(
    id="custom",
    label="自定义 (OpenAI 兼容)",
    base_url="",
    api_key_env="TCAD_LLM_API_KEY",
    needs_key=False,
    default_model="",
    models=[],
    context_window=128_000,
    supports_tools=True,
    supports_vision=False,
    use_env_proxy=False,
    notes="任何 OpenAI 兼容端点：填 base_url，key 可留空。",
)

PROVIDERS: dict[str, ProviderPreset] = {
    p.id: p for p in (DEEPSEEK, OPENAI, OPENROUTER, OPENCODE, OLLAMA, VLLM, CUSTOM)
}

CUSTOM_ID = CUSTOM.id


# ══════════════════════════════════════════════════════════════════════════
# lookups
# ══════════════════════════════════════════════════════════════════════════


def get_provider(provider_id: str | None) -> ProviderPreset | None:
    """Preset by id, or ``None``. Never raises — an unknown id must not be fatal,
    it just means "no preset", and the settings keep the literal values."""
    if not provider_id:
        return None
    return PROVIDERS.get(provider_id)


def require_provider(provider_id: str) -> ProviderPreset:
    preset = get_provider(provider_id)
    if preset is None:
        known = ", ".join(PROVIDERS)
        raise KeyError(f"unknown provider {provider_id!r}; known: {known}")
    return preset


def guess_provider(base_url: str | None) -> str:
    """Reverse-map a base_url to a preset id.

    Needed because the YAML config predates the notion of a provider: a config
    that says ``base_url: http://127.0.0.1:8000/v1`` should light up the vLLM
    preset in the UI rather than showing "custom".

    Matching is on the host, ignoring scheme, trailing slash and a ``/v1``
    suffix, so ``https://api.deepseek.com`` and ``https://api.deepseek.com/v1``
    both resolve to ``deepseek``.
    """
    if not base_url:
        return CUSTOM_ID
    host = _normalise_host(opencode_chat_base_url(base_url))
    for preset in PROVIDERS.values():
        if preset.base_url and _normalise_host(preset.base_url) == host:
            return preset.id
    # localhost heuristics: a bare local port is almost always a self-hosted
    # OpenAI-compatible server rather than something exotic.
    if host.startswith("127.0.0.1") or host.startswith("localhost"):
        if host.endswith("11434"):
            return OLLAMA.id
        return VLLM.id
    if "api.deepseek.com" in host:
        return DEEPSEEK.id
    if "api.openai.com" in host:
        return OPENAI.id
    return CUSTOM_ID


def _normalise_host(base_url: str) -> str:
    s = base_url.strip().lower()
    for scheme in ("https://", "http://"):
        if s.startswith(scheme):
            s = s[len(scheme) :]
    s = s.split("?", 1)[0].split("#", 1)[0]
    s = s.rstrip("/")
    if s.endswith("/v1"):
        s = s[: -len("/v1")]
    return s.rstrip("/")


def public_providers() -> list[dict]:
    """Preset list for the UI. Contains no credentials, by construction."""
    return [p.model_dump() for p in PROVIDERS.values()]
