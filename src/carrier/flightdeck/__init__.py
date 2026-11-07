# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Flight Deck — running Claude Code sessions.

The flight deck is where aircraft launch. This tool runs Claude Code
sessions through the Agent SDK, several at once: each session works on a
task — a project's current task, or a standalone task with no project.
The user starts, guides, stops and marks tasks done; related sessions are
kept aware of each other (see ``coordination``), and a finished session
can be handed to Debrief for review.

* ``manager.FlightDeck`` — sessions, tasks, events (no UI code).
* ``runner``             — one Claude Code connection per session.
* ``coordination``       — overlap warnings, briefings, contract notices.
* ``sdk``                — the only module here that imports the Agent SDK.

See ARCHITECTURE.md ("Flight Deck").
"""
