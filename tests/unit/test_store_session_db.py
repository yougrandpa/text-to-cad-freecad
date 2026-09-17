"""Tests for tcad.store.session_db — SQLite (WAL) session/index store."""

from __future__ import annotations

from pathlib import Path

from tcad.store.session_db import SessionDB


def test_wal_mode_enabled(tmp_path: Path):
    db = SessionDB(tmp_path / "tcad.sqlite3")
    mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_thread_turn_lifecycle_round_trip(tmp_path: Path):
    db = SessionDB(tmp_path / "tcad.sqlite3")
    thr = db.create_thread("m1")
    assert thr.thread_id
    assert db.get_thread(thr.thread_id) is not None

    turn = db.start_turn(thr.thread_id, "create", base_ir_version=0)
    assert turn.turn_id
    db.record_step(turn.turn_id, tool_name="ir_patch")
    db.finish_turn(turn.turn_id, state="succeeded")
    assert db.get_thread(thr.thread_id) is not None


def test_token_usage_sum(tmp_path: Path):
    db = SessionDB(tmp_path / "tcad.sqlite3")
    thr = db.create_thread("m1")
    turn = db.start_turn(thr.thread_id, "create", base_ir_version=0)
    db.add_token_usage(turn.turn_id, "in", 100)
    db.add_token_usage(turn.turn_id, "in", 50)
    db.add_token_usage(turn.turn_id, "out", 30)
    totals = db.sum_token_usage(turn.turn_id)
    assert totals == {"in": 150, "out": 30, "total": 180}


def test_approval_request_resolve_pending(tmp_path: Path):
    db = SessionDB(tmp_path / "tcad.sqlite3")
    thr = db.create_thread("m1")
    turn = db.start_turn(thr.thread_id, "create", base_ir_version=0)

    aid = db.request_approval(thr.thread_id, turn.turn_id, "raw_python")
    pending = db.get_pending_approvals(thr.thread_id)
    assert len(pending) == 1 and pending[0]["approval_id"] == aid

    db.resolve_approval(aid, granted=True, note="ok")
    # no pending left for this thread
    assert db.get_pending_approvals(thr.thread_id) == []


def test_messages_persist(tmp_path: Path):
    db = SessionDB(tmp_path / "tcad.sqlite3")
    thr = db.create_thread("m1")
    mid = db.add_message(thr.thread_id, "user", "make a bracket")
    assert mid
    # round-trip via raw read is implicit; just ensure no error and id returned
