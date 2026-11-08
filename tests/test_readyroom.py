# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Ready Room: projects, tasks, rules, presets, templates, tags, context."""

from pathlib import Path

import pytest

from carrier.core.db import Store
from carrier.readyroom.manager import ReadyRoom, RoomError, clean_tags


@pytest.fixture
def room(tmp_path: Path) -> ReadyRoom:
    return ReadyRoom(Store(tmp_path / "carrier.db"))


@pytest.fixture
def folders(tmp_path: Path):
    out = {}
    for name in ("server", "client", "scratch"):
        (tmp_path / name).mkdir()
        out[name] = tmp_path / name
    return out


def test_create_project_with_goal_task_tags(room: ReadyRoom, folders):
    p = room.create_project("API Server", str(folders["server"]), goal="Serve the public API.",
                            first_task="Add input validation", tags="backend, Backend, api ")
    assert p["goal"] == "Serve the public API." and p["tags"] == ["backend", "api"]
    assert [t["title"] for t in p["tasks"]] == ["Add input validation"]
    assert room.current_task(p["id"])["title"] == "Add input validation"
    with pytest.raises(RoomError, match="already a project"):
        room.create_project("dup", str(folders["server"]))
    with pytest.raises(RoomError, match="not found"):
        room.create_project("x", str(folders["server"] / "nope"))


def test_search_and_tag_filter(room: ReadyRoom, folders):
    room.create_project("API Server", str(folders["server"]), tags=["backend"])
    room.create_project("Web Client", str(folders["client"]), goal="Browser UI", tags=["frontend"])
    ov = room.overview()
    assert ov["tags"] == ["backend", "frontend"]
    assert [p["name"] for p in room.overview(tag="FRONTEND")["projects"]] == ["Web Client"]
    assert [p["name"] for p in room.overview(search="browser")["projects"]] == ["Web Client"]


def test_task_queue_order_and_lifecycle(room: ReadyRoom, folders):
    p = room.create_project("S", str(folders["server"]), first_task="one")
    pid = p["id"]
    room.add_task(pid, "two")
    room.add_task(pid, "urgent", first=True)
    assert [t["title"] for t in room.project(pid)["tasks"]] == ["urgent", "one", "two"]
    two = room.project(pid)["tasks"][2]["id"]
    room.move_task(two, "top")
    assert room.current_task(pid)["title"] == "two"
    room.move_task(two, "down")
    assert [t["title"] for t in room.project(pid)["tasks"]] == ["urgent", "two", "one"]
    room.finish_task(two, "Did two.")
    proj = room.project(pid)
    assert [t["title"] for t in proj["tasks"]] == ["urgent", "one"]
    assert proj["history"][0]["summary"] == "Did two."
    with pytest.raises(RoomError):
        room.add_task(pid, "  ")


def test_rules_presets_templates_and_preview(room: ReadyRoom, folders):
    rs = room.save_rule_set("No new deps", "Do not add dependencies without asking.")[0]
    room.set_global_rules("Write tests for new code.", [rs["id"]])
    preset = room.save_preset("Careful", model="opus", mode="default", policy="ask", rule_set_id=rs["id"])[0]
    tpl = room.save_template("Service", goal="A web service.", rules="Use type hints.", tags=["backend"],
                             first_task="Set up CI", preset_id=preset["id"])[0]
    p = room.create_project("", str(folders["server"]), template_id=tpl["id"])
    assert p["name"] == "server" and p["goal"] == "A web service." and p["tags"] == ["backend"]
    assert p["preset_id"] == preset["id"] and room.current_task(p["id"])["title"] == "Set up CI"
    room.finish_task(room.current_task(p["id"])["id"], "CI runs on push.")
    room.add_task(p["id"], "Add login", "Use the existing session store.")
    text = room.preview(p["id"])
    for expected in ("Project: server", "A web service.", "Current task\nAdd login", "existing session store",
                     "Project rules\nUse type hints.", "Global rules\nWrite tests", "No new deps",
                     "Recent work", "CI runs on push.", "Priority if anything conflicts"):
        assert expected in text, expected
    # Template from an existing project.
    names = [t["name"] for t in room.template_from_project(p["id"], "Copy")]
    assert "Copy" in names
    with pytest.raises(RoomError):
        room.save_preset("Bad", mode="nope")


