"""The front end is part of the deliverable, so it gets tests too.

The assertion that matters most is the *offline* one. "No build step, no
dependencies" is easy to claim and easy to break: one `<script src="https://…">`
or an `@import` of a webfont and the page silently stops working on a machine
without internet, which is exactly the environment this harness is meant to run
in. So the test walks the real files and refuses any external reference.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tcad.config.schema import Config
from tcad.server.app import create_app

TestClient = pytest.importorskip("fastapi.testclient").TestClient

UI_DIR = Path(__file__).resolve().parents[2] / "tcad" / "server" / "ui"


@pytest.fixture()
def client(tmp_path):
    cfg = Config()
    cfg.storage.data_dir = str(tmp_path / "data")
    app = create_app(None, config=cfg)
    with TestClient(app) as c:
        yield c


# ══════════════════════════════════════════════════════════════════════════


def test_ui_files_exist():
    for name in ("index.html", "app.js", "styles.css"):
        assert (UI_DIR / name).is_file(), f"missing front-end file: {name}"


def test_ui_is_served(client):
    r = client.get("/ui/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "tcad" in r.text


def test_root_redirects_to_the_ui(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"].endswith("/ui/")


def test_css_and_js_are_served(client):
    assert client.get("/ui/styles.css").status_code == 200
    js = client.get("/ui/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]


def test_favicon_is_served(client):
    """Browsers request /favicon.ico unprompted. Without a declared icon the
    console collects a 404 on every page load, which trains people to ignore
    the console — and then a real 404 looks like background noise."""
    for name in ("favicon.svg", "favicon.png"):
        r = client.get(f"/ui/{name}")
        assert r.status_code == 200, name
        assert r.content, name

    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    assert 'rel="icon"' in html, "index.html 没有声明图标"


def test_the_api_banner_exists_and_starts_hidden(client):
    """The page must be able to explain 'this is not served by tcad' — the
    symptom is otherwise a wall of 404s that says nothing about the cause."""
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="apiBanner"' in html
    assert re.search(r'id="apiBanner"[^>]*hidden', html), "横幅默认应是隐藏的"

    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    assert "openedAsStaticFile" in js
    # and it must actually distinguish the static-preview URL shape
    assert "/static-html/" in js
    assert "file:" in js


def test_settings_failure_is_shown_inside_the_dialog():
    """Closing the dialog on failure reads as 'clicking settings does nothing'."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    assert 'id="settingsError"' in (UI_DIR / "index.html").read_text(encoding="utf-8")

    # bounded by the next top-level function so the assertion is about
    # openSettings itself and not the rest of the file
    body = js.split("async function openSettings", 1)[1].split("function settingsPatch", 1)[0]
    assert "settingsError" in body, "设置面板内没有错误显示区"
    assert "errorBox.hidden = false" in body
    assert "modal.hidden = true" not in body, "失败时不应关闭对话框"


def test_ui_does_not_shadow_the_api(client):
    """The mount is on /ui, so the API must be untouched."""
    assert client.get("/health").status_code == 200
    assert client.get("/settings/providers").status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# offline guarantee
# ══════════════════════════════════════════════════════════════════════════

_EXTERNAL = re.compile(r"""(?:src|href)\s*=\s*["'](https?:)?//""", re.IGNORECASE)


def test_no_external_references_anywhere_in_the_front_end():
    offenders: list[str] = []
    for path in sorted(UI_DIR.iterdir()):
        if path.suffix not in (".html", ".js", ".css"):
            continue
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if _EXTERNAL.search(line):
                offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert not offenders, "外部资源引用会让界面在离线环境下失效：\n" + "\n".join(offenders)


def test_no_css_imports():
    css = (UI_DIR / "styles.css").read_text(encoding="utf-8")
    assert "@import" not in css, "CSS @import 通常是外部字体，请改用系统字体栈"


