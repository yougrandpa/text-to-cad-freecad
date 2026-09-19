/* tcad front end.
 *
 * No framework, no bundler: native ES modules, so the same files the server
 * serves are the files you edit. Three ideas organise everything below:
 *
 *  1. The **server decides**, the UI only displays. In particular the turn's
 *     outcome comes from the engine's `TurnResult`, never from anything the
 *     browser infers — `EXHAUSTED` must never be rendered as success.
 *  2. Text that came from a model or a file goes in as `textContent`, never
 *     `innerHTML`. Everything here is built with DOM calls for that reason.
 *  3. Progress is a *stream of real events*, not a spinner: `agent` frames are
 *     what the model actually did, `progress` frames are the hook dispatcher's
 *     real decisions.
 */

const $ = (id) => document.getElementById(id);

const state = {
  modelId: new URLSearchParams(location.search).get("model") || "part",
  threadId: null,
  version: null,
  view: "iso",
  busy: false,
  apiOk: null,
  dataDir: null,
  lastGate: null,
  settings: null,
  providers: [],
};

// ══════════════════════════════════════════════════════════════════════════
// tiny DOM helper — safe by default
// ══════════════════════════════════════════════════════════════════════════

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;   // never innerHTML
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child != null) node.append(child);
  }
  return node;
}

/** Is this page being served by the tcad app, or just handed over as a file?
 *
 * The distinction matters because the HTML is a plain file that any static
 * server (or `file://`) can deliver — in which case the page *looks* fine and
 * every API call 404s. The browser console then shows a wall of 404s that say
 * nothing about the cause, which is a genuinely bad place to leave someone.
 *
 * A real tcad URL is `/ui/`; a preview URL is `/static-html/<hash>/index.html`.
 */
function openedAsStaticFile() {
  return (
    location.protocol === "file:" ||
    location.pathname.includes("/static-html/") ||
    location.pathname.endsWith(".html")
  );
}

function showApiUnavailable(detail) {
  state.apiOk = false;
  const banner = $("apiBanner");
  const asFile = openedAsStaticFile();

  banner.replaceChildren(
    el("strong", {
      text: asFile
        ? "这个页面不是由 tcad 服务打开的"
        : "无法连接 tcad 服务",
    }),
    el("div", {
      text: asFile
        ? "界面本身只是静态文件 —— 它显示的每一个数字都来自后端 API，而这里没有后端在应答。" +
          "控制台里的那些 404 就是这么来的。"
        : `服务没有响应：${detail}`,
    }),
    el("div", { class: "apibanner-fix" }, [
      el("span", { text: "启动服务：" }),
      el("code", { text: "cd 项目根目录 && .venv/bin/python tools/serve.py --data-dir data --port 8765" }),
      el("span", { text: "然后访问" }),
      el("code", { text: "http://127.0.0.1:8765/ui/" }),
      el("button", {
        class: "mini",
        text: "我已启动，重试",
        onclick: () => location.reload(),
      }),
    ]),
  );
  banner.hidden = false;

  // Stop offering actions that cannot work. A button that produces another 404
  // is worse than a disabled one.
  $("sendBtn").disabled = true;
  $("settingsBtn").disabled = true;
  $("modelChip").disabled = true;
  $("input").disabled = true;
  $("refreshInspect").disabled = true;
  $("refreshView").disabled = true;
}

function setStatus(kind, text) {
  $("statusDot").className = "dot " + (kind || "");
  $("statusText").textContent = text;
}

// Declared before `append` so that helper can keep the live row pinned to the
// bottom. It describes what is happening *now*, so it must never be pushed up
// the screen by the content it is describing.
let liveRow = null;
let liveTimer = null;
let liveStartedAt = 0;

const stream = () => $("stream");
function append(node) {
  const welcome = $("welcome");
  if (welcome) welcome.remove();
  stream().append(node);
  // Re-appending an existing node moves it, which is how the live row stays last.
  if (liveRow && node !== liveRow) stream().append(liveRow);
  stream().scrollTop = stream().scrollHeight;
  return node;
}

// ── the live row ─────────────────────────────────────────────────────────
// A turn can run for a minute or more (a reasoning model, several compiles).
// Without a moving indicator the page looks frozen, and the user cannot tell
// "still working" from "died" — which is exactly how a run finishes without
// anyone noticing. The elapsed counter exists so the difference is visible
// even when the model is silent between tool calls.

