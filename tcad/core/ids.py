"""Identifiers that are allowed to become filesystem paths (task book §5-E).

``model_id`` arrives from an HTTP body or a URL path, and from the model's own
tool arguments (``asset_export``'s ``name``). It is then used to build a path:

    data/models/<model_id>/v0.json
    data/artifacts/<model_id>/v<N>/<model_id>.step

Without validation, ``model_id = "../../../../tmp/pwned"`` writes outside the
data directory, and an absolute id reads wherever it points. This module is the
one place that decides what an identifier may be, so the API boundary, the store
and the tools cannot each invent a different answer.

Two mechanisms, deliberately separate:

  * :func:`ensure_safe_id` constrains the *shape* of a name — it can never
    contain a separator, ``..`` or NUL, so it cannot name anything outside its
    parent by construction. This is the primary rule and it applies to
    model/thread ids and export names.
  * :func:`contained_path` verifies the *resolved* result anyway, for the paths
    that legitimately take a path (``asset_import``) and as defence in depth
    where a symlink could redirect a name that looked fine.

Honest limit: a resolved-path check is not atomic against an attacker who can
create symlinks between the check and the use. The primary rule (shape) does not
have that weakness, which is why it is the one applied to names.
"""

from __future__ import annotations

import re
from pathlib import Path

#: ``[A-Za-z0-9]`` first, then letters/digits/``.``/``_``/``-``, at most 64 chars.
#: No separators, no spaces, no Unicode, no leading dot. That is narrower than
#: POSIX allows, on purpose: every id the system mints or a user types is ASCII
#: and short, and a narrow rule is one that can be reasoned about.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

MAX_ID_LEN = 64


class InvalidIdentifier(ValueError):
    """An identifier or path that must not be turned into a filesystem path."""

    def __init__(self, value: object, *, kind: str = "identifier", reason: str = "") -> None:
        self.value = value
        self.kind = kind
        detail = f" ({reason})" if reason else ""
        super().__init__(
            f"invalid {kind}: {value!r}{detail}. Allowed: letters/digits/'.'/'_'/'-', "
            f"1-{MAX_ID_LEN} chars, must start with a letter or digit — no '/', no "
            "'..', no spaces."
        )


def is_safe_id(value: object) -> bool:
    """Whether ``value`` may be used as a single path component."""
    if not isinstance(value, str):
        return False
    if len(value) > MAX_ID_LEN:
        return False
    if "\x00" in value:
        return False
    return bool(_ID_RE.match(value))


def ensure_safe_id(value: object, *, kind: str = "identifier") -> str:
    """Return ``value`` if it is a safe single path component, else raise."""
    if not is_safe_id(value):
        reason = ""
        if isinstance(value, str) and value.startswith("."):
            reason = "must not start with '.'"
        elif isinstance(value, str) and any(s in value for s in ("/", "\\")):
            reason = "must not contain a path separator"
        elif isinstance(value, str) and len(value) > MAX_ID_LEN:
            reason = f"longer than {MAX_ID_LEN} characters"
        raise InvalidIdentifier(value, kind=kind, reason=reason)
    return str(value)


def contained_path(root: str | Path, *parts: str) -> Path:
    """``root/parts...``, proven to resolve inside ``root``.

    The containment test is performed on the **resolved** path, so ``..`` and
    symlinks are collapsed before the comparison — comparing the literal string
    is the version that looks right and is not.
    """
    root_resolved = Path(root).resolve()
    target = root_resolved.joinpath(*parts)
    resolved = target.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise InvalidIdentifier(
            str(target), kind="path",
            reason=f"resolves outside {root_resolved} (to {resolved})")
    return target


def ensure_contained(path: str | Path, root: str | Path, *, kind: str = "path") -> Path:
    """Return ``path`` resolved-clean if it lies under ``root``, else raise."""
    root_resolved = Path(root).resolve()
    resolved = Path(path).resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise InvalidIdentifier(
            str(path), kind=kind,
            reason=f"resolves outside {root_resolved} (to {resolved})")
    return resolved
