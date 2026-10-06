"""Close an actual HTTP stream after an actual persisted patch; never rerun it."""

import asyncio
import time

import httpx

from tcad.llm.client import LlmReply, ToolCall
from tcad.server.app import ChatRequest, create_app
from tcad.server.operations import OperationStore, fingerprint
from tcad.selection.types import SelectionError
from tests.http_support import serve
from tests.reference_support import MODEL, THREAD, edit_payload, protocol_services, publish
from tests.unit.test_server import _sse


class PatchThenWait:
    def __init__(self):
        self.calls = 0
        self.cancelled = False

    async def chat(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return LlmReply(tool_calls=[ToolCall(id="resize",name="cad_set_hole_diameter",
                args={"diameter_mm":8,"reason":"Resize before disconnect"})])
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def test_disconnect_after_patch_persists_terminal_and_replays_after_server_restart(tmp_path):
    model = PatchThenWait()
    services = protocol_services(tmp_path, model=model)
    services.config.loop.max_steps_per_turn = None
    publish(tmp_path, services.store)
    app = create_app(services)
    with serve(app) as base, httpx.Client(timeout=10, trust_env=False) as client:
        catalog = client.get(f"{base}/models/{MODEL}/selection-targets").json()
        ref = next(t["ref"] for t in catalog["targets"] if t["ref"].get("sketch_id") == "hole_circle")
        payload = edit_payload(ref)
        with client.stream("POST", f"{base}/chat", json=payload) as stream:
            assert stream.status_code == 200
            for line in stream.iter_lines():
                if line.startswith("data: "):
                    import json
                    event = json.loads(line[6:])
                    if event.get("kind") == "tool" and event.get("name") == "cad_set_hole_diameter":
                        assert event["ok"] is True
                        break
            else:
                raise AssertionError("Patch event did not arrive")
        # The context manager closed the socket, not the model or ledger.
        request = ChatRequest.model_validate(payload)
        request_hash = fingerprint({**request.model_dump(mode="json",exclude={"request_id"}),"thread_id":THREAD,"model_id":MODEL})
        operations = OperationStore(tmp_path)
        deadline = time.monotonic() + 5
        outcome = None
        while time.monotonic() < deadline:
            try:
                outcome = operations.read(payload["operation_id"],request_hash)
            except SelectionError:
                pass
            if outcome and not app.state.turns and model.cancelled:
                break
            time.sleep(0.01)
        assert outcome["data"]["code"] == "operation_interrupted"
        assert model.cancelled and not app.state.turns
        assert services.store.load(MODEL).find_sketch("hole_circle").geometry[0].radius == 4
        assert services.store.current_version(MODEL) == 1
        calls = model.calls
        replay = _sse(client.post(f"{base}/chat",json=payload).text)
        assert replay[0][1]["replayed"] is True and replay[-1][1] == outcome["data"]
        assert model.calls == calls
    if app.state.session_db:
        app.state.session_db.close()
    restarted = create_app(services)
    with serve(restarted) as base, httpx.Client(timeout=10, trust_env=False) as client:
        replay = _sse(client.post(f"{base}/chat",json=payload).text)
        assert replay[-1][1]["code"] == "operation_interrupted" and model.calls == calls
        assert services.store.current_version(MODEL) == 1
    if restarted.state.session_db:
        restarted.state.session_db.close()