def test_every_referenced_asset_exists():
    """A typo in a src/href is a 404 the user sees as a broken page."""
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r"""(?:src|href)\s*=\s*["']\./([^"'#?]+)["']""", html)
    assert refs, "index.html 没有引用任何本地资源，可能写错了"
    for ref in refs:
        assert (UI_DIR / ref).is_file(), f"index.html 引用了不存在的文件：{ref}"


def test_front_end_uses_no_third_party_runtime():
    """No CDN framework sneaking in through a bare import."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    for line in js.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") and "from" in stripped:
            target = stripped.split("from", 1)[1].strip().strip(";\"'")
            assert target.startswith(".") or target.startswith("/"), (
                f"app.js 引入了非本地模块：{target}"
            )


# ══════════════════════════════════════════════════════════════════════════
# static consistency between the three files
#
# A `$("someId")` that does not exist returns null and then throws on the next
# property access — at runtime, in the browser, silently from the test suite's
# point of view. These checks catch that class of typo without a browser.
# ══════════════════════════════════════════════════════════════════════════


def test_every_element_id_used_by_app_js_exists_in_the_html():
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    declared = set(re.findall(r'id="([^"]+)"', html))
    used = set(re.findall(r'\$\("([^"]+)"\)', js))
    missing = sorted(used - declared)
    assert not missing, f"app.js 引用了 HTML 中不存在的 id：{missing}"


def test_every_selector_class_used_by_app_js_exists_in_the_html():
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    declared: set[str] = set()
    for attr in re.findall(r'class="([^"]+)"', html):
        declared.update(attr.split())
    used = set(re.findall(r'querySelectorAll?\("\.([A-Za-z0-9_-]+)"\)', js))
    used |= set(re.findall(r'closest\("\.([A-Za-z0-9_-]+)"\)', js))
    missing = sorted(used - declared)
    assert not missing, f"app.js 查询了 HTML 中不存在的 class：{missing}"


def test_html_ids_are_unique():
    """A duplicate id silently binds to the first one, which is the kind of bug
    that only shows up as 'the wrong thing updated'."""
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    ids = re.findall(r'id="([^"]+)"', html)
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, f"重复的 id：{duplicates}"


def test_stylesheet_defines_the_classes_the_ui_actually_uses():
    """Spot-check the load-bearing ones. A missing rule is not fatal, but a
    missing rule for a state class means a state you cannot see."""
    css = (UI_DIR / "styles.css").read_text(encoding="utf-8")
    for name in (".gatecard", ".tool", ".checkrow", ".hookline", ".notice",
                 ".probe-result", ".viewport", ".chain", ".modal"):
        assert name in css, f"styles.css 缺少 {name} 的样式"


# ══════════════════════════════════════════════════════════════════════════
# the front end must not invent its own success criteria
# ══════════════════════════════════════════════════════════════════════════


def test_the_verdict_table_does_not_use_enum_names():
    """The original defect, stated directly: the keys were `SUCCEEDED:` etc.

    It was invisible because the earlier version of this test only asserted the
    *strings* "SUCCEEDED" and "EXHAUSTED" appeared somewhere in the file — which
    they did, as the keys that could never match.
    """
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    body = js.split("const verdicts = {", 1)[1].split("\n  };", 1)[0]
    for name in ("SUCCEEDED:", "EXHAUSTED:", "FAILED:", "ABORTED:", "CONFIRMED:"):
        assert name not in body, (
            f"verdicts 使用了枚举名 {name} —— 线上传的是小写值，永远不会命中"
        )


def test_the_ui_renders_the_engines_verdict_rather_than_deciding():
    """`result.state` and `gate_report` come from the engine; the UI must read
    them, not recompute anything from the stream."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    assert "result.gate_report" in js or "gate_report" in js
    assert "result.state" in js


def test_settings_ui_never_pretends_to_have_the_key():
    """The key is write-only from the client's point of view."""
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="apiKeyInput" type="password"' in html
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    # the masked hint is shown; the plaintext is never assigned into the input
    assert "api_key_masked" in js
    assert 'apiKeyInput").value = ""' in js or "apiKeyInput').value = ''" in js


