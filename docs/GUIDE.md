# Carrier — the full guide

**Carrier** — *Brief. Launch. Debrief.*

A mothership for Claude Code: launch AI agents in parallel, keep them aware
of each other, and review every change they bring home.

This is the complete guide. For a quick start, see the [README](../README.md).

## Contents

1. [How Carrier fits together](#how-carrier-fits-together)
2. [Getting started](#getting-started)
3. [Ready Room — setting projects up](#ready-room--setting-projects-up)
4. [Project folders](#project-folders)
5. [Flight Deck — launching work](#flight-deck--launching-work)
6. [Debrief — reviewing what came back](#debrief--reviewing-what-came-back)
7. [Review agents](#review-agents)
8. [Models and effort](#models-and-effort)
9. [Draft with Claude](#draft-with-claude)
10. [Terminal and command line](#terminal-and-command-line)
11. [Accessibility](#accessibility)
12. [Privacy and security](#privacy-and-security)
13. [Files Carrier writes](#files-carrier-writes)
14. [Building a standalone app](#building-a-standalone-app)
15. [Troubleshooting](#troubleshooting)
16. [Planned features and known issues](#planned-features-and-known-issues)

---

## How Carrier fits together

Carrier wraps your own Claude Code in three tools, one for each stage of a
mission:

| Tool | Stage | What it does |
|---|---|---|
| **Ready Room** | Brief | Set up each project: what it is (its long-term goal), the rules Claude follows, a queue of tasks, its folders, presets and review agents |
| **Flight Deck** | Launch | Run Claude Code on a project's current task. Several sessions run at once, and sessions in related projects are kept aware of each other |
| **Debrief** | Debrief | Review what changed since the last review, one change at a time, with an AI guide that explains each change and why it was made |

Work flows in a loop: the Ready Room hands **tasks** to Flight Deck, Flight
Deck's sessions produce **changes**, and Debrief reviews them. Anything you
send back for fixing becomes the project's **next task**.

All three share one local database of projects, tasks and edits, so each
tool knows what the others did.

---

## Getting started

### Requirements

* [Git](https://git-scm.com/downloads)
* [Python 3.11+](https://www.python.org/downloads/)
* [Claude Code](https://claude.com/claude-code), installed and signed in
  (run `claude` once)

Carrier uses your Claude Code sign-in, settings and `CLAUDE.md` files — no
API key. Claude Code itself is not bundled.

### Start the app

| Platform | Desktop app | Terminal version |
|---|---|---|
| Windows | Double-click `Carrier.bat` | Drag a project folder onto `Carrier Terminal.bat` |
| macOS, Linux | `./carrier.sh` | `carrier debrief` |

* **First run** creates a private Python environment in `.venv` and installs
  Carrier into it (about a minute). Later starts are immediate.
* **Setup checks** on the Ready Room's start page confirm that Git, Claude
  Code and the Agent SDK are found, with a hint for anything missing.
* **Opening tab:** the Ready Room until you have a project, then Flight Deck.

> **Linux** needs GTK WebKit for the window:
> `sudo apt install python3-gi gir1.2-webkit2-4.1` (Debian/Ubuntu),
> or use Qt: `.venv/bin/pip install "pywebview[qt]"`.

### Your first mission

1. **Ready Room → ＋ New project.** Choose the project's folder, write a
   short long-term goal and a starting task.
2. **Flight Deck → ▶ Start** next to the task. Guide Claude as it works;
   press **✓ Mark done** when it's finished.
3. **Debrief →** pick the project, **Start review**, and go through the
   changes: approve them, or send them back for fixing.

---

## Ready Room — setting projects up

The Ready Room is where each project is set up before work starts. Click
**＋ New project**, choose its main folder, and give it a long-term goal and
a starting task; everything else can be added later.

### What a project holds

| Part | What it is | Sent to Claude? |
|---|---|---|
| Long-term goal | What the project is, in a few sentences | Yes |
| Task queue | The tasks to do. The first one is the **current task**, the one Flight Deck works on next | The current task |
| History | Finished tasks with a short summary of what was done | The last few |
| Rules | Instructions for this project, added to the global rules | Yes |
| Rule sets | Reusable bundles of rules attached to the project | Yes |
| Folders | The main folder plus extra work or reference folders ([Project folders](#project-folders)) | Their names and paths |
| Links | Related projects and the files they share | Used for coordination |
| Default preset | The session settings used when a task starts | No |
| Tags | Labels for finding projects in a long list | No — they're only for you |

### Managing tasks

* Reorder with ↑ and ↓, or **Make current** to move a task to the top. Edit
  or delete any task that isn't in progress.
* Add a task at the end, or tick **make it current** to put it next. A task
  already in progress stays current.
* Finished tasks move to **History** with their summary. **Reopen** puts one
  back in the queue.

### Shared settings

The sidebar holds settings shared by all projects:

* **Global rules** apply to every session in every project, and to
  standalone tasks.
* **Rule sets** are named bundles of rules you can attach to projects,
  presets, templates or the global rules.
* **Session presets** save a model, effort level, permission mode, overlap
  setting and an optional rule set.
* **Project templates** are starting points for new projects. **Save as
  template** on a project makes one from it.
* **Review agents** set up Debrief's reviewers ([Review agents](#review-agents)).

### Finding and checking projects

* Search by name and filter by tag from the sidebar.
* **Linked projects:** link projects that affect each other and name the
  files they share, as patterns. For example, an API server and a web client
  that share a JSON schema: `schema/**/*.json` on the server side,
  `src/api-client/**` on the client side. Flight Deck uses these links to
  keep their sessions aware of each other; a project with no links is never
  interrupted by the others.
* **What Claude will be told…** shows the exact text added to Claude Code's
  instructions for the project: goal, current task, rules and recent history.
  Your `CLAUDE.md` files still load as usual.

---

## Project folders

Every project has one **main folder**, where Claude Code runs, and can have
extra folders of two kinds.

| Folder | Claude can | Reviewed by Debrief | Shared with other projects |
|---|---|---|---|
| Main folder | Read and edit | Yes | No — each main folder belongs to one project |
| Work folder | Read and edit | Yes, with the main folder | Yes |
| Reference folder | Read only; edits are blocked | No | Yes |

* Add extra folders with **＋ work folder** or **＋ reference folder**, and
  switch a folder's type later from its card.
* **Sharing:** a project can use another project's main folder as a work or
  reference folder. An edit there is recorded for both projects, so their
  sessions warn each other.
* **Separate reviews:** each project keeps its own record of where its last
  review ended, so reviewing one project never marks the other's changes as
  reviewed.
* **File names:** files in an extra folder are named `<folder name>/<path>`
  everywhere — in Flight Deck, in shared-file patterns and in reviews. If the
  name clashes with a top-level item in the main folder, Carrier adds a
  number, such as `lib-2`.
* **Git:** a work folder must be the top folder of its own git repository,
  or not in git yet (Debrief offers to start tracking it). Reference folders
  can be anywhere.

---

## Flight Deck — launching work

Flight Deck runs your normal Claude Code inside the Carrier window — the same
tools, `CLAUDE.md` files and settings, with a chat view instead of a
terminal. Work is always a task: a project's current task, or a standalone
task.

![Flight Deck](screenshot-flightdeck.png)

### Starting work

* **▶ Start** next to a project runs its current task. **＋ Task** picks a
  different open task or adds a new one.
* **The start dialog** chooses the task, a session preset, and optionally a
  model and effort for this session only. It can also send the task as
  Claude's first message.
* **Several sessions** can run at once, in different projects or the same
  one. When a folder already has a session, the dialog offers a choice:
  **share the folder** (Carrier warns about overlapping edits) or an **own
  copy** — a git worktree on a new branch, so the two can't clash; you merge
  it later.

### Working with a session

* **Chat:** Claude's replies, its thinking and each tool it uses appear as
  they happen. Type to guide it at any point.
* **Permissions:** prompts appear as buttons — **Allow**, **Allow for this
  session**, or **Deny** with a reason. The header's **Permissions** setting
  can also be *Ask before acting*, *Auto-accept edits*, *Plan only (no
  changes)*, or *Never ask* (which needs confirming).
* **Model and effort** can be changed in the header while Claude is waiting
  for you. A new model applies from the next message; a new effort reconnects
  the session and continues the same conversation.
* **Stop** interrupts Claude mid-reply. **End** closes the session. After a
  restart, open sessions show as paused; **Resume** picks up the same
  conversation.
* **Cost** for the session is shown in the header.

### Finishing a task

**✓ Mark done** saves a short summary to the project's history — written by
Claude or by you — and the next task in the queue becomes current. **Debrief
this session** opens a review of exactly what the session changed.

### Standalone tasks

**＋ Standalone task** runs a one-off task with no project, in a folder you
choose or a scratch folder Carrier makes. Afterwards:

* **Make it a project** turns its folder into a new project.
* **Add to a project** records the task in a project's history and can queue
  an *"Integrate this work"* task saying where the files are. Files are
  never moved.

### Keeping sessions aware of each other

1. **Automatic tracking.** Every file a session edits is recorded — Claude
   doesn't have to report anything.
2. **Before an edit:** if another live session changed the same file, or the
   file is shared with a linked project, the overlap setting decides what
   happens:

   | Overlap setting | What happens |
   |---|---|
   | Warn Claude (default) | Claude is told before the edit and can adjust |
   | Ask me first | You approve or deny the edit |
   | Block the edit | The edit is refused and Claude is told why |
   | Only show it | A notice in Carrier; Claude isn't told |

   The setting is global (bottom of the sidebar); a session preset can
   override it.
3. **When you send a message:** Claude gets a short briefing of what related
   sessions did since your last message, for example *"api-server session #1
   changed the shared contract file schema/user.json — check whether this
   project needs a matching update."* You see exactly what was shared.

---

## Debrief — reviewing what came back

Debrief is a guided review of what changed in a project since its last
review: you make every decision, and an AI guide explains each change. It
reviews the main folder and all work folders together.

![Debrief in the Carrier desktop app](screenshot-desktop.png)

### Starting a review

1. **Pick the project.** Debrief lists its folders, with **Start tracking**
   for any that aren't in git yet.
2. **Choose what to review.** The default is everything since the last
   review. You can also choose only uncommitted changes, or everything since
   a chosen commit (main folder only).
3. Leave **Use AI agents** on for grouping, flags and explanations, or turn
   it off for a plain diff review.
4. **Start review.** **Cancel** stops preparing, and nothing is saved. A
   review in progress can be resumed later.

### How a review is prepared

1. **Snapshot.** Each folder — including uncommitted and new files — is
   captured as a hidden git commit. Your branch, staged files and working
   files are not touched, and the review isn't affected if work continues.
2. **Context.** Debrief reads the project's goal, rules and notes
   (`CLAUDE.md`, `README.md`, `docs/`, `notes/`…) and your Claude Code
   session records, and links each edit to what you asked for at the time.
   Flight Deck's edit log tells it which task made each change.
3. **Grouping.** Related edits become one review item, such as "add input
   validation", rather than being shown file by file.
4. **Flags.** [Review agents](#review-agents) flag items worth a closer look.
   Flagged items are sorted high-severity first; nothing is hidden.

### What you see for each change

| Question | Where the answer comes from |
|---|---|
| What does it do? | The diff and surrounding code |
| What was it supposed to do? | Your request in the session, your project notes and goal |
| What was there before? | The original code |
| Why was it changed? | What you asked and what Claude said when it made the edit |

Every answer is labelled with its **source** — `request`, `claude`,
`notes:<file>`, `code`, `inferred` or `unknown`. When there is no record,
Debrief says so rather than inventing a reason.

### Reviewing

* **Items:** the list shows each item's decision, severity badge, title,
  files and the task that made it.
* **Diff:** unified, or side by side (old on the left, new on the right).
* **Ask the guide** what a change does, what was there before, or why it was
  made. Drag across lines in the diff to ask about exactly that code.
* **Decide:** **Approve**, **Needs fix…** (with a note), **Revert…** or
  **Skip**. You can also add a note to the project's notes, which future
  reviews read.

| Key | Action |
|---|---|
| `↓`/`j`, `↑`/`k` | Next / previous change |
| `a` `f` `r` `s` | Approve · needs fix · revert · skip |
| `n` | Add a project note |
| `v` | Unified / side by side |
| `/` | Ask the guide |
| `Esc` | Clear highlight / close dialog |
| `?` | Help (desktop) · `t` theme, `F` finish (terminal) |

In the terminal, press `Tab` to move into the diff, then `Shift+↑/↓` (or drag
with the mouse) to highlight lines; `←`/`→` pick the side in split view.

### Finishing

**Finish review** writes a record of every decision to
`.carrier/reviews/<id>/record.md` and marks this point as reviewed in every
folder. Fixes and reverts become `fixes.md`. They're also added to the
project as its next task, *Fix review findings* — **Open in Flight Deck**
goes straight to it.

**Automatic preparation:** `carrier debrief init --hook` adds a Claude Code
*Stop hook*, so a review is prepared in the background each time Claude Code
finishes.

---

## Review agents

Review agents are the reviewers Debrief runs over each change. Each one looks
for one kind of problem and flags changes you should look at closely; none of
them can change files. They're managed in the Ready Room under **Review
agents**.

### Built-in agents

| Agent | Flags | Default severity |
|---|---|---|
| Drifts from the project notes | Changes that contradict the project's notes, goal or rules | High |
| Touches something hard to undo | Changes that are costly to get wrong or hard to reverse, such as public interfaces other code depends on | Medium |
| Unrequested changes | Edits beyond what you asked for | Medium |
| Security-sensitive | Secrets in code, and commands, SQL or file paths built from untrusted input | High |
| Hides errors | Errors caught and ignored, or turned into log messages without being handled | Medium |
| Tests removed or weakened | Tests deleted, skipped or made easier to pass | High |

### Settings for each agent

* **On or off** — switch any built-in off for every project, or just one.
* **Flags as** — how serious its findings are: high, medium or low. Flagged
  changes are sorted high first; an agent may still raise or lower the level
  for a particular change.
* **Model** — which Claude model runs it. A bigger model notices more and
  costs more.
* **Effort** — how hard the model thinks. More effort is more thorough,
  slower and costs more; an agent that browses the code may also look
  further.

Each setting is made once for all projects and can be changed for any one
project; a project shows the value it inherits. For example, run
*Security-sensitive* on Opus at high effort for a project that handles user
data, and on Sonnet at low effort for a throwaway tool.

### Your own agents

Add agents for every project or a single project. Each has a name, what to
look for, the three settings above, and options to read the project's notes,
see Claude's own explanations, or browse the code (read only). **✨ Draft with
Claude** can write the instructions from a short description.

Agents written as markdown files in a folder's `.carrier/criteria/` still
work too — see [writing-criteria.md](writing-criteria.md) and
[examples/criteria/](../examples/criteria/).

---

## Models and effort

Model and effort can be set for Flight Deck sessions and for each review
agent. Carrier gets the list of models from Claude Code itself.

* **Model list:** at startup Carrier asks Claude Code which models it offers
  and which effort levels each supports. It only connects and disconnects,
  so it costs nothing. The answer is saved for the next start.
* **Effort levels:** low, medium, high, extra high and max.
* **Your choice is kept.** If a model doesn't offer the level you chose, it
  runs at the closest lower level and a note says so ("runs at High"). If a
  model has no effort setting, the field is greyed out but still editable,
  and your choice applies again when you switch back.

| Place | Model | Effort | Applies to |
|---|---|---|---|
| Session preset | Yes | Yes | Every session started with the preset, or with a project that uses it by default |
| Start dialog | Yes | Yes | This session only; overrides the preset |
| Session header | Yes, from the next message | Yes, reconnects the same conversation | The running session, while Claude is waiting |
| Review agents | Yes | Yes | One agent, for every project or one project |

For a session, settings come from the first of: your choice in the start
dialog, the chosen preset, the project's default preset, then Claude Code's
own defaults.

---

## Draft with Claude

**✨ Draft with Claude** writes rules or a review agent from a description in
your own words; nothing is saved until you press Save. It appears next to
project rules, global rules, rule sets and a review agent's "What to look
for".

1. Describe what you want, for example *"no new libraries without asking;
   old saves must keep loading"*.
2. If the field already has text, choose whether Claude revises it or starts
   fresh.
3. **Write draft.** Edit the result freely, or **Write again**.
4. **Use this draft** puts it in the field. Review it there, then **Save**.

For project rules, the project's goal is given to Claude as context. For
review agents, the draft also suggests a name and a severity. Each draft is
one short request to Claude with no tools, so it can't read or change files.

---

## Terminal and command line

Debrief also runs in the terminal, and every tool has commands for scripting
and quick checks. Run them inside a project's folder.

```bash
pip install -e .              # or: pipx install .
carrier                       # list the tools
carrier doctor                # check git, Claude Code and the Agent SDK
carrier app                   # open the desktop app
carrier debrief               # review since last time   (shortcut: debrief)
carrier debrief --from HEAD~5 # from a specific point
carrier debrief --no-ai       # walk the diff without any model calls
```

| Command | What it does |
|---|---|
| `carrier debrief prepare` / `list` | Prepare a review without opening it / print its items and flags |
| `carrier debrief status` / `checkpoint` | Show what's unreviewed / show or move the last-reviewed point |
| `carrier debrief fixes` / `criteria` | Show the latest fix list / list review agents |
| `carrier debrief init --hook` | Set Debrief up and add the Claude Code Stop hook |
| `carrier readyroom` | List projects, tags and current tasks |
| `carrier flightdeck status` | List projects, links and sessions |
| `carrier flightdeck add` / `link` / `unlink` / `remove` | Manage projects and links |

* In a project's main folder, terminal reviews cover the same folders,
  project context and review agents as the app, so a review can be started in
  one and resumed in the other.
* Ctrl+C cancels while a review is being prepared.
* Flight Deck sessions run in the desktop app only.

---

## Accessibility

Review colours can be switched for colour-vision deficiencies, and nothing
relies on colour alone: changed lines keep their `+`/`−` markers and severity
badges always show their name.

| Theme (⚙ Settings) | For |
|---|---|
| Default (red / green) | Typical colour vision |
| Red–green safe | Deuteranopia and protanopia |
| Blue–yellow safe | Tritanopia |
| High contrast | Low vision, bright screens |
| Monochrome | No colour at all |

* Removed lines in the colour-safe themes are also struck through, and any
  single colour can be changed with the colour picker. The theme applies in
  the terminal too.
* The review is fully usable from the keyboard; Esc closes dialogs.
* Text boxes underline misspelled words. On Windows, right-click a word for
  suggestions, plus undo, cut, copy, paste and select all.

---

## Privacy and security

Carrier keeps everything on your computer. The only thing that talks to the
internet is Claude Code itself, which sends your prompts to Anthropic as it
always does.

* **No network code.** Carrier's core makes no network connections, enforced
  by `tests/test_isolation.py`. The default *secure* build contains no
  networking code at all.
* **No open ports.** The app window opens no network port: the page is loaded
  straight into the window, talks to Python through an in-process bridge,
  and a Content-Security-Policy blocks all network requests.
* **Review agents and drafts can't change anything.** Debrief's agents get
  read-only tools; "Draft with Claude" gets no tools at all. Debrief never
  edits your code — fixes go back to you as a task.
* **Flight Deck sessions are normal Claude Code:** they ask before acting
  unless you choose otherwise, exactly as in the terminal.
* **Model output is shown as text.** Claude's replies, file names and task
  text can never run as HTML or script in the app.
* **Projects only.** The window can only review your projects and Flight Deck
  sessions, not arbitrary folders.
* The planned **network edition** (access from other devices on your LAN)
  will live in a separate package that the standard build leaves out
  entirely — see [ARCHITECTURE.md](../ARCHITECTURE.md#editions-and-the-network-seam).

---

## Files Carrier writes

**In each project's main folder:**

```
.carrier/
  config.toml             project settings (optional)
  criteria/               review agents as markdown files (optional)
  notes.md                notes added during reviews
  reviews/<id>/           review.json, record.md, fixes.md
```

Git refs under `refs/carrier/` mark where the last review ended (one per
project) and keep review snapshots safe from `git gc`; your branches are
never touched.

**In your settings folder** — `%APPDATA%\Carrier` on Windows,
`~/Library/Application Support/Carrier` on macOS, `~/.config/carrier` on
Linux:

| File | Holds |
|---|---|
| `carrier.db` | Projects, tasks, rules, presets, templates, review agents, links, sessions and transcripts |
| `models.json` | The model list from Claude Code |
| `settings.json` | Theme, colours, overlap setting |
| `desktop.log` | Errors and diagnostics from the desktop app |
| `worktrees/`, `scratch/` | Own-copy worktrees and standalone scratch folders |

---

## Building a standalone app

Builds a folder with `Carrier` (the app) and `carrier-cli` (terminal) that
runs without Python. Run it on each OS you want to ship for:

```bash
pip install -e ".[build]"
python packaging/build.py            # secure edition → dist/
```

On Windows, after `Carrier.bat` has run once, double-click
**`build-windows.bat`**. Claude Code isn't bundled (it's ~280 MB and you
already have it). `--bundle-claude` includes it for a personal build only —
Claude Code isn't open source and must not be in builds you publish. Each
build includes `LICENSE` and writes a `BUILD-INFO.txt` with the network
check result.

---

## Troubleshooting

Most problems leave a line in `desktop.log` in your settings folder — check
that first.

| Problem | What to do |
|---|---|
| Carrier won't start | Run `carrier doctor`, or check the setup checks on the Ready Room's start page |
| The window stops responding | Wait 20 seconds — Carrier writes what it was doing to `desktop.log`. Then close it (Windows: end `pythonw.exe` in Task Manager) |
| A project isn't in Debrief | Switch tabs to refresh. If it says *needs setup*, click it and **Start tracking** its folders |
| "Nothing to review" | Everything since the last review has been reviewed. Choose an earlier starting point to look again |
| A model rejects an effort level | Pick a lower effort or a bigger model |
| No right-click menu (Windows) | Check `desktop.log` for `[edit menu]` lines; `CARRIER_NO_EDIT_MENU=1` turns the menu off |
| The model list looks out of date | Restart Carrier; it asks Claude Code again at startup (`[models]` in the log) |

---

## Planned features and known issues

This section tracks what's coming and anything that doesn't work as it
should yet.

### Planned features

| Feature | What it adds |
|---|---|
| Local network access | Use Carrier from other devices on your network. An optional build that can be left out entirely |
| Right-click menu on macOS and Linux | Spelling suggestions and copy/paste in text boxes, as on Windows |

### Known issues

None reported yet. Found one? Please [open an issue](../../../issues).

### License

Carrier is licensed under the [GNU AGPL v3.0](../LICENSE). Use it, change it
and share it freely; if you distribute a modified version, or run one that
others use over a network, its source must be available to them under the
same license.

### Known limitations

* Reference folders block Claude's file-editing tools, but a shell command
  could still write there.
* Flight Deck sessions run in the desktop app only, not in the terminal.
* The right-click spelling menu is Windows only.
* Some models may not accept every effort level; Carrier maps effort to what
  each model reports it supports.
