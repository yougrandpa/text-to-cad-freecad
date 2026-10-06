"""Deliver tool renders as pixels after a complete assistant/tool exchange."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tcad.core.types import ToolResult


RENDER_PREFIX = "[Tool render images]"
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def is_render_message(message: Mapping[str, Any]) -> bool:
    content = message.get("content")
    return (
        message.get("role") == "user"
        and isinstance(content, list)
        and bool(content)
        and all(isinstance(part, Mapping) for part in content)
        and content[0].get("type") == "text"
        and content[0].get("text", "").startswith(RENDER_PREFIX)
        and any(part.get("type") == "image_url" for part in content)
        and all(part.get("type") in ("text", "image_url") for part in content)
    )


def tool_image_feedback(
    result: ToolResult, *, name: str, call_id: str, data_dir: str,
    supports_vision: bool,
) -> tuple[dict[str, Any] | None, str]:
    """Return multimodal feedback or an honest note that pixels were not sent.

    Only generated image files inside the runtime data directory may be read.
    The original tool text identifies the artifact/version being inspected.
    Callers append the image message *after all* matching tool results.
    """
    if not result.ok or not result.images:
        return None, ""
    if not supports_vision:
        return None, (
            "\nRender pixels were not sent: the configured provider does not declare "
            "vision support. These are render receipts, not visual evidence; do not "
            "claim to have inspected the images."
        )
    root = Path(data_dir).resolve()
    parts: list[dict[str, Any]] = [{
        "type": "text",
        "text": f"{RENDER_PREFIX} {name}, tool_call_id={call_id}\n{result.content}\n"
                "These pixels belong to this tool result. Later edits require a new render.",
    }]
    missing: list[str] = []
    for ref in result.images[:MAX_IMAGES]:
        try:
            path = Path(ref.path).resolve()
            if not path.is_relative_to(root):
                raise ValueError("outside runtime data directory")
            with path.open("rb") as stream:
                pixels = stream.read(MAX_IMAGE_BYTES + 1)
            if len(pixels) > MAX_IMAGE_BYTES:
                raise ValueError("image exceeds delivery size limit")
            if pixels.startswith(b"\x89PNG\r\n\x1a\n"):
                mime = "image/png"
            elif pixels.startswith(b"\xff\xd8\xff"):
                mime = "image/jpeg"
            elif pixels.startswith(b"RIFF") and pixels[8:12] == b"WEBP":
                mime = "image/webp"
            else:
                raise ValueError("unsupported image bytes")
        except (OSError, ValueError) as exc:
            missing.append(f"{ref.view}: {exc.__class__.__name__}")
            continue
        parts.extend([
            {"type": "text", "text": f"View: {ref.view}, {ref.width} x {ref.height}"},
            {"type": "image_url", "image_url": {
                "url": f"data:{mime};base64,{base64.b64encode(pixels).decode('ascii')}",
                "detail": "high",
            }},
        ])
    if len(result.images) > MAX_IMAGES:
        missing.append("additional views omitted; request them separately")
    note = ""
    if missing:
        note = "\nRender pixels not delivered for " + "; ".join(missing) + ". Do not claim visual inspection of those views."
    if len(parts) == 1:
        return None, note
    return {"role": "user", "content": parts}, note
