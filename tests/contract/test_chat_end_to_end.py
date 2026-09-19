"""The whole `/chat` path, driven by a scripted model.

This is the heaviest test in the suite and it earns it: it starts a real
FreeCADCmd, a real HTTP model provider, and a real ASGI app, then reads the
streamed turn frame by frame. The turn runs **once** and every test in the
module reads the same frames — running it per-test would cost minutes for no
extra coverage.

Three things it pins that nothing else can:

1. **The tool surface is drivable.** The script is written the way a model would
   write it — including ``base_version: "current"``, which is the natural thing
   to type and used to be rejected with a validation error that said nothing.
2. **Failures are legible.** ``tools/sessions/demo_bracket.json`` deliberately
   contains an over-constrained sketch and a pocket that cuts away from the
   material. Both must come back as *actionable* errors, and the run must still
   finish green afterwards.
3. **The verdict is the engine's.** The final state and the Gate report come from
   ``TurnResult``; nothing the server or the client does may change them.

Requires a built FreeCADCmd; skipped otherwise. Slow (tens of seconds).
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tcad.config.loader import REPO_ROOT, load_default_config, resolve_paths
from tcad.core.types import TurnState
from tcad.server.app import create_app

pytestmark = pytest.mark.contract

FREECAD_CMD = REPO_ROOT / "free-cad" / "FreeCAD" / "build" / "debug" / "bin" / "FreeCADCmd"
SCRIPT = REPO_ROOT / "tools" / "sessions" / "demo_bracket.json"
MODEL_ID = "plate"
REQUEST = "一个 80x50 的安装底板，厚度 8mm，中间开一个 40x20 的通槽"

TestClient = pytest.importorskip("fastapi.testclient").TestClient


# ══════════════════════════════════════════════════════════════════════════
# harness
# ══════════════════════════════════════════════════════════════════════════


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_port(port: int, *, timeout_s: float = 20.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.15)
    return False


def _frames(body: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into (event, data) pairs — the same framing `_sse` writes."""
    out: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        event, data = "message", None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: "):
                data = line[6:]
        if data is None:
            continue
        try:
            out.append((event, json.loads(data)))
        except json.JSONDecodeError:
            continue
    return out


class Session:
    """One completed turn, plus the client and stack that produced it."""

    def __init__(self, client, frames, services) -> None:
        self.client = client
        self.frames = frames
        self.services = services

    def of(self, kind: str) -> list[dict]:
        return [d for k, d in self.frames if k == kind]

    def agent(self, agent_kind: str) -> list[dict]:
        return [d for d in self.of("agent") if d.get("kind") == agent_kind]

    @property
    def result(self) -> dict:
        return next(d for k, d in self.frames if k == "result")

    @property
    def tools(self) -> list[dict]:
        return self.agent("tool")

    @property
    def tool_errors(self) -> list[dict]:
        return [t["error"] for t in self.tools if t.get("error")]


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    if not FREECAD_CMD.exists():
        pytest.skip(f"FreeCADCmd not built at {FREECAD_CMD}")
    if not SCRIPT.exists():
        pytest.skip(f"scripted session missing: {SCRIPT}")

    from tcad.config.settings import LlmSettings, RuntimeSettings
    from tcad.core.wiring import apply_llm_settings, build_services

    port = _free_port()
    stub = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "tools" / "stub_llm.py"),
         "--script", str(SCRIPT), "--port", str(port)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    services = None
    try:
        if not _wait_for_port(port):
            stub.kill()
            pytest.fail("stub model provider did not start")

        data_dir = tmp_path_factory.mktemp("chat_e2e")
        cfg = load_default_config()
        cfg.storage.data_dir = str(data_dir)
        cfg.storage.sqlite_path = ""
        cfg.runtime.freecad_cmd = str(FREECAD_CMD)
        resolve_paths(cfg, root=REPO_ROOT)

        services = build_services(cfg)
        apply_llm_settings(
            services,
            RuntimeSettings(llm=LlmSettings(
                provider="custom", base_url=f"http://127.0.0.1:{port}/v1",
                model="stub-scripted", request_timeout_s=60.0, max_retries=0,
            )),
            persist=False,
        )

        app = create_app(services, config=services.config)
        with TestClient(app) as client:
            client.post("/models", json={"model_id": MODEL_ID, "raw_requirement": REQUEST})
            with client.stream(
                "POST", "/chat", json={"model_id": MODEL_ID, "text": REQUEST}
            ) as r:
                assert r.status_code == 200, r.read()
                body = "".join(r.iter_text())
            yield Session(client, _frames(body), services)
    finally:
        if services is not None:
            services._worker_handle.close()
        stub.terminate()
        try:
            stub.wait(timeout=10)
        except subprocess.TimeoutExpired:
            stub.kill()


# ══════════════════════════════════════════════════════════════════════════
# framing
# ══════════════════════════════════════════════════════════════════════════


def test_the_stream_carries_all_four_frame_kinds(session):
    kinds = {k for k, _ in session.frames}
    assert "start" in kinds
    assert "progress" in kinds, "hook 事件没有流出"
    assert "agent" in kinds, "观察者事件没有流出 —— 界面将看不到模型在做什么"
    assert "result" in kinds
    assert "error" not in kinds, session.of("error")


def test_start_frame_names_the_thread(session):
    start = session.of("start")[0]
    assert start["model_id"] == MODEL_ID
    assert start["thread_id"]


def test_agent_frames_carry_the_models_own_output(session):
    model_frames = session.agent("model")
    assert model_frames, "没有 model 帧"
    assert any(d.get("text") for d in model_frames), "模型文本没有被转发"
    assert any(d.get("tool_calls") for d in model_frames), "工具调用没有被转发"

    tools = session.tools
    assert tools, "没有 tool 帧"
    assert all("name" in t and "ok" in t for t in tools)
    assert all("step" in t for t in tools)


