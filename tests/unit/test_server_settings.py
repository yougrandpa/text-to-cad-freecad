"""HTTP surface for model configuration, rendering, artefacts and history.

Two things here are not ordinary feature tests:

* the **artefact containment** check — a ``{file_path:path}`` route accepts
  ``../../etc/passwd`` verbatim, and a check that only compares strings is the
  version that looks correct and is not;
* the **no-service-stack** behaviour of ``/settings/llm`` — a broken worker is
  exactly when someone needs to change the model configuration, so those
  endpoints must not depend on a live stack.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tcad.config.settings import load_runtime_settings
from tcad.core.types import ImageRef, Mesh
from tcad.ir.schema import IrDocument
from tcad.server.app import _resolve_artifact, create_app

from tests.unit.test_server import make_services

TestClient = pytest.importorskip("fastapi.testclient").TestClient


# ══════════════════════════════════════════════════════════════════════════
# fakes that can actually produce geometry
# ══════════════════════════════════════════════════════════════════════════


class MeshWorker:
    """A worker that answers ``tessellate`` with a real (tiny) mesh."""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[str] = []

    def request(self, method, params=None, *, timeout_s=30.0):
        self.calls.append(method)
        if method != "tessellate":
            return {"ok": True, "result": {}}
        if not self.ok:
            return {"ok": False, "error": {"kind": "compile", "message": "boom"}}
        return {
            "ok": True,
            "result": {
                "mesh": Mesh(
                    vertices=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
                    facets=[(0, 1, 2)],
                    volume=1.0,
                ).model_dump()
            },
        }


class RecordingRenderer:
    """Writes a placeholder PNG and reports it, mirroring the real adapter."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def render(self, mesh, *, out_dir, views, style, width, height):
        self.calls.append(
            {"out_dir": out_dir, "views": list(views), "style": style,
             "width": width, "height": height}
        )
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        refs = []
        for view in views:
            p = out / f"{view}.png"
            p.write_bytes(b"\x89PNG\r\n\x1a\n-fake")
            refs.append(ImageRef(path=str(p), view=view, width=width, height=height))
        return refs


def _client(tmp_path: Path, **svc_kwargs):
    services = make_services(tmp_path, **svc_kwargs)
    services.worker = MeshWorker()
    services.renderer = RecordingRenderer()
    return services


@pytest.fixture()
def client(tmp_path):
    """A stack with a worker that can mesh and a renderer that writes files."""
    services = _client(tmp_path)
    app = create_app(services)
    with TestClient(app) as c:
        c.services = services
        yield c


# ══════════════════════════════════════════════════════════════════════════
# providers
# ══════════════════════════════════════════════════════════════════════════


def test_providers_endpoint_lists_presets_without_credentials(client):
    body = client.get("/settings/providers").json()
    ids = [p["id"] for p in body["providers"]]
    assert "deepseek" in ids and "openai" in ids and "ollama" in ids
    blob = json.dumps(body)
    assert "api_key\"" not in blob  # only api_key_env may appear
    deepseek = next(p for p in body["providers"] if p["id"] == "deepseek")
    assert deepseek["api_key_env"] == "DEEPSEEK_API_KEY"
    assert deepseek["base_url"].startswith("https://api.deepseek.com")


# ══════════════════════════════════════════════════════════════════════════
# reading settings
# ══════════════════════════════════════════════════════════════════════════


def test_get_settings_masks_the_key(client, tmp_path):
    client.put(
        "/settings/llm",
        json={"provider": "deepseek", "api_key": "sk-live-abcdefghijkl-9f2a"},
    )
    body = client.get("/settings/llm").json()
    s = body["settings"]
    assert s["provider"] == "deepseek"
    assert s["api_key_set"] is True
    assert s["api_key_source"] == "settings"
    # the plaintext must not appear anywhere in the response
    assert "sk-live-abcdefghijkl-9f2a" not in json.dumps(body)
    assert s["api_key_masked"].endswith("9f2a")
    assert body["persisted"] is True


def test_get_settings_falls_back_to_yaml_when_nothing_is_stored(client):
    body = client.get("/settings/llm").json()
    assert body["persisted"] is False
    assert body["settings"]["provider"]  # guessed from the YAML base_url


# ══════════════════════════════════════════════════════════════════════════
# writing settings
# ══════════════════════════════════════════════════════════════════════════


