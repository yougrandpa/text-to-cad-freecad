"""Layer 3 — a REAL LLM drives the whole path for sample A.

Unlike ``tests/contract/test_chat_end_to_end.py`` (which replays a scripted
model and therefore only proves orchestration), this module points the engine
at a live model service and gives it the sample-A sentence with nothing
pre-written. Every number is still judged by the deterministic layers: the
Gate verdict comes from ``TurnResult``, and the final volume is measured by
re-importing the delivered STEP through the real kernel — never taken from
anything the model said.

Configuration is explicit and opt-in — the test SKIPS unless a provider is
configured. Precedence:

    TCAD_E2E_BASE_URL / TCAD_E2E_MODEL / TCAD_E2E_API_KEY   (environment)
    data/settings.json -> llm.{base_url,model,api_key}      (what the UI saved)

The key is passed straight into the settings object; nothing here echoes it.

The provider is then **asked to serve one trivial completion** before the test
runs (``provider_probe``). Checking ``GET /models`` was not enough: a key whose
account has no balance answers 200 there and 402 to every completion, so such a
provider was reported as "reachable" and the run failed with four assertions
about the model's behaviour rather than the one true reason. A provider that
cannot serve is an environment condition, so it is a SKIP that names the cause.

What proves the chain (each item corresponds to one test):

  1. the build Gate passed and the engine exposes its requirement-review scope;
  2. real artefacts — .step/.stl/.FCStd — exist and are downloadable;
  3. the delivered STEP, re-imported and measured by FreeCAD, has the sample-A
     volume 25600 mm³ (rel 1e-6, the same tolerance as the kernel-layer test).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tcad.config.loader import REPO_ROOT, load_default_config, resolve_paths
from tcad.core.types import TurnState
from tcad.server.app import create_app
from tests.e2e.provider_probe import configured_provider, probe_provider

FREECAD_CMD = Path(os.environ.get("TCAD_FREECAD_CMD", str(
    REPO_ROOT / "free-cad" / "FreeCAD" / "build" / "debug" / "bin" / "FreeCADCmd")))

BASE_URL, MODEL, API_KEY = configured_provider(REPO_ROOT)
API_KEY = API_KEY or None

MODEL_ID = "real_llm_plate"
REQUEST = "80×50×8 的矩形板，中心有 40×20 的矩形贯穿开口"
EXPECTED_VOLUME = 25600.0  # 80*50*8 - 40*20*8, judged by the kernel below

TestClient = pytest.importorskip("fastapi.testclient").TestClient

# Probed once, at import, so the skip message can carry the real reason. With no
# provider configured this is a local no-op (never touches the network).
_PROBE_OK, _PROBE_REASON = (True, "") if not (BASE_URL and MODEL) else probe_provider(
    BASE_URL, MODEL, API_KEY
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(not BASE_URL or not MODEL, reason=(
        "real-LLM e2e not configured: set TCAD_E2E_BASE_URL and TCAD_E2E_MODEL "
        "(optionally TCAD_E2E_API_KEY), or save a provider in the UI, to run layer 3"
    )),
    pytest.mark.skipif(not FREECAD_CMD.exists(), reason="FreeCADCmd not built"),
    pytest.mark.skipif(not _PROBE_OK, reason=_PROBE_REASON),
]


def _frames(body: str) -> list[tuple[str, dict]]:
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


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    from tcad.config.settings import LlmSettings, RuntimeSettings
    from tcad.core.wiring import apply_llm_settings, build_services

    data_dir = tmp_path_factory.mktemp("real_llm_e2e")
    cfg = load_default_config()
    cfg.storage.data_dir = str(data_dir)
    cfg.storage.sqlite_path = ""
    cfg.runtime.freecad_cmd = str(FREECAD_CMD)
    resolve_paths(cfg, root=REPO_ROOT)

    services = build_services(cfg)
    apply_llm_settings(
        services,
        RuntimeSettings(llm=LlmSettings(
            provider="custom", base_url=BASE_URL, model=MODEL, api_key=API_KEY,
            request_timeout_s=180.0, max_retries=1,
        )),
        persist=False,
    )

    app = create_app(services, config=services.config)
    try:
        with TestClient(app) as client:
            client.post("/models", json={"model_id": MODEL_ID, "raw_requirement": REQUEST})
            with client.stream(
                "POST", "/chat", json={"model_id": MODEL_ID, "text": REQUEST}
            ) as r:
                assert r.status_code == 200, r.read()
                body = "".join(r.iter_text())
            frames = _frames(body)
            yield {"client": client, "frames": frames, "services": services}
    finally:
        services._worker_handle.close()


def _result(session) -> dict:
    return next(d for k, d in session["frames"] if k == "result")


def test_the_build_passed_and_requirement_review_is_explicit(session):
    """The verdict is the engine's, not the model's."""
    result = _result(session)
    assert result["state"] in (TurnState.SUCCEEDED.value, TurnState.DRAFT.value), (
        f"state={result['state']} error={result.get('error')}"
    )
    review = result["completion_review"]
    assert review is not None and review["scope"] == "recorded_constraints"
    if result["state"] == TurnState.SUCCEEDED.value:
        assert review["verified"] and not review["remaining_work"]
    else:
        assert not review["verified"] and review["remaining_work"]
    report = result["gate_report"]
    assert report["passed"] is True, report["blocking_failures"]
    assert report["blocking_failures"] == []

    by_id = {r["check_id"]: r for r in report["results"]}
    for check_id in ("solid_validity", "solid_count", "round_trip", "exportability"):
        assert by_id[check_id]["status"] == "pass", (check_id, by_id[check_id]["message"])
    # `GET /approvals` answers `{"pending": [...]}` — the shape the front end
    # destructures (`const { pending } = await api("/approvals")`). Asserting
    # truthiness of the whole body was a bug that could not surface while this
    # layer was BLOCKED: `{"pending": []}` is truthy, so the check failed even
    # when nothing was pending.
    assert session["client"].get("/approvals").json()["pending"] == [], (
        "normal modelling path must not leave pending privileged approvals"
    )


def _artifact_names(client) -> list[str]:
    """The delivered file list, from the shape the endpoint actually returns.

    This used to iterate the response body directly, which yielded its *keys*
    (`dir`, `files`, `model_id`, …) and made the assertions below fail with
    "交付清单里没有 .step: ['dir', 'files', ...]" — a test bug that had never
    been reachable, because the layer was blocked on a provider for its whole
    life. A blocked test is an unverified test.
    """
    body = client.get(f"/models/{MODEL_ID}/artifacts")
    assert body.status_code == 200, body.text
    payload = body.json()
    assert isinstance(payload, dict) and "files" in payload, payload
    return list(payload["files"])


def test_real_artifacts_are_delivered_and_downloadable(session, tmp_path):
    names = _artifact_names(session["client"])
    assert names, "no artifacts recorded for the model"

    for suffix in ("step", "stl", "FCStd"):
        candidate = next((n for n in names if n.endswith("." + suffix)), None)
        assert candidate, f"交付清单里没有 .{suffix}: {sorted(names)}"
        r = session["client"].get(f"/models/{MODEL_ID}/artifacts/{candidate}")
        assert r.status_code == 200, (candidate, r.status_code)
        p = tmp_path / candidate
        p.write_bytes(r.content)
        assert p.stat().st_size > 0, f"{candidate} 是空文件"


def test_delivered_step_measures_the_sample_a_volume(session, tmp_path):
    """The number that closes the loop: the file a user would download,
    re-imported into the real kernel, must measure 25600 mm³."""
    step_name = next(n for n in _artifact_names(session["client"])
                     if n.endswith(".step"))
    r = session["client"].get(f"/models/{MODEL_ID}/artifacts/{step_name}")
    step_path = tmp_path / step_name
    step_path.write_bytes(r.content)

    worker = session["services"]._worker_handle
    res = worker.request_sync("import_asset", {"path": str(step_path)}, timeout_s=180.0)
    assert res["ok"] is True, f"回读 STEP 失败: {res}"
    assert res["shape_summary"]["is_valid"] is True
    assert res["shape_summary"]["solids"] == 1
    assert res["shape_summary"]["volume"] == pytest.approx(EXPECTED_VOLUME, rel=1e-6), (
        f"交付的 STEP 体积 = {res['shape_summary']['volume']}，期望 {EXPECTED_VOLUME}"
    )


def test_the_model_actually_called_tools(session):
    """Guard against a lucky one-shot answer: the model must have used the
    tool surface (IR patch + commit), i.e. the IR really came out of its tool
    calls, not out of its prose."""
    agent = [d for k, d in session["frames"] if k == "agent"]
    model_frames = [d for d in agent if d.get("kind") == "model"]
    tool_frames = [d for d in model_frames if d.get("tool_calls")]
    assert tool_frames, "模型没有发起任何工具调用 —— 不是工具驱动链路"
    tools = [t for d in model_frames for t in (d.get("tool_calls") or [])]
    names = {t.get("name") or t.get("function", {}).get("name") for t in tools}
    assert any(n and "commit" in n for n in names), f"没有 commit 工具调用: {names}"
