"""Config layer contract tests.

A wrong config in a CAD pipeline produces wrong geometry quietly, so these tests
lean on "fails loudly" behaviour.
"""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from tcad.config.loader import (
    ConfigError,
    deep_merge,
    interpolate_env,
    load_config,
    load_default_config,
    read_yaml,
    resolve_paths,
)
from tcad.config.schema import Config, DegradeThresholds

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── interpolation ─────────────────────────────────────────────────────────


def test_interpolate_uses_env(monkeypatch):
    monkeypatch.setenv("TCAD_TEST_X", "hello")
    assert interpolate_env("${TCAD_TEST_X}") == "hello"


def test_interpolate_default_when_unset(monkeypatch):
    monkeypatch.delenv("TCAD_TEST_MISSING", raising=False)
    assert interpolate_env("${TCAD_TEST_MISSING:-fallback}") == "fallback"


def test_interpolate_unset_without_default_is_empty(monkeypatch):
    monkeypatch.delenv("TCAD_TEST_MISSING", raising=False)
    assert interpolate_env("a${TCAD_TEST_MISSING}b") == "ab"


def test_interpolate_strict_raises(monkeypatch):
    monkeypatch.delenv("TCAD_TEST_MISSING", raising=False)
    with pytest.raises(ConfigError):
        interpolate_env("${TCAD_TEST_MISSING}", strict=True)


# ── merge semantics ───────────────────────────────────────────────────────


def test_deep_merge_recurses_dicts_but_replaces_lists():
    base = {"a": {"x": 1, "y": 2}, "lst": [1, 2, 3]}
    over = {"a": {"y": 9}, "lst": [7]}
    got = deep_merge(base, over)
    assert got == {"a": {"x": 1, "y": 9}, "lst": [7]}
    # must not mutate its input
    assert base["a"]["y"] == 2


# ── schema guards ─────────────────────────────────────────────────────────


def test_degrade_thresholds_must_be_ordered():
    with pytest.raises(ValidationError):
        DegradeThresholds(summarized=0.9, minimal=0.5)


def test_defaults_are_safe_out_of_the_box():
    cfg = Config()
    # the escape hatch must be closed on a fresh config
    assert cfg.policy.allow_privileged is False
    assert cfg.tools.privileged == []
    # fail-closed: no sandbox probe means the third privileged condition is unmet
    assert cfg.policy.sandbox_probe is None
    assert cfg.verify.require_requirement_confirmation is True


def test_budget_total_excludes_history():
    cfg = Config()
    assert cfg.budget_total() == 6000 + 2000 + 2000 + 2400


# ── real files ────────────────────────────────────────────────────────────


def test_default_yaml_loads_and_matches_schema():
    cfg = load_default_config()
    assert cfg.version == 1
    assert cfg.runtime.freecad_cmd.endswith("FreeCADCmd")
    # The shipped configuration puts no work ceiling on a turn (`null`), so a
    # turn is bounded by liveness, not by a quota. A number here would be a
    # silent policy change — the UI badge reads the same values.
    assert cfg.loop.max_steps_per_turn is None
    assert cfg.loop.max_tokens_per_turn is None
    assert cfg.loop.step_timeout_s is None
    assert cfg.loop.turn_wall_clock_s is None
    assert cfg.loop.max_compile_retries is None
    # every check the design doc promises is present
    ids = {c.id for c in cfg.verify.checks}
    assert ids == {
        "solid_validity", "solid_count", "bbox_spec", "mass_spec",
        "sketch_fully_constrained", "round_trip", "exportability", "wall_thickness",
    }
    assert cfg.verify.check("round_trip").tol_ratio == 1e-6
    # privileged tier stays closed
    assert cfg.tools.privileged == []
    assert cfg.policy.allow_privileged is False


def test_strict_overlay_only_tightens():
    base = load_config(f"{REPO_ROOT}/configs/default.yaml")
    strict = load_config(
        f"{REPO_ROOT}/configs/default.yaml",
        overlays=[f"{REPO_ROOT}/configs/policies/strict.yaml"],
    )
    # The overlay is where ceilings are put back: the base is unbounded, so
    # "strict < base" would compare against None. Assert what actually matters —
    # every dimension ends up bounded, and bounded *tightly*.
    for name in ("max_steps_per_turn", "max_tokens_per_turn",
                 "step_timeout_s", "turn_wall_clock_s", "max_compile_retries"):
        base_value = getattr(base.loop, name)
        strict_value = getattr(strict.loop, name)
        assert base_value is None, f"{name}: base should impose no ceiling"
        assert strict_value is not None, f"{name}: strict must bound every dimension"
    assert strict.loop.max_steps_per_turn <= 12
    assert strict.loop.turn_wall_clock_s <= 240.0
    assert strict.llm.temperature <= base.llm.temperature
    # and never loosens anything security-relevant
    assert strict.policy.allow_privileged is False
    assert strict.tools.privileged == []
    assert strict.policy.approval_ttl_s <= base.policy.approval_ttl_s


def test_sandbox_exec_does_not_break_loading_but_probe_is_null():
    cfg = load_default_config()
    assert cfg.sandbox.backend in {"sandbox-exec", "bwrap", "none"}
    if cfg.sandbox.backend == "sandbox-exec" and os.uname().sysname != "Darwin":
        pytest.skip("macOS-specific backend")
    assert cfg.policy.sandbox_probe is None


def test_missing_file_raises_unless_missing_ok():
    with pytest.raises(ConfigError):
        read_yaml(f"{REPO_ROOT}/configs/definitely-not-here.yaml")
    assert read_yaml(f"{REPO_ROOT}/configs/definitely-not-here.yaml", missing_ok=True) == {}


def test_top_level_must_be_mapping(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        read_yaml(p)


def test_env_override_beats_file():
    cfg = load_config(
        f"{REPO_ROOT}/configs/default.yaml",
        env_overrides={"loop": {"max_steps_per_turn": 3}},
    )
    assert cfg.loop.max_steps_per_turn == 3


def test_resolve_paths_makes_paths_absolute():
    from pathlib import Path

    cfg = load_default_config()
    resolve_paths(cfg, root=Path(REPO_ROOT))
    assert os.path.isabs(cfg.runtime.freecad_cmd)
    assert os.path.isabs(cfg.storage.data_dir)
    assert os.path.isabs(cfg.storage.sqlite_path)
    assert cfg.runtime.freecad_cmd.startswith(REPO_ROOT)


def test_env_var_interpolation_in_llm_base_url(monkeypatch):
    monkeypatch.setenv("TCAD_LLM_BASE_URL", "http://example.invalid/v1")
    cfg = load_default_config()
    assert cfg.llm.base_url == "http://example.invalid/v1"
