# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Import existing work into Carrier projects (readyroom/importer.py)."""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from carrier.core import gitutil
from carrier.core.db import Store
from carrier.core.llm import FakeBackend
from carrier.debrief.transcripts import project_key
from carrier.readyroom import importer
from carrier.readyroom.importer import (
    ImportChat,
    ImportError_,
    apply_plan,
    validate_plan,
)
from carrier.readyroom.manager import ReadyRoom


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A modding workspace with three strands of work in one repository."""
    ws = tmp_path / "Workspace"
    files = {
        "CLAUDE.md": "# Workspace\n- Never write outside this folder.\n",
        ".claude/settings.json": json.dumps({"permissions": {"allow": ["Bash(npm:*)"]}, "env": {"TOKEN": "secret"}}),
        "mods/Anim/README.md": "Animation library for ships.\n",
        "mods/Anim/build.sh": "echo build\n",
        "mods/AnimDemo/mod_info.json": "{}\n",
        "mods/Voice/README.md": "Reads dialog text out loud.\n",
        "mods/Voice/src/a.txt": "x\n",
        "tools/editor/package.json": "{}\n",
        "tools/editor/node_modules/dep/index.js": "skip me\n",
    }
    for rel, text in files.items():
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text(text)
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
        _git(ws, *args)
    (ws / ".gitignore").write_text("node_modules/\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "Animation: first puppet")
    (ws / "mods/Voice/src/a.txt").write_text("y\n")
    _git(ws, "commit", "-qam", "Voice: speaker button")
    (ws / "mods/Voice/untracked.txt").write_text("not in git\n")

    home = tmp_path / "claude-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    proj = home / "projects" / project_key(ws)
    proj.mkdir(parents=True)
    lines = [
        {"type": "summary", "summary": "Voice pools"},
        {"type": "user", "cwd": str(ws), "timestamp": "2026-10-01T10:00:00Z",
         "message": {"content": "Add male and female voice pools to the Voice mod"}},
        {"type": "user", "cwd": str(ws), "timestamp": "2026-10-01T10:05:00Z", "isMeta": True,
         "message": {"content": "meta noise"}},
        {"type": "user", "cwd": str(ws), "timestamp": "2026-10-01T10:06:00Z",
         "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
    ]
    (proj / "s1.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
    return ws


def _export(tmp_path: Path) -> Path:
    convos = [
        {"uuid": "c1", "name": "Ship editor layout", "updated_at": "2026-09-01T00:00:00Z",
         "chat_messages": [{"sender": "human", "text": "How should the editor panel look?"},
                           {"sender": "assistant", "text": "A sidebar with slots."}]},
        {"uuid": "c2", "name": "Dinner ideas", "created_at": "2026-09-02",
         "chat_messages": [{"sender": "human", "content": [{"type": "text", "text": "Pasta tonight?"}]}]},
    ]
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("data/conversations.json", json.dumps(convos))
    return z


def test_scan_reads_everything_and_changes_nothing(workspace: Path, tmp_path: Path):
    before = _git(workspace, "status", "--porcelain")
    ws = importer.scan(str(workspace), export=str(_export(tmp_path)))
    paths = {f["path"] for f in ws["folders"]}
    assert {"mods/Anim", "mods/Voice", "tools/editor"} <= paths
    assert not any("node_modules" in p for p in paths)
    anim = next(f for f in ws["folders"] if f["path"] == "mods/Anim")
    assert "build.sh" in anim["markers"] and anim["readme"].startswith("Animation")
    assert [m["path"] for m in ws["claude_md"]] == ["CLAUDE.md"]
    assert ws["settings"][0]["settings"] == {"permissions": {"allow": ["Bash(npm:*)"]}}  # never env values
    assert ws["git"]["is_repo"] and ws["git"]["sub"] == "" and ws["git"]["dirty"]
    assert [c["subject"] for c in ws["git"]["commits"]] == ["Voice: speaker button", "Animation: first puppet"]
    assert "mods/Voice" in ws["git"]["commits"][0]["areas"]
    (s,) = ws["sessions"]
    assert s["prompts"] == ["Add male and female voice pools to the Voice mod"] and s["summary"] == "Voice pools"
    assert [c["name"] for c in ws["chats"]] == ["Ship editor layout", "Dinner ideas"]
    assert ws["chats_selected"] == ["c1"]  # mentions "editor", a folder of the workspace
    assert _git(workspace, "status", "--porcelain") == before

    digest = importer.build_digest(ws)
    assert "Never write outside this folder" in digest and "voice pools" in digest
    assert "How should the editor panel look?" in digest and "Pasta" not in digest
    assert "secret" not in digest
    small = importer.build_digest(ws, limit=600)
    assert len(small) < 1400 and "voice pools" in small  # newest history kept

    with pytest.raises(ImportError_):
        importer.scan(str(tmp_path / "missing"))
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    with pytest.raises(ImportError_, match="conversations export"):
        importer.read_chat_export(bad)


PLAN = {
    "summary": "Three strands.",
    "moves": [],
    "rule_sets": [{"name": "Modding", "rules": "- Never touch the game folder."}],
    "projects": [
        {"name": "Animation", "goal": "Ship animations.", "main": "mods/Anim",
         "folders": [{"path": "mods/AnimDemo", "kind": "work", "label": "demo"}],
         "rules": "- Run build.sh.", "rule_sets": ["Modding"], "tags": ["mod"],
         "tasks": [{"title": "Finish puppet format", "details": "v0.2"}, "Write demo mission"]},
        {"name": "Read Aloud", "goal": "Text to speech.", "main": "mods/Voice", "rule_sets": ["Modding"],
         "tasks": []},
        {"name": "Editor", "main": "tools/editor", "folders": ["mods/Anim"]},
    ],
}


def test_conversation_turns_and_plan(workspace: Path):
    replies = iter([
        "I found three strands: animation, voice, editor. Split in place?",
        "Done.\n```carrier-plan\n" + json.dumps(PLAN) + "\n```",
    ])
    backend = FakeBackend(lambda system, prompt: next(replies))
    chat = ImportChat.start(str(workspace), mode="in_place")
    view = chat.turn(backend)
    assert view["messages"][0]["role"] == "assistant" and view["plan"] is None
    assert "IN PLACE" in backend.calls[0]["prompt"] and backend.calls[0]["tools"] is None
    view = chat.turn(backend, "Yes, in place.")
    assert [m["role"] for m in view["messages"]] == ["assistant", "user", "assistant"]
    assert view["messages"][-1]["text"] == "Done." and view["plan"]["projects"][0]["name"] == "Animation"
    assert "Yes, in place." in backend.calls[1]["prompt"]
    # Saved after every turn, and listed until it's done.
    again = ImportChat.load(chat.id)
    assert again.plan == view["plan"] and len(again.messages) == 3
    assert [r["id"] for r in ImportChat.recent()] == [chat.id]
    # The user's edits to the plan are what the next turn builds on.
    edited = {**PLAN, "summary": "Edited by me"}
    chat.set_plan(edited)
    backend2 = FakeBackend(lambda s, p: "ok")
    chat.turn(backend2, "Anything else?")
    assert "Edited by me" in backend2.calls[0]["prompt"]
    with pytest.raises(ImportError_):
        ImportChat.load("../../etc")


def test_validate_plan_reports_problems(workspace: Path):
    clean, problems = validate_plan(workspace, PLAN)
    assert problems == []
    assert clean["projects"][0]["folders"][0]["path"] == "mods/AnimDemo"
    assert clean["projects"][2]["folders"][0]["kind"] == "work"
    assert clean["projects"][0]["tasks"][1] == {"title": "Write demo mission", "details": ""}
    bad = {"projects": [
        {"name": "A", "main": "mods/Anim", "folders": [{"path": "mods/Anim/sub"}, {"path": "nope", "kind": "x"}],
         "rule_sets": ["Missing"]},
        {"name": "a", "main": "mods/Anim"},
        {"name": "", "main": ""},
    ], "moves": [{"from": "mods/Voice", "to": "mods/Anim"}, {"from": "../outside", "to": "x"}]}
    _clean, problems = validate_plan(workspace, bad, existing_mains={str(workspace / "tools" / "editor")})
    text = " ".join(problems)
    for expected in ("inside (or contains) its main folder", "doesn't exist", "must be work, reference or workspace",
                     "Missing", "Two projects are called a", "same main folder", "needs a name",
                     "needs a main folder", "already exists", "stay inside the workspace"):
        assert expected in text, expected
    _c, problems = validate_plan(workspace, {"projects": [{"name": "E", "main": "tools/editor"}]},
                                 existing_mains={str((workspace / "tools" / "editor").resolve())})
    assert any("already a Carrier project" in p for p in problems)
    _c, problems = validate_plan(workspace, {**PLAN, "moves": [{"from": "mods/Voice", "to": "voice"}]},
                                 allow_moves=False)
    assert any("in place" in p for p in problems)


def test_apply_in_place(workspace: Path, tmp_path: Path):
    room = ReadyRoom(Store(tmp_path / "c.db"))
    room.save_rule_set("Modding", "- my own version")  # reused, not duplicated
    out = apply_plan(room, workspace, PLAN)
    assert len(out["projects"]) == 3 and out["rule_sets"] == 0 and out["moves"] == 0
    anim = room.project(out["projects"][0])
    assert anim["name"] == "Animation" and Path(anim["path"]) == (workspace / "mods" / "Anim").resolve()
    assert anim["goal"] == "Ship animations." and anim["tags"] == ["mod"]
    assert [f["label"] for f in anim["folders"]] == ["demo"]
    assert [t["title"] for t in room.store.tasks(anim["id"])] == ["Finish puppet format", "Write demo mission"]
    assert [r["name"] for r in room.store.rule_sets()] == ["Modding"]
    # Marked as reviewed: each project's first Debrief review covers only new work.
    for pid in out["projects"]:
        assert gitutil.get_checkpoint(workspace, str(pid))
    # Running it again fails cleanly: the main folders are taken now.
    with pytest.raises(ImportError_, match="already a Carrier project"):
        apply_plan(room, workspace, PLAN)
    assert len(room.store.projects()) == 3


def test_apply_with_moves_keeps_git_history(workspace: Path, tmp_path: Path):
    room = ReadyRoom(Store(tmp_path / "c.db"))
    plan = {"moves": [{"from": "mods/Voice", "to": "projects/read-aloud/mod"},
                      {"from": "tools/editor", "to": "projects/editor"}],
            "projects": [{"name": "Read Aloud", "main": "projects/read-aloud"},
                         {"name": "Editor", "main": "projects/editor"}]}
    staged_before = _git(workspace, "diff", "--cached", "--name-status")
    assert staged_before == ""
    out = apply_plan(room, workspace, plan, baseline=False)
    assert out["moves"] == 2
    assert (workspace / "projects/read-aloud/mod/untracked.txt").exists()  # untracked files move too
    assert (workspace / "projects/editor/node_modules/dep/index.js").exists()  # ignored ones too
    assert not (workspace / "mods/Voice").exists()
    staged = _git(workspace, "diff", "--cached", "-M", "--name-status").split("\n")
    assert any(line.startswith("R") and "projects/read-aloud/mod/src/a.txt" in line for line in staged)
    assert not any("untracked.txt" in line or "node_modules" in line for line in staged)
    assert gitutil.get_checkpoint(workspace, str(out["projects"][0])) is None  # baseline=False


def test_apply_undoes_everything_on_failure(workspace: Path, tmp_path: Path, monkeypatch):
    room = ReadyRoom(Store(tmp_path / "c.db"))
    plan = {"moves": [{"from": "mods/Voice", "to": "voice"}],
            "rule_sets": [{"name": "New set", "rules": "- x"}],
            "projects": [{"name": "Voice", "main": "voice", "rule_sets": ["New set"]},
                         {"name": "Anim", "main": "mods/Anim"}]}
    create = room.create_project

    def failing(name, *args, **kwargs):
        if name == "Anim":
            raise RuntimeError("disk full")
        return create(name, *args, **kwargs)

    monkeypatch.setattr(room, "create_project", failing)
    with pytest.raises(ImportError_, match="disk full"):
        apply_plan(room, workspace, plan)
    assert room.store.projects() == [] and room.store.rule_sets() == []
    assert (workspace / "mods/Voice/src/a.txt").exists() and not (workspace / "voice").exists()
    assert _git(workspace, "diff", "--cached", "--name-status") == ""  # git's index is back too


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    ws = tmp_path / "ws"
    for rel, text in files.items():
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text(text)
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"],
                 ["add", "-A"], ["commit", "-qm", "start"]):
        _git(ws, *args)
    return ws


def test_move_names_git_would_read_as_patterns(tmp_path: Path):
    # "a[b]" is a glob pattern that matches "ab"; a leading quote needs git's quoting.
    quote = "'q" if sys.platform == "win32" else '"q'
    ws = _repo(tmp_path, {"a[b]/f.txt": "1\n", "ab/f.txt": "2\n", f"{quote}/f.txt": "3\n", "sp ace/f.txt": "4\n"})
    importer._move(ws / "a[b]", ws / "moved")
    importer._move(ws / quote, ws / "q2")
    importer._move(ws / "sp ace", ws / "new place")
    status = _git(ws, "status", "--porcelain", "-uall")
    assert "ab/f.txt" not in status  # the sibling that matches the pattern is untouched
    staged = _git(ws, "diff", "--cached", "-M", "--name-status")
    assert "moved/f.txt" in staged and "q2/f.txt" in staged and "new place/f.txt" in staged
    assert "??" not in status


def test_failed_git_update_puts_the_folder_back(tmp_path: Path):
    ws = _repo(tmp_path, {"a/f.txt": "1\n"})
    (ws / ".git" / "index.lock").write_text("")
    with pytest.raises(ImportError_, match="Couldn't update git"):
        importer._move(ws / "a", ws / "deep" / "b")
    assert (ws / "a" / "f.txt").exists() and not (ws / "deep").exists()
    room = ReadyRoom(Store(tmp_path / "c.db"))
    with pytest.raises(ImportError_, match="Git is busy"):
        apply_plan(room, ws, {"moves": [{"from": "a", "to": "b"}], "projects": [{"name": "B", "main": "b"}]})
    assert (ws / "a").exists()


def test_overlapping_moves_links_submodules_and_nested_mains(tmp_path: Path):
    ws = _repo(tmp_path, {"a/x.txt": "1\n", "d/y.txt": "2\n", "c/a/z.txt": "3\n", "sub/m.txt": "4\n",
                          "v2/k.txt": "5\n"})
    def problems(plan):
        return " ".join(validate_plan(ws, {"projects": [{"name": "P", "main": "v2"}], **plan})[1])
    assert "overlap" in problems({"moves": [{"from": "a", "to": "b/c"}, {"from": "d", "to": "b"}]})
    assert "moves too" in problems({"moves": [{"from": "a", "to": "c/a2"}, {"from": "c", "to": "e"}]})
    assert "nested" in problems({"moves": [{"from": "c", "to": "e"}, {"from": "c/a", "to": "f"}]})
    try:
        (ws / "link").symlink_to(ws / "v2", target_is_directory=True)
    except OSError:  # Windows without the symlink privilege
        pass
    else:
        assert "is a link" in problems({"moves": [{"from": "link", "to": "moved"}]})
    (ws / "sub" / ".git").write_text("gitdir: ../.git/modules/sub\n")
    assert "submodule" in problems({"moves": [{"from": "sub", "to": "s2"}]})
    nested = validate_plan(ws, {"projects": [{"name": "Root", "main": "."}, {"name": "Inner", "main": "a"}]})[1]
    assert any("inside one another" in p for p in nested)
    outside = validate_plan(ws, {"projects": [{"name": "Inner", "main": "a"}]}, existing_mains={str(ws)})[1]
    assert any("existing project" in p for p in outside)
    room = ReadyRoom(Store(tmp_path / "c.db"))
    room.create_project("Root", str(ws))
    from carrier.readyroom.manager import RoomError
    with pytest.raises(RoomError, match="inside"):
        room.create_project("Inner", str(ws / "a"))


def test_questions_and_rule_proposals_in_replies(workspace: Path):
    reply = ("Two things.\n```carrier-questions\n"
             + json.dumps([{"question": "Split?", "header": "Layout",
                            "options": [{"label": "In place (Recommended)", "description": "Nothing moves."},
                                        "Reorganise"]}, {"no": "question"}])
             + "\n```\n```carrier-rule\n"
             + json.dumps({"text": "- Never edit D:/GameInstall.", "rule_set": "Modding", "why": "You said so"})
             + "\n```")
    text, plan, extras = importer.split_reply(reply)
    assert text == "Two things." and plan is None
    (q,) = extras["questions"]
    assert q["options"][1] == {"label": "Reorganise", "description": ""} and not q["multiSelect"]
    assert extras["rules"][0]["rule_set"] == "Modding" and extras["rules"][0]["status"] == "proposed"
    chat = ImportChat.start(str(workspace))
    view = chat.turn(FakeBackend(lambda s, p: reply))
    msg = view["messages"][0]
    assert msg["questions"][0]["question"] == "Split?" and msg["rules"][0]["text"].startswith("- Never")
    view = chat.rule_done(0, 0, "applied")
    assert view["messages"][0]["rules"][0]["status"] == "applied"
    with pytest.raises(ImportError_):
        chat.rule_done(0, 5, "applied")
    assert "carrier-questions" in importer.SYSTEM and "carrier-rule" in importer.SYSTEM


def test_first_commit_and_gitignore_suggestions(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "tool" / "node_modules" / "x").mkdir(parents=True)
    (ws / "tool" / "app.js").write_text("x\n")
    (ws / "mod" / "runtime").mkdir(parents=True)
    (ws / "mod" / "runtime" / "big.bin").write_text("weights\n")
    (ws / ".env").write_text("TOKEN=1\n")
    lines = importer.suggest_gitignore(ws)
    assert "node_modules/" in lines and "runtime/" in lines and ".env" in lines and "dist/" not in lines
    assert "Made a first commit" in importer.first_commit(ws, lines)
    tracked = _git(ws, "ls-files").split()
    assert "tool/app.js" in tracked and ".gitignore" in tracked
    assert not any("node_modules" in t or "runtime" in t or t == ".env" for t in tracked)
    assert importer.first_commit(ws, lines) == "It already has git history."
