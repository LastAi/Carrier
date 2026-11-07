# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Transcripts, notes, criteria loading and JSON extraction."""

from pathlib import Path

import pytest

from carrier.core.config import load_config
from carrier.debrief.criteria import load_criteria, parse_frontmatter
from carrier.core.jsonutil import extract_json
from carrier.core.notes import append_review_note, collect_notes
from carrier.debrief.transcripts import collect_events, user_requests


def test_transcript_events(repo: Path, claude_home: Path):
    events = collect_events(repo, since=None)
    assert [(e.path, e.tool) for e in events] == [("save.py", "Edit"), ("tests/test_save.py", "Write")]
    assert events[0].user_request == "Make loading saves retry on IO errors."
    assert events[0].assistant_note == "I'll add a retry loop to load()."
    # tool_result entries must not overwrite the human's request
    assert events[1].user_request == "Make loading saves retry on IO errors."
    assert events[1].assistant_note == "The old test is obsolete, removing it."
    assert user_requests(events) == ["Make loading saves retry on IO errors."]


def test_transcript_since_filter(repo: Path, claude_home: Path):
    assert collect_events(repo, since=4_102_444_800 + 10**6) == []  # after year 2100


def test_no_claude_dir_is_fine(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nope"))
    assert collect_events(repo, None) == []


def test_notes_and_review_note(repo: Path):
    cfg = load_config(repo)
    notes, warnings = collect_notes(cfg)
    assert [n.path for n in notes] == ["README.md"] and not warnings
    append_review_note(cfg, "Add retry", ["save.py"], "Retries must be logged.")
    notes, _ = collect_notes(cfg)
    assert ".carrier/notes.md" in [n.path for n in notes]


def test_criteria_builtins_and_project_override(repo: Path):
    cfg = load_config(repo)
    names = {c.name for c in load_criteria(cfg)}
    assert {"design-drift", "scope-creep", "tests-weakened", "security"} <= names
    folder = repo / ".carrier" / "criteria"
    folder.mkdir(parents=True)
    (folder / "security.md").write_text("---\nseverity: low\nenabled: false\n---\nnope\n")
    (folder / "save-compat.md").write_text("---\ntitle: Save compat\nseverity: high\n---\nCheck saves.\n")
    active = {c.name: c for c in load_criteria(cfg)}
    assert "security" not in active  # project file disabled the built-in
    assert active["save-compat"].severity == "high" and active["save-compat"].prompt == "Check saves."


def test_frontmatter_comments_and_bools():
    fields, body = parse_frontmatter("---\nname: x  # comment\nexplore: yes\n---\nBody")
    assert fields == {"name": "x", "explore": True} and body == "Body"


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Sure!\n```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json('Here: {"a": 3} hope that helps') == {"a": 3}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_sessions_started_in_subfolder_are_found(repo: Path, claude_home: Path):
    import json

    from carrier.debrief.transcripts import project_key, projects_dir

    (repo / "src").mkdir()
    sub = projects_dir() / project_key(repo / "src")
    sub.mkdir()
    entry = {"type": "assistant", "sessionId": "s2", "cwd": str(repo / "src"),
             "timestamp": "2099-01-02T00:00:00Z",
             "message": {"content": [{"type": "tool_use", "name": "Write",
                                      "input": {"file_path": str(repo / "src" / "Main.java"),
                                                "content": "x"}}]}}
    sub.joinpath("s2.jsonl").write_text(json.dumps(entry) + "\n")
    # A sibling project sharing the prefix must NOT be included.
    sib = projects_dir() / (project_key(repo) + "-old")
    sib.mkdir()
    sib.joinpath("s3.jsonl").write_text(json.dumps({**entry, "cwd": str(repo) + "-old"}) + "\n")
    paths = [e.path for e in collect_events(repo, None)]
    assert "src/Main.java" in paths  # case preserved
    assert len([p for p in paths if p == "src/Main.java"]) == 1
