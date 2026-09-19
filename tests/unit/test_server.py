"""Server-layer tests: the HTTP/SSE/CLI surface.

These inject a fake service stack, so they run without FreeCAD and without a
model provider. What they are actually checking is the plumbing that has no unit
test otherwise: SSE framing, the hook tap, endpoint error codes, and — most
importantly — that the streamed result carries the *engine's* verdict rather than
anything the server decided for itself.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tcad.config.schema import Config
from tcad.core.types import (
    GateReport,
    HookDecision,
    HookEvent,
    HookResult,
    IrEvent,
    TurnKind,
    TurnState,
)
from tcad.hooks.approval import JsonFileApprovalStore
from tcad.hooks.dispatcher import HookDispatcher
from tcad.ir.schema import IrDocument
from tcad.llm.client import LlmReply
from tcad.loop.engine import LoopConfig
from tcad.server.app import ChatRequest, HookEventTap, create_app, turn_succeeded

REPO_ROOT = Path(__file__).resolve().parents[2]
TestClient = pytest.importorskip("fastapi.testclient").TestClient


# ══════════════════════════════════════════════════════════════════════════
# fakes
# ══════════════════════════════════════════════════════════════════════════


class FakeStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._docs: dict[str, IrDocument] = {}

    def create(self, model_id: str, ir: IrDocument) -> IrDocument:
        self._docs[model_id] = ir.model_copy(deep=True)
        return self._docs[model_id]

    def current_version(self, model_id: str) -> int:
        if model_id not in self._docs:
            raise FileNotFoundError(model_id)
        return int(self._docs[model_id].version)

    def load(self, model_id: str, version: int | None = None) -> IrDocument:
        if model_id not in self._docs:
            raise FileNotFoundError(model_id)
        return self._docs[model_id].model_copy(deep=True)

    def artifact_dir(self, model_id: str, version: int) -> Path:
        return self.data_dir / "artifacts" / model_id / f"v{version}"

    def apply_patch(self, model_id: str, patch):
        doc = self.load(model_id)
        doc.version += 1
        self._docs[model_id] = doc
        return doc, IrEvent(seq=1, model_id=model_id, kind="patch_applied")

    def validate_patch(self, ir, patch):
        return []

    def validate_document(self, ir):
        return []

    def persist_digest(self, model_id, ir_version, digest):
        d = self.artifact_dir(model_id, ir_version)
        d.mkdir(parents=True, exist_ok=True)
        (d / "digest.json").write_text(digest.model_dump_json(), encoding="utf-8")


class FakeWorker:
    def request(self, method, params=None, *, timeout_s=30.0):
        return {"ok": True, "result": {}}


class FakeGate:
    def __init__(self, passed: bool) -> None:
        self.passed = passed
        self.calls = 0

    def evaluate(self, model_id, ir_version):
        self.calls += 1
        return GateReport(model_id=model_id, ir_version=ir_version, passed=self.passed)


class ScriptedLlm:
    """Returns plain text with no tool calls, so the turn runs out of budget."""

    def __init__(self, text: str = "I'm done, the part is complete.") -> None:
        self._text = text
        self.calls = 0

    async def chat(self, *, messages, tools=None, tool_choice=None, temperature=None):
        self.calls += 1
        return LlmReply(text=self._text)


class FakeHandle:
    def is_alive(self) -> bool:
        return True


def make_services(tmp_path: Path, *, gate_passed: bool = True, max_steps: int = 2):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config()
    cfg.storage.data_dir = str(data_dir)
    cfg.loop.max_steps_per_turn = max_steps

    # The stack's LLM is a swappable holder in production, so the fake must be
    # one too — otherwise the settings endpoints would be untestable here and
    # their absence from the contract would go unnoticed.
    from tcad.config.settings import LlmSettings
    from tcad.llm.hotswap import HotSwapLlm

    llm = HotSwapLlm(
        LlmSettings(provider="custom", base_url="http://fake.invalid/v1", model="fake"),
        client=ScriptedLlm(),
    )

    return SimpleNamespace(
        store=FakeStore(data_dir),
        worker=FakeWorker(),
        gate=FakeGate(gate_passed),
        renderer=None,
        hooks=HookDispatcher([], {}),
        context=None,
        llm=llm,
        approvals=JsonFileApprovalStore(str(data_dir / "approvals.json"), ttl_s=60),
        config=cfg,
        loop_config=LoopConfig(data_dir=str(data_dir), workdir=str(REPO_ROOT)),
        _worker_handle=FakeHandle(),
    )


@pytest.fixture()
def client(tmp_path):
    services = make_services(tmp_path)
    app = create_app(services)
    with TestClient(app) as c:
        c.services = services
        yield c


def _sse(body: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into (event, data) pairs."""
    out: list[tuple[str, dict]] = []
    for block in body.strip().split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        event = next((ln[7:] for ln in lines if ln.startswith("event: ")), "message")
        data = next((ln[6:] for ln in lines if ln.startswith("data: ")), "{}")
        out.append((event, json.loads(data)))
    return out