def test_standalone_upgrade_and_assign(room: ReadyRoom, folders):
    store = room.store
    target = room.create_project("API Server", str(folders["server"]))
    tid = store.add_task("prototype parser", None, cwd=str(folders["scratch"]))
    with pytest.raises(RoomError, match="Finish the task first"):
        room.assign_standalone(tid, target["id"])
    room.finish_task(tid, "Parser works for simple input.")
    p = room.assign_standalone(tid, target["id"], add_merge_task=True)
    assert p["history"][0]["origin"] == "standalone" and p["history"][0]["origin_path"] == str(folders["scratch"])
    assert p["tasks"][0]["title"] == "Integrate standalone work: prototype parser"
    assert str(folders["scratch"]) in p["tasks"][0]["details"]
    assert "done as a standalone task" in room.preview(target["id"])

    tid2 = store.add_task("try a client", None, cwd=str(folders["client"]))
    up = room.upgrade_standalone(tid2, "Web Client", goal="Browser UI", tags=["frontend"])
    assert up["path"] == str(folders["client"].resolve()) and up["tasks"][0]["title"] == "try a client"
    assert room.standalone_tasks() == []


def test_links_and_project_update(room: ReadyRoom, folders):
    a = room.create_project("A", str(folders["server"]))
    b = room.create_project("B", str(folders["client"]))
    room.add_link(a["id"], b["id"], "B uses A's schema", "schema/**", "src/client/**")
    pa = room.project(a["id"])
    assert pa["links"][0]["other"] == "B" and pa["links"][0]["mine"] == ["schema/**"]
    room.update_project(b["id"], name="B2", rules="Keep it small.", rule_set_ids=[])
    assert room.project(b["id"])["name"] == "B2"
    with pytest.raises(RoomError):
        room.update_project(b["id"], name=" ")
    room.remove_project(b["id"])
    assert [p["name"] for p in room.overview()["projects"]] == ["A"]


def test_clean_tags():
    assert clean_tags(" a , A, b  c ,,") == ["a", "b c"]


def test_deleted_references_are_cleaned(room: ReadyRoom, folders):
    """Deleting a rule set or preset must not leave dangling ids behind (review finding)."""
    rs = room.save_rule_set("Strict", "Be strict.")[0]
    safe = room.save_preset("Safe", mode="plan")[0]
    tpl = room.save_template("Tpl", rule_set_ids=[rs["id"]], preset_id=safe["id"])[0]
    room.set_global_rules("", [rs["id"]])
    p = room.create_project("Server", str(folders["server"]), template_id=tpl["id"])
    room.delete_rule_set(rs["id"])
    room.delete_preset(safe["id"])
    assert room.store.template(tpl["id"])["rule_set_ids"] == []
    assert room.overview()["global"]["rule_set_ids"] == []
    risky = room.save_preset("Risky", mode="acceptEdits")[0]
    assert room.project(p["id"])["preset_id"] is None  # not silently the new preset
    q = room.create_project("Client", str(folders["client"]), template_id=tpl["id"])  # still works
    assert q["rule_set_ids"] == [] and q["preset_id"] is None and risky


def test_insert_first_keeps_active_task_current_and_move_done(room: ReadyRoom, folders):
    p = room.create_project("Server", str(folders["server"]), first_task="A")
    a = room.current_task(p["id"])
    room.store.update_task(a["id"], status="active")
    room.add_task(p["id"], "B")
    out = room.add_task(p["id"], "Urgent", first=True)
    assert [t["title"] for t in out["tasks"]] == ["A", "Urgent", "B"]
    room.finish_task(a["id"], "done")
    with pytest.raises(RoomError, match="open tasks"):
        room.move_task(a["id"], "up")


