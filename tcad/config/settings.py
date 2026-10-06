"""Runtime-mutable settings — the layer that makes the model configurable
without editing YAML or restarting the process.

The precedence rule, stated once so it cannot drift:

    settings.json (explicit, written by the UI)  >  YAML / environment (defaults)

``settings.json`` is a **complete snapshot**: once it exists, it is authoritative
for the LLM section, and deleting the file returns control to the YAML. The
alternative — per-field merge — sounds friendlier but produces a configuration
nobody can reason about ("I changed the YAML and nothing happened"). A snapshot
with a documented escape hatch is debuggable; a merge lattice is not.

Credentials
-----------
The API key is written to ``settings.json`` as plain text with mode ``0600``
(and its parent directory ``0700``). That is a deliberate, bounded choice for a
single-user machine: the alternative is not storing it at all and making the user
re-export an environment variable on every launch. Two properties are
non-negotiable and enforced here and in the server:

* the key is **never** returned to a client — every outward shape is masked;
* the environment variable name is stored alongside it, so a key can always be
  supplied out-of-band instead of on disk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from tcad.config.providers import get_provider, guess_provider, opencode_chat_base_url
from tcad.config.schema import Config

SETTINGS_FILENAME = "settings.json"


class SettingsError(RuntimeError):
    """A settings file that exists but cannot be trusted."""


# ══════════════════════════════════════════════════════════════════════════


class LlmSettings(BaseModel):
    """The whole LLM configuration the harness can change at runtime."""

    provider: str = "custom"
    model: str = ""
    base_url: str = ""
    """Empty => inherit the preset's base_url."""
    api_key: str | None = None
    """Explicit credential. Never echoed to a client (see :meth:`masked`)."""
    api_key_env: str = ""
    """Empty => inherit the preset's variable name."""
    temperature: float = 0.2
    max_tokens_per_step: int = 4096
    request_timeout_s: float = 120.0
    max_retries: int = 2
    use_env_proxy: bool = False
    context_window: int | None = None
    """``None`` => inherit the preset. Must stay Optional: a default of 128000
    would be indistinguishable from a user explicitly choosing 128000, and the
    budget's degradation thresholds are calibrated against this number."""
    supports_vision: bool | None = None
    """None inherits the provider preset; explicit values describe this model."""

    @field_validator("temperature")
    @classmethod
    def _temp_range(cls, v: float) -> float:
        if not 0.0 <= v <= 2.0:
            raise ValueError(f"temperature must be within [0, 2], got {v}")
        return v

    @field_validator("max_tokens_per_step")
    @classmethod
    def _positive_tokens(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"max_tokens_per_step must be positive, got {v}")
        return v

    # ── resolution against the preset ─────────────────────────────────────

    def resolved_base_url(self) -> str:
        if self.base_url.strip():
            return opencode_chat_base_url(self.base_url)
        preset = get_provider(self.provider)
        return preset.base_url if preset else ""

    def resolved_api_key_env(self) -> str:
        if self.api_key_env.strip():
            return self.api_key_env.strip()
        preset = get_provider(self.provider)
        return preset.api_key_env if preset else ""

    def resolved_model(self) -> str:
        if self.model.strip():
            return self.model.strip()
        preset = get_provider(self.provider)
        return preset.default_model if preset else ""

    def resolved_context_window(self) -> int:
        if self.context_window is not None and self.context_window > 0:
            return int(self.context_window)
        preset = get_provider(self.provider)
        return int(preset.context_window) if preset else 128_000

    def resolved_api_key(self) -> str:
        """Explicit key, else the environment, else empty.

        Returns ``""`` rather than a sentinel like ``"EMPTY"``: the OpenAI SDK
        wants *something* for local servers, and that substitution is the
        client's business, not the settings layer's.
        """
        if self.api_key and self.api_key.strip():
            return self.api_key.strip()
        env = self.resolved_api_key_env()
        if env:
            return os.environ.get(env, "").strip()
        return ""

    def needs_key(self) -> bool:
        preset = get_provider(self.provider)
        if preset is not None and not preset.needs_key:
            return False
        # An explicit base_url overrides a preset's stance only in the
        # permissive direction: if the preset says a key is required, it is.
        return bool(preset.needs_key) if preset else False

    # ── outward shapes ────────────────────────────────────────────────────

    def resolved_supports_vision(self) -> bool:
        if self.supports_vision is not None:
            return self.supports_vision
        preset = get_provider(self.provider)
        return bool(preset.supports_vision) if preset else False

    def masked(self) -> dict:
        """Everything a client may see. The key becomes a hint, never a value."""
        preset = get_provider(self.provider)
        return {
            "provider": self.provider,
            "provider_label": preset.label if preset else self.provider,
            "model": self.resolved_model(),
            "base_url": self.resolved_base_url(),
            "temperature": self.temperature,
            "max_tokens_per_step": self.max_tokens_per_step,
            "request_timeout_s": self.request_timeout_s,
            "max_retries": self.max_retries,
            "use_env_proxy": self.use_env_proxy,
            "context_window": self.resolved_context_window(),
            "api_key_env": self.resolved_api_key_env(),
            "api_key_set": bool(self.resolved_api_key()),
            "api_key_masked": mask_secret(self.resolved_api_key()),
            "api_key_source": self.api_key_source(),
            "supports_vision": self.resolved_supports_vision(),
            "supports_vision_override": self.supports_vision,
            "needs_key": self.needs_key(),
        }

    def api_key_source(self) -> str:
        """Where the effective credential came from — the single most useful
        debugging fact when a request is rejected."""
        if self.api_key and self.api_key.strip():
            return "settings"
        env = self.resolved_api_key_env()
        if env and os.environ.get(env, "").strip():
            return f"env:{env}"
        if self.needs_key():
            return "missing"
        return "not-required"


