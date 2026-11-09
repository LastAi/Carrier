# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""A project's pre-approved actions: what sessions may do without asking."""

from pathlib import Path

import pytest

from carrier.flightdeck import sdk
from carrier.flightdeck.manager import FlightDeck
from carrier.readyroom.manager import RoomError

from .fakeclaude import ALL_CLIENTS, FakeClaudeClient
from .test_flightdeck import events, make_repo, wait


def test_matching(tmp_path: Path):
    work, ref, other = tmp_path / "work", tmp_path / "ref", tmp_path / "elsewhere"
    for d in (work, ref, other):
        d.mkdir()
    allow = {**sdk.ALLOW_DEFAULT, "read": True, "edit": True, "web_fetch": True, "sites": ["python.org"],
             "commands": ["git", "pytest"], "tools": ["mcp__gh__get_issue"]}

    def ok(tool: str, inp: dict) -> str:
        return sdk.preapproved(tool, inp, allow, work, [str(work), str(ref)], [str(work)])

    assert ok("Read", {"file_path": str(work / "a.py")}) == "read"
    assert ok("Read", {"file_path": str(ref / "b.md")}) == "read"
    assert ok("Grep", {"pattern": "x"}) == "read"  # no path: the session's folder
    assert ok("Glob", {"pattern": "*.py", "path": "src"}) == "read"  # relative to the session's folder
    assert ok("Read", {"file_path": str(other / "secret")}) == ""
    assert ok("Read", {"file_path": str(work / ".." / "elsewhere" / "x")}) == ""
    assert ok("Edit", {"file_path": str(work / "a.py")}) == "edit"
    assert ok("Write", {"file_path": str(ref / "b.md")}) == ""  # reference folders: reading only
    assert ok("WebFetch", {"url": "https://docs.python.org/3/"}) == "web_fetch"
    assert ok("WebFetch", {"url": "https://evilpython.org/"}) == ""
    assert ok("WebSearch", {"query": "x"}) == ""
    assert ok("Bash", {"command": "git status"}) == "commands"
    assert ok("Bash", {"command": "git status && rm -rf /"}) == ""  # chained: still asks
    assert ok("Bash", {"command": "pytest -q > out.txt"}) == ""
    assert ok("Bash", {"command": "git log $(whoami)"}) == ""
    assert ok("Bash", {"command": "npm install"}) == ""
    assert ok("mcp__gh__get_issue", {}) == "tools"
    assert sdk.project_allow("Bash", {"command": "git push"}) == (
        {"commands": ["git"]}, "Always allow simple `git` commands in this project")
    assert sdk.project_allow("mcp__carrier__ask_user", {}) is None


@pytest.fixture
def deck(tmp_path: Path):
    ALL_CLIENTS.clear()
    d = FlightDeck(db_path=tmp_path / "fd.db", client_factory=FakeClaudeClient)
    yield d
    d.close()


def test_sessions_use_the_projects_preapprovals(deck: FlightDeck, tmp_path: Path):
    p = make_repo(tmp_path / "Proj", {"a.txt": "a\n"})
    pid = deck.add_project(str(p))["projects"][0]["id"]
    with pytest.raises(RoomError):
        deck.room.set_allowances(pid, commands="git status")  # program names only
    s = deck.start_session(pid, "job", send_task=False)  # default mode: asks
    deck.send(s["id"], "write notes.md hi")
    wait(deck, s["id"], "needs_you")
    req = events(deck, s["id"], "permission")[0]
    assert req["project_label"] == "Always allow editing files in this project"
    deck.answer(s["id"], req["req_id"], "allow_project")
    wait(deck, s["id"])
    assert deck.room.allowances(pid)["edit"] is True
    import json

    assert "pre-approved actions — edit: on" in json.dumps(deck.store.project_log(pid), ensure_ascii=False)
    # A new session of the project doesn't ask any more.
    s2 = deck.start_session(pid, "job 2", send_task=False)
    deck.send(s2["id"], "write more.md yo")
    wait(deck, s2["id"])
    assert not events(deck, s2["id"], "permission") and (Path(s2["cwd"]) / "more.md").read_text() == "yo\n"
    # Turned off again: it asks again.
    deck.room.set_allowances(pid, edit=False)
    deck.send(s2["id"], "write more.md again")
    wait(deck, s2["id"], "needs_you")


def test_templates_copy_preapprovals(deck: FlightDeck, tmp_path: Path):
    p = make_repo(tmp_path / "Proj", {"a.txt": "a\n"})
    pid = deck.add_project(str(p))["projects"][0]["id"]
    deck.room.set_allowances(pid, read=True, commands="git, npm")
    extras = deck.room._template_extras(pid)
    q = make_repo(tmp_path / "Proj2", {"b.txt": "b\n"})
    pid2 = deck.add_project(str(q))["projects"][0]["id"]
    deck.room._apply_template_extras(pid2, extras)
    a = deck.room.allowances(pid2)
    assert a["read"] and a["commands"] == ["git", "npm"] and not a["edit"]
