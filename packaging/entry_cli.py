# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""PyInstaller entry point for ``carrier-cli`` (terminal tools and commands).

Also used by the Claude Code Stop hook in compiled builds, because a frozen
app cannot run ``python -m carrier`` (see carrier.debrief.hooks.self_command).
"""

from carrier.cli import main

raise SystemExit(main())