# ══════════════════════════════════════════════════════════════════════════
# the viewport must follow the turn, not wait for it to end
#
# Reported symptom: "the model is working on the left, but the middle never
# renders, and I can't tell when it has finished." Three separate causes, each
# pinned below.
# ══════════════════════════════════════════════════════════════════════════


def _function_body(js: str, signature: str, next_signature: str) -> str:
    return js.split(signature, 1)[1].split(next_signature, 1)[0]


def test_tool_produced_images_are_applied_to_the_viewport():
    """The engine already ships the URL of every rendered view. Ignoring it is
    what made the middle pane sit still for the whole turn."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    handler = js.split("function handleAgentEvent", 1)[1].split("async function", 1)[0]
    assert "applyToolImages" in handler, "工具产出的图没有进入视图区"

    apply_body = js.split("function applyToolImages", 1)[1].split("\n}", 1)[0]
    assert "displayImage" in apply_body, "有了图却没有把它显示出来"


def test_a_green_commit_refreshes_the_view_even_without_a_geo_view():
    """A model that never calls geo_view would otherwise leave a stale picture
    next to a Gate that says the part is correct."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    handler = js.split("function handleAgentEvent", 1)[1].split("async function", 1)[0]
    assert "ir_commit" in handler
    assert "loadView" in handler


def test_a_running_turn_shows_what_it_is_doing_and_for_how_long():
    """A static screen during a minute-long turn is indistinguishable from a
    hung one — which is how a run finishes without anyone noticing."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    assert "setLive" in js and "clearLive" in js
    assert "live-time" in js, "没有已用时显示"

    # it must be cleared on every exit path, or it lingers after the turn
    send_body = js.split("async function send", 1)[1].split("function handleAgentEvent", 1)[0]
    assert "clearLive()" in send_body.split("finally", 1)[1]


def test_the_outcome_is_stated_louder_than_a_hint():
    """Ending a turn is the most important event in the conversation; it used to
    be reported with the same weight as a note about conversation history."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    assert "pushVerdict" in js
    css = (UI_DIR / "styles.css").read_text(encoding="utf-8")
    assert ".verdict" in css
    for state in ("SUCCEEDED", "EXHAUSTED", "FAILED"):
        assert state in js, state


def test_the_terminal_status_comes_from_the_engine_not_the_stream_ending():
    """`setStatus('ok', …)` at the end of send() would fire even for a failed
    turn — the status must be driven by result.state."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    send_body = js.split("async function send", 1)[1].split("function handleAgentEvent", 1)[0]
    assert 'setStatus("ok"' not in send_body, "回合结束时不应无条件把状态置为成功"

    result_body = js.split("async function handleResult", 1)[1].split("function ", 1)[0]
    assert "setStatus(" in result_body
    assert "succeeded" in result_body


def test_the_verdict_table_is_keyed_by_serialised_state_values():
    """This is the bug behind "it finished and I could not tell".

    The table was keyed by ENUM NAME (`SUCCEEDED`) while the SSE frame carries
    the serialised VALUE (`"succeeded"`). Nothing ever matched, so a finished
    turn produced no conclusion on screen at all — and the only signal was the
    text quietly stopping. The assertion is against `TurnState` itself so the
    two sides cannot drift apart again.
    """
    from tcad.core.types import TurnState

    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    body = js.split("const verdicts = {", 1)[1].split("\n  };", 1)[0]
    keys = set(re.findall(r"^\s{4}(\w+):\s*\[", body, re.MULTILINE))
    assert keys, "没有从 app.js 里解析出任何 verdict 键 —— 这个测试可能已经失效"

    values = {s.value for s in TurnState}
    unknown = sorted(keys - values)
    assert not unknown, (
        f"这些键不是 TurnState 的序列化值：{unknown}。"
        "SSE 里传的是小写值，用枚举名永远匹配不到。"
    )

    for state in (
        TurnState.SUCCEEDED,
        TurnState.EXHAUSTED,
        TurnState.FAILED,
        TurnState.ABORTED,
        TurnState.AWAITING_APPROVAL,
        TurnState.CONFIRMED,
    ):
        assert state.value in keys, f"缺少 {state.value} 的结论文案"


def test_the_verdict_labelling_is_honest_about_exhausted():
    """EXHAUSTED must never read as success."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    body = js.split("const verdicts = {", 1)[1].split("\n  };", 1)[0]
    exhausted = body.split("exhausted:", 1)[1].split("],", 1)[0]
    assert "不是成功" in exhausted, "EXHAUSTED 的文案必须明说它不是成功"
    succeeded = body.split("succeeded:", 1)[1].split("],", 1)[0]
    assert "不是成功" not in succeeded


