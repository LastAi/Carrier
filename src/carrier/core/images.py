# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Images shown in, or pasted into, a Flight Deck conversation.

Images travel as base64 data (``data:`` URLs in the page, image blocks for
Claude Code): the app loads nothing from the network or from file URLs. The
type is checked from the file's own bytes, not its name.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

MAX_BYTES = 5 * 1024 * 1024  # Claude's own limit for one image
MIMES = ("image/png", "image/jpeg", "image/gif", "image/webp")


def sniff(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:512].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:2048].lower()):
        return "image/svg+xml"
    return None


def load(path: str) -> dict[str, Any]:
    """Read an image file: ``{"mime", "data" (base64), "name"}``."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise ValueError(f"No image at {path}.")
    if p.stat().st_size > MAX_BYTES:
        raise ValueError(f"{p.name} is larger than 5 MB.")
    raw = p.read_bytes()
    mime = sniff(raw)
    if mime is None:
        raise ValueError(f"{p.name} isn't a PNG, JPEG, GIF, WebP or SVG image.")
    return {"mime": mime, "data": base64.b64encode(raw).decode("ascii"), "name": p.name}


def check_pasted(mime: str, data: str) -> bytes:
    """Validate an image the user pasted (base64) before it goes to Claude; returns the bytes."""
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("That image couldn't be read.") from exc
    if len(raw) > MAX_BYTES:
        raise ValueError("Images must be under 5 MB.")
    real = sniff(raw)
    if real not in MIMES or real != mime:
        raise ValueError("Paste a PNG, JPEG, GIF or WebP image.")
    return raw
