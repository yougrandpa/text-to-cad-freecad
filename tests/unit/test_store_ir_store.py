"""Tests for tcad.store.ir_store — versioned IR + crash recovery."""

from __future__ import annotations

import pytest
from pathlib import Path

from tcad.core.types import IrEvent
from tcad.ir.patch import apply_patch
from tcad.ir.schema import IrPatch, IrPatchOp
from tcad.store.ir_store import IrStore

from .conftest import make_minimal_ir


def _add_hole_patch(base_version: int, name: str, diameter: int) -> IrPatch:
    return IrPatch(
        base_version=base_version,
        summary=f"add {name}",
        ops=[IrPatchOp(
            op="add_feature",
            payload={"op": "hole", "profile_sketch": "sk_outline",
                     "params": {"diameter": diameter}, "name": name},
            reason="r")],
    )


def test_create_then_load_round_trip(tmp_path: Path):
    store = IrStore(tmp_path)
    ir0 = make_minimal_ir(version=0)
    stored = store.create("m", ir0)
    assert stored.version == 0
    # create normalises model_id; compare against the stored (normalised) doc
    assert store.load("m").model_dump() == stored.model_dump()


def test_apply_patch_increments_version_and_lists(tmp_path: Path):
    store = IrStore(tmp_path)
    store.create("m", make_minimal_ir(version=0))
    new_ir, ev = store.apply_patch("m", _add_hole_patch(0, "hole1", 3))
    assert new_ir.version == 1
    assert store.load("m").version == 1
    assert store.list_versions("m") == [0, 1]
    assert ev.ir_version_after == 1


def test_rollback_to_older_version(tmp_path: Path):
    store = IrStore(tmp_path)
    store.create("m", make_minimal_ir(version=0))
    _, e1 = store.apply_patch("m", _add_hole_patch(0, "hole1", 3))
    _, e2 = store.apply_patch("m", _add_hole_patch(1, "hole2", 4))
    id1 = e1.payload["created_ids"][0]
    id2 = e2.payload["created_ids"][0]
    assert store.load("m").version == 2
    # rollback is an O(1) file read, not a replay
    older = store.rollback("m", 1)
    assert older.version == 1
    assert older.find_feature(id2) is None
    assert older.find_feature(id1) is not None


def test_snapshot_before_commit_returns_reversible_point(tmp_path: Path):
    store = IrStore(tmp_path)
    store.create("m", make_minimal_ir(version=0))
    assert store.snapshot_before_commit("m") == 0
    store.apply_patch("m", _add_hole_patch(0, "hole1", 3))
    assert store.snapshot_before_commit("m") == 1


def test_event_before_snapshot_crash_recovery(tmp_path: Path):
    """The core guarantee of this layer.

    Simulate a crash *between* the event append (write-ahead) and the snapshot
    write. The event is on disk; the v2 snapshot is not. ``rebuild_from_events``
    must reconstruct exactly the intended document.
    """
    store = IrStore(tmp_path)
    store.create("m", make_minimal_ir(version=0))

    # patch 1: fully committed (event + snapshot)
    store.apply_patch("m", _add_hole_patch(0, "hole1", 3))

    # compute the *intended* result of patch 2 without touching the store
    cur = store.load("m")  # v1
    p2 = _add_hole_patch(1, "hole2", 4)
    intended = apply_patch(cur, p2).ir

    # ---- simulate crash: append the patch_applied event, but DO NOT write v2 ----
    crash_event = IrEvent(
        model_id="m",
        kind="patch_applied",
        ir_version_before=1,
        ir_version_after=2,
        payload={
            "action": "apply",
            "patch": p2.model_dump(),
            "summary": p2.summary,
            "changes": ["add_feature hole2"],
            "created_ids": [],
            "renamed_ids": [],
        },
    )
    store._log.append("m", crash_event)

    # the snapshot for v2 was never written
    assert not store._snapshot_path("m", 2).exists()
    with pytest.raises(FileNotFoundError):
        store.load("m", 2)

    # recovery from the event log yields exactly the intended document
    rebuilt = store.rebuild_from_events("m")
    assert rebuilt.version == 2
    intended_last = intended.all_features()[-1].id
    assert rebuilt.find_feature(intended_last) is not None
    assert rebuilt.model_dump() == intended.model_dump()


def test_rebuild_matches_loaded_when_no_crash(tmp_path: Path):
    store = IrStore(tmp_path)
    store.create("m", make_minimal_ir(version=0))
    store.apply_patch("m", _add_hole_patch(0, "hole1", 3))
    store.apply_patch("m", _add_hole_patch(1, "hole2", 4))
    rebuilt = store.rebuild_from_events("m")
    assert rebuilt.model_dump() == store.load("m").model_dump()


def test_load_missing_raises(tmp_path: Path):
    store = IrStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.load("m")