# ══════════════════════════════════════════════════════════════════════════
# health / models / artifacts
# ══════════════════════════════════════════════════════════════════════════


def test_health_reports_worker_state(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["worker_alive"] is True


def test_health_identifies_which_instance_this_is(client):
    """Two servers can run side by side with different data directories, and
    nothing distinguished them — so seeing the wrong model in the UI looked like
    "my configuration was lost" instead of "this is a different server"."""
    body = client.get("/health").json()
    assert body["data_dir"], "health 应报告数据目录"
    assert body["data_dir"] == str(client.services.config.storage.data_dir)


def test_health_still_answers_without_a_service_stack(tmp_path):
    """`data_dir` must come from the config, not from a live stack — otherwise a
    server that has not built its stack yet cannot be identified either."""
    from tcad.config.schema import Config
    from tcad.server.app import create_app

    cfg = Config()
    cfg.storage.data_dir = str(tmp_path / "data")
    app = create_app(None, config=cfg)
    with TestClient(app) as c:
        body = c.get("/health").json()
        assert body["status"] == "ok"
        assert body["data_dir"] == str(tmp_path / "data")


def test_create_model_then_reject_duplicate(client):
    r = client.post("/models", json={"model_id": "bracket", "raw_requirement": "a plate"})
    assert r.status_code == 200
    assert r.json()["version"] == 0

    again = client.post("/models", json={"model_id": "bracket"})
    assert again.status_code == 409


def test_get_ir_round_trips(client):
    client.post("/models", json={"model_id": "m1", "raw_requirement": "60x40 plate"})
    body = client.get("/models/m1/ir").json()
    assert body["model_id"] == "m1"
    assert body["requirements"]["raw_text"] == "60x40 plate"


def test_get_ir_404_for_unknown_model(client):
    assert client.get("/models/nope/ir").status_code == 404


def test_artifact_listing_is_empty_before_any_commit(client):
    client.post("/models", json={"model_id": "m1"})
    body = client.get("/models/m1/artifacts").json()
    assert body["files"] == []
    assert body["version"] == 0


# ══════════════════════════════════════════════════════════════════════════
# chat / SSE
# ══════════════════════════════════════════════════════════════════════════


def test_chat_streams_start_progress_and_result(client):
    client.post("/models", json={"model_id": "m1"})
    r = client.post("/chat", json={"model_id": "m1", "text": "make a plate"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    events = _sse(r.text)
    names = [e for e, _ in events]
    assert names[0] == "start"
    assert names[-1] == "result"
    assert "progress" in names, "the hook tap produced no lifecycle events"

    start = dict(events)["start"]
    assert start["model_id"] == "m1" and start["kind"] == "create"


def test_chat_progress_events_are_real_lifecycle_hooks(client):
    client.post("/models", json={"model_id": "m1"})
    r = client.post("/chat", json={"model_id": "m1", "text": "go"})
    progress = [d for e, d in _sse(r.text) if e == "progress"]
    seen = {p["event"] for p in progress}
    # pre_turn / pre_step / post_turn always fire, whatever the turn does.
    assert "pre_turn" in seen
    assert "pre_step" in seen
    assert "post_turn" in seen


def test_chat_result_carries_the_engines_verdict_not_the_servers(tmp_path):
    """A model that *says* it is done, with no green Gate, must not be SUCCEEDED.

    The stream is produced by the server but the verdict must come from the
    engine — this asserts the server does not invent a success.
    """
    services = make_services(tmp_path, gate_passed=True, max_steps=2)
    app = create_app(services)
    with TestClient(app) as client:
        client.post("/models", json={"model_id": "m1"})
        r = client.post("/chat", json={"model_id": "m1", "text": "make it"})
        result = dict(_sse(r.text))["result"]

    assert result["state"] == TurnState.EXHAUSTED.value
    assert result["state"] != TurnState.SUCCEEDED.value
    assert result["gate_report"] is None
    assert services.gate.calls == 0, "the Gate must not have been consulted at all"


def test_chat_stream_survives_a_worker_explosion(tmp_path):
    """An exception inside the turn becomes an SSE error frame, not a 500."""

    services = make_services(tmp_path)

    async def boom(*a, **kw):
        raise RuntimeError("llm is down")

    services.llm.chat = boom
    app = create_app(services)
    with TestClient(app) as client:
        client.post("/models", json={"model_id": "m1"})
        r = client.post("/chat", json={"model_id": "m1", "text": "go"})
    assert r.status_code == 200
    events = _sse(r.text)
    # the engine catches LLM failures and reports FAILED — either way it streams
    assert events[-1][0] in {"result", "error"}


def test_chat_unknown_model_yields_an_error_frame(client):
    r = client.post("/chat", json={"model_id": "ghost", "text": "go"})
    assert r.status_code == 200
    events = _sse(r.text)
    assert events[-1][0] in {"result", "error"}


def test_turn_succeeded_helper_matches_the_state():
    assert turn_succeeded(SimpleNamespace(state=TurnState.SUCCEEDED)) is True
    for st in (TurnState.EXHAUSTED, TurnState.FAILED, TurnState.ABORTED):
        assert turn_succeeded(SimpleNamespace(state=st)) is False


# ══════════════════════════════════════════════════════════════════════════
# approvals
# ══════════════════════════════════════════════════════════════════════════


def test_approval_lifecycle_over_http(client):
    assert client.get("/approvals").json()["pending"] == []

    rec = client.services.approvals.request("raw_python", args_hash="abc")
    pending = client.get("/approvals").json()["pending"]
    assert [p["id"] for p in pending] == [rec.id]

    granted = client.post(f"/approvals/{rec.id}", json={"granted": True})
    assert granted.status_code == 200
    assert granted.json()["granted"] is True
    assert client.get("/approvals").json()["pending"] == []


def test_approval_404_for_unknown_id(client):
    assert client.post("/approvals/nope", json={"granted": True}).status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# the hook tap must observe without changing decisions
# ══════════════════════════════════════════════════════════════════════════


def test_hook_tap_records_and_delegates_the_decision():
    def deny(event, payload):
        return HookResult(decision=HookDecision.DENY, hook_name="d", reason="no")

    inner = HookDispatcher(
        [__import__("tcad.core.types", fromlist=["HookSpec"]).HookSpec(
            name="d", events=[HookEvent.PRE_TOOL_USE])],
        {"d": deny},
    )
    tap = HookEventTap(inner)
    res = tap.dispatch(HookEvent.PRE_TOOL_USE, {"tool_name": "raw_python"})

    assert res.decision is HookDecision.DENY, "the tap must not alter the verdict"
    drained = tap.drain()
    assert len(drained) == 1
    assert drained[0]["decision"] == "deny"
    assert drained[0]["hook"] == "d"
    assert tap.drain() == [], "draining must be destructive"


def test_hook_tap_survives_non_serialisable_payloads():
    tap = HookEventTap(HookDispatcher([], {}))
    res = tap.dispatch(HookEvent.PRE_STEP, {"obj": object()})
    assert res.decision is HookDecision.ALLOW
    assert isinstance(tap.drain()[0]["payload"], dict)


# ══════════════════════════════════════════════════════════════════════════
# CLI wiring (argument parsing only — it drives the same engine)
# ══════════════════════════════════════════════════════════════════════════


def test_cli_parser_is_wired():
    from tcad.server.cli import build_parser

    p = build_parser()
    assert p.parse_args(["new", "m1", "--requirement", "x"]).func.__name__ == "cmd_new"
    assert p.parse_args(["chat", "m1", "hi"]).func.__name__ == "cmd_chat"
    assert p.parse_args(["repl", "m1"]).func.__name__ == "cmd_repl"
    assert p.parse_args(["approvals"]).func.__name__ == "cmd_approvals"
    a = p.parse_args(["approve", "abc", "--grant"])
    assert a.func.__name__ == "cmd_approve" and a.grant is True


def test_cli_requires_a_subcommand():
    from tcad.server.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_cli_chat_kind_is_validated():
    from tcad.server.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["chat", "m1", "hi", "--kind", "not-a-kind"])
    assert build_parser().parse_args(["chat", "m1", "hi", "--kind", "inspect"]).kind == "inspect"
