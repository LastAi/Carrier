# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Debrief — human-led, agent-guided review of work done by Claude Code.

After the flight comes the debrief: Debrief collects what changed, works out
why from the Claude Code transcripts and project notes, has criteria agents
flag what deserves attention, and walks the reviewer through each change.

Pipeline::

    checkpoint ──► snapshot/diff ──► notes + transcripts ──► segment ──► flagging
        ──► ranking ──► (saved Review) ──► guide (interactive) ──► record

Front ends call :class:`carrier.debrief.session.ReviewSession`.
"""
