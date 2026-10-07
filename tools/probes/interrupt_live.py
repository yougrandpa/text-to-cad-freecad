#!/usr/bin/env python3
"""Drive a live server: start a turn, stop it in the middle, say what happened.

This is the *behavioural* half of the interruption feature — the unit tests pin
the mechanism (the stop predicate, the cancellation, the race, the verdict
frame), and this pins the thing a person actually cares about: press stop while
the model is generating, and the turn ends promptly, is reported as stopped, and
leaves the conversation usable.

It needs a *slow* model, or there is no "middle of the turn" to interrupt. The
scripted stub with a delay is enough, and costs no tokens:

    .venv/bin/python tools/stub_llm.py --script tools/sessions/demo_bracket.json \\
        --port 8766 --delay 1.5
    .venv/bin/python tools/serve.py --data-dir output/local/tcad_interrupt --port 8767 \\
        --base-url http://127.0.0.1:8766/v1 --model stub-scripted
    .venv/bin/python tools/probes/interrupt_live.py --port 8767

Do not point it at a server someone is using: it creates a session and runs
turns in it. Stdlib only, so it runs anywhere the server does.
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import threading
import time
import uuid

RUNNING = "running"


# ══════════════════════════════════════════════════════════════════════════
# tiny HTTP helpers (stdlib: this must run wherever the server runs)
# ══════════════════════════════════════════════════════════════════════════


def request(host: str, port: int, method: str, path: str, body: dict | None = None):
    conn = http.client.HTTPConnection(host, port, timeout=300)
    payload = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if payload else {}
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read().decode()
    conn.close()
    if resp.status >= 400:
        raise RuntimeError(f"{method} {path} -> {resp.status} {raw[:400]}")
    return json.loads(raw) if raw else {}


def stream_chat(host: str, port: int, payload: dict, on_frame):
    """POST /chat and hand each SSE frame to *on_frame* as it arrives.

    Returns when the stream ends. The frame callback runs on this thread, so it
    is the right place to decide "the turn is now far enough along to stop it".
    """
    conn = http.client.HTTPConnection(host, port, timeout=600)
    conn.request(
        "POST", "/chat", body=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    if resp.status >= 400:
        raise RuntimeError(f"/chat -> {resp.status} {resp.read().decode()[:400]}")

    event, data = "message", "{}"
    started = time.monotonic()
    while True:
        line = resp.readline()
        if not line:
            break
        text = line.decode("utf-8").rstrip("\n")
        if text.startswith("event: "):
            event = text[7:].strip()
        elif text.startswith("data: "):
            data = text[6:]
        elif text == "":
            on_frame(event, json.loads(data), time.monotonic() - started)
            event, data = "message", "{}"
    conn.close()


def describe(event: str, data: dict) -> str:
    if event == "start":
        return f"request_id={data.get('request_id')} thread={data.get('thread_id')}"
    if event == "agent":
        if data.get("kind") == "model":
            return f"模型第 {data.get('step')} 步 · {len(data.get('tool_calls') or [])} 个工具调用"
        if data.get("kind") == "tool":
            return f"工具 {data.get('name')} ok={data.get('ok')}"
        if data.get("kind") == "turn_end":
            return f"turn_end state={data.get('state')}"
    if event == "progress":
        return f"{data.get('event')} -> {data.get('decision')} ({data.get('hook')})"
    if event == "result":
        return f"state={data.get('state')} steps={data.get('steps')} error={data.get('error')}"
    if event == "error":
        return f"{data.get('type')}: {data.get('message')}"
    return ""


# ══════════════════════════════════════════════════════════════════════════


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--text", default="一个 80x50 的安装底板，厚度 8mm，中间开一个 40x20 的通槽")
    parser.add_argument("--stop-after-agent", type=int, default=4,
                        help="收到第几个 agent 帧后打断。模型每步一个 model 帧 + 每个工具一个 tool 帧，"
                             "所以默认的 4 = 第一步的工具刚跑完、第二步的模型调用正在飞 —— "
                             "那正是必须靠取消（而不只是步骤边界）才停得下来的时刻")
    args = parser.parse_args()

    session = request(args.host, args.port, "POST", "/sessions", {})
    thread_id, model_id = session["thread_id"], session["model_id"]
    print(f"会话      {thread_id}  model {model_id}")

    frames: list[tuple[float, str, dict]] = []
    stop_result: dict = {}
    interrupt_at: list[float] = []
    agents = 0
    stop_sent = threading.Event()
    # Frame times are relative to the start of the stream; the interrupt thread
    # has to report on the same clock or the two cannot be subtracted.
    stream_clock: list[float] = []

    def on_frame(event: str, data: dict, t: float) -> None:
        nonlocal agents
        frames.append((t, event, data))
        print(f"{t:7.2f}s  {event:9s} {describe(event, data)}")
        if event == "agent":
            agents += 1
            if agents == args.stop_after_agent and not stop_sent.is_set():
                stop_sent.set()
                thread = threading.Thread(target=interrupt, daemon=True)
                stopper.append(thread)
                thread.start()

    # A fresh id per run, the way the UI mints one per turn. Reusing one would
    # be a client bug: a stop is aimed at a specific turn, and the server is
    # entitled to refuse a second turn under a live id.
    stamp = uuid.uuid4().hex[:8]
    request_id = f"probe-{stamp}-1"
    stopper: list[threading.Thread] = []

    def interrupt() -> None:
        time.sleep(0.3)   # let the next model call actually be in flight
        t0 = time.monotonic() - stream_clock[0]
        try:
            stop_result.update(
                request(args.host, args.port, "POST", "/chat/interrupt",
                        {"request_id": request_id})
            )
        except Exception as exc:  # noqa: BLE001
            stop_result.update({"error": str(exc)})
        interrupt_at.append(t0)

    print(f"\nPOST /chat  request_id={request_id}  （第 {args.stop_after_agent} 个 agent 帧后打断）\n")
    stream_clock.append(time.monotonic())
    stream_chat(
        args.host, args.port,
        {"model_id": model_id, "text": args.text, "thread_id": thread_id,
         "request_id": request_id},
        on_frame,
    )
    # The stream can end while the interrupt thread is still in flight. Wait for
    # it, or the report would say "the stop did nothing" when in truth it was
    # never observed.
    for thread in stopper:
        thread.join(timeout=5)

    results = [(t, d) for (t, e, d) in frames if e == "result"]
    if not results:
        print("\n✗ 流结束了却没有 result 帧 —— 被打断的回合不能让界面无从显示结论")
        return 1
    t_result, result = results[-1]

    after = [(t, e) for (t, e, _) in frames if t > t_result]
    prompt = ""
    if interrupt_at:
        prompt = f"，打断后 {t_result - interrupt_at[0]:.2f}s 结束"

    print(f"\n打断返回  {stop_result}")
    print(f"结论      state={result['state']} steps={result['steps']} "
          f"tokens_in={result['tokens_in']} tokens_out={result['tokens_out']}{prompt}")
    print(f"错误文案  {result['error']}")

    failures = []
    if not stop_result:
        failures.append("打断请求没有在流结束前完成 —— 这个回合太短，给替身加 --delay 再试")
    elif stop_result.get("stage") != RUNNING:
        failures.append(f"打断没有落到运行中的回合上：{stop_result}")
    if result["state"] != "aborted":
        failures.append(f"被打断的回合结论是 {result['state']}，不是 aborted")
    if "stopped by the user" not in (result["error"] or ""):
        failures.append("结论没有说明「是谁停的、以及此后没有被验证」")
    if after:
        failures.append(f"result 之后还有 {len(after)} 帧：{after}")
    if interrupt_at and t_result - interrupt_at[0] > 5.0:
        failures.append(f"停止用了 {t_result - interrupt_at[0]:.1f}s，说明在飞的模型调用没有被取消")

    # The conversation has to keep working: an interruption that poisons the
    # session would be a worse bug than the one this feature fixes.
    #
    # The scripted stub has a finite script, so this second turn is *expected*
    # to end badly once the replay runs out — what is being checked here is that
    # it still runs and still produces a verdict, not what the verdict says.
    print("\n紧接着再跑一个回合（会话必须仍然可用）…")
    later: list[tuple[float, str, dict]] = []
    stream_chat(
        args.host, args.port,
        {"model_id": model_id, "text": "继续", "thread_id": thread_id,
         "request_id": f"probe-{stamp}-2"},
        lambda e, d, t: later.append((t, e, d)),
    )
    second = [d for (_, e, d) in later if e == "result"]
    if not second:
        failures.append("打断之后的下一个回合没有结论")
    else:
        print(f"第二回合  state={second[-1]['state']} steps={second[-1]['steps']} "
              f"error={second[-1]['error']}")
        print("          （替身脚本是有限的，播完后必然 FAILED —— 这里要的是「它还能跑完并给出结论」）")

    history = request(args.host, args.port, "GET", f"/threads/{thread_id}/messages")
    print(f"会话记录  {len(history['messages'])} 条（打断前已生成的内容仍然在）")
    if not any(args.text[:12] in (m.get("content") or "") for m in history["messages"]):
        failures.append("会话记录里找不到被打断那次的用户消息")

    if failures:
        print("\n✗ " + "\n✗ ".join(failures))
        return 1
    print("\n✓ 打断：结论明确、够快、且会话可以继续")
    return 0


if __name__ == "__main__":
    sys.exit(main())