def test_the_viewport_renders_even_before_the_version_is_known():
    """At boot the model may not exist yet, so `state.version` is null. The old
    loadView early-returned on that and therefore never rendered a part built in
    the same session — it only appeared after a page reload."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    body = js.split("async function loadView", 1)[1].split("async function", 1)[0]
    assert "!state.version" not in body, (
        "loadView 不应依据 boot 时必然为空的 state.version 提前返回 —— "
        "让服务端解析当前版本，用 HTTP 状态判断"
    )
    assert "res.status === 404" in body, "应把「模型尚不存在」与「渲染失败」区分开"


def test_the_live_row_is_kept_at_the_bottom():
    """It says what is happening *now*, so it must not be pushed up the screen
    by the very content it describes."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    body = js.split("function append(node)", 1)[1].split("\n}", 1)[0]
    assert "liveRow" in body, "live 行会被后续内容顶到上面，失去「当前状态」的含义"


# ══════════════════════════════════════════════════════════════════════════
# telling one instance from another
#
# Two servers can run at once with different data directories. Nothing in the
# UI distinguished them, so seeing someone else's model in the header read as
# "my configuration was lost" rather than "this is a different server".
# ══════════════════════════════════════════════════════════════════════════


def test_the_settings_dialog_names_the_instance_and_the_config_file():
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="storageNote"' in html

    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    body = js.split("async function openSettings", 1)[1].split("function settingsPatch", 1)[0]
    assert "settings_file" in body, "没有显示配置文件路径"
    assert "storageNote" in body
    assert "persisted" in body, "没有说明配置是否真的写到了磁盘"


def test_the_model_chip_can_identify_the_instance():
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    body = js.split("function updateModelChip", 1)[1].split("\n}", 1)[0]
    assert "dataDir" in body, "顶栏无法分辨实例"
    assert "data_dir" in js, "没有从 /health 取数据目录"


def test_the_key_box_never_reads_as_empty():
    """The stored secret is deliberately never written into the input, so an
    empty box is the normal state — the placeholder has to say that, or it reads
    as "my key was not saved"."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    body = js.split("async function openSettings", 1)[1].split("function settingsPatch", 1)[0]
    assert "apiKeyInput" in body
    assert "placeholder" in body, "空输入框需要 placeholder 说明「已保存，留空不改」"


# ══════════════════════════════════════════════════════════════════════════
# the session list
#
# The failure mode worth guarding is not "the list is missing" — that is visible
# immediately. It is switching the *conversation* while leaving the viewport,
# artefacts and inspector describing the part you navigated away from. Those
# panes look authoritative either way, so a stale one is worse than an empty one.
# ══════════════════════════════════════════════════════════════════════════


def test_the_session_sidebar_exists_and_can_be_toggled():
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    css = (UI_DIR / "styles.css").read_text(encoding="utf-8")

    assert 'id="sessionPane"' in html
    assert 'id="sessionList"' in html
    assert 'id="newSessionBtn"' in html
    assert 'id="sidebarToggle"' in html
    # Collapsing must remove the grid track, not just hide the contents —
    # otherwise the pane's width stays reserved and nothing is gained.
    assert ".layout.no-sidebar .col-sessions" in css
    assert "grid-template-columns" in css
    assert "no-sidebar" in js


def test_switching_a_session_updates_the_model_and_every_derived_pane():
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    body = js.split("async function switchSession", 1)[1].split("async function loadMessages", 1)[0]

    assert "state.modelId" in body, "切换会话没有改变当前模型"
    for call in ("refreshInspector()", "loadArtifacts()", "loadView("):
        assert call in body, f"切换会话后没有刷新 {call}"


def test_switching_sessions_clears_the_previous_verdict():
    """The Gate report belongs to the session it was produced for. Carrying it
    across would put another part's verdict next to this one's geometry."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    body = js.split("async function switchSession", 1)[1].split("async function loadMessages", 1)[0]
    assert "state.lastGate = null" in body


