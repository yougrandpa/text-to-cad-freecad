"""Config loading: YAML -> pydantic, with ${ENV} interpolation and file overlays.

Deliberately dependency-light (pyyaml + pydantic only) so the server, the CLI and
the tests all load configuration the same way.

Precedence, lowest to highest:
    built-in defaults  <  config file  <  overlay files (in order)  <  env overrides
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from tcad.config.schema import Config

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "default.yaml"
STRICT_POLICY_PATH = REPO_ROOT / "configs" / "policies" / "strict.yaml"


class ConfigError(RuntimeError):
    pass


def interpolate_env(value: str, *, strict: bool = False) -> str:
    """Substitute ${VAR} and ${VAR:-default}.

    ``strict=True`` raises on an unset variable with no default; otherwise the
    literal empty string is used. Reference-style config is usually not strict —
    a missing optional key should not stop the harness from starting.
    """

    def repl(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        if strict:
            raise ConfigError(f"environment variable {name!r} is not set")
        return ""

    return _ENV_PATTERN.sub(repl, value)


def _walk(node: Any, *, strict: bool) -> Any:
    if isinstance(node, str):
        return interpolate_env(node, strict=strict)
    if isinstance(node, dict):
        return {k: _walk(v, strict=strict) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, strict=strict) for v in node]
    return node


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursive merge. Lists are replaced wholesale, not concatenated — a partial
    list merge would make deny-globs and tool whitelists impossible to reason about."""
    out = dict(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def read_yaml(path: str | Path, *, missing_ok: bool = False, strict_env: bool = False) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        if missing_ok:
            return {}
        raise ConfigError(f"config file not found: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"config file must contain a mapping at top level: {p}")
    return _walk(raw, strict=strict_env)


def load_config(
    path: str | Path | None = None,
    *,
    overlays: list[str | Path] | None = None,
    env_overrides: dict[str, Any] | None = None,
    strict_env: bool = False,
) -> Config:
    """Load, merge and validate configuration.

    Raises ``ConfigError`` for file/format problems and ``pydantic.ValidationError``
    for semantic problems — both are loud, because a silently-wrong config in a
    CAD pipeline produces silently-wrong geometry.
    """
    data: dict[str, Any] = {}
    if path is not None:
        data = read_yaml(path, strict_env=strict_env)
    for ov in overlays or []:
        data = deep_merge(data, read_yaml(ov, strict_env=strict_env))
    if env_overrides:
        data = deep_merge(data, env_overrides)
    return Config.model_validate(data)


def load_default_config(**kwargs: Any) -> Config:
    """Load ``configs/default.yaml`` (if present) merged over the built-in defaults."""
    return load_config(
        DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.exists() else None, **kwargs
    )


def resolve_paths(cfg: Config, *, root: Path | None = None) -> Config:
    """Make relative paths absolute against *root* (defaults to the repo root).

    Called once at start-up so that a worker subprocess — which may inherit a
    different cwd — still finds the FreeCAD binary and the data directory.
    """
    base = root or REPO_ROOT
    cfg.runtime.freecad_cmd = str((base / cfg.runtime.freecad_cmd).resolve())
    cfg.runtime.freecad_python_path = str(
        (base / cfg.runtime.freecad_python_path).resolve()
    )
    cfg.storage.data_dir = str((base / cfg.storage.data_dir).resolve())
    cfg.storage.sqlite_path = str((base / cfg.storage.sqlite_path).resolve())
    cfg.sandbox.writable_root = str((base / cfg.sandbox.writable_root).resolve())
    cfg.sandbox.read_only_roots = [
        str((base / r).resolve()) for r in cfg.sandbox.read_only_roots
    ]
    return cfg
