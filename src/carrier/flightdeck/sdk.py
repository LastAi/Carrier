# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Adapter between Flight Deck and the Claude Agent SDK.

This is the only Flight Deck module that imports the SDK (``core/llm.py``
does the same for one-shot calls). It does three things:

1. :func:`build_options` — configure a session so it behaves like **normal
   Claude Code**: the Claude Code system prompt, your user/project/local
   settings and CLAUDE.md files, all tools, and your chosen permission mode.
   Flight Deck adds only a short note to the system prompt, its hooks, and a
   permission callback that shows prompts in the Carrier window.
2. :func:`normalize` — turn SDK message objects into small JSON *events*
   that the UI renders and the store persists (see ``EVENT TYPES`` below).
3. Small helpers to build hook outputs and permission results.

EVENT TYPES (dicts with a ``type`` key)::

    user        {text}                          a message the user sent
    text        {text}                          Claude's reply text
    thinking    {text}                          extended thinking (collapsed in UI)
    live        {text}                          streaming preview (not persisted)
    tool_use    {id, name, summary, input}      Claude used a tool
    tool_result {id, text, is_error}            the tool's output (truncated)
    result      {cost, turns, is_error, text}   end of a turn
    permission  {req_id, tool, summary, reason} Claude Code asks for approval
    permission_done {req_id, decision}
    notice      {kind, text}                    coordination notice (Flight Deck)
    status      {status}
    error       {text}
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from carrier.core.models import effective_effort

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
MAX_RESULT_CHARS = 4000

SYSTEM_APPEND = """\
You are running inside Carrier Flight Deck, which may run several Claude Code
sessions in parallel on related projects. Flight Deck may add notes, marked
[Carrier Flight Deck], about other sessions' work. Treat them as information:
avoid undoing other sessions' changes, keep shared files compatible, and say
in your reply when your change affects another project.

Carrier gives you tools (mcp__carrier__*):
- propose_rule: when the user states something that should hold for future
  work ("never…", "always…", "from now on…") or asks you to remember a rule.
- propose_skill: when a procedure you worked out is worth reusing (a release
  checklist, a build-and-test routine), or the user asks for a skill.
- list_projects / get_project: read the user's other projects' setup, e.g.
  to copy or adapt their rules and skills (then propose them).
- create_work_folder: for a new folder inside a workspace folder.
- list_tasks / propose_work: when work comes up that this task didn't ask
  for, don't just do it or drop it — ask with propose_work where it belongs
  (the current task, or a queue), with a suggestion based on list_tasks.
  Work for a LINKED project (see Linked projects): propose_work with
  project = its name; list_tasks with project shows its queues.
- propose_task_change: when this task needs work planned in another task,
  or part of it belongs elsewhere, so the tasks match what is really done.
- show_image: show the user a screenshot or picture you made.
The user reviews proposals before anything is saved; don't wait for them.
When you need the user to choose between options, use AskUserQuestion (or
mcp__carrier__ask_user if that fails)."""

PROPOSE_RULE = "mcp__carrier__propose_rule"


IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
_S = {"type": "string"}
# Carrier's own tools for Flight Deck sessions: name -> (description, input schema).
TOOLS: dict[str, tuple[str, dict[str, Any]]] = {
    "propose_rule": (
        "Propose a lasting rule for this project (or all projects) to the user. They edit and approve it; "
        "it applies to future sessions. Returns immediately.",
        {"type": "object", "required": ["text"], "properties": {
            "text": {**_S, "description": "The rule as short imperative '- ' bullet(s)."},
            "scope": {**_S, "enum": ["project", "global", "rule_set"],
                      "description": "Where it belongs: this project, every project, or a shared rule set."},
            "rule_set": {**_S, "description": "For scope rule_set: the set's name."},
            "why": {**_S, "description": "One line: what prompted it (e.g. what the user said)."}}}),
    "list_projects": (
        "List the user's Carrier projects: name, goal (first line), main folder and how far along they are. "
        "Read only.",
        {"type": "object", "properties": {}}),
    "get_project": (
        "Read another Carrier project's setup: goal, rules, rule sets, folders, open tasks and skills — e.g. "
        "to copy or adapt its rules for this project (then use propose_rule / propose_skill). Read only.",
        {"type": "object", "required": ["name"], "properties": {"name": {**_S, "description": "Project name."}}}),
    "create_work_folder": (
        "Create a NEW folder inside one of this project's workspace folders and add it to this project as a "
        "work folder (for work that belongs outside the main folder, e.g. a release tool).",
        {"type": "object", "required": ["path"], "properties": {
            "path": {**_S, "description": "Absolute path of the new folder, inside a workspace folder."}}}),
    "propose_skill": (
        "Propose a Claude Code skill (a reusable procedure) for this project. The user reviews, edits and "
        "approves it; it's saved as .claude/skills/<name>/SKILL.md in the project. Returns immediately.",
        {"type": "object", "required": ["name", "description", "body"], "properties": {
            "name": {**_S, "description": "kebab-case name, e.g. release-mod."},
            "description": {**_S, "description": "One sentence: when to use it (Claude Code reads this to "
                                                  "decide)."},
            "body": {**_S, "description": "The instructions (markdown): steps, commands, checks."},
            "why": {**_S, "description": "One line: why this is worth a skill."}}}),
    "ask_user": (
        "Ask the user one to four multiple-choice questions, shown as option cards (they can also type their own "
        "answer). Use this if AskUserQuestion fails or isn't available. WAITS for the answers.",
        {"type": "object", "required": ["questions"], "properties": {"questions": {"type": "array", "items": {
            "type": "object", "required": ["question", "options"], "properties": {
                "question": {**_S}, "header": {**_S, "description": "A short label, 1-3 words."},
                "multiSelect": {"type": "boolean"},
                "options": {"type": "array", "items": {"type": "object", "required": ["label"], "properties": {
                    "label": {**_S}, "description": {**_S}}}}}}}}}),
    "list_tasks": (
        "Read this project's work: the main queue and each task group (priority, tags, tasks in order, with "
        "ids), the routines (trigger, group, review gate), and which task this session is on. Read it before "
        "suggesting where new work belongs. With project: a LINKED project's queues instead (read only).",
        {"type": "object", "properties": {
            "project": {**_S, "description": "A linked project's name, to see its queues (optional)."}}}),
    "propose_work": (
        "New work came up that the current task didn't ask for. Ask the user where it belongs, with your "
        "suggestion: add it to the current task, or a new task in the main queue, an existing task group, or a "
        "new group (and where in that queue). Call list_tasks first and base the suggestion on the project's "
        "queues and routines. WAITS for the user; the reply says what they chose. Don't do the work in this "
        "session unless they added it to the current task.",
        {"type": "object", "required": ["title", "details", "suggest"], "properties": {
            "title": {**_S, "description": "Short title for the work."},
            "details": {**_S, "description": "What to do and what 'done' means."},
            "why": {**_S, "description": "How it came up."},
            "project": {**_S, "description": "For work a LINKED project should do: its name (then suggest is main, "
                                             "group or new_group in THAT project's queues). Omit for this project."},
            "wait": {"type": "boolean", "description": "With project: this session's task can't be finished until "
                                                       "that work is done (it waits on it)."},
            "suggest": {**_S, "enum": ["current", "main", "group", "new_group"],
                        "description": "Where you think it belongs."},
            "group": {**_S, "description": "For suggest=group: the group's name."},
            "new_group": {**_S, "description": "For suggest=new_group: a name for the new group."},
            "new_group_priority": {**_S, "enum": ["high", "normal", "low", "backlog"]},
            "position": {**_S, "description": "Where in that queue: 'next', 'end', or 'after:<task id>'."},
            "reasoning": {**_S, "description": "One or two sentences: why there (mention related tasks or "
                                             "routines)."}}}),
    "propose_task_change": (
        "When this task needs work that is planned in ANOTHER task, or part of this task belongs elsewhere: "
        "ask the user to change the tasks so the record matches what is actually done. kind: 'pull_in' (take "
        "another task in whole), 'pull_part' (take part of another task; give 'part' and what 'remaining' stays "
        "there), 'split' (move part of THIS task out; give 'keep', 'new_title', 'new_details'). WAITS for the "
        "user, who can edit everything.",
        {"type": "object", "required": ["kind", "why"], "properties": {
            "kind": {**_S, "enum": ["pull_in", "pull_part", "split"]},
            "task_id": {"type": "integer", "description": "The other task (pull_in / pull_part)."},
            "part": {**_S, "description": "pull_part: the part to bring into this task."},
            "remaining": {**_S, "description": "pull_part: the other task's details afterwards."},
            "keep": {**_S, "description": "split: this task's details afterwards."},
            "new_title": {**_S}, "new_details": {**_S},
            "group": {**_S, "description": "split: group name for the new task (empty = main queue)."},
            "why": {**_S, "description": "Why the tasks should change."}}}),
    "show_image": (
        "Show the user an image file (PNG, JPEG, GIF, WebP or SVG) in the chat, e.g. a screenshot you took "
        "or a diagram you made, with an optional caption.",
        {"type": "object", "required": ["path"], "properties": {
            "path": {**_S, "description": "Absolute path of the image file."},
            "caption": {**_S, "description": "What to look at."}}}),
}
PREFIX = "mcp__carrier__"
PROPOSE_RULE = PREFIX + "propose_rule"
ALLOWED = [PREFIX + name for name in TOOLS]


