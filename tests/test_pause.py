# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Pausing a session, what it hears when it wakes, watching reference folders, and handoff notes."""

import time
from pathlib import Path

import pytest

from carrier.flightdeck import coordination
from carrier.flightdeck.manager import DeckError, FlightDeck

from .fakeclaude import ALL_CLIENTS, FakeClaudeClient
from .test_flightdeck import events, make_repo, wait


@pytest.fixture
def deck(tmp_path: Path):
    ALL_CLIENTS.clear()
    d = FlightDeck(db_path=tmp_path / "fd.db", client_factory=FakeClaudeClient)
    yield d
    d.close()


def project(deck: FlightDeck, path: Path, files: dict[str, str]) -> int:
    make_repo(path, files)
    deck.add_project(str(path))
    return next(p["id"] for p in deck.store.projects() if Path(p["path"]) == path.resolve())


def test_pause_sleeps_and_wakes_with_whats_changed(deck: FlightDeck, tmp_path: Path):
    pid = project(deck, tmp_path / "Game", {"a.txt": "a\n", "b.txt": "b\n"})
    deck.room.add_task(pid, "first job")
    t1 = deck.store.tasks(pid)[0]
    s1 = deck.start_task(t1["id"], mode="acceptEdits", send_task=False)
    deck.send(s1["id"], "write a.txt mine")
    wait(deck, s1["id"])
    deck.pause_session(s1["id"])
    row = deck.store.session(s1["id"])
    assert row["status"] == "paused" and s1["id"] not in deck.runners
    assert deck.store.task(t1["id"])["status"] == "active"  # the task stays with the paused session
    # One session per task: a second can't start while this one is open.
    with pytest.raises(DeckError, match="already has a session"):
        deck.start_task(t1["id"])
    # Asleep: another session isn't warned about its recent edits.
    deck.room.add_task(pid, "second job")
    t2 = [t for t in deck.store.tasks(pid) if t["title"] == "second job"][0]
    s2 = deck.start_task(t2["id"], mode="acceptEdits", send_task=False)
    assert coordination.check_edit(deck.store, deck.store.session(s2["id"]), "a.txt") == []
    deck.send(s2["id"], "write b.txt theirs")  # another session changes a file...
    wait(deck, s2["id"])
    (tmp_path / "Game" / "c.txt").write_text("by hand\n")  # ...and so do you
    # Waking: Claude is told what changed, and by whom where Carrier knows.
    deck.resume_session(s1["id"])
    assert any("2 files changed" in e["text"] for e in events(deck, s1["id"], "notice"))
    deck.send(s1["id"], "hello")
    wait(deck, s1["id"])
    told = "\n".join(ALL_CLIENTS[-1].contexts)
    assert "b.txt (changed, by" in told and "second job" in told
    assert "c.txt (new)" in told and "Re-read" in told
    assert deck.store.session(s1["id"])["paused_at"] is None


def test_closing_carrier_pauses_and_wakes_the_same_way(tmp_path: Path):
    ALL_CLIENTS.clear()
    d = FlightDeck(db_path=tmp_path / "fd.db", client_factory=FakeClaudeClient)
    pid = project(d, tmp_path / "Game", {"a.txt": "a\n"})
    s = d.start_session(pid, "job", mode="acceptEdits", send_task=False)
    d.close()
    (tmp_path / "Game" / "a.txt").write_text("changed while closed\n")
    d2 = FlightDeck(db_path=tmp_path / "fd.db", client_factory=FakeClaudeClient)
    try:
        d2.resume_session(s["id"])
        d2.send(s["id"], "hello")
        wait(d2, s["id"])
        assert "a.txt (changed)" in "\n".join(ALL_CLIENTS[-1].contexts)
    finally:
        d2.close()


def test_watching_reference_folders(deck: FlightDeck, tmp_path: Path):
    anim = project(deck, tmp_path / "Animation", {"formats/anim.md": "v1\n"})
    editor = project(deck, tmp_path / "ShipEditor", {"main.txt": "x\n"})
    deck.room.add_folder(editor, str(tmp_path / "Animation"), kind="reference")
    deck.room.set_watch_refs(editor, True)
    se = deck.start_session(editor, "editor work", send_task=False)
    deck.send(se["id"], "hello")  # the first look: nothing to compare with yet
    wait(deck, se["id"])
    sa = deck.start_session(anim, "new format", mode="acceptEdits", send_task=False)
    deck.send(sa["id"], "write formats/anim.md v2")
    wait(deck, sa["id"])
    # Right away in the page; with the next message for Claude.
    assert any("changed formats/anim.md in a reference folder" in e["text"].replace("Animation/", "")
               for e in events(deck, se["id"], "notice"))
    deck.send(se["id"], "carry on")
    wait(deck, se["id"])
    told = ALL_CLIENTS[0].contexts[-1]
    assert "reference folders" in told and "anim.md (changed, by" in told and "new format" in told
    # Off: not told.
    deck.room.set_watch_refs(editor, False)
    deck.send(sa["id"], "write formats/anim.md v3")
    wait(deck, sa["id"])
    deck.send(se["id"], "and again")
    wait(deck, se["id"])
    assert "anim.md" not in (ALL_CLIENTS[0].contexts[-1] if len(ALL_CLIENTS[0].contexts) > 1 else "")