def test_migrates_old_sessions_table_without_breaking_foreign_keys(tmp_path: Path):
    import sqlite3

    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript("""
    CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL UNIQUE, created REAL NOT NULL);
    CREATE TABLE sessions (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
      task TEXT NOT NULL, cwd TEXT NOT NULL, worktree INTEGER NOT NULL DEFAULT 0, branch TEXT, base_ref TEXT,
      model TEXT, mode TEXT NOT NULL DEFAULT 'default', status TEXT NOT NULL, sdk_session TEXT,
      cost_usd REAL NOT NULL DEFAULT 0, started REAL NOT NULL, last_activity REAL NOT NULL,
      last_briefed REAL NOT NULL DEFAULT 0, ended REAL);
    CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id)
      ON DELETE CASCADE, ts REAL NOT NULL, body TEXT NOT NULL);
    INSERT INTO projects VALUES (1, 'A', '/a', 0);
    INSERT INTO sessions(id, project_id, task, cwd, status, started, last_activity) VALUES (1, 1, 't', '/a', 'ended', 0, 0);
    INSERT INTO messages VALUES (1, 1, 0, '{}');
    """)
    c.commit()
    c.close()
    store = Store(db)
    schema = " ".join(r["sql"] or "" for r in store._q("SELECT sql FROM sqlite_master"))
    assert "sessions_old" not in schema
    store.add_message(1, {"type": "user", "text": "hi"})  # foreign key still resolves
    assert store.add_session(project_id=None, task="standalone", cwd="/b")
    assert len(store._q("SELECT * FROM messages")) == 2
    Store(db)  # opening again is a no-op


def test_drafting_rules_and_agents():
    import json as _json

    from carrier.core.llm import FakeBackend
    from carrier.readyroom import drafting

    rules = FakeBackend(lambda s, p: "- Keep it small.")
    assert drafting.draft("global_rules", "small changes", rules, current="old rule")["text"] == "- Keep it small."
    assert "old rule" in rules.calls[0]["prompt"] and rules.calls[0]["tools"] is None  # no tools, ever
    agent = FakeBackend(lambda s, p: _json.dumps({"title": "Perf", "severity": "LOW", "prompt": "Flag slow loops."}))
    assert drafting.draft("agent", "catch slow code", agent) == {"title": "Perf", "severity": "low",
                                                                  "prompt": "Flag slow loops."}
    loose = FakeBackend(lambda s, p: "Flag slow loops.")  # model ignored the JSON format
    assert drafting.draft("agent", "x", loose)["prompt"] == "Flag slow loops."
    with pytest.raises(ValueError):
        drafting.draft("rule_set", "  ", rules)


def test_extra_folder_rules(room: ReadyRoom, folders, tmp_path: Path):
    """Main folders are exclusive; extra folders (work or reference) may be any
    other folder, including another project's main folder."""
    import subprocess

    a = room.create_project("A", str(folders["server"]))
    c = room.create_project("C", str(folders["client"]))
    with pytest.raises(RoomError, match="already a project's main folder"):
        room.create_project("Dup", str(folders["server"]))
    # Another project's main folder, as work or reference: allowed.
    p = room.add_folder(c["id"], str(folders["server"]), kind="reference")
    assert p["folders"][0]["kind"] == "reference"
    shared = tmp_path / "shared"
    shared.mkdir()
    room.add_folder(a["id"], str(shared))
    assert room.add_folder(c["id"], str(shared))["folders"][1]["kind"] == "work"  # two projects, one folder
    with pytest.raises(RoomError, match="already part"):
        room.add_folder(c["id"], str(shared))
    with pytest.raises(RoomError, match="Unknown folder type"):
        room.add_folder(a["id"], str(tmp_path), kind="scratch")
    # Reference folders don't count as work roots (not reviewed, not edited).
    roots = room.store.project_roots(room.store.project(c["id"]))
    assert [lbl for lbl, _ in roots] == ["", "shared"]
    # A label that is also a top-level name in the main folder gets a suffix.
    (folders["server"] / "lib").mkdir()
    lib = tmp_path / "other" / "lib"
    lib.mkdir(parents=True)
    assert [f["label"] for f in room.add_folder(a["id"], str(lib))["folders"]] == ["shared", "lib-2"]
    # A folder inside a git repository can be a work folder too (only it is reviewed).
    repo = tmp_path / "mono"
    (repo / "pkg").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    room.add_folder(a["id"], str(repo / "pkg"), kind="reference")
    room.set_folder_kind(a["id"], str((repo / "pkg").resolve()), "work")
    room.set_folder_kind(a["id"], str(lib.resolve()), "reference")
    assert next(f for f in room.project(a["id"])["folders"] if f["label"] == "lib-2")["kind"] == "reference"


