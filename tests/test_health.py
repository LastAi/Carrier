# SPDX-License-Identifier: AGPL-3.0-only
"""Debrief health checks (debrief/health.py)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from carrier.core.db import Store
from carrier.core.llm import FakeBackend
from carrier.debrief import health
from carrier.readyroom.manager import ReadyRoom


def _git(d: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=d, check=True, capture_output=True)


@pytest.fixture
def proj(tmp_path: Path):
    main = tmp_path / "app"
    (main / "src").mkdir(parents=True)
    (main / "src" / "save.py").write_text("def load(p):\n    try:\n        return open(p).read()\n    except Exception:\n        pass\n")
    (main / "src" / "util.py").write_text("TIMEOUT = 30\n")
    (main / "logo.png").write_bytes(b"\x89PNG\0\0binary")
    (main / "package-lock.json").write_text("{}")
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"],
                 ["add", "-A"], ["commit", "-qm", "start"]):
        _git(main, *args)
    store = Store(tmp_path / "c.db")
    room = ReadyRoom(store)
    pid = room.create_project("App", str(main), rules="- Never swallow errors.")["id"]
    return store, room, pid, main


def handler_for(findings_by_agent: dict[str, list[dict]]):
    def handler(system: str, prompt: str) -> str:
        for title, found in findings_by_agent.items():
            if prompt.startswith(f"# Health check: {title}"):
                return json.dumps({"findings": found})
        return json.dumps({"findings": []})
    return handler


def test_agents_and_settings(proj):
    store, _room, pid, _main = proj
    names = [a.name for a in health.builtin_agents()]
    assert len(names) == 12 and len(set(names)) == 12
    by = {a.name: a for a in health.builtin_agents()}
    assert by["security"].due == "always" and by["rules"].due == "rules" and by["structure"].mode == "project"
    health.set_option(store, None, "security", "days", 7)
    health.set_option(store, pid, "security", "model", "opus")
    a = {a.name: a for a in health.agents(store, pid)}["security"]
    assert a.days == 7 and a.model == "opus"
    health.set_option(store, pid, "security", "model", "")
    assert {a.name: a for a in health.agents(store, pid)}["security"].model is None
    for bad in (("nope", "days", 3), ("security", "colour", 1), ("security", "days", 0), ("security", "effort", "x")):
        with pytest.raises(ValueError):
            health.set_option(store, pid, *bad)


def test_scope_is_the_projects_own_text_files(proj):
    store, _room, pid, main = proj
    scope = health.project_scope(store, pid)
    assert sorted(f.path for f in scope.files) == ["src/save.py", "src/util.py"]
    assert scope.skipped["generated, lock or binary files"] == 2


def test_run_compare_and_decide(proj):
    store, room, pid, main = proj
    swallow = {"file": "src/save.py", "line": 4, "title": "Errors swallowed when loading",
               "detail": "except Exception: pass hides failures", "severity": "high"}
    backend = FakeBackend(handler_for({"Error handling and robustness": [swallow],
                                       "Structure and boundaries": [
                                           {"file": "nowhere.py", "title": "Not this project's file"},
                                           {"title": "Odd layout"}]}))
    log: list[str] = []
    asyncio.run(health.run(store, pid, backend, ["robustness", "structure"], log.append))
    rep = health.report(store, pid)
    found = {f["title"]: f for t in rep["themes"] for f in t["findings"]}
    assert found["Errors swallowed when loading"]["new"] and found["Odd layout"]["file"] == ""
    assert "Not this project's file" not in found  # a file that isn't the project's: dropped
    # The files agent saw numbered lines and no tools; the project agent explores read-only.
    calls = {c["prompt"].split("\n", 1)[0]: c for c in backend.calls}
    files_call = calls["# Health check: Error handling and robustness"]
    assert "    4         pass" not in files_call["prompt"] and "    4     except Exception:" in files_call["prompt"]
    assert files_call["tools"] is None and "Never swallow errors." in files_call["prompt"]
    assert calls["# Health check: Structure and boundaries"]["tools"] == ["Read", "Grep", "Glob"]
    assert "src/save.py" in calls["# Health check: Structure and boundaries"]["prompt"]

    # Found again (reworded): the same finding, no longer new. Accepted ones stay quiet.
    fid = found["Errors swallowed when loading"]["id"]
    odd = found["Odd layout"]["id"]
    health.act(store, pid, odd, "accept")
    again = dict(swallow, title="Errors are swallowed when loading the file")
    backend2 = FakeBackend(handler_for({"Error handling and robustness": [again],
                                        "Structure and boundaries": [{"title": "Odd layout"}]}))
    asyncio.run(health.run(store, pid, backend2, ["robustness", "structure"]))
    found = {f["id"]: f for t in health.report(store, pid)["themes"] for f in t["findings"]}
    assert found[fid]["status"] == "open" and not found[fid]["new"]
    assert found[odd]["status"] == "accepted"
    # Not found again: "gone"; found later: open again, and new again.
    asyncio.run(health.run(store, pid, FakeBackend(handler_for({})), ["robustness"]))
    assert {f["id"]: f for t in health.report(store, pid)["themes"] for f in t["findings"]}[fid]["status"] == "gone"
    asyncio.run(health.run(store, pid, backend, ["robustness"]))
    back = {f["id"]: f for t in health.report(store, pid)["themes"] for f in t["findings"]}[fid]
    assert back["status"] == "open" and back["new"]
    # Make a task of it: marked as written by a check, not by the user.
    health.act(store, pid, fid, "task", add_task=lambda title, details: room.add_task(pid, title, details)
               ["tasks"][-1]["id"])
    t = [t for t in room.project(pid)["tasks"] if t["title"] == "Errors swallowed when loading"][0]
    assert "src/save.py:4" in t["details"] and "Error handling and robustness" in t["details"]
    assert "written by an AI check" in t["details"]
    with pytest.raises(ValueError):
        health.act(store, pid, fid, "explode")


def test_due(proj):
    store, room, pid, main = proj
    view = {a["name"]: a for a in health.view(store, pid)["agents"]}
    assert all(a["due"] and a["why"] == "never checked" for a in view.values())
    asyncio.run(health.run(store, pid, FakeBackend(handler_for({})), ["robustness", "security", "rules"]))
    view = {a["name"]: a for a in health.view(store, pid)["agents"]}
    assert not view["robustness"]["due"] and not view["security"]["due"] and not view["rules"]["due"]
    # Rules changed: the rules check is due at once.
    room.update_project(pid, rules="- Never swallow errors.\n- Log every failure.")
    assert {a["name"]: a for a in health.view(store, pid)["agents"]}["rules"]["due"]
    # Time passes: security is due whether or not the code changed; robustness only after changes.
    past = time.time() - 40 * 86400
    store._x("UPDATE health_runs SET started=?, finished=?", (past, past))
    view = {a["name"]: a for a in health.view(store, pid)["agents"]}
    assert view["security"]["due"] and not view["robustness"]["due"] and "no changes" in view["robustness"]["why"]
    (main / "src" / "util.py").write_text("TIMEOUT = 45\n")
    os.utime(main / "src" / "util.py", (time.time() + 5, time.time() + 5))
    assert {a["name"]: a for a in health.view(store, pid)["agents"]}["robustness"]["due"]
    assert health.due_count(store, pid) >= 2


def test_budget_and_estimate(proj):
    store, _room, pid, main = proj
    (main / "src" / "big.py").write_text("x = 1\n" * 4000)  # ~24 KB
    health.set_option(store, pid, "performance", "budget_kb", 20)
    a = {a.name: a for a in health.agents(store, pid)}["performance"]
    scope = health.project_scope(store, pid)
    chosen, left = health._budget_files(a, scope.files)
    assert left == 1 and "src/big.py" not in {f.path for f in chosen}
    assert health.estimate(a, scope) < health.estimate({x.name: x for x in health.agents(store, pid)}["robustness"],
                                                       scope)


def test_cancel(proj):
    import threading

    store, _room, pid, _main = proj

    class Slow:
        async def ask(self, **_kw):
            await asyncio.sleep(5)
            return "{}"

    ev = threading.Event()
    threading.Timer(0.5, ev.set).start()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(health.run(store, pid, Slow(), ["security"], cancel=ev))
    assert store._q("SELECT status FROM health_runs")[0]["status"] == "cancelled"


def test_findings_edge_cases(proj, tmp_path):
    store, _room, pid, main = proj
    (main / ".github").mkdir()
    (main / ".github" / "ci.yml").write_text("on: push\n")
    scope = health.project_scope(store, pid)
    a = {x.name: x for x in health.agents(store, pid)}["dead-code"]
    reply = json.dumps({"findings": [
        {"file": ".github/ci.yml", "title": "dotfile"},
        {"file": "./src/save.py", "title": "dot slash"},
        {"file": str(main / "src" / "util.py"), "title": "absolute"},
        {"file": str(tmp_path / "elsewhere.py"), "title": "outside"}]})
    got = {f["title"]: f["file"] for f in health.parse_findings(reply, a, scope)}
    assert got == {"dotfile": ".github/ci.yml", "dot slash": "src/save.py", "absolute": "src/util.py"}
    # Twice in one run (two parts reporting the same thing), and titles that differ in a short word.
    dup = [{"file": "src/save.py", "line": 1, "title": "Unused import os", "detail": "", "suggestion": "",
            "severity": "low"}]
    other = [dict(dup[0], title="Unused import re")]
    counts = health.merge_findings(store, pid, "dead-code", 1, dup + dup + other, None)
    assert counts["new"] == 2
    # A reworded title far from the old line is a different problem.
    moved = [dict(dup[0], title="Unused import os here", line=200)]
    assert health.merge_findings(store, pid, "dead-code", 2, dup + other + moved, None)["new"] == 1


def test_failed_parts_dont_mark_findings_gone(proj):
    store, _room, pid, main = proj
    for i in range(3):
        (main / f"f{i}.py").write_text(("x = 1  # padding\n" * 4000))  # each its own part
    seen = [{"file": "f1.py", "line": 1, "title": "Problem in f1", "severity": "low"}]
    asyncio.run(health.run(store, pid, FakeBackend(handler_for({"Performance hotspots": seen})), ["performance"]))

    def flaky(system: str, prompt: str) -> str:
        if "### f1.py" in prompt:
            raise health.BackendError("timed out")
        return json.dumps({"findings": []})

    asyncio.run(health.run(store, pid, FakeBackend(flaky), ["performance"]))
    f = [f for t in health.report(store, pid)["themes"] for f in t["findings"]][0]
    assert f["status"] == "open"
    last = store._q("SELECT status, error FROM health_runs ORDER BY id DESC LIMIT 1")[0]
    assert last["status"] == "partial" and "failed" in last["error"]
    assert {a["name"]: a for a in health.view(store, pid)["agents"]}["performance"]["due"] is False  # earlier ok run
    # One check crashing doesn't stop the others or leave runs "running".

    def boom(system: str, prompt: str) -> str:
        if "Security sweep" in prompt:
            raise RuntimeError("unexpected")
        return json.dumps({"findings": []})

    res = asyncio.run(health.run(store, pid, FakeBackend(boom), ["security", "robustness"]))
    assert {r["agent"]: r["ok"] for r in res} == {"security": False, "robustness": True}
    assert not store._q("SELECT id FROM health_runs WHERE status='running'")
    store._x("INSERT INTO health_runs (project_id, agent, started) VALUES (?,?,?)", (pid, "tests", time.time()))
    health.clear_stale(store)
    assert not store._q("SELECT id FROM health_runs WHERE status='running'")


def test_code_state_follows_content(proj):
    store, _room, pid, main = proj
    before = health.code_state(health.project_scope(store, pid, text_only=False))
    f = main / "src" / "util.py"
    os.utime(f, (time.time() + 100, time.time() + 100))  # touched, same content
    assert health.code_state(health.project_scope(store, pid, text_only=False)) == before
    f.write_text("TIMEOUT = 31\n")
    changed = health.code_state(health.project_scope(store, pid, text_only=False))
    assert changed != before
    f.write_text("TIMEOUT = 30\n")  # back as it was
    assert health.code_state(health.project_scope(store, pid, text_only=False)) == before


def test_group_defaults_and_own_checks(proj):
    store, _room, pid, main = proj
    health.set_option(store, None, "*", "model", "haiku")
    health.set_option(store, None, "security", "model", "opus")
    health.set_option(store, pid, "*", "effort", "high")
    by = {a.name: a for a in health.agents(store, pid)}
    assert by["tests"].model == "haiku" and by["security"].model == "opus"  # its own setting wins
    assert by["tests"].effort == "high" and by["security"].effort == "high"
    health.set_option(store, pid, "*", "model", "sonnet")  # the project's group setting beats the global one
    assert {a.name: a for a in health.agents(store, pid)}["tests"].model == "sonnet"
    assert {a.name: a for a in health.agents(store, pid)}["security"].model == "sonnet"
    with pytest.raises(ValueError):
        health.set_option(store, None, "*", "days", 3)  # each check keeps its own schedule
    # Own checks: for every project, and for one project.
    g = health.save_custom(store, {"title": "Save compatibility", "prompt": "Old saves must load.",
                                   "theme": "Reliability", "mode": "files", "due": "always", "days": 7})
    p = health.save_custom(store, {"title": "Shader budget", "prompt": "Too many passes.", "mode": "project"}, pid=pid)
    by = {a.name: a for a in health.agents(store, pid)}
    assert by[g].source == "global" and by[g].due == "always" and by[p].source == "project"
    assert by[g].model is None  # group settings don't change your own checks
    health.set_option(store, pid, g, "enabled", False)  # off for this project only
    assert not {a.name: a for a in health.agents(store, pid)}[g].enabled
    assert {a.name: a for a in health.agents(store, None)}[g].enabled
    v = health.ready_room_view(store, pid)
    assert [c["name"] for c in v["custom"]] == [p] and v["global_custom"][0]["enabled_here"] is False
    assert v["group"]["inherited"]["model"] == "haiku" and v["group"]["own"]["model"] == "sonnet"
    # It runs like a built-in.
    found = [{"file": "src/save.py", "line": 3, "end_line": 4, "title": "Old saves fail", "severity": "high"}]
    asyncio.run(health.run(store, pid, FakeBackend(handler_for({"Save compatibility": found})), [g]))
    f = [f for t in health.report(store, pid)["themes"] for f in t["findings"]][0]
    assert f["agent_title"] == "Save compatibility" and f["end_line"] == 4
    x = health.excerpt(store, pid, f["id"], around=1)
    assert x["start"] == 2 and x["focus"] == [3, 4] and x["lines"][1].strip() == "return open(p).read()"
    with pytest.raises(ValueError):
        health.save_custom(store, {"title": "x", "prompt": "y", "mode": "nope"})
    health.delete_custom(store, p, pid)
    assert p not in {a.name for a in health.agents(store, pid)}


def test_ready_room_shows_what_each_check_inherits(proj):
    store, _room, pid, _main = proj
    health.set_option(store, None, "*", "model", "haiku")
    assert {b["name"]: b for b in health.ready_room_view(store, None)["builtins"]}["tests"]["inherited"]["model"] == "haiku"
    health.set_option(store, pid, "*", "model", "opus")
    assert {b["name"]: b for b in health.ready_room_view(store, pid)["builtins"]}["tests"]["inherited"]["model"] == "opus"


def test_review_fixes(proj, tmp_path):
    store, room, pid, main = proj
    # Names are never shared or reused, so findings and settings stay with their check.
    a = health.save_custom(store, {"title": "Foo", "prompt": "x"}, pid=pid)
    b = health.save_custom(store, {"title": "Foo", "prompt": "x"})
    assert a != b
    health.set_option(store, pid, b, "enabled", False)
    found = [{"file": "src/save.py", "line": 2, "title": "Foo problem", "severity": "low"}]
    asyncio.run(health.run(store, pid, FakeBackend(handler_for({"Foo": found})), [a]))
    health.delete_custom(store, a, pid)
    titles = [f["agent_title"] for t in health.report(store, pid)["themes"] for f in t["findings"]]
    assert titles == ["(a deleted check)"]  # findings stay, under a deleted check
    health.delete_custom(store, b)
    assert b not in health.options(store, pid)  # its settings went with it
    # A decision can't be made twice (a held key, a double click).
    fid = [f for t in health.report(store, pid)["themes"] for f in t["findings"]][0]["id"]
    health.act(store, pid, fid, "task", add_task=lambda t, d: 1)
    with pytest.raises(ValueError):
        health.act(store, pid, fid, "task", add_task=lambda t, d: 2)
    # Lines far down a file stay in view however much context is asked for.
    (main / "long.py").write_text("".join(f"x{i} = {i}\n" for i in range(1, 2001)))
    long = [{"file": "long.py", "line": 1000, "end_line": 1002, "title": "Deep", "severity": "low"}]
    asyncio.run(health.run(store, pid, FakeBackend(handler_for({"Performance hotspots": long})), ["performance"]))
    deep = [f for t in health.report(store, pid)["themes"] for f in t["findings"] if f["title"] == "Deep"][0]
    x = health.excerpt(store, pid, deep["id"], around=400)
    assert x["start"] <= 1000 and x["start"] + len(x["lines"]) - 1 >= 1002
    # A link out of the project is never read.
    secret = tmp_path / "secret.txt"
    secret.write_text("TOKEN=abc\n")
    (main / "notes.txt").symlink_to(secret)
    assert "notes.txt" not in {f.path for f in health.project_scope(store, pid).files}