function setLive(text) {
  if (!liveRow) {
    liveRow = el("div", { class: "live" }, [
      el("span", { class: "spinner" }),
      el("span", { class: "live-text", text }),
      el("span", { class: "live-time", text: "0s" }),
    ]);
    append(liveRow);
    liveStartedAt = Date.now();
    liveTimer = setInterval(() => {
      const clock = liveRow && liveRow.querySelector(".live-time");
      if (clock) clock.textContent = `${Math.round((Date.now() - liveStartedAt) / 1000)}s`;
    }, 1000);
  } else {
    const label = liveRow.querySelector(".live-text");
    if (label) label.textContent = text;
  }
}

function clearLive() {
  if (liveTimer) {
    clearInterval(liveTimer);
    liveTimer = null;
  }
  if (liveRow) {
    liveRow.remove();
    liveRow = null;
  }
}

// ══════════════════════════════════════════════════════════════════════════
// http
// ══════════════════════════════════════════════════════════════════════════

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: options.body ? { "Content-Type": "application/json" } : {},
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || JSON.stringify(body);
    } catch { /* not json */ }
    throw new Error(`${res.status} ${detail}`);
  }
  const type = res.headers.get("content-type") || "";
  return type.includes("application/json") ? res.json() : res.text();
}

/** POST /chat and dispatch SSE frames as they arrive.
 *
 * `EventSource` cannot POST, and the turn needs a request body, so the stream
 * is parsed by hand. Frames are `event: <name>\ndata: <json>` separated by a
 * blank line — the same framing `_sse()` writes on the server.
 */