def test_end_leaves_a_note_for_the_next_session(deck: FlightDeck, tmp_path: Path):
    pid = project(deck, tmp_path / "Game", {"a.txt": "a\n"})
    deck.room.add_task(pid, "big job")
    t = deck.store.tasks(pid)[0]
    s = deck.start_task(t["id"], send_task=False)
    deck.end_session(s["id"], handoff="- parser done\n- writer left")
    task = deck.store.task(t["id"])
    assert task["status"] == "planned" and task["handoff"].startswith("- parser done")
    assert "handoff" in [e["kind"] for e in deck.store.task_edits(t["id"])]
    s2 = deck.start_task(t["id"], send_task=False)  # ending frees the task for a new session
    append = ALL_CLIENTS[-1].options.system_prompt["append"]
    assert "Where the last session on this task left off" in append and "writer left" in append
    # Claude writes the note, then the session ends.
    deck.end_with_note(s2["id"])
    deadline = time.time() + 10
    while deck.store.session(s2["id"])["status"] != "ended" and time.time() < deadline:
        time.sleep(0.05)
    assert deck.store.session(s2["id"])["status"] == "ended"
    assert deck.store.task(t["id"])["handoff"].startswith("Done: [Carrier] The user is ending")
    # A note can be edited or removed in the Ready Room.
    deck.room.set_handoff(t["id"], "")
    assert deck.store.task(t["id"])["handoff"] == ""


def test_pause_refuses_while_working_and_twice(deck: FlightDeck, tmp_path: Path):
    pid = project(deck, tmp_path / "Game", {"a.txt": "a\n"})
    s = deck.start_session(pid, "job", send_task=False)
    deck.pause_session(s["id"])
    with pytest.raises(DeckError, match="already paused"):
        deck.pause_session(s["id"])


def test_catch_up_is_accurate(deck: FlightDeck, tmp_path: Path):
    """Found in review: changes credited to the wrong session, and reported twice."""
    pid = project(deck, tmp_path / "Game", {"a.txt": "a\n", "src/a.txt": "a\n"})
    s1 = deck.start_session(pid, "one", send_task=False)
    deck.send(s1["id"], "hello")
    wait(deck, s1["id"])
    deck.pause_session(s1["id"])
    s2 = deck.start_session(pid, "two", mode="acceptEdits", send_task=False)
    deck.send(s2["id"], "write src/a.txt theirs")
    wait(deck, s2["id"])
    (tmp_path / "Game" / "a.txt").write_text("by hand\n")
    deck.resume_session(s1["id"])
    deck.send(s1["id"], "hello again")
    wait(deck, s1["id"])
    told = "\n".join(ALL_CLIENTS[-1].contexts)  # the resumed connection
    assert "- a.txt (changed)" in told  # by hand: no session named
    assert "src/a.txt (changed, by" in told
    assert "edited src/a.txt since your last message" not in told  # not twice


def test_partial_scans_report_only_changes(monkeypatch, tmp_path: Path):
    from carrier.flightdeck import folderscan

    monkeypatch.setattr(folderscan, "MAX_FILES", 20)
    for i in range(30):
        (tmp_path / f"f{i:03}.txt").write_text("x")
    old = folderscan.manifest([str(tmp_path)])
    first = sorted(old["files"])[0]
    Path(first).unlink()
    d = folderscan.diff(old, folderscan.manifest([str(tmp_path)]))
    assert d["truncated"] and d["added"] == [] and d["removed"] == []


def test_turning_watching_off_forgets_the_old_look(deck: FlightDeck, tmp_path: Path):
    ref = project(deck, tmp_path / "Ref", {"x.md": "1\n"})
    pid = project(deck, tmp_path / "App", {"a.txt": "a\n"})
    deck.room.add_folder(pid, str(tmp_path / "Ref"), kind="reference")
    deck.room.set_watch_refs(pid, True)
    s = deck.start_session(pid, "job", send_task=False)
    deck.send(s["id"], "hello")
    wait(deck, s["id"])
    assert deck.store.manifest(s["id"], "refs") is not None
    deck.room.set_watch_refs(pid, False)
    assert deck.store.manifest(s["id"], "refs") is None
    del ref
