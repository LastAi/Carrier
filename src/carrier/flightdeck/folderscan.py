# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""What changed in a set of folders between two moments, without git.

A paused session (and one watching its reference folders) keeps a *manifest* — every
file's size and modification time — and later compares it with the folders as they are
now. It works for any folder (git or not) and costs one directory walk.
"""

from __future__ import annotations

import json
import os
import time
import zlib
from typing import Any

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".gradle", ".mypy_cache", ".pytest_cache",
             ".tox", ".cache"}
MAX_FILES = 20000
MAX_SECONDS = 5.0


def manifest(roots: list[str]) -> dict[str, Any]:
    """``{"files": {abs path: [size, mtime_ns]}, "roots": [...], "truncated": bool, "taken": ts}``."""
    files: dict[str, list[int]] = {}
    truncated = False
    deadline = time.time() + MAX_SECONDS
    seen_roots: list[str] = []
    for root in roots:
        if not root or not os.path.isdir(root) or root in seen_roots:
            continue
        seen_roots.append(root)
        stack = [root]
        while stack and not truncated:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if e.name not in SKIP_DIRS:
                                    stack.append(e.path)
                            elif e.is_file(follow_symlinks=False):
                                st = e.stat(follow_symlinks=False)
                                files[os.path.normcase(e.path)] = [st.st_size, st.st_mtime_ns]
                        except OSError:
                            continue
                        if len(files) >= MAX_FILES or time.time() > deadline:
                            truncated = True
                            break
            except OSError:
                continue
    return {"files": files, "roots": seen_roots, "truncated": truncated, "taken": time.time()}


def diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Files changed, added and removed between two manifests (of the same roots)."""
    a, b = old.get("files", {}), new.get("files", {})
    partial = bool(old.get("truncated") or new.get("truncated"))
    changed = sorted(p for p in a.keys() & b.keys() if a[p] != b[p])
    # A scan cut short covers a different part of the folders each time: only files seen
    # both times can be compared, so new and deleted files aren't reported then.
    added = [] if partial else sorted(b.keys() - a.keys())
    removed = [] if partial else sorted(a.keys() - b.keys())
    return {"changed": changed, "added": added, "removed": removed, "truncated": partial}


def pack(m: dict[str, Any]) -> bytes:
    return zlib.compress(json.dumps(m, separators=(",", ":")).encode("utf-8"))


def unpack(blob: bytes | None) -> dict[str, Any] | None:
    if not blob:
        return None
    try:
        return json.loads(zlib.decompress(blob).decode("utf-8"))
    except (zlib.error, ValueError):
        return None