async function streamChat(body, handlers) {
  const res = await fetch("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) {
    throw new Error(`${res.status} ${await res.text()}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let event = "message";
      let data = "{}";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7).trim();
        else if (line.startsWith("data: ")) data = line.slice(6);
      }
      let parsed;
      try { parsed = JSON.parse(data); } catch { parsed = { raw: data }; }
      const handler = handlers[event];
      if (handler) handler(parsed);
    }
  }
}

// ══════════════════════════════════════════════════════════════════════════
// chat rendering
// ══════════════════════════════════════════════════════════════════════════

function pushUser(text) {
  append(el("div", { class: "msg user" }, el("div", { class: "bubble", text })));
}

let assistantBody = null;

function pushAssistantText(text) {
  if (!assistantBody) {
    assistantBody = el("div", { class: "text" });
    append(el("div", { class: "msg assistant" }, [
      el("div", { class: "who", text: "模型" }),
      assistantBody,
    ]));
  }
  assistantBody.textContent += (assistantBody.textContent ? "\n" : "") + text;
}

function pushToolCard(data) {
  assistantBody = null;   // the next model text starts a new bubble

  const gateNote = data.gate && data.gate.passed
    ? el("span", { class: "badge", text: "GATE PASS" })
    : null;

  const body = [];
  if (data.error) {
    body.push(el("pre", { text: `[${data.error.kind}] ${data.error.message}` +
      (data.error.feature_id ? `\n(feature_id=${data.error.feature_id})` : "") }));
  } else if (data.content) {
    body.push(el("pre", { text: data.content }));
  }

  const card = el("details", {
    class: "tool " + (data.ok ? "ok" : "bad"),
    open: Boolean(data.error),
  }, [
    el("summary", {}, [
      el("span", { class: "name", text: `${data.name}` }),
      data.error
        ? el("span", { class: "badge", text: `失败 · ${data.error.kind}` })
        : el("span", { class: "badge", text: "成功" }),
      gateNote,
      el("span", { class: "muted small", text: `第 ${data.step} 步` }),
    ]),
    ...body,
  ]);
  append(card);
}

/** Show an image the turn already produced, without re-rendering.
 *
 * The engine hands us the URL of every view a tool generated, so the viewport
 * can follow along live. Re-rendering through `/render` would also work, but it
 * would recompute what the worker has already computed.
 */
function applyToolImages(data) {
  const images = (data.images || []).filter((i) => i && i.url);
  if (!images.length) return null;
  const latest = images[images.length - 1];
  displayImage(latest.url, latest.view);
  return latest;
}

function pushHookLine(ev) {
  append(el("div", { class: "hookline" }, [
    el("span", { text: ev.event }),
    el("span", { class: `dec-${ev.decision}`, text: ev.decision }),
    el("span", { text: ev.hook || "" }),
    ev.reason ? el("span", { class: "muted", text: ev.reason.slice(0, 90) }) : null,
  ]));
}

function pushNotice(kind, text) {
  append(el("div", { class: `notice ${kind}`, text }));
}

/** The turn's outcome, stated plainly.
 *
 * A turn ending is the single most important event in the conversation, and it
 * used to be reported with the same weight as a hint about history. This is
 * deliberately louder than `pushNotice`.
 */
function pushVerdict(kind, title, detail) {
  append(el("div", { class: `verdict ${kind}` }, [
    el("div", { class: "verdict-title", text: title }),
    detail ? el("div", { class: "verdict-detail", text: detail }) : null,
  ]));
}

function checkRow(row) {
  const cls = row.status === "pass" ? "pass"
    : row.status === "skip" ? "skip" : "fail";
  const mark = row.status === "pass" ? "✓"
    : row.status === "skip" ? "–"
    : row.status === "error" ? "!" : "✗";
  return el("div", { class: `checkrow ${cls}` }, [
    el("span", { class: "st", text: mark }),
    el("span", { class: "cid", text: row.check_id }),
    el("span", { class: "cmsg", text: row.message || "" }),
    row.feature_id ? el("span", { class: "fid", text: `(${row.feature_id})` }) : null,
  ]);
}

function pushGateCard(report, turnState) {
  const passed = report.passed;
  const checks = (report.results || []).filter(
    (r) => r.severity === "blocking" || r.status !== "pass"
  );

  const card = el("div", { class: `gatecard ${passed ? "pass" : "fail"}` }, [
    el("div", { class: "head" }, [
      el("span", { text: passed ? "✓ Gate 通过" : "✗ Gate 未通过" }),
      el("span", { class: "muted small", text: `IR v${report.ir_version}` }),
      el("span", { class: "grow" }),
      turnState ? el("span", { class: "muted small", text: turnState }) : null,
    ]),
  ]);
  if (checks.length) {
    card.append(el("div", { class: "checks" }, checks.map(checkRow)));
  }
  if (report.skipped_checks && report.skipped_checks.length) {
    card.append(el("div", { class: "checks" },
      el("div", { class: "checkrow skip" }, el("span", {
        class: "cmsg",
        text: `跳过的检查：${report.skipped_checks.join(", ")}`,
      }))));
  }
  append(card);
}

async function handleResult(result) {
  state.threadId = result.thread_id;
  state.lastGate = result.gate_report || null;
  $("threadLabel").textContent = result.thread_id;

  if (result.gate_report) pushGateCard(result.gate_report, result.state);

  // Keyed by TurnState's *serialised value*, not its enum name.
  //
  // This table was originally keyed SUCCEEDED / EXHAUSTED / …, which never
  // matched anything: the SSE frame carries what pydantic emits for a
  // `(str, Enum)`, i.e. the lowercase value. Every turn therefore ended with no
  // conclusion on screen at all — which is precisely "it finished and I could
  // not tell". The test now checks these keys against `TurnState` itself.
  const verdicts = {
    succeeded: [
      "ok",
      "✓ 完成 —— Gate 全绿",
      "这是「完成」的唯一含义。产物已导出，可在下方下载。",
    ],
    exhausted: [
      "warn",
      "⚠ 预算耗尽，回合结束",
      "这不是成功。模型可能自称完成了 —— 以 Gate 报告为准。",
    ],
    failed: ["bad", "✗ 回合失败", result.error || "原因未知"],
    aborted: ["warn", "回合被中止", ""],
    awaiting_approval: [
      "warn",
      "⏸ 等待审批",
      "有工具调用需要人工批准，请在右侧「待批」中处理。",
    ],
    confirmed: ["ok", "回合已确认", ""],
  };
  const verdict = verdicts[result.state] || [
    "warn",
    `回合结束（${result.state}）`,
    result.error || "",
  ];
  pushVerdict(verdict[0], verdict[1], verdict[2]);

  if (result.error && result.state !== "FAILED") pushNotice("bad", result.error);

  append(el("div", {
    class: "muted small",
    style: "margin-bottom:16px",
    text: `步数 ${result.steps} · 输入 ${result.tokens_in} / 输出 ${result.tokens_out} tokens`,
  }));

  const ok = result.state === "succeeded";
  setStatus(ok ? "ok" : "bad", ok ? "完成" : "未完成");

  // Await the inspector so `state.version` is current before the artefacts and
  // the viewport are read — otherwise the UI can describe a version it has not
  // caught up with yet.
  await refreshInspector();
  loadArtifacts();
  loadView(false);
}

// ══════════════════════════════════════════════════════════════════════════
// sending
// ══════════════════════════════════════════════════════════════════════════

async function ensureModel() {
  try {
    await api("/models", {
      method: "POST",
      body: JSON.stringify({ model_id: state.modelId }),
    });
  } catch (err) {
    if (!String(err.message).startsWith("409")) throw err;   // 409 = already there
  }
}

async function send(text) {
  const trimmed = text.trim();
  if (!trimmed || state.busy) return;

  state.busy = true;
  assistantBody = null;
  $("sendBtn").disabled = true;
  setStatus("busy", "生成中…");
  setLive("正在请求模型…");
  pushUser(trimmed);

  try {
    await ensureModel();
    await streamChat(
      { model_id: state.modelId, text: trimmed, thread_id: state.threadId },
      {
        start: (d) => {
          state.threadId = d.thread_id;
          $("threadLabel").textContent = d.thread_id;
        },
        agent: handleAgentEvent,
        progress: pushHookLine,
        error: (d) => pushNotice("bad", `${d.type}: ${d.message}`),
        result: handleResult,
      },
    );
    // The terminal status is set by handleResult, from the engine's verdict —
    // not here, where we would only know that the stream ended.
  } catch (err) {
    pushNotice("bad", `请求失败：${err.message}`);
    setStatus("bad", "出错");
  } finally {
    state.busy = false;
    clearLive();
    $("sendBtn").disabled = false;
  }
}

/** What a progress frame means for the UI, in one place.
 *
 * The viewport follows the turn: a tool that produced images shows them
 * immediately, and a commit the Gate accepted refreshes the view even if the
 * model never asked to see it. Previously the viewport only updated after the
 * turn ended, so a long run looked like nothing was happening.
 */
function handleAgentEvent(d) {
  if (d.kind === "model") {
    if (d.text && d.text.trim()) pushAssistantText(d.text.trim());
    if (d.tool_calls && d.tool_calls.length) {
      setLive(`第 ${d.step} 步 · ${d.tool_calls.map((c) => c.name).join(" → ")}`);
    }
    return;
  }
  if (d.kind !== "tool") return;

  pushToolCard(d);
  setLive(`第 ${d.step} 步 · ${d.name} ${d.ok ? "完成" : "失败"}`);

  if (applyToolImages(d)) return;

  if (d.name === "ir_commit" && d.ok && d.gate && d.gate.passed) {
    loadView(false);
  }
}

// ══════════════════════════════════════════════════════════════════════════
// viewport / files / inspector
// ══════════════════════════════════════════════════════════════════════════

/** Put an image in the viewport, optionally switching the active tab. */
function displayImage(url, view) {
  const img = $("viewImage");
  const placeholder = $("viewPlaceholder");
  if (view) {
    state.view = view;
    for (const tab of document.querySelectorAll(".tab")) {
      tab.classList.toggle("active", tab.dataset.view === view);
    }
  }
  img.src = url;
  img.hidden = false;
  placeholder.hidden = true;
}

async function loadView(force) {
  const img = $("viewImage");
  const placeholder = $("viewPlaceholder");

  // Deliberately does NOT consult `state.version`. At boot the model may not
  // exist yet, so the cached version is null — and the old early-return on that
  // meant a freshly built part never got rendered until a page reload. The
  // server resolves "current version" itself; the HTTP status is the truth.
  placeholder.textContent = state.busy ? "生成中…" : "渲染中…";
  placeholder.hidden = false;
  img.hidden = true;

  const url = `/models/${encodeURIComponent(state.modelId)}/render` +
    `?view=${state.view}&force=${force ? "true" : "false"}&t=${Date.now()}`;
  try {
    const res = await fetch(url);
    if (res.status === 404) {
      placeholder.textContent = "还没有模型 —— 发送第一条消息后会自动创建";
      return;
    }
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch { /* ignore */ }
      placeholder.textContent = `无法渲染：${detail}`;
      return;
    }
    const blob = await res.blob();
    displayImage(URL.createObjectURL(blob));
  } catch (err) {
    placeholder.textContent = `无法渲染：${err.message}`;
  }
}

async function loadArtifacts() {
  const bar = $("fileBar");
  bar.replaceChildren();
  try {
    const body = await api(
      `/models/${encodeURIComponent(state.modelId)}/artifacts` +
      (state.version != null ? `?version=${state.version}` : "")
    );
    const files = (body.files || []).filter((f) => !f.endsWith(".png") && !f.endsWith(".json"));
    if (!files.length) {
      bar.append(el("span", { class: "muted small", text: "暂无导出产物（STEP / STL 在 Gate 通过后生成）" }));
      return;
    }
    for (const name of files) {
      bar.append(el("a", {
        href: `/models/${encodeURIComponent(state.modelId)}/artifacts/${name}` +
              `?version=${body.version}`,
        text: name,
        download: "",
      }));
    }
  } catch (err) {
    bar.append(el("span", { class: "muted small", text: `产物读取失败：${err.message}` }));
  }
}

function kv(key, value) {
  return el("div", { class: "kv" }, [
    el("span", { class: "k", text: key }),
    el("span", { class: "v", text: String(value) }),
  ]);
}

async function refreshInspector() {
  const box = $("inspector");
  box.replaceChildren();

  const model = el("div", { class: "sec" }, [
    el("h4", { text: "模型" }),
    kv("model_id", state.modelId),
    kv("IR 版本", state.version ?? "—"),
    state.threadId ? kv("thread", state.threadId) : null,
  ]);
  box.append(model);

  // ── the last Gate verdict, verbatim ────────────────────────────────────
  if (state.lastGate) {
    const g = state.lastGate;
    const sec = el("div", { class: "sec" }, [
      el("h4", { text: "最近一次 Gate" }),
      el("div", { class: "kv" }, [
        el("span", { class: "k", text: "结果" }),
        el("span", {
          class: "v",
          style: `color:${g.passed ? "var(--ok)" : "var(--bad)"}`,
          text: g.passed ? "通过" : "未通过",
        }),
      ]),
      kv("IR 版本", g.ir_version),
    ]);
    for (const row of g.results || []) sec.append(checkRow(row));
    if (g.skipped_checks?.length) {
      sec.append(el("div", { class: "muted small", text: `跳过：${g.skipped_checks.join(", ")}` }));
    }
    box.append(sec);
  }

  // ── the IR feature chain ───────────────────────────────────────────────
  try {
    const ir = await api(`/models/${encodeURIComponent(state.modelId)}/ir`);
    state.version = ir.version;

    const chain = el("div", { class: "sec" }, el("h4", { text: "特征链（构建顺序）" }));
    const list = el("ul", { class: "chain" });
    let count = 0;
    for (const body of ir.bodies || []) {
      for (const sk of body.sketches || []) {
        list.append(el("li", {}, [
          el("span", { class: "op", text: `sketch · ${sk.geometry?.length || 0} 段` }),
          el("span", { class: "nm", text: `  ${sk.name || sk.id}` }),
          el("span", { class: "pr", text: `${sk.constraints?.length || 0} 个约束` }),
        ]));
        count++;
      }
      for (const feat of body.features || []) {
        const params = Object.entries(feat.params || {})
          .map(([k, v]) => `${k}=${v}`).join(" ");
        list.append(el("li", {}, [
          el("span", { class: "op", text: feat.op }),
          el("span", { class: "nm", text: `  ${feat.name || feat.id}` }),
          el("span", { class: "pr", text: params || "—" }),
        ]));
        count++;
      }
    }
    if (!count) list.append(el("li", { class: "muted", text: "（空模型）" }));
    chain.append(list);
    box.append(chain);

    if (ir.requirements?.raw_text) {
      box.append(el("div", { class: "sec" }, [
        el("h4", { text: "需求原文" }),
        el("div", { class: "muted small", text: ir.requirements.raw_text }),
      ]));
    }
  } catch (err) {
    // "Model does not exist yet" is the normal state before the first message,
    // not a failure — reporting it as one trains people to ignore errors.
    const missing = String(err.message).startsWith("404");
    box.append(el("div", {
      class: "muted small",
      text: missing
        ? "模型尚未创建 —— 发送第一条消息时会自动创建。"
        : `IR 读取失败：${err.message}`,
    }));
  }

  // ── pending approvals ───────────────────────────────────────────────────
  try {
    const { pending } = await api("/approvals");
    if (pending.length) {
      const sec = el("div", { class: "sec" }, el("h4", { text: `待批 (${pending.length})` }));
      for (const p of pending) {
        sec.append(el("div", { class: "notice warn" }, [
          el("div", { text: `${p.tool_name}` }),
          el("div", { class: "small", text: p.approval_id }),
          el("div", { class: "row", style: "margin-top:8px;display:flex;gap:8px" }, [
            el("button", {
              class: "mini",
              text: "批准",
              onclick: async () => {
                await api(`/approvals/${p.approval_id}`, {
                  method: "POST", body: JSON.stringify({ granted: true }),
                });
                refreshInspector();
              },
            }),
            el("button", {
              class: "mini",
              text: "拒绝",
              onclick: async () => {
                await api(`/approvals/${p.approval_id}`, {
                  method: "POST", body: JSON.stringify({ granted: false }),
                });
                refreshInspector();
              },
            }),
          ]),
        ]));
      }
      box.append(sec);
    }
  } catch { /* approvals are optional */ }
}

// ══════════════════════════════════════════════════════════════════════════
// settings
// ══════════════════════════════════════════════════════════════════════════

function fillProviderSelect(providers, selected) {
  const select = $("providerSelect");
  select.replaceChildren();
  for (const p of providers) {
    select.append(el("option", { value: p.id, text: p.label, selected: p.id === selected }));
  }
}

function applyProviderDefaults() {
  const id = $("providerSelect").value;
  const preset = state.providers.find((p) => p.id === id);
  if (!preset) return;
  $("baseUrlInput").value = preset.base_url || "";
  $("modelInput").value = preset.default_model || "";
  fillModelOptions(preset.models || [], "预设候选（点「列模型」获取实时列表）");
}

function fillModelOptions(models, hint) {
  const list = $("modelOptions");
  list.replaceChildren();
  for (const m of models) list.append(el("option", { value: m }));
  if (hint !== undefined) $("modelsHint").textContent = hint;
}

function showProbe(result) {
  const box = $("probeResult");
  box.hidden = false;
  box.className = "probe-result " + (result.ok ? "ok" : "bad");
  if (result.ok) {
    const models = result.models?.length ? `\n可用模型 ${result.models.length} 个：${result.models.slice(0, 12).join(", ")}` : "";
    box.textContent =
      `✓ 连通 (${result.method}, ${result.latency_ms ?? "?"} ms)` +
      `\n${result.base_url}${models}`;
  } else {
    box.textContent = `✗ ${result.error || "无法连接"}`;
  }
}

async function openSettings() {
  const modal = $("settingsModal");
  const errorBox = $("settingsError");
  errorBox.hidden = true;
  modal.hidden = false;
  try {
    const [{ providers }, current] = await Promise.all([
      api("/settings/providers"),
      api("/settings/llm"),
    ]);
    state.providers = providers;
    state.settings = current.settings;
    fillProviderSelect(providers, current.settings.provider);
    $("modelInput").value = current.settings.model || "";
    $("baseUrlInput").value = current.settings.base_url || "";
    $("apiKeyInput").value = "";
    $("tempInput").value = current.settings.temperature ?? 0.2;
    $("tempOut").textContent = Number(current.settings.temperature ?? 0.2).toFixed(2);
    $("proxyInput").checked = Boolean(current.settings.use_env_proxy);

    const s = current.settings;
    $("keyHint").textContent = s.api_key_set
      ? `已设置（${s.api_key_source}）：${s.api_key_masked}`
      : (s.needs_key ? "未设置 —— 该供应商需要 Key" : "该供应商无需 Key");

    // The input is deliberately never filled with the stored secret, so an
    // empty box is the normal state — say so in the box itself, or it reads as
    // "my key was not saved".
    $("apiKeyInput").placeholder = s.api_key_set
      ? `已保存 ${s.api_key_masked} —— 留空表示不修改`
      : (s.needs_key ? "粘贴你的 API Key" : "该供应商无需 Key");

    $("probeResult").hidden = true;

    // Where this configuration lives, and whether it is on disk at all.
    // Without it, a second server on a different data directory is
    // indistinguishable from "my settings disappeared".
    const file = current.settings_file || "（未知）";
    const dir = file.replace(/[/\\]settings\.json$/, "");
    $("storageNote").replaceChildren(
      el("div", { text: `数据目录  ${dir}` }),
      el("div", {}, [
        el("span", { text: `配置文件  ${file}` }),
        el("span", {
          class: current.persisted ? "ok" : "warn",
          text: current.persisted
            ? "   ✓ 已持久化，重启后仍生效"
            : "   ⚠ 尚未写入磁盘（当前生效的是 YAML 默认）",
        }),
      ]),
    );
    $("storageNote").hidden = false;

    fillModelOptions(
      state.providers.find((p) => p.id === s.provider)?.models || [],
      "模型名会随代际变化，点「列模型」获取实时列表。"
    );
  } catch (err) {
    // Deliberately do NOT close the dialog. Closing on failure leaves an
    // unchanged page and a console message, which reads as "clicking settings
    // does nothing" — the user has no way to tell a missing endpoint from a
    // dead server from a typo.
    errorBox.hidden = false;
    errorBox.textContent =
      `无法读取设置：${err.message}\n` +
      (openedAsStaticFile()
        ? "这个页面是从静态文件打开的，不是由 tcad 服务托管 —— 请改用 http://127.0.0.1:<端口>/ui/ 访问。"
        : "服务可能已停止，或这个接口不存在（旧进程？）。");
  }
}

function settingsPatch() {
  const patch = {
    provider: $("providerSelect").value,
    model: $("modelInput").value.trim(),
    base_url: $("baseUrlInput").value.trim(),
    temperature: Number($("tempInput").value),
    use_env_proxy: $("proxyInput").checked,
  };
  // Only send the key when one was typed: the UI never holds the stored
  // plaintext, so an empty box means "leave it alone", not "clear it".
  const key = $("apiKeyInput").value.trim();
  if (key) patch.api_key = key;
  return patch;
}

async function saveSettings() {
  const btn = $("saveSettings");
  btn.disabled = true;
  try {
    const body = await api("/settings/llm", {
      method: "PUT",
      body: JSON.stringify(settingsPatch()),
    });
    $("settingsModal").hidden = true;
    state.settings = body.settings;
    updateModelChip(body.settings);
    pushNotice("info",
      body.applied === "hot-swapped"
        ? `模型已切换：${body.settings.provider_label} / ${body.settings.model}（无需重启）`
        : `配置已保存（${body.note || ""}）`);
  } catch (err) {
    showProbe({ ok: false, error: err.message });
  } finally {
    btn.disabled = false;
  }
}

async function probeSettings() {
  const btn = $("probeBtn");
  btn.disabled = true;
  btn.textContent = "测试中…";
  try {
    showProbe(await api("/settings/llm/probe", {
      method: "POST",
      body: JSON.stringify(settingsPatch()),
    }));
  } catch (err) {
    showProbe({ ok: false, error: err.message });
  } finally {
    btn.disabled = false;
    btn.textContent = "测试连通性";
  }
}

async function fetchModels() {
  const btn = $("fetchModels");
  btn.disabled = true;
  btn.textContent = "…";
  try {
    const provider = $("providerSelect").value;
    const body = await api(`/settings/models?provider=${encodeURIComponent(provider)}`);
    if (body.models?.length) {
      fillModelOptions(body.models,
        body.source === "live"
          ? `来自供应商的实时列表（${body.models.length} 个）`
          : `供应商未返回列表，显示预设候选（${body.error || ""}）`);
      if (!$("modelInput").value) $("modelInput").value = body.models[0];
    } else {
      fillModelOptions([], `没有拿到模型列表：${body.error || "未知原因"}`);
    }
  } catch (err) {
    fillModelOptions([], `列模型失败：${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "列模型";
  }
}

function updateModelChip(s) {
  $("modelChipText").textContent = `${s.provider_label || s.provider} / ${s.model || "未设置"}`;
  const parts = [
    `端点 ${s.base_url}`,
    s.api_key_set ? `Key 已设置（${s.api_key_masked}）` : "未设置 Key",
  ];
  // Two servers can run side by side with different data directories. Without
  // this in the tooltip there is no way to tell which one a tab is talking to.
  if (state.dataDir) parts.push(`数据目录 ${state.dataDir}`);
  $("modelChip").title = parts.join(" · ");
}

// ══════════════════════════════════════════════════════════════════════════
// boot
// ══════════════════════════════════════════════════════════════════════════

function wire() {
  $("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("input");
    const text = input.value;
    input.value = "";
    input.style.height = "";
    send(text);
  });

  const input = $("input");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      $("composer").requestSubmit();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 180) + "px";
  });

  for (const s of document.querySelectorAll(".sample")) {
    s.addEventListener("click", () => {
      $("input").value = s.dataset.text;
      $("composer").requestSubmit();
    });
  }

  $("viewTabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".tab");
    if (!tab) return;
    for (const t of document.querySelectorAll(".tab")) t.classList.remove("active");
    tab.classList.add("active");
    state.view = tab.dataset.view;
    loadView(false);
  });

  $("refreshView").addEventListener("click", () => loadView(true));
  $("refreshInspect").addEventListener("click", refreshInspector);
  $("clearBtn").addEventListener("click", () => {
    stream().replaceChildren();
    pushNotice("info", "显示已清空。已保存的会话仍在服务端（GET /threads）。");
  });

  $("settingsBtn").addEventListener("click", openSettings);
  $("modelChip").addEventListener("click", openSettings);
  $("closeSettings").addEventListener("click", () => { $("settingsModal").hidden = true; });
  $("cancelSettings").addEventListener("click", () => { $("settingsModal").hidden = true; });
  $("saveSettings").addEventListener("click", saveSettings);
  $("probeBtn").addEventListener("click", probeSettings);
  $("fetchModels").addEventListener("click", fetchModels);
  $("providerSelect").addEventListener("change", applyProviderDefaults);
  $("clearKey").addEventListener("click", async () => {
    // Explicit null, which the API reads as "clear" — distinct from omitting
    // the field, which means "leave the stored credential alone".
    try {
      await api("/settings/llm", {
        method: "PUT",
        body: JSON.stringify({ api_key: null }),
      });
      $("apiKeyInput").value = "";
      await openSettings();
    } catch (err) {
      showProbe({ ok: false, error: err.message });
    }
  });
  $("tempInput").addEventListener("input", (e) => {
    $("tempOut").textContent = Number(e.target.value).toFixed(2);
  });
  $("settingsModal").addEventListener("click", (e) => {
    if (e.target === $("settingsModal")) $("settingsModal").hidden = true;
  });
}