def test_model_catalog_and_effort_fallback(tmp_path: Path, monkeypatch):
    from carrier.core import models

    monkeypatch.setenv("CARRIER_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(models, "_cache", None)
    raw = [{"value": "default", "displayName": "Default", "supportsEffort": True,
            "supportedEffortLevels": ["low", "medium", "high"]},
           {"value": "opus", "displayName": "Opus", "supportsEffort": True,
            "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
           {"value": "mini", "displayName": "Mini", "supportsEffort": False},
           {"value": "mid", "displayName": "Mid", "supportsEffort": True, "supportedEffortLevels": ["medium", "high"]}]
    found, default = models.from_server_info(raw)
    models.store(found, default)
    monkeypatch.setattr(models, "_cache", None)  # read back from models.json
    assert [m["value"] for m in models.catalog()["models"]] == ["opus", "mini", "mid"]
    assert models.effective_effort("opus", "max") == "max"
    assert models.effective_effort("mid", "max") == "high"      # closest lower
    assert models.effective_effort("mid", "low") == "medium"    # nothing lower: its lowest
    assert models.effective_effort("mini", "high") is None      # no effort setting: send none
    assert models.effective_effort(None, "xhigh") == "high"     # the default model's levels
    assert models.effective_effort("opus", None) is None
    assert models.known("mini") and models.known("sonnet") and not models.known("nope")


def test_builtin_agent_options_and_effort_reach_the_model(room: ReadyRoom, folders):
    from carrier.debrief import projects as DP

    store = room.store
    p = room.create_project("Server", str(folders["server"]))
    q = room.create_project("Client", str(folders["client"]))
    # Global: security on opus at high effort; Server lowers it, Client inherits.
    DP.set_builtin_option(store, "security", "model", "opus", None)
    DP.set_builtin_option(store, "security", "effort", "high", None)
    DP.set_builtin_option(store, "security", "effort", "low", p["id"])
    DP.set_builtin_option(store, "security", "severity", "low", p["id"])
    sec = lambda pid, path: next(c for c in DP.effective_criteria(store, pid, Path(path)) if c.name == "security")  # noqa: E731
    a, b = sec(p["id"], folders["server"]), sec(q["id"], folders["client"])
    assert (a.model, a.effort, a.severity) == ("opus", "low", "low")
    assert (b.model, b.effort, b.severity) == ("opus", "high", "high")
    view = DP.agents_view(store, p["id"])
    row = next(x for x in view["builtins"] if x["name"] == "security")
    assert row["inherited"]["effort"] == "high" and row["own"]["effort"] == "low"
    DP.set_builtin_option(store, "security", "effort", None, p["id"])  # back to inheriting
    assert sec(p["id"], folders["server"]).effort == "high"
    with pytest.raises(ValueError):
        DP.set_builtin_option(store, "security", "effort", "extreme", None)
    # Custom agents keep their own model and effort.
    DP.save_agent(store, {"title": "Perf", "prompt": "Flag slow code.", "model": "haiku", "effort": "medium"},
                  pid=p["id"])
    perf = next(c for c in DP.effective_criteria(store, p["id"], folders["server"]) if c.name == "perf")
    assert (perf.model, perf.effort) == ("haiku", "medium")


def test_skills_files_and_drafting(tmp_path: Path, monkeypatch):
    import asyncio
    import json

    from carrier.core import skills
    from carrier.core.llm import FakeBackend
    from carrier.readyroom import drafting

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    proj.mkdir()
    saved = skills.save(proj, "Release Mod", "Use when releasing\na mod.", "1. Build.\n2. Zip.")
    assert saved["name"] == "release-mod" and saved["description"] == "Use when releasing a mod."
    text = (proj / ".claude/skills/release-mod/SKILL.md").read_text()
    assert text.startswith("---\nname: release-mod\ndescription: Use when releasing a mod.\n---")
    skills.save(None, "notes", "Use when taking notes.", "Write notes.", scope="personal")
    found = skills.list_skills(proj)
    assert [s["name"] for s in found["project"]] == ["release-mod"]
    assert [s["name"] for s in found["personal"]] == ["notes"]
    skills.save(proj, "ship-mod", "Use when shipping.", "1. Ship.", old_name="release-mod")  # rename
    assert [s["name"] for s in skills.list_skills(proj)["project"]] == ["ship-mod"]
    for bad in (("", "d", "b"), ("x", "", "b"), ("x", "d", "")):
        with pytest.raises(ValueError):
            skills.save(proj, *bad)
    skills.delete(proj, "ship-mod")
    assert skills.list_skills(proj)["project"] == []
    backend = FakeBackend(lambda s, p: json.dumps({"name": "release-mod", "description": "Use when releasing.",
                                                   "body": "1. Build."}))
    out = asyncio.run(drafting.draft_async("skill", "release my mod", backend))
    assert out == {"name": "release-mod", "description": "Use when releasing.", "body": "1. Build."}
    assert "Claude Code skills" in backend.calls[0]["system"] and backend.calls[0]["tools"] is None


def test_workspace_folder_rules(tmp_path: Path):
    room = ReadyRoom(Store(tmp_path / "c.db"))
    ws = tmp_path / "ws"
    (ws / "mods" / "a").mkdir(parents=True)
    (ws / "other").mkdir()
    pid = room.create_project("A", str(ws / "mods" / "a"))["id"]
    with pytest.raises(RoomError, match="inside"):
        room.add_folder(pid, str(ws), kind="work")  # contains the main folder
    p = room.add_folder(pid, str(ws), kind="workspace")
    assert p["folders"][0]["kind"] == "workspace"
    p = room.add_folder(pid, str(ws / "other"), kind="work")  # a work folder inside the workspace is fine
    assert [f["kind"] for f in p["folders"]] == ["workspace", "work"]
    # Only work folders are reviewed and edited; the workspace isn't one of them.
    roots = [lbl for lbl, _ in room.store.project_roots(room.store.project(pid))]
    assert roots == ["", "other"]


def test_routines_triggers_and_review_gate(tmp_path: Path):
    import subprocess

    from carrier.core import gitutil
    from carrier.readyroom import routines as R

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("1\n")
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"],
                 ["add", "-A"], ["commit", "-qm", "start"]):
        subprocess.run(["git", *args], cwd=proj, check=True)
    room = ReadyRoom(Store(tmp_path / "c.db"))
    pid = room.create_project("P", str(proj), first_task="Build it")["id"]
    with pytest.raises(R.RoutineError):
        R.save(room, pid, "", "x")
    docs = R.save(room, pid, "Update docs", "Update README.md.", "after_task")[0]["id"]
    R.save(room, pid, "Push", "git push", "manual", "reviewed")
    push = next(r["id"] for r in room.store.routines(pid) if r["name"] == "Push")
    # Finishing a task queues the "after each task" routine next — once, and not after itself.
    build = room.store.tasks(pid)[0]
    room.finish_task(build["id"], "built")
    queued = room.store.tasks(pid, statuses=("planned",))
    assert [t["title"] for t in queued] == ["Update docs"] and queued[0]["routine_id"] == docs
    room.finish_task(queued[0]["id"], "docs done")
    assert room.store.tasks(pid, statuses=("planned",)) == []
    # Run puts a manual routine next; the gate holds it until the review is clear.
    R.run(room, push)
    R.run(room, push)  # not twice
    task = next(t for t in room.store.tasks(pid, statuses=("planned",)) if t["routine_id"] == push)
    assert len(room.store.tasks(pid, statuses=("planned",))) == 1
    with pytest.raises(R.RoutineError, match="never been reviewed"):
        R.check_gate(room.store, task)
    sha = gitutil.snapshot_worktree(proj)
    gitutil.set_checkpoint(proj, sha, str(pid))
    R.check_gate(room.store, task)  # clear now
    (proj / "b.txt").write_text("new\n")
    with pytest.raises(R.RoutineError, match="1 changed file"):
        R.check_gate(room.store, task)
    (proj / "b.txt").unlink()
    room.add_task(pid, "Fix review findings (1)", "x", fix_review="r1")
    with pytest.raises(R.RoutineError, match="fix task"):
        R.check_gate(room.store, task)


def test_task_groups_queues_and_group_routines(tmp_path: Path):
    from carrier.readyroom import routines as R

    proj = tmp_path / "p"
    proj.mkdir()
    room = ReadyRoom(Store(tmp_path / "c.db"))
    pid = room.create_project("P", str(proj), first_task="Main work")["id"]
    with pytest.raises(RoomError):
        room.save_group(pid, "Docs", priority="urgent")
    gid = room.save_group(pid, "Docs", "normal", "docs, writing")["groups"][0]["id"]
    room.save_group(pid, "Ideas", "backlog")
    room.add_task(pid, "Draft guide", group_id=gid)
    room.add_task(pid, "Draft FAQ", group_id=gid)
    room.add_task(pid, "Urgent doc", group_id=gid, first=True)
    # Each queue has its own current task; the main queue is unaffected.
    assert room.current_task(pid)["title"] == "Main work"
    assert [t["title"] for t in room.queue(pid, gid)] == ["Urgent doc", "Draft guide", "Draft FAQ"]
    faq = room.queue(pid, gid)[2]
    room.move_task(faq["id"], "top")
    assert room.current_task(pid, gid)["title"] == "Draft FAQ"
    assert room.current_task(pid)["title"] == "Main work"
    # A routine tied to the group runs when the group's last task is done (not before).
    with pytest.raises(R.RoutineError, match="Pick the task group"):
        R.save(room, pid, "Publish docs", "Publish them.", "group_done")
    R.save(room, pid, "Publish docs", "Publish them.", "group_done", group_id=gid)
    R.save(room, pid, "Spell-check", "Check spelling.", "after_task", group_id=gid)
    for t in list(room.queue(pid, gid)):
        if not t.get("routine_id"):
            room.finish_task(t["id"])
    queued = {t["title"]: t for t in room.queue(pid, gid)}
    assert "Spell-check" in queued  # after each task of the group, in the group's queue
    room.finish_task(queued["Spell-check"]["id"])
    assert [t["title"] for t in room.queue(pid, gid)] == ["Publish docs"]
    # Finishing a main-queue task doesn't trigger the group's routines.
    room.finish_task(room.current_task(pid)["id"])
    assert [t["title"] for t in room.queue(pid, gid)] == ["Publish docs"]
    take = R.takeoff_queue(room.store)
    assert [q["title"] for q in take] == ["Publish docs"] and take[0]["group"] == "Docs"
    # Moving a task between queues; deleting a group moves its tasks to the main queue.
    t = room.add_task(pid, "Loose idea")["tasks"][-1]
    ideas = next(g["id"] for g in room.project(pid)["groups"] if g["name"] == "Ideas")
    room.set_task_group(t["id"], ideas)
    assert [x["title"] for x in room.queue(pid, ideas)] == ["Loose idea"]
    room.delete_group(pid, ideas)
    assert "Loose idea" in [x["title"] for x in room.queue(pid)]


def test_reshaping_tasks_is_logged(tmp_path: Path):
    from carrier.debrief.projects import task_scope_text

    proj = tmp_path / "p"
    proj.mkdir()
    room = ReadyRoom(Store(tmp_path / "c.db"))
    pid = room.create_project("P", str(proj), first_task="Build the editor")["id"]
    cur = room.current_task(pid)
    room.update_task(cur["id"], details="- panel layout\n- save button\n- undo")
    other = room.add_task(pid, "Write the manual", "- intro\n- shortcuts page")["tasks"][-1]
    # New work: into the current task, or placed in a queue (new group, after a task).
    room.add_to_task(cur["id"], "Keyboard shortcuts", "- Ctrl+S saves", note="Came up while wiring save")
    assert "### Added: Keyboard shortcuts" in room.store.task(cur["id"])["details"]
    t = room.place_task(pid, "Icons", "- draw icons", new_group={"name": "Art", "priority": "low"}, by="claude")
    assert room.store.task_group(t["group_id"])["name"] == "Art"
    t2 = room.place_task(pid, "Tooltips", "", position=f"after:{cur['id']}")
    assert [x["title"] for x in room.queue(pid)][:2] == ["Build the editor", "Tooltips"]
    # Pull part of another task in; then split part of the current task out.
    room.merge_tasks(cur["id"], other["id"], part="- shortcuts page", remaining="- intro", note="Needed now")
    assert room.store.task(other["id"])["details"] == "- intro"
    assert "From task “Write the manual” (part)" in room.store.task(cur["id"])["details"]
    new = room.split_task(cur["id"], "- panel layout\n- save button", "Undo support", "- undo", note="Too big")
    assert new["title"] == "Undo support" and room.store.task(cur["id"])["details"] == "- panel layout\n- save button"
    # Pull a whole task in: it's closed as merged, not lost.
    room.merge_tasks(cur["id"], t2["id"])
    assert room.store.task(t2["id"])["status"] == "dropped"
    kinds = [e["kind"] for e in room.task_history(cur["id"])]
    assert kinds == ["created", "edited", "scope_added", "merged_in", "split_out", "merged_in"]
    split = next(e for e in room.task_history(cur["id"]) if e["kind"] == "split_out")
    assert "- undo" in split["removed"] and "Ctrl+S" in split["removed"]  # what left the task is recorded
    assert room.task_history(other["id"])[-1]["kind"] == "merged_away"
    with pytest.raises(RoomError):
        room.merge_tasks(cur["id"], cur["id"])
    # Reviews get each task's final scope and its changes, with removals called out.
    room.store.update_task(cur["id"], status="active")
    text = task_scope_text(room.store, pid, proj)
    assert "Task #%d: Build the editor" % cur["id"] in text and "REMOVED from the task" in text


def test_database_is_backed_up_before_an_upgrade(tmp_path: Path):
    import sqlite3

    from carrier.core import db as D

    path = tmp_path / "carrier.db"
    Store(path)  # current version: nothing to back up
    assert not (tmp_path / "backups").exists()
    with sqlite3.connect(path) as c:  # pretend it's an older version
        c.execute("DROP TABLE task_edits")
        c.execute("INSERT INTO settings(key, value) VALUES ('marker', ?)", ('"keep me"',))
    s = Store(path)
    assert s.backup is not None and s.backup.parent == tmp_path / "backups"
    with sqlite3.connect(s.backup) as c:
        assert c.execute("SELECT value FROM settings WHERE key='marker'").fetchone()[0] == '"keep me"'
    assert s.get_setting("marker") == "keep me"  # upgraded in place, data kept
    for i in range(7):
        (tmp_path / "backups" / f"carrier-2000010{i}-000000.db").write_text("")
    D.backup_database(path)
    assert len(list((tmp_path / "backups").glob("carrier-*.db"))) == D.MAX_BACKUPS


def test_templates_carry_groups_routines_skills_and_agents(tmp_path: Path, monkeypatch):
    from carrier.core import skills
    from carrier.readyroom import routines as R

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home"))
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    room = ReadyRoom(Store(tmp_path / "c.db"))
    pid = room.create_project("Read Aloud", str(a))["id"]
    gid = room.save_group(pid, "Release", "high", "ship")["groups"][0]["id"]
    R.save(room, pid, "Push", "git push", "group_done", "reviewed", group_id=gid, mode="acceptEdits")
    skills.save(a, "release-mod", "Use when releasing.", "1. Zip it.")
    room.store.add_review_agent(project_id=pid, name="magic-numbers", title="Magic numbers", prompt="Flag them.")
    tid = room.template_from_project(pid, "Release-ready")[0]["id"]
    new = room.create_project("Puppeteer", str(b), template_id=tid)["id"]
    g = room.store.task_groups(new)[0]
    assert (g["name"], g["priority"], g["tags"]) == ("Release", "high", ["ship"])
    r = room.store.routines(new)[0]
    assert (r["name"], r["trigger"], r["gate"], r["mode"], r["group_id"]) == ("Push", "group_done", "reviewed",
                                                                              "acceptEdits", g["id"])
    assert skills.list_skills(b)["project"][0]["name"] == "release-mod"
    assert room.store.review_agents(new)[0]["name"] == "magic-numbers"
    # Editing the template's text keeps what it copies.
    room.save_template("Release-ready", goal="Ship mods.", tid=tid)
    assert room.store.template(tid)["extras"]["routines"][0]["name"] == "Push"


def test_review_gate_is_cached_until_something_changes(tmp_path: Path, monkeypatch):
    import subprocess

    from carrier.readyroom import routines as R

    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "a.txt").write_text("1\n")
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"],
                 ["add", "-A"], ["commit", "-qm", "start"]):
        subprocess.run(["git", *args], cwd=proj, check=True)
    room = ReadyRoom(Store(tmp_path / "c.db"))
    pid = room.create_project("P", str(proj))["id"]
    R.invalidate()
    calls = []
    real = R._review_blockers
    monkeypatch.setattr(R, "_review_blockers", lambda s, p: calls.append(p) or real(s, p))
    first = R.review_blockers(room.store, pid)
    assert R.review_blockers(room.store, pid) == first and len(calls) == 1  # cached
    R.invalidate(pid)
    R.review_blockers(room.store, pid)
    assert len(calls) == 2
    R.review_blockers(room.store, pid, fresh=True)  # starting a gated routine always checks now
    assert len(calls) == 3


