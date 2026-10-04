"""The request guard uses the configured model window, including YAML overrides."""

from types import SimpleNamespace

import pytest

from tcad.config.schema import Config
from tcad.config.settings import (
    LlmSettings,
    RuntimeSettings,
    apply_to_config,
    effective,
    from_config,
    save_runtime_settings,
)
from tcad.core.wiring import apply_llm_settings
from tcad.loop.engine import LoopConfig
from tcad.server.app import LlmSettingsPatch, _merge_llm


@pytest.mark.parametrize("endpoint", [
    "http://127.0.0.1:8000/v1",
    "http://127.0.0.1:11434/v1",
    "https://custom-provider.invalid/v1",
])
def test_yaml_context_window_is_not_replaced_by_provider_preset(endpoint):
    cfg = Config()
    cfg.llm.base_url = endpoint
    cfg.context.window_tokens = 8192

    settings = from_config(cfg)

    assert settings.llm.context_window == 8192
    assert settings.llm.resolved_context_window() == 8192
    assert apply_to_config(cfg, settings).context.window_tokens == 8192


@pytest.mark.parametrize("saved_window, expected", [(16384, 16384), (None, 32768)])
def test_persisted_context_settings_still_override_yaml(tmp_path, saved_window, expected):
    cfg = Config()
    cfg.context.window_tokens = 8192
    assert effective(cfg, tmp_path).llm.resolved_context_window() == 8192
    save_runtime_settings(
        tmp_path,
        RuntimeSettings(llm=LlmSettings(provider="ollama", context_window=saved_window)),
    )

    assert effective(cfg, tmp_path).llm.resolved_context_window() == expected


def test_ui_provider_change_resets_yaml_pin_to_new_provider_window():
    cfg = Config()
    cfg.context.window_tokens = 8192
    initial = from_config(cfg).llm

    changed = _merge_llm(initial, LlmSettingsPatch(provider="ollama"))

    assert changed.context_window is None
    assert changed.resolved_context_window() == 32768
    explicit = _merge_llm(initial, LlmSettingsPatch(provider="ollama", context_window=16384))
    assert explicit.resolved_context_window() == 16384


def test_live_request_guard_tracks_yaml_and_ui_windows_without_network():
    class FakeClient:
        descriptor = {"model": "test"}

        def configure(self, settings):
            self.settings = settings

    cfg = Config()
    cfg.context.window_tokens = 8192
    services = SimpleNamespace(llm=FakeClient(), loop_config=LoopConfig(), config=cfg)

    apply_llm_settings(services, from_config(cfg), persist=False)

    assert services.loop_config.context_window_tokens == 8192
    assert services.config.context.window_tokens == 8192
    switched = RuntimeSettings(llm=LlmSettings(provider="ollama"))
    apply_llm_settings(services, switched, persist=False)
    assert services.loop_config.context_window_tokens == 32768
    assert services.config.context.window_tokens == 32768
