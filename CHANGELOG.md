# Changelog

All notable changes are listed here. Format: [Keep a Changelog](https://keepachangelog.com/).

## Unreleased

- **Health report like a review.** Findings on the left by theme, the code
  each one is about on the right with the lines marked (and more context on
  request), keys to move and decide. Fixed the report's colours, which filled
  whole findings.
- **Health checks in the Ready Room**, like review agents: settings for every
  project or one project, **All health checks** to set the model, effort,
  severity and file budget of every check at once, and your own checks (with
  ✨ Draft with Claude), for every project or one.

- **Health checks in Debrief.** Twelve agents that look at the whole project
  now and then rather than at one set of changes: security, rules compliance,
  error handling, tests, cross-platform, performance, structure, duplication,
  magic numbers, dead code, docs drift, and dependencies. Checks never run by
  themselves; they show as due (security and rules compliance on a timer,
  rules compliance also when the rules change; the rest once the code has
  changed), with a token estimate, per-project settings (model, effort,
  interval, file budget) and a report by theme that marks findings new, still
  open or not found again. Each finding can become a task, or be accepted or
  dismissed.

- **Folder ownership.** A project's main folder is now its own alone: other
  projects can use it only as a reference folder (adding it as a work folder
  offers a reference folder instead, and older setups are converted when
  Carrier starts). A work folder that more than one project works in is a
  **shared space**: each project owns its part (patterns set with **Part…**
  on the folder's card) and the files it creates there. Editing anything else
  in a shared space always asks, even when editing is pre-approved; another
  project's main folder inside a work folder is read only. Claude is told
  which part is its own.
- **Reviews follow ownership.** Debrief leaves out other projects' main
  folders and, in shared spaces, files outside the project's part that its
  sessions didn't change; changes made by another project's sessions are
  marked as that project's work. Each work folder can be left out of reviews
  and can carry a note for the review agents. (This replaces the earlier
  "review only this project's changes" setting, which becomes a shared space.)
- **Import:** a planned work folder inside another project's main folder is
  added as a reference folder.
- **Pre-approved commands that change files are checked too.** `cp`, `mv`,
  `rm`, `tee`, `touch`, `mkdir`, `dd`, `git checkout`/`restore`/`reset` and
  the like run without asking only when every file they change is one the
  session may edit (inside the project's work folders, not protected, not
  another project's); otherwise they ask, even if the program is pre-approved.

- **Claude knows what runs without asking.** Each session's instructions now
  include a *Permissions in this project* section generated from the
  project's pre-approved actions, asking Claude to use the file tools, keep
  commands simple with literal paths, and only ask for more when the task
  needs it or it saves many steps. Changes reach running sessions with the
  next message.

- **⏸ Pause a session** when you switch focus. Claude Code disconnects, the
  task stays with the session, and other sessions stop treating it as active.
  **Resume** carries on the same conversation, and Claude is told which files
  changed in its folders while it slept (who changed them, where Carrier
  knows) and to re-read them. Closing Carrier pauses sessions the same way.
- **One session per task:** while a task has a session, its button opens that
  session instead of starting another.
- **End session leaves a note:** the End dialog asks for a note for the next
  session on the task — yours, or written by Claude — which the next session
  gets with its task. Editable in the Ready Room. The dialog also says plainly
  that ending is for good.
- Fixed: a session's header could stay on "working" after Claude finished,
  when its status changed just as the page loaded it (also the cause of
  some flaky page tests).
- **Watch reference folders** (project switch): with each message Claude
  hears what changed in the project's reference and workspace folders, and
  the page shows a notice when another session edits one. (Planned: choose
  this per folder.)

- **Pre-approved programs understand real commands.** A shell command runs
  without asking when every program in it is pre-approved — pipes, `&&`,
  variables (`"$JP" -c Foo | awk …`), full paths (`…/javap.exe` is `javap`)
  and harmless redirects included; `$( )`, here-documents, loops and files
  outside the project still ask. Program groups with one tick each: Java,
  Gradle, Maven, Node.js, Python, .NET, Rust, Go, Git (looking only),
  read-only helpers, and text processing (awk, sed), with suggestions from
  the project's folder. Entries can be scripts (`./gradlew`, `bash build.sh`)
  or a program with its first arguments (`git status`).
- **Safety fix:** programs that run other commands (`bash`, `sh`, `cmd`,
  `powershell`, `env`, `xargs`, `sudo` …) can no longer be pre-approved on
  their own — in 0.8.0 pre-approving `bash` let `bash -c "…"` run anything.
  Such entries saved by 0.8.0 are dropped. Carrier fails closed: settings
  that change how programs behave (`PATH=`, `GIT_…`, `LD_PRELOAD`,
  `VAR=x program`), `cd` out of the project, program files in unusual
  places, and options that run code or touch other files (`git -c`,
  `git diff --output`, `find -exec`, `sort -o`, `sed -i` …) still ask.
  Edits to `.git`, `.claude`, `.carrier`, `.mcp.json`, `.gitmodules`,
  `.vscode`, `.idea`, `.husky`, `.devcontainer` and `.envrc` always ask: they
  can make git, Claude Code or an editor run commands. Flight Deck sessions
  also tell git to ignore stray bare repositories (`safe.bareRepository`),
  so a folder Claude makes can't turn `git status` into a command. Checked by
  three rounds of independent review.
- Permission cards for shell commands show which programs are pre-approved,
  add exactly the missing ones ("for this session" now works per program
  too), and say why a command asks when adding programs wouldn't help.
- **Shared with Claude Code:** a project's pre-approvals are also written to
  its `.claude/settings.local.json` (Carrier's own rules only; git ignores
  the file via `.git/info/exclude`), so plain Claude Code doesn't ask
  either. Can be turned off per project.

- **Draft a new project with Claude**: on ＋ New project, describe it in your
  own words and Claude fills in the name, goal, project rules, a starting
  task with details, and tags (with a glimpse of the chosen folder: its name,
  top-level names and the start of its README). The form also gains
  optional starting-task details and project rules.

- **Pre-approved actions** per project (Ready Room): let its sessions read
  files, create and edit files, search the web, fetch pages (any site or
  listed sites), run listed programs (simple commands only) and use named
  tools without asking. Files outside the project's folders still ask. A
  Flight Deck permission card can add to the list with **Always allow … in
  this project**; templates copy it.

## 0.8.0 — Usage and limits (alpha)

- **Usage and limits.** A Usage button on every tab shows how full Claude's
  5-hour and weekly limits are (as Claude Code reports them) and when they
  reset. The Usage panel adds tokens for today and the last 7 days, Flight
  Deck against other Claude Code use, a 14-day chart, the top projects, tasks
  and models, and the cost at API prices. Token counts come from Claude
  Code's own logs, read only, numbers only, kept 60 days; can be turned off.
- **Usage-limit stops are waited out.** When a limit stops a task, the session
  waits instead of failing and Carrier tells Claude to carry on after the
  reset — also after a restart. **Continue now** carries on at once.
- **Holding near a limit:** a warning at a level you set (80%), and at a
  higher level (90%) auto take-off waits and starting a routine asks first.
  While a limit is reached nothing starts on its own, and starting work by
  hand asks first.

## 0.7.0 — Import, groups, routines and safety

- **Alerts** when Claude needs you: a pulsing badge on the Flight Deck tab, a
  banner on the other tabs with "Go there", pulsing highlights on the session
  and its waiting card, a taskbar flash (Windows) and an optional chime, with
  do-not-disturb. A dot on the tab when a routine is ready for take-off.
- **Secrets protection**: a fast local scan (no AI, no network) holds gated
  routines while likely secrets are in the project's files, with "Mark safe"
  per line; a warning before sending a message that looks like it contains a
  key; a check for key and `.env` files git would publish, with a one-click
  `.gitignore` fix; and a "Secrets" review agent (off by default).
- **Bring own copies back**: merge a session's own copy (worktree) into the
  main folder (conflicts leave nothing changed), review it first, keep only
  the branch, or discard it; the Ready Room lists copies not brought back.
- **Starting history**: importing a folder with no git history offers a first
  commit with suggested `.gitignore` lines; any project can "Start review
  history from now".
- **Question fallback**: questions Claude Code asks without routing them to
  Carrier appear in the chat to answer as a message; Carrier's own `ask_user`
  tool works regardless.
- The review gate is cached (it no longer runs git on every refresh).

- **Automatic backup before upgrades**: when a new version needs to change
  `carrier.db`, a copy is saved first in `backups/` beside it (newest five
  kept), and the app says where.
- **Routines have their own permission mode** (ask, auto-accept edits or
  plan only); routines and Auto take-off never run with "Never ask".
- **Images can be turned off per project** (Ready Room → the project), with a
  note that pictures are sent to Anthropic as part of the conversation; when
  off, Claude can't open image files either.
- **Activity log per project**: every task change plus project events —
  goal, rules, rule sets, folders, groups, routines, skills, images,
  finished reviews and scans — newest first.
- **Editing a task while a session works on it** pauses Claude and tells it
  exactly what changed.
- **Templates copy more**: "Save as template" now includes the project's task
  groups, routines, skills and review agents.

- **New work during a task**: Claude asks where it belongs — the current
  task, or a new task in the main queue, a task group or a new group, and at
  which position — with a suggestion based on the project's queues and
  routines (`list_tasks`, `propose_work`).
- **Reshaping tasks**: with your approval Claude can pull another task (or a
  part of it) into the current one, or split part of the current task out
  (`propose_task_change`).
- **Task history**: every change to a task is logged (who, when, why, and
  any text removed); shown per task, and given to Debrief's agents so reviews
  judge work against the task's final scope and flag silent descoping.

- **Task groups**: extra queues inside a project (with priority — including
  Backlog — and tags) that run alongside the main queue; each has its own
  current task and ▶ Start in Flight Deck, and parallel sessions stay aware
  of each other.
- **Routines tied to task groups**, with a new trigger: when the group is
  finished.
- **Ready for take-off** in Flight Deck lists queued routines and their
  review gates; **Auto take-off** starts them automatically once clear.

- **✨ Draft with Claude for tasks and routines**: a task comes back as a title
  and details; a routine as a name, instructions, trigger and review gate.
  New tasks can now have details when added.

- **Whole-project scans** in Debrief: check every file as it is now, with the
  agents you pick (e.g. security), without needing any history. Scans don't
  move the review checkpoint.
- **Routines** (repeatable tasks) per project: run on demand or after each
  finished task, optionally gated on a clear review (no open review, no
  unreviewed changes, no open fix tasks) — e.g. update docs, push to GitHub.
- Fix: folders inside a bigger git repository (such as a workspace's new
  folders) showed as untracked in Debrief, and "Start tracking" would have
  made a nested repository.

- **Workspace folders**: a new extra-folder type for a shared parent folder.
  Its contents are read only, but a new folder the project creates there
  (by writing a file into it, or with Carrier's `create_work_folder` tool)
  is added to the project as a work folder.
- **Skills**: a Skills page lists, edits and creates a project's Claude Code
  skills (`.claude/skills`) and your personal ones, with ✨ Draft with
  Claude; in Flight Deck, Claude can suggest a skill for you to edit and save.
- **Images in Flight Deck**: paste, drop or add pictures, mark them up (pen,
  box, arrow) with a note, and send them; Claude can show you images, and
  images it opens appear in the chat.
- **Other projects**: Carrier's `list_projects` / `get_project` tools let
  Claude read another project's goal, rules, rule sets, folders, tasks and
  skills — e.g. to adapt a finished project's release rules — and suggest
  them for approval.

- **Questions as option cards**: in Flight Deck, Claude Code's questions
  (AskUserQuestion) appear as clickable options with descriptions, a typed
  answer for each, and Skip; the import conversation asks the same way.
- **Suggested rules**: when you state a lasting preference, Claude proposes a
  rule (a Carrier tool in each session, in-process, no network). Edit it,
  choose project rules, global rules or a rule set (new or existing), and
  add it — nothing is saved until you do. During an import, rules go into
  the plan.
- Chat text now shows markdown **tables** and numbered lists.
- **Layout settings for wide screens**: limit the work area to Normal, Wide
  (about a 16:9 screen) or Wider, or use the full window, and optionally
  centre it. Applies to Ready Room pages and Flight Deck conversations.

- **Import existing work** (Ready Room): point Carrier at a folder used with
  plain Claude Code or the Claude app. It reads the layout, READMEs,
  CLAUDE.md, `.claude` permissions, git history, past Claude Code sessions
  and (optionally) a claude.ai chat export, then Claude talks it through with
  you and drafts a plan — one project or several, in place or with folder
  moves you approve. Edit the plan on the page, then create everything in one
  step (undone on failure). Unfinished imports are saved.
- **Projects inside a bigger repository:** a main or work folder can now be a
  subfolder of a git repository. Debrief reviews only that folder (paths
  relative to it), each project keeps its own checkpoints and open review,
  and Flight Deck's own copies run in the project's folder inside the copy.

## 0.6.0 — Projects everywhere (first public release)

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
