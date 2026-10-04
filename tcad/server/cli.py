"""Command-line front end — the same harness without an HTTP server.

In-process, so no uvicorn needed. Useful for a single-user desktop session and
for debugging a turn without a browser in the way.

    python -m tcad.server.cli new bracket --requirement "一个 60x40 的安装底板，厚 10mm"
    python -m tcad.server.cli chat bracket "做一个 60x40 的矩形底板，厚度 10mm"
    python -m tcad.server.cli repl bracket
    python -m tcad.server.cli approvals
    python -m tcad.server.cli approve <approval_id> --grant

Progress is streamed from the hook dispatcher (see tcad/server/app.py), so what
you see on the terminal is the real lifecycle the engine went through, not a
reconstruction.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from tcad.core.types import TurnKind, TurnState
from tcad.server.app import ChatRequest, HookEventTap, run_turn_request

_STATE_MARK = {
    TurnState.SUCCEEDED: "✓ SUCCEEDED (recorded constraints accepted; physical use still needs validation)",
    TurnState.DRAFT: "△ DRAFT (build passed; functional acceptance pending)",
    TurnState.EXHAUSTED: "✗ EXHAUSTED (budget ran out — this is NOT success)",
    TurnState.FAILED: "✗ FAILED",
    TurnState.ABORTED: "✗ ABORTED",
    TurnState.CONFIRMED: "✓ CONFIRMED by user",
    TurnState.AWAITING_APPROVAL: "⏸ AWAITING APPROVAL",
    TurnState.RUNNING: "… still running",
}


def _services(args):
    from tcad.config.loader import load_config, load_default_config
    from tcad.core.wiring import build_services

    cfg = (
        load_config(args.config) if getattr(args, "config", None) else load_default_config()
    )
    if getattr(args, "data_dir", None):
        cfg.storage.data_dir = args.data_dir
    svc = build_services(cfg)
    # The CLI is a REPL: without a conversation store the second request would
    # reach the model as though nothing had been said before it. Attaching one
    # here is what makes "change the height to 50" resolvable against turn 1.
    # Best-effort — a machine that cannot open SQLite can still run one turn.
    try:
        from tcad.store.session_db import SessionDB

        svc.session_db = SessionDB(svc.config.storage.sqlite_file(), check_same_thread=False)
    except Exception:  # noqa: BLE001
        pass
    return svc


def _print_events(tap: HookEventTap) -> None:
    for ev in tap.drain():
        detail = ev["reason"] or ""
        print(f"  · {ev['event']:<18} {ev['decision']:<6} {ev['hook']:<22} {detail}")


def cmd_new(args) -> int:
    from tcad.ir.schema import IrDocument, RequirementSpec

    svc = _services(args)
    try:
        ir = IrDocument(
            model_id=args.model_id,
            version=0,
            requirements=RequirementSpec(raw_text=args.requirement or ""),
        )
        created = svc.store.create(args.model_id, ir)
        print(f"created model {args.model_id!r} at version {int(created.version)}")
        return 0
    finally:
        svc._worker_handle.close()


def _remember(session_db, thread_id: str, model_id: str, role: str, content: str) -> None:
    """Persist one message for the REPL's conversation history.

    Best-effort: a turn must not fail because bookkeeping did.
    """
    if session_db is None:
        return
    try:
        if session_db.get_thread(thread_id) is None:
            session_db.create_thread(model_id, thread_id=thread_id)
        session_db.add_message(thread_id, role, content)
    except Exception:  # noqa: BLE001
        pass


def _one_turn(svc, args, text: str) -> int:
    # The tap is passed to the engine, not assigned onto the shared bundle: the
    # REPL may run concurrently with an HTTP turn against the same services.
    tap = HookEventTap(svc.hooks)
    thread_id = f"cli-{args.model_id}"
    req = ChatRequest(
        model_id=args.model_id,
        text=text,
        thread_id=thread_id,
        kind=TurnKind(args.kind),
        privileged_requested=bool(args.privileged),
    )
    session_db = getattr(svc, "session_db", None)
    _remember(session_db, thread_id, args.model_id, "user", text)

    def observe(kind: str, data: dict) -> None:
        if kind == "model" and (data.get("text") or "").strip():
            _remember(session_db, thread_id, args.model_id, "assistant", data["text"])

    print(f"→ {text}")
    result = asyncio.run(run_turn_request(svc, req, observer=observe, hooks=tap))
    _print_events(tap)

    print(f"\n{_STATE_MARK.get(result.state, result.state.value)}")
    print(f"  steps={result.steps}  tokens_in={result.tokens_in}  tokens_out={result.tokens_out}")
    if result.error:
        print(f"  error: {result.error}")
    if result.completion_review:
        print(f"  review: {result.completion_review['summary']}")
        for item in result.completion_review['remaining_work']:
            print(f"  pending: {item}")
        print(f"  scope: {result.completion_review['note']}")
    if result.gate_report is not None:
        rep = result.gate_report
        print(f"  gate: passed={rep.passed}  "
              f"blocking={rep.blocking_failures or '[]'}  "
              f"advisory={len(rep.advisory_findings)}  skipped={rep.skipped_checks or '[]'}")
    return 0 if result.state is TurnState.SUCCEEDED else 1


def cmd_chat(args) -> int:
    svc = _services(args)
    try:
        return _one_turn(svc, args, args.text)
    finally:
        svc._worker_handle.close()


def cmd_repl(args) -> int:
    svc = _services(args)
    try:
        print(f"model {args.model_id!r} — type a request, or 'quit'.")
        while True:
            try:
                text = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if text in {"quit", "exit", ":q"}:
                return 0
            if not text:
                continue
            try:
                _one_turn(svc, args, text)
            except Exception as exc:  # noqa: BLE001 — a bad turn must not end the session
                print(f"turn error: {type(exc).__name__}: {exc}")
    finally:
        svc._worker_handle.close()


def cmd_approvals(args) -> int:
    svc = _services(args)
    try:
        store = getattr(svc, "approvals", None)
        records = store._load() if store and hasattr(store, "_load") else []
        pending = [
            r for r in records
            if not getattr(r, "granted", False) and getattr(r, "resolved_at", None) is None
        ]
        if not pending:
            print("no pending approvals")
            return 0
        for r in pending:
            where = f"  session={r.thread_id} turn={r.turn_id}" if r.thread_id else "  (unscoped)"
            print(f"  {r.id}  tool={r.tool_name}  expires={r.expires_at.isoformat()}{where}")
            # What is actually being authorised. Without this the operator is
            # asked to grant a tool name and an opaque hash.
            if getattr(r, "args_summary", None):
                for line in str(r.args_summary).splitlines() or [""]:
                    print(f"      | {line}")
            else:
                print("      | (no payload recorded)")
        return 0
    finally:
        svc._worker_handle.close()


def cmd_approve(args) -> int:
    svc = _services(args)
    try:
        store = getattr(svc, "approvals", None)
        if store is None:
            print("no approval store configured", file=sys.stderr)
            return 2
        rec = store.resolve(args.approval_id, granted=args.grant)
        if rec is None:
            print(f"no such approval: {args.approval_id}", file=sys.stderr)
            return 2
        print(f"{'granted' if rec.granted else 'denied'}: {rec.id} ({rec.tool_name})")
        return 0
    finally:
        svc._worker_handle.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tcad", description="chat-driven CAD harness")
    p.add_argument("--config", help="path to a YAML config (default: configs/default.yaml)")
    p.add_argument("--data-dir", help="override storage.data_dir")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_new = sub.add_parser("new", help="create an empty model")
    p_new.add_argument("model_id")
    p_new.add_argument("--requirement", default="", help="the user's requirement, verbatim")
    p_new.set_defaults(func=cmd_new)

    p_chat = sub.add_parser("chat", help="run a single turn")
    p_chat.add_argument("model_id")
    p_chat.add_argument("text")
    p_chat.add_argument("--kind", default="create", choices=[k.value for k in TurnKind])
    p_chat.add_argument("--privileged", action="store_true",
                        help="request the privileged tier (still needs config + approval)")
    p_chat.set_defaults(func=cmd_chat)

    p_repl = sub.add_parser("repl", help="interactive session")
    p_repl.add_argument("model_id")
    p_repl.add_argument("--kind", default="modify", choices=[k.value for k in TurnKind])
    p_repl.add_argument("--privileged", action="store_true")
    p_repl.set_defaults(func=cmd_repl)

    p_appr = sub.add_parser("approvals", help="list pending approvals")
    p_appr.set_defaults(func=cmd_approvals)

    p_ok = sub.add_parser("approve", help="grant or deny an approval")
    p_ok.add_argument("approval_id")
    p_ok.add_argument("--grant", action="store_true", help="grant (default: deny)")
    p_ok.set_defaults(func=cmd_approve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
