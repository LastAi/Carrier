# Architecture

This document is the map. If you are about to change Carrier, read this
first; each module also starts with a docstring explaining its job.

## The three tools

Carrier is named after a carrier flight operation. Each stage is its own
package under `src/carrier/`, sharing code through `carrier.core`:

```
 Ready Room  ──tasks──►  Flight Deck  ──changes──►  Debrief
 (readyroom/)            (flightdeck/)               (debrief/)
 projects: goal, rules,  Claude Code sessions on     human-led review of
 task queue, presets     those tasks, in parallel    what changed
                └───────────── carrier.core ─────────────┘
          git · model backend · config · notes · themes · user settings
```

Layering rules:

* `carrier.core` never imports a tool package.
* Tools may import `carrier.core`; a tool imports another tool only through
  that tool's public session API (e.g. Flight Deck handing a finished
  session to `carrier.debrief.session`).
* Code moves into `core` when a second tool needs it — not before.
* One desktop app (`carrier.desktop` + `carrier.web`) hosts all tools as
  tabs; each tool also has its own terminal commands (`carrier <tool>`).

## Ready Room and the task model

```
 Ready Room (readyroom/manager.py) ──writes──►  carrier.db  ◄──reads/writes── Flight Deck
   projects · tasks · rule sets ·                (core/db.py, SQLite, WAL)      sessions · edits ·
   presets · templates · links · tags                                          events · messages
                       readyroom/context.build(project, task, preset) ──► appended to the
                       Claude Code system prompt of each session
```

* **Project** = folder + long-term goal + rules + rule sets + default preset
  + tags (visual only, never sent to Claude) + links.
* **Task** belongs to a project (`project_id`) or is **standalone**
  (`project_id` NULL, own `cwd`). Status: `planned → active → done`. The
  first open task by `position` is the project's *current task*. `active`
  means a live session works on it; ending the last such session puts it
  back to `planned` (`FlightDeck._release_task`).
* **Done** tasks keep a `summary` (written by the user, or by Claude when
  asked via `FlightDeck.complete_task`) and form the project history; the
  last few go into the context text so later sessions know what happened.
* **Standalone → project:** `upgrade_standalone` makes the task's folder a
  project and the task its first history entry. `assign_standalone` records
  a done task in an existing project (`origin = 'standalone'`,
  `origin_path`) and can queue an "Integrate standalone work" task first.
  Files are never moved.
* **Session settings** resolve: explicit choice → chosen preset → project's
  default preset → Claude Code defaults. A preset's overlap policy is stored
  per session (`sessions.policy`).
* `core/db.py` is shared because two tools need it. Flight Deck uses Ready
  Room only through `ReadyRoom` and `context.build`.

## Flight Deck: how coordination works

```
 desktop page ──fd_* calls──► FlightDeck (manager.py) ── owns one asyncio loop thread
      ▲                          │   SessionRunner per session ── ClaudeSDKClient ── claude CLI
      └──── poll(seq) ◄── event stream (seq, session, event)
                                 │
            hooks (in-process) ──┼── UserPromptSubmit → coordination.briefing → additionalContext
                                 ├── PreToolUse(edits) → check_edit → warn / ask / deny (policy)
                                 └── PostToolUse(edits) → record_edit → events for linked sessions
                                 ▼
                        carrier.db (SQLite, WAL — core/db.py)
```

* **Normal Claude Code.** Sessions use the `claude_code` system-prompt preset
  and load user/project/local settings, so `CLAUDE.md`, permission rules and
  hooks apply as in the CLI. Flight Deck only appends a short note
  (`sdk.SYSTEM_APPEND`) and adds its hooks and a permission callback.
* **Tracking is automatic**: edits are recorded from the real tool calls
  (PostToolUse), never from what Claude says it did.
* **Relatedness** = same project, or projects joined by a link. Unlinked
  projects are never mentioned to each other.
* **Contracts** are globs on a link; editing a contract file queues an
  event for every active session in the linked project(s), delivered on
  that session's next prompt (and shown immediately in the UI).
* **Policy** (`warn` default, `ask`, `block`, `inform`) decides what a
  PreToolUse conflict does; it's a per-user setting.
