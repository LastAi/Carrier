# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Per-project configuration: defaults plus the optional ``.carrier/config.toml``.

Carrier must work with **zero configuration**. Every setting has a default
here; the project file only needs the keys a user wants to change.
``debrief init`` writes a fully commented config file as documentation.

Layout of the per-project ``.carrier/`` folder::

    .carrier/
      config.toml        optional overrides (this module)
      criteria/          project-specific criteria (see criteria.py)
      notes.md           notes added during reviews (picked up by notes.py)
      reviews/<id>/      one folder per Debrief review: review.json, record.md, fixes.md
      prepare.log        output of background prepares (git-ignored)
"""

from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CARRIER_DIR = ".carrier"

# Default settings. The structure mirrors config.toml sections.
DEFAULTS: dict[str, Any] = {
    "notes": {
        # Globs relative to the repo root. Missing files are ignored.
        "paths": [
            "CLAUDE.md",
            "AGENTS.md",
            "README.md",
            "docs/**/*.md",
            "notes/**/*.md",
            ".carrier/notes.md",
        ],
        "max_file_kb": 24,  # each file is truncated beyond this
        "max_total_kb": 80,  # stop adding files beyond this
    },
    "models": {
        # Claude Code model aliases ("haiku", "sonnet", "opus") or full IDs.
        "segment": "haiku",  # groups hunks into logical changes — cheap task
        "criteria": "sonnet",  # default for criteria; a criterion may override
        "guide": "sonnet",  # explains items and answers questions
    },
    "agents": {
        "concurrency": 4,  # max model calls running at once
        "batch_kb": 40,  # diff size per criteria call (items are batched)
        "guide_max_turns": 8,  # tool-use turns the guide may take per answer
    },
    "criteria": {
        "disabled": [],  # names of criteria (built-in or project) to skip
    },
    "review": {
        # Used when there is no checkpoint yet and --from is not given.
        "first_review_from": "HEAD~1",
        "max_hunks_for_ai_grouping": 300,  # above this, group by file instead
    },
}


@dataclass
class Config:
    """Resolved configuration for one repository."""

    repo: Path
    data: dict[str, Any]

    @property
    def data_dir(self) -> Path:
        return self.repo / CARRIER_DIR

    @property
    def reviews_dir(self) -> Path:
        return self.data_dir / "reviews"

    @property
    def notes_file(self) -> Path:
        """Where reviewer notes ("n" in the UI) are appended."""
        return self.data_dir / "notes.md"

    def get(self, section: str, key: str) -> Any:
        return self.data[section][key]


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``."""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(repo: Path) -> Config:
    """Load defaults, then apply ``<repo>/.carrier/config.toml`` if present."""
    data = copy.deepcopy(DEFAULTS)
    path = repo / CARRIER_DIR / "config.toml"
    if path.exists():
        with path.open("rb") as fh:
            data = _merge(data, tomllib.load(fh))
    return Config(repo=repo, data=data)


# Written by ``debrief init``. Every line is commented out so the file is
# pure documentation until the user changes something.
CONFIG_TEMPLATE = """\
# Carrier configuration. Everything here is optional — the values shown are
# the defaults. Uncomment a line to change it.

[notes]
# Project notes the agents read to understand what the code is *supposed*
# to do. Globs are relative to the repository root.
# paths = ["CLAUDE.md", "AGENTS.md", "README.md", "docs/**/*.md", "notes/**/*.md", ".carrier/notes.md"]
# max_file_kb = 24
# max_total_kb = 80

[models]
# Claude Code model aliases ("haiku", "sonnet", "opus") or full model IDs.
# segment = "haiku"
# criteria = "sonnet"
# guide = "sonnet"

[agents]
# concurrency = 4
# batch_kb = 40
# guide_max_turns = 8

[criteria]
# Names of criteria to turn off, e.g. ["security"].
# Add your own criteria as markdown files in .carrier/criteria/.
# disabled = []

[review]
# Starting point for the very first review when there is no checkpoint.
# first_review_from = "HEAD~1"
# max_hunks_for_ai_grouping = 300
"""