def mask_secret(value: str | None) -> str:
    """``sk-abc…f9a2`` — enough to confirm *which* key, never enough to use it."""
    if not value:
        return ""
    v = value.strip()
    if len(v) <= 8:
        return "•" * 8
    return f"{v[:3]}…{v[-4:]}"


class RuntimeSettings(BaseModel):
    version: int = 1
    llm: LlmSettings = Field(default_factory=LlmSettings)


# ══════════════════════════════════════════════════════════════════════════
# persistence
# ══════════════════════════════════════════════════════════════════════════


def settings_path(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir) / SETTINGS_FILENAME


def load_runtime_settings(data_dir: str | os.PathLike[str]) -> RuntimeSettings | None:
    """``None`` when absent; :class:`SettingsError` when present but broken.

    A corrupt settings file is NOT silently replaced by defaults: that would
    discard an API key the user pasted, with no trace. The caller decides.
    """
    path = settings_path(data_dir)
    if not path.exists():
        return None
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SettingsError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise SettingsError(f"{path} must contain a JSON object")
    try:
        return RuntimeSettings.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 — surface pydantic's message verbatim
        raise SettingsError(f"{path} does not match the settings schema: {exc}") from exc


def save_runtime_settings(
    data_dir: str | os.PathLike[str], settings: RuntimeSettings
) -> Path:
    """Write atomically, then tighten permissions before publishing."""
    data_path = Path(data_dir)
    data_path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(data_path, 0o700)
    except OSError:
        pass  # best effort; a shared data dir is the operator's choice
    path = settings_path(data_path)
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps(
        settings.model_dump(mode="json"), ensure_ascii=False, indent=2
    )
    # Create with restrictive permissions from the start, so the key is never
    # briefly world-readable between write and chmod.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


# ══════════════════════════════════════════════════════════════════════════
# bridging to the YAML config
# ══════════════════════════════════════════════════════════════════════════


def from_config(cfg: Config) -> RuntimeSettings:
    """Derive settings from the YAML/env config — the pre-settings state.

    The provider is *guessed* from ``base_url`` so that a config written before
    providers existed (``http://127.0.0.1:8000/v1``) still lights up the right
    preset instead of showing a generic "custom".
    """
    preset = get_provider(guess_provider(cfg.llm.base_url))
    return RuntimeSettings(
        llm=LlmSettings(
            provider=preset.id if preset else "custom",
            model=cfg.llm.model,
            base_url=cfg.llm.base_url,
            api_key=cfg.llm.api_key,
            api_key_env=cfg.llm.api_key_env,
            temperature=float(cfg.llm.temperature),
            max_tokens_per_step=int(cfg.llm.max_tokens_per_step),
            request_timeout_s=float(cfg.llm.request_timeout_s),
            max_retries=int(cfg.llm.max_retries),
            supports_vision=cfg.llm.supports_vision,
            use_env_proxy=bool(preset.use_env_proxy) if preset else False,
            # YAML may calibrate a local/custom model below its provider's
            # generic preset. Preserve that explicit request-window budget;
            # persisted settings and later UI provider changes still own their
            # existing precedence/resolution paths.
            context_window=int(cfg.context.window_tokens),
        )
    )


def effective(
    cfg: Config, data_dir: str | os.PathLike[str]
) -> RuntimeSettings:
    """The settings actually in force: the snapshot if it exists, else the YAML."""
    stored = load_runtime_settings(data_dir)
    return stored if stored is not None else from_config(cfg)


def apply_to_config(cfg: Config, settings: RuntimeSettings) -> Config:
    """Return a copy of ``cfg`` with the LLM section replaced by ``settings``.

    A copy, not a mutation: callers that hold the old config (the Gate, the
    budget) must not observe a change mid-turn.
    """
    llm = settings.llm
    new = cfg.model_copy(deep=True)
    new.llm.base_url = llm.resolved_base_url()
    new.llm.model = llm.resolved_model()
    new.llm.api_key = llm.resolved_api_key() or None
    new.llm.api_key_env = llm.resolved_api_key_env()
    new.llm.temperature = llm.temperature
    new.llm.max_tokens_per_step = llm.max_tokens_per_step
    new.llm.request_timeout_s = llm.request_timeout_s
    new.llm.max_retries = llm.max_retries
    new.llm.supports_vision = llm.supports_vision
    new.context.window_tokens = llm.resolved_context_window()
    return new
