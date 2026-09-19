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


# ══════════════════════════════════════════════════════════════════════════
# the session list
#
# What the sidebar reads. Two properties matter and neither is about fields:
# an empty *new* session has to sort to the top (otherwise clicking "new" looks
# like it did nothing), and a session that has just been used has to bubble up.
# ══════════════════════════════════════════════════════════════════════════


def test_list_threads_orders_by_last_activity(tmp_path: Path):
    db = SessionDB(tmp_path / "tcad.sqlite3")
    older = db.create_thread("m-old")
    db.add_message(older.thread_id, "user", "第一个")

    newer = db.create_thread("m-new")
    db.add_message(newer.thread_id, "user", "第二个")

    order = [t["thread_id"] for t in db.list_threads()]
    assert order == [newer.thread_id, older.thread_id]

    # Touching the older one moves it back to the top.
    db.add_message(older.thread_id, "user", "继续")
    assert [t["thread_id"] for t in db.list_threads()][0] == older.thread_id


def test_a_brand_new_session_sorts_first_despite_having_no_messages(tmp_path: Path):
    """It falls back to the thread's own creation time.

    Keying the sort on the newest message alone would place a fresh session
    below every conversation that has ever been used — so clicking "new" would
    appear to do nothing at all.
    """
    db = SessionDB(tmp_path / "tcad.sqlite3")
    used = db.create_thread("m-used")
    db.add_message(used.thread_id, "user", "老的")

    fresh = db.create_thread("m-fresh")
    assert [t["thread_id"] for t in db.list_threads()] == [fresh.thread_id, used.thread_id]


def test_list_threads_carries_a_title_and_a_preview(tmp_path: Path):
    db = SessionDB(tmp_path / "tcad.sqlite3")
    thr = db.create_thread("m1")
    db.add_message(thr.thread_id, "user", "一个 80x50 的底板")
    db.add_message(thr.thread_id, "assistant", "好的，我来建模")
    db.add_message(thr.thread_id, "user", "再加个槽")

    row = db.list_threads()[0]
    assert row["title"] == "一个 80x50 的底板", "标题应取首条 user 消息"
    assert row["last_message"] == "再加个槽"
    assert row["messages"] == 3
    assert row["last_at"] >= row["created_at"]


def test_list_threads_still_filters_by_model(tmp_path: Path):
    db = SessionDB(tmp_path / "tcad.sqlite3")
    db.create_thread("m-a")
    db.create_thread("m-b")
    assert len(db.list_threads("m-a")) == 1
    assert len(db.list_threads()) == 2


# ══════════════════════════════════════════════════════════════════════════
# the session list
#
# What the sidebar reads. Two properties matter and neither is about fields:
# an empty *new* session has to sort to the top (otherwise clicking "new" looks
# like it did nothing), and a session that has just been used has to bubble up.
# ══════════════════════════════════════════════════════════════════════════


def test_list_threads_orders_by_last_activity(tmp_path: Path):
    db = SessionDB(tmp_path / "tcad.sqlite3")
    older = db.create_thread("m-old")
    db.add_message(older.thread_id, "user", "第一个")

    newer = db.create_thread("m-new")
    db.add_message(newer.thread_id, "user", "第二个")

    order = [t["thread_id"] for t in db.list_threads()]
    assert order == [newer.thread_id, older.thread_id]

    # Touching the older one moves it back to the top.
    db.add_message(older.thread_id, "user", "继续")
    assert [t["thread_id"] for t in db.list_threads()][0] == older.thread_id


def test_a_brand_new_session_sorts_first_despite_having_no_messages(tmp_path: Path):
    """It falls back to the thread's own creation time.

    Keying the sort on the newest message alone would place a fresh session
    below every conversation that has ever been used — so clicking "new" would
    appear to do nothing at all.
    """
    db = SessionDB(tmp_path / "tcad.sqlite3")
    used = db.create_thread("m-used")
    db.add_message(used.thread_id, "user", "老的")

    fresh = db.create_thread("m-fresh")
    assert [t["thread_id"] for t in db.list_threads()] == [fresh.thread_id, used.thread_id]


def test_list_threads_carries_a_title_and_a_preview(tmp_path: Path):
    db = SessionDB(tmp_path / "tcad.sqlite3")
    thr = db.create_thread("m1")
    db.add_message(thr.thread_id, "user", "一个 80x50 的底板")
    db.add_message(thr.thread_id, "assistant", "好的，我来建模")
    db.add_message(thr.thread_id, "user", "再加个槽")

    row = db.list_threads()[0]
    assert row["title"] == "一个 80x50 的底板", "标题应取首条 user 消息"
    assert row["last_message"] == "再加个槽"
    assert row["messages"] == 3
    assert row["last_at"] >= row["created_at"]


def test_list_threads_still_filters_by_model(tmp_path: Path):
    db = SessionDB(tmp_path / "tcad.sqlite3")
    db.create_thread("m-a")
    db.create_thread("m-b")
    assert len(db.list_threads("m-a")) == 1
    assert len(db.list_threads()) == 2
