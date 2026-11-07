# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Ready Room — setting projects up before work starts.

On a carrier, pilots get their mission briefing in the ready room before
they fly. Here the user sets up each project: a long-term goal, rules
(global and per project, plus reusable rule sets), a queue of tasks (the
first is the current task), links to related projects, session presets,
project templates and purely visual tags.

* ``manager.ReadyRoom`` — the operations the UI calls (no UI code).
* ``context.build``     — turns goal, rules, task and recent history into the
  text Flight Deck adds to Claude Code's system prompt.

Data lives in the shared store (``carrier.core.db``). See ARCHITECTURE.md.
"""
