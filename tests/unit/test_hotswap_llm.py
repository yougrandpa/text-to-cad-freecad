"""Hot-swapping the model, and honestly probing whether a configuration works.

The two properties that matter most here:
  * a failed reconfigure must leave the PREVIOUS model in place — losing the
    model entirely because of a typo would be a serious regression;
  * `probe_llm` must report which transport it used, because on this machine an
    ambient proxy can swallow a request and produce an error that names the
    provider instead of the proxy.
"""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from tcad.config.settings import LlmSettings
from tcad.llm.client import LlmReply, TokenUsage
from tcad.llm.hotswap import HotSwapLlm, ProbeResult, build_client, probe_llm

# ══════════════════════════════════════════════════════════════════════════
# a recording fake
# ══════════════════════════════════════════════════════════════════════════


class RecordingLlm:
    def __init__(self, tag: str, *, raises: Exception | None = None) -> None:
        self.tag = tag
        self.raises = raises
        self.calls: list[dict] = []

    async def chat(self, *, messages, tools=None, tool_choice=None, temperature=None):
        self.calls.append({"temperature": temperature, "tools": tools})
        if self.raises is not None:
            raise self.raises
        return LlmReply(
            text=f"reply-from-{self.tag}",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
        )


# ══════════════════════════════════════════════════════════════════════════
# swapping
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_swap_changes_which_client_answers():
    first = RecordingLlm("first")
    holder = HotSwapLlm(LlmSettings(provider="deepseek"), client=first)
    assert (await holder.chat(messages=[])).text == "reply-from-first"

    second = RecordingLlm("second")
    holder.configure(LlmSettings(provider="openai", model="gpt-4o"))
    # install the fake *after* configure so the swap itself is what we test
    holder.adopt(LlmSettings(provider="openai", model="gpt-4o"), second)
    assert (await holder.chat(messages=[])).text == "reply-from-second"
    assert first.calls and second.calls


@pytest.mark.asyncio
async def test_the_engine_holds_one_object_forever():
    """`services.llm` is read at call time and must never be replaced — that is
    the whole reason the swap is internal."""
    holder = HotSwapLlm(LlmSettings(provider="deepseek"), client=RecordingLlm("a"))
    identity = id(holder)
    holder.adopt(LlmSettings(provider="deepseek"), RecordingLlm("b"))
    assert id(holder) == identity


def test_generation_advances_on_each_successful_swap():
    holder = HotSwapLlm(LlmSettings(provider="deepseek"), client=RecordingLlm("a"))
    assert holder.generation == 0
    holder.adopt(LlmSettings(provider="deepseek", model="x"), RecordingLlm("b"))
    assert holder.generation == 1


def test_a_failed_reconfigure_keeps_the_working_model():
    """The single most important property of this class."""
    good = RecordingLlm("good")
    holder = HotSwapLlm(LlmSettings(provider="deepseek"), client=good)

    def explode(_settings):
        raise RuntimeError("cannot build client: base_url is empty")

    holder._factory = explode  # noqa: SLF001 — deliberate fault injection
    with pytest.raises(RuntimeError, match="base_url is empty"):
        holder.configure(LlmSettings(provider="custom", base_url="", model=""))

    assert holder.client is good, "the previous client was discarded on failure"
    assert holder.generation == 0


def test_settings_follow_a_successful_swap():
    holder = HotSwapLlm(LlmSettings(provider="deepseek"), client=RecordingLlm("a"))
    holder.adopt(LlmSettings(provider="ollama", model="llama3"), RecordingLlm("b"))
    assert holder.settings.provider == "ollama"
    assert holder.descriptor["model"] == "llama3"
    assert holder.descriptor["provider_label"] == "Ollama (本地)"


def test_descriptor_reports_the_resolved_values():
    holder = HotSwapLlm(LlmSettings(provider="deepseek"), client=RecordingLlm("a"))
    d = holder.descriptor
    assert d["base_url"] == "https://api.deepseek.com/v1"
    assert d["model"] == "deepseek-v4-flash"
    assert d["supports_vision"] is False


