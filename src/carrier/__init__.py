# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Carrier — Brief. Launch. Debrief. A mothership for Claude Code.

Three tools, named after the stages of a carrier flight operation:

* ``carrier.readyroom``  — Ready Room: projects, goals, rules, tasks, presets
* ``carrier.flightdeck`` — Flight Deck: running Claude Code sessions on those tasks
* ``carrier.debrief``    — Debrief: human-led review of what changed

Shared code lives in ``carrier.core``. Front ends: the terminal UI of each
tool, and one desktop app (``carrier.desktop`` + ``carrier.web``) that
hosts all three. See ARCHITECTURE.md in the repository root for the map.
"""

__version__ = "0.6.0"
