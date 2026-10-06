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

import { MeshViewport } from "./viewport.js?v=20261004-refactor";
import { validateArtifactScene } from "../../viewer/core/artifact.js";
import { ReferenceController } from "./references.mjs";
import { StructureController } from "./structure.mjs";
import { TreeHighlighter } from "./highlight.mjs";
import { WorkspaceController } from "./workspace.mjs";

let references = null;
let inspectorGeneration = 0;
let structure = null;
let workspace = null;
let compactMedia = null;
let sourceIr = null;
let displayedArtifact = null;
let structureRequest = 0;
let treeHighlighter = null;

function highlightSelection(entity, artifactId) {
  if (!meshViewer) return;
  treeHighlighter ||= new TreeHighlighter(meshViewer, { fetch, notice: message => pushNotice("warn", message) });
  return treeHighlighter.select(entity, artifactId, state.modelId);
}

function clearDisplayedStructure() {
  displayedArtifact = null;
  structureRequest += 1;
  structure?.update(sourceIr);
}

async function syncDisplayedStructure(artifact = displayedArtifact) {
  if (!structure) return;
  if (!artifact) { structure.update(sourceIr); return; }
  const token = sessionToken();
  const request = ++structureRequest;
  const current = () => !stale(token) && request === structureRequest && displayedArtifact === artifact;
  if (structure.artifactId === artifact.artifactId) {
    structure.setSourceVersion(sourceIr?.version);
    structure.render();
    return;
  }
  structure.message("正在读取画布构建的结构…");
  try {
    const ir = await api(`/artifact-sets/${encodeURIComponent(artifact.artifactId)}/files/ir.json`);
    if (!current()) return;
    if (ir.model_id !== token.modelId || ir.version !== artifact.version) throw new Error("结构与画布构建不匹配");
    structure.update(ir, { artifactId: artifact.artifactId, status: artifact.status, sourceVersion: sourceIr?.version });
  } catch (err) {
    if (current()) structure.message(`构建结构读取失败：${err.message}。点击刷新重试。`);
  }
}

function displayStructureArtifact(modelId, artifactId, version, status) {
  if (!artifactId || version == null) { clearDisplayedStructure(); return; }
  displayedArtifact = { modelId, artifactId, version, status };
  workspace?.showModel();
  syncDisplayedStructure();
}
function referenceUI() {
  if (!references) references = new ReferenceController($("referenceChips"), {
    onChange: refs => {
      if (!meshViewer) return;
      const latest = refs.filter(ref => ref.artifact_id === meshViewer.artifactId).at(-1);
      highlightSelection(latest, meshViewer.artifactId);
      if (latest) structure?.selectReference(latest);
    },
    notice: (message) => pushNotice("warn", message), focus: () => { workspace?.select("chat"); $("input").focus(); }, document,
  });
  return references;
}

const $ = (id) => document.getElementById(id);

