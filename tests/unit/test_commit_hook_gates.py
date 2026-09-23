"""``pre_commit`` is the last gate before anything is written — and it has to
actually gate.

``run_commit`` checked only ``DENY``. An ``ASK`` — "a human should look at this
before it is built" — fell straight through to compile/export/Gate, so by the
time anyone was asked, the artefacts, the exported STEP and the verdict all
already existed. Objective §5-D: "ASK 不能继续执行后续写操作".
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from tcad.core.types import HookDecision, HookResult, ToolErrorKind
from tcad.loop.commit import run_commit
from tests.fixtures.gate_fixtures import make_ir


class _Hooks:
    def __init__(self, decision: HookDecision):
        self.decision = decision
        self.events = []

    def dispatch(self, event, payload):
        self.events.append(event)
        return HookResult(decision=self.decision, hook_name="test",
                          reason="suspend for review")


class _Store:
    def load(self, model_id, version=None):
        return make_ir(model_id=model_id)

    def validate_document(self, ir):
        return []


class _ExplodingWorker:
    """Any worker call at all is a failure: the build must not have started."""

    def request(self, *_a, **_kw):
        raise AssertionError("the build ran after pre_commit refused it")

    def abort_inflight(self, *_a, **_kw):
        return False


def _services(tmp_path):
    return SimpleNamespace(
        store=_Store(), hooks=_Hooks(HookDecision.ASK),
        worker=_ExplodingWorker(),
        gate=SimpleNamespace(evaluate=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("the Gate ran after pre_commit refused it"))),
    )


def test_a_pre_commit_ask_does_not_build(tmp_path: Path):
    services = _services(tmp_path)
    services.hooks = _Hooks(HookDecision.ASK)

    result, report = asyncio.run(run_commit(
        services, model_id="m1", ir_version=1, message="build",
        workdir=str(tmp_path), data_dir=str(tmp_path), hooks=services.hooks,
    ))

    assert result.ok is False
    assert report is None, "a verdict was produced for a build that never ran"
    assert result.error.kind == ToolErrorKind.DENIED
    assert "approval" in result.error.message.lower()
    assert "Nothing was compiled" in result.error.message


def test_a_pre_commit_deny_still_refuses(tmp_path: Path):
    """The pre-existing behaviour, pinned so the ASK change cannot loosen it."""
    services = _services(tmp_path)
    services.hooks = _Hooks(HookDecision.DENY)

    result, report = asyncio.run(run_commit(
        services, model_id="m1", ir_version=1, message="build",
        workdir=str(tmp_path), data_dir=str(tmp_path), hooks=services.hooks,
    ))

    assert result.ok is False
    assert report is None
    assert result.error.kind == ToolErrorKind.DENIED


def test_an_allowing_pre_commit_reaches_the_worker(tmp_path: Path):
    """The other direction: ALLOW must not be turned into a refusal."""
    services = _services(tmp_path)
    services.hooks = _Hooks(HookDecision.ALLOW)
    calls: list[str] = []

    def _record(method, params=None, *, timeout_s=30.0):
        calls.append(method)
        return {"ok": False, "error": {"kind": "compile", "message": "stop here"}}

    services.worker = SimpleNamespace(request=_record, abort_inflight=lambda *a, **k: False)

    result, report = asyncio.run(run_commit(
        services, model_id="m1", ir_version=1, message="build",
        workdir=str(tmp_path), data_dir=str(tmp_path), hooks=services.hooks,
    ))

    assert calls, "pre_commit ALLOW was treated as a refusal"
    assert result.ok is False  # the fake worker refuses, which is fine