def test_secrets_scan_gate_and_gitignore(tmp_path: Path):
    import subprocess

    from carrier.core import secrets
    from carrier.readyroom import routines as R

    key = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    hits = secrets.scan_text(f'token = "{key}"\napi_key = "YOUR_KEY_HERE_123"\nkey = os.environ["X"]\n', "a.py")
    assert [(h["kind"], h["line"]) for h in hits] == [("GitHub token", 1)] and key not in hits[0]["preview"]
    assert secrets.scan_text("-----BEGIN OPENSSH PRIVATE KEY-----")[0]["kind"] == "private key"
    assert secrets.scan_text("db = 'postgres://bob:hunter22@localhost/x'")[0]["kind"] == "password in URL"

    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "app.py").write_text(f'TOKEN = "{key}"\n')
    (proj / ".env").write_text("SECRET=1\n")
    (proj / ".env.example").write_text("SECRET=\n")
    (proj / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0" + key.encode())
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=proj, check=True)
    room = ReadyRoom(Store(tmp_path / "c.db"))
    pid = room.create_project("P", str(proj))["id"]
    R.invalidate()
    found = R.secret_hits(room.store, pid)
    assert [(h["path"], h["kind"]) for h in found] == [("app.py", "GitHub token")]  # not the binary file
    assert any("Possible secret (GitHub token) in app.py line 1" in b for b in R.review_blockers(room.store, pid))
    R.mark_secret_safe(room.store, pid, found[0]["fingerprint"])
    assert R.secret_hits(room.store, pid) == []
    (proj / "app.py").write_text(f'TOKEN = "{key}"  # changed\n')
    assert len(R.secret_hits(room.store, pid)) == 1  # a changed line is reported again
    # Key files git would publish, and the one-click .gitignore fix.
    assert [f["path"] for f in secrets.unignored_secret_files(proj)] == [".env"]
    added = secrets.ignore_secret_files(proj)
    assert ".env" in added and "!.env.example" in added
    assert secrets.unignored_secret_files(proj) == []
    assert secrets.ignore_secret_files(proj) == []  # only once
    assert (proj / ".env.example").exists() and ".env.example" in subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"], cwd=proj, capture_output=True, text=True).stdout