def test_descriptor_reports_provider_vision_support():
    holder = HotSwapLlm(LlmSettings(provider="openai"), client=RecordingLlm("a"))
    assert holder.descriptor["supports_vision"] is True


@pytest.mark.asyncio
async def test_chat_forwards_temperature_and_tools():
    inner = RecordingLlm("a")
    holder = HotSwapLlm(LlmSettings(provider="deepseek"), client=inner)
    tools = [{"type": "function", "function": {"name": "t"}}]
    await holder.chat(messages=[{"role": "user", "content": "x"}], tools=tools, temperature=0.7)
    assert inner.calls[0]["temperature"] == 0.7
    assert inner.calls[0]["tools"] == tools


def test_build_client_refuses_an_empty_endpoint():
    """Not a style preference: with no base_url the SDK silently targets
    api.openai.com, so a blank "custom" endpoint would ship the user's prompts
    and key to a provider they did not choose."""
    with pytest.raises(ValueError, match="api.openai.com"):
        build_client(LlmSettings(provider="custom", base_url="", model="m"))


def test_build_client_refuses_a_whitespace_endpoint():
    with pytest.raises(ValueError):
        build_client(LlmSettings(provider="custom", base_url="   ", model="m"))


def test_build_client_targets_the_resolved_base_url():
    client = build_client(LlmSettings(provider="deepseek"))
    assert client.model == "deepseek-v4-flash"


# ══════════════════════════════════════════════════════════════════════════
# probing against a real HTTP server
# ══════════════════════════════════════════════════════════════════════════


