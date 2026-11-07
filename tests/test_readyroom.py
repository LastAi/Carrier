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
    # A work folder inside a git repository must be its top folder; reference folders needn't be.
    repo = tmp_path / "mono"
    (repo / "pkg").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    with pytest.raises(RoomError, match="inside the git repository"):
        room.add_folder(a["id"], str(repo / "pkg"))
    room.add_folder(a["id"], str(repo / "pkg"), kind="reference")
    with pytest.raises(RoomError, match="inside the git repository"):
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
