# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Which Claude models are available, and which effort levels each supports.

Claude Code reports its models when a session connects (the ``models``
list of its initialize response: value, display name, ``supportsEffort``,
``supportedEffortLevels``). The desktop app asks once at startup — a
connect and disconnect, no message, so it costs nothing — and keeps the
answer in ``models.json`` in the user settings folder for the next start.
Until the first answer, a built-in list (Haiku, Sonnet, Opus; every
effort level) is used.

**Effort is a preference, not a promise.** The user's chosen effort is
stored as chosen; :func:`effective_effort` works out what is actually sent:

* the model supports that level → use it;
* it doesn't → the closest *lower* level it supports (or, if there is
  none, its lowest level);
* the model has no effort setting → send none (the UI greys the field but
  keeps the choice, so switching back to a model with effort restores it).

This module never imports the Agent SDK; ``core.llm.probe_models`` does
the asking.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")

# Used until Claude Code has been asked (or if asking fails).
FALLBACK: list[dict[str, Any]] = [
    {"value": "haiku", "label": "Haiku", "description": "", "efforts": list(EFFORT_ORDER)},
    {"value": "sonnet", "label": "Sonnet", "description": "", "efforts": list(EFFORT_ORDER)},
    {"value": "opus", "label": "Opus", "description": "", "efforts": list(EFFORT_ORDER)},
]
FALLBACK_DEFAULT: dict[str, Any] = {"value": "default", "label": "Default", "description": "",
                                    "efforts": list(EFFORT_ORDER)}

_lock = threading.Lock()
_cache: dict[str, Any] | None = None


def _path() -> Any:
    from carrier.core.usersettings import config_dir

    return config_dir() / "models.json"


def catalog() -> dict[str, Any]:
    """``{"models": [...], "default": {...}, "checked": epoch seconds | None}``."""
    global _cache
    with _lock:
        if _cache is None:
            try:
                data = json.loads(_path().read_text(encoding="utf-8"))
                if not isinstance(data.get("models"), list) or not data["models"]:
                    raise ValueError
                _cache = {"models": data["models"], "default": data.get("default") or FALLBACK_DEFAULT,
                          "checked": data.get("checked")}
            except (OSError, ValueError, AttributeError):
                _cache = {"models": FALLBACK, "default": FALLBACK_DEFAULT, "checked": None}
        return _cache


def from_server_info(models: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Turn Claude Code's ``models`` list into ``(models, default entry)``."""
    out, default = [], None
    for m in models or []:
        value = str(m.get("value") or "").strip()
        if not value:
            continue
        efforts = [e for e in (m.get("supportedEffortLevels") or EFFORT_ORDER) if e in EFFORT_ORDER] \
            if m.get("supportsEffort") else []
        entry = {"value": value, "label": str(m.get("displayName") or value),
                 "description": str(m.get("description") or ""), "efforts": efforts}
        if value == "default":
            default = entry
        else:
            out.append(entry)
    return out, default


def store(models: list[dict[str, Any]], default: dict[str, Any] | None) -> dict[str, Any]:
    """Save a fresh catalog (from :func:`from_server_info`)."""
    global _cache
    if not models:
        return catalog()
    data = {"models": models, "default": default or FALLBACK_DEFAULT, "checked": time.time()}
    with _lock:
        _cache = data
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass
    return data


def known(model: str | None) -> bool:
    """Is ``model`` one the user can pick (empty = Claude Code's default)?"""
    return not model or model in {m["value"] for m in catalog()["models"]} or model in {"haiku", "sonnet", "opus"}


def efforts_for(model: str | None) -> list[str]:
    """Effort levels ``model`` supports (empty list: it has no effort setting)."""
    cat = catalog()
    if not model:
        return list(cat["default"].get("efforts") or [])
    for m in cat["models"]:
        if m["value"] == model:
            return list(m.get("efforts") or [])
    return list(EFFORT_ORDER)  # unknown to us (e.g. a full model id): let Claude Code decide


def effective_effort(model: str | None, effort: str | None) -> str | None:
    """The effort actually sent for ``model`` when the user prefers ``effort``."""
    if not effort or effort not in EFFORT_ORDER:
        return None
    supported = [e for e in EFFORT_ORDER if e in efforts_for(model)]
    if not supported:
        return None
    if effort in supported:
        return effort
    want = EFFORT_ORDER.index(effort)
    lower = [e for e in supported if EFFORT_ORDER.index(e) < want]
    return lower[-1] if lower else supported[0]
