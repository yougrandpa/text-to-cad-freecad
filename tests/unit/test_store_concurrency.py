"""Concurrent writes to one model must be serialised (task book §5-D).

The patch layer's optimistic concurrency is a comparison between
``patch.base_version`` and the version of the document the *store* just loaded.
That check is only worth something if the load and the write are one critical
section — otherwise two writers can both read v0, both pass a check that is
against different copies of the same stale document, and both snapshot v1. The
caller that lost its patch was still told it succeeded.

These tests are deterministic and need no FreeCAD.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from tcad.ir.patch import PatchError
from tcad.ir.schema import (
    BodySpec,
    FeatureSpec,
    IrDocument,
    IrPatch,
    IrPatchOp,
)
from tcad.store.ir_store import IrStore


def _seed(store: IrStore, model_id: str = "m") -> IrDocument:
    return store.create(model_id, IrDocument(model_id=model_id, version=0))


def _add_feature(fid: str, name: str) -> IrPatch:
    return IrPatch(
        base_version=0,
        ops=[IrPatchOp(
            op="add_feature",
            payload={"id": fid, "name": name, "op": "pad", "params": {"length": 1.0}},
            reason=f"add {name}",
        )],
        summary=f"add {name}",
    )


def _apply_events(store: IrStore, model_id: str = "m") -> list[dict]:
    path = store.data_dir / "models" / model_id / "events.jsonl"
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if (rec.get("payload") or {}).get("action") == "apply":
            out.append(rec)
    return out


class _OverlapSpy:
    """Instruments ``IrStore.load`` to measure concurrent entry.

    Holds its own lock only for the counter, and sleeps *inside* the instrumented
    call so two racing writers reliably overlap unless something serialises them.
    """

    def __init__(self, sleep_s: float = 0.05):
        self.sleep_s = sleep_s
        self.inside = 0
        self.max_inside = 0
        self._guard = threading.Lock()

    def install(self, monkeypatch):
        real_load = IrStore.load

        def spy(store_self, model_id, version=None):
            with self._guard:
                self.inside += 1
                self.max_inside = max(self.max_inside, self.inside)
            try:
                time.sleep(self.sleep_s)
                return real_load(store_self, model_id, version)
            finally:
                with self._guard:
                    self.inside -= 1

        monkeypatch.setattr(IrStore, "load", spy)


def test_two_writers_cannot_both_win_the_same_version(tmp_path, monkeypatch):
    store = IrStore(tmp_path)
    _seed(store)
    spy = _OverlapSpy()
    spy.install(monkeypatch)

    start = threading.Barrier(2)
    outcomes: list[tuple[str, object]] = []
    guard = threading.Lock()

    def writer(fid: str):
        start.wait()
        try:
            doc, _event = store.apply_patch("m", _add_feature(fid, fid))
            with guard:
                outcomes.append(("ok", doc.version))
        except PatchError as exc:
            with guard:
                outcomes.append(("rejected", exc.error.message))
        except Exception as exc:  # noqa: BLE001
            with guard:
                outcomes.append(("error", f"{type(exc).__name__}: {exc}"))

    threads = [threading.Thread(target=writer, args=(f"f{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads), "a writer deadlocked"

    ok = [o for o in outcomes if o[0] == "ok"]
    rejected = [o for o in outcomes if o[0] == "rejected"]
    errors = [o for o in outcomes if o[0] == "error"]

    assert not errors, errors
    assert len(ok) == 1, f"both writers believed they succeeded: {outcomes}"
    assert len(rejected) == 1, f"the loser was not told it lost: {outcomes}"
    assert "stale base_version" in str(rejected[0][1]), rejected

    # One patch, one snapshot, one event: nothing was silently lost.
    assert ok[0][1] == 1
    assert store.load("m").version == 1
    events = _apply_events(store)
    assert len(events) == 1, f"expected exactly one applied patch, got {len(events)}"


def test_the_load_and_the_write_are_one_critical_section(tmp_path, monkeypatch):
    """The serialisation proof, independent of who happened to win."""
    store = IrStore(tmp_path)
    _seed(store)
    spy = _OverlapSpy(sleep_s=0.05)
    spy.install(monkeypatch)

    start = threading.Barrier(2)

    def writer(fid: str):
        start.wait()
        try:
            store.apply_patch("m", _add_feature(fid, fid))
        except PatchError:
            pass  # losing is fine; overlapping is not

    threads = [threading.Thread(target=writer, args=(f"f{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert spy.max_inside == 1, (
        "two writers were inside the load/apply/write section at once — "
        "the base_version check is a TOCTOU race again")


def test_sequential_patches_still_work(tmp_path):
    """The lock must not have broken the ordinary path."""
    store = IrStore(tmp_path)
    _seed(store)
    store.apply_patch("m", _add_feature("f0", "f0"))
    second = IrPatch(
        base_version=1,
        ops=[IrPatchOp(op="add_feature",
                       payload={"id": "f1", "name": "f1", "op": "pad",
                                "params": {"length": 2.0}},
                       reason="second")],
        summary="second",
    )
    doc, _ = store.apply_patch("m", second)
    assert doc.version == 2
    assert [f.id for f in doc.all_features()] == ["f0", "f1"]
    assert len(_apply_events(store)) == 2
