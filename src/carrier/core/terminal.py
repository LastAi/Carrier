# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Open a new terminal window running Claude Code on a fix list.

Used by the desktop app's "Start Claude Code with fixes" button and by
``debrief fixes --run``. Instead of pasting the whole fix list onto the
command line (fragile quoting on every OS), Claude Code is given one short
instruction to read the file.

If no terminal can be launched, the caller gets ``ok=False`` plus the exact
command to run by hand.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def fix_prompt(repo: Path, fixes: Path) -> str:
    try:
        rel = fixes.relative_to(repo).as_posix()
    except ValueError:
        rel = fixes.as_posix()
    return f"Read {rel} and carry out the review fixes it lists, in order."


def manual_command(repo: Path, fixes: Path) -> str:
    prompt = fix_prompt(repo, fixes)
    if sys.platform == "win32":
        return f'cd /d "{repo}" && claude "{prompt}"'
    return f"cd {shlex.quote(str(repo))} && claude {shlex.quote(prompt)}"


def launch_claude(repo: Path, fixes: Path) -> dict[str, Any]:
    prompt = fix_prompt(repo, fixes)
    manual = manual_command(repo, fixes)
    try:
        if sys.platform == "win32":
            # A new console window (Windows Terminal or conhost, whichever is default).
            subprocess.Popen(["cmd", "/k", "claude", prompt], cwd=repo,
                             creationflags=subprocess.CREATE_NEW_CONSOLE)  # type: ignore[attr-defined]
            return {"ok": True, "command": manual}
        if sys.platform == "darwin":
            script = manual.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{script}"',
                              "-e", 'tell application "Terminal" to activate'])
            return {"ok": True, "command": manual}
        # Linux / BSD: try common terminals; keep the window open afterwards.
        inner = f"{manual}; exec ${{SHELL:-bash}}"
        candidates = [
            ["x-terminal-emulator", "-e", "bash", "-lc", inner],
            ["gnome-terminal", "--", "bash", "-lc", inner],
            ["konsole", "-e", "bash", "-lc", inner],
            ["xfce4-terminal", "-x", "bash", "-lc", inner],
            ["kitty", "bash", "-lc", inner],
            ["alacritty", "-e", "bash", "-lc", inner],
            ["xterm", "-e", "bash", "-lc", inner],
        ]
        for cmd in candidates:
            if shutil.which(cmd[0]):
                subprocess.Popen(cmd, cwd=repo, start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ.copy())
                return {"ok": True, "command": manual}
    except OSError:
        pass
    return {"ok": False, "command": manual}