def carrier_tool(name: str, fn: Callable[[dict[str, Any]], Any]) -> Any:
    """One of :data:`TOOLS`; ``fn(args)`` (sync or async) returns the text Claude gets back."""
    from claude_agent_sdk import tool

    description, schema = TOOLS[name]

    @tool(name, description, schema)
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = fn(args)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:  # report to Claude instead of failing the turn
            return {"content": [{"type": "text", "text": f"Error: {exc}"}], "is_error": True}
        return {"content": [{"type": "text", "text": str(result)}]}

    return handler


def carrier_tools(handlers: dict[str, Callable[[dict[str, Any]], Any]]) -> Any:
    """Carrier's own tools for a session, as an in-process MCP server (no network)."""
    from claude_agent_sdk import create_sdk_mcp_server

    return create_sdk_mcp_server(name="carrier", version="1.0.0",
                                 tools=[carrier_tool(n, fn) for n, fn in handlers.items()])


def propose_rule_tool(on_propose: Callable[[dict[str, Any]], Any]) -> Any:
    def run(args: dict[str, Any]) -> str:
        on_propose(args)
        return "Shown to the user as a rule card to edit and approve. Carry on; you'll hear if they change it."

    return carrier_tool("propose_rule", run)


def user_message(text: str, images: list[dict[str, Any]]) -> Any:
    """A user message with pasted images (and their notes), as Claude Code's streaming input."""
    content: list[dict[str, Any]] = []
    for i, img in enumerate(images, 1):
        content.append({"type": "image", "source": {"type": "base64", "media_type": img["mime"],
                                                     "data": img["data"]}})
        if img.get("note"):
            content.append({"type": "text", "text": f"(Image {i} — the user's note: {img['note']})"})
    content.append({"type": "text", "text": text})

    async def stream() -> Any:
        yield {"type": "user", "message": {"role": "user", "content": content},
               "parent_tool_use_id": None, "session_id": "default"}

    return stream()


