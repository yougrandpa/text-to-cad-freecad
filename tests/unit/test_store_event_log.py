"""Tests for tcad.store.event_log — append-only JSONL."""

from __future__ import annotations

from pathlib import Path

from tcad.core.types import IrEvent
from tcad.store.event_log import EventLog

_PATCH_APPLIED = "patch_applied"


def _ev(model_id: str, kind: str = _PATCH_APPLIED, **kw) -> IrEvent:
    return IrEvent(model_id=model_id, kind=kind, **kw)


def test_append_assigns_monotonic_seq_and_persists(tmp_path: Path):
    log = EventLog(tmp_path)
    e1 = log.append("m", _ev("m"))
    e2 = log.append("m", _ev("m"))
    e3 = log.append("m", _ev("m"))
    assert [e1.seq, e2.seq, e3.seq] == [0, 1, 2]
    # file exists and is durable
    assert (tmp_path / "models" / "m" / "events.jsonl").exists()


def test_tail_returns_last_n(tmp_path: Path):
    log = EventLog(tmp_path)
    for i in range(5):
        log.append("m", _ev("m", ir_version_after=i))
    tail = log.tail("m", 2)
    assert [e.ir_version_after for e in tail] == [3, 4]


def test_read_all_in_order(tmp_path: Path):
    log = EventLog(tmp_path)
    for i in range(3):
        log.append("m", _ev("m", ir_version_after=i))
    all_ = log.read_all("m")
    assert [e.ir_version_after for e in all_] == [0, 1, 2]


def test_rebuild_yields_every_event(tmp_path: Path):
    log = EventLog(tmp_path)
    for i in range(4):
        log.append("m", _ev("m", ir_version_after=i))
    seen = [e.ir_version_after for e in log.rebuild("m")]
    assert seen == [0, 1, 2, 3]


def test_empty_model_returns_empty(tmp_path: Path):
    log = EventLog(tmp_path)
    assert log.read_all("nope") == []