def test_switching_aborts_an_in_flight_turn():
    """Otherwise the old session's frames keep arriving and render into the new
    one. Aborting also tells the server to cancel the turn, so the work stops
    rather than being orphaned."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    body = js.split("async function switchSession", 1)[1].split("async function loadMessages", 1)[0]
    assert "state.abort" in body and ".abort()" in body
    # ...and the resulting AbortError is not reported as a failure.
    send_body = js.split("async function send", 1)[1].split("function handleAgentEvent", 1)[0]
    assert "AbortError" in send_body, "切换导致的中断会被当成请求失败报出来"


def test_a_first_message_creates_the_session_before_rendering_the_message():
    """Switching sessions clears the stream, so creating it afterwards would
    erase the bubble that was just appended."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    body = js.split("async function send", 1)[1].split("function handleAgentEvent", 1)[0]
    assert "newSession" in body, "没有会话时直接发送会写到「没有模型的」虚空里"
    assert body.index("newSession") < body.index("pushUser"), (
        "必须先建会话再渲染消息，否则切换会把刚加的消息清掉"
    )


def test_the_session_list_is_rendered_from_data_not_hard_coded():
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    body = js.split("function renderSessions", 1)[1].split("async function loadSessions", 1)[0]
    assert "state.sessions" in body
    assert "s.thread_id" in body
    # A session with no geometry has to be distinguishable from one at v0.
    assert "ir_version" in body
    assert "无几何" in body


def test_the_url_carries_the_session_so_a_reload_returns_to_it():
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    assert 'params.set("thread"' in js
    assert 'get("thread")' in js, "boot 没有读回 URL 里的会话"


def test_the_turn_ending_refreshes_the_session_list():
    """A session that just built a part must stop reading "新会话"."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    body = js.split("async function handleResult", 1)[1].split("async function ensureModel", 1)[0]
    assert "loadSessions()" in body


def test_clicking_clear_keeps_the_session():
    """「清空视图」clears the display, not the conversation on disk."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    body = js.split('$("clearBtn").addEventListener', 1)[1].split("});", 1)[0]
    assert "resetStream" in body
    assert "GET /threads" not in body


# ══════════════════════════════════════════════════════════════════════════
# switching sessions must not let the session you left keep writing
#
# `abort()` stops the *network*, not the JavaScript: the abandoned request's
# frame handlers, its `catch` and its `finally` all still run. Un-guarded, they
# render the old session's frames into the new one, report "已离开该会话" inside a
# conversation you never left, delete the live row of the turn that is actually
# running, and null the abort controller the next turn is holding.
#
# These are structural assertions over the source, which is what this suite can
# do without a browser. The *behaviour* is verified in a real Chromium (see the
# round's notes); this test pins the mechanism so it cannot be deleted by
# accident.
# ══════════════════════════════════════════════════════════════════════════


