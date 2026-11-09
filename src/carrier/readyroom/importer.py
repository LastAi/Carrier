# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Import existing work: turn a folder used with plain Claude Code (or the
Claude app) into one or more Carrier projects.

Three steps, all driven from the Ready Room's Import page:

1. :func:`scan` looks at the folder without changing anything — its layout,
   README files, CLAUDE.md files and ``.claude`` settings, the git log, the
   Claude Code sessions that ran there and, optionally, chats from a
   claude.ai data export the user points at.
2. :class:`ImportChat` talks it through with the user. The model gets a
   digest of what was found (it has **no tools**: it cannot read or change
   anything itself) and proposes how to split the work into projects — in
   place, or by moving folders — with goals, rules, rule sets and open tasks,
   as a plan in a ``carrier-plan`` JSON block.
3. :func:`validate_plan` checks the plan (which the user may edit) and
   :func:`apply_plan` carries it out: approved folder moves first, then the
   rule sets, projects, folders and tasks. Nothing happens until the user
   confirms, and a failure undoes what was done.

Untrusted text: everything read here (files, transcripts, chats) is data for
the model and is only ever shown in the page as text or rendered markdown.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from carrier.core import gitutil, usersettings
from carrier.core.jsonutil import extract_json
from carrier.core.llm import Backend, BackendError

# Folders never worth looking into (dependencies, caches, build output).
SKIP_DIRS = {".git", "node_modules", ".cache", "__pycache__", ".venv", "venv", "env", "dist", "build",
             "target", "out", ".next", ".gradle", ".idea", ".vs", ".pytest_cache", ".mypy_cache",
             ".ruff_cache", "runtime", ".tox", "bin", "obj", "$RECYCLE.BIN", "System Volume Information"}
# Files that mark a folder as a unit of work of its own.
MARKERS = ("README.md", "README.txt", "README", "CLAUDE.md", "package.json", "pyproject.toml",
           "setup.py", "Cargo.toml", "go.mod", "build.sh", "build.gradle", "pom.xml", "Makefile",
           "CMakeLists.txt", "mod_info.json", ".git")
MAX_DEPTH = 3
MAX_FOLDERS = 400
MAX_COMMITS = 300
MAX_SESSIONS = 80
PROMPTS_PER_SESSION = 12
PROMPT_CHARS = 400
README_CHARS = 1500
CLAUDE_MD_CHARS = 12000
DIGEST_CHARS = 90000

PLAN_FENCE = "carrier-plan"


class ImportError_(ValueError):
    """Shown to the user as is."""


# -- 1. scanning ------------------------------------------------------------------------

def _rel(root: Path, p: Path) -> str:
    try:
        r = Path(os.path.relpath(p, root)).as_posix()
    except ValueError:  # another drive on Windows
        return str(p)
    return "" if r == "." else r


def _read(p: Path, limit: int) -> str:
    try:
        with p.open(encoding="utf-8", errors="replace") as fh:
            text = fh.read(limit + 1)
    except OSError:
        return ""
    return text[:limit] + ("\n…(cut)" if len(text) > limit else "")


def survey_folders(root: Path, deadline: float | None = None) -> list[dict[str, Any]]:
    """Folders down to ``MAX_DEPTH`` with what marks them as a unit of work.

    Skips dependency/cache folders and stops at ``MAX_FOLDERS`` or the deadline.
    """
    out: list[dict[str, Any]] = []
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue and len(out) < MAX_FOLDERS:
        if deadline and time.monotonic() > deadline:
            break
        folder, depth = queue.pop(0)
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
        except OSError:
            continue
        files = [e.name for e in entries if not e.is_dir(follow_symlinks=False)]
        dirs = [e for e in entries if e.is_dir(follow_symlinks=False)]
        markers = [m for m in MARKERS if m in files or (m == ".git" and any(d.name == ".git" for d in dirs))]
        readme = next((folder / f for f in ("README.md", "README.txt", "README") if f in files), None)
        out.append({"path": _rel(root, folder), "depth": depth, "files": len(files),
                    "dirs": len(dirs), "markers": markers,
                    "readme": _read(readme, README_CHARS).strip() if readme else ""})
        if depth < MAX_DEPTH:
            queue += [(Path(d.path), depth + 1) for d in dirs
                      if d.name not in SKIP_DIRS and not d.name.startswith(".")]
    return out


