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
