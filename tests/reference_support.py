"""Shared data and protocol harness; synthetic publications are not CAD evidence."""

import hashlib
from pathlib import Path
from types import SimpleNamespace

from tcad.artifacts.manifest import ArtifactSet
from tcad.config.schema import Config
from tcad.core.types import BuildStamp, GateReport, GeometryDigest, MeasuredHole
from tcad.core.wiring import StoreAdapter
from tcad.hooks.approval import JsonFileApprovalStore
from tcad.hooks.dispatcher import HookDispatcher
from tcad.ir.schema import IrDocument
from tcad.llm.client import LlmReply
from tcad.loop.engine import LoopConfig
from tcad.store.artifacts import ArtifactStore, write_build_stamp
from tests.unit.test_server import FakeGate, FakeHandle, FakeWorker

ROOT = Path(__file__).resolve().parents[1]
MODEL = "reference_plate"
THREAD = "th-reference_plate"
EDIT = "把选中孔的直径改成 8 毫米"


def plate():
    return IrDocument.model_validate_json((ROOT / "tests/fixtures/referenced_plate.json").read_bytes())


def publish(data_dir, store, *, attempt="protocol", measured=True):
    ir = store.load(MODEL)
    artifacts = ArtifactStore(data_dir)
    root = artifacts.staging_dir(MODEL, ir.version, attempt)
    root.mkdir(parents=True)
    source = ir.model_dump_json()
    sha = hashlib.sha256(source.encode()).hexdigest()
    (root / "ir.json").write_text(source)
    (root / "protocol.step").write_text("Synthetic geometry, only for protocol tests")
    digest = GeometryDigest(model_id=MODEL, ir_version=ir.version, holes=[MeasuredHole(
        index=0, diameter=6, radius=3, depth=6, center=[20,15,0], axis=[0,0,1], through=True)] if measured else [])
    artifacts.write_digest_at(root, digest)
    write_build_stamp(root, BuildStamp(model_id=MODEL, ir_version=ir.version, attempt_id=attempt,
                                     started_at=0, ir_sha256=sha))
    (root / "gate_report.json").write_text(GateReport(model_id=MODEL, ir_version=ir.version,
        attempt_id=attempt, ir_sha256=sha, passed=True).model_dump_json())
    artifacts.write_manifest(root, model_id=MODEL, version=ir.version, attempt_id=attempt,
                             ir_sha256=sha, status="verified")
    manifest = ArtifactSet.model_validate_json((root / "manifest.json").read_text())
    artifacts.publish(MODEL, ir.version, root)
    return manifest


class RecordingModel:
    def __init__(self):
        self.calls = 0
        self.tools = []
        self.messages = []

    async def chat(self, *, messages, tools=None, **kwargs):
        self.calls += 1
        self.tools.append([t["function"]["name"] for t in tools or []])
        self.messages.append(messages)
        return LlmReply(text="Inspection complete.")


def protocol_services(data_dir, model=None, ir=None):
    cfg = Config()
    cfg.storage.data_dir = str(data_dir)
    cfg.loop.max_steps_per_turn = 1
    store = StoreAdapter(data_dir)
    store.create(MODEL, ir or plate())
    return SimpleNamespace(config=cfg, store=store, worker=FakeWorker(), gate=FakeGate(True),
        renderer=None, context=None, hooks=HookDispatcher([], {}), llm=model or RecordingModel(),
        approvals=JsonFileApprovalStore(str(data_dir / "approvals.json"), ttl_s=60),
        loop_config=LoopConfig(data_dir=str(data_dir), workdir=str(ROOT)), _worker_handle=FakeHandle())


def edit_payload(ref, operation_id="edit-eight"):
    return {"model_id": MODEL, "thread_id": THREAD, "text": EDIT, "kind": "modify",
            "operation_id": operation_id, "selection_context": {"selection_refs": [ref]}}