class _ModelsHandler(http.server.BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible ``GET /v1/models``."""

    payload = {
        "object": "list",
        "data": [
            {"id": "alpha-model", "object": "model", "created": 0, "owned_by": "test"},
            {"id": "beta-model", "object": "model", "created": 0, "owned_by": "test"},
        ],
    }

    def do_GET(self):  # noqa: N802 — http.server's naming
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps(self.payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *args):  # keep the test output clean
        pass


@pytest.fixture
def models_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ModelsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_probe_lists_models_from_a_live_endpoint(models_server):
    """The reason the UI can offer a real model list instead of a stale
    hard-coded one."""
    settings = LlmSettings(
        provider="custom", base_url=models_server, model="alpha-model", api_key="x"
    )
    result = await probe_llm(settings, timeout_s=5.0)
    assert isinstance(result, ProbeResult)
    assert result.ok is True
    assert result.method == "models.list"
    assert result.models == ["alpha-model", "beta-model"]
    assert result.latency_ms is not None and result.latency_ms >= 0
    assert result.error is None


@pytest.mark.asyncio
async def test_probe_failure_is_legible_and_names_the_transport():
    """A closed port on localhost: the message must say the request was direct,
    so nobody spends an hour blaming a proxy (or vice versa)."""
    settings = LlmSettings(
        provider="custom",
        base_url="http://127.0.0.1:1/v1",  # nothing listens here
        model="m",
        request_timeout_s=2.0,
    )
    result = await probe_llm(settings, timeout_s=2.0)
    assert result.ok is False
    assert result.error
    assert "直连" in result.error or "代理" in result.error
    assert result.base_url == "http://127.0.0.1:1/v1"


@pytest.mark.asyncio
async def test_probe_reports_an_empty_endpoint_without_calling_anything():
    result = await probe_llm(LlmSettings(provider="custom", base_url="", model=""))
    assert result.ok is False
    assert "base_url" in (result.error or "")


@pytest.mark.asyncio
async def test_probe_reflects_the_proxy_setting_in_its_result(models_server):
    settings = LlmSettings(
        provider="custom", base_url=models_server, model="alpha-model",
        api_key="x", use_env_proxy=True,
    )
    result = await probe_llm(settings, timeout_s=5.0)
    # trust_env=True with no proxy configured must still succeed
    assert result.ok is True
    assert result.via_proxy is True


@pytest.mark.asyncio
async def test_opencode_uses_gateway_model_list_and_chat_route(monkeypatch):
    import httpx
    import tcad.llm.hotswap as module

    requests = []

    def handle(request):
        requests.append(request)
        if request.method == "GET":
            assert str(request.url) == "https://opencode.ai/inference/v1/models"
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": model, "object": "model", "created": 0, "owned_by": "opencode"}
                for model in ["kimi-k2.6", "gpt-5.5", "claude-sonnet-4-6", "qwen3.6-plus", "gemini-3.1-pro"]
            ]})
        assert str(request.url) == "https://opencode.ai/inference/openai/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "kimi-k2.6"
        assert body["tools"][0]["function"]["name"] == "ir_get"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
            "model": "kimi-k2.6", "choices": [{"index": 0, "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": None, "tool_calls": [{"id": "call1",
            "type": "function", "function": {"name": "ir_get", "arguments": "{}"}}]}}]})

    monkeypatch.setattr(module, "_transport", lambda *a, **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(handle)))
    settings = LlmSettings(provider="opencode", base_url="https://opencode.ai/inference", api_key="test-key")
    result = await probe_llm(settings)
    assert result.ok
    assert result.models == ["kimi-k2.6"]
    client = build_client(settings)
    reply = await client.chat(messages=[{"role": "user", "content": "test"}],
        tools=[{"type": "function", "function": {"name": "ir_get", "parameters": {"type": "object"}}}])
    assert reply.tool_calls[0].name == "ir_get"
    await client._client.close()
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_openrouter_lists_models_and_roundtrips_tool_calls(monkeypatch):
    import httpx
    import tcad.llm.hotswap as module

    model = "vendor/tool-model"
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer router-test-key"
        if request.method == "GET":
            assert str(request.url) == "https://openrouter.ai/api/v1/models"
            return httpx.Response(200, json={"data": [
                {"id": model, "object": "model", "created": 0, "owned_by": "vendor",
                 "pricing": {"prompt": "0", "completion": "0"}}
            ]})
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == model
        assert body["tools"][0]["function"]["name"] == "ir_get"
        assert body["tool_choice"] == "auto"
        assert body["provider"] == {"require_parameters": True, "allow_fallbacks": True}
        return httpx.Response(200, json={"id": "reply", "object": "chat.completion", "created": 0,
            "model": model, "choices": [{"index": 0, "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": None, "tool_calls": [{"id": "call1",
            "type": "function", "function": {"name": "ir_get", "arguments": "{}"}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    monkeypatch.setattr(module, "_transport", lambda *a, **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(handle)))
    settings = LlmSettings(provider="openrouter", model=model, api_key="router-test-key")
    result = await probe_llm(settings)
    assert result.ok and result.models == [model]
    assert result.free_models == [model]
    client = build_client(settings)
    try:
        reply = await client.chat(messages=[{"role": "user", "content": "test"}],
            tools=[{"type": "function", "function": {"name": "ir_get", "parameters": {"type": "object"}}}],
            tool_choice="auto")
        assert reply.tool_calls[0].name == "ir_get"
        assert reply.tool_calls[0].args == {}
        assert reply.usage.prompt_tokens == 10
    finally:
        await client._client.close()
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_model_listing_permission_denial_does_not_attempt_inference(monkeypatch):
    import httpx
    import tcad.llm.hotswap as module

    requests = []
    def handle(request):
        requests.append(request)
        assert request.method == 'GET'
        return httpx.Response(403, json={'error': {'message': '无权访问 vip 分组', 'type': 'new_api_error'}})
    monkeypatch.setattr(module, '_transport', lambda *a, **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(handle)))
    result = await probe_llm(LlmSettings(provider='custom', base_url='https://gateway.example/v1', model='m', api_key='test'))
    assert not result.ok
    assert result.error_type == 'permission_denied'
    assert 'vip 分组' in result.error
    assert len(requests) == 1
