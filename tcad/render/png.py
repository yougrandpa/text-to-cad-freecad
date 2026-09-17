"""PNG encoding with two backends.

The render path must *never* die because a dependency is missing. Pillow is the
preferred backend (fast, correct), but a pure-stdlib ``zlib`` + ``struct`` RGB8
encoder is the guarantee: if Pillow is absent we still produce a valid PNG.

The stdlib backend is not decoration — it is the fallback that keeps "let the
model see what it drew" alive in a minimal supervisor install. ``backend_in_use``
reports which one is active so operators can see it.
"""

from __future__ import annotations

import importlib.util
import struct
import zlib

import numpy as np

_HAS_PILLOW = importlib.util.find_spec("PIL") is not None


def backend_in_use() -> str:
    """Return the active backend name: ``"pillow"`` or ``"stdlib"``."""
    return "pillow" if _HAS_PILLOW else "stdlib"


def write_png(arr: np.ndarray, path: str) -> tuple[int, int]:
    """Encode an ``(H, W, 3)`` uint8 RGB array to ``path``. Returns (w, h)."""
    if _HAS_PILLOW:
        return _write_png_pillow(arr, path)
    return _write_png_stdlib(arr, path)


def _write_png_pillow(arr: np.ndarray, path: str) -> tuple[int, int]:
    from PIL import Image

    img = np.asarray(arr, dtype=np.uint8)
    if img.ndim == 2:
        img = np.stack([img, img, img], axis=-1)
    im = Image.fromarray(img, "RGB")
    im.save(path)
    return int(im.width), int(im.height)


def _write_png_stdlib(arr: np.ndarray, path: str) -> tuple[int, int]:
    """Hand-rolled RGB8 PNG encoder (no third-party deps).

    Layout: 8-byte signature, then IHDR / IDAT / IEND chunks. Each chunk is
    ``[length:4 BE][type:4][data][crc32:4 BE]`` where the CRC covers type+data.
    Scanlines are prefixed with filter byte 0 (None) and the lot is zlib-
    compressed.
    """
    img = np.asarray(arr, dtype=np.uint8)
    if img.ndim == 2:
        img = np.stack([img, img, img], axis=-1)
    if img.ndim != 3 or img.shape[2] != 3:
        raise ValueError(f"expected (H,W,3) RGB array, got shape {img.shape}")
    H, W, _ = img.shape

    # Build raw scanlines with a leading filter-type byte (0) per row.
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        raw.extend(img[y].tobytes())

    compressed = zlib.compress(bytes(raw), 9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)  # 8-bit, colour type 2 (RGB)
    png = sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)
    return W, H
