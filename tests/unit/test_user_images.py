"""Images reach the model, survive history/compaction, and fail before mutation."""

import base64
from copy import deepcopy
from io import BytesIO
import sqlite3
from types import SimpleNamespace

from PIL import Image
from pydantic import ValidationError
import pytest
from fastapi.testclient import TestClient

from tcad.config.settings import LlmSettings, RuntimeSettings
from tcad.context.assembler import ContextAssembler, ContextBudget, to_openai_messages
from tcad.context.history import messages_from_rows
from tcad.core.user_images import UserImage
from tcad.llm.client import LlmReply
from tcad.ir.schema import IrDocument
from tcad.loop.budget import BudgetLimits
from tcad.loop.engine import LoopEngine, UserMessage
from tcad.server.app import ChatRequest, create_app
from tcad.store.session_db import SessionDB
from tests.unit.test_server import make_services, _sse


def image_payload(format="PNG", mime="image/png"):
    buffer = BytesIO()
    Image.new("RGB", (8, 6), "red").save(buffer, format=format)
    return {"name": "参考图.png", "data_url": f"data:{mime};base64," + base64.b64encode(buffer.getvalue()).decode()}


@pytest.mark.parametrize("format,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp"), ("GIF", "image/gif")])
def test_supported_formats(format, mime):
    assert UserImage(**image_payload(format, mime)).data_url.startswith(f"data:{mime};base64,")


@pytest.mark.parametrize("value", ["https://example.com/a.png", "data:image/svg+xml;base64,PHN2Zz4=", "data:image/png;base64,###", "data:image/png;base64,aGVsbG8=", "data:image/jpeg;base64," + image_payload()["data_url"].split(",")[1]])
def test_remote_invalid_and_mislabeled_images_are_rejected(value):
    with pytest.raises(ValidationError):
        UserImage(data_url=value)


def test_count_and_byte_limits():
    with pytest.raises(ValidationError):
        ChatRequest(model_id="m", text="", images=[image_payload()] * 5)
    with pytest.raises(ValidationError):
        UserImage(data_url="data:image/png;base64," + base64.b64encode(b"x" * (5 * 1024 * 1024 + 1)).decode())


def test_unsupported_model_rejects_before_history_or_model_calls(tmp_path):
    services = make_services(tmp_path, max_steps=1)
    with TestClient(create_app(services)) as client:
        session = client.post("/sessions", json={}).json()
        response = client.post("/chat", json={**session, "text": "参考这张图", "images": [image_payload()]})
        assert response.status_code == 400
        assert "不支持图片输入" in response.json()["detail"]
        assert services.llm.client.calls == 0
        assert client.get(f"/threads/{session['thread_id']}/messages").json()["messages"] == []


@pytest.mark.parametrize("assembled", [False, True])
def test_images_and_history_reach_model_once_and_are_replayed(tmp_path, assembled):
    services = make_services(tmp_path, max_steps=1)
    services.settings = RuntimeSettings(llm=LlmSettings(provider="custom", model="fake", supports_vision=True))
    captured = []

    class VisionLlm:
        descriptor = {"supports_vision": True}

        async def chat(self, *, messages, **kwargs):
            captured.append(deepcopy(messages))
            return LlmReply(text="收到参考图")

    services.llm = VisionLlm()
    if assembled:
        services.context_assembler = ContextAssembler(ContextBudget(window_tokens=128_000))
    with TestClient(create_app(services)) as client:
        session = client.post("/sessions", json={}).json()
        payload = image_payload()
        response = client.post("/chat", json={**session, "text": "", "images": [payload]})
        assert response.status_code == 200
        assert any(event == "result" for event, _ in _sse(response.text))
        wire = captured[0][-1]
        assert wire["role"] == "user"
        assert wire["content"][0]["image_url"]["url"] == payload["data_url"]
        assert sum(isinstance(message["content"], list) for message in captured[0]) == 1
        rows = client.get(f"/threads/{session['thread_id']}/messages").json()["messages"]
        assert rows[0]["images"] == [payload]
        assert rows[0]["content"] == ""
        client.post("/chat", json={**session, "text": "沿用刚才的图"})
        if assembled:
            assert any(isinstance(message["content"], list) for message in captured[1][:-1])


async def test_current_images_survive_authoritative_context_rebuild(tmp_path):
    services = make_services(tmp_path)
    services.store.create("m", IrDocument(model_id="m"))
    engine = LoopEngine(services, None, BudgetLimits(), services.loop_config)
    engine._context_blocks = lambda turn: {"requirements_text": "keep dimensions", "digest_text": "current CAD", "gate_report_text": ""}
    request = UserMessage(text="保持图片中的尺寸", images=[image_payload()])
    turn = SimpleNamespace(thread_id="th", model_id="m", base_ir_version=0)
    before = await engine._build_messages(request, turn)
    rebuilt = await engine._build_messages(request, turn, require_state=True)
    assert before[-1] == rebuilt[-1]
    assert rebuilt[-1]["content"][1]["image_url"]["url"] == request.images[0].data_url


def test_existing_database_migrates_without_losing_text_and_keeps_images(tmp_path):
    path = tmp_path / "history.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE messages(message_id TEXT PRIMARY KEY, thread_id TEXT, turn_id TEXT, role TEXT, content TEXT, created_at TEXT)")
        connection.execute("INSERT INTO messages VALUES('old','th',NULL,'user','原来的文字','today')")
    db = SessionDB(path)
    db.add_message("th", "user", "", images=[image_payload()])
    db.close()
    db = SessionDB(path)
    rows = db.list_messages("th")
    assert rows[0]["content"] == "原来的文字"
    assert rows[0]["images"] == []
    history = messages_from_rows(rows)
    assert len(history) == 2
    assert isinstance(to_openai_messages(history, supports_vision=True)[1]["content"], list)
    text_only = to_openai_messages(history)
    assert all(isinstance(message["content"], str) for message in text_only)
    assert "不支持读取图片" in text_only[1]["content"]
    assert len(messages_from_rows(rows, current_text="")) == 1
    db.close()