def test_review_findings_secrets_gitignore_and_first_commit(tmp_path: Path):
    import subprocess

    from carrier.core import secrets
    from carrier.readyroom import importer

    for line in ("DB_PASSWORD=Sup3rS3cretValue9", "export AWS_SECRET_ACCESS_KEY=abcdEFGH1234ijklMNOP5678",
                 "-----BEGIN PGP PRIVATE KEY BLOCK-----", "x" * 3000 + "-----BEGIN RSA PRIVATE KEY-----"):
        assert secrets.scan_text(line), line
    for line in ("tokens_count = 1234567890123", "api_key = YOUR_KEY_HERE", "password = ${DB_PASSWORD}"):
        assert not secrets.scan_text(line), line
    assert secrets.decode("ghp_x".encode("utf-16")) == "ghp_x"  # PowerShell writes UTF-16
    # .gitignore: kept byte for byte (encoding, CRLF), exceptions always last.
    gi = tmp_path / ".gitignore"
    gi.write_bytes("caf\xe9/\r\n!.env.example\r\n".encode("cp1252"))
    secrets.append_gitignore(gi, [".env", ".env.*", "!.env.example"], "test")
    raw = gi.read_bytes()
    assert raw.startswith("caf\xe9/\r\n!.env.example\r\n".encode("cp1252")) and raw.endswith(b"!.env.example\r\n")
    # A workspace inside an empty parent repository gets its own history; the parent is untouched.
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Documents" / "taxes.txt").write_text("private\n")
    subprocess.run(["git", "init", "-q"], cwd=home, check=True)
    ws = home / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("1\n")
    importer.first_commit(ws, [".env"])
    assert (ws / ".git").exists()
    assert subprocess.run(["git", "ls-files"], cwd=ws, capture_output=True, text=True).stdout.split() == \
        [".gitignore", "a.txt"]
    assert subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=home, capture_output=True).returncode != 0