def test_hook_frames_come_from_the_dispatcher(session):
    hooks = session.of("progress")
    assert hooks, "SSE 里没有 hook 事件"
    assert all({"event", "decision", "hook"} <= set(h) for h in hooks)


# ══════════════════════════════════════════════════════════════════════════
# failures must be legible
# ══════════════════════════════════════════════════════════════════════════


def test_no_tool_error_is_a_bare_traceback(session):
    """A raw exception means the harness lost the ability to explain itself.

    Not hypothetical: a worker error path once raised
    ``NameError: name 'E_COMPILE' is not defined`` *while formatting* the error,
    so a conflicting-constraint failure reached the model as a NameError. That
    is the whole difference between a model that can repair itself and one that
    cannot — and it was invisible until an end-to-end run went through it.
    """
    for tool in session.tools:
        message = ((tool.get("error") or {}).get("message")) or ""
        assert "NameError" not in message, message
        assert "Traceback (most recent call last)" not in message, message
        assert "name '" not in message, message


def test_the_scripted_conflict_arrives_as_a_solver_problem(session):
    """The script clashes a sketch on purpose. That must arrive as a solver error,
    attributed to a feature — the single most useful sentence available."""
    errors = session.tool_errors
    assert errors, "脚本里的失败没有产生任何工具错误"
    kinds = {e["kind"] for e in errors}
    assert "solver" in kinds, f"约束冲突没有被归类为 solver 错误：{kinds}"
    assert any(e.get("feature_id") for e in errors), "错误没有指出是哪个特征"


def test_the_failed_commit_is_reported_as_a_failed_gate(session):
    """A commit that cannot satisfy the requirements must come back with a Gate
    verdict attached, not just 'ok'."""
    commits = [t for t in session.tools if t["name"] == "ir_commit"]
    assert commits, "没有 ir_commit 被调用"
    with_gate = [c for c in commits if c.get("gate")]
    assert with_gate, "ir_commit 没有带上 Gate 结果"
    assert any(not c["gate"]["passed"] for c in with_gate), (
        "脚本里失败的 commit 没有被报告为 Gate 未通过 —— 两次失败轨迹的可见性丢失了"
    )
    assert any(c["gate"]["passed"] for c in with_gate), "没有任何一次 commit 通过 Gate"


def test_the_model_never_sees_a_raw_traceback_in_tool_content(session):
    for tool in session.tools:
        content = tool.get("content") or ""
        assert "Traceback (most recent call last)" not in content


# ══════════════════════════════════════════════════════════════════════════
# the verdict
# ══════════════════════════════════════════════════════════════════════════


def test_the_run_recovers_and_the_gate_ends_green(session):
    result = session.result
    assert result["state"] == TurnState.SUCCEEDED.value, (
        f"state={result['state']} error={result.get('error')}"
    )
    report = result["gate_report"]
    assert report["passed"] is True
    assert report["blocking_failures"] == []

    by_id = {r["check_id"]: r for r in report["results"]}
    for check_id in (
        "solid_validity", "solid_count", "bbox_spec", "mass_spec",
        "sketch_fully_constrained", "round_trip", "exportability",
    ):
        assert by_id[check_id]["status"] == "pass", (check_id, by_id[check_id]["message"])


def test_skipped_checks_are_reported_rather_than_hidden(session):
    """`wall_thickness` is advisory and cannot run without a measurement. The
    requirement is that skipping is *visible*, never silent."""
    report = session.result["gate_report"]
    assert report["skipped_checks"] == ["wall_thickness"]
    assert report["passed"] is True, "advisory 级的跳过不得影响通过"


def test_success_is_not_claimed_before_the_gate_agrees(session):
    """Ordering matters: no commit may report a pass before one actually passed."""
    passed_seen = False
    for tool in session.tools:
        if tool["name"] != "ir_commit":
            continue
        gate = tool.get("gate") or {}
        if gate.get("passed"):
            passed_seen = True
        elif passed_seen:
            pytest.fail("通过 Gate 之后又出现了未通过的 commit — 顺序被破坏")
    assert passed_seen, "整条轨迹里没有一次 commit 通过过 Gate"


def test_the_streamed_verdict_matches_a_fresh_independent_gate_run(session):
    """If the streamed report disagreed with a fresh evaluation of the same
    version, the stream would not be reporting the real Gate."""
    result = session.result
    fresh = session.services.gate.evaluate(
        result["model_id"], result["gate_report"]["ir_version"]
    )
    assert fresh.passed == result["gate_report"]["passed"]
    assert {r.check_id for r in fresh.results} == {
        r["check_id"] for r in result["gate_report"]["results"]
    }


# ══════════════════════════════════════════════════════════════════════════
# side effects
# ══════════════════════════════════════════════════════════════════════════


def test_artefacts_and_history_survive_the_turn(session):
    artifacts = session.client.get(f"/models/{MODEL_ID}/artifacts").json()
    assert any(f.endswith(".step") for f in artifacts["files"]), artifacts["files"]

    threads = session.client.get("/threads").json()["threads"]
    assert threads and threads[0]["model_id"] == MODEL_ID
    messages = session.client.get(
        f"/threads/{threads[0]['thread_id']}/messages"
    ).json()["messages"]
    assert any(m["role"] == "user" for m in messages)
    assert any(m["role"] == "assistant" for m in messages)


def test_the_rendered_view_is_reachable_for_the_completed_model(session):
    """The human's view path must work on the finished part — that is the
    capability the whole front end is built around."""
    r = session.client.get(f"/models/{MODEL_ID}/render?view=iso")
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"\x89PNG")
    assert len(r.content) > 1000, "渲染结果太小，可能是一张空图"