def test_a_switched_away_turn_cannot_write_into_the_new_session():
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    send_body = js.split("async function send", 1)[1].split("function handleAgentEvent", 1)[0]
    switch_body = js.split("async function switchSession", 1)[1].split("async function loadMessages", 1)[0]

    # The mechanism: the turn has an identity, and switching replaces it.
    assert "state.turn = turn" in send_body
    assert "const current = () => state.turn === turn" in send_body
    assert "state.turn = null" in switch_body, "切走没有让旧回合失效"

    # Every frame handler asks "am I still current?" before touching the screen.
    handler_block = send_body.split("await streamChat(", 1)[1].split("turn.controller.signal", 1)[0]
    entries = re.split(r"\n\s+(?=(?:start|agent|progress|error|result):)", handler_block)[1:]
    assert len(entries) == 5, f"期望 5 个帧处理器，实际 {len(entries)}"
    for entry in entries:
        name = entry.strip().split(":", 1)[0]
        assert "current()" in entry, f"{name} 处理器没有以回合身份为前置条件"

    # The abort path writes text; the cleanup writes global state. Both are the
    # places the old turn reached into the new session.
    catch_body = send_body.split("} catch (err) {", 1)[1].split("} finally {", 1)[0]
    assert "if (!current()) return;" in catch_body, "被切走的回合仍会往新会话写提示"
    assert "已离开该会话" in catch_body, "提示不该消失，只是不该写进别的会话"

    finally_body = send_body.split("} finally {", 1)[1]
    assert "if (current())" in finally_body, "被切走的回合仍会清理全局状态"
    for mutation in ('state.busy = false', 'state.abort = null', 'clearLive()',
                     '$("sendBtn").disabled = false'):
        assert mutation in finally_body


def test_the_header_title_follows_the_session_after_a_turn():
    """The sidebar and the header read the same field. Refreshing only the list
    left the first turn of every session titled 新会话 in the header until you
    navigated away and back."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    body = js.split("async function handleResult", 1)[1].split("async function ensureModel", 1)[0]
    assert "await loadSessions()" in body
    assert "setSessionHeader(" in body, "回合结束后标题没有跟着会话走"


def test_a_failed_session_list_read_is_reported_not_shown_as_empty():
    """「读不到会话」and「还没有会话」are opposite claims about whether your work
    still exists. Rendering the first as the second is a lie you cannot see
    through — the same class of defect as a barrier that reports PASS without
    checking anything."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    body = js.split("function renderSessions", 1)[1].split("async function loadSessions", 1)[0]
    assert "state.sessionsError" in body, "列表读取失败被当成空列表"
    # The empty-state text is only reachable once the error branch has returned.
    error_branch = body.split("state.sessionsError", 1)[1]
    assert "return;" in error_branch.split("if (!state.sessions.length)", 1)[0]

    load_body = js.split("async function loadSessions", 1)[1].split("function resetStream", 1)[0]
    assert "state.sessionsError = null" in load_body, "一次成功后错误状态没有清掉"


def test_switching_does_not_force_a_rerender():
    """`/render` caches on disk keyed by (version, view, style, size), and the
    version is this model's own IR version — so a cached image can never belong
    to another session. Forcing on every switch re-tessellated the part for
    nothing."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    body = js.split("async function switchSession", 1)[1].split("async function loadMessages", 1)[0]
    assert "loadView(false)" in body
    assert "loadView(true)" not in body, "切换会话仍在强制重渲染"


def test_an_empty_session_reads_as_empty_not_as_a_render_failure():
    """A model exists from the moment its session is created; it has no geometry
    until the first turn. `/render` answers 422 `no solid to tessellate` for it,
    and repeating that verbatim made a brand-new session look broken."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    body = js.split("async function loadView", 1)[1].split("async function loadArtifacts", 1)[0]
    assert "no solid" in body
    assert "还没有几何" in body, "空模型被渲染成一次渲染失败"


def test_the_active_session_is_marked_while_a_turn_runs():
    """`styles.css` had a `.s-running` rule nothing ever applied — a state you
    cannot see. The sidebar is where you look to find a running turn."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    css = (UI_DIR / "styles.css").read_text(encoding="utf-8")
    body = js.split("function renderSessions", 1)[1].split("async function loadSessions", 1)[0]
    assert "s-running" in body, "styles.css 里的运行中样式没有任何代码会用"
    assert "state.busy" in body
    assert ".s-running" in css

    send_body = js.split("async function send", 1)[1].split("function handleAgentEvent", 1)[0]
    assert send_body.count("renderSessions()") >= 2, "开始与结束时都要刷新，否则标记不会消失"

