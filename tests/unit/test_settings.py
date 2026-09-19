"""Runtime settings: precedence, permissions, and never echoing a credential."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from tcad.config.loader import load_default_config
from tcad.config.settings import (
    SETTINGS_FILENAME,
    LlmSettings,
    RuntimeSettings,
    SettingsError,
    apply_to_config,
    effective,
    from_config,
    load_runtime_settings,
    mask_secret,
    save_runtime_settings,
    settings_path,
)

# ══════════════════════════════════════════════════════════════════════════
# masking — the property that protects a pasted key
# ══════════════════════════════════════════════════════════════════════════


def test_mask_never_reveals_a_usable_key():
    key = "sk-1234567890abcdefghijklmnop"
    masked = mask_secret(key)
    assert masked != key
    # the middle must be gone; prefix/suffix alone cannot be used
    assert "4567890abcdefghi" not in masked
    assert masked.startswith("sk-")
    assert masked.endswith("mnop")


def test_mask_of_short_or_absent_secrets_is_opaque():
    assert mask_secret(None) == ""
    assert mask_secret("") == ""
    assert mask_secret("short") == "•" * 8
    assert mask_secret("12345678") == "•" * 8  # exactly the boundary


def test_masked_shape_contains_no_secret_value():
    s = LlmSettings(provider="deepseek", api_key="sk-super-secret-value-9f2a")
    out = s.masked()
    blob = json.dumps(out)
    assert "super-secret" not in blob
    assert "9f2a" in blob          # the hint survives
    assert out["api_key_set"] is True
    assert out["api_key_source"] == "settings"


# ══════════════════════════════════════════════════════════════════════════
# resolution
# ══════════════════════════════════════════════════════════════════════════


def test_preset_fields_are_inherited_when_blank():
    s = LlmSettings(provider="deepseek")
    assert s.resolved_base_url() == "https://api.deepseek.com/v1"
    assert s.resolved_model() == "deepseek-v4-flash"
    assert s.resolved_api_key_env() == "DEEPSEEK_API_KEY"


def test_explicit_values_override_the_preset():
    s = LlmSettings(
        provider="deepseek",
        base_url="https://proxy.internal/v1",
        model="my-finetune",
        api_key_env="MY_KEY",
    )
    assert s.resolved_base_url() == "https://proxy.internal/v1"
    assert s.resolved_model() == "my-finetune"
    assert s.resolved_api_key_env() == "MY_KEY"


def test_api_key_precedence_settings_then_env_then_missing(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    from_env = LlmSettings(provider="deepseek")
    assert from_env.resolved_api_key() == "from-env"
    assert from_env.api_key_source() == "env:DEEPSEEK_API_KEY"

    explicit = LlmSettings(provider="deepseek", api_key="from-settings")
    assert explicit.resolved_api_key() == "from-settings"
    assert explicit.api_key_source() == "settings"

    monkeypatch.delenv("DEEPSEEK_API_KEY")
    missing = LlmSettings(provider="deepseek")
    assert missing.resolved_api_key() == ""
    assert missing.api_key_source() == "missing"


def test_local_provider_reports_missing_key_as_not_required():
    s = LlmSettings(provider="ollama")
    assert s.resolved_api_key() == ""
    assert s.api_key_source() == "not-required"
    assert s.masked()["api_key_set"] is False


def test_temperature_bounds_are_enforced():
    with pytest.raises(Exception):
        LlmSettings(temperature=3.0)
    with pytest.raises(Exception):
        LlmSettings(temperature=-0.1)
    with pytest.raises(Exception):
        LlmSettings(max_tokens_per_step=0)


# ══════════════════════════════════════════════════════════════════════════
# persistence
# ══════════════════════════════════════════════════════════════════════════


def test_round_trip(tmp_path: Path):
    rs = RuntimeSettings(llm=LlmSettings(provider="deepseek", model="deepseek-v4-pro"))
    path = save_runtime_settings(tmp_path, rs)
    assert path.name == SETTINGS_FILENAME
    loaded = load_runtime_settings(tmp_path)
    assert loaded is not None
    assert loaded.llm.provider == "deepseek"
    assert loaded.llm.model == "deepseek-v4-pro"


def test_settings_file_is_not_world_readable(tmp_path: Path):
    """A pasted key on disk is a bounded risk only if the file is 0600."""
    rs = RuntimeSettings(llm=LlmSettings(provider="deepseek", api_key="sk-secret"))
    path = save_runtime_settings(tmp_path, rs)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_atomic_write_leaves_no_temp_file(tmp_path: Path):
    save_runtime_settings(tmp_path, RuntimeSettings())
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_absent_file_is_none_not_an_error(tmp_path: Path):
    assert load_runtime_settings(tmp_path) is None
    assert not settings_path(tmp_path).exists()


def test_corrupt_file_raises_rather_than_silently_defaulting(tmp_path: Path):
    """Silently replacing a broken file would discard the user's key with no
    trace. The caller must get a chance to say so."""
    settings_path(tmp_path).write_text("{not json", encoding="utf-8")
    with pytest.raises(SettingsError, match="not valid JSON"):
        load_runtime_settings(tmp_path)


def test_schema_mismatch_raises(tmp_path: Path):
    settings_path(tmp_path).write_text(json.dumps({"llm": {"temperature": 99}}), encoding="utf-8")
    with pytest.raises(SettingsError, match="settings schema"):
        load_runtime_settings(tmp_path)


def test_non_object_json_raises(tmp_path: Path):
    settings_path(tmp_path).write_text("[1,2,3]", encoding="utf-8")
    with pytest.raises(SettingsError, match="JSON object"):
        load_runtime_settings(tmp_path)


# ══════════════════════════════════════════════════════════════════════════
# precedence against the YAML config
# ══════════════════════════════════════════════════════════════════════════


def test_from_config_reverse_maps_the_provider(tmp_path: Path):
    cfg = load_default_config()
    cfg.llm.base_url = "http://127.0.0.1:8000/v1"
    cfg.llm.model = "qwen2.5-72b-instruct"
    rs = from_config(cfg)
    # the YAML predates providers; it must not surface as "custom"
    assert rs.llm.provider == "vllm"
    assert rs.llm.model == "qwen2.5-72b-instruct"


def test_snapshot_wins_over_yaml_and_deleting_it_restores_yaml(tmp_path: Path):
    cfg = load_default_config()
    cfg.llm.base_url = "http://127.0.0.1:8000/v1"
    cfg.llm.model = "from-yaml"

    assert effective(cfg, tmp_path).llm.model == "from-yaml"

    save_runtime_settings(
        tmp_path, RuntimeSettings(llm=LlmSettings(provider="deepseek", model="from-settings"))
    )
    assert effective(cfg, tmp_path).llm.model == "from-settings"

    settings_path(tmp_path).unlink()
    assert effective(cfg, tmp_path).llm.model == "from-yaml"


# ══════════════════════════════════════════════════════════════════════════
# bridging into Config
# ══════════════════════════════════════════════════════════════════════════


def test_apply_to_config_returns_a_copy():
    """Anything already holding the old config (the Gate, the budget) must not
    observe a change mid-turn."""
    cfg = load_default_config()
    before = cfg.llm.model
    new = apply_to_config(cfg, RuntimeSettings(llm=LlmSettings(provider="deepseek")))
    assert cfg.llm.model == before, "the original config was mutated"
    assert new.llm.model == "deepseek-v4-flash"
    assert new.llm.base_url == "https://api.deepseek.com/v1"


def test_apply_to_config_syncs_the_context_window():
    """The budget's window size is a property of the chosen model, so it must
    travel with it — otherwise the degradation thresholds are calibrated
    against some other model's context."""
    cfg = load_default_config()
    new = apply_to_config(cfg, RuntimeSettings(llm=LlmSettings(provider="ollama")))
    assert new.context.window_tokens == 32_768


def test_context_window_inherits_from_the_preset_but_can_be_pinned():
    assert LlmSettings(provider="ollama").resolved_context_window() == 32_768
    assert LlmSettings(provider="deepseek").resolved_context_window() == 128_000
    # an explicit value wins, including one equal to another preset's default
    pinned = LlmSettings(provider="ollama", context_window=200_000)
    assert pinned.resolved_context_window() == 200_000
    assert pinned.masked()["context_window"] == 200_000


def test_apply_to_config_writes_the_resolved_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    cfg = load_default_config()
    new = apply_to_config(cfg, RuntimeSettings(llm=LlmSettings(provider="deepseek")))
    assert new.llm.api_key == "env-key"
    assert new.llm.api_key_env == "DEEPSEEK_API_KEY"


def test_apply_to_config_does_not_require_the_real_environment(tmp_path: Path):
    """`os.environ` is global state; make sure a resolved-empty key does not
    crash the copy."""
    cfg = load_default_config()
    new = apply_to_config(cfg, RuntimeSettings(llm=LlmSettings(provider="deepseek")))
    assert new.llm.api_key is None or isinstance(new.llm.api_key, str)
    assert os.environ.get("DEEPSEEK_API_KEY") in (None, "") or isinstance(new.llm.api_key, str)