def find_claude_files(root: Path, folders: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """CLAUDE.md files and ``.claude`` settings of the surveyed folders (and the root's parents)."""
    md: list[dict] = []
    settings: list[dict] = []
    seen: set[str] = set()
    candidates = [root / f["path"] for f in folders] + list(root.parents)[:3]
    for folder in candidates:
        for name in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md"):
            p = folder / name
            key = os.path.normcase(str(p))
            if key not in seen and p.is_file():
                seen.add(key)
                md.append({"path": _rel(root, p), "text": _read(p, CLAUDE_MD_CHARS)})
        for name in (".claude/settings.json", ".claude/settings.local.json"):
            p = folder / name
            key = os.path.normcase(str(p))
            if key in seen or not p.is_file():
                continue
            seen.add(key)
            try:
                data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            # Only what shapes rules; never env values (they may hold secrets).
            keep = {k: data[k] for k in ("permissions", "additionalDirectories", "model") if k in data}
            if keep:
                settings.append({"path": _rel(root, p), "settings": keep})
    return md, settings


def git_summary(root: Path) -> dict[str, Any]:
    """The repository the folder is in, and its recent commits with the folders they touched."""
    try:
        top, sub = gitutil.locate(root)
    except gitutil.GitError:
        return {"is_repo": False}
    out = gitutil.git(top, "log", f"-{MAX_COMMITS}", "--date=short", "--name-only",
                      "--format=%x1e%ad%x1f%s", "--", sub or ".", check=False)
    commits = []
    for block in out.split("\x1e")[1:]:
        head, _, names = block.partition("\n")
        date, _, subject = head.partition("\x1f")
        areas: list[str] = []
        for n in names.splitlines():
            n = n.strip()
            if not n:
                continue
            if sub:
                if not n.startswith(sub + "/"):
                    continue
                n = n[len(sub) + 1:]
            parts = n.split("/")
            area = "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else "(top)")
            if area not in areas:
                areas.append(area)
        commits.append({"date": date, "subject": subject.strip(), "areas": areas[:6]})
    return {"is_repo": True, "repo": str(top), "sub": sub, "commits": commits, "has_commits": gitutil.has_head(top),
            "dirty": bool(gitutil.git(top, "status", "--porcelain", "--", sub or ".", check=False).strip())}


def claude_code_sessions(root: Path) -> list[dict[str, Any]]:
    """Claude Code sessions that ran in the folder or below (newest last): what the user asked."""
    from carrier.debrief.transcripts import _parse_ts, _user_text, find_transcripts

    sessions = []
    for path in find_transcripts(root):
        prompts: list[str] = []
        summary = ""
        cwd = ""
        first = last = None
        try:
            fh = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") == "summary" and entry.get("summary"):
                    summary = str(entry["summary"])[:200]
                    continue
                cwd = cwd or entry.get("cwd") or ""
                t = _parse_ts(entry.get("timestamp"))
                if t:
                    first = first or t
                    last = t
                if entry.get("type") == "user" and not entry.get("isMeta") and not entry.get("isSidechain"):
                    text = _user_text((entry.get("message") or {}).get("content"))
                    if text and len(prompts) < PROMPTS_PER_SESSION:
                        prompts.append(text[:PROMPT_CHARS])
        if prompts:
            sessions.append({"id": path.stem, "cwd": _rel(root, Path(cwd)) if cwd else "",
                             "start": first, "end": last, "summary": summary, "prompts": prompts})
    sessions.sort(key=lambda s: s["start"] or 0)
    return sessions[-MAX_SESSIONS:]


def read_chat_export(path: Path) -> list[dict[str, Any]]:
    """Conversations from a claude.ai data export (the .zip, or its conversations.json)."""
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                name = next((n for n in zf.namelist() if n.endswith("conversations.json")), None)
                if name is None:
                    raise ImportError_("That export has no conversations.json.")
                raw = zf.read(name).decode("utf-8", errors="replace")
        else:
            raw = path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise ImportError_(f"Couldn't read the chat export: {exc}") from exc
    if not isinstance(data, list):
        raise ImportError_("That file doesn't look like a claude.ai conversations export.")
    chats = []
    for c in data:
        if not isinstance(c, dict):
            continue
        messages = []
        for m in c.get("chat_messages") or []:
            if not isinstance(m, dict):
                continue
            text = m.get("text") or "\n".join(
                b.get("text", "") for b in m.get("content") or [] if isinstance(b, dict) and b.get("type") == "text")
            if str(text).strip():
                messages.append({"sender": "user" if m.get("sender") == "human" else "assistant",
                                 "text": str(text).strip()})
        if messages:
            chats.append({"id": str(c.get("uuid") or len(chats)), "name": str(c.get("name") or "Untitled"),
                          "date": str(c.get("updated_at") or c.get("created_at") or "")[:10],
                          "messages": messages})
    return chats


def _keywords(root: Path, folders: list[dict[str, Any]]) -> set[str]:
    words = {root.name.lower()}
    for f in folders:
        if f["path"] and f["depth"] <= 2:
            name = f["path"].rsplit("/", 1)[-1].lower()
            if len(name) >= 4 and name not in {"docs", "test", "tests", "assets", "tools", "mods", "src", "lib"}:
                words.add(name)
    return words


def match_chats(chats: list[dict[str, Any]], words: set[str]) -> list[str]:
    """Ids of the chats that mention the workspace or one of its folder names."""
    picked = []
    for c in chats:
        hay = (c["name"] + " " + " ".join(m["text"][:3000] for m in c["messages"][:20])).lower()
        if any(w in hay for w in words):
            picked.append(c["id"])
    return picked


def scan(path: str, export: str = "", time_limit: float = 20.0) -> dict[str, Any]:
    """Everything the import needs to know about ``path``. Reads only."""
    root = Path(path).expanduser()
    if not root.is_dir():
        raise ImportError_(f"Folder not found: {path}")
    root = root.resolve()
    folders = survey_folders(root, time.monotonic() + time_limit)
    md, settings = find_claude_files(root, folders)
    chats = read_chat_export(Path(export).expanduser()) if export.strip() else []
    return {"root": str(root), "name": root.name, "folders": folders, "claude_md": md, "settings": settings,
            "git": git_summary(root), "sessions": claude_code_sessions(root), "chats": chats,
            "chats_selected": match_chats(chats, _keywords(root, folders))}


def scan_overview(ws: dict[str, Any]) -> dict[str, Any]:
    """The small summary the page shows (not the full texts)."""
    units = [f["path"] for f in ws["folders"] if f["path"] and f["markers"]]
    return {"root": ws["root"], "name": ws["name"], "folders": len(ws["folders"]), "units": units[:40],
            "claude_md": [m["path"] for m in ws["claude_md"]], "settings": [s["path"] for s in ws["settings"]],
            "git": {k: v for k, v in ws["git"].items() if k != "commits"} | {"commits": len(ws["git"].get("commits", []))},
            "sessions": len(ws["sessions"]),
            "chats": [{"id": c["id"], "name": c["name"], "date": c["date"], "messages": len(c["messages"])}
                      for c in ws["chats"]],
            "chats_selected": ws["chats_selected"]}


# -- 2. the digest and the conversation ---------------------------------------------------

def _when(t: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t)) if t else "?"


