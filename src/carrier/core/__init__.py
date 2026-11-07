# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Shared building blocks used by every Carrier tool.

Git access, model access (the Agent SDK backend), per-project config,
project notes, colour themes, per-user settings and terminal launching.
Code moves here when a second tool needs it; tool-specific logic stays in
that tool's package (``carrier.debrief`` …). Core never imports a tool.
"""
