"""Configuration schema. Every tunable in the design doc's §8 YAML maps to a field
here, so an operator can retune the harness without touching code.

Declarative-first (design §2): behaviour is described in YAML; the only imperative
escape hatch is the privileged tool tier, which ships disabled.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# ══════════════════════════════════════════════════════════════════════════


class RuntimeConfig(BaseModel):
    freecad_cmd: str = "./free-cad/FreeCAD/build/debug/bin/FreeCADCmd"
    freecad_python_path: str = "."
    worker_pool_size: int = 2
    worker_request_timeout_s: float = 60.0
    worker_restart_on_crash: bool = True
    worker_startup_timeout_s: float = 120.0
    """FreeCADCmd takes a while to boot; generous, but bounded."""


class StorageConfig(BaseModel):
    data_dir: str = "./data"
    sqlite_path: str = ""
    """Session database location. **Empty means ``<data_dir>/tcad.sqlite3``.**

    Deliberately empty rather than a hard-coded ``./data/tcad.sqlite3``: with a
    path literal here, overriding ``data_dir`` (which the CLI's ``--data-dir``
    and every test do) would leave the database pointing at the *old* directory,
    silently sharing one database between two configurations.
    """
    sqlite_journal_mode: Literal["WAL", "DELETE", "TRUNCATE"] = "WAL"
    artifact_exports: list[str] = Field(default_factory=lambda: ["step", "stl"])
    keep_ir_versions: int = 200

    def sqlite_file(self) -> str:
        if self.sqlite_path:
            return self.sqlite_path
        return str(Path(self.data_dir) / "tcad.sqlite3")


class LlmConfig(BaseModel):
    base_url: str = "http://127.0.0.1:8000/v1"
    model: str = "qwen2.5-72b-instruct"
    api_key_env: str = "TCAD_LLM_API_KEY"
    api_key: str | None = None
    temperature: float = 0.2
    max_tokens_per_step: int = 4096
    request_timeout_s: float = 120.0
    max_retries: int = 2


class ForkJoinConfig(BaseModel):
    candidates: int = 3
    score_by: list[str] = Field(
        default_factory=lambda: ["blocking_failures", "advisory_findings"]
    )


class AdversarialConfig(BaseModel):
    after_gate: bool = True


class StrategiesConfig(BaseModel):
    loop_until_done: dict = Field(default_factory=dict)
    fork_join: ForkJoinConfig = Field(default_factory=ForkJoinConfig)
    adversarial: AdversarialConfig = Field(default_factory=AdversarialConfig)


class LoopConfig(BaseModel):
    """The safeties live here (design §4.1 item 3).

    Each ceiling is optional and defaults to ``None`` = *no ceiling*, so a turn
    is bounded by liveness (per-request transport timeouts) rather than by a
    work quota. Set any of them to re-impose a cap; ``configs/policies/strict.yaml``
    is a ready-made bounded profile. See :mod:`tcad.loop.budget` for why ``None``
    rather than a magic large number.
    """

    max_steps_per_turn: int | None = None
    max_tokens_per_turn: int | None = None
    step_timeout_s: float | None = None
    turn_wall_clock_s: float | None = None
    max_compile_retries: int | None = None
    default_strategy: Literal[
        "loop_until_done", "fork_join", "adversarial"
    ] = "loop_until_done"
    strategies: StrategiesConfig = Field(default_factory=StrategiesConfig)


class DegradeThresholds(BaseModel):
    summarized: float = 0.70
    minimal: float = 0.85

    @field_validator("minimal")
    @classmethod
    def _ordered(cls, v: float, info) -> float:
        s = info.data.get("summarized", 0.70)
        if v <= s:
            raise ValueError(
                f"degrade_thresholds.minimal ({v}) must exceed summarized ({s})"
            )
        return v


class ContextBudget(BaseModel):
    system_prefix: int = 6_000
    digest: int = 2_000
    gate_report: int = 2_000
    images: int = 2_400
    # history is "the remainder" — modelled as None rather than a magic number
    history: int | None = None


class RenderConfig(BaseModel):
    width: int = 768
    height: int = 576
    supersample: int = 2
    style: Literal["flat_edges", "flat", "edges_only"] = "flat_edges"


class ContextConfig(BaseModel):
    window_tokens: int = 128_000
    """UNVERIFIED ESTIMATE — must be recalibrated against the real model
    (design doc §12-6). Do not treat as a hard fact."""
    budget: ContextBudget = Field(default_factory=ContextBudget)
    degrade_thresholds: DegradeThresholds = Field(default_factory=DegradeThresholds)
    summarize_keep_last_turns: int = 6
    visual_checkpoints: list[str] = Field(
        default_factory=lambda: ["first_compile", "major_change", "final"]
    )
    views: list[str] = Field(default_factory=lambda: ["iso", "front", "top"])
    render: RenderConfig = Field(default_factory=RenderConfig)


class ToolsConfig(BaseModel):
    read: list[str] = Field(
        default_factory=lambda: [
            "ir_get", "ir_digest", "ir_list_features",
            "geo_view", "geo_measure", "asset_export", "asset_import",
        ]
    )
    write: list[str] = Field(default_factory=lambda: ["ir_patch", "ir_commit"])
    privileged: list[str] = Field(default_factory=list)
    """Empty by default. The escape hatch is opt-in, per design §4.2."""


class HookEntry(BaseModel):
    name: str
    kind: Literal["policy", "command"] = "policy"
    events: list[str]
    module: str | None = None
    command: str | None = None
    timeout_s: float = 5.0
    priority: int = 100


class PolicyConfig(BaseModel):
    allow_privileged: bool = False
    """One of the three conditions in the privileged gate. Only changeable by a
    human editing this file — the agent cannot reach it (design §4.5 item 5)."""
    approval_ttl_s: int = 900
    hooks: list[HookEntry] = Field(default_factory=list)
    deny_globs: list[str] = Field(default_factory=list)
    sandbox_probe: str | None = None
    """Command whose zero exit code counts as 'sandbox healthy'. None => the
    privileged gate treats the sandbox condition as unmet (fail-closed)."""


class CheckEntry(BaseModel):
    id: str
    enabled: bool = True
    severity: Literal["blocking", "advisory"] = "blocking"
    expect: float | None = None
    tol_mm: float | None = None
    tol_ratio: float | None = None
    min_mm: float | None = None


class VerifyConfig(BaseModel):
    gate_requires_all_blocking: bool = True
    checks: list[CheckEntry] = Field(default_factory=list)
    require_requirement_confirmation: bool = True
    """Unconfirmed ConstraintExprs must never block (design §4.6 item 5)."""

    def check(self, check_id: str) -> CheckEntry | None:
        return next((c for c in self.checks if c.id == check_id), None)


class SandboxConfig(BaseModel):
    enabled: bool = True
    backend: Literal["sandbox-exec", "bwrap", "none"] = "sandbox-exec"
    no_network: bool = True
    read_only_roots: list[str] = Field(default_factory=list)
    writable_root: str = "./data/sandbox"
    wall_clock_s: float = 20.0


class Config(BaseModel):
    version: int = 1
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    loop: LoopConfig = Field(default_factory=LoopConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    verify: VerifyConfig = Field(default_factory=VerifyConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)

    def budget_total(self) -> int:
        """Static budget consumed before history is added."""
        b = self.context.budget
        return b.system_prefix + b.digest + b.gate_report + b.images
