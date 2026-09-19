"""Provider presets: the mapping from a base_url to something a human picks."""

from __future__ import annotations

import pytest

from tcad.config.providers import (
    CUSTOM_ID,
    PROVIDERS,
    get_provider,
    guess_provider,
    public_providers,
    require_provider,
)


def test_builtin_providers_are_addressable():
    for pid in ("deepseek", "openai", "ollama", "vllm", "custom"):
        assert pid in PROVIDERS
        assert get_provider(pid) is not None


@pytest.mark.parametrize(
    "base_url,expected",
    [
        # DeepSeek documents both spellings; both must land on the same preset.
        ("https://api.deepseek.com", "deepseek"),
        ("https://api.deepseek.com/v1", "deepseek"),
        ("https://api.deepseek.com/v1/", "deepseek"),
        ("  HTTPS://API.DEEPSEEK.COM/V1  ", "deepseek"),
        ("https://api.openai.com/v1", "openai"),
        ("http://127.0.0.1:11434/v1", "ollama"),
        ("http://localhost:11434/v1", "ollama"),
        ("http://127.0.0.1:8000/v1", "vllm"),
        # A self-hosted server on an unknown port is a vLLM-shaped thing, not
        # "custom" — guessing vLLM gives the user a correct default model name.
        ("http://127.0.0.1:9999/v1", "vllm"),
        ("https://my-endpoint.example.com/v1", CUSTOM_ID),
        ("", CUSTOM_ID),
        (None, CUSTOM_ID),
    ],
)
def test_guess_provider(base_url, expected):
    assert guess_provider(base_url) == expected


def test_deepseek_preset_carries_the_documented_facts():
    ds = PROVIDERS["deepseek"]
    assert ds.base_url.startswith("https://api.deepseek.com")
    assert ds.api_key_env == "DEEPSEEK_API_KEY"
    assert ds.needs_key is True
    assert ds.context_window == 128_000
    assert ds.default_model
    assert ds.default_model in ds.models
    # both documented generations must be offered as fallbacks
    assert "deepseek-chat" in ds.models


def test_local_providers_do_not_require_a_key():
    assert PROVIDERS["ollama"].needs_key is False
    assert PROVIDERS["vllm"].needs_key is False


def test_proxy_is_off_by_default_for_every_preset():
    """An ambient proxy that cannot reach the endpoint is the worst failure
    mode available, so opting in must be explicit."""
    for preset in PROVIDERS.values():
        assert preset.use_env_proxy is False, preset.id


def test_public_providers_leak_no_credentials():
    """The outward shape is built from the model, so this is really a schema
    lock: if a preset ever grows a secret field, this test is what notices."""
    payload = public_providers()
    assert len(payload) == len(PROVIDERS)
    allowed = {
        "id", "label", "base_url", "api_key_env", "needs_key", "default_model",
        "models", "context_window", "supports_tools", "supports_vision",
        "use_env_proxy", "docs_url", "notes",
    }
    for entry in payload:
        assert set(entry) <= allowed, set(entry) - allowed
        # the environment variable NAME may be public; a value never is
        assert isinstance(entry["api_key_env"], str)


def test_require_provider_names_the_alternatives():
    with pytest.raises(KeyError) as ei:
        require_provider("nope")
    assert "deepseek" in str(ei.value)


def test_get_provider_tolerates_unknown_and_empty():
    """An unknown id must not be fatal — it just means 'no preset', and the
    caller keeps whatever literal values the settings hold."""
    assert get_provider("nope") is None
    assert get_provider("") is None
    assert get_provider(None) is None