def test_put_hot_swaps_the_live_stack(client, tmp_path):
    """The whole point: no restart, and the engine keeps the same LLM object."""
    before = client.services.llm
    r = client.put(
        "/settings/llm",
        json={"provider": "deepseek", "model": "deepseek-v4-pro", "temperature": 0.05},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == "hot-swapped"
    assert body["descriptor"]["model"] == "deepseek-v4-pro"
    assert body["descriptor"]["base_url"] == "https://api.deepseek.com/v1"

    assert client.services.llm is before, "the LLM holder was replaced"
    assert client.services.llm.generation == 1
    # the engine reads temperature from LoopConfig on every call, so it must move
    assert client.services.loop_config.llm_temperature == 0.05
    assert client.services.loop_config.llm_model == "deepseek-v4-pro"


def test_put_persists_to_disk(client, tmp_path):
    client.put("/settings/llm", json={"provider": "deepseek", "model": "deepseek-v4-pro"})
    data_dir = client.services.config.storage.data_dir
    stored = load_runtime_settings(data_dir)
    assert stored is not None
    assert stored.llm.provider == "deepseek"
    assert stored.llm.model == "deepseek-v4-pro"


def test_put_omitting_the_key_keeps_the_stored_one(client):
    client.put("/settings/llm", json={"provider": "deepseek", "api_key": "sk-keep-me-please"})
    # a later edit that does not mention the key must not wipe it
    client.put("/settings/llm", json={"temperature": 0.33})
    body = client.get("/settings/llm").json()
    assert body["settings"]["api_key_set"] is True
    assert body["settings"]["temperature"] == 0.33


def test_put_with_explicit_null_clears_the_key(client):
    client.put("/settings/llm", json={"provider": "deepseek", "api_key": "sk-temp-value"})
    assert client.get("/settings/llm").json()["settings"]["api_key_set"] is True
    client.put("/settings/llm", json={"api_key": None})
    assert client.get("/settings/llm").json()["settings"]["api_key_set"] is False


def test_switching_provider_drops_the_previous_endpoint(client):
    """Switching providers must not keep the old base_url — that would send the
    request to the company the user just navigated away from."""
    client.put("/settings/llm", json={"provider": "deepseek", "model": "deepseek-v4-pro"})
    client.put("/settings/llm", json={"provider": "ollama"})
    s = client.get("/settings/llm").json()["settings"]
    assert s["provider"] == "ollama"
    assert s["base_url"] == "http://127.0.0.1:11434/v1"
    assert s["model"] == "qwen2.5:32b"


def test_explicit_base_url_survives_an_unrelated_edit(client):
    client.put(
        "/settings/llm",
        json={"provider": "custom", "base_url": "http://box:9000/v1", "model": "mine"},
    )
    client.put("/settings/llm", json={"temperature": 0.9})
    s = client.get("/settings/llm").json()["settings"]
    assert s["base_url"] == "http://box:9000/v1"
    assert s["model"] == "mine"


def test_put_rejects_a_configuration_that_cannot_be_built(client):
    """An empty endpoint must be refused rather than silently defaulting to
    api.openai.com, and the previous working configuration must survive."""
    client.put("/settings/llm", json={"provider": "custom", "base_url": "http://ok:1/v1", "model": "m"})
    r = client.put("/settings/llm", json={"provider": "custom", "base_url": "", "model": "m"})
    assert r.status_code == 400
    assert client.services.llm.descriptor["base_url"] == "http://ok:1/v1"


def test_put_rejects_an_out_of_range_temperature(client):
    r = client.put("/settings/llm", json={"temperature": 9.0})
    assert r.status_code == 422  # pydantic validation at the edge


# ══════════════════════════════════════════════════════════════════════════
# settings without a service stack
# ══════════════════════════════════════════════════════════════════════════


def test_settings_work_without_building_a_service_stack(tmp_path):
    """If FreeCADCmd will not start, the operator must still be able to point
    the harness at a different model. So these endpoints must not call svc()."""
    from tcad.config.schema import Config

    cfg = Config()
    cfg.storage.data_dir = str(tmp_path / "data")
    app = create_app(None, config=cfg)
    with TestClient(app) as c:
        body = c.get("/settings/llm").json()
        assert "settings" in body
        assert body["hot_swap_available"] is False

        r = c.put("/settings/llm", json={"provider": "deepseek", "model": "deepseek-v4-pro"})
        assert r.status_code == 200
        assert r.json()["applied"] == "persisted"
        assert "note" in r.json()

        # and it really is on disk
        stored = load_runtime_settings(cfg.storage.data_dir)
        assert stored is not None and stored.llm.provider == "deepseek"

        # no worker was started by any of this
        assert app.state.services is None


# ══════════════════════════════════════════════════════════════════════════
# probe
# ══════════════════════════════════════════════════════════════════════════


def test_probe_reports_failure_for_an_unreachable_endpoint(client):
    r = client.post(
        "/settings/llm/probe",
        json={"provider": "custom", "base_url": "http://127.0.0.1:1/v1", "model": "m"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"]
    # which transport was used must be legible in the failure
    assert "直连" in body["error"] or "代理" in body["error"]


def test_probe_does_not_change_the_stored_settings(client):
    """A probe answers a question; it must not be a back-door save."""
    client.put("/settings/llm", json={"provider": "deepseek", "model": "deepseek-v4-pro"})
    client.post("/settings/llm/probe", json={"provider": "ollama", "model": "llama3"})
    assert client.get("/settings/llm").json()["settings"]["model"] == "deepseek-v4-pro"


# ══════════════════════════════════════════════════════════════════════════
# model listing
# ══════════════════════════════════════════════════════════════════════════


def test_models_endpoint_falls_back_to_preset_candidates(client):
    body = client.get("/settings/models?live=false&provider=deepseek").json()
    assert body["source"] == "preset"
    assert "deepseek-chat" in body["models"]


def test_models_endpoint_is_honest_when_the_provider_is_unreachable(client):
    client.put("/settings/llm", json={"provider": "custom", "base_url": "http://127.0.0.1:1/v1", "model": "m"})
    body = client.get("/settings/models").json()
    assert body["ok"] is False
    assert body["source"] == "preset"
    assert body["error"]


# ══════════════════════════════════════════════════════════════════════════
# artefacts: containment
# ══════════════════════════════════════════════════════════════════════════


def test_resolve_artifact_accepts_a_file_inside_the_root(tmp_path):
    (tmp_path / "iso.png").write_bytes(b"x")
    assert _resolve_artifact(tmp_path, "iso.png").name == "iso.png"
    sub = tmp_path / "nested"
    sub.mkdir()
    (sub / "a.step").write_text("d")
    assert _resolve_artifact(tmp_path, "nested/a.step").name == "a.step"


@pytest.mark.parametrize(
    "escape",
    [
        "../outside.txt",
        "../../etc/passwd",
        "nested/../../outside.txt",
        "/etc/passwd",
        "",
    ],
)
def test_resolve_artifact_refuses_to_escape_the_root(tmp_path, escape):
    root = tmp_path / "artifacts"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("secret")
    with pytest.raises(Exception) as ei:
        _resolve_artifact(root, escape)
    assert getattr(ei.value, "status_code", None) in (400, 404)


def test_resolve_artifact_refuses_a_symlink_pointing_out(tmp_path):
    """String comparison would pass this; resolution does not."""
    root = tmp_path / "artifacts"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    (root / "link.txt").symlink_to(secret)
    with pytest.raises(Exception) as ei:
        _resolve_artifact(root, "link.txt")
    assert getattr(ei.value, "status_code", None) == 400


def test_artifact_endpoint_serves_a_real_file(client, tmp_path):
    _seed_model(client)
    d = client.services.store.artifact_dir("m1", 0)
    d.mkdir(parents=True, exist_ok=True)
    (d / "part.step").write_text("ISO-10303-21;")
    r = client.get("/models/m1/artifacts/part.step")
    assert r.status_code == 200
    assert r.text.startswith("ISO-10303")


def test_artifact_endpoint_404s_a_missing_file(client):
    _seed_model(client)
    assert client.get("/models/m1/artifacts/nope.png").status_code == 404


@pytest.mark.parametrize(
    "path",
    ["/models/ghost/ir", "/models/ghost/artifacts", "/models/ghost/artifacts/x.png"],
)
def test_an_unknown_model_is_a_404_everywhere(client, path):
    """Not a 500.

    The underlying `current_version()` answers 0 for "no such model", and 0 is
    also a legitimate version (a freshly created model is at v0) — so any
    endpoint that trusted it was one missing file away from a server error. The
    UI keys its "no model yet" message off this status.
    """
    assert client.get(path).status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# disk path -> servable URL
#
# The engine reports absolute paths (that is what the worker wrote). The browser
# cannot use those, and without the translation the viewport has no way to show
# a view the model just rendered.
# ══════════════════════════════════════════════════════════════════════════


def test_artifact_url_maps_a_disk_path_to_a_servable_url():
    from tcad.server.app import artifact_url_for

    assert artifact_url_for("/var/data/artifacts/plate/v4/iso.png") == (
        "/models/plate/artifacts/iso.png?version=4"
    )
    # a relative data dir must work the same way
    assert artifact_url_for("./data/artifacts/plate/v12/top.png") == (
        "/models/plate/artifacts/top.png?version=12"
    )


def test_artifact_url_handles_a_nested_filename():
    from tcad.server.app import artifact_url_for

    assert artifact_url_for("/x/artifacts/m/v2/views/iso.png") == (
        "/models/m/artifacts/views/iso.png?version=2"
    )


@pytest.mark.parametrize(
    "path",
    [
        None,
        "",
        "/tmp/whatever.png",                    # not an artefact at all
        "/x/artifacts/plate/iso.png",           # no version directory
        "/x/artifacts/plate/",                  # nothing after the version dir
    ],
)
def test_artifact_url_refuses_to_guess(path):
    """A wrong link is worse than no link: the UI would show a broken image and
    look like the render failed."""
    from tcad.server.app import artifact_url_for

    assert artifact_url_for(path) is None


def test_the_url_it_produces_is_actually_servable(client):
    """The two halves must agree — this is the join that silently breaks if
    either side changes its path layout."""
    from tcad.server.app import artifact_url_for

    _seed_model(client)
    d = client.services.store.artifact_dir("m1", 0)
    d.mkdir(parents=True, exist_ok=True)
    target = d / "iso.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n-fake")

    url = artifact_url_for(str(target))
    assert url is not None
    assert client.get(url).status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# on-demand rendering
# ══════════════════════════════════════════════════════════════════════════


def _seed_model(client, model_id: str = "m1"):
    client.services.store.create(model_id, IrDocument(model_id=model_id))


def test_render_produces_a_png(client, tmp_path):
    _seed_model(client)
    r = client.get("/models/m1/render?view=iso")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")


def test_render_does_not_retessellate_on_a_second_call(client):
    """The human's view path must be cheap to repeat — that is the entire reason
    it is separate from the model's `geo_view` tool."""
    _seed_model(client)
    assert client.get("/models/m1/render?view=iso").status_code == 200
    assert client.get("/models/m1/render?view=iso").status_code == 200
    assert client.services.worker.calls.count("tessellate") == 1


def test_render_force_bypasses_the_cache(client):
    _seed_model(client)
    client.get("/models/m1/render?view=iso")
    client.get("/models/m1/render?view=iso&force=true")
    assert client.services.worker.calls.count("tessellate") == 2


def test_render_rejects_an_unknown_view(client):
    _seed_model(client)
    assert client.get("/models/m1/render?view=sideways").status_code == 400


def test_render_rejects_a_traversal_shaped_style(client):
    """``view`` had an allowlist; ``style`` had nothing.

    ``style`` is the one interpolated into the cache filename, so a value like
    ``../../../../escaped`` resolved *outside* the version directory — both as
    the file the endpoint serves on a cache hit and as the target
    ``produced.replace()`` moves the render onto. Asserting the 400 alone would
    not prove the boundary held, so this also checks nothing appeared outside.
    """
    _seed_model(client)
    d = client.services.store.artifact_dir("m1", 0)
    r = client.get("/models/m1/render?view=iso&style=../../../../escaped")
    assert r.status_code == 400, r.text
    assert not list(d.parent.glob("escaped*")), (
        "the traversal wrote outside the version directory: "
        f"{[p.name for p in d.parent.glob('escaped*')]}"
    )


def test_render_rejects_an_unknown_style(client):
    _seed_model(client)
    assert client.get("/models/m1/render?view=iso&style=wireframe").status_code == 400


def test_render_accepts_every_declared_style(client):
    """The allowlist must be the set the rasteriser can actually draw — a style
    that renders must not be refused, and it must come from ``RenderStyle`` so
    the two cannot drift."""
    from typing import get_args

    from tcad.core.types import RenderStyle

    _seed_model(client)
    styles = sorted(get_args(RenderStyle))
    assert styles, "RenderStyle is empty — the allowlist would reject everything"
    for style in styles:
        r = client.get(f"/models/m1/render?view=iso&style={style}")
        assert r.status_code == 200, (style, r.text)


def test_render_rejects_absurd_dimensions(client):
    _seed_model(client)
    assert client.get("/models/m1/render?view=iso&width=99999").status_code == 400


def test_render_reports_a_worker_failure_as_a_client_error(client):
    _seed_model(client)
    client.services.worker = MeshWorker(ok=False)
    r = client.get("/models/m1/render?view=iso")
    assert r.status_code == 422
    assert "无法网格化" in r.json()["detail"]


def test_render_404s_for_a_model_that_does_not_exist(client):
    """A missing model is a 404, not a 500 — the UI keys its "no model yet"
    message off the status, and at boot the model legitimately does not exist."""
    r = client.get("/models/ghost/render?view=iso")
    assert r.status_code == 404


def test_render_passes_the_configured_style_and_size(client):
    _seed_model(client)
    client.get("/models/m1/render?view=top")
    call = client.services.renderer.calls[-1]
    assert call["views"] == ["top"]
    assert call["style"] == client.services.config.context.render.style
    assert call["width"] == client.services.config.context.render.width


# ══════════════════════════════════════════════════════════════════════════
# conversation history
# ══════════════════════════════════════════════════════════════════════════


def test_threads_start_empty(client):
    assert client.get("/threads").json()["threads"] == []


def test_chat_records_both_sides_of_the_conversation(client):
    _seed_model(client)
    with client.stream(
        "POST", "/chat", json={"model_id": "m1", "text": "做一个 60x40 的底板"}
    ) as r:
        body = "".join(r.iter_text())
    assert "event: start" in body

    threads = client.get("/threads").json()["threads"]
    assert len(threads) == 1
    assert threads[0]["model_id"] == "m1"

    msgs = client.get(f"/threads/{threads[0]['thread_id']}/messages").json()["messages"]
    roles = [m["role"] for m in msgs]
    assert "user" in roles
    assert "assistant" in roles
    assert msgs[0]["content"] == "做一个 60x40 的底板"


def test_messages_come_back_in_arrival_order(client):
    """`created_at` has one-second resolution, so a user message and the reply
    routinely share a timestamp. Order must not depend on it."""
    _seed_model(client)
    with client.stream("POST", "/chat", json={"model_id": "m1", "text": "first"}) as r:
        "".join(r.iter_text())
    tid = client.get("/threads").json()["threads"][0]["thread_id"]
    msgs = client.get(f"/threads/{tid}/messages").json()["messages"]
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "first"


def test_chat_can_continue_an_existing_thread(client):
    _seed_model(client)
    with client.stream(
        "POST", "/chat", json={"model_id": "m1", "text": "one", "thread_id": "th-fixed"}
    ) as r:
        "".join(r.iter_text())
    with client.stream(
        "POST", "/chat", json={"model_id": "m1", "text": "two", "thread_id": "th-fixed"}
    ) as r:
        "".join(r.iter_text())
    threads = client.get("/threads").json()["threads"]
    assert [t["thread_id"] for t in threads] == ["th-fixed"]
    msgs = client.get("/threads/th-fixed/messages").json()["messages"]
    assert [m["content"] for m in msgs if m["role"] == "user"] == ["one", "two"]


def test_unknown_thread_has_no_messages(client):
    body = client.get("/threads/does-not-exist/messages").json()
    assert body["messages"] == []


def test_draft_model_listing_uses_unsaved_endpoint_and_key_without_persisting(client, monkeypatch):
    from tcad.llm.hotswap import ProbeResult
    import tcad.llm.hotswap as module

    before = client.get('/settings/llm').json()
    async def probe(settings, **kwargs):
        assert settings.resolved_base_url() == 'https://draft.example/v1'
        assert settings.resolved_api_key() == 'draft-test-key'
        return ProbeResult(ok=True, models=['draft-model'], method='models.list')
    monkeypatch.setattr(module, 'probe_llm', probe)
    response = client.post('/settings/models', json={
        'provider': 'custom', 'base_url': 'https://draft.example/v1', 'api_key': 'draft-test-key'})
    assert response.status_code == 200
    assert response.json()['models'] == ['draft-model']
    assert client.get('/settings/llm').json() == before
