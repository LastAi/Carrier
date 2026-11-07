# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Carrier's command line: ``carrier <tool> [...]``.

Carrier is a suite of three tools around Claude Code, named after the
stages of a carrier flight operation::

    carrier readyroom       Ready Room  — set up projects, goals, rules and tasks (edited in the
                            app; the terminal lists projects and their current tasks)
    carrier flightdeck ...  Flight Deck — run Claude Code sessions in parallel (in the app;
                            the terminal manages projects and links)
    carrier debrief ...     Debrief     — human-led review of what changed
    carrier app             open the desktop app (all tools)
    carrier doctor          check git, Claude Code and the Agent SDK

``debrief`` on its own is a shortcut for ``carrier debrief``. Each tool owns
its own sub-commands (see ``carrier debrief --help``); this module only
dispatches.
"""

from __future__ import annotations

import sys

from carrier import __version__

TOOLS = {
    "readyroom": "Ready Room  — set up projects: goals, rules, tasks, presets",
    "flightdeck": "Flight Deck — run Claude Code sessions in parallel, aware of each other",
    "debrief": "Debrief     — review what changed, with a guide, then approve or request fixes",
}

USAGE = f"""Carrier {__version__} — Brief. Launch. Debrief. A mothership for Claude Code.

Tools:
  carrier readyroom    {TOOLS["readyroom"].split("— ", 1)[1]}
  carrier flightdeck   {TOOLS["flightdeck"].split("— ", 1)[1]}
  carrier debrief      {TOOLS["debrief"].split("— ", 1)[1]}

Also:
  carrier app          open the desktop app
  carrier doctor       check git, Claude Code and the Agent SDK
  carrier --version

`debrief` on its own is a shortcut for `carrier debrief`.
Run `carrier debrief --help` for the review commands.
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "--version":
        print(f"Carrier {__version__}")
        return 0
    if cmd == "debrief":
        from carrier.debrief.cli import main as debrief_main

        return debrief_main(rest)
    if cmd == "flightdeck":
        from carrier.flightdeck.cli import main as fd_main

        return fd_main(rest)
    if cmd == "readyroom":
        return _readyroom(rest)
    if cmd == "app":
        from carrier.desktop.main import main as app_main

        return app_main()
    if cmd == "doctor":
        from carrier.debrief.cli import main as debrief_main

        return debrief_main(["doctor"])
    if cmd == "prepare":
        # Hooks installed by older builds call `... prepare --background`.
        from carrier.debrief.cli import main as debrief_main

        return debrief_main(argv)
    print(f"carrier: unknown command {cmd!r}\n\n{USAGE}", file=sys.stderr)
    return 2


def _readyroom(args: list[str]) -> int:
    """List projects and their current tasks. Editing happens in the desktop app."""
    if args and args[0] in ("-h", "--help"):
        print("usage: carrier readyroom\n\nLists projects, tags and current tasks. "
              "Set projects up in the desktop app (carrier app → Ready Room).")
        return 0
    from carrier.core.db import Store, default_path
    from carrier.readyroom.manager import ReadyRoom

    ov = ReadyRoom(Store(default_path())).overview()
    if not ov["projects"]:
        print("No projects yet. Open the app (carrier app) and use Ready Room → New project.")
        return 0
    for p in ov["projects"]:
        tags = f"  [{', '.join(p['tags'])}]" if p["tags"] else ""
        print(f"{p['name']}{tags}\n  {p['path']}")
        print(f"  current task: {p['current_task'] or '(none)'}" + (f"  (+{p['planned'] - 1} more)" if p["planned"] > 1 else ""))
    return 0
