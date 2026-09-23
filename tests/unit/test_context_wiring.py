"""Budgeted conversation context wired into the model request (task book §5-D).

Before this existed the session database held the conversation but the model
received ``[system, user]`` and nothing else. These tests pin the contract that
closes that gap: prior turns, the requirement contract, the current IR summary
(with stable feature ids), the current version and the previous Gate verdict all
reach the model — and the current request still arrives exactly once, last.

Every test here is deterministic: the model and the worker are fakes, so a green
result proves the assembly logic, never geometry.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tcad.context.assembler import (
    ContextAssembler,
    ContextBudget,
    Message,
)
from tcad.context.digest import render_digest_text
from tcad.context.history import load_thread_history, messages_from_rows
from tcad.context.requirements import render_requirements_text
from tcad.context.verdict import render_verdict_text
from tcad.core.types import (
    CheckResult,
    CheckStatus,
    Confidence,
    GateReport,
    GeometryDigest,
    Severity,
)
from tcad.ir.schema import (
    BodySpec,
    ConstraintExpr,
    FeatureSpec,
    IrDocument,
    RequirementSpec,
)
from tcad.loop.budget import BudgetLimits
from tcad.loop.engine import LoopConfig, LoopEngine, UserMessage
from tcad.llm.client import LlmReply
from tcad.core.types import Thread, TurnKind


# ══════════════════════════════════════════════════════════════════════════
# 1. The requirement contract block
# ══════════════════════════════════════════════════════════════════════════


def _ir_with_requirements() -> IrDocument:
    return IrDocument(
        model_id="plate",
        version=3,
        requirements=RequirementSpec(
            raw_text="80x50x8 的板，四个直径 6 的通孔",
            constraints=[
                ConstraintExpr(
                    kind="bbox", target="base", value={"x": 80.0, "y": 50.0, "z": 8.0},
                    source_text="80x50x8 的板", confirmed=True,
                ),
                ConstraintExpr(
                    kind="hole_diameter", target="holes", value=6.0,
                    source_text="四个直径 6 的通孔", confirmed=True,
                ),
                ConstraintExpr(kind="count", target="holes", value=4, confirmed=False),
            ],
        ),
    )


def test_requirement_contract_separates_confirmed_from_inferred():
    text = render_requirements_text(_ir_with_requirements())
    assert "80x50x8 的板，四个直径 6 的通孔" in text
    assert "2 confirmed" in text
    # A confirmed requirement is allowed to block; an inferred one explicitly is not.
    assert "CONFIRMED — may block the build" in text
    assert "UNCONFIRMED — advisory only, cannot block" in text
    # The verbatim source is what proves the number came from the user.
    assert '"四个直径 6 的通孔"' in text


def test_empty_contract_says_so_instead_of_being_silent():
    text = render_requirements_text(IrDocument(model_id="m", version=0))
    assert "constraints: NONE" in text
    assert "update_requirement" in text


def test_requirement_contract_is_independent_of_geometry():
    """It renders the same before and after a feature is added."""
    ir = _ir_with_requirements()
    before = render_requirements_text(ir)
    ir.bodies.append(
        BodySpec(id="b1", name="body",
                 features=[FeatureSpec(id="f1", name="pad", op="pad", params={"length": 8})])
    )
    assert render_requirements_text(ir) == before


# ══════════════════════════════════════════════════════════════════════════
# 2. Stable feature ids reach the digest
# ══════════════════════════════════════════════════════════════════════════


def test_digest_prints_stable_feature_ids():
    ir = IrDocument(
        model_id="m", version=2,
        bodies=[BodySpec(id="b1", name="body", features=[
            FeatureSpec(id="ft_hole_1", name="mounting_hole_1", op="pocket", params={"length": 8}),
        ])],
    )
    digest = GeometryDigest(model_id="m", ir_version=2, text="")
    text = render_digest_text(digest, ir)
    # The id (not just the human name) is what a later patch must reference.
    assert "mounting_hole_1 [ft_hole_1] (pocket)" in text


# ══════════════════════════════════════════════════════════════════════════
# 3. History loader
# ══════════════════════════════════════════════════════════════════════════


def test_history_excludes_the_current_user_message():
    rows = [
        {"role": "user", "content": "make an 80x50x8 plate"},
        {"role": "assistant", "content": "built the plate"},
        {"role": "user", "content": "change the holes to 8"},
    ]
    msgs = messages_from_rows(rows, current_text="change the holes to 8")
    assert [m.content for m in msgs] == ["make an 80x50x8 plate", "built the plate"]


def test_history_keeps_an_earlier_repeat_of_the_same_sentence():
    """Only the trailing copy is dropped, so a real earlier repeat survives."""
    rows = [
        {"role": "user", "content": "same words"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "same words"},
    ]
    msgs = messages_from_rows(rows, current_text="same words")
    assert [m.content for m in msgs] == ["same words", "ok"]


def test_history_skips_unknown_roles_and_blank_content():
    rows = [
        {"role": "tool", "content": "raw tool output"},
        {"role": "user", "content": "   "},
        {"role": "assistant", "content": "real"},
    ]
    msgs = messages_from_rows(rows)
    assert [m.content for m in msgs] == ["real"]


def test_missing_session_db_is_not_an_error():
    assert load_thread_history(None, "th1") == []
    assert load_thread_history(SimpleNamespace(list_messages=lambda *a, **k: 1 / 0), "th1") == []


# ══════════════════════════════════════════════════════════════════════════
# 4. Previous-verdict block
# ══════════════════════════════════════════════════════════════════════════


def test_verdict_of_nothing_is_empty():
    assert render_verdict_text(None) == ""


def test_verdict_renders_measured_vs_expected_for_a_repair():
    report = GateReport(
        model_id="m", ir_version=4, passed=False,
        results=[CheckResult(
            check_id="spec_volume", status=CheckStatus.FAIL, severity=Severity.BLOCKING,
            confidence=Confidence.DETERMINISTIC,
            message="requirement 'volume' NOT met",
            measurements={"value": 31095.2}, expected={"value": 30391.5},
            feature_id="ft_pad",
        )],
        blocking_failures=["spec_volume"],
        advisory_findings=["wall_thickness"],
    )
    text = render_verdict_text(report)
    assert "passed: false" in text
    assert "spec_volume" in text and "feature_id=ft_pad" in text
    assert "measured:" in text and "expected:" in text
    assert "advisory_findings" in text


def test_verdict_accepts_the_persisted_dict_form():
    as_dict = {
        "passed": False, "ir_version": 2,
        "blocking_failures": ["exportability"],
        "results": [{
            "check_id": "exportability", "status": "fail", "severity": "blocking",
            "message": "missing required export: step", "feature_id": None,
            "measurements": {"missing_formats": "step"}, "expected": {},
        }],
        "advisory_findings": [], "skipped_checks": [],
    }
    text = render_verdict_text(as_dict)
    assert "exportability" in text and "missing required export: step" in text


def test_verdict_of_a_pass_says_a_change_invalidates_it():
    text = render_verdict_text(GateReport(model_id="m", ir_version=1, passed=True))
    assert "passed: true" in text
    assert "invalidates" in text


# ══════════════════════════════════════════════════════════════════════════
# 5. The engine actually sends the blocks
# ══════════════════════════════════════════════════════════════════════════


class _Store:
    def __init__(self, ir, reports=None):
        self.ir = ir
        self.reports = reports or {}

    def load(self, model_id, version=None):
        return self.ir

    def current_version(self, model_id):
        return self.ir.version

    def read_gate_report(self, model_id, version):
        return self.reports.get(version)


class _Context:
    def __init__(self, text):
        self._text = text

    def digest(self, model_id, ir_version):
        return GeometryDigest(model_id=model_id, ir_version=ir_version, text=self._text)


def _engine(*, store, context_text="DIGEST TEXT", assembler=None, history=None):
    services = SimpleNamespace(store=store, context=_Context(context_text))
    cfg = LoopConfig(data_dir="/tmp", default_strategy="loop_until_done")
    return LoopEngine(
        services, None, BudgetLimits(), cfg,
        context_assembler=assembler, history_provider=history,
    )


async def test_engine_sends_history_contract_digest_and_verdict():
    ir = _ir_with_requirements()
    report = {
        "passed": False, "ir_version": 3,
        "blocking_failures": ["spec_volume"],
        "results": [{
            "check_id": "spec_volume", "status": "fail", "severity": "blocking",
            "message": "volume off", "measurements": {"value": 1.0}, "expected": {"value": 2.0},
        }],
        "advisory_findings": [], "skipped_checks": [],
    }
    store = _Store(ir, reports={3: report})
    history = [Message(role="user", content="PRIOR TURN", kind="history", tokens_estimate=5)]
    engine = _engine(
        store=store,
        assembler=ContextAssembler(ContextBudget(window_tokens=100_000)),
        history=lambda tid, text: list(history),
    )
    turn = SimpleNamespace(thread_id="th1", model_id="plate", base_ir_version=3)
    msgs = await engine._build_messages(UserMessage(kind=TurnKind.MODIFY, text="holes to 8"), turn)

    joined = "\n".join(m["content"] for m in msgs)
    assert "80x50x8 的板，四个直径 6 的通孔" in joined   # requirement contract
    assert "DIGEST TEXT" in joined                        # current IR summary
    assert "spec_volume" in joined                        # previous verdict
    assert "PRIOR TURN" in joined                         # session history
    # The current request is present exactly once and is the last message.
    assert msgs[-1] == {"role": "user", "content": "holes to 8"}
    assert sum(1 for m in msgs if m["content"] == "holes to 8") == 1


async def test_engine_without_assembler_keeps_the_old_two_message_shape():
    engine = _engine(store=_Store(_ir_with_requirements()))
    turn = SimpleNamespace(thread_id="th1", model_id="plate", base_ir_version=3)
    msgs = await engine._build_messages(UserMessage(kind=TurnKind.CREATE, text="hi"), turn)
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert msgs[-1]["content"] == "hi"


async def test_a_broken_assembler_never_breaks_the_turn():
    class _Exploding:
        async def build(self, ctx, images):
            raise RuntimeError("assembler is down")

    engine = _engine(store=_Store(_ir_with_requirements()), assembler=_Exploding())
    turn = SimpleNamespace(thread_id="th1", model_id="plate", base_ir_version=3)
    msgs = await engine._build_messages(UserMessage(kind=TurnKind.CREATE, text="hi"), turn)
    assert [m["role"] for m in msgs] == ["system", "user"]


async def test_a_broken_history_provider_never_breaks_the_turn():
    def _boom(tid, text):
        raise RuntimeError("db is down")

    engine = _engine(
        store=_Store(_ir_with_requirements()),
        assembler=ContextAssembler(ContextBudget(window_tokens=100_000)),
        history=_boom,
    )
    turn = SimpleNamespace(thread_id="th1", model_id="plate", base_ir_version=3)
    msgs = await engine._build_messages(UserMessage(kind=TurnKind.MODIFY, text="x"), turn)
    assert msgs[-1] == {"role": "user", "content": "x"}


async def test_the_whole_turn_reaches_the_model_with_prior_context():
    """End to end through run_turn, not just the message builder."""
    from tests.unit.test_loop_engine import ScriptedLlm, make_ir, make_services
    from tcad.tools.base import build_default_registry

    ir = make_ir()
    llm = ScriptedLlm([LlmReply(tool_calls=[], text="thinking")])
    svc = make_services(ir, llm, gate_passed=True)
    engine = LoopEngine(
        svc,
        build_default_registry(svc),
        BudgetLimits(),
        LoopConfig(data_dir="/tmp"),
        context_assembler=ContextAssembler(ContextBudget(window_tokens=100_000)),
        history_provider=lambda tid, text: [
            Message(role="user", content="EARLIER ASK", kind="history")
        ],
    )
    thread = Thread(thread_id="th1", model_id="m1")
    await engine.run_turn(thread, UserMessage(kind=TurnKind.MODIFY, text="now change it"))
    joined = "\n".join(m["content"] for m in llm.last_messages)
    assert "EARLIER ASK" in joined
    assert sum(1 for m in llm.last_messages if m["content"] == "now change it") == 1


# ══════════════════════════════════════════════════════════════════════════
# 6. The verdict survives the process (server builds a fresh engine per turn)
# ══════════════════════════════════════════════════════════════════════════


def test_verdict_is_persisted_outside_the_artifact_directory(tmp_path):
    from tcad.core.wiring import StoreAdapter

    adapter = StoreAdapter(tmp_path)
    report = GateReport(
        model_id="plate", ir_version=2, passed=False,
        results=[CheckResult(
            check_id="solid_validity", status=CheckStatus.FAIL, severity=Severity.BLOCKING,
            confidence=Confidence.DETERMINISTIC, message="not a solid",
        )],
        blocking_failures=["solid_validity"], advisory_findings=[],
    )
    adapter.write_gate_report("plate", 2, report)

    back = adapter.read_gate_report("plate", 2)
    assert back["passed"] is False
    assert "not a solid" in render_verdict_text(back)

    # Deliberately NOT in the artifact directory: the Gate audits that file set
    # and a bookkeeping file there would change what "delivered" means.
    assert not (tmp_path / "artifacts" / "plate" / "v2").exists()
    assert (tmp_path / "gate_reports" / "plate" / "v2.json").is_file()


def test_missing_verdict_reads_back_as_none(tmp_path):
    from tcad.core.wiring import StoreAdapter

    assert StoreAdapter(tmp_path).read_gate_report("nope", 7) is None


def test_commit_persists_the_verdict_and_tolerates_a_store_that_cannot():
    from tcad.loop.commit import _persist_gate_report

    seen = {}

    class _Storing:
        def write_gate_report(self, model_id, version, report):
            seen["args"] = (model_id, version, report)

    report = GateReport(model_id="m", ir_version=1, passed=True)
    _persist_gate_report(SimpleNamespace(store=_Storing()), "m", 1, report)
    assert seen["args"] == ("m", 1, report)

    # A store with no writer, and one whose writer raises, must both be no-ops:
    # context bookkeeping must never fail a build.
    _persist_gate_report(SimpleNamespace(store=SimpleNamespace()), "m", 1, report)

    class _Broken:
        def write_gate_report(self, *a, **k):
            raise OSError("disk full")

    _persist_gate_report(SimpleNamespace(store=_Broken()), "m", 1, report)


# ══════════════════════════════════════════════════════════════════════════
# 7. The HTTP path really replays history (not just the builder in isolation)
# ══════════════════════════════════════════════════════════════════════════


def test_the_http_chat_endpoint_replays_the_previous_turn(tmp_path):
    """Two POSTs to /chat; the second request must carry the first one's text.

    This is the end-to-end proof that "the history is in SQLite" became "the
    model was told the history". The engine is constructed per request, so only
    the on-disk store can carry it across.
    """
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from tests.unit import test_server as ts
    from tcad.server.app import create_app

    class _Recorder:
        def __init__(self):
            self.calls: list[list[dict]] = []

        async def chat(self, *, messages, tools=None, tool_choice=None, temperature=None):
            self.calls.append(list(messages))
            return LlmReply(text="noted")

    recorder = _Recorder()
    services = ts.make_services(tmp_path)
    services.llm = recorder
    services.context_assembler = ContextAssembler(ContextBudget(window_tokens=100_000))

    app = create_app(services)
    with TestClient(app) as client:
        client.post("/models", json={"model_id": "m1", "raw_requirement": "a plate 80x50x8"})
        r1 = client.post("/chat", json={
            "model_id": "m1", "text": "make a plate", "kind": "create", "request_id": "r1",
        })
        calls_after_first_turn = len(recorder.calls)
        r2 = client.post("/chat", json={
            "model_id": "m1", "text": "put four holes in it", "kind": "modify", "request_id": "r2",
        })

    assert r1.status_code == 200 and r2.status_code == 200
    assert len(recorder.calls) > calls_after_first_turn, "second turn never reached the model"
    # The *first* step of the second turn is the request built from context; later
    # steps append the assistant's own replies to the same conversation.
    second_turn = recorder.calls[calls_after_first_turn]
    joined = "\n".join(m["content"] for m in second_turn)
    assert "make a plate" in joined, second_turn
    assert "a plate 80x50x8" in joined          # the requirement contract block
    assert second_turn[-1] == {"role": "user", "content": "put four holes in it"}
    assert sum(1 for m in second_turn if m["content"] == "put four holes in it") == 1
