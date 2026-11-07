# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Shared fixtures: a throwaway git repo, a fake Claude Code config dir with
a session transcript, and a scripted fake model backend.

None of the tests need network access or a Claude Code login.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from carrier.core.llm import FakeBackend
from carrier.debrief.prompts import CRITERIA_SYSTEM, GUIDE_SYSTEM, SEGMENT_SYSTEM
from carrier.debrief.transcripts import project_key


def run(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


@pytest.fixture(autouse=True)
def _isolated_user_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the real per-user settings file during tests."""
    monkeypatch.setenv("CARRIER_CONFIG_HOME", str(tmp_path / "user-config"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Repo with two commits and uncommitted work, like after a Claude Code task."""
    r = tmp_path / "app"
    r.mkdir()
    run(r, "init", "-q")
    run(r, "config", "user.email", "t@example.com")
    run(r, "config", "user.name", "Tester")
    (r / "README.md").write_text("# App\n\nStored data must stay readable by older versions.\n")
    (r / "save.py").write_text("def load(path):\n    return open(path).read()\n")
    (r / "tests").mkdir()
    (r / "tests" / "test_save.py").write_text("def test_load():\n    assert True\n")
    run(r, "add", "-A")
    run(r, "commit", "-qm", "initial")
    (r / "util.py").write_text("X = 1\n")
    run(r, "add", "-A")
    run(r, "commit", "-qm", "util")
    # Uncommitted "Claude Code" work:
    (r / "save.py").write_text(
        "import time\n\n\ndef load(path, retries=3):\n    for _ in range(retries):\n"
        "        try:\n            return open(path).read()\n        except OSError:\n"
        "            time.sleep(1)\n"
    )
    (r / "tests" / "test_save.py").unlink()
    (r / "notes.txt").write_text("untracked file\n")
    return r


@pytest.fixture
def claude_home(tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake ~/.claude with one session transcript for ``repo``."""
    home = tmp_path / "claude-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    proj = home / "projects" / project_key(repo)
    proj.mkdir(parents=True)
    lines = [
        {"type": "user", "sessionId": "s1", "cwd": str(repo), "timestamp": "2099-01-01T10:00:00Z",
         "message": {"role": "user", "content": "Make loading saves retry on IO errors."}},
        {"type": "assistant", "sessionId": "s1", "cwd": str(repo), "timestamp": "2099-01-01T10:00:05Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "I'll add a retry loop to load()."},
             {"type": "tool_use", "id": "t1", "name": "Edit",
              "input": {"file_path": str(repo / "save.py"), "old_string": "x",
                        "new_string": "def load(path, retries=3):\n    for _ in range(retries):"}}]}},
        {"type": "user", "sessionId": "s1", "cwd": str(repo), "timestamp": "2099-01-01T10:00:06Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "assistant", "sessionId": "s1", "cwd": str(repo), "timestamp": "2099-01-01T10:00:09Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "The old test is obsolete, removing it."},
             {"type": "tool_use", "id": "t2", "name": "Write",
              "input": {"file_path": str(repo / "tests" / "test_save.py"), "content": ""}}]}},
        "this line is not json and must be ignored",
    ]
    (proj / "s1.jsonl").write_text(
        "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n"
    )
    return home


def scripted_handler(system: str, prompt: str) -> str:
    """Pretend model: answers each agent type with plausible JSON."""
    if system == SEGMENT_SYSTEM:
        # Find hunk ids in the prompt and group the save.py + test hunks.
        ids = re.findall(r"^(h\d+) (\S+)", prompt, re.M)
        save = [h for h, p in ids if p in ("save.py", "tests/test_save.py")]
        items = [{"title": "Add retry to save loading", "summary": "Retries IO errors.", "hunks": save}]
        # Other hunks are deliberately left out to exercise the repair logic.
        return "```json\n" + json.dumps({"items": items}) + "\n```"
    if system == CRITERIA_SYSTEM:
        if "Tests removed or weakened" in prompt and "test_save.py" in prompt:
            item = re.search(r"### (c\d+): Add retry", prompt)
            if item:
                return json.dumps({"flags": [{"item": item.group(1), "severity": "high",
                                              "reason": "The only save test was deleted.",
                                              "evidence": "tests/test_save.py deleted", "confidence": 0.9}]})
        return '{"flags": []}'
    if system == GUIDE_SYSTEM:
        if "Reviewer's question" in prompt:
            return "It sleeps 1s between attempts (source: code)."
        return json.dumps({"what": "Retries load up to 3 times.", "intent": "Retry on IO errors.",
                           "intent_source": "request", "before": "Single read.", "why": "User asked.",
                           "why_source": "request", "attention": "Test was removed.",
                           "questions": ["Why remove the test?"]})
    return "OK"



@pytest.fixture
def fake_backend() -> FakeBackend:
    return FakeBackend(scripted_handler)