* **Own copies**: `git worktree add` in the user config folder on branch
  `carrier/<id>-<slug>`; removal keeps the branch.
* **Why SQLite** rather than a manifest file: concurrent writers from
  several sessions, atomic updates, crash safety — stdlib, no server.
* **Debrief handoff**: each session snapshots its folder at start
  (`refs/carrier/flightdeck/<id>`); "Debrief this" reviews from there.
* The page polls `fd_poll(seq)` (250 ms while visible). Pull, not push,
  keeps the bridge simple and is what a network front end would use too.

## Debrief: the big picture

Debrief is a **pipeline** that produces one JSON file (a `Review`), plus a
**front end** that walks a human through it.

```
            prepare (pipeline.py)                         review (tui/)          finish (record.py)
 ┌──────────────────────────────────────────────┐   ┌──────────────────────┐   ┌─────────────────────┐
 │ base ─► snapshot ─► diff ─► notes+transcripts │   │ explain (guide.py)   │   │ record.md           │
 │   ─► segment ─► link events ─► criteria ─► rank│──►│ ask     (guide.py)   │──►│ fixes.md            │
 │                         └──► review.json      │   │ decide  (models)     │   │ move checkpoint     │
 └──────────────────────────────────────────────┘   └──────────────────────┘   └─────────────────────┘
```

* **Prepare** is non-interactive and can run in the background (the Claude
  Code Stop hook triggers it). It is the expensive part: grouping and
  criteria agents.
* **Review** is interactive. The guide explains items on demand; decisions
  are saved after every keypress.
* **Finish** writes the human-readable record, the fix list, and advances
  the checkpoint.

All state lives in `.carrier/reviews/<id>/review.json`. Nothing is held only
in memory, so any step can be resumed.

### Project reviews

The desktop app reviews Ready Room **projects** (`debrief/projects.py` builds
a `ProjectSpec`; `ReviewSession(project=...)` runs it):

* **Folders.** The main folder is `Review.repo`; each extra folder is a
  `Review.parts` entry with its own base, snapshot and checkpoint. Its files
  are prefixed `<label>/` and hunk ids are numbered across all folders, so
  every later stage works on one list. Finish moves every checkpoint;
  staleness and snapshot refs are tracked per folder.
* **Context.** `readyroom.context.build(for_review=True)` (goal, rules,
  recent history) is added as the note `carrier:project` for all agents.
* **Tasks.** `task_lookup` reads Flight Deck's `edits` table (same
  `<label>/<path>` naming) to fill `ChangeItem.tasks`.
* **Agents.** `effective_criteria`: built-ins (minus those switched off
  globally or for the project) < `.carrier/criteria/*.md` < global Ready
  Room agents < the project's agents (`review_agents` table).
* **Fixes.** `finish()` adds a "Fix review findings" task to the front of
  the project's queue (after any task already in progress).

The terminal commands and the Stop hook use `project_kwargs(repo)`, so a
review prepared there matches one started in the app.

## Front ends

```
                 ┌──────────────────────────── engine (pipeline, agents, store) ─┐
                 │                         session.ReviewSession                  │
                 │                  (plain JSON in / JSON out, thread-safe)       │
                 └──────────▲──────────────────────▲───────────────────▲──────────┘
                            │                      │                   │ (future, optional)
          tui/ (Textual, async, in-process)   desktop/api.py      carrier_net/ (HTTP + pairing)
                                               ▲ in-process JS bridge     ▲ LAN
                                          web/ page in a pywebview window  web/ page in a browser
```

* **Terminal UI** (`tui/`) — Textual. Calls the async engine directly.
* **Desktop app** (`desktop/` + `web/`) — a native window (pywebview:
  WebView2 on Windows, WebKit on macOS, GTK/Qt on Linux) showing the plain
  HTML/JS page in `web/`. The page calls `DesktopApi`, which wraps
  `ReviewSession`.
* **Network server** (planned, optional) — would serve the same `web/` page
  and forward the same calls to `ReviewSession` over HTTP. Because every
  session method already takes and returns JSON, the server adds transport
  and authentication only — no review logic.