const state = {
  // A session is one conversation bound to one model. `modelId` is derived from
  // the selected session rather than defaulting to a fixed name, so two sessions
  // cannot accidentally share a part.
  modelId: null,
  threadId: null,
  sessions: [],
  sessionsLoaded: false,
  // A failed list read is not the same as an empty list, and the sidebar must
  // not render one as the other.
  sessionsError: null,
  version: null,
  view: "iso",
  busy: false,
  apiOk: null,
  dataDir: null,
  lastGate: null,
  settings: null,
  providers: [],
  accessMode: "auto",
  sidebarHidden: false,
  abort: null,
  // The turn currently allowed to write to the screen. Identity, not a flag: a
  // switched-away turn is invalidated by replacing this object, which is what
  // makes its already-scheduled callbacks no-ops.
  turn: null,
  // Which session the screen currently belongs to. Every async loader captures
  // this before its first await and refuses to write afterwards if it changed —
  // otherwise a slow response for the session you just left lands in the panel of
  // the one you just opened (and, worse, gets labelled with the new model's id).
  sessionEpoch: 0,
  // The backend's verdict for the current version: { verified, passed, reason, … }.
  verdict: null,
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
  $("stopBtn").disabled = true;
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
  // Hidden, not removed: switching to an empty session has to be able to bring
  // the welcome panel back, and a deleted node cannot be restored.
  const welcome = $("welcome");
  if (welcome) welcome.hidden = true;
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

// A snapshot of which session a request belongs to, taken before any await.
function sessionToken() {
  return { epoch: state.sessionEpoch, modelId: state.modelId, version: state.version, threadId: state.threadId };
}

// Has the user moved to another session since this request started?
function stale(token) {
  return token.epoch !== state.sessionEpoch;
}

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
async function streamChat(body, handlers, signal) {
  const res = await fetch("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
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
    ? el("span", { class: "badge", text: "构建检查通过" })
    : null;

  const body = [];
  if (data.error) {
    body.push(el("pre", { text: `[${data.error.kind}] ${data.error.message}` +
      (data.error.feature_id ? `\n(feature_id=${data.error.feature_id})` : "") +
      (data.error.hint ? `\n修复建议：${data.error.hint}` : "") }));
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
  // A tool's fixed camera must never replace the user's interactive camera.
  if (meshViewer?.available) loadView(false);
  else {
    cancelViewRequest();
    displayImage(latest.url, latest.view);
    setViewMode(false, "静态预览 · 交互视图不可用");
  }
  return latest;
}

function pushHookLine(ev) {
  if (ev.decision === "allow" && ev.hook === "<no-hooks>") return;
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
  const advisory = row.severity === "advisory" && ["fail", "error"].includes(row.status);
  const cls = row.status === "pass" ? "pass"
    : row.status === "skip" || advisory ? "skip" : "fail";
  const mark = advisory ? "⚠" : row.status === "pass" ? "✓"
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
      el("span", { text: passed ? "✓ 构建检查通过" : "✗ 构建检查未通过" }),
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
  const token = sessionToken();
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
      result.completion_review?.verified ? "✓ 已记录的需求约束验收通过" : "✓ 构建通过 · 功能待验收",
      "产物已导出。构建通过仅说明几何与导出检查通过；实际功能和需求完整性仍需验收。",
    ],
    draft: ["warn", "草稿已生成 · 功能待验收", "当前构建通过，但需求仍有未验证项，不能据此认定功能完成。"],
    inspected: ["ok", "只读查看结束", "本次只查看和分析模型，没有修改或验收新的构建。"],
    exhausted: [
      "warn",
      "⚠ 预算耗尽，回合结束",
      "这不是成功。模型可能自称完成了 —— 以 Gate 报告为准。",
    ],
    failed: ["bad", "✗ 回合失败", result.error || "原因未知"],
    // Reached only by an explicit stop (a client that leaves aborts the stream
    // and gets no result at all), so it can say what actually happened. The
    // second line is the part that matters: stopping is not a failure, but it
    // also means the rest of the work was never checked.
    aborted: [
      "warn",
      "⏹ 回合已被打断",
      "你停止了这次生成。打断前已经写进 IR 的改动仍在（见右侧检查器与视图），" +
        "但此后没有任何东西经过 Gate 验证 —— 这不是完成。",
    ],
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
  if (result.completion_review) {
    pushNotice("info", result.completion_review.summary);
    for (const item of result.completion_review.checklist || []) {
      pushNotice("info", `需求复核：${item.source_text} · ${(item.check_ids || []).join(", ") || "无客观测量证据"}`);
    }
    for (const item of result.completion_review.remaining_work || []) pushNotice("warn", `待验收：${item}`);
    pushNotice("info", result.completion_review.note);
  }

  if (result.error && result.state !== "FAILED") pushNotice("bad", result.error);

  append(el("div", {
    class: "muted small",
    style: "margin-bottom:16px",
    text: `步数 ${result.steps} · 输入 ${result.tokens_in} / 输出 ${result.tokens_out} tokens`,
  }));

  const ok = result.state === "succeeded";
  // A stopped turn is neither success nor fault: it is a deliberate outcome, so
  // it gets neither the green "完成" nor the red "未完成" that a failure gets.
  const stopped = result.state === "aborted";
  const draft = result.state === "draft";
  const inspected = result.state === "inspected";
  setStatus(ok || inspected ? "ok" : draft || stopped ? "" : "bad",
    inspected ? "只读查看结束" : ok ? (result.completion_review?.verified ? "约束验收通过" : "构建通过") : draft ? "草稿 · 待验收" : stopped ? "已打断" : "未完成");

  // Await the inspector so `state.version` is current before the artefacts and
  // the viewport are read — otherwise the UI can describe a version it has not
  // caught up with yet.
  await refreshInspector();
  if (stale(token)) return;
  loadArtifacts();
  loadView(false);

  // The turn changed this session's title, age and message count, and possibly
  // its IR version. Refresh the list so the sidebar describes the state it is
  // actually in — a stale "新会话" on a session that just built a part is the
  // kind of small lie that erodes trust in the whole panel.
  await loadSessions();
  if (stale(token)) return;

  // ...and the header of the conversation you are looking at. The list and the
  // header read the same field; leaving the header behind means the first turn
  // of every session is titled "新会话" until you navigate away and back.
  setSessionHeader(state.sessions.find((s) => s.thread_id === state.threadId) || null);
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

/** A name for this turn, minted before anything is sent.
 *
 * The stop button has to be able to say *which* turn to stop, and it must be
 * able to say it immediately: waiting for the server's `start` frame would
 * leave a window in which a click is simply ignored while the model keeps
 * generating. So the client names its own turn and the server adopts the name.
 */
function newRequestId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/** Stop the turn that is running, without leaving the session.
 *
 * Two mechanisms, in order:
 *
 *  1. ask the server to cancel exactly this turn — it can, because the client
 *     named it before sending anything. The outcome comes back on the *same*
 *     stream as a normal `result` frame with state `aborted`, so the verdict on
 *     screen still comes from the engine, like every other outcome;
 *  2. if that request itself fails (an older server, a dead socket), close the
 *     stream — which the server also reads as "stop this turn". Falling back to
 *     it beats leaving a model generating with no way to stop it.
 */
async function stopTurn() {
  const turn = state.turn;
  if (!turn || turn.stopped) return;
  turn.stopped = true;
  $("stopBtn").disabled = true;
  setStatus("busy", "正在打断…");
  setLive("正在打断这次生成…");
  try {
    const res = await api("/chat/interrupt", {
      method: "POST",
      body: JSON.stringify({ request_id: turn.requestId }),
    });
    if (res && res.stage === "pending") {
      // The turn had not registered yet; the stop is remembered and will be
      // honoured at registration. Say that, rather than "stopped" — it has not
      // stopped *yet*.
      pushNotice("info", "该回合尚未开始，已记为「一启动就停」。");
    }
  } catch (err) {
    pushNotice("warn", `打断接口不可用（${err.message}），改为断开连接。`);
    turn.controller.abort();
  }
}

async function send(text) {
  const trimmed = text.trim();
  if (!trimmed || state.busy) return;

  // Nothing selected means nothing to send into. Create the session first, and
  // before the message is rendered: switching sessions clears the stream, which
  // would otherwise erase the bubble we had just appended.
  if (!state.threadId) {
    try {
      await newSession({ announce: false });
    } catch (err) {
      pushNotice("bad", `无法新建会话：${err.message}`);
      return;
    }
  }

  // This turn's identity, captured before anything can switch sessions.
  //
  // Switching nulls `state.turn`, which is what turns every callback below into
  // a no-op. Without it an abandoned request keeps writing to whatever session
  // replaced it: its frames render there, its AbortError is reported as "已离开
  // 该会话" *inside a conversation you never left*, and its `finally` deletes the
  // live row of the turn that is still running.
  //
  // `requestId` is what "stop" names, and `stopped` records that the user asked
  // for the end — the only way to tell a deliberate stop from "the user left the
  // session" if the stop has to fall back to closing the socket.
  const turn = {
    controller: new AbortController(),
    requestId: newRequestId(),
    accessMode: state.accessMode,
    selectionContext: referenceUI().context(),
    inspectionOnly: referenceUI().inspectionOnly(),
    operationId: newRequestId(),
    stopped: false,
  };
  state.turn = turn;
  const current = () => state.turn === turn;

  state.busy = true;
  referenceUI().setBusy(true);
  state.abort = turn.controller;
  assistantBody = null;
  $("sendBtn").disabled = true;
  $("stopBtn").hidden = false;
  $("stopBtn").disabled = false;
  setStatus("busy", "生成中…");
  setLive("正在请求模型…");
  renderSessions();   // the sidebar marks this session as running
  pushUser(trimmed);

  let resultWork = Promise.resolve();
  let needsReferenceRefresh = false;
  let terminalReceived = false;
  try {
    await ensureModel();
    await streamChat(
      {
        model_id: state.modelId,
        text: trimmed,
        thread_id: state.threadId,
        request_id: turn.requestId,
        access_mode: turn.inspectionOnly ? "read_only" : turn.accessMode,
        ...(turn.selectionContext ? { selection_context: turn.selectionContext,
          operation_id: turn.operationId, kind: turn.inspectionOnly ? "inspect" : "modify" } : {}),
      },
      {
        start: (d) => {
          if (!current()) return;
          state.threadId = d.thread_id;
          $("threadLabel").textContent = d.thread_id;
        },
        agent: (d) => { if (current()) handleAgentEvent(d); },
        progress: (ev) => { if (current()) pushHookLine(ev); },
        error: (d) => {
          if (current()) {
            terminalReceived = true;
            pushNotice("bad", `${d.type}: ${d.message}`); needsReferenceRefresh = true;
          }
        },
        result: (r) => {
          if (current()) { terminalReceived = true; resultWork = handleResult(r); }
        },
      },
      turn.controller.signal,
    );
    await resultWork;
    if (current() && !terminalReceived && turn.selectionContext) {
      needsReferenceRefresh = true;
      pushNotice("warn", "连接结束，但修改结果未确认。请查看当前模型后重新引用对象。");
    }
    // The terminal status is set by handleResult, from the engine's verdict —
    // not here, where we would only know that the stream ended.
  } catch (err) {
    // A superseded turn has no screen to write to: `switchSession` already
    // replaced the transcript and set the status line. This branch is exactly
    // where a deliberate switch used to be reported as a fault.
    if (!current()) return;
    needsReferenceRefresh = true;
    if (err.name === "AbortError") {
      // Two very different reasons land here: the user left the session, or the
      // user pressed 停止 and the stop had to fall back to closing the socket.
      // Reporting the second as "you left the session" would be a lie about
      // what the user just did.
      pushNotice("warn", turn.stopped
        ? "已打断本次回合。打断前的改动仍在，但此后没有任何东西经过 Gate 验证。"
        : "已离开该会话，本次回合已中断。");
    } else {
      pushNotice("bad", `请求失败：${err.message}`);
      setStatus("bad", "出错");
    }
  } finally {
    // Only the turn that still owns the screen may clean up after itself.
    // Otherwise it disarms the *next* turn's abort controller and re-enables the
    // send button while that turn is mid-flight.
    if (current()) {
      state.turn = null;
      state.busy = false;
      referenceUI().setBusy(false);
      if (needsReferenceRefresh && turn.selectionContext) {
        referenceUI().clear();
        refreshInspector();
      }
      state.abort = null;
      clearLive();
      $("sendBtn").disabled = false;
      $("stopBtn").hidden = true;
      $("stopBtn").disabled = false;
      renderSessions();
    }
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
  if (d.kind === "build") {
    const phases = { staging: "准备构建", cache: "检查已有几何", geometry_reused: "复用已有几何",
      compile: "构建零件", components: "构建装配组件", assemble: "组合组件",
      queued: "等待构建", validate: "校验输入", gate: "验证构建", published: "构建已发布",
      failed_attempt: "构建未通过验证", failed: "构建失败", cancelled: "构建已取消", timeout: "构建超时" };
    if (d.phase === "complete") phases.complete = d.state === "published" ? "构建已发布" : "构建失败";
    if (d.model_id === state.modelId && phases[d.phase]) setLive(`v${d.ir_version} · ${phases[d.phase]}`);
    return;
  }
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
    loadView(false, { version: d.gate.ir_version });
  }
}

// ══════════════════════════════════════════════════════════════════════════
// viewport / files / inspector
// ══════════════════════════════════════════════════════════════════════════

// Every load, including another load in the SAME session, invalidates the last.
// Aborting saves work; the sequence/token checks are the authority, because an
// already-resolved response can still finish after abort().
let meshViewer = null;
let viewRequest = 0;
let viewAbort = null;
let viewBlobUrl = null;

function cancelViewRequest() {
  viewRequest += 1;
  viewAbort?.abort();
  viewAbort = null;
  return viewRequest;
}

function selectView(view) {
  state.view = view;
  for (const tab of document.querySelectorAll(".tab")) {
    const selected = tab.dataset.view === view;
    tab.classList.toggle("active", selected);
    tab.setAttribute("aria-pressed", String(selected));
  }
}

function setViewMode(interactive, message = "", warning = false) {
  $("fitView").disabled = !interactive || !meshViewer?.hasMesh;
  $("viewHelp").hidden = !interactive || !meshViewer?.hasMesh;
  $("viewStatus").textContent = message;
  $("viewStatus").title = message;
  $("viewStatus").classList.toggle("warn", warning);
  // The headless PNG API supports these four views. Do not offer buttons that
  // would request unsupported views when WebGL or mesh loading is unavailable.
  for (const tab of document.querySelectorAll(".tab")) {
    tab.disabled = !interactive && !["iso", "front", "top", "right"].includes(tab.dataset.view);
  }
}

function clearViewImage() {
  const img = $("viewImage");
  img.onload = null;
  img.onerror = null;
  img.hidden = true;
  img.removeAttribute("src");
  if (viewBlobUrl) URL.revokeObjectURL(viewBlobUrl);
  viewBlobUrl = null;
}

/** Put an already-generated PNG in the fallback viewport. */
function displayImage(url, view) {
  const img = $("viewImage"), placeholder = $("viewPlaceholder");
  const token = sessionToken(), request = viewRequest;
  clearViewImage();
  if (view) selectView(view);
  if (url.startsWith("blob:")) viewBlobUrl = url;
  img.onload = () => {
    if (stale(token) || request !== viewRequest) return;
    img.hidden = false;
    placeholder.hidden = true;
  };
  img.onerror = () => {
    if (stale(token) || request !== viewRequest) return;
    img.hidden = true;
    placeholder.hidden = false;
    placeholder.textContent = "预览图片加载失败，请点击刷新重试";
  };
  img.src = url;
}

async function loadView(force, { version = null } = {}) {
  const token = sessionToken();
  const request = cancelViewRequest();
  const controller = new AbortController();
  viewAbort = controller;
  const current = () => !stale(token) && request === viewRequest;
  const placeholder = $("viewPlaceholder");
  const hasPreview = meshViewer?.hasMesh || !$("viewImage").hidden;
  placeholder.textContent = state.busy ? "生成中…" : "加载几何…";
  placeholder.hidden = !!hasPreview;
  $("viewStatus").textContent = hasPreview ? "正在更新几何，保留当前视角…" : "正在读取几何…";

  if (!token.modelId) {
    meshViewer?.clear();
    clearDisplayedStructure();
    clearViewImage();
    placeholder.hidden = false;
    placeholder.textContent = "还没有会话 —— 左侧点「＋ 新建」，或直接在下面描述一个零件";
    setViewMode(!!meshViewer?.available, "暂无几何");
    return;
  }

  const params = new URLSearchParams({ force: force ? "true" : "false" });
  // A live commit can arrive before the inspector learns its version. Omit the
  // version then and let the server resolve current, instead of reloading an
  // old cached state.version. Explicit known commit versions stay pinned.
  if (version != null) params.set("version", String(version));
  let fallbackReason = meshViewer?.error || "WebGL 不可用";
  if (meshViewer?.available) {
    try {
      const res = await fetch(`/models/${encodeURIComponent(token.modelId)}/mesh?${params}`, { signal: controller.signal });
      if (stale(token) || request !== viewRequest) return;
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
        if (!current()) return;
        if (res.status === 429) {
          // PNG rendering uses the same worker: falling back would bypass the
          // server's preview admission limit and amplify a refresh storm.
          const prior = meshViewer.hasMesh || !$("viewImage").hidden;
          placeholder.hidden = prior;
          placeholder.textContent = "预览繁忙，请稍后点击刷新重试";
          setViewMode(meshViewer.hasMesh, prior
            ? "预览繁忙 · 仍显示此前几何，请稍后点击刷新"
            : "预览繁忙 · 请稍后点击刷新重试", true);
          return;
        }
        if (res.status === 404 || (res.status === 422 && /no solid|empty mesh|no triangles/i.test(detail))) {
          meshViewer.clear(); clearViewImage(); clearDisplayedStructure();
          placeholder.hidden = false;
          placeholder.textContent = res.status === 404
            ? "还没有已发布的几何 —— 构建并验证通过后这里会自动显示"
            : "还没有几何 —— 在左边描述一个零件，构建后这里会自动显示";
          setViewMode(true, "暂无几何");
          return;
        }
        throw new Error(`${res.status} ${detail}`);
      }
      const body = await res.json();
      if (!current()) return;
      validateArtifactScene(body, { modelId: token.modelId, version });
      params.set("artifact_id", body.artifact_id);
      const count = meshViewer.setMesh(body.mesh, body.motion || [], body.animation || null, body.pick_mapping || null);
      meshViewer.artifactId = body.artifact_id;
      displayStructureArtifact(body.model_id, body.artifact_id, body.version, body.status);
      $("pickKind").disabled = state.selectionEnabled === false || !body.pick_mapping || body.status !== "verified";
      meshViewer.pickMode = $("pickKind").disabled ? null : $("pickKind").value || null;
      clearViewImage();
      placeholder.hidden = true;
      const status = body.status === "verified" ? "几何已验证" : "几何未验证";
      setViewMode(true, `构建 v${body.version} · ${status} · ${count.toLocaleString()} 三角面 · 拖动旋转，滚轮缩放`);
      return;
    } catch (err) {
      if (!current() || err.name === "AbortError") return;
      fallbackReason = err.message;
    }
  }

  // A failed mesh/WebGL path still leaves the offline software renderer usable.
  // Never label this image as an interactive model, or leave the prior version
  // on screen after a failed refresh.
  if (!current()) return;
  meshViewer?.clear(); clearViewImage(); clearDisplayedStructure();
  placeholder.hidden = false;
  placeholder.textContent = "正在加载静态预览…";
  setViewMode(false, `静态预览 · ${fallbackReason}`, true);
  const view = ["iso", "front", "top", "right"].includes(state.view) ? state.view : "iso";
  selectView(view);
  params.set("view", view);
  try {
    const res = await fetch(`/models/${encodeURIComponent(token.modelId)}/render?${params}`, { signal: controller.signal });
    if (!current()) return;
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
      if (!current()) return;
      placeholder.textContent = res.status === 404 ? "还没有已发布的几何 —— 构建并验证通过后这里会自动显示"
        : res.status === 422 && /no solid/i.test(detail) ? "还没有几何 —— 在左边描述一个零件"
        : `无法渲染：${detail}`;
      return;
    }
    const blob = await res.blob();
    if (!current()) return;
    const renderedVersion = res.headers?.get("X-Artifact-Version");
    const renderedStatus = res.headers?.get("X-Artifact-Status");
    const renderedId = res.headers?.get("X-Artifact-ID");
    if (params.has("artifact_id") && renderedId !== params.get("artifact_id")) throw new Error("预览与请求的构建身份不匹配");
    if (renderedVersion != null) {
      if (version != null && Number(renderedVersion) !== version) throw new Error("预览与请求的构建版本不匹配");
      setViewMode(false, `静态预览 · 构建 v${renderedVersion} · ${renderedStatus === "verified" ? "几何已验证" : "几何未验证"}`);
    }
    displayStructureArtifact(token.modelId, renderedId, renderedVersion == null ? null : Number(renderedVersion), renderedStatus);
    displayImage(URL.createObjectURL(blob));
  } catch (err) {
    if (!current() || err.name === "AbortError") return;
    placeholder.textContent = `无法渲染：${err.message}`;
  }
}

async function loadArtifacts() {
  const bar = $("fileBar");
  const token = sessionToken();
  bar.replaceChildren();
  if (!token.modelId) {
    bar.append(el("span", { class: "muted small", text: "未选择会话" }));
    return;
  }
  try {
    const body = await api(
      `/models/${encodeURIComponent(token.modelId)}/artifacts` +
      (token.version != null ? `?version=${token.version}` : "")
    );
    if (stale(token)) return;   // the user moved on; this answer is not ours to show
    const files = (body.files || []).filter((f) => !f.endsWith(".png") && (!f.endsWith(".json") || f.endsWith("animation.json")) && !f.toLowerCase().endsWith(".fcbak"));
    if (!files.length) {
      bar.append(el("span", { class: "muted small", text: "暂无导出产物（STEP / STL 在 Gate 通过后生成）" }));
      return;
    }
    // Link with the identity the RESPONSE described, not with whatever the
    // session variable holds now — those differ exactly when they must not.
    bar.append(el("span", { class: "export-label", text: "导出" }));
    for (const name of files) {
      const format = name.endsWith("animation.json") ? "动画数据" : name.split(".").at(-1);
      bar.append(el("a", {
        href: `/models/${encodeURIComponent(token.modelId)}/artifacts/${name}` +
              `?version=${body.version}`,
        text: format === "FCStd" ? "FreeCAD" : format.toUpperCase(),
        title: name,
        download: "",
      }));
    }
  } catch (err) {
    if (stale(token)) return;
    bar.append(el("span", { class: "muted small", text: `产物读取失败：${err.message}` }));
  }
}

// The backend's answer to "is this version verified?", shown verbatim.
//
// Artifacts on disk are not the same claim as "the Gate passed this version":
// after a write, the previous version's report is still green but describes a
// part the user has since changed. Only the backend can tell those apart, so the
// panel shows its verdict and its reason rather than inferring one.
async function renderVerdict(box, token) {
  const sec = el("div", { class: "sec" }, el("h4", { text: "验证状态" }));
  if (!token.modelId) {
    sec.append(el("div", { class: "muted small", text: "—" }));
    box.append(sec);
    return;
  }
  let v = null;
  try {
    v = await api(`/models/${encodeURIComponent(token.modelId)}/verdict`);
    if (stale(token)) return;
    state.verdict = v;
  } catch (err) {
    if (stale(token)) return;
    sec.append(el("div", { class: "muted small", text: `状态读取失败：${err.message}` }));
    box.append(sec);
    return;
  }
  sec.append(el("div", { class: "kv" }, [
    el("span", { class: "k", text: "当前版本" }),
    el("span", {
      class: "v",
      style: `color:${v.verified ? "var(--ok)" : "var(--bad)"}`,
      text: v.verified ? "构建已验证 · 功能待验收" : "构建未验证",
    }),
  ]));
  if (v.graded_version !== null && v.graded_version !== undefined) {
    sec.append(kv("最近评级版本", `v${v.graded_version}`));
  }
  if (v.attempt_id) sec.append(kv("attempt", v.attempt_id));
  if (v.blocking_failures?.length) {
    sec.append(el("div", { class: "muted small", text: `阻断项：${v.blocking_failures.join(", ")}` }));
  }
  sec.append(el("div", { class: "muted small", text: v.reason || "" }));
  box.append(sec);
}

function kv(key, value) {
  return el("div", { class: "kv" }, [
    el("span", { class: "k", text: key }),
    el("span", { class: "v", text: String(value) }),
  ]);
}

async function refreshInspector() {
  const box = $("inspector");
  const token = sessionToken();
  const generation = ++inspectorGeneration;
  box.replaceChildren();

  const model = el("div", { class: "sec" }, [
    el("h4", { text: "模型" }),
    kv("model_id", token.modelId ?? "—"),
    kv("IR 版本", token.version ?? "—"),
    state.threadId ? kv("thread", state.threadId) : null,
  ]);
  box.append(model);

  await renderVerdict(box, token);
  const outdated = () => stale(token) || generation !== inspectorGeneration;
  if (outdated()) return;

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

  // ── source version and the current publication's reference catalog ──────
  if (!token.modelId) {
    box.append(el("div", { class: "muted small", text: "未选择会话。" }));
    return;
  }
  try {
    const ir = await api(`/models/${encodeURIComponent(token.modelId)}/ir`);
    // The await is where the user can switch sessions. Writing afterwards would
    // put this model's feature chain (and version) into the new session's panel.
    if (outdated()) return;
    state.version = ir.version;

    let catalog = null;
    try { catalog = await api(`/models/${encodeURIComponent(token.modelId)}/selection-targets`); }
    catch { /* Browsing pending/legacy builds remains available. */ }
    if (outdated()) return;
    referenceUI().update(catalog, token.modelId, ir.version);
    referenceUI().beginTree();

    sourceIr = ir;
    syncDisplayedStructure();

    if (ir.requirements?.raw_text) {
      box.append(el("div", { class: "sec" }, [
        el("h4", { text: "需求原文" }),
        el("div", { class: "muted small", text: ir.requirements.raw_text }),
      ]));
    }
  } catch (err) {
    if (outdated()) return;
    referenceUI().update(null, token.modelId, token.version);
    referenceUI().beginTree();
    if (!displayedArtifact) {
      sourceIr = null;
      structure?.message(String(err.message).startsWith("404") ? "模型尚未创建，发送第一条设计要求后会自动生成。" : `模型结构读取失败：${err.message}`);
    }
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
    if (!token.threadId) return;
    const { pending } = await api(`/approvals?thread_id=${encodeURIComponent(token.threadId)}`);
    if (outdated()) return;
    if (pending.length) {
      const sec = el("div", { class: "sec" }, el("h4", { text: `待批 (${pending.length})` }));
      for (const p of pending) {
        // The record's key is `id`. This used to read the `approval_id` spelling,
        // which the API never returns, so both buttons posted to
        // `/approvals/undefined` and no approval could be granted from the UI.
        sec.append(el("div", { class: "notice warn" }, [
          el("div", { text: `${p.tool_name}` }),
          el("div", { class: "small", text: p.id }),
          // What is actually being authorised. An approval that shows only a tool
          // name is a signature on a blank page.
          el("div", { class: "small", text: p.args_summary || "(未记录参数)" }),
          el("div", { class: "small", text: p.thread_id ? `会话 ${p.thread_id}` : "" }),
          el("div", { class: "row", style: "margin-top:8px;display:flex;gap:8px" }, [
            el("button", {
              class: "mini",
              text: "批准",
              onclick: async () => {
                await api(`/approvals/${p.id}`, {
                  method: "POST", body: JSON.stringify({ granted: true }),
                });
                refreshInspector();
              },
            }),
            el("button", {
              class: "mini",
              text: "拒绝",
              onclick: async () => {
                await api(`/approvals/${p.id}`, {
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

function syncProviderEndpoint() {
  const preset = state.providers.find((p) => p.id === $("providerSelect").value);
  const input = $("baseUrlInput");
  input.disabled = Boolean(preset && preset.id !== "custom");
  if (input.disabled) input.value = preset.base_url || "";
}

let settingsReturnFocus = null;

function closeSettingsDialog() {
  $("settingsModal").hidden = true;
  settingsReturnFocus?.focus();
  settingsReturnFocus = null;
}

function bindSettingsKeyboard() {
  const modal = $("settingsModal");
  modal.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeSettingsDialog();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = Array.from(modal.querySelectorAll("button, input, select, textarea, [tabindex='0']"))
      .filter(node => !node.disabled && node.getClientRects().length);
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!first) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
}

function bindSettingsDismissal() {
  const modal = $("settingsModal");
  let pressedBackdrop = false;
  modal.addEventListener("pointerdown", (event) => {
    pressedBackdrop = event.target === modal;
  });
  modal.addEventListener("pointercancel", () => { pressedBackdrop = false; });
  modal.addEventListener("click", (event) => {
    if (pressedBackdrop && event.target === modal) closeSettingsDialog();
    pressedBackdrop = false;
  });
}

function applyProviderDefaults() {
  const id = $("providerSelect").value;
  const preset = state.providers.find((p) => p.id === id);
  if (!preset) return;
  $("baseUrlInput").value = preset.base_url || "";
  syncProviderEndpoint();
  $("modelInput").value = preset.default_model || "";
  fillModelOptions(preset.models || [], "预设候选（点「列模型」获取实时列表）");
}

function fillModelOptions(models, hint, freeModels = []) {
  const list = $("modelOptions");
  list.replaceChildren(el("option", { value: "", text: "选择模型…" }));
  const isOpenRouter = $("providerSelect").value === "openrouter";
  const free = new Set(freeModels);
  const isFree = (model) => isOpenRouter &&
    (free.has(model) || model.endsWith(":free") || model === "openrouter/free");
  const ordered = [...models].sort((a, b) => Number(isFree(b)) - Number(isFree(a)));
  for (const m of ordered) list.append(el("option", {
    value: m, text: isFree(m) ? `免费 · ${m}` : m,
  }));
  list.disabled = models.length === 0;
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
  settingsReturnFocus = document.activeElement;
  modal.hidden = false;
  $("closeSettings").focus();
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
    syncProviderEndpoint();
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
    closeSettingsDialog();
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
    const body = await api("/settings/models", {
      method: "POST",
      body: JSON.stringify(settingsPatch()),
    });
    if (body.models?.length) {
      fillModelOptions(body.models,
        body.source === "live"
          ? `来自供应商的实时列表（${body.models.length} 个）`
          : `供应商未返回列表，显示预设候选（${body.error || ""}）`,
        body.free_models || []);
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
  const modes = {
    auto: "自动审批：允许 CAD 读写，保留路径限制；不开放 Python 执行。",
    read_only: "仅可读取：允许查看与测量，不修改模型、不生成导出；预览可能生成缓存。",
    full: "完全访问：允许 CAD 读写及 Python 执行；不进行工具审批、路径限制或 Python 沙箱隔离。",
  };
  try {
    const saved = localStorage.getItem("tcad.accessMode");
    if (Object.hasOwn(modes, saved)) state.accessMode = saved;
  } catch { /* storage may be unavailable */ }
  const accessTrigger = $("accessMode"), accessMenu = $("accessMenu");
  const accessOptions = [...accessMenu.querySelectorAll(".access-option")];
  const syncAccess = () => {
    const chosen = accessOptions.find(option => option.dataset.mode === state.accessMode);
    $("accessModeLabel").textContent = chosen.querySelector(".access-option-title").firstChild.textContent;
    accessTrigger.dataset.mode = state.accessMode;
    accessTrigger.title = modes[state.accessMode] + " 切换影响下一次请求。";
    accessTrigger.setAttribute("aria-label", "权限模式：" + $("accessModeLabel").textContent);
    for (const option of accessOptions) option.setAttribute("aria-checked", String(option === chosen));
  };
  const closeAccess = (focus = false) => {
    accessMenu.hidden = true;
    accessTrigger.setAttribute("aria-expanded", "false");
    if (focus) accessTrigger.focus();
  };
  const openAccess = (last = false) => {
    accessMenu.hidden = false;
    accessTrigger.setAttribute("aria-expanded", "true");
    (last ? accessOptions.at(-1) : accessOptions.find(option => option.dataset.mode === state.accessMode)).focus();
  };
  syncAccess();
  accessTrigger.addEventListener("click", () => accessMenu.hidden ? openAccess() : closeAccess(true));
  accessTrigger.addEventListener("keydown", event => {
    if (["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault(); openAccess(event.key === "ArrowUp");
    }
  });
  for (const [index, option] of accessOptions.entries()) {
    option.addEventListener("click", () => {
      const changed = state.accessMode !== option.dataset.mode;
      state.accessMode = option.dataset.mode;
      syncAccess(); closeAccess(true);
      try { localStorage.setItem("tcad.accessMode", state.accessMode); } catch { /* optional */ }
      if (changed) pushNotice("info", modes[state.accessMode] + (state.busy ? " 当前回合权限保持不变。" : ""));
    });
    option.addEventListener("keydown", event => {
      const target = { ArrowDown: (index + 1) % 3, ArrowUp: (index + 2) % 3, Home: 0, End: 2 }[event.key];
      if (target !== undefined) { event.preventDefault(); accessOptions[target].focus(); }
    });
  }
  $("accessPicker").addEventListener("keydown", event => {
    if (event.key === "Escape" && !accessMenu.hidden) {
      event.preventDefault(); event.stopPropagation(); closeAccess(true);
    }
  });
  document.addEventListener("pointerdown", event => {
    if (!$("accessPicker").contains(event.target)) closeAccess();
  });
  $("accessPicker").addEventListener("focusout", event => {
    if (!$("accessPicker").contains(event.relatedTarget)) closeAccess();
  });
  meshViewer = new MeshViewport($("viewCanvas"), {
    axes: $("viewAxes"),
    motionControls: { root: $("motionControls"), input: $("crankAngle"), output: $("crankAngleValue"), play: $("animationPlay"), reset: $("animationReset"), speed: $("animationSpeed"), loop: $("animationLoop"), label: $("motionLabel"), note: $("motionNote") },
    onChange: ({ view }) => selectView(view),
    onSelectionChange: entity => {
      if (entity) structure?.selectBody(entity.body_id, meshViewer.artifactId);
      return referenceUI().selectGeometry(entity, meshViewer.artifactId);
    },
    onError: () => loadView(false),
  });
  setViewMode(meshViewer.available, meshViewer.available
    ? "真实网格 · 自由旋转 / 缩放 / 平移" : "静态预览 · " + meshViewer.error, !meshViewer.available);
  $("pickKind").addEventListener("change", () => { meshViewer.pickMode = $("pickKind").value || null; });
  $("fitView").addEventListener("click", () => meshViewer.fit());

  structure = new StructureController({
    tree: $("structureTree"), detail: $("structureDetail"), search: $("structureSearch"),
    meta: $("structureMeta"), count: $("structureCount"), expand: $("expandStructure"), collapse: $("collapseStructure"),
  }, {
    beginReferences: () => referenceUI().beginTree(),
    referenceButton: (node, artifactId) => artifactId && referenceUI().catalog?.artifact_id === artifactId
      ? referenceUI().button(node.bodyId, node.kind, node.id) : null,
    onSelect: (node, artifactId) => {
      if (!meshViewer?.hasMesh) return;
      highlightSelection(node ? { body_id: node.bodyId, entity_kind: node.kind,
        ...(node.kind === "sketch" ? { sketch_id: node.id } : node.kind === "feature" ? { feature_id: node.id } : {}) } : null, artifactId);
    },
  });
  const panelTabs = [$("structureTab"), $("checksTab")];
  const selectPanel = index => {
    for (const [i, tab] of panelTabs.entries()) {
      tab.setAttribute("aria-selected", String(i === index));
      tab.tabIndex = i === index ? 0 : -1;
    }
    $("structurePanel").hidden = index !== 0;
    $("inspector").hidden = index !== 1;
  };
  for (const [index, tab] of panelTabs.entries()) {
    tab.addEventListener("click", () => selectPanel(index));
    tab.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const target = event.key === "Home" ? 0 : event.key === "End" ? 1 : 1 - index;
      selectPanel(target); panelTabs[target].focus();
    });
  }

  bindWorkspace();
  bindComposer();

  $("viewTabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".tab");
    if (!tab) return;
    if (tab.disabled) return;
    selectView(tab.dataset.view);
    if (meshViewer?.available && meshViewer.hasMesh) meshViewer.setPreset(state.view);
    else {
      if (meshViewer?.available) meshViewer.setPreset(state.view);
      loadView(false);
    }
  });

  $("refreshView").addEventListener("click", () => loadView(true));
  $("refreshInspect").addEventListener("click", refreshInspector);
  $("clearBtn").addEventListener("click", () => {
    // Clears the *view*, not the session. The transcript is still on disk and
    // comes back by switching away and back.
    resetStream(false);
    pushNotice("info", "显示已清空（不影响已保存的会话）。切走再切回即可重新回放。");
  });

  $("newSessionBtn").addEventListener("click", async () => {
    try {
      await newSession();
    } catch (err) {
      pushNotice("bad", `新建会话失败：${err.message}`);
    }
  });
  $("sidebarToggle").addEventListener("click", () => toggleSidebar());

  // ⌘/Ctrl+K for a new session, the shortcut people already have in their fingers.
  document.addEventListener("keydown", (e) => {
    if (e.defaultPrevented) return;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      $("newSessionBtn").click();
    }
    // Esc is the "get me out of this" key. While a turn is generating, that is
    // the turn — but only when no dialog is open, or Esc would mean two things
    // at once and stop a turn the user was not even looking at.
    if (e.key === "Escape" && state.busy && $("settingsModal").hidden) {
      e.preventDefault();
      stopTurn();
    }
  });

  $("settingsBtn").addEventListener("click", openSettings);
  $("modelChip").addEventListener("click", openSettings);
  $("stopBtn").addEventListener("click", stopTurn);
  $("closeSettings").addEventListener("click", closeSettingsDialog);
  $("cancelSettings").addEventListener("click", closeSettingsDialog);
  $("saveSettings").addEventListener("click", saveSettings);
  $("probeBtn").addEventListener("click", probeSettings);
  $("fetchModels").addEventListener("click", fetchModels);
  $("providerSelect").addEventListener("change", applyProviderDefaults);
  $("modelOptions").addEventListener("change", () => {
    if ($("modelOptions").value) $("modelInput").value = $("modelOptions").value;
    $("modelOptions").value = "";
  });
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
  bindSettingsDismissal();
  bindSettingsKeyboard();
}

// A turn with no work ceiling can legitimately run for a long time, and nothing
// else on screen would explain why. Show it, and say what still protects the
// loop against a wedged call — "unbounded" here means "no work quota", not
// "no timeouts at all", and conflating the two would be its own confusion.
function renderBudgetBadge(budget) {
  const el = $("budgetBadge");
  if (!el) return;
  if (!budget || !budget.unbounded || !budget.unbounded.length) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  el.textContent = budget.unbounded_all ? "无预算上限" : `${budget.unbounded.length} 项无上限`;
  const bounded = Object.entries(budget.limits || {})
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => `  ${k} = ${v}`);
  el.title = [
    budget.unbounded_all
      ? "本次运行不限制步数、token、时长与编译重试。"
      : "以下维度未设上限：" + budget.unbounded.join(", "),
    "",
    "构建通过后还要复核需求；缺少功能证据会生成待验收草稿，不能视为功能完成。",
    "单次请求的活性仍受保护：LLM 请求超时、工具自身超时、worker 传输超时。",
    ...(bounded.length ? ["", "在效的上限：", ...bounded] : []),
  ].join("\n");
}

// ══════════════════════════════════════════════════════════════════════════
// sessions
//
// A session is one conversation bound to one model. Switching therefore has to
// move *two* things — the transcript and everything derived from the model: the
// viewport, the artefacts, the feature chain, the IR version. Updating only the
// transcript would leave the panes describing the part you just navigated away
// from, which is worse than not switching at all: the numbers on screen would
// look authoritative and belong to something else.
// ══════════════════════════════════════════════════════════════════════════

function sessionTitle(s) {
  const raw = (s.title || "").trim() || (s.last_message || "").trim();
  if (raw) return raw.length > 60 ? raw.slice(0, 60) + "…" : raw;
  return s.messages ? `会话 ${s.thread_id.slice(-6)}` : "新会话";
}

/** Coarse relative time. Exactness is not the point — ordering is, and the
 *  ordering is already carried by the list itself. */
function formatWhen(iso) {
  if (!iso) return "";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)} 天前`;
  return new Date(then).toLocaleDateString();
}

function renderSessions() {
  const box = $("sessionList");
  box.replaceChildren();

  // A read that failed is not an empty list. Rendering it as "还没有会话" tells
  // you your conversations are gone when in fact the request never landed —
  // exactly the kind of failure that looks like a normal state and therefore
  // gets believed. Say what happened, and offer the retry.
  if (state.sessionsError) {
    box.append(el("div", { class: "session-empty" }, [
      el("div", { class: "s-error", text: `会话列表读取失败：${state.sessionsError}` }),
      el("div", {
        class: "s-error-hint",
        text: "这个服务可能不是 tcad，或进程过旧没有 /sessions 接口。",
      }),
      el("button", {
        class: "mini",
        type: "button",
        text: "重试",
        onclick: () => loadSessions(),
      }),
    ]));
    return;
  }

  if (!state.sessions.length) {
    box.append(el("div", {
      class: "session-empty",
      text: "每一个想法，都从这里开始。新建会话，或直接描述你的设计。",
    }));
    return;
  }

  for (const s of state.sessions) {
    const active = s.thread_id === state.threadId;
    // A running turn lives in exactly one session, and while the list is what
    // you are looking at that is where you need to see it.
    const running = active && state.busy;
    const meta = el("div", { class: "s-meta" }, [
      el("span", { text: formatWhen(s.last_at) }),
      el("span", { text: `${s.messages} 条` }),
      // The one piece of session metadata that is about the *part* rather than
      // the conversation: whether this session ever produced geometry, and
      // whether that latest version was actually verified. `v5` alone reads as
      // progress; `v5 ✓` and `v5 ✗` are different states and must look different.
      el("span", {
        class: s.ir_version === null ? "" : "s-geom",
        text: s.ir_version === null
          ? "无几何"
          : `v${s.ir_version} ${s.verified === true ? "✓" : s.verified === false ? "✗" : "?"}`,
        title: s.ir_version === null
          ? "这个会话还没有产出几何"
          : s.verified === true
            ? "该版本通过了 Gate 且产物已发布"
            : "该版本尚未验证 —— 不要把它当成已完成",
      }),
      running ? el("span", { class: "s-running", text: "进行中" }) : null,
    ]);
    box.append(el("button", {
      class: `session-item${active ? " active" : ""}`,
      type: "button",
      title: `${s.thread_id}\nmodel ${s.model_id}`,
      onclick: () => switchSession(s.thread_id),
    }, [
      el("div", { class: "s-title", text: sessionTitle(s) }),
      meta,
    ]));
  }
}

async function loadSessions() {
  try {
    const { sessions } = await api("/sessions");
    state.sessions = sessions || [];
    state.sessionsLoaded = true;
    state.sessionsError = null;
  } catch (err) {
    state.sessionsLoaded = false;
    state.sessionsError = err.message;
  }
  renderSessions();
  return state.sessions;
}

function resetStream(showWelcome) {
  const box = $("stream");
  for (const child of [...box.children]) {
    if (child.id !== "welcome") child.remove();
  }
  const welcome = $("welcome");
  if (welcome) welcome.hidden = !showWelcome;
  clearLive();
}

function setSessionHeader(session) {
  $("threadLabel").textContent = session ? session.thread_id : "";
  const title = $("sessionTitle");
  if (title) title.textContent = session ? sessionTitle(session) : "设计对话";
}

function syncUrl() {
  const params = new URLSearchParams();
  if (state.threadId) params.set("thread", state.threadId);
  const qs = params.toString();
  history.replaceState(null, "", qs ? `?${qs}` : location.pathname);
}

/** Load one session: transcript, then everything derived from its model. */
async function switchSession(threadId, { force = false } = {}) {
  if (!threadId) return;
  if (compactMedia?.matches) toggleSidebar(true, { persist: false });
  if (threadId === state.threadId && !force) return;

  // A turn may be mid-flight. Its frames would otherwise keep arriving and
  // render into the session we are switching *to*. Aborting the fetch closes
  // the SSE stream, and the server treats a client that hung up as a reason to
  // cancel the turn — so this also stops the work rather than orphaning it.
  //
  // Nulling `state.turn` is what makes the abandoned request's callbacks stop
  // mattering; `abort()` alone only stops the *network*, and the generator's
  // catch/finally still run afterwards.
  if (state.busy && state.abort) {
    state.turn = null;
    state.abort.abort();
    state.busy = false;
    state.abort = null;
    $("sendBtn").disabled = false;
    $("stopBtn").hidden = true;
    setStatus("", "已切换会话");
  }

  const session = state.sessions.find((s) => s.thread_id === threadId) || null;

  // Bump first: in-flight loaders from the previous session must not write.
  state.sessionEpoch += 1;
  inspectorGeneration += 1;
  sourceIr = null;
  clearDisplayedStructure();
  referenceUI().reset();
  referenceUI().setBusy(false);
  cancelViewRequest();
  meshViewer?.clear({ resetCamera: true });
  clearViewImage();
  selectView("iso");
  $("viewPlaceholder").hidden = false;
  $("viewPlaceholder").textContent = "正在切换会话…";
  setViewMode(!!meshViewer?.available, "正在读取会话几何…");
  state.verdict = null;

  state.threadId = threadId;
  state.modelId = session ? session.model_id : null;
  state.version = session && session.ir_version !== null ? session.ir_version : null;
  // The previous session's verdict belongs to the previous session. Leaving it
  // on screen next to a different part would be actively misleading.
  state.lastGate = null;

  setSessionHeader(session);
  syncUrl();
  renderSessions();

  resetStream(true);
  const token = sessionToken();
  await loadMessages(threadId);
  if (stale(token)) return;

  refreshInspector();
  loadArtifacts();
  // Cache-first, NOT forced. The mesh cache is keyed by the exact model
  // snapshot; a session switch does not need to re-tessellate the same solid.
  loadView(false);
}

/** Replay a session's transcript.
 *
 * Tool calls and Gate reports are not persisted, so a resumed session shows the
 * conversation but not the process that produced it — the panel says so rather
 * than silently appearing to have lost it.
 */
async function loadMessages(threadId) {
  const token = sessionToken();
  let messages = [];
  try {
    ({ messages } = await api(`/threads/${encodeURIComponent(threadId)}/messages`));
    if (stale(token)) return;
  } catch (err) {
    if (stale(token)) return;
    pushNotice("bad", `读取会话记录失败：${err.message}`);
    return;
  }
  if (!messages.length) return;

  const welcome = $("welcome");
  if (welcome) welcome.hidden = true;
  for (const message of messages) {
    if (message.role === "user") {
      pushUser(message.content);
    } else if (message.role === "assistant") {
      assistantBody = null;             // each stored message is its own bubble
      pushAssistantText(message.content);
    }
  }
  append(el("div", {
    class: "muted small",
    text: `已回放 ${messages.length} 条历史消息。工具调用与 Gate 报告的完整过程只在当次流式输出中显示，` +
          "在此处继续对话即可接着做。",
  }));
}

async function newSession({ announce = true } = {}) {
  const created = await api("/sessions", { method: "POST", body: JSON.stringify({}) });
  await loadSessions();
  await switchSession(created.thread_id, { force: true });
  if (announce) pushNotice("info", "已新建会话。直接描述你要的零件即可。");
  workspace?.select("chat");
  $("input").focus();
  return created;
}

// Keep sample selection editable and ignore Enter while an IME confirms text.
function bindComposer() {
  const input = $("input");
  const resizeInput = () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 180) + "px";
  };
  $("composer").addEventListener("submit", (event) => {
    event.preventDefault();
    if (state.busy || input.disabled || !input.value.trim()) return;
    const text = input.value;
    input.value = "";
    input.style.height = "";
    send(text);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      $("composer").requestSubmit();
    }
  });
  input.addEventListener("input", resizeInput);
  for (const sample of document.querySelectorAll(".sample")) {
    sample.addEventListener("click", () => {
      if (state.busy || input.disabled) return;
      input.value = sample.dataset.text;
      resizeInput();
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    });
  }
}

function toggleSidebar(force, { persist = !compactMedia?.matches } = {}) {
  state.sidebarHidden = force === undefined ? !state.sidebarHidden : force;
  $("layout").classList.toggle("no-sidebar", state.sidebarHidden);
  $("sidebarToggle").setAttribute("aria-expanded", String(!state.sidebarHidden));
  if (compactMedia?.matches) {
    for (const id of ["chatPane", "modelPane", "inspectPane"]) $(id).inert = !state.sidebarHidden;
    $("sessionPane").setAttribute("aria-modal", String(!state.sidebarHidden));
    if (!state.sidebarHidden && force === undefined) $("newSessionBtn").focus();
  }
  if (persist) {
    try { localStorage.setItem("tcad.sidebarHidden", state.sidebarHidden ? "1" : "0"); } catch { /* private mode */ }
  }
}

function bindWorkspace() {
  compactMedia = window.matchMedia("(max-width: 980px)");
  workspace = new WorkspaceController({
    layout: $("layout"), media: compactMedia,
    tabs: { chat: $("workspaceChat"), model: $("workspaceModel"), inspect: $("workspaceInspect") },
    panels: { chat: $("chatPane"), model: $("modelPane"), inspect: $("inspectPane") },
    onSelect: (name, compact) => { if (compact) toggleSidebar(true, { persist: false }); },
    onModeChange: compact => {
      const pane = $("sessionPane");
      if (compact) {
        pane.setAttribute("role", "dialog"); pane.setAttribute("aria-label", "设计项目");
        $("sidebarToggle").setAttribute("aria-haspopup", "dialog");
        toggleSidebar(true, { persist: false });
      } else {
        pane.removeAttribute("role"); pane.removeAttribute("aria-modal"); pane.removeAttribute("aria-label");
        $("sidebarToggle").removeAttribute("aria-haspopup");
        for (const id of ["chatPane", "modelPane", "inspectPane"]) $(id).inert = false;
        let hidden = false;
        try { hidden = localStorage.getItem("tcad.sidebarHidden") === "1"; } catch { /* private mode */ }
        toggleSidebar(hidden, { persist: false });
      }
    },
  });
  const close = () => { toggleSidebar(true, { persist: false }); $("sidebarToggle").focus(); };
  $("sessionBackdrop").addEventListener("click", close);
  $("closeSessions").addEventListener("click", close);
  $("showModelFromStructure").addEventListener("click", () => workspace.select("model"));
  $("sessionPane").addEventListener("keydown", event => {
    if (!compactMedia.matches || event.key !== "Tab") return;
    const buttons = Array.from($("sessionPane").querySelectorAll("button"))
      .filter(button => !button.disabled && button.getClientRects().length);
    if (event.shiftKey && document.activeElement === buttons[0]) {
      event.preventDefault(); buttons.at(-1)?.focus();
    } else if (!event.shiftKey && document.activeElement === buttons.at(-1)) {
      event.preventDefault(); buttons[0]?.focus();
    }
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && compactMedia.matches && !state.sidebarHidden && $("settingsModal").hidden) {
      event.preventDefault(); close();
    }
  });
}

async function boot() {
  wire();
  setStatus("", "连接中…");
  try {
    const health = await api("/health");
    state.apiOk = true;
    state.dataDir = health.data_dir || null;
    state.selectionEnabled = health.selection_enabled;
    renderBudgetBadge(health.budget);
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

  // Which session to open: the one in the URL (so a reload or a shared link
  // lands where you left off), else the most recently used one.
  const wanted = new URLSearchParams(location.search).get("thread");
  await loadSessions();
  const target = state.sessions.find((s) => s.thread_id === wanted) || state.sessions[0];
  if (target) {
    await switchSession(target.thread_id, { force: true });
  } else {
    // No sessions at all. Deliberately not auto-creating one: an empty list is
    // an accurate description, and the first message creates the session then.
    setSessionHeader(null);
    resetStream(true);
    renderSessions();
  }
}

boot();
