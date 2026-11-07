# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Extract JSON from model replies.

Agents are asked to reply with a single JSON object, but models sometimes
wrap it in a ```json fence or add a sentence before it. :func:`extract_json`
copes with both. When nothing parses it raises :class:`ValueError`, and each
caller decides on a safe fallback (no flags, per-file grouping, …).
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    text = text.strip()
    candidates = [text, *(m.group(1) for m in _FENCE_RE.finditer(text))]
    # Also try the outermost {...} span.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except (json.JSONDecodeError, TypeError):
            continue
    raise ValueError(f"no JSON object found in model reply: {text[:200]!r}")