def build_digest(ws: dict[str, Any], chat_ids: list[str] | None = None, limit: int = DIGEST_CHARS) -> str:
    """One text with what was found, newest and most telling first, within ``limit``."""
    out: list[str] = [f"# Workspace: {ws['root']}"]
    g = ws["git"]
    if g.get("is_repo"):
        where = "the top of a git repository" if not g["sub"] else f"the folder `{g['sub']}` of the git repository {g['repo']}"
        if not g.get("has_commits", True):
            out.append(f"It is {where}, which has no commits yet: everything is uncommitted.")
        else:
            out.append(f"It is {where}" + (" (with uncommitted changes)." if g.get("dirty") else "."))
    else:
        out.append("It is not in a git repository.")
    out.append("\n## Folders (path — markers — files/subfolders)")
    for f in sorted(ws["folders"], key=lambda f: [x.lower() for x in f["path"].split("/")] if f["path"] else []):
        if f["depth"] <= MAX_DEPTH:
            m = ", ".join(f["markers"])
            out.append(f"{'  ' * f['depth']}- {f['path'] or '.'}/ — {m or '-'} — {f['files']} files, {f['dirs']} folders")
    if ws["claude_md"]:
        out.append("\n## CLAUDE.md files")
        for m in ws["claude_md"]:
            out.append(f"### {m['path']}\n{m['text']}")
    if ws["settings"]:
        out.append("\n## Claude Code settings (.claude)")
        for s in ws["settings"]:
            out.append(f"### {s['path']}\n{json.dumps(s['settings'], indent=1)[:3000]}")
    readmes = [f for f in ws["folders"] if f["readme"]]
    if readmes:
        out.append("\n## README excerpts")
        for f in readmes:
            out.append(f"### {f['path'] or '.'}/README\n{f['readme']}")
    if g.get("commits"):
        counts: dict[str, int] = {}
        for c in g["commits"]:
            for a in c["areas"]:
                counts[a] = counts.get(a, 0) + 1
        out.append(f"\n## Git history ({len(g['commits'])} recent commits)")
        out.append("Commits per area: " + ", ".join(f"{a} {n}" for a, n in sorted(counts.items(), key=lambda x: -x[1])[:30]))
        for c in g["commits"][:80]:
            out.append(f"- {c['date']} {c['subject']} [{', '.join(c['areas'])}]")
    text = "\n".join(out)

    tail: list[str] = []
    if ws["sessions"]:
        tail.append(f"\n## Claude Code sessions here ({len(ws['sessions'])}, oldest first) — what the user asked")
        for s in ws["sessions"]:
            head = f"### {_when(s['start'])}–{_when(s['end'])} in {s['cwd'] or '.'}"
            if s["summary"]:
                head += f" — {s['summary']}"
            tail.append(head + "\n" + "\n".join(f"- {p}" for p in s["prompts"]))
    chosen = [c for c in ws["chats"] if c["id"] in set(chat_ids if chat_ids is not None else ws["chats_selected"])]
    if chosen:
        tail.append(f"\n## Chats from the Claude app ({len(chosen)})")
        for c in chosen:
            lines = [f"### {c['date']} {c['name']}"]
            for m in c["messages"][:40]:
                cut = 600 if m["sender"] == "user" else 300
                lines.append(f"- {m['sender']}: {m['text'][:cut]}")
            tail.append("\n".join(lines))
    rest = "\n".join(tail)
    if len(text) + len(rest) > limit:
        # Keep the newest history: cut from the front of the history part.
        room = max(limit - len(text), limit // 3)
        text = text[: limit - room]
        rest = "…(older history cut)\n" + rest[-room:]
    return text + "\n" + rest


SYSTEM = """You help a user bring existing work into Carrier, a desktop app that runs Claude Code (an AI
coding agent) on *projects*. The user has been working in one folder with plain Claude Code or the Claude
app; you get a digest of that folder. Together you decide how to organise it into Carrier projects, then
you write the plan.

## Carrier's model
- A project has a **main folder** (where Claude Code runs; exclusive: no two projects share one), a
  **goal**, **rules** (instructions given to every task), **rule sets** (named, reusable bundles of rules
  shared by several projects), **tags**, and an ordered list of **tasks**.
- Extra folders: **work** (Claude may read and edit; reviewed), **reference** (read only), or
  **workspace**: a shared parent folder (e.g. the whole workspace) whose existing contents are read only,
  but where a NEW folder the project creates (a release tool, a publish folder) is added to the project
  as a work folder automatically. A workspace folder may contain the project's main folder.
  Another project's main folder may be added as work or reference.
- A main folder may be a subfolder of a bigger git repository: then only that folder is reviewed, so one
  workspace can be split into several projects *in place*, without moving anything.
- Claude Code still loads every CLAUDE.md from the main folder and its parent folders, so a workspace-wide
  CLAUDE.md keeps applying to projects inside it. Don't copy it into rules; move only project-specific
  parts into that project's rules if the user wants the CLAUDE.md slimmed down.
- A folder can't be inside one of the same project's other folders.

## How to talk
- Be brief and concrete. Start by saying what you found (the separate strands of work, in one line each)
  and propose a split; then ask the few questions you really need (at most three at a time).
- Name folders by their path relative to the workspace (absolute paths for anything outside it).
- Use what the history shows: which areas get the most work, what the user kept asking for, what looks
  unfinished — that becomes goals and open tasks. Say when something looks abandoned or like scratch.
- Never invent folders. Only propose moves if the user chose to reorganise; then keep moves few and
  explain each one, and add a task to fix paths in CLAUDE.md / scripts that the moves break.

## Asking questions
When you need the user to choose, ask with ONE fenced ```carrier-questions block (the page shows each
question as clickable options, and the user can always type their own answer):
[{"question": "Split in place or reorganise?", "header": "Layout", "multiSelect": false,
  "options": [{"label": "In place (Recommended)", "description": "Nothing moves."},
              {"label": "Reorganise", "description": "…"}]}]
1-4 questions, 2-4 options each; put a recommended option first with "(Recommended)" in its label. Keep the
question text itself in the block only (don't repeat it in your prose).

## Proposing a rule
When the user states something that should hold for future work ("never…", "always…", "from now on…"),
propose it with a fenced ```carrier-rule block; the user edits and approves it into the plan:
{"text": "- Never edit files in D:/GameInstall.", "project": "project name from the plan, or empty",
 "rule_set": "rule set name if it belongs in a shared set, else empty", "why": "one line"}

## The plan
Whenever you have a complete proposal (and every time it changes), end your reply with ONE fenced
```carrier-plan block holding JSON:
{"summary": "one paragraph",
 "moves": [{"from": "old/path", "to": "new/path", "why": "…"}],
 "rule_sets": [{"name": "…", "rules": "- rule\\n- rule"}],
 "projects": [{"name": "…", "goal": "2-5 sentences", "main": "path",
               "folders": [{"path": "…", "kind": "work|reference|workspace", "label": "short-name"}],
               "rules": "- rule\\n- rule", "rule_sets": ["names from rule_sets"], "tags": ["…"],
               "tasks": [{"title": "…", "details": "…"}]}]}
Paths are relative to the workspace (forward slashes) unless outside it; main and folder paths are where
things will be AFTER the moves. Rules are short imperative "- " bullets. Tasks are the next real pieces of
work, most important first (at most eight per project). "moves" is [] when working in place.
A project's main folder belongs to that project alone: another project can only have it (or anything
inside it) as a reference folder. A work folder used by two projects is a shared space."""

MODES = {
    "in_place": "The user wants to split the work IN PLACE: nothing moves; projects use existing folders.",
    "reorganize": "The user is open to REORGANISING: you may propose moving folders into a cleaner layout "
                  "(they approve every move).",
    "ask": "Ask the user whether to split in place (nothing moves) or reorganise folders, with your recommendation.",
}

def _block_re(name: str) -> re.Pattern[str]:
    return re.compile(r"```" + name + r"\s*\n(.*?)```", re.DOTALL)


_PLAN_RE = _block_re(PLAN_FENCE)
_QUESTIONS_RE = _block_re("carrier-questions")
_RULE_RE = _block_re("carrier-rule")


def clean_questions(data: Any) -> list[dict[str, Any]]:
    """Questions in the shape the page (and Claude Code's AskUserQuestion) uses."""
    if isinstance(data, dict):
        data = data.get("questions", [data])
    out = []
    for q in data if isinstance(data, list) else []:
        if not isinstance(q, dict) or not str(q.get("question", "")).strip():
            continue
        options = []
        for o in q.get("options") or []:
            if isinstance(o, str):
                o = {"label": o}
            if isinstance(o, dict) and str(o.get("label", "")).strip():
                options.append({"label": str(o["label"]).strip()[:120],
                                "description": str(o.get("description", "")).strip()[:300]})
        out.append({"question": str(q["question"]).strip()[:500], "header": str(q.get("header", "")).strip()[:30],
                    "multiSelect": bool(q.get("multiSelect")), "options": options[:6]})
    return out[:4]


def clean_rule(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict) or not str(data.get("text", "")).strip():
        return None
    return {"text": str(data["text"]).strip()[:4000], "project": str(data.get("project", "")).strip()[:200],
            "rule_set": str(data.get("rule_set", "")).strip()[:200], "why": str(data.get("why", "")).strip()[:500]}


def split_reply(reply: str) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
    """The text to show, the plan if the reply has one, and any questions and proposed rules."""
    plan = None
    extras: dict[str, Any] = {"questions": [], "rules": []}

    def take(rx: re.Pattern[str], text: str) -> tuple[str, list[Any]]:
        found = []
        for m in rx.finditer(text):
            try:
                found.append(extract_json(m.group(1)))
            except ValueError:
                continue
        return rx.sub("", text), found

    text, plans = take(_PLAN_RE, reply)
    plans = [p for p in plans if isinstance(p, dict)]
    if plans:
        plan = plans[-1]
    text, qs = take(_QUESTIONS_RE, text)
    for q in qs:
        extras["questions"] += clean_questions(q)
    extras["questions"] = extras["questions"][:4]
    text, rules = take(_RULE_RE, text)
    for r in rules:
        for item in r if isinstance(r, list) else [r]:
            if (c := clean_rule(item)) is not None:
                extras["rules"].append({**c, "status": "proposed"})
    return text.strip(), plan, extras


@dataclass
class ImportChat:
    """One import, saved to disk after every turn so it survives a restart."""

    id: str
    root: str
    mode: str = "ask"
    model: str = "sonnet"
    chat_ids: list[str] = field(default_factory=list)
    digest: str = ""
    overview: dict[str, Any] = field(default_factory=dict)
    messages: list[dict[str, str]] = field(default_factory=list)  # {"role": "user"|"assistant", "text"}
    plan: dict[str, Any] | None = None
    created: float = field(default_factory=time.time)
    done: bool = False

    @classmethod
    def start(cls, path: str, mode: str = "ask", export: str = "", chat_ids: list[str] | None = None,
              model: str = "sonnet") -> ImportChat:
        if mode not in MODES:
            raise ImportError_(f"Unknown import mode: {mode}")
        ws = scan(path, export)
        ids = chat_ids if chat_ids is not None else ws["chats_selected"]
        return cls(id=uuid.uuid4().hex[:12], root=ws["root"], mode=mode, model=model or "sonnet",
                   chat_ids=list(ids), digest=build_digest(ws, ids), overview=scan_overview(ws))

    # -- persistence -----------------------------------------------------------------

    @staticmethod
    def folder() -> Path:
        return usersettings.config_dir() / "imports"

    def save(self) -> None:
        self.folder().mkdir(parents=True, exist_ok=True)
        tmp = self.folder() / f"{self.id}.json.tmp"
        tmp.write_text(json.dumps(asdict(self)), encoding="utf-8")
        os.replace(tmp, self.folder() / f"{self.id}.json")

    @classmethod
    def load(cls, iid: str) -> ImportChat:
        if not re.fullmatch(r"[0-9a-f]{12}", iid or ""):
            raise ImportError_("Unknown import.")
        try:
            data = json.loads((cls.folder() / f"{iid}.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ImportError_("That import is gone.") from exc
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    @classmethod
    def recent(cls) -> list[dict[str, Any]]:
        out = []
        for p in sorted(cls.folder().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
            try:
                c = cls.load(p.stem)
            except ImportError_:
                continue
            if not c.done:
                out.append({"id": c.id, "root": c.root, "turns": len(c.messages), "created": c.created})
        return out

    def discard(self) -> None:
        try:
            (self.folder() / f"{self.id}.json").unlink()
        except OSError:
            pass

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "root": self.root, "mode": self.mode, "model": self.model,
                "overview": self.overview, "messages": self.messages, "plan": self.plan}

    # -- talking -----------------------------------------------------------------------

    def _prompt(self) -> str:
        convo = "\n\n".join(f"**{'User' if m['role'] == 'user' else 'You'}:** {m['text']}" for m in self.messages)
        plan = ("\n\n## The current plan (the user may have edited it; build on this version)\n```json\n"
                + json.dumps(self.plan, indent=1)[:20000] + "\n```") if self.plan else ""
        ask = ("Reply to the user's last message." if self.messages else
               "Open the conversation: what you found, a proposed split, and your questions.")
        return (f"{MODES[self.mode]}\n\n<digest>\n{self.digest}\n</digest>{plan}\n\n## Conversation so far\n"
                f"{convo or '(none yet)'}\n\n{ask}")

    async def turn_async(self, backend: Backend, text: str = "") -> dict[str, Any]:
        if text.strip():
            self.messages.append({"role": "user", "text": text.strip()[:8000]})
        try:
            reply = await backend.ask(system=SYSTEM, prompt=self._prompt(), model=self.model or None)
        except BackendError:
            if text.strip():
                self.messages.pop()  # let the user send it again
            raise
        shown, plan, extras = split_reply(reply)
        if plan is not None:
            self.plan = plan
            shown = shown or "Here's the plan — review it below."
        else:
            try:  # keep edits the user saved while Claude was answering
                self.plan = ImportChat.load(self.id).plan
            except ImportError_:
                pass
        if extras["questions"] and not shown:
            shown = "A few questions:"
        self.messages.append({"role": "assistant", "text": shown, **{k: v for k, v in extras.items() if v}})
        self.save()
        return self.view()

    def rule_done(self, message: int, index: int, status: str) -> dict[str, Any]:
        """Remember that a proposed rule was added to the plan or dismissed."""
        if status not in ("applied", "dismissed"):
            raise ImportError_(f"Unknown rule status: {status}")
        try:
            self.messages[message]["rules"][index]["status"] = status
        except (IndexError, KeyError, TypeError) as exc:
            raise ImportError_("That proposal is gone.") from exc
        self.save()
        return self.view()

    def turn(self, backend: Backend, text: str = "") -> dict[str, Any]:
        """Blocking wrapper for the desktop API (own thread, own event loop)."""
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(lambda: asyncio.run(self.turn_async(backend, text))).result()
        except BackendError as exc:
            raise ImportError_(f"Claude couldn't answer: {exc}") from exc

    def set_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(plan, dict):
            raise ImportError_("The plan must be a JSON object.")
        self.plan = plan
        self.save()
        return self.view()


# -- 3. checking and applying the plan ------------------------------------------------------

def _resolve(root: Path, p: str) -> Path:
    q = Path(str(p).strip())
    return (q if q.is_absolute() else root / q).resolve()


def _resolve_link(root: Path, p: str) -> Path:
    """Like :func:`_resolve`, but a link itself is not followed (moving a link must not move its target)."""
    q = Path(str(p).strip())
    q = q if q.is_absolute() else root / q
    if q.name in ("", ".", ".."):
        return q.resolve()
    return q.parent.resolve() / q.name


def _fold(p: Path) -> str:
    """Path for comparing: Windows and macOS file names ignore case."""
    text = os.path.normcase(os.path.normpath(str(p)))
    return text.lower() if sys.platform == "darwin" else text


def _inside(child: Path, parent: Path) -> bool:
    c, q = _fold(child), _fold(parent)
    return c == q or c.startswith(q.rstrip(os.sep) + os.sep)


def _is_link(p: Path) -> bool:
    try:
        return p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())
    except OSError:
        return False


def validate_plan(root: str | Path, plan: dict[str, Any], existing_mains: set[str] | None = None,
                  existing_rule_sets: set[str] | None = None, allow_moves: bool = True
                  ) -> tuple[dict[str, Any], list[str]]:
    """Normalise ``plan`` and list every problem (an empty list means it can be applied)."""
    root = Path(root).resolve()
    problems: list[str] = []
    existing = [Path(m) for m in existing_mains or set()]
    existing_rule_sets = {n.lower() for n in existing_rule_sets or set()}
    if not isinstance(plan, dict):
        return {}, ["The plan must be a JSON object."]

    moves = []
    for i, m in enumerate(plan.get("moves") or []):
        if not isinstance(m, dict) or not str(m.get("from", "")).strip() or not str(m.get("to", "")).strip():
            problems.append(f"Move {i + 1} needs a from and a to.")
            continue
        src, dst = _resolve_link(root, m["from"]), _resolve(root, m["to"])
        moves.append({"from": _rel(root, src), "to": _rel(root, dst), "why": str(m.get("why", "")).strip(),
                      "_src": src, "_dst": dst})
    if moves and not allow_moves:
        problems.append("This import is set to work in place, but the plan moves folders.")
    after: set[Path] = set()  # destinations that will exist
    gone: set[Path] = set()   # sources that will be gone
    for i, m in enumerate(moves):
        src, dst = m["_src"], m["_dst"]
        label = f"{m['from']} → {m['to']}"
        if not (_inside(src, root) and _inside(dst, root)) or _fold(src) == _fold(root):
            problems.append(f"Moves stay inside the workspace: {label}.")
            continue
        if _is_link(src):
            problems.append(f"{m['from']} is a link (shortcut, symlink or junction); move what it points to instead.")
        elif not src.exists():
            problems.append(f"Nothing to move at {m['from']}.")
        elif (src / ".git").is_file():
            problems.append(f"{m['from']} is a git submodule or worktree; Carrier can't move those.")
        if dst.exists() and _fold(dst) != _fold(src):
            problems.append(f"{m['to']} already exists; pick another place for {m['from']}.")
        if _inside(dst, src):
            problems.append(f"Can't move {m['from']} into itself.")
        # Moves must not touch each other: one destination inside another's
        # folder, or a folder that another move takes away, would mix them up.
        for j, o in enumerate(moves):
            if j == i:
                continue
            if _inside(dst, o["_dst"]) or _inside(o["_dst"], dst):
                if j > i:
                    problems.append(f"{m['to']} and {o['to']} overlap; move them to separate places.")
            elif _inside(dst, o["_src"]):
                problems.append(f"{m['to']} is inside {o['from']}, which moves too; move them one at a time.")
            elif j > i and (_inside(src, o["_src"]) or _inside(o["_src"], src)):
                problems.append(f"{m['from']} and {o['from']} are nested; move the outer folder only.")
        after.add(dst)
        gone.add(src)

    def exists_after(p: Path) -> bool:
        if any(_inside(p, d) or _inside(d, p) for d in after):
            return True  # a destination, inside one, or a folder made to hold one
        return p.exists() and not any(_inside(p, g) for g in gone)

    sets = []
    names: set[str] = set()
    for rs in plan.get("rule_sets") or []:
        if not isinstance(rs, dict) or not str(rs.get("name", "")).strip():
            problems.append("Every rule set needs a name.")
            continue
        name = str(rs["name"]).strip()
        if name.lower() in names:
            problems.append(f"Two rule sets are called {name}.")
        names.add(name.lower())
        sets.append({"name": name, "rules": str(rs.get("rules", "")).strip()})

    projects = []
    mains: dict[str, tuple[Path, str]] = {}
    pnames: set[str] = set()
    for i, p in enumerate(plan.get("projects") or []):
        if not isinstance(p, dict):
            problems.append(f"Project {i + 1} isn't an object.")
            continue
        name = str(p.get("name", "")).strip()
        label = name or f"Project {i + 1}"
        if not name:
            problems.append(f"{label} needs a name.")
        elif name.lower() in pnames:
            problems.append(f"Two projects are called {name}.")
        pnames.add(name.lower())
        main_raw = str(p.get("main", "")).strip()
        main = _resolve(root, main_raw) if main_raw else None
        if main is None:
            problems.append(f"{label} needs a main folder.")
        else:
            key = _fold(main)
            if not exists_after(main):
                problems.append(f"{label}: the main folder {main_raw} doesn't exist.")
            if key in mains:
                problems.append(f"{label} and {mains[key][1]} have the same main folder.")
            elif any(_fold(e) == key for e in existing):
                problems.append(f"{label}: {main_raw} is already a Carrier project's main folder.")
            else:
                # Nested main folders would review (and let Claude edit) each other's files.
                for other, other_label in mains.values():
                    if _inside(main, other) or _inside(other, main):
                        problems.append(f"{label}'s main folder and {other_label}'s are inside one another.")
                for e in existing:
                    if _inside(main, e) or _inside(e, main):
                        problems.append(f"{label}: {main_raw} is inside (or contains) an existing project's "
                                        f"main folder ({e}).")
            mains[key] = (main, label)
        folders = []
        for f in p.get("folders") or []:
            if isinstance(f, str):
                f = {"path": f}
            if not isinstance(f, dict) or not str(f.get("path", "")).strip():
                problems.append(f"{label}: an extra folder has no path.")
                continue
            fp = _resolve(root, f["path"])
            kind = str(f.get("kind", "work")).strip().lower()
            if kind not in ("work", "reference", "workspace"):
                problems.append(f"{label}: folder {f['path']} must be work, reference or workspace.")
                kind = "work"
            if not exists_after(fp):
                problems.append(f"{label}: the folder {f['path']} doesn't exist.")
            # A workspace folder may contain the project's other folders (it's their shared parent).
            if main is not None and (_inside(fp, main) or (_inside(main, fp) and kind != "workspace")):
                problems.append(f"{label}: {f['path']} is inside (or contains) its main folder.")
            if any((_inside(fp, Path(o["_path"])) and o["kind"] != "workspace")
                   or (_inside(Path(o["_path"]), fp) and kind != "workspace") for o in folders):
                problems.append(f"{label}: {f['path']} overlaps another of its folders.")
            folders.append({"path": _rel(root, fp) if _inside(fp, root) else str(fp), "_path": str(fp),
                            "kind": kind, "label": str(f.get("label", "")).strip()})
        refs = [str(n).strip() for n in p.get("rule_sets") or [] if str(n).strip()]
        for n in refs:
            if n.lower() not in names and n.lower() not in existing_rule_sets:
                problems.append(f"{label} uses the rule set {n}, which isn't in the plan.")
        tasks = []
        for t in p.get("tasks") or []:
            if isinstance(t, str):
                t = {"title": t}
            if isinstance(t, dict) and str(t.get("title", "")).strip():
                tasks.append({"title": str(t["title"]).strip()[:200], "details": str(t.get("details", "")).strip()})
        tags = p.get("tags") or []
        projects.append({"name": name, "goal": str(p.get("goal", "")).strip(),
                         "main": (_rel(root, main) if main is not None and _inside(main, root) else main_raw),
                         "_main": str(main) if main is not None else "", "folders": folders,
                         "rules": str(p.get("rules", "")).strip(), "rule_sets": refs,
                         "tags": [str(t).strip() for t in (tags if isinstance(tags, list) else str(tags).split(","))
                                  if str(t).strip()],
                         "tasks": tasks})
    if not projects:
        problems.append("The plan has no projects.")
    clean = {"summary": str(plan.get("summary", "")).strip(), "moves": moves, "rule_sets": sets,
             "projects": projects}
    return clean, problems


def public(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan without the internal ``_`` keys (for the page and for saving)."""
    if isinstance(plan, dict):
        return {k: public(v) for k, v in plan.items() if not k.startswith("_")}
    if isinstance(plan, list):
        return [public(v) for v in plan]
    return plan


def _git_entries(src: Path) -> tuple[Path | None, str, list[tuple[str, str, str, str]]]:
    """``(repository, src's path in it, index entries under src)``; entries are ``(mode, sha, stage, path)``."""
    try:
        repo, rel = gitutil.locate(src if src.is_dir() and not _is_link(src) else src.parent)
    except gitutil.GitError:
        return None, "", []
    if not src.is_dir() or _is_link(src):
        rel = f"{rel}/{src.name}" if rel else src.name
    if not rel:
        return repo, rel, []  # src is a repository's own top folder: git needs no update
    out = gitutil.git(repo, "--literal-pathspecs", "ls-files", "-s", "-z", "--", rel)
    entries = []
    for e in out.split("\0"):
        meta, tab, path = e.partition("\t")
        if not tab:
            continue
        if path != rel and not path.startswith(rel + "/"):
            continue  # never touch anything outside the moved folder
        mode, sha, stage = meta.split()
        entries.append((mode, sha, stage, path))
    return repo, rel, entries


def _prune_empty(folder: Path, stop: Path) -> None:
    """Remove empty folders from ``folder`` up to (not including) ``stop``."""
    while _inside(folder, stop) and _fold(folder) != _fold(stop):
        try:
            folder.rmdir()  # only succeeds when empty
        except OSError:
            return
        folder = folder.parent


def _move(src: Path, dst: Path) -> str:
    """Move one folder or file, keeping git's view of it.

    The move itself is a plain rename (fast, and takes ignored and untracked
    files along). If git tracks files there, the index is updated to the new
    paths too — what ``git mv`` records, so history follows the files and
    nothing else gets staged. If git can't be updated, the rename is undone.
    """
    if dst.exists() or _is_link(dst):
        raise ImportError_(f"{dst} already exists.")
    repo, rel_src, entries = _git_entries(src)
    if any(mode == "160000" for mode, *_ in entries):
        raise ImportError_(f"{src} holds a git submodule; Carrier can't move those.")
    if any(stage != "0" for _m, _s, stage, _p in entries):
        raise ImportError_(f"{src} has unresolved merge conflicts; resolve them first.")
    rel_dst = Path(os.path.relpath(dst, repo)).as_posix() if repo is not None and entries else ""
    if rel_dst.startswith(".."):
        entries = []  # leaving the repository: it's just a move
    made = dst.parent
    while not made.parent.exists():
        made = made.parent
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    if repo is None or not entries:
        return "plain"
    records = []
    for mode, sha, _stage, path in entries:
        records.append(f"0 {'0' * len(sha)}\t{path}\0")
        records.append(f"{mode} {sha} 0\t{rel_dst + path[len(rel_src):]}\0")
    proc = subprocess.run(["git", "update-index", "-z", "--index-info"], cwd=repo, input="".join(records),
                          check=False, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        shutil.move(str(dst), str(src))  # put it back: nothing half-done
        _prune_empty(dst.parent, made.parent)
        raise ImportError_(f"Couldn't update git for {src}: {proc.stderr.strip()}")
    return "git"


def _index_locked(root: Path) -> Path | None:
    try:
        git_dir = Path(gitutil.git(root, "rev-parse", "--absolute-git-dir").strip())
    except gitutil.GitError:
        return None
    lock = git_dir / "index.lock"
    return lock if lock.exists() else None


def apply_plan(room: Any, root: str | Path, plan: dict[str, Any], *, baseline: bool = True) -> dict[str, Any]:
    """Carry out a validated plan. ``room`` is the Ready Room manager.

    Order: moves, rule sets, projects (folders, tasks). If anything fails,
    what was created is removed and the moves are undone, newest first.
    ``baseline`` marks the current state as already reviewed in Debrief, so a
    project's first review covers only new work.
    """
    root = Path(root).resolve()
    existing = {os.path.normcase(p["path"]) for p in room.store.projects()}
    rs_names = {r["name"].lower(): r["id"] for r in room.store.rule_sets()}
    clean, problems = validate_plan(root, plan, existing, set(rs_names))
    if problems:
        raise ImportError_("Fix the plan first: " + " ".join(problems))
    if clean["moves"] and (lock := _index_locked(root)):
        raise ImportError_(f"Git is busy in this folder ({lock} exists). Close other git tools and try again.")
    moved: list[tuple[Path, Path]] = []
    made_sets: list[int] = []
    made_projects: list[int] = []
    try:
        for m in clean["moves"]:
            _move(m["_src"], m["_dst"])
            moved.append((m["_src"], m["_dst"]))
        for rs in clean["rule_sets"]:
            if rs["name"].lower() in rs_names:
                continue  # reuse the user's own rule set of that name
            before = {r["id"] for r in room.store.rule_sets()}
            room.save_rule_set(rs["name"], rs["rules"])
            new = next(r["id"] for r in room.store.rule_sets() if r["id"] not in before)
            made_sets.append(new)
            rs_names[rs["name"].lower()] = new
        for p in clean["projects"]:
            pid = room.create_project(p["name"], p["_main"], goal=p["goal"], rules=p["rules"], tags=p["tags"],
                                      rule_set_ids=[rs_names[n.lower()] for n in p["rule_sets"]])["id"]
            made_projects.append(pid)
            for f in p["folders"]:
                kind = f["kind"]
                if kind == "work" and room._main_of_other(pid, str(Path(f["_path"]).resolve())):
                    kind = "reference"  # another project's main folder belongs to that project alone
                room.add_folder(pid, f["_path"], label=f["label"], kind=kind)
            for t in p["tasks"]:
                room.add_task(pid, t["title"], t["details"])
    except Exception as exc:
        for pid in reversed(made_projects):
            try:
                room.store.remove_project(pid)
            except Exception:
                pass
        for rid in made_sets:
            try:
                room.delete_rule_set(rid)
            except Exception:
                pass
        undone = []
        for src, dst in reversed(moved):
            try:
                _move(dst, src)
                undone.append(dst)
                _prune_empty(dst.parent, root)
            except Exception:
                pass
        note = "" if len(undone) == len(moved) else " Some moves could not be undone; check the folders."
        raise ImportError_(f"Import stopped: {exc}. Nothing was kept.{note}") from exc

    warnings: list[str] = []
    if baseline:
        for pid in made_projects:
            warnings += mark_reviewed(room.store, pid)
    return {"projects": made_projects, "moves": len(moved), "rule_sets": len(made_sets), "warnings": warnings}


# Folders and files almost nobody wants in git (checked against what's actually there).
IGNORE_SUGGESTIONS = ("node_modules/", ".cache/", "__pycache__/", ".venv/", "venv/", "dist/", "build/", "out/",
                      "runtime/", ".pytest_cache/", ".mypy_cache/", "*.log", ".DS_Store", "Thumbs.db")


def suggest_gitignore(root: str | Path) -> list[str]:
    """Lines worth adding to .gitignore before a first commit: build output, caches and
    dependency folders that exist here, plus key and secret files (see core.secrets)."""
    from carrier.core import secrets

    root = Path(root)
    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        names = set(dirnames)
        for pat in IGNORE_SUGGESTIONS:
            if pat.endswith("/") and pat[:-1] in names:
                found.add(pat)
            elif not pat.endswith("/") and any(Path(f).match(pat) for f in filenames):
                found.add(pat)
        dirnames[:] = [d for d in dirnames if d not in {p.rstrip("/") for p in IGNORE_SUGGESTIONS} and d != ".git"
                       and depth < 4]
    lines = [p for p in IGNORE_SUGGESTIONS if p in found]
    have = (root / ".gitignore").read_text(encoding="utf-8", errors="replace").splitlines() \
        if (root / ".gitignore").exists() else []
    lines += [p for p in secrets.SECRET_FILES] + [f"!{p}" for p in secrets.SAFE_FILES]
    return [ln for ln in lines if ln not in have or ln.startswith("!")]


def first_commit(root: str | Path, ignore_lines: list[str] | None = None) -> str:
    """Start git history for an imported workspace: ``git init`` if needed, add the
    .gitignore lines, and commit everything else. Does nothing if it already has commits.
    Returns what happened."""
    root = Path(root).resolve()
    if gitutil.in_repo(root):
        top = gitutil.repo_root(root).resolve()
        if gitutil.has_head(top):
            return "It already has git history."
        if top != root:
            # Inside a bigger, empty repository (e.g. an accidental `git init` in the home
            # folder): never commit that one — start the workspace's own history instead.
            gitutil.git(root, "init", "-q")
    else:
        gitutil.git(root, "init", "-q")
    repo = gitutil.repo_root(root)
    if repo.resolve() != root:
        raise ImportError_(f"Couldn't start a repository in {root}.")
    lines = [ln.strip() for ln in ignore_lines or [] if ln.strip()]
    if lines:
        from carrier.core.secrets import append_gitignore

        append_gitignore(repo / ".gitignore", lines, "Added by Carrier on import")
    gitutil.git(repo, "add", "-A")
    env = {}
    if not gitutil.git(repo, "config", "user.email", check=False).strip():
        env = {"GIT_AUTHOR_NAME": "Carrier", "GIT_AUTHOR_EMAIL": "carrier@localhost",
               "GIT_COMMITTER_NAME": "Carrier", "GIT_COMMITTER_EMAIL": "carrier@localhost"}
    gitutil.git(repo, "commit", "-q", "--allow-empty", "-m", "Start of history (imported into Carrier)", env=env)
    return "Made a first commit, so reviews have a starting point."


def mark_reviewed(store: Any, pid: int) -> list[str]:
    """Set the project's Debrief checkpoints to the current state of its folders."""
    p = store.project(pid)
    warnings = []
    done: set[str] = set()
    for _label, folder in store.project_roots(p):
        try:
            repo = gitutil.repo_root(Path(folder))
        except gitutil.GitError:
            continue  # not in git yet: Debrief offers to start tracking it
        key = os.path.normcase(str(repo))
        if key in done:
            continue
        done.add(key)
        try:
            sha = gitutil.snapshot_worktree(repo, keep_ref=f"refs/carrier/import/{pid}")
            gitutil.set_checkpoint(repo, sha, str(pid))
            gitutil.git(repo, "update-ref", "-d", f"refs/carrier/import/{pid}", check=False)
        except gitutil.GitError as exc:
            warnings.append(f"{p['name']}: couldn't mark {repo} as reviewed ({exc}).")
    return warnings