async function boot() {
  wire();
  setStatus("", "连接中…");
  try {
    const health = await api("/health");
    state.apiOk = true;
    state.dataDir = health.data_dir || null;
    const alive = health.worker_alive;
    if (alive === null || alive === undefined) {
      // The service stack is built lazily on the first real request, so "not
      // started yet" is the normal state at boot — showing it as a failure
      // would be alarming and wrong.
      setStatus("", "待启动（首次请求时拉起 FreeCAD）");
    } else if (alive) {
      setStatus("ok", "就绪");
    } else {
      setStatus("bad", "FreeCAD worker 未启动");
    }
  } catch (err) {
    // Nothing below this point can work, and every additional request would
    // just add another 404 to the console. Say what is wrong and stop.
    setStatus("bad", "服务不可达");
    showApiUnavailable(err.message);
    return;
  }

  try {
    const current = await api("/settings/llm");
    state.settings = current.settings;
    updateModelChip(current.settings);
    if (!current.settings.api_key_set && current.settings.needs_key) {
      pushNotice("warn",
        `模型「${current.settings.provider_label}」还没有 API Key —— 点右上角 ⚙ 配置后再发送。`);
    }
  } catch { /* settings are optional for the shell to render */ }

  try {
    const ir = await api(`/models/${encodeURIComponent(state.modelId)}/ir`);
    state.version = ir.version;
  } catch { state.version = null; }

  await loadHistory();
  refreshInspector();
  loadView(false);
}

/** Replay any stored conversation for this model.
 *
 * Without this, a page reload looks like the work was lost — the messages are
 * on disk the whole time (that is what `/threads` is for), they just were not
 * being read back.
 */
async function loadHistory() {
  try {
    const { threads } = await api(`/threads?model_id=${encodeURIComponent(state.modelId)}`);
    if (!threads.length) return;
    state.threadId = threads[0].thread_id;
    $("threadLabel").textContent = state.threadId;
    const { messages } = await api(`/threads/${state.threadId}/messages`);
    if (!messages.length) return;
    for (const message of messages) {
      if (message.role === "user") {
        pushUser(message.content);
      } else if (message.role === "assistant") {
        assistantBody = null;             // each stored message is its own bubble
        pushAssistantText(message.content);
      }
    }
    const welcome = $("welcome");
    if (welcome) welcome.remove();
    append(el("div", {
      class: "muted small",
      text: `已回放 ${messages.length} 条历史消息（thread ${state.threadId}）` +
            "。工具调用与 Gate 报告的完整过程只在当次流式输出中显示。",
    }));
  } catch { /* no history is a perfectly normal state */ }
}

boot();
