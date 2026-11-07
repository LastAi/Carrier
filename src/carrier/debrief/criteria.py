# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Review criteria: small markdown files that tell an agent what to flag.

A criterion is a markdown file with a short front-matter block::

    ---
    name: scope-creep            # unique id (defaults to the file name)
    title: Unrequested changes   # shown in the UI
    severity: medium             # default severity: high | medium | low
    sees_rationale: false        # may the agent read Claude's own explanations?
    uses_notes: true             # include project notes in the prompt
    explore: false               # give the agent read-only repo tools
    model: sonnet                # optional per-criterion model override
    effort: high                 # optional: low | medium | high | xhigh | max
    enabled: true
    ---
    Flag any change the user did not ask for ...

The body is the instruction. Debrief wraps it in a fixed prompt (see
``flagging.py``) that supplies the diff, the user's requests and the notes,
and asks for JSON back — so authors only describe *what to look for*.

Where criteria come from (later wins on name clashes):

1. built-ins shipped in ``debrief/criteria_builtin/``,
2. the project's ``.carrier/criteria/*.md``.

So a project can override a built-in by creating a file with the same
``name``, or switch it off with ``[criteria] disabled = ["name"]``.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from carrier.core.config import Config
from carrier.debrief.models import SEVERITIES


@dataclass
class Criterion:
    name: str
    title: str
    prompt: str
    severity: str = "medium"
    sees_rationale: bool = False
    uses_notes: bool = True
    explore: bool = False
    model: str | None = None
    effort: str | None = None  # Claude Code reasoning effort; None = default
    enabled: bool = True
    source: str = "builtin"  # "builtin" or the file path


def _coerce(value: str) -> str | bool:
    low = value.strip().lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    return value.strip().strip('"').strip("'")


def parse_frontmatter(text: str) -> tuple[dict[str, str | bool], str]:
    """Minimal ``---`` front-matter parser (``key: value`` lines only).

    Deliberately tiny so Debrief needs no YAML dependency. ``#`` starts a
    comment. Returns ``(fields, body)``.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    fields: dict[str, str | bool] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return fields, "\n".join(lines[i + 1 :]).strip()
        line = line.split(" #", 1)[0]
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = _coerce(value)
    return {}, text  # unterminated front matter: treat whole file as body


def parse_criterion(text: str, default_name: str, source: str) -> Criterion:
    fields, body = parse_frontmatter(text)
    name = str(fields.get("name") or default_name)
    severity = str(fields.get("severity", "medium")).lower()
    if severity not in SEVERITIES:
        severity = "medium"
    model = fields.get("model")
    effort = str(fields.get("effort") or "").lower() or None
    return Criterion(
        name=name,
        title=str(fields.get("title") or name.replace("-", " ").capitalize()),
        prompt=body,
        severity=severity,
        sees_rationale=bool(fields.get("sees_rationale", False)),
        uses_notes=bool(fields.get("uses_notes", True)),
        explore=bool(fields.get("explore", False)),
        model=str(model) if model else None,
        effort=effort if effort in ("low", "medium", "high", "xhigh", "max") else None,
        enabled=bool(fields.get("enabled", True)),
        source=source,
    )


def builtin_criteria() -> list[Criterion]:
    out = []
    pkg = resources.files("carrier.debrief.criteria_builtin")
    for entry in sorted(pkg.iterdir(), key=lambda p: p.name):
        if entry.name.endswith(".md"):
            out.append(parse_criterion(entry.read_text(encoding="utf-8"), entry.name[:-3], "builtin"))
    return out


def project_criteria(cfg: Config) -> list[Criterion]:
    folder: Path = cfg.data_dir / "criteria"
    if not folder.is_dir():
        return []
    return [
        parse_criterion(p.read_text(encoding="utf-8"), p.stem, p.relative_to(cfg.repo).as_posix())
        for p in sorted(folder.glob("*.md"))
        if p.name.lower() != "readme.md"  # the folder's own explainer, not a criterion
    ]


def load_criteria(cfg: Config, include_disabled: bool = False) -> list[Criterion]:
    """Built-ins overlaid with project criteria, minus disabled ones."""
    merged: dict[str, Criterion] = {}
    for c in [*builtin_criteria(), *project_criteria(cfg)]:
        merged[c.name] = c
    disabled = set(cfg.get("criteria", "disabled"))
    result = list(merged.values())
    if include_disabled:
        return result
    return [c for c in result if c.enabled and c.name not in disabled]
