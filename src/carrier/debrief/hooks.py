# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Claude Code integration: the Stop hook and background preparation.

Claude Code runs a *Stop hook* every time it finishes responding. Debrief
installs one that starts ``carrier debrief prepare`` **in the background**, so by
the time you open a review the diff is grouped and the criteria have run.

The hook must return instantly (it would otherwise delay Claude Code), so it
only spawns a detached process and exits. A lock file stops several
prepares running at once when Claude Code stops repeatedly.

The hook goes into ``.claude/settings.local.json`` by default — that file is
personal and not committed, so teammates without Carrier are unaffected.
``--shared`` writes to ``.claude/settings.json`` instead.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from carrier.core.config import Config

# Text that identifies our hook command in settings files (works for both
# "python -m carrier debrief prepare --background", "carrier-cli debrief prepare
# --background", and hooks from builds before the rename).
HOOK_MARKER = "prepare --background"
LOCK_STALE_SECONDS = 3 * 60  # well above HEARTBEAT_SECONDS


def self_command() -> list[str]:
    """How to invoke Debrief's command line from a subprocess.

    * From source / pip: this exact Python with ``-m carrier debrief`` (works
      inside venvs and pipx without relying on PATH).
    * From a compiled build (PyInstaller sets ``sys.frozen``): the
      ``carrier-cli`` executable that ships next to the app, because a
      frozen app has no ``-m``.
    """
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).with_name("carrier-cli.exe" if os.name == "nt" else "carrier-cli")
        return [str(exe), "debrief"]
    return [sys.executable, "-m", "carrier", "debrief"]


def is_our_hook(command: str) -> bool:
    """Recognises hooks from this and earlier builds (incl. pre-rename 'debrief')."""
    return HOOK_MARKER in command and ("carrier" in command or "debrief" in command)


def hook_command() -> str:
    """Command line for the hook (quoted for the shell Claude Code uses)."""
    first, *rest = self_command()
    return " ".join([f'"{first}"', *rest, "prepare", "--background", "--quiet"])


def settings_path(repo: Path, shared: bool) -> Path:
    return repo / ".claude" / ("settings.json" if shared else "settings.local.json")


def install_stop_hook(repo: Path, shared: bool = False) -> tuple[Path, bool]:
    """Add the hook. Returns ``(settings_file, changed)``; idempotent."""
    path = settings_path(repo, shared)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    stop = data.setdefault("hooks", {}).setdefault("Stop", [])
    for group in stop:
        for h in group.get("hooks", []):
            if is_our_hook(h.get("command", "")):
                if h["command"] == hook_command():
                    return path, False
                # Installed by a different Debrief (e.g. pip vs compiled app):
                # point it at this one.
                h["command"] = hook_command()
                path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                return path, True
    stop.append({"hooks": [{"type": "command", "command": hook_command()}]})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if not shared:
        _exclude_locally(repo, ".claude/settings.local.json")
    return path, True


def _exclude_locally(repo: Path, rel: str) -> None:
    """Make sure a personal file is ignored *without* editing .gitignore.

    ``.git/info/exclude`` is git's per-clone ignore list: it is never
    committed, so the user's repository is not changed.
    """
    from carrier.core.gitutil import git

    if subprocess.run(["git", "check-ignore", "-q", rel], cwd=repo).returncode == 0:
        return  # already ignored (e.g. Claude Code added it to .gitignore)
    exclude = Path(git(repo, "rev-parse", "--git-path", "info/exclude").strip())
    if not exclude.is_absolute():
        exclude = repo / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if rel not in existing.splitlines():
        with exclude.open("a", encoding="utf-8") as fh:
            fh.write(("" if existing.endswith("\n") or not existing else "\n") + rel + "\n")


def remove_stop_hook(repo: Path) -> list[Path]:
    """Remove Debrief's hook from both settings files. Returns files changed.

    Builds new lists instead of editing in place, so other hooks that share
    a group with ours are kept and the change is reliably detected.
    """
    changed = []
    for shared in (False, True):
        path = settings_path(repo, shared)
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        stop = data.get("hooks", {}).get("Stop", [])
        removed = False
        kept = []
        for group in stop:
            hooks_list = group.get("hooks", [])
            remaining = [h for h in hooks_list if not is_our_hook(h.get("command", ""))]
            removed |= len(remaining) != len(hooks_list)
            if remaining:
                kept.append({**group, "hooks": remaining})
        if removed:
            if kept:
                data["hooks"]["Stop"] = kept
            else:
                del data["hooks"]["Stop"]
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            changed.append(path)
    return changed


# --------------------------------------------------------------------------
# Lock + background process
# --------------------------------------------------------------------------


# The lock file holds the owner's PID and is "touched" every HEARTBEAT
# seconds while a prepare runs. A lock not touched for LOCK_STALE_SECONDS
# belongs to a crashed process and may be taken over. (We don't probe the
# PID: on Windows os.kill(pid, 0) would *terminate* the process.)
HEARTBEAT_SECONDS = 20


def lock_path(cfg: Config) -> Path:
    return cfg.data_dir / "prepare.lock"


def prepare_running(cfg: Config) -> bool:
    lock = lock_path(cfg)
    try:
        age = time.time() - lock.stat().st_mtime
    except FileNotFoundError:
        return False
    return age <= LOCK_STALE_SECONDS


def acquire_lock(cfg: Config) -> bool:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    lock = lock_path(cfg)
    if lock.exists() and not prepare_running(cfg):
        lock.unlink(missing_ok=True)  # stale: its owner stopped heartbeating
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as fh:
        fh.write(str(os.getpid()))
    return True


def _owned(cfg: Config) -> bool:
    try:
        return lock_path(cfg).read_text().strip() == str(os.getpid())
    except OSError:
        return False


def release_lock(cfg: Config) -> None:
    """Remove the lock only if this process owns it."""
    if _owned(cfg):
        lock_path(cfg).unlink(missing_ok=True)


@contextmanager
def held_lock(cfg: Config) -> Iterator[bool]:
    """``with held_lock(cfg) as ok:`` — acquire, heartbeat, release.

    Yields False (and does nothing) if another prepare holds the lock.
    """
    if not acquire_lock(cfg):
        yield False
        return
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            if _owned(cfg):
                os.utime(lock_path(cfg))

    t = threading.Thread(target=beat, daemon=True)
    t.start()
    try:
        yield True
    finally:
        stop.set()
        release_lock(cfg)


def spawn_background_prepare(cfg: Config) -> None:
    """Start ``python -m carrier debrief prepare`` detached from this process."""
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    log = (cfg.data_dir / "prepare.log").open("a", encoding="utf-8")
    log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} background prepare ---\n")
    log.flush()
    cmd = [*self_command(), "prepare"]
    common = dict(cwd=cfg.repo, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    if os.name != "nt":
        subprocess.Popen(cmd, start_new_session=True, **common)  # type: ignore[call-overload]
        return
    # Windows: CREATE_NO_WINDOW gives the child a hidden console that its git
    # subprocesses inherit (DETACHED_PROCESS would make each git flash a
    # window). BREAKAWAY lets it outlive Claude Code's hook job object; some
    # jobs forbid breakaway, so retry without it.
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    try:
        subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB,  # type: ignore[attr-defined]
                         **common)  # type: ignore[call-overload]
    except OSError:
        subprocess.Popen(cmd, creationflags=flags, **common)  # type: ignore[call-overload]
