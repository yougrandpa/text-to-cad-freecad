"""Rendered pixels must reach the model, with valid tool protocol and provenance."""

import base64
from copy import deepcopy

import pytest

from tcad.context.tool_images import tool_image_feedback
from tcad.core.types import ImageRef, Thread, ToolResult, ToolSpec, ToolTier, TurnKind
from tcad.llm.client import LlmReply, ToolCall
from tcad.loop.engine import UserMessage
from tests.unit.test_loop_engine import ScriptedLlm, make_engine, make_ir, make_services


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aDVkAAAAASUVORK5CYII=")


def rendered(path):
    return ToolResult(ok=True, content="artifact_id=sha256:abc, v4, status=passed",
                      images=[ImageRef(path=str(path), view="iso", width=1, height=1)])


def feedback(result, root, *, vision=True):
    return tool_image_feedback(result, name="geo_view", call_id="render4",
                               data_dir=str(root), supports_vision=vision)


def test_render_feedback_contains_actual_pixels_view_and_artifact(tmp_path):
    path = tmp_path / "iso.png"
    path.write_bytes(PNG)
    message, note = feedback(rendered(path), tmp_path)
    assert note == ""
    assert message["role"] == "user"
    assert "artifact_id=sha256:abc, v4" in message["content"][0]["text"]
    assert "iso" in message["content"][1]["text"]
    encoded = message["content"][2]["image_url"]["url"]
    assert encoded.startswith("data:image/png;base64,")
    assert base64.b64decode(encoded.split(",", 1)[1]) == PNG


def test_text_provider_gets_honest_missing_vision_note_without_reading_file(tmp_path):
    message, note = feedback(rendered(tmp_path / "missing.png"), tmp_path, vision=False)
    assert message is None
    assert "does not declare vision support" in note
    assert "do not claim" in note


def test_partial_delivery_identifies_missing_view_without_losing_valid_pixels(tmp_path):
    path = tmp_path / "iso.png"
    path.write_bytes(PNG)
    result = rendered(path)
    result.images.append(ImageRef(path=str(tmp_path / "top.png"), view="top", width=1, height=1))
    message, note = feedback(result, tmp_path)
    assert sum(part["type"] == "image_url" for part in message["content"]) == 1
    assert "top: FileNotFoundError" in note


def test_render_delivery_limits_image_bytes_and_number(tmp_path, monkeypatch):
    import tcad.context.tool_images as module
    path = tmp_path / "iso.png"
    path.write_bytes(PNG)
    monkeypatch.setattr(module, "MAX_IMAGE_BYTES", len(PNG) - 1)
    message, note = feedback(rendered(path), tmp_path)
    assert message is None
    assert "not delivered" in note
    monkeypatch.setattr(module, "MAX_IMAGE_BYTES", len(PNG))
    result = rendered(path)
    result.images *= 5
    message, note = feedback(result, tmp_path)
    assert sum(part["type"] == "image_url" for part in message["content"]) == 4
    assert "additional views omitted" in note


@pytest.mark.parametrize("failure", ["missing", "not_image", "outside", "symlink"])
def test_unavailable_or_untrusted_images_are_not_sent(tmp_path, failure):
    root = tmp_path / "data"
    root.mkdir()
    path = root / "iso.png"
    if failure == "not_image":
        path.write_text("private data")
    elif failure in ("outside", "symlink"):
        outside = tmp_path / "private.png"
        outside.write_bytes(PNG)
        if failure == "outside":
            path = outside
        else:
            path.symlink_to(outside)
    message, note = feedback(rendered(path), root)
    assert message is None
    assert "not delivered" in note
    assert "Do not claim visual inspection" in note
    assert "private data" not in note


@pytest.mark.parametrize("vision", [True, False])
async def test_next_model_request_receives_images_after_all_tool_results(vision):
    class CapturingLlm(ScriptedLlm):
        descriptor = {"supports_vision": vision}

        async def chat(self, **kwargs):
            self.requests.append(deepcopy(kwargs["messages"]))
            return await super().chat(**kwargs)

    llm = CapturingLlm([LlmReply(tool_calls=[
        ToolCall(id="render4", name="render_probe", args={}),
        ToolCall(id="measure4", name="measure_probe", args={}),
    ]), LlmReply(text="review")])
    llm.requests = []
    engine = make_engine(make_services(make_ir(), llm, gate_passed=True), max_steps=2)
    from pathlib import Path
    path = Path(engine.config.data_dir) / "iso.png"
    path.write_bytes(PNG)

    async def render_handler(args, ctx):
        return rendered(path)

    async def measure_handler(args, ctx):
        return ToolResult(ok=True, content="measured bbox")

    for name, handler in [("render_probe", render_handler), ("measure_probe", measure_handler)]:
        engine.registry.register_tool(ToolSpec(name=name, tier=ToolTier.READ,
            description="test", params_schema={"type": "object"}, handler=handler))
    await engine.run_turn(Thread(thread_id="th1", model_id="m1"),
                          UserMessage(kind=TurnKind.CREATE, text="mouse"))
    request = llm.requests[1]
    round_index = next(i for i, message in enumerate(request) if message.get("tool_calls"))
    assert [message["role"] for message in request[round_index:round_index + 3]] == ["assistant", "tool", "tool"]
    if vision:
        assert request[round_index + 3]["role"] == "user"
        assert "data:image/png;base64," in str(request[round_index + 3])
    else:
        assert "do not claim" in request[round_index + 1]["content"]
        assert "image_url" not in str(request)