def safe_git_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for Claude Code's commands: git only uses a bare repository (a folder
    with HEAD, config, objects and refs) when told to with --git-dir. Otherwise Claude
    could make such a folder with ordinary edits, and its config could make a plain
    ``git status`` there run a command. Added to any GIT_CONFIG_* settings already there."""
    env = dict(os.environ if base is None else base)
    try:
        n = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    except ValueError:
        n = 0
    return {"GIT_CONFIG_COUNT": str(n + 1), f"GIT_CONFIG_KEY_{n}": "safe.bareRepository",
            f"GIT_CONFIG_VALUE_{n}": "explicit"}


MAX_MESSAGE_BYTES = 64 * 1024 * 1024

# Carrier's own permission mode: nothing asks, except what the folder rules say must (Claude Code
# itself runs in "default" so that every tool call still comes to Carrier to be checked).
LIVE = "liveDangerously"
LIVE_PHRASE = "I accept the blast radius"
# "Set the World on Fire" is Claude Code's bypassPermissions mode: nothing asks, nothing is checked.
FIRE = "bypassPermissions"
FIRE_PHRASE = "Let it all burn"


def claude_mode(mode: str) -> str:
    """The permission mode Claude Code is given for a Carrier mode."""
    return "default" if mode == LIVE else mode


# Tools that "Danger Zone" runs without asking, besides the file tools and Bash checked
# below. Other tools (another MCP server's, PowerShell …) still ask: their paths, if any,
# can't be checked.
LIVE_TOOLS = {"TodoWrite", "Task", "Agent", "BashOutput", "KillShell", "KillBash", "ExitPlanMode",
              "WebFetch", "WebSearch", "Skill", "SlashCommand", "ListMcpResourcesTool", "ReadMcpResourceTool"}


def live_check(name: str, inp: dict[str, Any], cwd: Path, readable: list[str], editable: list[str],
               no_write: Callable[[str], bool] | None = None, blocked: str | None = None) -> str:
    """In "Danger Zone" mode: "" if the tool call may run without asking, else why it asks.

    Only the folder rules hold: reading in the project's folders, editing in its own folders
    (not .git, .claude and the like, not other projects' files), shell commands whose files and
    folders are those — any program, but programs that run other commands still ask."""
    from carrier.flightdeck import shellcheck

    def where(p: str | None) -> str:
        target = Path(os.path.expanduser(p)) if p else cwd
        return os.path.realpath(target if target.is_absolute() else cwd / target)

    def fixed(pattern: str) -> str:
        """The part of a file pattern before its first wildcard (the folder it searches)."""
        head = re.split(r"[*?\[{]", pattern, maxsplit=1)[0]
        return head if head.endswith(("/", "\\")) or not head else os.path.dirname(head) or "."

    def inside(path: str, roots: list[str]) -> bool:
        t = os.path.normcase(path)
        return any(t == os.path.normcase(os.path.realpath(r)) or
                   t.startswith(os.path.normcase(os.path.realpath(r)).rstrip(os.sep) + os.sep) for r in roots)

    if blocked and not inside(where(blocked), readable + editable):
        return f"Claude Code flagged {blocked}, outside the project's folders"
    if name in READ_TOOLS:
        base = where(_tool_path(name, inp))
        if not inside(base, readable + editable):
            return "it reads outside the project's folders"
        for key in ("pattern", "glob") if name in ("Glob", "Grep") else ():
            pat = str(inp.get(key) or "")
            if name == "Grep" and key == "pattern":
                continue  # Grep's pattern is what it searches for, not a file
            if pat and (".." in pat.replace("\\", "/").split("/") or re.search(r"\{[^}]*([/\\~:]|\.\.)", pat)
                        or not inside(
                    os.path.realpath(os.path.join(base, os.path.expanduser(fixed(pat)))), readable + editable)):
                return "it searches outside the project's folders"
        return ""
    if name in EDIT_TOOLS:
        target = where(_tool_path(name, inp))
        if not inside(target, editable):
            return "it changes a file outside the project's own folders"
        if shellcheck.protected(target, editable):
            return "it changes a file Claude Code or git runs things from"
        if no_write and no_write(target):
            return "it changes another project's file"
        return ""
    if name == "Bash":
        v = shellcheck.check(str(inp.get("command", "")), [], cwd, readable + editable, editable, can_edit=True,
                             can_read=True, no_write=no_write, any_program=True)
        if v.ok:
            return ""
        whys = [p["why"] for p in v.programs if not p["listed"] and p.get("why")] + ([v.reason] if v.reason else [])
        parts = [p for w in whys if w for p in w.split("; ")]
        return "; ".join(dict.fromkeys(parts)) or "it can't be checked against the folder rules"
    if name in LIVE_TOOLS or name.startswith("mcp__carrier__"):
        return ""  # no files of their own (web tools reach the internet, which the warning says)
    return "Carrier can't check which files this tool uses"


def build_options(*, cwd: Path, model: str | None, mode: str, resume: str | None,
                  hooks: dict[str, list[Any]], can_use_tool: Callable[..., Any], context: str = "",
                  add_dirs: list[str] | None = None, effort: str | None = None,
                  carrier_server: Any = None) -> Any:
    """``context`` is the Ready Room text (goal, rules, task) for this session;
    ``add_dirs`` are the project's extra folders, which Claude Code may also use;
    ``carrier_server`` is :func:`carrier_tools` (its tool needs no permission)."""
    from claude_agent_sdk import ClaudeAgentOptions

    extra: dict[str, Any] = {}
    if carrier_server is not None:
        extra = {"mcp_servers": {"carrier": carrier_server}, "allowed_tools": list(ALLOWED)}
    return ClaudeAgentOptions(
        **extra,
        # A single message from Claude Code can be large (a big file read, a long tool result, an image):
        # the default 1 MB limit stops the connection dead.
        max_buffer_size=MAX_MESSAGE_BYTES,
        cwd=str(cwd),
        add_dirs=list(add_dirs or []),
        model=model or None,
        # The user's preferred effort, mapped to the nearest level this model
        # supports (none if it has no effort setting) — see core.models.
        effort=effective_effort(model or None, effort),
        permission_mode=claude_mode(mode),  # "default" | "acceptEdits" | "plan" | "bypassPermissions"
        resume=resume,
        # Behave like normal Claude Code: its system prompt, plus a short note.
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": SYSTEM_APPEND + ("\n\n" + context if context else "")},
        # Load the user's settings and CLAUDE.md files, as the CLI would.
        setting_sources=["user", "project", "local"],
        include_partial_messages=True,  # streaming preview in the UI
        # Each Bash command starts in the session's folder, so a "cd" can't carry over unchecked.
        env={**safe_git_env(), "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR": "1"},
        hooks=hooks,
        can_use_tool=can_use_tool,
    )


def make_client(options: Any) -> Any:
    from claude_agent_sdk import ClaudeSDKClient

    return ClaudeSDKClient(options)


def hook_matcher(matcher: str | None, fn: Callable[..., Any]) -> Any:
    from claude_agent_sdk import HookMatcher

    return HookMatcher(matcher=matcher, hooks=[fn])


def allow(updated_input: dict[str, Any] | None = None) -> Any:
    from claude_agent_sdk import PermissionResultAllow

    return PermissionResultAllow(updated_input=updated_input)


def deny(message: str, interrupt: bool = False) -> Any:
    from claude_agent_sdk import PermissionResultDeny

    return PermissionResultDeny(message=message, interrupt=interrupt)


# -- hook outputs --------------------------------------------------------------


def pre_tool_output(context: str | None = None, decision: str | None = None,
                    reason: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"hookEventName": "PreToolUse"}
    if context:
        out["additionalContext"] = context
    if decision:
        out["permissionDecision"] = decision
        out["permissionDecisionReason"] = reason or ""
    return {"hookSpecificOutput": out} if len(out) > 1 else {}


def post_tool_output(context: str | None) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}} if context else {}


def prompt_output(context: str | None) -> dict[str, Any]:
    return ({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}
            if context else {})


# -- messages → events -----------------------------------------------------------


def allow_key(name: str, inp: dict[str, Any]) -> tuple[str, str]:
    """What "allow for this session" covers, and a label for the button.

    Shell commands are scoped to their program (``git``, ``npm`` …) and web
    fetches to their site, so allowing one ``git status`` doesn't allow
    every command. Other tools (edits, reads) are allowed as a whole, like
    Claude Code's own "auto-accept edits".
    """
    if name == "Bash":
        cmd = str(inp.get("command", "")).strip().split()
        prog = cmd[0] if cmd else ""
        return f"Bash:{prog}", f"Allow `{prog}` commands for this session" if prog else "Allow this command"
    if name == "WebFetch":
        from urllib.parse import urlparse

        host = urlparse(str(inp.get("url", ""))).netloc
        return f"WebFetch:{host}", f"Allow fetching from {host} for this session"
    return name, f"Allow {name} for this session"


# -- pre-approved actions (a project's "Pre-approved actions", readyroom/manager.py) --------

READ_TOOLS = ("Read", "Glob", "Grep", "LS", "NotebookRead")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
ALLOW_DEFAULT: dict[str, Any] = {"read": False, "edit": False, "web_search": False, "web_fetch": False,
                                 "sites": [], "commands": [], "tools": [], "groups": [], "export": True}

# Ready-made groups of programs (entries as in flightdeck/shellcheck.py), one checkbox each.
GROUPS: dict[str, dict[str, Any]] = {
    "java": {"label": "Java (JDK)", "entries": ["java", "javac", "javap", "jar", "jdeps"],
             "note": "Runs Java code, including code Claude wrote."},
    "gradle": {"label": "Gradle", "entries": ["gradle", "./gradlew", "./gradlew.bat"],
               "note": "Runs the build, including build scripts Claude can edit."},
    "maven": {"label": "Maven", "entries": ["mvn", "./mvnw", "./mvnw.cmd"],
              "note": "Runs the build, including build files Claude can edit."},
    "node": {"label": "Node.js", "entries": ["node", "npm", "npx", "pnpm", "yarn"],
             "note": "Runs JavaScript and package scripts, which Claude can edit; npx can download packages."},
    "python": {"label": "Python", "entries": ["python", "python3", "py", "pip", "pytest"],
               "note": "Runs Python code, including code Claude wrote; pip installs packages."},
    "dotnet": {"label": ".NET", "entries": ["dotnet"], "note": "Builds and runs .NET code."},
    "rust": {"label": "Rust", "entries": ["cargo", "rustc"], "note": "Builds and runs Rust code."},
    "go": {"label": "Go", "entries": ["go"], "note": "Builds and runs Go code."},
    "git_read": {"label": "Git (looking only)",
                 "entries": ["git status", "git diff", "git log", "git show", "git branch", "git rev-parse",
                             "git ls-files", "git blame"],
                 "note": "Commands that only show the repository; commits, pushes and options that change "
                         "it still ask. Carrier only (not shared with plain Claude Code)."},
    "helpers": {"label": "Read-only helpers",
                "entries": ["grep", "head", "tail", "wc", "sort", "uniq", "cut", "tr", "cat", "ls", "which",
                            "diff", "file", "tree"],
                "note": "In Carrier, only inside the project's folders: a file elsewhere still asks. "
                        "Not shared with plain Claude Code, which couldn't keep them to the project."},
    "text": {"label": "Text processing (awk, sed)", "entries": ["awk", "sed"],
             "note": "awk can run other programs from its script. sed is allowed for printing, deleting and "
                     "substituting lines; sed -i, sed -e and other sed scripts still ask. Carrier only."},
}


def group_entries(allow: dict[str, Any]) -> list[str]:
    """All command entries: the listed ones plus the ticked groups'."""
    out = list(allow.get("commands") or [])
    for g in allow.get("groups") or []:
        for e in GROUPS.get(g, {}).get("entries", []):
            if e not in out:
                out.append(e)
    return out


def _tool_path(name: str, inp: dict[str, Any]) -> str | None:
    for k in ("file_path", "notebook_path", "path"):
        if inp.get(k):
            return str(inp[k])
    return None


def _host_matches(host: str, sites: list[str]) -> bool:
    host = host.lower().split(":")[0]
    return any(host == s or host.endswith("." + s) for s in (x.lower().strip().lstrip(".") for x in sites) if s)


def preapproved(name: str, inp: dict[str, Any], allow: dict[str, Any], cwd: Path,
                readable: list[str], editable: list[str], no_write: Callable[[str], bool] | None = None) -> str:
    """Which of the project's pre-approvals covers this tool call ("" if none).

    Files count only inside the project's folders: ``editable`` (the session's folder
    and work folders) for edits, those plus reference and workspace folders for reads.
    Shell commands: every program in them must be pre-approved (flightdeck/shellcheck.py).
    """
    from carrier.flightdeck import shellcheck

    def inside(roots: list[str]) -> bool:
        p = _tool_path(name, inp)
        target = Path(p) if p else cwd  # no path: the tool works in the session's folder
        if not target.is_absolute():
            target = cwd / target
        t = os.path.normcase(os.path.realpath(target))
        for r in roots:
            root = os.path.normcase(os.path.realpath(r))
            if t == root or t.startswith(root.rstrip(os.sep) + os.sep):
                return True
        return False

    if name in READ_TOOLS and allow.get("read") and inside(readable):
        return "read"
    if name in EDIT_TOOLS and allow.get("edit") and inside(editable):
        p = _tool_path(name, inp)
        target = Path(p) if p else cwd
        if not shellcheck.protected(str(target if target.is_absolute() else cwd / target), editable):
            return "edit"  # (.git, .claude, .carrier and .mcp.json always ask: they can run commands)
    if name == "WebSearch" and allow.get("web_search"):
        return "web_search"
    if name == "WebFetch" and allow.get("web_fetch"):
        from urllib.parse import urlparse

        sites = allow.get("sites") or []
        if not sites or _host_matches(urlparse(str(inp.get("url", ""))).netloc, sites):
            return "web_fetch"
    if name == "Bash":
        entries = group_entries(allow)
        if entries and shellcheck.check(str(inp.get("command", "")), entries, cwd, readable, editable,
                                        can_edit=bool(allow.get("edit")), can_read=bool(allow.get("read")),
                                        no_write=no_write).ok:
            return "commands"
    if name in (allow.get("tools") or []):
        return "tools"
    return ""


def project_allow(name: str, inp: dict[str, Any], allow: dict[str, Any] | None = None, cwd: Path | None = None,
                  readable: list[str] | None = None, editable: list[str] | None = None,
                  no_write: Callable[[str], bool] | None = None) -> dict[str, Any] | None:
    """What "always allow in this project" would add for this tool call.

    ``{"change": {...}, "label": "...", "programs": [...], "reason": "..."}``; for a shell
    command, ``programs`` says which programs are pre-approved already, and ``change`` is
    None when adding programs wouldn't be enough (``reason`` says why it asks).
    """
    from carrier.flightdeck import shellcheck

    if name in READ_TOOLS:
        return {"change": {"read": True}, "label": "Always allow reading files in this project"}
    if name in EDIT_TOOLS:
        return {"change": {"edit": True}, "label": "Always allow editing files in this project"}
    if name == "WebSearch":
        return {"change": {"web_search": True}, "label": "Always allow web searches in this project"}
    if name == "WebFetch":
        from urllib.parse import urlparse

        host = urlparse(str(inp.get("url", ""))).netloc.split(":")[0]
        return {"change": {"web_fetch": True, "sites": [host]},
                "label": f"Always allow fetching from {host} in this project"} if host else None
    if name == "Bash":
        allow = allow or dict(ALLOW_DEFAULT)
        v = shellcheck.check(str(inp.get("command", "")), group_entries(allow), cwd or Path.cwd(),
                             readable or [], editable or [], can_edit=bool(allow.get("edit")),
                             can_read=bool(allow.get("read")), no_write=no_write)
        out = {"programs": v.programs, "reason": v.reason, "change": None, "label": ""}
        missing = v.missing
        blockers = [p for p in v.programs if not p["listed"] and not p.get("entry")]
        unreadable = v.reason and not v.programs and not missing
        if missing and not blockers and not unreadable and _only_programs_missing(v):
            out["change"] = {"commands": missing}
            names = ", ".join(f"`{m}`" for m in missing)
            out["label"] = f"Always allow {names} in this project"
        return out
    if name in ("AskUserQuestion",) or name.startswith("mcp__carrier__"):
        return None
    return {"change": {"tools": [name]}, "label": f"Always allow {name} in this project"}


def _only_programs_missing(v: Any) -> bool:
    """Every reason the command asks is a program that isn't listed yet (nothing else in the way)."""
    unlisted = [p for p in v.programs if not p["listed"]]
    whys = [p["why"] or f"{p['name']} isn't pre-approved" for p in unlisted]
    return v.reason == "; ".join(dict.fromkeys(whys))


def summarize_tool(name: str, inp: dict[str, Any], cwd: Path | None = None) -> str:
    """One-line description of a tool call for the UI ("Edit src/app.py")."""
    def rel(p: str) -> str:
        if cwd:
            try:
                return Path(p).resolve().relative_to(cwd.resolve()).as_posix()
            except (ValueError, OSError):
                pass
        return p

    if name in ("Read", "Edit", "Write", "MultiEdit"):
        return f"{name} {rel(str(inp.get('file_path', '')))}"
    if name == "NotebookEdit":
        return f"{name} {rel(str(inp.get('notebook_path', '')))}"
    if name == "Bash":
        return f"Bash: {str(inp.get('command', ''))[:200]}"
    if name in ("Grep", "Glob"):
        return f"{name} {inp.get('pattern', '')}" + (f" in {inp['path']}" if inp.get("path") else "")
    if name in ("WebFetch", "WebSearch"):
        return f"{name} {inp.get('url') or inp.get('query') or ''}"
    if name == "Task":
        return f"Task: {inp.get('description', '')}"
    if name == "TodoWrite":
        return "Update to-do list"
    text = json.dumps(inp, ensure_ascii=False)
    return f"{name} {text[:160]}"


def _result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                parts.append(c.get("text") or (f"[{c.get('type')}]" if c.get("type") else json.dumps(c)))
            else:
                parts.append(str(c))
        text = "\n".join(parts)
    else:
        text = str(content)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + "\n… (truncated)"


def _trim_input(inp: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in inp.items():
        if isinstance(v, str) and len(v) > 2000:
            v = v[:2000] + "… (truncated)"
        out[k] = v
    return out


def normalize(msg: Any, cwd: Path | None = None) -> list[dict[str, Any]]:
    """Convert one SDK message into zero or more events."""
    from claude_agent_sdk import (AssistantMessage, ResultMessage, StreamEvent, SystemMessage, TextBlock,
                                  ThinkingBlock, ToolResultBlock, ToolUseBlock, UserMessage)

    events: list[dict[str, Any]] = []
    if isinstance(msg, StreamEvent):
        ev = msg.event or {}
        if ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
            events.append({"type": "live", "text": ev["delta"].get("text", "")})
        return events
    if isinstance(msg, AssistantMessage):
        if msg.parent_tool_use_id:  # inside a sub-agent: show only tool use, compactly
            for b in msg.content:
                if isinstance(b, ToolUseBlock):
                    events.append({"type": "tool_use", "id": b.id, "name": b.name, "sub": True,
                                   "summary": summarize_tool(b.name, b.input or {}, cwd),
                                   "input": _trim_input(b.input or {})})
            return events
        for b in msg.content:
            if isinstance(b, TextBlock) and b.text.strip():
                events.append({"type": "text", "text": b.text})
            elif isinstance(b, ThinkingBlock) and b.thinking.strip():
                events.append({"type": "thinking", "text": b.thinking})
            elif isinstance(b, ToolUseBlock):
                events.append({"type": "tool_use", "id": b.id, "name": b.name,
                               "summary": summarize_tool(b.name, b.input or {}, cwd),
                               "input": _trim_input(b.input or {})})
        if msg.error:
            code = str(msg.error)
            text = "Claude's usage limit was reached." if code == "rate_limit" else code
            events.append({"type": "error", "code": code, "text": text})
        return events
    if isinstance(msg, UserMessage):
        content = msg.content if isinstance(msg.content, list) else []
        for b in content:
            if isinstance(b, ToolResultBlock):
                events.append({"type": "tool_result", "id": b.tool_use_id,
                               "text": _result_text(b.content), "is_error": bool(b.is_error),
                               **({"sub": True} if getattr(msg, "parent_tool_use_id", None) else {})})
                # Images Claude looked at (e.g. it read a screenshot) are shown too.
                for c in b.content if isinstance(b.content, list) else []:
                    if not isinstance(c, dict):
                        continue
                    src = c.get("source") or {}
                    if c.get("type") == "image" and src.get("type") == "base64" \
                            and src.get("media_type") in IMAGE_TYPES and len(src.get("data", "")) < 7_000_000:
                        events.append({"type": "image", "from": "tool", "mime": src["media_type"],
                                       "data": src["data"], "name": "", "caption": ""})
        return events
    if isinstance(msg, ResultMessage):
        events.append({"type": "result", "cost": msg.total_cost_usd or 0.0, "turns": msg.num_turns,
                       "is_error": bool(msg.is_error), "text": (msg.result or "") if msg.is_error else "",
                       "session": msg.session_id, "api_status": getattr(msg, "api_error_status", None)})
        return events
    if type(msg).__name__ == "RateLimitEvent":  # a usage-limit window changed state (subscriptions)
        info = msg.rate_limit_info
        events.append({"type": "rate_limit", "kind": info.rate_limit_type or "five_hour", "status": info.status,
                       "resets_at": info.resets_at, "utilization": info.utilization,
                       "overage_status": info.overage_status})
        return events
    if type(msg).__name__ == "ConversationResetMessage":  # e.g. after /clear: cost counter restarts
        events.append({"type": "reset", "text": "Conversation cleared."})
        return events
    if isinstance(msg, SystemMessage) and msg.subtype == "init":
        data = msg.data or {}
        events.append({"type": "system", "session": data.get("session_id"), "model": data.get("model"),
                       "key_source": data.get("apiKeySource"),
                       "text": f"Claude Code session started ({data.get('model', 'default model')})."})
    return events