Shared by all front ends: `sidebyside.py` (diff rows for unified and split
views, and turning highlighted rows into a `Selection`), `themes.py`
(palettes, including colour-blind presets) and `usersettings.py`
(per-user theme, colours, view mode, recent projects).

## Modules

Paths are relative to `src/carrier/`.

| Module | Responsibility | Talks to a model? |
|---|---|---|
| `cli.py` | `carrier` command: dispatches to each tool | no |
| **core/** | **shared by all tools** | |
| `core/config.py` | defaults + `.carrier/config.toml` | no |
| `core/gitutil.py` | snapshot commits, diff parsing, checkpoint/snapshot refs | no |
| `core/diffmodel.py` | `Hunk`, `FileChange` | no |
| `core/notes.py` | collect project notes; append reviewer notes | no |
| `core/llm.py` | `Backend` interface; Agent SDK backend; fake backend for tests | yes (the only place) |
| `core/jsonutil.py` | tolerant JSON extraction from model replies | no |
| `core/themes.py` | colour palettes incl. colour-blind presets; custom overrides | no |
| `core/usersettings.py` | per-user settings file (OS config folder) | no |
| `core/terminal.py` | open a terminal running Claude Code on a task | no |
| `core/db.py` | shared SQLite store (`carrier.db`): projects, tasks, rule sets, presets, templates, links, sessions, edits, events, transcripts; migrations | no |
| **debrief/** | **the review tool** | |
| `debrief/models.py` | `Review`, `ChangeItem`, `Flag`, `Decision`, `Selection`, … | no |
| `debrief/transcripts.py` | parse Claude Code session JSONL → `EditEvent`s; link to items | no |
| `debrief/prompts.py` | **every Debrief prompt** in one place | — |
| `debrief/segment.py` | group hunks into logical items (with deterministic fallback) | yes |
| `debrief/criteria.py`, `criteria_builtin/` | load criteria markdown (built-in + project) | no |
| `debrief/flagging.py` | run criteria agents in parallel batches → `Flag`s | yes |
| `debrief/health.py`, `health_builtin/` | health checks: periodic whole-project agents, when they are due, findings compared run to run | yes |
| `debrief/ranking.py` | dedupe flags, order items | no |
| `debrief/guide.py` | explain an item; answer questions | yes |
| `debrief/pipeline.py` | orchestrates *prepare* (read this one first); cancel-safe | via the above |
| `debrief/store.py` | atomic save/load of reviews; review lifecycle | no |
| `debrief/record.py` | `record.md`, `fixes.md`, checkpoint on finish | no |
| `debrief/hooks.py` | Stop hook install/remove, lock, background process | no |
| `debrief/session.py` | **Debrief's front-end API**: open folder or project, start/cancel/resume, items, ask, decide, finish | via the above |
| `debrief/projects.py` | project reviews: `ProjectSpec`, review agents (Ready Room), task lookup, fix task | no |
| `debrief/sidebyside.py` | diff → display rows (unified / split); rows → `Selection` | no |
| `debrief/cli.py` | `carrier debrief …` / `debrief …` commands | no |
| `debrief/tui/` | Textual UI; scrollable diff with line selection | via `guide.py` |
| **flightdeck/** | **running Claude Code sessions in parallel** | |
| `flightdeck/coordination.py` | conflict rules (`check_edit`), edit recording + contract notices, briefings | no |
| `flightdeck/sdk.py` | **the only Flight Deck module importing the Agent SDK**: options, hook outputs, message → event | — |
| `flightdeck/runner.py` | one live session: SDK client, streaming, permission prompts, in-process hooks | yes (Claude Code) |
| `flightdeck/manager.py` | **Flight Deck's front-end API**: start/complete tasks, standalone tasks, sessions, poll() event stream | via runner |
| `flightdeck/worktrees.py` | "own copy" git worktrees for same-project sessions | no |
| `flightdeck/cli.py` | `carrier flightdeck …` (projects, links, status) | no |
| **readyroom/** | **setting projects up** | |
| `readyroom/manager.py` | **Ready Room's front-end API**: projects, tasks, links, rules, rule sets, presets, templates, tags | no |
| `readyroom/context.py` | goal + task + rules + history → the text added to Claude Code's system prompt | no |
| `readyroom/ownership.py` | folder ownership: main folders, shared spaces and their parts, who a file belongs to (used by Flight Deck and Debrief) | no |
| `readyroom/drafting.py` | "Draft with Claude": rules and review agents from a description (one tool-less call) | yes |
| **desktop/** + **web/** | | |
| `desktop/api.py` | methods the desktop page may call (`{ok, data}` / `{ok, error}`) | via the sessions |
| `desktop/main.py` | opens the window; inlines the page; **no HTTP server** | no |
| `web/` | the page: `index.html`, `app.css`, `app.js` (no build step); one tab per tool | no |

Rule of thumb: **only `core/llm.py` (one-shot calls) and `flightdeck/sdk.py`
(interactive sessions) import the Agent SDK**, and only the agent
modules (`segment`, `flagging`, `guide`, `health` and `readyroom/drafting`) call `Backend.ask`. Everything else
is deterministic and unit-tested without a model.

## Key design decisions

### Snapshot instead of "working tree"
Claude Code usually leaves changes uncommitted. Reviewing the live working
tree would let hunks shift while the human reads. `gitutil.snapshot_worktree`
records the exact state as a commit object using a temporary index — the
user's index, files and branches are untouched. The checkpoint
(`refs/carrier/debrief/last`) is set to that snapshot on finish, so the next review
starts exactly where this one ended, including uncommitted work.

### Transcripts are the source of "why"
Claude Code stores every session as JSONL under
`~/.claude/projects/<project-key>/`. Each file edit there is preceded by the
user's request and usually a sentence from Claude about what it is doing.
`transcripts.py` turns these into `EditEvent`s and links them to items by
file path and by matching the text Claude wrote against the diff's added
lines. The format is undocumented, so parsing is forgiving: unknown lines
are skipped.

### Sources are mandatory
The guide's prompt requires a source tag for intent and history (`request`,
`claude`, `notes:<path>`, `code`, `inferred`, `unknown`). This is the main
defence against confident, invented explanations. Please keep it when
editing prompts.

### Independent reviewers
Criteria default to `sees_rationale: false`: the agent sees what the *user
asked for*, not Claude's justification. Authors are persuasive about their
own changes; reviewers shouldn't start from the author's framing.

### Batching for cost
Criteria run per *batch of items* (default 40 KB of diff), not per item, so
a typical review costs about one call per criterion, all in parallel.

### Ask about highlighted lines
Both UIs render the same `sidebyside.Row`s, so a highlighted range of rows
maps to the same `Selection` (file, side, line numbers, text) everywhere.
The selection is sent with the question (`GUIDE_SELECTION_BLOCK` in
`prompts.py`) and stored on the `ChatTurn`, so the record shows what each
question was about.

### Colour is never the only signal
Themes change colours only. Markers (`+`/`−`), strikethrough of removed text
(in the accessible presets), severity names and letter badges (H/M/L)
always carry the same information, so every theme — including monochrome —
stays usable.

### Untrusted text in the desktop page
Diff text and model answers are untrusted. `app.js` inserts them with
`textContent`, or via its small `md()` renderer, which escapes all HTML
first and only then adds a fixed set of harmless tags. The browser test
feeds the page a malicious answer to prove it stays inert.

### Never block, never write
Every model failure degrades to a safe fallback (per-file grouping, no
flags, "guide failed" message) plus a warning. Agents get read-only tools;
Debrief never edits the user's code.

### No recursion
Agent calls run Claude Code with `setting_sources=[]` (user hooks are not
loaded) and `DEBRIEF_INNER=1` (Debrief's own prepare exits immediately).
Without this the Stop hook would trigger itself.

## Debrief review lifecycle

```
prepared ──open in UI──► in_progress ──F finish──► done
    │                         │
    └──newer prepare──►  superseded  ◄──another review finished first
```

`debrief` resumes an `in_progress` review if there is one; otherwise it
prepares (reusing a `prepared` review whose diff fingerprint still matches).

**Checkpoint safety.** Each review stores the checkpoint it was prepared
against (`checkpoint_at_prepare`). Finishing a review supersedes every
other open review. If a review is finished after the checkpoint moved
anyway, it is recorded but the checkpoint is **not** moved — so the
checkpoint can never go backwards and re-show reviewed work.

**Snapshot pinning.** Each open review's snapshot commit is pinned by
`refs/carrier/debrief/reviews/<id>` so `git gc` cannot delete it; the pin is
dropped when the review is finished or superseded (the checkpoint ref then
keeps the finished snapshot alive).

**Cancel.** `ReviewSession.cancel_start()` cancels the running prepare
task (and with it the agents' Claude Code calls). `pipeline.prepare` writes
the review only at the very end and unpins the snapshot if stopped earlier,
so a cancelled start leaves nothing behind.

**Prepare lock.** `.carrier/prepare.lock` holds the owner's PID and is
touched every 20 s while a prepare runs; a lock untouched for 3 minutes is
treated as abandoned. Only the owner removes it.

## Editions and the network seam

Network features are **optional and removable at build time**:

* The `carrier` package (all tools and core) contains no networking code. This is a
  tested contract (`tests/test_isolation.py`): no imports of socket servers
  or HTTP libraries, no network calls from the page, a strict
  Content-Security-Policy, the desktop window created with pywebview's
  HTTP server off, and a full review run with `socket.bind`/`listen`
  disabled.
* Network features will live in a separate top-level package,
  `src/carrier_net/`, that depends on `carrier` — never the other way round.
  It will serve `web/` over HTTP and forward calls to `ReviewSession`,
  with pairing-code authentication and binding to localhost unless LAN
  access is explicitly enabled.
* **AGPL section 13:** a network edition lets people use Carrier remotely,
  so its page must offer those users the source of the exact version they
  are using (for example a "Source" link in the help dialog pointing at the
  repository and version).
* `packaging/build.py --variant secure` (the default) excludes
  `carrier_net` and scans the finished bundle to prove it is absent;
  `--variant network` includes it.

Honest caveat: pywebview contains a small HTTP server module of its own,
which it imports. Carrier never starts it, but the code is present in every
edition; `BUILD-INFO.txt` says so.

## Packaging

`packaging/build.py` runs PyInstaller with a generated spec that produces
two executables sharing one runtime: `Carrier` (windowed app) and
`carrier-cli` (console). In a compiled build there is no `python -m`, so
`debrief.hooks.self_command()` points the Stop hook and background prepares
at `carrier-cli debrief`. Claude Code itself (~280 MB inside the Agent SDK) is left out
by default; the SDK then uses the `claude` on PATH. PyInstaller cannot
cross-compile, so `.github/workflows/build.yml` builds on each OS.

For running from source, `Carrier.bat` (Windows) and `carrier.sh`
(macOS/Linux) create a `.venv` on first run and open the app.

## Testing

`pytest` runs everything offline. `tests/conftest.py` builds a throwaway git
repo, a fake `CLAUDE_CONFIG_DIR` with a transcript, a `FakeBackend` whose
`scripted_handler` answers each agent type, and points per-user settings at
a temp folder.

* `test_tui.py` drives the terminal UI with Textual's pilot.
* `test_desktop.py` loads the real page in Chromium (Playwright) with a shim
  that forwards `window.pywebview.api` calls to a real `DesktopApi`.
  Install the browser once with `playwright install chromium`; the test is
  skipped if it is missing.
* `test_isolation.py` is the network contract described above.

## Roadmap

- **Ready Room**: planning help with Claude (break a goal into tasks), drag-to-reorder tasks.
- Flight Deck: merge an own-copy branch from the UI; image attachments; per-session policy.

Ideas, roughly in priority order — open an issue before starting a big one:

- **Escalation:** run criteria on a fast model first, re-check flagged
  items with a stronger one.
- **Dependency ordering:** show foundational changes before the code that
  uses them.
- **Apply reverts directly** (with confirmation) instead of only listing them.
- **Network edition** (`carrier_net`): LAN access from other devices with a
  pairing code — see "Editions and the network seam".
- **Keyboard range selection in the desktop diff** (mouse-only today).
- **Light theme** for the desktop app chrome.
- **Per-item model context:** let the guide open the transcript around an
  edit on request.
- **Other agents' transcripts** (Codex, Aider…) behind the same
  `EditEvent` interface.
