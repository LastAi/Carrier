# SPDX-License-Identifier: AGPL-3.0-only
"""Guided conversations (carrier/guided.py): a health walk-through and task tidying."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from carrier.core.db import Store
from carrier.core.llm import FakeBackend
from carrier.debrief import health
from carrier.guided import Guide
from carrier.readyroom.manager import ReadyRoom


@pytest.fixture
def proj(tmp_path: Path):
    main = tmp_path / "app"
    main.mkdir()
    (main / "save.py").write_text("def load(p):\n    try:\n        return open(p).read()\n    except Exception:\n        pass\n")
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=main, check=True)
    store = Store(tmp_path / "c.db")
    room = ReadyRoom(store)
    pid = room.create_project("App", str(main), first_task="Ship it")["id"]
    return store, room, pid


def replying(*replies: dict):
    it = iter(replies)
    seen: list[str] = []

    def handler(system: str, prompt: str) -> str:
        seen.append(prompt)
        return json.dumps(next(it))
    handler.seen = seen  # type: ignore[attr-defined]
    return handler


def _findings(store, pid):
    for i, title in enumerate(["Errors swallowed", "No retry on lock", "Odd name"], 1):
        store._x("INSERT INTO health_findings (project_id, agent, key, file, line, title, detail, severity, "
                 "first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (pid, "robustness", f"k{i}", "save.py", i, title, "detail", "medium", 1.0, 1.0))
    return [r["id"] for r in store._q("SELECT id FROM health_findings ORDER BY id")]


def test_health_walkthrough(proj):
    store, room, pid = proj
    f1, f2, f3 = _findings(store, pid)
    ship = room.project(pid)["tasks"][0]["id"]
    h = replying(
        {"message": f"Two groups. First: F{f1} and F{f2} are the same problem.", "show": [f"F{f1}", f"F{f2}"],
         "proposals": [{"kind": "task", "ids": [f"F{f1}", f"F{f2}", "F999"], "title": "Make loading robust",
                        "details": "Handle failures"},
                       {"kind": "dismiss", "ids": [f"F{f3}"], "why": "a style nit"},
                       {"kind": "bogus", "ids": [f"F{f3}"]}]},
        {"message": "Then add it to the shipping task.",
         "proposals": [{"kind": "add_to_task", "ids": [f"F{f3}"], "task": f"#{ship}", "title": "Naming"}]})
    g = Guide(store, "health", pid)
    backend = FakeBackend(h)
    turn = asyncio.run(g.say(backend, ""))
    assert turn["show"] == [f"F{f1}", f"F{f2}"]
    kinds = [p["kind"] for p in turn["proposals"]]
    assert kinds == ["task", "dismiss"] and "left out" in turn["text"]  # unknown kind dropped, with a note
    task_p, dismiss_p = turn["proposals"]
    assert task_p["data"]["ids"] == [f1, f2]  # F999 isn't in the list
    # The prompt has the findings and the project's open tasks; Claude may read the code (read only).
    assert f"F{f1}" in h.seen[0] and f"#{ship}" in h.seen[0]
    assert backend.calls[0]["tools"] == ["Read", "Grep", "Glob"]
    # Applying with one finding unticked: one task for the rest.
    res = g.apply(task_p["id"], {"ids": [f1]})
    assert res["status"] == "applied" and "task #" in res["result"]
    t = [t for t in room.project(pid)["tasks"] if t["title"] == "Make loading robust"][0]
    assert "Errors swallowed" in t["details"] and "No retry" not in t["details"] and "verify each" in t["details"]
    status = {r["id"]: r["status"] for r in store._q("SELECT id, status FROM health_findings")}
    assert status[f1] == "tasked" and status[f2] == "open"
    with pytest.raises(ValueError):
        g.apply(task_p["id"])  # once only
    g.skip(dismiss_p["id"])
    turn2 = asyncio.run(g.say(backend, "skip the dismissal, add it to shipping instead"))
    assert "P" in h.seen[1] and "skipped" in h.seen[1]  # Claude sees what happened to its proposals
    g.apply(turn2["proposals"][0]["id"])
    assert "Naming" in store.task(ship)["details"]
    assert {r["id"]: r["status"] for r in store._q("SELECT id, status FROM health_findings")}[f3] == "tasked"


def test_tidy_tasks(proj):
    store, room, pid = proj
    for title in ("Fix save bug", "Save bug: handle locks", "Old idea", "Docs"):
        room.add_task(pid, title, f"{title} details")
    ids = {t["title"]: t["id"] for t in room.project(pid)["tasks"]}
    h = replying({"message": "Merge the save tasks, drop the old idea, move docs to a new group.",
                  "proposals": [
                      {"kind": "merge", "into": f"#{ids['Fix save bug']}", "ids": [f"#{ids['Save bug: handle locks']}"],
                       "title": "Fix saving", "why": "same bug"},
                      {"kind": "drop", "ids": [f"#{ids['Old idea']}"], "why": "superseded"},
                      {"kind": "move", "ids": [f"#{ids['Docs']}"], "queue": "new:Docs", "priority": "low"},
                      {"kind": "order", "queue": "main", "ids": [f"#{ids['Fix save bug']}", f"#{ids['Ship it']}"]},
                      {"kind": "merge", "into": "#99999", "ids": [f"#{ids['Docs']}"]}]})
    g = Guide(store, "tasks", pid)
    turn = asyncio.run(g.say(FakeBackend(h), ""))
    assert [p["kind"] for p in turn["proposals"]] == ["merge", "drop", "move", "order"]
    for p in turn["proposals"]:
        g.apply(p["id"])
    p = room.project(pid)
    open_titles = [t["title"] for t in p["tasks"]]
    assert "Fix saving" in open_titles and "Save bug: handle locks" not in open_titles and "Old idea" not in open_titles
    assert open_titles[0] == "Fix saving"
    docs = [t for t in p["tasks"] if t["title"] == "Docs"][0]
    assert store.task_group(docs["group_id"])["name"] == "Docs"
    dropped = store.task(ids["Old idea"])
    assert dropped["status"] == "dropped" and "superseded" in dropped["summary"]
    hist = room.task_history(ids["Old idea"])
    assert any(h["kind"] == "dropped" and h["by"] == "claude" for h in hist)


def test_findings_handed_to_a_linked_project(proj, tmp_path):
    store, room, pid = proj
    other = tmp_path / "lib"
    other.mkdir()
    lib = room.create_project("Lib", str(other))["id"]
    f1, f2, _ = _findings(store, pid)
    h = replying({"message": "Lib should fix this.", "proposals": [
        {"kind": "task", "ids": [f"F{f1}"], "title": "Handle empty files", "project": "Lib"},
        {"kind": "task", "ids": [f"F{f2}"], "title": "Elsewhere", "project": "Nope"}]})
    g = Guide(store, "health", pid)
    turn = asyncio.run(g.say(FakeBackend(h), ""))
    assert len(turn["proposals"]) == 0  # not linked yet: both left out
    room.add_link(pid, lib, "App reads Lib's files")
    g2 = Guide(store, "health", pid)
    h2 = replying({"message": "Lib should fix this.", "proposals": [
        {"kind": "task", "ids": [f"F{f1}"], "title": "Handle empty files", "project": "Lib"}]})
    turn = asyncio.run(g2.say(FakeBackend(h2), ""))
    assert "Linked projects" in h2.seen[0] and "Lib" in h2.seen[0]
    res = g2.apply(turn["proposals"][0]["id"])
    assert "in Lib" in res["result"]
    t = [t for t in room.project(lib)["tasks"] if t["title"] == "Handle empty files"][0]
    assert "health check of App" in t["details"]
    rep = health.report(store, pid)
    f = [x for th in rep["themes"] for x in th["findings"] if x["id"] == f1][0]
    assert f["status"] == "tasked" and f["task_project"] == "Lib"


def test_proposals_are_checked_again_when_applied(proj):
    store, room, pid = proj
    f1, f2, _ = _findings(store, pid)
    for title in ("A", "B", "C"):
        room.add_task(pid, title, f"{title} details")
    ids = {t["title"]: t["id"] for t in room.project(pid)["tasks"]}
    # Health: a finding decided after the card was shown isn't taken over; the card stays open.
    g = Guide(store, "health", pid)
    turn = asyncio.run(g.say(FakeBackend(replying({"message": "x", "proposals": [
        {"kind": "task", "ids": [f"F{f1}", f"F{f2}"], "title": "T"}]})), ""))
    prop = turn["proposals"][0]["id"]
    health.act(store, pid, f1, "dismiss")
    with pytest.raises(ValueError, match=f"F{f1}"):
        g.apply(prop)
    assert g.proposals[prop].status == "open"
    assert {r["id"]: r["status"] for r in store._q("SELECT id, status FROM health_findings")}[f1] == "dismissed"
    g.apply(prop, {"ids": [f2]})  # untick it: fine
    # A dismissed finding can't be proposed at all.
    g2 = Guide(store, "health", pid)
    turn = asyncio.run(g2.say(FakeBackend(replying({"message": "x", "proposals": [
        {"kind": "task", "ids": [f"F{f1}"], "title": "T2"}]})), ""))
    assert turn["proposals"] == []
    # Tasks: a split must say what stays; a merge of a task closed meanwhile is refused, nothing changes.
    t = Guide(store, "tasks", pid)
    turn = asyncio.run(t.say(FakeBackend(replying({"message": "x", "proposals": [
        {"kind": "split", "id": f"#{ids['A']}", "title": "A2", "details": "part"},
        {"kind": "merge", "into": f"#{ids['B']}", "ids": [f"#{ids['C']}"]}]})), ""))
    assert [p["kind"] for p in turn["proposals"]] == ["merge"]
    merge = turn["proposals"][0]["id"]
    room.drop_task(ids["C"], "not needed")
    with pytest.raises(ValueError):
        t.apply(merge)
    assert t.proposals[merge].status == "open" and "C details" not in store.task(ids["B"])["details"]


def test_reordering_keeps_running_tasks_first(proj, monkeypatch):
    store, room, pid = proj
    for title in ("A", "B"):
        room.add_task(pid, title)
    ids = [t["id"] for t in room.project(pid)["tasks"]]
    running = ids[0]
    monkeypatch.setattr(type(room), "_active_sessions", lambda self, pid=None, task_id=None:
                        [1] if task_id == running else [])
    room.reorder_queue(pid, None, list(reversed(ids)))
    assert room.project(pid)["tasks"][0]["id"] == running
