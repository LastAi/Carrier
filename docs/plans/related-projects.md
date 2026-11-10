# Plan: related projects and tasks across projects

Status: **built** (November 2026) — see *Tasks across projects* in the GUIDE. Kept as the design record.

## The need

Some projects depend on each other (Carrier already lets you link them: see "Linked projects" in the GUIDE). A library defines a file format and an app
reads and writes files in it; a server defines an API and a client calls it.
When one side changes something the other relies on (the format gains a field)
the other side needs a task. Likewise, when the other side runs into a case the
first should handle, the first needs a task.

Today a Flight Deck session can *read* other projects (`list_projects`,
`get_project`), but `propose_work` only targets its own project's queues, and
`propose_task_change` refuses tasks from other projects. So the work is noticed
but can't be handed over.

## Decisions to make before starting

| Question | Options | Leaning |
|---|---|---|
| Which projects can a session propose work to? | Any project / only **related** projects | Related only: fewer stray tasks, and Claude knows *why* the other project cares |
| Should a task be able to *wait on* a task in another project? | Links only / links + "waiting on" | Links first (phase 1–3); waiting in phase 4 if wanted |

## Design

### 1. Related projects = the existing **Linked projects** (Ready Room)

Carrier already has **Linked projects**: two projects, a note on how they
relate, and the files each side shares (patterns, e.g. `schema/**/*.json` on
one side, `src/api-client/**` on the other). Flight Deck uses them to keep
sessions aware of each other's changes to those files. This feature builds on
them rather than adding a second kind of link:

* "Related project" below means a linked project.
* The link's card gets a clearer prompt for the note (*how do they depend on
  each other?*), and ✨ Draft with Claude from both projects' goals.
* Session context (`readyroom/context.py`): a **Linked projects** section —
  each linked project's name, the note, the shared-file patterns on both sides,
  and the instruction *"When your work changes something they rely on (their
  shared files), or needs something from them, propose work for them with
  `propose_work` (project=…). Don't edit their files."*
* A change to this project's shared files (the link's patterns) is the natural
  moment for Claude to propose work: the context says so explicitly.

### 2. Proposing work for a related project (Flight Deck)

* `propose_work` gains an optional `project` (name or id). Allowed only for a
  related project; otherwise the tool answers with the list of related projects
  and says to ask the user to link them in the Ready Room.
* `list_tasks` gains an optional `project` to read a related project's queues
  (read only), so Claude can suggest where the work fits there.
* The proposal card shows a **For: Editor** badge, the target project's queues
  in the "where" choice, and the usual edit / decline. Nothing is saved without
  the user's OK.
* Saved with `room.add_task(other_pid, …, by="claude", note=…, session_id=sid)`,
  so the other project's task history says *"proposed by a session of Mod
  (task #12), approved by you"*.
* The current session's transcript gets a notice: *"Opened task #31 in Editor."*

### 3. Task links

* New table `task_links (id, from_task, to_task, kind, created, session_id)`;
  `kind` = `opened` (phase 2) or `waits_on` (phase 4). Cascade on task delete.
* Ready Room task details show **Linked tasks**: *"Opened from Mod #12 —
  Schema v3"*, *"Opened Editor #31"*, each a link that jumps to that project
  and task.
* Session context for a task with links lists them (title, project, status), so
  the Editor's session knows *why* the task exists and can read the Mod task's
  handoff notes.
* Debrief: a review item whose session opened a task elsewhere shows it
  (*"opened Editor #31"*), so the reviewer sees the hand-off.

### 4. Waiting on another project's task (optional)

* `propose_work` / the card get **This task waits for it** (off by default).
* A waiting task shows *"waiting on Editor #31 (queued)"* in queues and on
  take-off; Flight Deck asks before starting it while the other is open.
* When the other task is done, the waiting task's project gets a notice, and
  an open session on it gets a message.
* Auto take-off skips waiting tasks.

### 5. Debrief hand-offs

* A review's **Request a fix** and a health finding's **Make a task** get
  **in: this project ▾ / a related project** — for a finding that belongs to the
  other side. Same `task_links` record (`opened`).

## Ownership and safety

* The proposed task lives in the other project; its sessions work in their own
  folders. Nothing about folder ownership changes: the proposing session still
  can't edit the other project's main folder.
* Task text written by Claude is marked as such (as for health-check tasks),
  so the other side verifies it before acting.
* No networking; everything stays in `carrier.db`.

## Phases

| Phase | What | Rough size |
|---|---|---|
| 1 | Linked projects in the session context; note prompt and Draft | small |
| 2 | `propose_work(project=…)`, `list_tasks(project=…)`, proposal card | medium |
| 3 | `task_links` (`opened`), linked tasks in the Ready Room, context, Debrief | medium |
| 4 | `waits_on`, take-off checks, notices (if wanted) | medium |
| 5 | Debrief hand-offs to a related project | small |

Each phase: tests (unit + a Playwright test of the card), GUIDE + CHANGELOG,
an independent review, then sync.

## Tests to write

* Context lists linked projects, their shared files and the rule not to edit
  their files.
* `propose_work(project=…)`: refused for unrelated projects; card shows the
  target's queues; approved → task in the other project with history and a
  `task_links` row; declined → nothing saved.
* `list_tasks(project=…)` read only and related-only.
* Linked tasks shown both ways; jumping between projects in the Ready Room.
* (Phase 4) waiting tasks skipped by auto take-off; notice when the other
  task is done.
