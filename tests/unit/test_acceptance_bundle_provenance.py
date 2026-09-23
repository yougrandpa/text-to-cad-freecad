"""The downloadable bundle must say which code built it (task §8 「版本清单」).

``review/acceptance/`` is the evidence a reviewer can actually open: the models,
STEP files, FCStd documents, previews, IR, requirement contracts and manifest.
The manifest already binds every *file* by sha256 — but until this round nothing
in it answered "which code produced these files?", and ``git`` cannot answer
either (this workspace's git is refused by an unaccepted Xcode licence; the
report's header records that). A bundle you cannot date is a bundle you have to
take on faith, which is exactly what the task book forbids.

So the builder now stamps the manifest with a digest over every ``*.py`` the
build depends on (``tcad/`` + ``tools/`` + ``tests/``) and ships
``--check`` to compare that stamp against the working tree.

These tests are hermetic: they run the digest and the check against a temporary
tree via a patched ``REPO_ROOT``, so they never read or mutate the real bundle.
The real bundle's state is a command, not an assertion:

    .venv/bin/python tools/build_acceptance_artifacts.py --check
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _builder():
    """Load tools/build_acceptance_artifacts.py without running main().

    The script is not a module in a package and imports the contract tests' IR
    builders, so it is loaded by path — the same file the operator runs.
    """
    path = REPO_ROOT / "tools" / "build_acceptance_artifacts.py"
    spec = importlib.util.spec_from_file_location("_acceptance_builder", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def fake_tree(tmp_path, monkeypatch):
    """A miniature repo: two hashed roots, one file each, plus a cache file."""
    for rel in ("tcad/__init__.py", "tcad/compiler.py", "tools/build.py",
                "tests/unit/test_x.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(f"# {rel}\n", encoding="utf-8")
    (tmp_path / "tcad" / "__pycache__").mkdir()
    (tmp_path / "tcad" / "__pycache__" / "compiler.cpython-313.pyc").write_bytes(b"\x00")
    builder = _builder()
    monkeypatch.setattr(builder, "REPO_ROOT", tmp_path)
    return builder, tmp_path


# ══════════════════════════════════════════════════════════════════════════
# the digest: what it covers, and what it must react to
# ══════════════════════════════════════════════════════════════════════════


def test_the_digest_covers_the_product_the_producers_and_the_builders(fake_tree):
    builder, _ = fake_tree
    digest = builder.source_digest()
    assert digest["roots"] == ["tcad", "tools", "tests"]
    assert digest["files"] == 4, "every *.py under the three roots, nothing else"
    assert len(digest["sha256"]) == 64 and int(digest["sha256"], 16) >= 0


def test_a_cache_file_does_not_change_the_digest(fake_tree):
    """__pycache__ holds no source; deleting it must not invalidate a bundle."""
    builder, _ = fake_tree
    before = builder.source_digest()["sha256"]
    (builder.REPO_ROOT / "tcad" / "__pycache__" / "extra.py").write_text("", encoding="utf-8")
    assert builder.source_digest()["sha256"] == before


def test_editing_one_line_of_one_file_changes_the_digest(fake_tree):
    builder, tree = fake_tree
    before = builder.source_digest()["sha256"]
    (tree / "tcad" / "compiler.py").write_text("# one more line\n", encoding="utf-8")
    after = builder.source_digest()["sha256"]
    assert after != before, "a bundle stamped before this edit must not look current"


def test_the_digest_is_stable_across_calls(fake_tree):
    builder, _ = fake_tree
    assert builder.source_digest() == builder.source_digest()


# ══════════════════════════════════════════════════════════════════════════
# --check: the answer a reader gets instead of an assumption
# ══════════════════════════════════════════════════════════════════════════


def test_a_matching_manifest_is_accepted(fake_tree):
    builder, tree = fake_tree
    manifest = tree / "manifest.json"
    manifest.write_text(json.dumps({"source_tree": builder.source_digest()}), encoding="utf-8")
    ok, message = builder.check_source_tree(manifest)
    assert ok and "matches the working tree" in message


def test_a_stale_bundle_is_reported_as_stale(fake_tree):
    builder, tree = fake_tree
    stamp = builder.source_digest()
    manifest = tree / "manifest.json"
    manifest.write_text(json.dumps({"source_tree": stamp}), encoding="utf-8")
    (tree / "tcad" / "compiler.py").write_text("# changed after the build\n", encoding="utf-8")
    ok, message = builder.check_source_tree(manifest)
    assert not ok
    assert "STALE" in message and stamp["sha256"][:16] in message
    assert "rebuild" in message, "a stale verdict must say what to do about it"


def test_missing_unreadable_and_legacy_manifests_are_refused(tmp_path, fake_tree):
    builder, tree = fake_tree
    ok, message = builder.check_source_tree(tmp_path / "nope.json")
    assert not ok and "no manifest" in message

    broken = tree / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    ok, message = builder.check_source_tree(broken)
    assert not ok and "unreadable" in message

    legacy = tree / "legacy.json"
    legacy.write_text(json.dumps({"attempt_id": "acc-1"}), encoding="utf-8")
    ok, message = builder.check_source_tree(legacy)
    assert not ok and "no source_tree" in message


def test_the_check_never_silently_passes_on_a_wrong_field(fake_tree):
    """A manifest whose stamp is a string, not the documented object, fails."""
    builder, tree = fake_tree
    manifest = tree / "manifest.json"
    manifest.write_text(json.dumps({"source_tree": "60efd082"}), encoding="utf-8")
    ok, message = builder.check_source_tree(manifest)
    assert not ok and "no source_tree" in message
