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


def test_the_ui_knows_that_exhausted_is_not_success():
    """The harness's central invariant. If the UI blurred these two the product
    would be lying about its own results, so the distinction is pinned here."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    assert "SUCCEEDED" in js and "EXHAUSTED" in js
    exhausted = js.split("EXHAUSTED:", 1)[1].split("],", 1)[0]
    assert "不是" in exhausted, "EXHAUSTED 的提示必须明确说明它不是成功"


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
