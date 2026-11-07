# Changelog

All notable changes are listed here. Format: [Keep a Changelog](https://keepachangelog.com/).

## Unreleased

## 0.6.0 — Projects everywhere (not yet published)

- The app opens on the **Ready Room** until there is a project, then on
  **Flight Deck**.
- **Extra folders** per project: Claude Code runs in the main folder and is
  given access to the others; edits there are tracked as `<label>/<path>`.
- **Debrief reviews projects**, not folders: all of a project's folders in
  one review (each with its own checkpoint), every change labelled with the
  Flight Deck task that made it, the project's goal and rules given to the
  agents, and requested fixes added to the project as its next task
  ("Open in Flight Deck"). A session in its own copy (worktree) is still
  reviewed on its own. Terminal reviews of a project's main folder include
  the same folders, context and agents.
- **Review agents in the Ready Room**: built-ins on/off globally or per
  project, custom agents for every project or one.
- **✨ Draft with Claude** for project rules, global rules, rule sets and
  review agents: describe it, edit the draft, then save.
- Setup checks moved to the Ready Room's start page.
- **Licensed under AGPL-3.0-only** (`LICENSE`), with SPDX headers on every
  source file. New short README; the full guide moved to `docs/GUIDE.md`.
- **Review agents: severity, model and effort** are selectable for every
  agent, built-in or custom — globally, with per-project overrides.
- **Tasks: model and effort** in session presets, in the start dialogs, and
  switchable mid-session (model live; effort reconnects and resumes).
- **Model list from Claude Code**: asked at startup (no cost) and saved; effort
  falls back to the closest lower supported level, greyed when a model has
  none — the user's choice is kept.
- **Folder types**: extra folders are *work* (read/edit, reviewed) or
  *reference* (read only; edits blocked, not reviewed). A main folder is
  exclusive, but other projects may use it as a work or reference folder;
  edits there are logged for every project working in it, and each project
  keeps its own Debrief checkpoint.
- "Flags as" selects are coloured by severity.
- Windows: no more console windows flashing up while Carrier runs git and
  Claude Code; right-click spelling suggestions fixed.
- Debrief's project list refreshes when you open the tab (new projects appear).
- Windows: right-clicking a text box shows spelling suggestions, undo, cut,
  copy, paste and select all (page items such as reload are left out). It is
  set up without ever blocking the window, and switches itself off if an
  attempt doesn't finish (or set `CARRIER_NO_EDIT_MENU=1`).
- The opening tab is chosen before anything else loads.
- A watchdog writes all thread stacks to `desktop.log` if the window stops
  responding for 20 seconds, so freezes can be diagnosed.

## 0.5.0 — Ready Room and tasks (not yet published)

- **Ready Room**: set up projects with a long-term goal, rules, a task queue
  (current task first), history, links with shared-file globs, a default
  session preset and visual-only tags (search and tag filter). Global rules,
  reusable rule sets, session presets and project templates ("Save as
  template"). "What Claude will be told" preview.
- **Flight Deck works on tasks**: ▶ Start a project's current task, ＋ Task
  for others (in parallel), ✓ Mark done with a summary from Claude or you.
  Context (goal, task, rules, recent history) is added to Claude Code's
  system prompt.
- **Standalone tasks** in a chosen or scratch folder; make one a project,
  or add it to a project's history with an optional "Integrate this work"
  task.
- Projects are set up in the Ready Room; Flight Deck no longer adds
  projects or edits links.
- Shared store renamed `carrier.db` (moved to `carrier.core.db`); old
  `flightdeck.db` files are migrated.
- `carrier readyroom` lists projects and current tasks.

## 0.4.0 — Flight Deck (not yet published)

- **Flight Deck**: run Claude Code (via the Agent SDK) in the Carrier window —
  chat view, streaming, tool cards, permission prompts as buttons, Stop,
  permission mode per session, cost, resume after restart.
- **Parallel sessions** across projects or in one project (optional "own
  copy" git worktree per session).
- **Coordination**: projects + links with shared-file (contract) globs;
  automatic edit tracking; warn / ask / block / inform before overlapping
  edits; briefings to Claude about related sessions' work.
- SQLite store (`flightdeck.db`) for projects, links, sessions, edits,
  notices and transcripts.
- "Debrief this": review exactly what a session changed.
- `carrier flightdeck status|add|remove|link|unlink`.

## 0.3.0 — renamed to Carrier (not yet published)

- **Renamed to Carrier**, a suite of three tools: **Ready Room** (planning,
  coming soon), **Flight Deck** (running Claude Code, coming soon) and
  **Debrief** (review — everything from 0.2).
- Code restructured into `carrier.core` (shared) and one package per tool.
- Commands: `carrier` lists the tools; `carrier debrief …` reviews (plain
  `debrief` still works); `carrier app`, `carrier doctor`.
- Desktop app: one window with a tab per tool.
- **Cancel** while a review is being prepared (desktop button, Ctrl+C in
  the terminal); nothing is saved and running agent calls are stopped.
- Renamed paths: `.debrief/` → `.carrier/`, git refs → `refs/carrier/debrief/`,
  launchers `Carrier.bat` / `carrier.sh`, settings folder `Carrier`.

## 0.2.0 — desktop app (not yet published)

- **Desktop app** (pywebview) for Windows, macOS and Linux: choose a folder,
  "Start tracking this folder" for non-git folders, setup checks, resume,
  buttons for every action, finish dialog that can start Claude Code with
  the fix list. Opens no network port.
- **Side-by-side diffs** (old | new) in the desktop app and terminal.
- **Ask about highlighted lines**: drag (or Shift+↑/↓ in the terminal) to
  highlight code; the question is sent with those lines and recorded.
- **Colour themes** incl. red–green, blue–yellow, high-contrast and
  monochrome presets, plus per-element custom colours (desktop); themes
  apply in the terminal too.
- `session.py`: one JSON API for all front ends (the future network seam).
- One-click launchers (`Debrief.bat`, `debrief.sh`), `build-windows.bat`,
  PyInstaller build with secure/network editions and a bundle check.
- Network-isolation test contract; CI build workflow for all three OSes.
- Hardening from an independent review: cancellable sessions, atomic finish,
  selections that never drop highlighted lines, tab-safe terminal diffs,
  page actions limited to files the review produced, reliable first-run setup.

## 0.1.0 — first pass (not yet published)

- Review pipeline: snapshot, diff, Claude Code transcript linking, project
  notes, AI grouping into logical changes, criteria agents, ranking.
- Six built-in criteria: design-drift, scope-creep, tests-weakened,
  risky-surface, security, silent-failure. Project criteria in
  `.carrier/criteria/`.
- Terminal UI with guide explanations (with sources), Q&A, approve / fix /
  revert / skip, project notes, resume.
- Review record and Claude Code-ready fix list; checkpoint advances on finish.
- Claude Code Stop hook for background preparation.
- Safety: checkpoint can't move backwards; snapshots pinned against `git gc`;
  heartbeat prepare lock; robust diff path parsing (quoted paths, ` b/` in
  names, form feeds, CRLF); Windows-safe transcript paths and background spawn.
- `--no-ai` mode; `doctor`, `status`, `list`, `criteria`, `checkpoint`,
  `fixes --run` commands.
