# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Per-user settings (theme, colours, view mode, recent projects).

These belong to the *person*, not the repository, so they live in the OS's
normal per-user config folder rather than in ``.carrier/``:

* Windows: ``%APPDATA%\\Carrier\\settings.json``
* macOS:   ``~/Library/Application Support/Carrier/settings.json``
* Linux:   ``$XDG_CONFIG_HOME/carrier/settings.json`` (default ``~/.config``)

``CARRIER_CONFIG_HOME`` overrides the folder (used by tests, and handy for a
portable install). Both the terminal UI and the desktop app read this file,
so a theme chosen in one applies in the other — and in every Carrier tool.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "theme": "default",
    "custom_colors": {},  # key -> "#rrggbb", see themes.KEYS
    "diff_mode": "unified",  # "unified" | "split"
    "font_size": 14,  # desktop app only
    "recent_repos": [],  # most recent first
    "fd_policy": "warn",  # Flight Deck: what happens when sessions overlap (see flightdeck/manager.py)
}
MAX_RECENT = 10

# The desktop app calls these from several worker threads at once (e.g. a
# slider firing repeatedly); one lock makes read-modify-write atomic.
_LOCK = threading.RLock()


def config_dir() -> Path:
    if override := os.environ.get("CARRIER_CONFIG_HOME"):
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Carrier"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Carrier"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "carrier"


def settings_path() -> Path:
    return config_dir() / "settings.json"


def load() -> dict[str, Any]:
    with _LOCK:
        return _load()


def _load() -> dict[str, Any]:
    data = dict(DEFAULTS)
    try:
        stored = json.loads(settings_path().read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            data.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass  # missing or corrupt file: defaults
    return data


def save(data: dict[str, Any]) -> None:
    with _LOCK:
        _save(data)


def _save(data: dict[str, Any]) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: data.get(k, v) for k, v in DEFAULTS.items()}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, indent=2)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: file briefly open elsewhere (antivirus, indexer)
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


def update(**changes: Any) -> dict[str, Any]:
    with _LOCK:
        data = _load()
        data.update({k: v for k, v in changes.items() if k in DEFAULTS})
        _save(data)
        return data


def remember_repo(path: str) -> None:
    with _LOCK:
        data = _load()
        recent = [p for p in data["recent_repos"] if p != path]
        data["recent_repos"] = [path, *recent][:MAX_RECENT]
        _save(data)
